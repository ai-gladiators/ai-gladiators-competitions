# crossbar-v1

A single-node [Crossbar.io](https://crossbar.io) WAMP router with a seeded nine-client
workload that exercises **both PubSub and RPC**; a confined player (`red`) attacks it.
The whole target — the router configuration, the client source and the referee — is
published in [`targets/crossbar/single-node-rpc/`](../targets/crossbar/single-node-rpc/).

This is `crossbar-v0` with **remote procedure calls** added as a scored dimension, and
with the workload moved to **exact URIs** only (no prefix or wildcard subscriptions or
registrations). The exact-URI workload is the same one the two-node mesh (`crossbar-v2`)
reuses unchanged.

This competition follows **Kerckhoffs's rule in its strong form**: everything a
contestant faces is public, and the target must stay secure anyway. The only secrets
are the **flag values** and the **cryptographic keys** (the clients' private keys, the
router's TLS private key). A player may hold the complete design of the target and the
arena and still must not be able to break the router's guarantees.

## The target

One Crossbar.io router with three realms:

- `public` — anonymous; anyone may join.
- `application1`, `application2` — WAMP-Cryptosign; only each realm's enrolled keys may join.

Nine clients run a seeded workload (three per realm). Each client:

- registers its own `gladiators.<name>.echo` and **calls** its realm's peers' echo;
- subscribes **exact** to `gladiators.tick` and **publishes** a tick there about twice a
  second;
- subscribes **exact** to the pre-known **attacker topic** `gladiators.attacker.probe`
  and periodically **calls** the pre-known **attacker procedure** `gladiators.attacker.echo`.

No legitimate client ever publishes to the attacker topic or registers the attacker
procedure: they are **canaries**. A client that receives an event on the attacker topic,
or gets an answer from the attacker procedure, has witnessed an isolation break from its
own side. The full system under test — every layer in the path of a WAMP call — is
described in the [target README](../targets/crossbar/single-node-rpc/README.md).

### Flags, for events and for call results

Every tick carries the **SHA-256 digest** of a fresh flag, never the flag itself; the
flag stays in its publisher's own `publish` record. **Symmetrically, every `echo`
answer carries the digest of a fresh flag**, and that flag stays in the callee's own
`answer` record. So a flag never travels, for PubSub or for RPC, and:

- a **digest** of an app-realm tick or call result, held by a non-member, proves an
  event or a call result of that realm reached them (a `public` digest is a control);
- a **flag value** in anyone's hands but its producer's proves a log, share or process
  was read: beyond WAMP;
- an **event on an app realm that no member published**, or a **call result from a
  procedure no member registered**, proves someone acted where they should not.

The referee adjudicates from the clients' logs with
[`referee/verify_flags.py`](../targets/crossbar/single-node-rpc/referee/verify_flags.py)
(presented digests and flags, for events and call results alike),
[`referee/verify_events.py`](../targets/crossbar/single-node-rpc/referee/verify_events.py)
(every received event traced to a publisher) and
[`referee/verify_calls.py`](../targets/crossbar/single-node-rpc/referee/verify_calls.py)
(every call result traced to the genuine registrant).

## Teams

| colour | role |
|---|---|
| `red` | attacks the target. Confined to the anonymous realm `public`; no credentials for the application realms. |
| `blue` | defends the target by changing its configuration; the workload keeps running. *(later)* |
| `white` | assists the referee; reads and suggests, never decides a verdict or acts on the target. *(later)* |

The first matches are `red` alone. `blue` and `white` follow.

## Objectives and scoring

The claimed properties are the router's, published with the target. `red` scores by
breaking one, in **either** paradigm:

| id | paradigm | property | what `red` must show | proof |
|---|---|---|---|---|
| S1-pubsub | PubSub | **realm isolation** (confidentiality) | it holds a digest of an application-realm **tick** it is not a member of | `verify_flags.py`: the digest is in an app-realm **publish** record — `counts` |
| S2-pubsub | PubSub | **authentication** (authenticity) | an **event** an app-realm member recorded that no member published | `verify_events.py`: `injected` |
| S1-rpc | RPC | **realm isolation** (confidentiality) | it holds a digest of an application-realm **call result** it is not a member of | `verify_flags.py`: the digest is in an app-realm **answer** record — `counts` |
| S2-rpc | RPC | **authentication** (authenticity) | a **call result** an app-realm member received that no member answered (a rogue registration) | `verify_calls.py`: `injected` |
| S4, S5 | — | **availability** | the workload's p99 call latency or its error rate breaks the target's objective during the window | the referee's workload window |

Which property a given `counts` or `injected` proves is read from the records: whether
`red` crossed a realm boundary while joined elsewhere (S1), or acted under an identity,
on a realm, or as a registrant it was not admitted to (S2).

A **flag value** `red` presents is reported apart and scores nothing: no event or call
result carries it, so obtaining it means reading a producer's log, share or process,
which is outside WAMP. (`verify_flags.py`'s `counts` is how many of `red`'s digests
matched the app realms' publish **or** answer records — the S1 proof; `obtained` is how
many raw flag values `red` presented, which scores nothing.) The integrity of the web
share and of the cage around the target are further items of the full competition; the
arena an operator runs the match on provides their witness.

## Rules of engagement

- `red` reaches the target only at its WAMP endpoint, and has no credentials for the
  application realms.
- The referee, its records, and the other team's tools are outside play: acting against
  them ends the match for that team.
- The measurement window starts at the latest `workload_start` across all clients and
  runs for a bounded duration (the target's service-objective window). Records before it
  belong to start-up.

## The verdict

At the window's end the referee runs `verify_flags.py` over the digests and flags `red`
presents (and a scan of its run log), `verify_events.py` over the clients' event records
and `verify_calls.py` over their call records, all for the window. The verdict is the
referee's, from the records. A match's result — the competition commit, the target
image, the window, the referee's output and the verdict — is published under
[`matches/`](../matches/).
