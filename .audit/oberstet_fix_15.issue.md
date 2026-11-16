Issue:    ai-gladiators/ai-gladiators-competitions#15
Title:    crossbar/single-node: the client records calls it never sent, after its session has left, as failures
URL:      https://github.com/ai-gladiators/ai-gladiators-competitions/issues/15
State:    OPEN at freeze
Author:   @oberstet
Updated:  2026-11-15T20:01:36Z
Comments: 0
Frozen:   2026-11-15T20:31:16Z by `just new-branch`
SHA-256:  5d6136295a5ba2afcc14c805e3f9ddb01ca51b5b7bf77ff19c2b5409f3e7c0f6 (of everything below the line)

---

## What happened

A deployment of this target stopped its router on purpose, at 2026-11-15T02:52:03Z. Every joined client logged `leave` (`wamp.close.transport_lost`) and `disconnect`, as expected. Then client01–03 logged five more records, between 0.1 s and 0.5 s after their own `leave`:

```
{"name": "client02", "op": "leave", "reason": "wamp.close.transport_lost", "ts": "...T02:52:03.144219+00:00", ...}
{"name": "client02", "op": "disconnect", "clean": false, "ts": "...T02:52:03.148770+00:00", ...}
{"name": "client02", "op": "call", "peer": "client01", "ok": false, "error": "TransportLost", "latency_ms": 0.038, "ts": "...T02:52:03.258520+00:00", ...}
```

| client | record | after its `leave` |
|---|---|---|
| client02 | call, `TransportLost` | ~0.1 s |
| client01 | call, then publish, `TransportLost` | ~0.2 s |
| client03 | call, then publish, `TransportLost` | ~0.5 s |

Their latency is 0.03–0.07 ms: these calls never left the client. Each counts in the client's `errors`, so its `summary` says `ok: false`.

## Why

In `client/workload.py`, `workload()` checks `session.is_attached()` only at the top of its loop. Inside one iteration it sleeps (`rng.expovariate(args.rate)`), then calls, then maybe publishes. A `leave` during the sleep is noticed only after the call and the publish have both been tried on the detached session.

So the record says "the router failed this call", but the client made the call after it already knew there was no session. A match's results must contain the router's failures and nothing else: a record like this is a false alarm.

## Change

1. **The workload stops at `leave`.** `on_leave` (joined case) cancels the workload task (`state["task"]`). The loop also checks `is_attached()` after its sleep and between the call and the publish, so it does not depend on the cancellation alone.
2. **A call in flight at `leave` stays a failure.** A call sent before the `leave` that fails with `TransportLost` is a true result of the router's stop, and keeps its record.
3. **The README** (records): after `leave`, a client issues no call or publish until its next `join`. A `call` or `publish` record with `ok: false` therefore always means a request that was sent.
4. **A test:** a session that leaves during the workload's sleep produces no `call` or `publish` record after its `leave`. `smoke.sh`, or a unit test of the loop with a fake session.

## Rules

This also concerns `competitions/crossbar-v0.md`: a match must say what a peer restart or a router stop during the match means for the score. That question stays open in the rules. This issue only makes the client's records true.

## Acceptance

- `just check` and `just smoke` pass, with the test of 4.
- Stopping the router during a run: no client logs a `call` or `publish` after its `leave`. A client whose call was in flight logs that call once, failed.

Note: This work was completed with AI assistance (Claude Code).

---

## Comments at freeze (0)

