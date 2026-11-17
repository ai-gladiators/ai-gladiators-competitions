# SPDX-License-Identifier: MIT
"""The workload client of the Crossbar.io single-node target.

Each of the nine clients runs this program. It joins its realm, registers its own
`gladiators.<name>.echo`, subscribes to `gladiators.tick`, and then, driven by a
seed, calls its peers' echo procedures and publishes ticks. It checks every answer
and writes one JSON line per event to its log, from a thread of its own, each line
durable before the next (competitions#19).

Every tick carries a flag: 16 random bytes from the operating system, fresh per tick,
recorded in the publisher's own log together with the tick's sequence number
(competitions#21). A flag held by anyone who is not a member of the realm it was
published on shows that the realm's confidentiality was broken; the referee checks a
presented flag against the publishers' logs (referee/verify_flags.py).

    python workload.py --generate-key client04.key
    python workload.py --name client04 --realm application1 --key client04.key \\
        --peers client05,client06 --url wss://localhost:8080/ws --trust server.crt \\
        --seed 1 --log client04.jsonl

Without --key the client authenticates anonymously (realm `public`).
"""

import argparse
import asyncio
import atexit
import binascii
import json
import os
import queue
import random
import secrets
import ssl
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from urllib.parse import urlparse

from autobahn.asyncio.component import Component
from autobahn.wamp.exception import ApplicationError
from autobahn.wamp.types import PublishOptions, RegisterOptions, SubscribeOptions

PREFIX = "gladiators."
# While waiting for its peers, a client logs who is still missing this often (seconds).
PEER_WAIT_REPORT_S = 30.0
# A transport failure (router not up yet, connection refused, TLS) is retried at this
# pace at most (seconds): clients start in any order and find the router quickly.
TRANSPORT_MAX_DELAY_S = 5.0
# A refused join is retried after REFUSED_FIRST_DELAY_S, doubling with each refusal in a
# row, up to --refused-max-delay (competitions#10).
REFUSED_FIRST_DELAY_S = 5.0
TICK = PREFIX + "tick"
# A tick's flag: this many random bytes, as hex (competitions#21).
FLAG_BYTES = 16


def check_echo(result, payload, peer, my_authid):
    """The violation an echo result shows, or None. Pure, so --self-test can prove it fires."""
    if not isinstance(result, dict) or result.get("payload") != payload:
        return "payload changed"
    if result.get("callee") != peer:
        return "answered by another callee"
    if result.get("caller_seen") != my_authid:
        return "router reported another caller authid"
    return None


def refused_delay(attempt, cap):
    """Seconds before the next join after `attempt` refusals in a row: 5, 10, 20, ... up to cap."""
    return min(REFUSED_FIRST_DELAY_S * 2 ** (attempt - 1), cap)


def check_tick(tick, my_realm):
    """The violation a received tick shows, or None. Only the realm is judged; the
    other fields (name, seq, flag) are the publisher's and are not checked here."""
    if not isinstance(tick, dict) or tick.get("realm") != my_realm:
        return "tick from another realm"
    return None


def new_flag():
    """A tick's flag: from the operating system's generator (`secrets`), never from the
    workload's seeded `random.Random`, which repeats from its seed - a flag made by it
    could be predicted instead of captured."""
    return secrets.token_hex(FLAG_BYTES)


class Ticks:
    """The ticks of one process: `seq` counts from 1 and restarts with the process, so
    the flag, not the seq, identifies a tick."""

    def __init__(self, realm, name, flag=new_flag):
        self.realm = realm
        self.name = name
        self.seq = 0
        self._flag = flag

    def next(self):
        self.seq += 1
        return {"realm": self.realm, "name": self.name, "seq": self.seq, "flag": self._flag()}


async def publish_tick(session, log, ticks):
    """Publish the next tick and record it, with its seq and flag, in the publisher's log -
    also when the publish fails: a failed publish's flag may still have left the client.
    This record is the only place a flag is written (competitions#21)."""
    tick = ticks.next()
    t0 = time.perf_counter()
    try:
        await session.publish(TICK, tick, options=PublishOptions(acknowledge=True, exclude_me=False))
    except Exception as e:  # noqa: BLE001 - every failure is a record, not a crash
        log.write(
            "publish",
            latency_ms=round((time.perf_counter() - t0) * 1000, 3),
            ok=False,
            error=getattr(e, "error", None) or type(e).__name__,
            seq=tick["seq"],
            flag=tick["flag"],
        )
        return
    log.write(
        "publish",
        latency_ms=round((time.perf_counter() - t0) * 1000, 3),
        ok=True,
        seq=tick["seq"],
        flag=tick["flag"],
    )


