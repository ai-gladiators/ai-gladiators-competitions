# Target: Crossbar.io, single node, PubSub + RPC

One [Crossbar.io](https://crossbar.io) router, three realms, and nine
[AutobahnPython](https://github.com/crossbario/autobahn-python) clients running a
seeded workload that exercises **both** publish/subscribe **and** remote procedure
calls. The router configuration and the client code are published here in full; only
the keys stay out of the repository.

This is the `crossbar-v0` single-node target with **RPC added as a scored dimension**,
and with the workload moved to **exact URIs** (no prefix or wildcard subscriptions or
registrations). Read the [`crossbar-v0` README](../single-node/README.md) for the
system under test, the TLS stacks, the durable logging and the flag token scheme; this
README covers only what RPC and the exact-URI design add.

## The realms and roles

Three realms, as in `crossbar-v0`:

| realm | authentication | role | clients |
|---|---|---|---|
| `public` | anonymous | `guest` | `client01`–`client03` |
| `application1` | WAMP-Cryptosign | `device` | `client04`–`client06` |
| `application2` | WAMP-Cryptosign | `device` | `client07`–`client09` |

Each role may call, register, publish and subscribe under the URI prefix `gladiators.`,
and nowhere else. Caller disclosure is on, so a callee sees the caller's authid.

## Exact URIs only

Every subscription and registration this workload makes is an **exact** URI:

- it registers `gladiators.<name>.echo` (exact);
- it subscribes to `gladiators.tick` and to `gladiators.attacker.probe` (exact), not to
  the `gladiators.` prefix.

This is deliberate: a Crossbar.io router-to-router link forwards exact registrations and
subscriptions, but not prefix/wildcard ones, so the same workload reuses unchanged
across the two-node mesh (`crossbar-v2`). Role *permissions* stay prefix-based (the role
may act anywhere under `gladiators.`); only the clients' own subscriptions and
registrations are exact.

## Flags, for events and for call results

`crossbar-v0` gives every **tick** a fresh flag, recorded only in the publisher's own
`publish` record; the tick on the wire carries the flag's SHA-256 digest. This target
does the **same for RPC**:

- **Every `echo` answer** draws a fresh flag, recorded only in the callee's own
  **`answer` record** (op `answer`, with the flag and an answer sequence number). The
  call **result** carries only the flag's **digest** (`{payload, callee, caller_seen,
  digest}`), never the flag.

So a flag never travels, for PubSub or RPC alike. A digest a non-member holds proves an
event **or a call result** of that realm reached them; a flag in anyone's hands but its
producer's proves a log or process was read. Every `call` record carries the digest its
result returned and the callee the caller meant to reach, so the referee can trace it to
exactly one answer record — or report it injected.

## The probe canaries

Two URIs are **pre-known to the attacker** and used by no legitimate client, so a
workload client can witness an isolation break from its own side:

- **`gladiators.attacker.probe`** (a topic). Every client subscribes to it (exact). No
  legitimate client ever publishes there, so **any** event a client receives on it is an
  injection — recorded as a violation.
- **`gladiators.attacker.echo`** (a procedure). Every client **calls** it about every
  two seconds. No legitimate client registers it, so the expected outcome is
  `wamp.error.no_such_procedure` (recorded as an expected `probe_call`). An **answer**
  means a rogue registered it — recorded as a violation, with the fabricated result's
  digest.

The canaries make the two authenticity breaks (S2) visible in the defenders' own logs,
independent of the attacker's honesty.

## The referee

Three standard-library tools, each reading only the clients' logs:

- [`referee/verify_flags.py`](referee/verify_flags.py) — looks a presented digest or
  flag up in the **publish and answer** records. An app-realm digest `counts` (S1, for
  PubSub or RPC); a `public` digest is a `control`; a raw flag is `obtained` (scores
  nothing). Extends `crossbar-v0`'s tool to treat answer records as flag sources.
- [`referee/verify_events.py`](referee/verify_events.py) — traces every received event
  to a publisher; an event no member published is `injected` (S2-pubsub). Unchanged from
  `crossbar-v0`.
- [`referee/verify_calls.py`](referee/verify_calls.py) — traces every call result's
  digest to the genuine registrant's answer record; a result from no answer record, or
  from another name or realm than the caller meant, or any answer to the attacker
  procedure, is `injected` (S2-rpc). **The result's self-reported `callee` is never
  trusted** — a rogue can claim any name; the flag's digest is the proof, because only
  the genuine registrant holds the flag.

Each tool has `--self-test`. `verify_flags.py` and `verify_calls.py` take
`--logs --claims --start --end` and `--logs --start --end` respectively, as
`crossbar-v0`'s do.

## A note on the RPC rogue (what the attacker can and cannot do)

WAMP exact registration is **single-owner**: a rogue cannot register
`gladiators.client05.echo` while `client05` holds it, so it cannot *shadow* a genuine
procedure. The authenticity break it can demonstrate is registering the **unclaimed**
attacker procedure `gladiators.attacker.echo` (which legitimate clients poll) and
answering it — reaching only the realm it managed to join. Realm isolation still holds:
a rogue on `application1` never answers an `application2` member's probe.

## Smoke test

[`smoke.sh`](smoke.sh) runs the whole target on one machine: it stands up the router and
the nine clients, injects PubSub events (a forged tick and an attacker-topic event:
[`smoke_inject.py`](smoke_inject.py)), stands up an RPC rogue that registers the attacker
procedure and answers with a fabricated flag ([`smoke_rogue.py`](smoke_rogue.py)), and
then requires:

- every client's own-realm ticks and matched call results, no errors, every flag in its
  own publish or answer record only; both intruders (an unknown key, an anonymous client
  in an authenticated realm) refused;
- `verify_flags.py`: a PubSub digest and an RPC digest both `count`, a `public` digest is
  a `control`, a flag is `obtained`, a non-claim is `invalid`;
- `verify_events.py`: the injected events at the `application1` members, none at
  `application2`;
- `verify_calls.py`: the rogue-answered calls `injected` at the `application1` members,
  none at `application2`.

From the repository root:

```console
just smoke-rpc ~/work/autobahn-crossbar/crossbar/.venvs/cpy314/bin/crossbar 18080
```

`smoke.sh` takes `PYTHON` (the client's venv python), `CROSSBAR` and `PORT` from the
environment (defaults `python3`, `crossbar`, 8080), and `DURATION` (default 120; the RPC
rogue holds its registration for part of the run, so the clients' probes overlap it). It
refuses to start when the port is taken. To keep it off the host's network, run it in its
own network namespace (`unshare -rn --pid --fork --mount-proc …`).

---

Code and configuration in this directory are MIT ([LICENSE](../../../LICENSE)); this
README is CC BY 4.0 ([LICENSE-docs](../../../LICENSE-docs)).
