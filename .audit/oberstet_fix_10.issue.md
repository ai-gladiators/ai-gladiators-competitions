Issue:    ai-gladiators/ai-gladiators-competitions#10
Title:    crossbar/single-node: a client the router refuses backs off, instead of retrying every few seconds forever
URL:      https://github.com/ai-gladiators/ai-gladiators-competitions/issues/10
State:    OPEN at freeze
Author:   @oberstet
Updated:  2026-11-13T20:49:36Z
Comments: 0
Frozen:   2026-11-13T20:50:07Z by `just new-branch`
SHA-256:  12291712eac5d9d6a038466f207d5e53c4bd4ab8c35d4814015325e81b994c7f (of everything below the line)

---

## Why

A Cryptosign client makes its key on its first start. The router authorizes it only after its public key is committed into `config.json` and the router is redeployed. Until then, every join is refused with `wamp.error.not_authorized`. That is expected, and it can last hours.

**What the client does today.** The transport retries forever with `max_retry_delay: 5`. A refused join ends in a clean disconnect, and the client connects again a few seconds later. On the first real deployment, one client logged **412 `refused` records in 106 minutes**, about one every 15 s, until the router knew its key. Each attempt is a TLS handshake plus a Cryptosign authentication at the router.

**Why that matters.**
- A deployment brings up several clients at once and authorizes them in one router change. During that window, nine clients would cost the router a refused authentication about every 2 s, for as long as the window lasts.
- In the router's log, a client that is refused because it is not authorized yet looks exactly like an unknown key trying its luck. The real signal, a key the router should never have seen, drowns in the expected noise.
- A refusal is not a transient failure like a router that is still starting. Retrying it at the transport's pace gains nothing.

## Change

`targets/crossbar/single-node/client/workload.py`:

1. **Two kinds of failure, two paces.**
   - **A transport failure** (connection refused, timeout, TLS): unchanged. The fast retry is what lets clients start in any order.
   - **A refused join** (`refused`, any error): the delay before the next attempt doubles with each refusal in a row, from 5 s up to `--refused-max-delay` (default 300 s). A successful join resets it.
2. **The log says it.** Each `refused` record carries `attempt` (refusals in a row) and `next_s` (the delay before the next attempt).
3. **No dead end.** The client never gives up. Once the router knows its key, it joins within `--refused-max-delay` seconds at most. Restarting the client joins at once.

How the delay is applied is the implementation's choice, inside the process. It must not stop the client from reacting to a stop.

`README.md`, the client section: the two paces, the bound on the time to join after authorization, and the restart shortcut.

`smoke.sh`: the stranger and the intruder run for 5 s, so they see one or two refusals. The check stays "refused, with the right error", and also requires `attempt` and `next_s` on each `refused` record.

## Not in scope

- **The router's side,** for example a rate limit on authentication failures. That belongs to the target under test, not to the client.
- **Peers that leave during a match:** a rules question (competitions#6, "Not in scope").

## Acceptance

- `--self-test` covers the delay sequence: 5, 10, 20, … up to the cap, and a reset after a join.
- A client with an unknown key, run for 10 minutes against the smoke router: its `refused` records show the delays growing to 300 s (5, 10, 20, 40, 80, 160, 300). That is at most 8 records in the 10 minutes, against about 40 at today's pace.
- The same client, authorized while it is backing off (its key added, the router restarted): it joins within 300 s, and its `workload_start` follows.
- A router that is down at the client's start: the client connects within about 5 s of the router coming up. That is unchanged.
- `just smoke` passes.

Note: This work was completed with AI assistance (Claude Code).

---

## Comments at freeze (0)

