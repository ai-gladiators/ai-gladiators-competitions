# SPDX-License-Identifier: MIT
"""The workload client of the Crossbar.io two-node mesh PubSub + RPC target (crossbar-v2).

This is the SAME exact-URI workload as the single-node target (crossbar-v1): each of the
nine clients runs this program, joins its realm, registers its own
`gladiators.<name>.echo`, subscribes **exact** to `gladiators.tick` and to the
pre-known attacker topic `gladiators.attacker.probe`, and then, driven by a seed,
calls its peers' echo procedures, publishes ticks on `gladiators.tick`, and
periodically probes the pre-known attacker procedure `gladiators.attacker.echo`. It
checks every answer and writes one JSON line per event to its log, from a thread of
its own, each line durable before the next (competitions#19).

In the two-node mesh the nine clients are split across the two routers (node A and node
B), so an application realm's members sit on both routers and reach each other only
across the router-to-router link (`rlink`). Each client records which node it connected
to (`--node`) in its `start` and `join` records. This target scores CTF flags for
**both** PubSub and RPC. Only **exact** URIs are used (no prefix/wildcard subscriptions
or registrations), so both ticks and echo calls are forwarded across the rlink, and the
same workload reuses across the mesh unchanged.

Every tick has a flag (an autobahn token, fresh per tick), recorded only in the
publisher's own `publish` record with the tick's sequence number (competitions#21);
the tick on the wire carries the flag's SHA-256 digest, never the flag (competitions#24).
Symmetrically, **every `echo` answer has a flag**, recorded only in the callee's own
`answer` record with an answer sequence number; the call result carries the flag's
digest, never the flag. So a digest a non-member holds proves an event or a call result
of that realm reached them, for PubSub and for RPC alike (referee/verify_flags.py):

- a digest held by anyone not a member of the realm it came from shows an event or a
  call result of that realm reached them;
- a flag held by anyone but its producer shows that producer's log or process was read;
- every event a client receives, and every call it makes, is logged so the referee can
  trace it to exactly one publish or answer record, or report it as injected
  (referee/verify_events.py for events, referee/verify_calls.py for calls);
- the probe canaries log the expected failure of the attacker topic/procedure, or the
  **unexpected success** that an injection or a rogue registration causes.

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
import hashlib
import json
import os
import queue
import random
import re
import ssl
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime
from urllib.parse import urlparse

from autobahn.asyncio.component import Component
from autobahn.util import DEFAULT_TOKEN_CHARS, generate_token
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
# Pre-known attacker URIs (the probe canaries). No legitimate client ever publishes to
# the attacker topic or registers the attacker procedure, so any event received on the
# topic, or any answer to the procedure, is an injection or a rogue registration - an
# isolation break a workload client can see for itself.
ATTACKER_TOPIC = PREFIX + "attacker.probe"
ATTACKER_PROC = PREFIX + "attacker.echo"
# A client probes the attacker procedure about this often (seconds).
PROBE_INTERVAL_S = 2.0
# A tick's flag (competitions#21): autobahn's token, FLAG_GROUPS groups of
# FLAG_GROUP_CHARS characters of DEFAULT_TOKEN_CHARS (27 characters without look-alikes),
# e.g. K7AT-TMJS-WT47-SEER-3ELJ-UUYM-WRXF: 28 x log2(27) = 133 bits.
FLAG_GROUPS = 7
FLAG_GROUP_CHARS = 4
# What a tick carries instead of its flag (competitions#24): the SHA-256 of the flag's
# UTF-8 bytes, as 64 lower-case hex characters.
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
TICK_FIELDS = ("digest", "name", "realm", "seq")
# A publisher remembers the digests of its own ticks until it receives them back
# (exclude_me=False), at most this many: in normal operation a tick comes back within
# milliseconds; a publish that failed may never come back.
OWN_DIGESTS_KEPT = 4096
# A field of a received event longer than this is logged as its type and length only.
FIELD_MAX = 80


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


def digest_of(flag):
    """The SHA-256 of a flag, as hex: what a tick carries instead of the flag (competitions#24)."""
    return hashlib.sha256(flag.encode("utf-8")).hexdigest()


def tick_shape(tick):
    """What is wrong with the shape of a received tick, or None."""
    if not isinstance(tick, dict):
        return f"not a tick: a {type(tick).__name__} payload"
    missing = [k for k in TICK_FIELDS if k not in tick]
    if missing:
        return f"not a tick: no {', '.join(missing)}"
    extra = sorted(str(k)[:FIELD_MAX] for k in tick if k not in TICK_FIELDS)
    if extra:
        return f"not a tick: unexpected field {', '.join(extra)}"
    if not (isinstance(tick["name"], str) and isinstance(tick["realm"], str)):
        return "not a tick: name and realm are not strings"
    if not (isinstance(tick["seq"], int) and not isinstance(tick["seq"], bool) and tick["seq"] > 0):
        return "not a tick: seq is not a positive integer"
    if not (isinstance(tick["digest"], str) and DIGEST_RE.match(tick["digest"])):
        return "not a tick: digest is not 64 lower-case hex characters"
    return None


def new_flag():
    """A tick's flag: autobahn's `generate_token`, which draws from the operating
    system's generator (`random.SystemRandom`), never from the workload's seeded
    `random.Random`, which repeats from its seed - a flag made by it could be predicted
    instead of captured."""
    return generate_token(FLAG_GROUPS, FLAG_GROUP_CHARS)


class Ticks:
    """The ticks of one process: `seq` counts from 1 and restarts with the process, so
    the flag, not the seq, identifies a tick.

    It remembers the digests of the ticks it made until each comes back once
    (`received`), so a tick that names this client but was not made by it, or a second
    copy of one, is told apart on arrival (competitions#24)."""

    def __init__(self, realm, name, flag=new_flag, keep=OWN_DIGESTS_KEPT):
        self.realm = realm
        self.name = name
        self.seq = 0
        self._flag = flag
        self._keep = keep
        self._pending = OrderedDict()

    def next(self):
        """(tick, flag): the tick to publish, which carries the flag's digest, and the
        flag, which goes into the publisher's record only."""
        self.seq += 1
        flag = self._flag()
        digest = digest_of(flag)
        self._pending[digest] = self.seq
        while len(self._pending) > self._keep:
            self._pending.popitem(last=False)
        return {"realm": self.realm, "name": self.name, "seq": self.seq, "digest": digest}, flag

    def received(self, digest, seq):
        """True once for each tick this client made, when it comes back with its seq."""
        if self._pending.get(digest) != seq:
            return False
        del self._pending[digest]
        return True


class Answers:
    """The `echo` answers of one process: the RPC twin of Ticks. Each answer has a fresh
    flag whose digest is returned to the caller; the flag goes into the callee's own
    `answer` record only. `seq` counts answers from 1 and restarts with the process."""

    def __init__(self, realm, name, flag=new_flag):
        self.realm = realm
        self.name = name
        self.seq = 0
        self._flag = flag

    def next(self):
        """(digest, flag, seq): the digest the result carries, the flag for the callee's
        record only, and this answer's sequence number."""
        self.seq += 1
        flag = self._flag()
        return digest_of(flag), flag, self.seq


async def publish_tick(session, log, ticks):
    """Publish the next tick and record it, with its seq and flag, in the publisher's log -
    also when the publish fails: a failed publish may still have reached subscribers.
    This record is the only place a flag is written (competitions#21); the tick itself
    carries only the flag's digest (competitions#24)."""
    tick, flag = ticks.next()
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
            flag=flag,
        )
        return
    log.write(
        "publish",
        latency_ms=round((time.perf_counter() - t0) * 1000, 3),
        ok=True,
        seq=tick["seq"],
        flag=flag,
    )


