# SPDX-License-Identifier: MIT
"""The smoke test's injector (competitions#24): three events that no member published as such.

It joins an app realm with a member's key, as a second session of that member, waits
for a genuine tick of another member, and then publishes:

1. that tick again, unchanged: a second copy;
2. a tick that names another member, with a seq and a digest nobody published;
3. an event on a topic other than `gladiators.tick`.

It writes what it sent to --out as JSON, so smoke.sh knows what each member must
report: every member records the three as violations or the referee finds them
injected (referee/verify_events.py). Used by smoke.sh only.

    python smoke_inject.py --name client04 --realm application1 --key client04.key \\
        --forge-as client05 --url wss://localhost:8080/ws --trust server.crt --out inject.json
"""

import argparse
import asyncio
import hashlib
import json
import secrets
import ssl
import sys
from urllib.parse import urlparse

from autobahn.asyncio.component import Component
from autobahn.wamp.types import PublishOptions

TICK = "gladiators.tick"
# The off-topic event uses the pre-known attacker topic, which workload clients
# subscribe to exactly (crossbar-v1 uses no prefix subscriptions): any event there
# is an injection a member records.
OFF_TOPIC = "gladiators.attacker.probe"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--name", required=True)
    p.add_argument("--realm", required=True)
    p.add_argument("--key", required=True)
    p.add_argument("--forge-as", required=True, help="the member the forged tick names")
    p.add_argument("--url", required=True)
    p.add_argument("--trust", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--timeout", type=float, default=60.0)
    args = p.parse_args(argv)

    with open(args.key) as f:
        privkey = f.read().strip()
    u = urlparse(args.url)
    ctx = ssl.create_default_context(cafile=args.trust)
    endpoint = {"type": "tcp", "host": u.hostname, "port": u.port, "tls": ctx}
    transport = {
        "type": "websocket",
        "url": args.url,
        "serializers": ["cbor"],
        "endpoint": endpoint,
        "max_retries": 0,
    }
    comp = Component(
        transports=[transport],
        realm=args.realm,
        authentication={"cryptosign": {"privkey": privkey, "authid": args.name}},
    )
    result = {}

    @comp.on_join
    async def joined(session, details):
        got = asyncio.get_running_loop().create_future()

        def on_tick(tick):
            if (
                isinstance(tick, dict)
                and tick.get("name") not in (None, args.name)
                and not got.done()
            ):
                got.set_result(tick)

        await session.subscribe(on_tick, TICK)
        try:
            genuine = await asyncio.wait_for(got, args.timeout)
        except TimeoutError:
            result["error"] = f"no tick of another member within {args.timeout} s"
            await session.leave()
            return
        forged = {
            "realm": args.realm,
            "name": args.forge_as,
            "seq": 999999,
            "digest": hashlib.sha256(secrets.token_hex(16).encode("utf-8")).hexdigest(),
        }
        options = PublishOptions(acknowledge=True)
        await session.publish(TICK, genuine, options=options)
        await session.publish(TICK, forged, options=options)
        await session.publish(
            OFF_TOPIC, {"smoke": "an event no client publishes"}, options=options
        )
        result.update(
            {
                "authid": details.authid,
                "replayed": genuine,
                "forged": forged,
                "off_topic": OFF_TOPIC,
            }
        )
        # let the router deliver before the session goes
        await asyncio.sleep(1.0)
        await session.leave()

    async def run():
        done = comp.start(asyncio.get_running_loop())
        await asyncio.wait_for(done, args.timeout + 30)

    try:
        asyncio.run(run())
    except Exception as e:  # noqa: BLE001 - reported to smoke.sh, which fails on it
        result.setdefault("error", f"{type(e).__name__}: {e}")
    with open(args.out, "w") as f:
        json.dump(result, f, indent=2, sort_keys=True)
    return 1 if "error" in result else 0


if __name__ == "__main__":
    sys.exit(main())
