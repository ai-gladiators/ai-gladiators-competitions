Issue:    ai-gladiators/ai-gladiators-competitions#6
Title:    crossbar/single-node: the client waits for all its peers, and the logs mark when the workload starts
URL:      https://github.com/ai-gladiators/ai-gladiators-competitions/issues/6
State:    OPEN at freeze
Author:   @oberstet
Updated:  2026-11-13T16:31:01Z
Comments: 0
Frozen:   2026-11-13T16:36:32Z by `just new-branch`
SHA-256:  a8a20093208a4e382f50b5d1f667685dd3370b1629d1bcb6fc0c78cb00e27bea (of everything below the line)

---

## Why

A match should measure the router under test, not the order in which a deployment happened to start its clients.

**What `workload.py` does today.** Every client is callee and subscriber (it registers `gladiators.<name>.echo` and subscribes to `gladiators.tick` right after joining), and caller and publisher. So no client depends on another having started first, as far as WAMP is concerned. Before its workload starts, `wait_for_peers` polls each peer's `echo`. But it gives up after `--peer-wait` seconds (default 60), logs `peer_ready ok=false`, and starts calling anyway.

**Why the deadline breaks a real deployment.** A deployment that boots its clients one after the other, for example one machine every two minutes so that each boots alone, starts the last client of a realm minutes after the first. The first client's barrier runs out, and its calls fail with `wamp.error.no_such_procedure` until its peers exist. Those failures are artefacts of the start: they would count as router errors in the analysis, and they are false alarms. A single client deployed on its own showed exactly that: `no_such_procedure` on every call while it had no peers.

`smoke.sh` doesn't see this, because it starts all nine clients at once.

## Change

`targets/crossbar/single-node/client/workload.py`:

1. **The barrier holds until every peer answers.** `--peer-wait` defaults to 0, meaning no deadline. A positive value keeps today's behaviour: give up after that many seconds and log `peer_ready ok=false`.
2. **Progress while waiting.** Every 30 s, a `peer_wait` record names the peers still missing and how long the client has waited. A peer that never comes is visible in the log, not hidden, and it is not counted as call errors.
3. **A `workload_start` record** when the first call or publish is about to happen, with the peers that answered. It is the client's statement that its realm is complete.

`targets/crossbar/single-node/smoke.sh`: pass a bounded `--peer-wait` (60), so that a peer that never starts fails the smoke test instead of hanging it.

`README.md`, the client section:
- the barrier, and why it has no deadline by default;
- **the measurement window of a match starts at the latest `workload_start` across all clients**. Records before it belong to the start-up, not to the match.

`--self-test`: unchanged. A test for the new records is welcome where it fits the self-test's shape.

## Not in scope

- Restarting a peer during a match. A peer that leaves and comes back is a real event; whether its `no_such_procedure` calls count against the router is a rules question, for the competition's rules document.
- How long a client waits before it reconnects after a refused join. That is a separate issue.

## Acceptance

- Three clients of one realm started 120 s apart: the first two log `peer_wait` records and no `call` record until the third has joined. Then each logs `workload_start`, and its calls succeed.
- With `--peer-wait 5` and a peer that never starts: `peer_ready ok=false` after about 5 s, then `workload_start`, as before.
- `just smoke` passes. Its summary is unchanged, apart from the new records.

Note: This work was completed with AI assistance (Claude Code).

---

## Comments at freeze (0)