def _field(value):
    """A received value as it may go into a record: short strings and integers as they
    are, anything else as its type (and length), so an event cannot fill the log."""
    if value is None or (isinstance(value, int) and not isinstance(value, bool)):
        return value
    if isinstance(value, str) and len(value) <= FIELD_MAX:
        return value
    size = f", {len(value)} long" if hasattr(value, "__len__") else ""
    return f"<{type(value).__name__}{size}>"


def event_fields(topic, args, kwargs, details, my_realm, ticks):
    """The record of a received event (competitions#24): its topic, the publisher, realm,
    seq and digest it states, the publisher the router disclosed, and the violation it
    shows, or None. Never a flag: a tick carries only the digest, and the flag is in its
    publisher's record only (competitions#21).

    The violations, the first that applies:
    - an event on a topic other than `gladiators.tick`;
    - not exactly one positional argument, or not a tick of the expected shape;
    - a tick of another realm;
    - a tick that names this client but carries a digest it did not publish, or one it
      already received: no other client can make this client's ticks."""
    tick = args[0] if len(args) == 1 and not kwargs else None
    stated = tick if isinstance(tick, dict) else {}
    fields = {
        "topic": _field(topic),
        "peer": _field(stated.get("name")),
        "from_realm": _field(stated.get("realm")),
        "seq": _field(stated.get("seq")),
        "digest": _field(stated.get("digest")),
        "publisher_seen": details.publisher_authid if details else None,
    }
    if topic != TICK:
        violation = "event on an unexpected topic"
    elif tick is None:
        violation = f"not a tick: {len(args)} positional and {len(kwargs)} keyword arguments"
    else:
        violation = tick_shape(tick)
        if violation is None and tick["realm"] != my_realm:
            violation = "tick from another realm"
        if violation is None and tick["name"] == ticks.name and not ticks.received(tick["digest"], tick["seq"]):
            violation = "names this client, but it did not publish this digest, or already received it"
    return {**fields, "ok": violation is None, "violation": violation}


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
    """competitions#21, #24: each tick a fresh flag, which goes into the publish record
    only; the tick carries the flag's digest; an event record holds no flag."""
    group = f"[{DEFAULT_TOKEN_CHARS}]{{{FLAG_GROUP_CHARS}}}"
    flagform = re.compile(rf"^{group}(-{group}){{{FLAG_GROUPS - 1}}}$")
    assert len(DEFAULT_TOKEN_CHARS) == len(set(DEFAULT_TOKEN_CHARS)) == 27, DEFAULT_TOKEN_CHARS
    assert digest_of("K7AT-TMJS-WT47-SEER-3ELJ-UUYM-WRXF") == hashlib.sha256(b"K7AT-TMJS-WT47-SEER-3ELJ-UUYM-WRXF").hexdigest()
    ticks = Ticks("application1", "client04")
    sent = [ticks.next() for _ in range(1000)]
    assert [t["seq"] for t, _ in sent] == list(range(1, 1001))
    assert all(flagform.match(f) for _, f in sent), "a flag is not 7 groups of 4 token characters"
    assert len({f for _, f in sent}) == len(sent), "a flag repeated"
    assert all(set(t) == set(TICK_FIELDS) for t, _ in sent), "a tick carries more or less than its four fields"
    assert all(t["digest"] == digest_of(f) and f not in json.dumps(t) for t, f in sent), "the flag is on the wire"
    assert all(t["realm"] == "application1" and t["name"] == "client04" for t, _ in sent)
    assert all(tick_shape(t) is None for t, _ in sent)
    # a seeded generator would make the same flags again; the flags must not follow a seed
    random.seed(1)
    a = Ticks("public", "client01").next()[1]
    random.seed(1)
    b = Ticks("public", "client01").next()[1]
    assert a != b, "the flag follows the seeded generator"

    class _Pub:
        def __init__(self, fail):
            self.fail = fail
            self.published = []

        async def publish(self, topic, payload, options=None):
            self.published.append((topic, payload, options))
            if self.fail:
                raise ApplicationError("wamp.error.not_authorized")

    class _Details:
        def __init__(self, publisher="client05", topic=TICK):
            self.publisher_authid = publisher
            self.topic = topic

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "f.jsonl")
        log = Log(path, "client04", "application1")
        ticks = Ticks("application1", "client04")
        ok_session, failing_session = _Pub(False), _Pub(True)
        asyncio.run(publish_tick(ok_session, log, ticks))
        asyncio.run(publish_tick(failing_session, log, ticks))
        topic, tick, options = ok_session.published[0]
        assert topic == TICK and options.exclude_me is False and options.acknowledge is True, options
        log.write("event", **event_fields(TICK, (tick,), {}, _Details("client04"), "application1", ticks))
        log.close()
        with open(path) as f:
            recs = [json.loads(line) for line in f]
        pubs = [r for r in recs if r["op"] == "publish"]
        payloads = [ok_session.published[0][1], failing_session.published[0][1]]
        assert [p["ok"] for p in pubs] == [True, False], pubs
        assert [(p["seq"], digest_of(p["flag"])) for p in pubs] == [(t["seq"], t["digest"]) for t in payloads]
        (event,) = [r for r in recs if r["op"] == "event"]
        assert "flag" not in event and pubs[0]["flag"] not in json.dumps(event), "a flag reached an event record"
        assert event["ok"] and event["violation"] is None, event
        want = {"topic": TICK, "peer": "client04", "from_realm": "application1", "seq": 1, "digest": tick["digest"]}
        assert {k: event[k] for k in want} == want, event

    # every violation on its case, and a genuine tick of a peer passes
    mine = Ticks("application1", "client04")
    own, _ = mine.next()
    peer, _ = Ticks("application1", "client05").next()
    other, _ = Ticks("application2", "client07").next()

    def v(topic, args, kwargs=None, realm="application1"):
        return event_fields(topic, args, kwargs or {}, _Details(), realm, mine)["violation"]

    assert v(TICK, (peer,)) is None
    assert v(PREFIX + "other", (peer,)) == "event on an unexpected topic"
    assert v(PREFIX + "other", ()) == "event on an unexpected topic"
    assert v(TICK, ()) == "not a tick: 0 positional and 0 keyword arguments"
    assert v(TICK, (peer,), {"x": 1}) == "not a tick: 1 positional and 1 keyword arguments"
    assert v(TICK, (peer, peer)) == "not a tick: 2 positional and 0 keyword arguments"
    assert v(TICK, ("junk",)) == "not a tick: a str payload"
    assert v(TICK, ({k: peer[k] for k in ("name", "realm", "seq")},)) == "not a tick: no digest"
    assert v(TICK, ({**peer, "flag": "x"},)) == "not a tick: unexpected field flag"
    assert v(TICK, ({**peer, "seq": True},)) == "not a tick: seq is not a positive integer"
    assert v(TICK, ({**peer, "seq": 0},)) == "not a tick: seq is not a positive integer"
    assert v(TICK, ({**peer, "name": 5},)) == "not a tick: name and realm are not strings"
    assert v(TICK, ({**peer, "digest": peer["digest"].upper()},)) == "not a tick: digest is not 64 lower-case hex characters"
    assert v(TICK, ({**peer, "digest": peer["digest"][:-1]},)) == "not a tick: digest is not 64 lower-case hex characters"
    assert v(TICK, (other,)) == "tick from another realm"
    forged = "names this client, but it did not publish this digest, or already received it"
    assert v(TICK, ({**peer, "name": "client04"},)) == forged  # a digest this client never made
    assert v(TICK, ({**own, "seq": own["seq"] + 1},)) == forged  # its digest, under another seq
    assert v(TICK, (own,)) is None  # its own tick, back once
    assert v(TICK, (own,)) == forged  # a second copy of it
    # a received value is logged short, never as given
    rec = event_fields(TICK, ({**peer, "name": "x" * 5000},), {}, None, "application1", mine)
    assert rec["peer"] == "<str, 5000 long>" and rec["publisher_seen"] is None, rec
    # the publisher keeps a bounded number of its own digests
    small = Ticks("public", "client01", keep=3)
    made = [small.next()[0] for _ in range(5)]
    assert not small.received(made[0]["digest"], 1) and small.received(made[4]["digest"], 5)


