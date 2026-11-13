# SPDX-License-Identifier: MIT
"""The workload client of the Crossbar.io single-node target.

Each of the nine clients runs this program. It joins its realm, registers its own
`gladiators.<name>.echo`, subscribes to `gladiators.tick`, and then, driven by a
seed, calls its peers' echo procedures and publishes ticks. It checks every answer
and writes one JSON line per event to its log, with an fsync after each line.

    python workload.py --generate-key client04.key
    python workload.py --name client04 --realm application1 --key client04.key \\
        --peers client05,client06 --url wss://localhost:8080/ws --trust server.crt \\
        --seed 1 --log client04.jsonl

Without --key the client authenticates anonymously (realm `public`).
"""

import argparse
import asyncio
import binascii
import json
import os
import random
import ssl
import sys
import time
from datetime import UTC, datetime
from urllib.parse import urlparse

from autobahn.asyncio.component import Component
from autobahn.wamp.exception import ApplicationError
from autobahn.wamp.types import PublishOptions, RegisterOptions, SubscribeOptions

PREFIX = "gladiators."
# While waiting for its peers, a client logs who is still missing this often (seconds).
PEER_WAIT_REPORT_S = 30.0
TICK = PREFIX + "tick"


def check_echo(result, payload, peer, my_authid):
    """The violation an echo result shows, or None. Pure, so --self-test can prove it fires."""
    if not isinstance(result, dict) or result.get("payload") != payload:
        return "payload changed"
    if result.get("callee") != peer:
        return "answered by another callee"
    if result.get("caller_seen") != my_authid:
        return "router reported another caller authid"
    return None


def check_tick(tick, my_realm):
    """The violation a received tick shows, or None."""
    if not isinstance(tick, dict) or tick.get("realm") != my_realm:
        return "tick from another realm"
    return None


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


class Log:
    """Append-only JSON Lines; every record is flushed and fsync'd before the next."""

    def __init__(self, path, name, realm):
        self._f = open(path, "a", encoding="utf-8")
        self._base = {"name": name, "realm": realm}
        self.errors = 0
        self.violations = 0

    def write(self, op, **fields):
        rec = {"ts": datetime.now(UTC).isoformat(timespec="microseconds"), "op": op, **self._base, **fields}
        if rec.get("error"):
            self.errors += 1
        if rec.get("violation"):
            self.violations += 1
        self._f.write(json.dumps(rec, sort_keys=True) + "\n")
        self._f.flush()
        os.fsync(self._f.fileno())

    def close(self):
        self._f.close()


def make_transport(url, trust):
    """TLS that trusts exactly `trust`, and checks the certificate for the URL's host."""
    u = urlparse(url)
    ctx = ssl.create_default_context(cafile=trust)
    ctx.check_hostname = True
    host = u.hostname
    endpoint = {"type": "tcp", "host": host, "port": u.port or 8080, "tls": ctx}
    common = {"endpoint": endpoint, "max_retries": -1, "max_retry_delay": 5}
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
    peers = [p for p in args.peers.split(",") if p]

    if args.key:
        authentication = {"cryptosign": {"privkey": read_key(args.key), "authid": args.name}}
    else:
        authentication = {"anonymous": {}}

    transport = make_transport(args.url, args.trust)
    comp = Component(transports=[transport], realm=args.realm, authentication=authentication)
    state = {"authid": None, "stop": False, "joined": False}

    def echo(payload, details=None):
        return {"payload": payload, "callee": args.name, "caller_seen": details.caller_authid if details else None}

    def on_tick(tick, details=None):
        violation = check_tick(tick, args.realm)
        log.write(
            "event",
            peer=tick.get("name") if isinstance(tick, dict) else None,
            from_realm=tick.get("realm") if isinstance(tick, dict) else None,
            publisher_seen=details.publisher_authid if details else None,
            ok=violation is None,
            violation=violation,
        )

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
        t0 = time.perf_counter()
        try:
            await session.publish(
                TICK,
                {"realm": args.realm, "name": args.name},
                options=PublishOptions(acknowledge=True, exclude_me=False),
            )
        except Exception as e:  # noqa: BLE001
            log.write(
                "publish",
                latency_ms=round((time.perf_counter() - t0) * 1000, 3),
                ok=False,
                error=getattr(e, "error", None) or type(e).__name__,
            )
            return
        log.write("publish", latency_ms=round((time.perf_counter() - t0) * 1000, 3), ok=True)

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
        else:
            # the router ended the session before it was joined: a refused join
            log.write("refused", ok=False, error=details.reason, message=details.message)

    @comp.on_join
    async def joined(session, details):
        state["authid"] = details.authid
        state["joined"] = True
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
        state["task"] = asyncio.ensure_future(workload(session))

    async def workload(session):
        barrier = await wait_for_peers(session)
        if barrier is None:
            return
        ready, missing = barrier
        # The client's statement that its realm is complete (or, with --peer-wait, that
        # it gave up): a match's measurement window starts at the latest of these.
        log.write("workload_start", ok=not missing, peers=ready, missing=missing)
        while not state["stop"] and session.is_attached():
            await asyncio.sleep(rng.expovariate(args.rate))
            if peers:
                await call_once(session, rng.choice(peers))
            if rng.random() < 0.5:
                await publish_once(session)

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
        log.write("summary", ok=log.errors == 0 and log.violations == 0, errors=log.errors, violations=log.violations)
        log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