def event_fields(tick, details, my_realm):
    """The record of a received tick: who published it, on which realm, and whether that
    is this client's realm. Never the flag: a subscriber's log is not where flags are
    recorded, so a flag found in a log is always the publisher's (competitions#21)."""
    violation = check_tick(tick, my_realm)
    return {
        "peer": tick.get("name") if isinstance(tick, dict) else None,
        "from_realm": tick.get("realm") if isinstance(tick, dict) else None,
        "publisher_seen": details.publisher_authid if details else None,
        "ok": violation is None,
        "violation": violation,
    }


async def workload_loop(session, left, rng, rate, peers, call_once, publish_once, stopping):
    """The workload: sleep, call a peer, maybe publish - until the client stops or the
    session leaves (competitions#15).

    After a `leave` the client issues no call and no publish: `left` (set by on_leave)
    wakes the sleep, and the session is checked after the sleep and again before the
    publish. Nothing can detach the session between a check and the request it guards:
    no `await` lies between them. A call sent before the `leave` is not cancelled: it
    fails with the session and keeps its record. The random draws are the same as
    before this change, so a seed still gives the same calls."""
    while not stopping() and session.is_attached():
        try:
            await asyncio.wait_for(left.wait(), timeout=rng.expovariate(rate))
            return  # the session left during the sleep
        except TimeoutError:
            pass
        if stopping() or not session.is_attached():
            return
        if peers:
            await call_once(session, rng.choice(peers))
        if rng.random() < 0.5:
            if stopping() or not session.is_attached():
                return
            await publish_once(session)


class _FakeRng:
    """A fixed workload for the self-test: one delay, always the first peer, always publish."""

    def __init__(self, delay):
        self.delay = delay

    def expovariate(self, rate):
        return self.delay

    def choice(self, seq):
        return seq[0]

    def random(self):
        return 0.0


class _FakeSession:
    """A session as the workload sees it. lose() does what autobahn does on a lost
    transport, in its order: fail the outstanding requests, detach, fire leave."""

    def __init__(self, hang=False):
        self.attached = True
        self.hang = hang
        self.pending = []

    def is_attached(self):
        return self.attached

    async def request(self):
        if not self.attached:
            raise RuntimeError("TransportLost")
        if self.hang:
            fut = asyncio.get_running_loop().create_future()
            self.pending.append(fut)
            await fut

    def lose(self, left):
        for fut in self.pending:
            fut.set_exception(ApplicationError("wamp.close.transport_lost"))
        self.pending.clear()
        self.attached = False
        left.set()


def _loop_test(hang, delay, lose_after):
    """Run workload_loop on a fake session that loses its transport after lose_after
    seconds; return the records [(op, ok, sent_before_leave)] and the seconds it ran."""

    async def run():
        session, left, records, leave_at = _FakeSession(hang), asyncio.Event(), [], []

        async def request(op):
            sent_before_leave = not leave_at
            try:
                await session.request()
                records.append((op, True, sent_before_leave))
            except Exception:  # noqa: BLE001 - as call_once/publish_once: a failure is a record
                records.append((op, False, sent_before_leave))

        def lose():
            leave_at.append(True)
            session.lose(left)

        loop = asyncio.get_running_loop()
        loop.call_later(lose_after, lose)
        t0 = time.monotonic()
        await workload_loop(
            session, left, _FakeRng(delay), 2.0, ["client02"],
            lambda s, peer: request("call"), lambda s: request("publish"), lambda: False,
        )
        return records, time.monotonic() - t0

    return asyncio.run(run())