def _rpc_test():
    """competitions#33: each echo answer a fresh flag, its digest returned, the flag in
    the answer record only; the attacker URIs are exact and distinct from the tick topic."""
    group = f"[{DEFAULT_TOKEN_CHARS}]{{{FLAG_GROUP_CHARS}}}"
    flagform = re.compile(rf"^{group}(-{group}){{{FLAG_GROUPS - 1}}}$")
    answers = Answers("application1", "client04")
    made = [answers.next() for _ in range(1000)]
    assert [seq for _, _, seq in made] == list(range(1, 1001)), "answer seq not 1..n"
    assert all(flagform.match(f) for _, f, _ in made), "an answer flag is not a token"
    assert len({f for _, f, _ in made}) == len(made), "an answer flag repeated"
    assert all(d == digest_of(f) for d, f, _ in made), "the returned digest is not the flag's"
    # a seeded generator would repeat; the answer flags must not follow a seed
    random.seed(1)
    a = Answers("public", "client01").next()[1]
    random.seed(1)
    b = Answers("public", "client01").next()[1]
    assert a != b, "the answer flag follows the seeded generator"
    # the attacker URIs are exact and are not the tick topic
    assert ATTACKER_TOPIC != TICK and ATTACKER_PROC != TICK
    assert ATTACKER_TOPIC.startswith(PREFIX) and ATTACKER_PROC.startswith(PREFIX)


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
    _rpc_test()
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
    p.add_argument(
        "--node", default=None, help="a label for the node this client connects to (e.g. nodeA), recorded in its start and join records"
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
    log.write("start", ok=True, node=args.node, stack=stack(args.url))
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

    answers = Answers(args.realm, args.name)

    def echo(payload, details=None):
        # Every answer carries a fresh flag's digest; the flag goes into this callee's
        # own `answer` record only, never on the wire (the RPC twin of a published tick).
        digest, flag, seq = answers.next()
        caller = details.caller_authid if details else None
        log.write("answer", ok=True, uri=f"{PREFIX}{args.name}.echo", seq=seq, flag=flag, caller_seen=caller)
        return {"payload": payload, "callee": args.name, "caller_seen": caller, "digest": digest}

    async def probe_once(session):
        """Call the pre-known attacker procedure. It must not exist: a legitimate client
        never registers it. The expected outcome is `wamp.error.no_such_procedure`
        (isolation holds); an answer means a rogue registered it - a violation, with the
        fabricated result's digest recorded."""
        t0 = time.perf_counter()
        try:
            res = await session.call(ATTACKER_PROC, b"probe")
        except ApplicationError as e:
            err = e.error
            log.write(
                "probe_call",
                uri=ATTACKER_PROC,
                latency_ms=round((time.perf_counter() - t0) * 1000, 3),
                ok=(err == "wamp.error.no_such_procedure"),
                outcome=err,
                violation=None if err == "wamp.error.no_such_procedure" else f"unexpected error {err}",
            )
            return
        except Exception as e:  # noqa: BLE001 - a failure is a record, not a crash
            log.write("probe_call", uri=ATTACKER_PROC, ok=False, error=type(e).__name__,
                      violation="the attacker procedure could not be probed")
            return
        digest = res.get("digest") if isinstance(res, dict) else None
        log.write(
            "probe_call",
            uri=ATTACKER_PROC,
            latency_ms=round((time.perf_counter() - t0) * 1000, 3),
            ok=False,
            result_digest=digest,
            violation="the attacker procedure was answered: a rogue registration",
        )

    async def probe_loop(session, left):
        """Probe the attacker procedure every PROBE_INTERVAL_S, until the client stops or
        the session leaves (as the workload loop does)."""
        while not state["stop"] and session.is_attached():
            try:
                await asyncio.wait_for(left.wait(), timeout=PROBE_INTERVAL_S)
                return
            except TimeoutError:
                pass
            if state["stop"] or not session.is_attached():
                return
            await probe_once(session)

    def on_event(*event_args, details=None, **event_kwargs):
        topic = details.topic if details is not None and details.topic else None
        log.write("event", **event_fields(topic, event_args, event_kwargs, details, args.realm, ticks))

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
        # the digest the result carried (the callee's answer flag): the referee checks it
        # against the callee's own answer record (referee/verify_calls.py)
        result_digest = res.get("digest") if isinstance(res, dict) else None
        log.write(
            "call", peer=peer, bytes=len(payload), latency_ms=ms,
            ok=violation is None, violation=violation, result_digest=result_digest,
        )

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
            node=args.node,
            authid=details.authid,
            authrole=details.authrole,
            authmethod=details.authmethod,
            websocket_extensions=None if ws_ext is None else [str(e) for e in ws_ext],
        )
        await session.register(echo, f"{PREFIX}{args.name}.echo", options=RegisterOptions(details_arg="details"))
        # EXACT subscriptions only (no prefix/wildcard), so this workload also meshes
        # across a router-to-router link unchanged (crossbar-v2). The tick topic carries
        # the workload's events; the pre-known attacker topic is a canary - no legitimate
        # client publishes there, so any event on it is an injection (competitions#24).
        await session.subscribe(on_event, TICK, options=SubscribeOptions(details_arg="details"))
        await session.subscribe(on_event, ATTACKER_TOPIC, options=SubscribeOptions(details_arg="details"))
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
        probe = asyncio.ensure_future(probe_loop(session, left))
        try:
            await workload_loop(session, left, rng, args.rate, peers, call_once, publish_once, lambda: state["stop"])
        finally:
            probe.cancel()

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
