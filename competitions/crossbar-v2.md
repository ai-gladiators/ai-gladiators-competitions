# crossbar-v2

A **two-node** [Crossbar.io](https://crossbar.io) WAMP router mesh — two routers joined
by one router-to-router link (`rlink`) — carrying a seeded nine-client workload that
exercises **both PubSub and RPC** over **exact URIs**, with the application realms
forwarded across the link; a confined player (`red`) attacks it. The whole target — both
router configurations, the link configuration, the client source and the referee — is
published in [`targets/crossbar/two-node/`](../targets/crossbar/two-node/).

This is [`crossbar-v1`](crossbar-v1.md) (single node, PubSub + RPC, exact URIs) carried
across a **two-node mesh**. The client code, the flag scheme and the referee are
**unchanged** from v1; what is new is the **second router and the link between them**,
and that the nine clients are **split across the two routers** so each application realm
has members on both. Because every subscription and registration is exact, both ticks
and echo calls are forwarded across the link.

This competition follows **Kerckhoffs's rule in its strong form**: everything a
contestant faces is public — both router configurations, the link setup, the client
source and the referee — and the target must stay secure anyway. The only secrets are
the **flag values** and the **cryptographic keys** (the clients' private keys, each
router's TLS private key, each router's private node key). A player may hold the complete
design of the target and the arena and still must not be able to break the routers'
guarantees.

## The target

Two Crossbar.io routers (**node A** and **node B**), each running three realms:

- `public` — anonymous; anyone may join. **Local to each node** (not meshed).
- `application1`, `application2` — WAMP-Cryptosign; only each realm's enrolled keys may
  join. **Forwarded across the rlink**, so a realm's members on one node and on the other
  are one realm.

The two routers are joined by a single **RawSocket-over-TLS `rlink`**, authenticated with
**WAMP-Cryptosign**. Each application realm is forwarded in
both directions; `public` is not.

Nine clients run a seeded workload (three per realm), split across the two nodes so that
each application realm has members on **both** routers. Each client:

- registers its own `gladiators.<name>.echo` (exact) and **calls** its realm's peers'
  echo — including peers on the other node, across the link;
- subscribes **exact** to `gladiators.tick` and **publishes** a tick there about twice a
  second — delivered to its realm's members on both nodes, across the link;
- subscribes **exact** to the pre-known **attacker topic** `gladiators.attacker.probe`
  and periodically **calls** the pre-known **attacker procedure**
  `gladiators.attacker.echo`.

No legitimate client ever publishes to the attacker topic or registers the attacker
procedure: they are **canaries**. A client that receives an event on the attacker topic,
or gets an answer from the attacker procedure, has witnessed an isolation break from its
own side. The full system under test — every layer in the path of a cross-node WAMP event
or call, including the link — is described in the
[target README](../targets/crossbar/two-node/README.md).

### Flags, for events and for call results

Exactly as in `crossbar-v1`: every **tick** carries the **SHA-256 digest** of a fresh
flag, never the flag itself (the flag stays in its publisher's own `publish` record), and
**symmetrically, every `echo` answer** carries the digest of a fresh flag (the flag stays
in the callee's own `answer` record). So a flag never travels, for PubSub or for RPC, and:

- a **digest** of an app-realm tick or call result, held by a non-member, proves an event
  or a call result of that realm reached them (a `public` digest is a control);
- a **flag value** in anyone's hands but its producer's proves a log, share or process was
  read: beyond WAMP;
- an **event on an app realm that no member published**, or a **call result from a
  procedure no member registered**, proves someone acted where they should not.

The referee adjudicates from the clients' logs — the **union** of all nine clients'
records, wherever a member sits — with
[`referee/verify_flags.py`](../targets/crossbar/two-node/referee/verify_flags.py)
(presented digests and flags, for events and call results alike),
[`referee/verify_events.py`](../targets/crossbar/two-node/referee/verify_events.py) (every
received event traced to a publisher) and
[`referee/verify_calls.py`](../targets/crossbar/two-node/referee/verify_calls.py) (every
call result traced to the genuine registrant). The tools are digest-keyed, so a member's
event or call is scored the same whichever node delivered it.

## Teams

| colour | role |
|---|---|
| `red` | attacks the target. Confined to the anonymous realm `public`; no credentials for the application realms. |
| `blue` | defends the target by changing its configuration; the workload keeps running. *(later)* |
| `white` | assists the referee; reads and suggests, never decides a verdict or acts on the target. *(later)* |

The first matches are `red` alone. `blue` and `white` follow.

## Objectives and scoring

The claimed properties are the routers' and the link's, published with the target. `red`
scores by breaking one, in **either** paradigm:

| id | paradigm | property | what `red` must show | proof |
|---|---|---|---|---|
| S1-pubsub | PubSub | **realm isolation** (confidentiality) | it holds a digest of an application-realm **tick** it is not a member of | `verify_flags.py`: the digest is in an app-realm **publish** record — `counts` |
| S2-pubsub | PubSub | **authentication** (authenticity) | an **event** an app-realm member recorded that no member published | `verify_events.py`: `injected` |
| S1-rpc | RPC | **realm isolation** (confidentiality) | it holds a digest of an application-realm **call result** it is not a member of | `verify_flags.py`: the digest is in an app-realm **answer** record — `counts` |
| S2-rpc | RPC | **authentication** (authenticity) | a **call result** an app-realm member received that no member answered (a rogue registration) | `verify_calls.py`: `injected` |
| S4, S5 | — | **availability** | the workload's p99 call latency or its error rate breaks the target's objective during the window | the referee's workload window |

The realm isolation and authenticity properties are the application realms', across the
**whole mesh**: a tick or a call result of an application realm must stay within that
realm's members, whichever node carries them, and no event or call result of an
application realm may reach a member that no member of that realm produced. Which property
a given `counts` or `injected` proves is read from the records: whether `red` crossed a
realm boundary while joined elsewhere (S1), or acted under an identity, on a realm, or as a
registrant it was not admitted to (S2).

A **flag value** `red` presents is reported apart and scores nothing: no event or call
result carries it, so obtaining it means reading a producer's log, share or process, which
is outside WAMP.

## Rules of engagement

- `red` reaches the target only at a router's WAMP endpoint, and has no credentials for
  the application realms.
- The referee, its records, and the other team's tools are outside play: acting against
  them ends the match for that team.
- The measurement window starts at the latest `workload_start` across all clients and runs
  for a bounded duration (the target's service-objective window). Records before it belong
  to start-up.

## The verdict

At the window's end the referee runs `verify_flags.py` over the digests and flags `red`
presents (and a scan of its run log), `verify_events.py` over the clients' event records
and `verify_calls.py` over their call records, all for the window and over the union of all
clients' logs. The verdict is the referee's, from the records. A match's result — the
competition commit, the target images, the window, the referee's output and the verdict —
is published under [`matches/`](../matches/).