def _log_test():
    """competitions#19: order, drain, off the loop, the failure path, O_DSYNC."""
    import fcntl

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "t.jsonl")
        # order and drain: every record arrives, complete, in order, when close() returns
        log = Log(path, "client04", "application1")
        assert fcntl.fcntl(log._fd, fcntl.F_GETFL) & os.O_DSYNC, "log not opened O_DSYNC"
        for i in range(2000):
            log.write("call", ok=True, i=i)
        log.close()
        with open(path) as f:
            recs = [json.loads(line) for line in f]
        assert [r["i"] for r in recs] == list(range(2000)), "records lost or out of order"
        assert all(r["name"] == "client04" and r["realm"] == "application1" for r in recs)

        # off the loop: with storage taking 20 ms per write, write() does not wait
        def slow_write(fd, data):
            time.sleep(0.02)
            return os.write(fd, data)

        log = Log(os.path.join(d, "slow.jsonl"), "c", "r", _os_write=slow_write)
        t0 = time.monotonic()
        for i in range(50):
            log.write("call", ok=True, i=i)
        took = time.monotonic() - t0
        assert took < 0.02, f"write() waited for storage ({took:.3f} s for 50 records)"
        assert log.backlog_max >= 2, log.backlog_max  # they queued, and were counted
        log.close()
        with open(os.path.join(d, "slow.jsonl")) as f:
            assert [json.loads(line)["i"] for line in f] == list(range(50))

        # a short write is completed, not truncated
        def short_write(fd, data):
            return os.write(fd, data[: max(1, len(data) // 3)])

        log = Log(os.path.join(d, "short.jsonl"), "c", "r", _os_write=short_write)
        for i in range(20):
            log.write("call", ok=True, i=i)
        log.close()
        with open(os.path.join(d, "short.jsonl")) as f:
            assert [json.loads(line)["i"] for line in f] == list(range(20))

        # a failed write reaches on_error, and close() does not hang
        failures = []

        def failing_write(fd, data):
            raise OSError(5, "Input/output error")

        log = Log(os.path.join(d, "fail.jsonl"), "c", "r", on_error=failures.append, _os_write=failing_write)
        log.write("call", ok=True)
        t = threading.Thread(target=log.close)
        t.start()
        t.join(5)
        assert not t.is_alive(), "close() hung after a failed write"
        assert len(failures) == 1 and isinstance(failures[0], OSError), failures


def _flag_test():
    """competitions#21: each tick a fresh flag; the publish record holds the tick's seq
    and flag, a failed publish too; an event record never holds a flag."""
    import re

    hexflag = re.compile(rf"^[0-9a-f]{{{2 * FLAG_BYTES}}}$")
    ticks = Ticks("application1", "client04")
    sent = [ticks.next() for _ in range(1000)]
    assert [t["seq"] for t in sent] == list(range(1, 1001))
    assert all(hexflag.match(t["flag"]) for t in sent), "a flag is not 32 hex characters"
    assert len({t["flag"] for t in sent}) == len(sent), "a flag repeated"
    assert all(t["realm"] == "application1" and t["name"] == "client04" for t in sent)
    assert check_tick(sent[0], "application1") is None  # the new fields are accepted
    assert check_tick(sent[0], "application2") == "tick from another realm"
    # a seeded generator would make the same flags again; the flags must not follow a seed
    random.seed(1)
    a = Ticks("public", "client01").next()["flag"]
    random.seed(1)
    b = Ticks("public", "client01").next()["flag"]
    assert a != b, "the flag follows the seeded generator"

    class _Pub:
        def __init__(self, fail):
            self.fail = fail
            self.published = []

        async def publish(self, topic, payload, options=None):
            self.published.append((topic, payload))
            if self.fail:
                raise ApplicationError("wamp.error.not_authorized")

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "f.jsonl")
        log = Log(path, "client04", "application1")
        ticks = Ticks("application1", "client04")
        ok_session, failing_session = _Pub(False), _Pub(True)
        asyncio.run(publish_tick(ok_session, log, ticks))
        asyncio.run(publish_tick(failing_session, log, ticks))

        class _Details:
            publisher_authid = "client05"

        log.write("event", **event_fields(ok_session.published[0][1], _Details(), "application1"))
        log.close()
        with open(path) as f:
            recs = [json.loads(line) for line in f]
        pubs = [r for r in recs if r["op"] == "publish"]
        payloads = [ok_session.published[0][1], failing_session.published[0][1]]
        assert [p["ok"] for p in pubs] == [True, False], pubs
        assert [(p["seq"], p["flag"]) for p in pubs] == [(t["seq"], t["flag"]) for t in payloads], (pubs, payloads)
        assert ok_session.published[0][0] == TICK
        (event,) = [r for r in recs if r["op"] == "event"]
        assert "flag" not in event and "seq" not in event, event
        assert payloads[0]["flag"] not in json.dumps(event), "the flag reached an event record"
        assert event["ok"] and event["peer"] == "client04" and event["from_realm"] == "application1", event


def self_test():
    """Every check fires on the input it exists for, and stays quiet otherwise."""
    good = {"payload": b"x", "callee": "client05", "caller_seen": "client04"}
    assert check_echo(good, b"x", "client05", "client04") is None
    assert check_echo({**good, "payload": b"y"}, b"x", "client05", "client04") == "payload changed"
    assert check_echo({**good, "callee": "client06"}, b"x", "client05", "client04") == "answered by another callee"
    assert (
        check_echo({**good, "caller_seen": "client06"}, b"x", "client05", "client04")
        == "router reported another caller authid"
    )
    assert check_echo(None, b"x", "client05", "client04") == "payload changed"
    assert check_tick({"realm": "application1", "name": "client05"}, "application1") is None
    assert check_tick({"realm": "application2", "name": "client07"}, "application1") == "tick from another realm"
    assert check_tick("junk", "application1") == "tick from another realm"
    assert [refused_delay(n, 300) for n in range(1, 10)] == [5, 10, 20, 40, 80, 160, 300, 300, 300]
    assert [refused_delay(n, 12) for n in range(1, 4)] == [5, 10, 12]
    pace = Pace(_FakeTransport())
    assert pace.refused() == 5 and pace.refused() == 10 and pace.refused() == 20
    assert pace.transport.max_retry_delay == 20 and pace.transport.retry_delay * pace.transport.retry_delay_growth == 20
    pace.transport_failed()  # the router went away meanwhile: find it again quickly, keep the count
    assert pace.transport.max_retry_delay == TRANSPORT_MAX_DELAY_S and pace.attempt == 3
    assert pace.refused() == 40
    pace.joined()  # a join resets the count and the pace
    assert pace.attempt == 0 and pace.transport.max_retry_delay == TRANSPORT_MAX_DELAY_S
    assert pace.refused() == 5
    # competitions#15: no call or publish after `leave`
    records, took = _loop_test(hang=False, delay=10.0, lose_after=0.05)
    assert records == [] and took < 1.0, (records, took)  # left during the sleep: wakes, issues nothing
    records, took = _loop_test(hang=True, delay=0.001, lose_after=0.05)
    assert records == [("call", False, True)], records  # in flight at leave: failed once, nothing after it
    records, took = _loop_test(hang=False, delay=0.01, lose_after=0.2)
    assert records and all(ok and sent for _, ok, sent in records), records  # attached: it works
    assert {op for op, _, _ in records} == {"call", "publish"}, records
    _log_test()
    _flag_test()
    print("self-test OK: every check fires on its case")


def stack(url):
    """What this client actually runs on - measured, so every log says what was tested."""
    import platform
    import selectors
    from importlib.metadata import PackageNotFoundError, version

    def ver(dist):
        try:
            return version(dist)
        except PackageNotFoundError:
            return None

    scheme = urlparse(url).scheme
    loop = asyncio.new_event_loop()
    loop_name = type(loop).__name__
    loop.close()
    return {
        "python": f"{platform.python_implementation()} {platform.python_version()}",
        "openssl": ssl.OPENSSL_VERSION,
        "autobahn": ver("autobahn"),
        "txaio": ver("txaio"),
        "pynacl": ver("pynacl"),
        "cbor2": ver("cbor2"),
        "event_loop": f"asyncio {loop_name} / {selectors.DefaultSelector.__name__}",
        "transport": {"wss": "WebSocket over TLS", "rss": "RawSocket over TLS"}.get(scheme, scheme),
        "serializer": "cbor",
        "os": platform.platform(),
    }


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--self-test", action="store_true", help="prove the answer checks fire, then exit")
    p.add_argument(
        "--generate-key", metavar="FILE", help="create a Cryptosign key in FILE (mode 0600), print its public key, exit"
    )
    p.add_argument("--name", help="this client's name, e.g. client04")
    p.add_argument("--realm", help="the realm to join")
    p.add_argument(
        "--url", default="wss://localhost:8080/ws", help="wss://host:port/ws (WebSocket) or rss://host:port (RawSocket)"
    )
    p.add_argument("--trust", help="the router certificate to trust (PEM); nothing else is trusted")
    p.add_argument("--key", help="a Cryptosign private key file (none: anonymous)")
    p.add_argument("--peers", default="", help="comma-separated names of the other clients in this realm")
    p.add_argument("--seed", default="0", help="the workload seed; combined with --name")
    p.add_argument("--rate", type=float, default=2.0, help="mean calls per second")
    p.add_argument("--log", help="the JSON Lines log to append to")
    p.add_argument("--duration", type=float, default=None, help="stop after this many seconds (default: run forever)")
    p.add_argument(
        "--peer-wait",
        type=float,
        default=0.0,
        help="seconds to wait for every peer's echo before the workload starts; 0 (default): until all answer",
    )
    p.add_argument(
        "--stop-grace",
        type=float,
        default=5.0,
        help="after --duration: seconds to keep serving echo before leaving (peers may still be calling)",
    )
    p.add_argument(
        "--refused-max-delay",
        type=float,
        default=300.0,
        help="after a refused join, retry after 5 s, doubling up to this many seconds (default 300)",
    )
    args = p.parse_args(argv)
    if not (args.generate_key or args.self_test):
        for k in ("name", "realm", "trust", "log"):
            if getattr(args, k) is None:
                p.error(f"--{k} is required")
    return args


def generate_key(path):
    from nacl.signing import SigningKey

    key = SigningKey.generate()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(binascii.b2a_hex(key.encode()).decode() + "\n")
    pub = binascii.b2a_hex(key.verify_key.encode()).decode()
    print(pub)
    return pub


def read_key(path):
    with open(path) as f:
        return f.read().strip()


def _log_failed(exc):
    """A log that cannot be written ends the client: a match without its evidence is void."""
    print(f"workload: the log cannot be written ({exc}); stopping", file=sys.stderr, flush=True)
    os._exit(3)


class Log:
    """Append-only JSON Lines, written and made durable by a thread of its own (competitions#19).

    `write()` takes the record's timestamp when the event happens and only queues the
    record, so the event loop never waits for storage. The writer thread writes the
    records in order; the file is opened with O_DSYNC, so every write is durable when
    it returns, with no fsync. Linux's NFS client does not widen a write on such a
    file to the whole cached page, so each write should carry its own bytes only. Records that queued up while a write was in progress go out
    together in the next write.

    A crash loses the records still in the queue: normally the one in flight, more
    when storage is slow (the summary record says the deepest the queue got). A
    failed write calls `on_error` (default: end the process with exit code 3).
    """

    def __init__(self, path, name, realm, on_error=_log_failed, _os_write=os.write):
        self._fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_DSYNC, 0o644)
        self._base = {"name": name, "realm": realm}
        self.errors = 0
        self.violations = 0
        self.backlog_max = 0
        self._q = queue.SimpleQueue()
        self._on_error = on_error
        self._os_write = _os_write
        self._closed = False
        # A daemon, so an exception that skips close() cannot keep the process alive;
        # atexit still drains the queue on any normal exit (not on os._exit).
        self._thread = threading.Thread(target=self._writer, name="log-writer", daemon=True)
        self._thread.start()
        atexit.register(self.close)

    def write(self, op, **fields):
        rec = {"ts": datetime.now(UTC).isoformat(timespec="microseconds"), "op": op, **self._base, **fields}
        if rec.get("error"):
            self.errors += 1
        if rec.get("violation"):
            self.violations += 1
        self._q.put((json.dumps(rec, sort_keys=True) + "\n").encode("utf-8"))
        self.backlog_max = max(self.backlog_max, self._q.qsize())

    def _writer(self):
        while True:
            lines = [self._q.get()]
            while True:
                try:
                    lines.append(self._q.get_nowait())
                except queue.Empty:
                    break
            done = lines[-1] is None
            data = b"".join(line for line in lines if line is not None)
            try:
                while data:
                    data = data[self._os_write(self._fd, data) :]
            except Exception as exc:  # noqa: BLE001 - any failure ends the log, loudly
                self._on_error(exc)
                return
            if done:
                return

    def close(self):
        """Write everything queued, then close: when this returns, every record is durable."""
        if self._closed:
            return
        self._closed = True
        self._q.put(None)
        self._thread.join()
        os.close(self._fd)


class _FakeTransport:
    """The fields of autobahn's per-transport retry state that Pace uses (for --self-test)."""

    def __init__(self):
        self.max_retry_delay = TRANSPORT_MAX_DELAY_S
        self.initial_retry_delay = 1.5
        self.retry_delay = 1.5
        self.retry_delay_growth = 1.5


class Pace:
    """How soon the client tries again (competitions#10).

    autobahn's Component waits between attempts with its own, cancellable delay
    (comp.stop() ends it at once). Pace sets that delay: after a refused join, the
    next delay is refused_delay(attempt); after a transport failure, the fast pace.
    """

    FIELDS = ("max_retry_delay", "initial_retry_delay", "retry_delay", "retry_delay_growth")

    def __init__(self, transport, cap=300.0):
        missing = [f for f in self.FIELDS if not hasattr(transport, f)]
        if missing:
            # an autobahn that changed its retry state: refuse to run rather than retry blindly
            raise SystemExit(f"this autobahn's transport has no {', '.join(missing)}: the retry pace cannot be set")
        self.transport = transport
        self.cap = cap
        self.attempt = 0

    def _next(self, delay):
        # Component's next_delay() returns retry_delay * growth (jittered), capped at max_retry_delay
        self.transport.max_retry_delay = delay
        self.transport.retry_delay = delay / self.transport.retry_delay_growth

    def refused(self):
        self.attempt += 1
        delay = refused_delay(self.attempt, self.cap)
        self._next(delay)
        return delay

    def transport_failed(self):
        self.transport.max_retry_delay = TRANSPORT_MAX_DELAY_S
        self.transport.retry_delay = min(self.transport.retry_delay, self.transport.initial_retry_delay)

    def joined(self):
        self.attempt = 0
        self.transport.max_retry_delay = TRANSPORT_MAX_DELAY_S


def make_transport(url, trust):
    """TLS that trusts exactly `trust`, and checks the certificate for the URL's host."""
    u = urlparse(url)
    ctx = ssl.create_default_context(cafile=trust)
    ctx.check_hostname = True
    host = u.hostname
    endpoint = {"type": "tcp", "host": host, "port": u.port or 8080, "tls": ctx}
    common = {"endpoint": endpoint, "max_retries": -1, "max_retry_delay": TRANSPORT_MAX_DELAY_S}
    if u.scheme == "wss":
        return {"type": "websocket", "url": url, "serializers": ["cbor"], **common}
    if u.scheme == "rss":
        return {"type": "rawsocket", "url": url, "serializer": "cbor", **common}
    raise SystemExit(f"unsupported URL scheme {u.scheme!r}: use wss:// or rss://")


def main(argv=None):
    args = parse_args(argv)
    if args.self_test:
        self_test()
        return 0
    if args.generate_key:
        generate_key(args.generate_key)
        return 0

    log = Log(args.log, args.name, args.realm)
    log.write("start", ok=True, stack=stack(args.url))
    rng = random.Random(f"{args.seed}:{args.name}")
    ticks = Ticks(args.realm, args.name)
    peers = [p for p in args.peers.split(",") if p]

    if args.key:
        authentication = {"cryptosign": {"privkey": read_key(args.key), "authid": args.name}}
    else:
        authentication = {"anonymous": {}}

    transport = make_transport(args.url, args.trust)
    comp = Component(transports=[transport], realm=args.realm, authentication=authentication)
    # Component keeps one retry state per transport; this client has exactly one
    pace = Pace(comp._transports[0], cap=args.refused_max_delay)
    state = {"authid": None, "stop": False, "joined": False}

    def echo(payload, details=None):
        return {"payload": payload, "callee": args.name, "caller_seen": details.caller_authid if details else None}

    def on_tick(tick, details=None):
        log.write("event", **event_fields(tick, details, args.realm))

    async def wait_for_peers(session):
        """The barrier: wait until every peer's echo answers, so no call of the workload
        can fail because a peer has not started yet - a start-up artefact, not a fault
        of the router. No deadline by default (a deployment may start its clients
        minutes apart); a peer that never comes shows as `peer_wait` records, not as
        failed calls. With --peer-wait > 0 it gives up after that long, as before.
        Returns (ready, missing), or None when the client stops or loses its session
        while waiting."""
        t0 = time.monotonic()
        deadline = t0 + args.peer_wait if args.peer_wait > 0 else None
        next_report = t0 + PEER_WAIT_REPORT_S
        pending = set(peers)
        while pending:
            if state["stop"] or not session.is_attached():
                return None
            if deadline is not None and time.monotonic() >= deadline:
                break
            for peer in sorted(pending):
                if not session.is_attached():
                    return None
                try:
                    await session.call(f"{PREFIX}{peer}.echo", b"")
                    pending.discard(peer)
                    log.write("peer_ready", peer=peer, ok=True)
                except ApplicationError:
                    pass
            if pending:
                now = time.monotonic()
                if now >= next_report:
                    log.write("peer_wait", ok=True, missing=sorted(pending), waited_s=round(now - t0, 1))
                    next_report = now + PEER_WAIT_REPORT_S
                await asyncio.sleep(0.5)
        for peer in sorted(pending):
            log.write("peer_ready", peer=peer, ok=False, error="peer not reachable before the workload started")
        return sorted(set(peers) - pending), sorted(pending)

    async def call_once(session, peer):
        payload = rng.randbytes(rng.randint(16, 1024))
        t0 = time.perf_counter()
        try:
            res = await session.call(f"{PREFIX}{peer}.echo", payload)
        except Exception as e:  # noqa: BLE001 - every failure is a record, not a crash
            log.write(
                "call",
                peer=peer,
                bytes=len(payload),
                latency_ms=round((time.perf_counter() - t0) * 1000, 3),
                ok=False,
                error=getattr(e, "error", None) or type(e).__name__,
            )
            return
        ms = round((time.perf_counter() - t0) * 1000, 3)
        violation = check_echo(res, payload, peer, state["authid"])
        log.write("call", peer=peer, bytes=len(payload), latency_ms=ms, ok=violation is None, violation=violation)

    async def publish_once(session):
        await publish_tick(session, log, ticks)

    @comp.on_connect
    def _(session, transport):
        state["joined"] = False
        log.write("connect", ok=True)

    @comp.on_disconnect
    def _(session, was_clean=None):
        log.write("disconnect", ok=True, clean=was_clean)

    @comp.on_leave
    def _(session, details):
        if state["joined"]:
            log.write("leave", ok=True, reason=details.reason)
            # the workload issues nothing more on this session (competitions#15)
            state["left"].set()
        else:
            # the router ended the session before it was joined: a refused join. It will
            # not change by itself (a key not authorized yet), so back off.
            next_s = pace.refused()
            log.write(
                "refused", ok=False, error=details.reason, message=details.message, attempt=pace.attempt, next_s=next_s
            )

    @comp.on_connectfailure
    def _(component, exc):
        # a refused join also ends here, as an ApplicationError; that pace is set above
        if not isinstance(exc, ApplicationError):
            pace.transport_failed()

    @comp.on_join
    async def joined(session, details):
        state["authid"] = details.authid
        state["joined"] = True
        state["left"] = asyncio.Event()
        pace.joined()
        # measured on the live connection: the WebSocket extensions (e.g. permessage-deflate)
        # the router and this client agreed on; None on RawSocket
        ws_ext = getattr(getattr(session, "_transport", None), "websocket_extensions_in_use", None)
        log.write(
            "join",
            ok=True,
            authid=details.authid,
            authrole=details.authrole,
            authmethod=details.authmethod,
            websocket_extensions=None if ws_ext is None else [str(e) for e in ws_ext],
        )
        await session.register(echo, f"{PREFIX}{args.name}.echo", options=RegisterOptions(details_arg="details"))
        await session.subscribe(on_tick, TICK, options=SubscribeOptions(details_arg="details"))
        # the workload runs as its own task, so the join completes now
        state["task"] = asyncio.ensure_future(workload(session, state["left"]))

    async def workload(session, left):
        barrier = await wait_for_peers(session)
        if barrier is None:
            return
        ready, missing = barrier
        # The client's statement that its realm is complete (or, with --peer-wait, that
        # it gave up): a match's measurement window starts at the latest of these.
        log.write("workload_start", ok=not missing, peers=ready, missing=missing)
        await workload_loop(session, left, rng, args.rate, peers, call_once, publish_once, lambda: state["stop"])

    async def runner():
        loop = asyncio.get_running_loop()
        done = comp.start(loop)
        if args.duration is not None:
            await asyncio.sleep(args.duration)
            # Two-phase stop: stop calling, finish the call in flight, then keep
            # serving echo for the grace period, so peers that started later and
            # are still calling get their answers. Only then leave.
            state["stop"] = True
            task = state.get("task")
            if task is not None:
                await asyncio.wait([task], timeout=args.stop_grace)
            await asyncio.sleep(args.stop_grace)
            stopped = comp.stop()
            if stopped is not None:
                await stopped
        await done

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        pass
    finally:
        log.write(
            "summary",
            ok=log.errors == 0 and log.violations == 0,
            errors=log.errors,
            violations=log.violations,
            log_backlog_max=log.backlog_max,
        )
        log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
