# SPDX-License-Identifier: MIT
"""The smoke test's RPC rogue (competitions#33): a registration that should not exist.

It joins an application realm with a member's key, as a second session of that member,
and registers the pre-known attacker procedure `gladiators.attacker.echo` - which no
legitimate client registers. It answers every call with a FABRICATED flag (a fresh
token nobody recorded in an `answer` record), claiming to be another member. The
workload clients on that realm probe the attacker procedure; while the rogue is up,
their probes succeed, and the referee (referee/verify_calls.py) finds the result digest
in no answer record, so it reports the call injected - and each prober records the
unexpected success itself. The rogue cannot shadow a genuine `gladiators.<name>.echo`
(WAMP exact registration is single-owner), so it answers the unclaimed attacker
procedure instead.

It writes what it did to --out as JSON. Used by smoke.sh only.

    python smoke_rogue.py --name client04 --realm application1 --key client04.key \\
        --claim-as client05 --url wss://localhost:8080/ws --trust server.crt \\
        --hold 20 --out rogue.json
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
from autobahn.wamp.types import RegisterOptions

ATTACKER_PROC = "gladiators.attacker.echo"


def new_fake_flag():
    """A token of the flag's shape, but one no legitimate client ever recorded."""
    groups = ["".join(secrets.choice("345679ACEFGHJKLMNPQRSTUVWXY") for _ in range(4)) for _ in range(7)]
    return "-".join(groups)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--name", required=True, help="the member whose key the rogue uses")
    p.add_argument("--realm", required=True)
    p.add_argument("--key", required=True)
    p.add_argument("--claim-as", required=True, help="the callee name the rogue puts in its results")
    p.add_argument("--url", required=True)
    p.add_argument("--trust", required=True)
    p.add_argument("--hold", type=float, default=20.0, help="seconds to keep the rogue registration up")
    p.add_argument("--out", required=True)
    p.add_argument("--timeout", type=float, default=90.0)
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
    result = {"answered": 0, "fake_digests": []}

    @comp.on_join
    async def joined(session, details):
        def rogue_echo(payload=b"", details=None):
            flag = new_fake_flag()
            digest = hashlib.sha256(flag.encode("utf-8")).hexdigest()
            result["answered"] += 1
            if len(result["fake_digests"]) < 50:
                result["fake_digests"].append(digest)
            # claim to be another member; the digest is of a flag in no answer record
            return {
                "payload": payload,
                "callee": args.claim_as,
                "caller_seen": details.caller_authid if details else None,
                "digest": digest,
            }

        try:
            await session.register(rogue_echo, ATTACKER_PROC, options=RegisterOptions(details_arg="details"))
        except Exception as e:  # noqa: BLE001 - reported to smoke.sh
            result["error"] = f"could not register {ATTACKER_PROC}: {type(e).__name__}: {e}"
            await session.leave()
            return
        result.update({"authid": details.authid, "realm": args.realm, "registered": ATTACKER_PROC})
        await asyncio.sleep(args.hold)
        await session.leave()

    async def run():
        done = comp.start(asyncio.get_running_loop())
        await asyncio.wait_for(done, args.timeout + args.hold)

    try:
        asyncio.run(run())
    except Exception as e:  # noqa: BLE001 - reported to smoke.sh
        result.setdefault("error", f"{type(e).__name__}: {e}")
    with open(args.out, "w") as f:
        json.dump(result, f, indent=2, sort_keys=True)
    return 1 if "error" in result else 0


if __name__ == "__main__":
    sys.exit(main())
