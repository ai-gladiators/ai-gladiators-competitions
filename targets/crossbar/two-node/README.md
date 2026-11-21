# Target: Crossbar.io, two-node mesh (PubSub + RPC)

Two [Crossbar.io](https://crossbar.io) routers (**node A** and **node B**), joined by
one router-to-router link (`rlink`), carrying nine
[AutobahnPython](https://github.com/crossbario/autobahn-python) clients that run a
seeded **exact-URI** workload exercising **both** publish/subscribe **and** remote
procedure calls, split across the two routers. Both router configurations, the link
configuration, the client code and the referee are published here in full. Only the
keys and the flag values stay out of the repository.

This is `crossbar-v2`, the third in a sequence:

- **v0** — single node, PubSub only;
- **v1** — single node, PubSub **and** RPC, **exact** URIs
  ([`../single-node-rpc/`](../single-node-rpc/));
- **v2 (this target)** — the exact-URI v1 workload carried across a **two-node mesh**,
  with the application realms forwarded across the rlink.

The client code, the flag scheme and the referee are **identical** to v1; read the
[single-node-rpc README](../single-node-rpc/README.md) first for the client, the flag
scheme (digests on the wire, flags only in the producer's own record), the durable
logging and the three referee tools. This README covers only what the mesh adds.

## The system under test

Everything in the single-node target's call path is here too (the WAMP stack, the
interpreter, the TLS library, the event loop). The mesh adds one element to the path of
a cross-node event or call: **the router-to-router link**.

| layer | routers | the link | clients |
|---|---|---|---|
| software | Crossbar.io on Twisted, epoll reactor | an rlink session on each router | AutobahnPython on asyncio |
| transport | WebSocket/RawSocket over TLS | **RawSocket over TLS** | WebSocket/RawSocket over TLS |
| serializer | CBOR | CBOR | CBOR |
| authentication | anonymous (`public`), WAMP-Cryptosign (`application1`, `application2`) | **WAMP-Cryptosign** | the same |

## The two routers

Each node runs the same three realms as the single-node target, plus an `rlink` role on
the two application realms:

| realm | authentication | role(s) | meshed across the link? |
|---|---|---|---|
| `public` | anonymous | `guest` | no (local to each node) |
| `application1` | WAMP-Cryptosign | `device`, `rlink` | yes |
| `application2` | WAMP-Cryptosign | `device`, `rlink` | yes |

The `public` / `guest` block and the `application1` / `application2` `device` blocks
(the six Cryptosign principals `client04`–`client09`) are **reconciled byte-for-byte**
with [`../single-node-rpc/.crossbar/config.json`](../single-node-rpc/.crossbar/config.json):
the realm and auth design is the authoritative single-node one. The mesh only **adds**,
to [`nodeA/.crossbar/config.json`](nodeA/.crossbar/config.json) and
[`nodeB/.crossbar/config.json`](nodeB/.crossbar/config.json):

- an **rlink listener** — a RawSocket-over-TLS transport with a WAMP-Cryptosign
  `cryptosign` principal named `rlink`, whose `authorized_keys` is the **other node's
  public key**; it pins no realm and carries role `rlink` on the application realms;
- on each application realm, an **`rlinks` entry** that dials the other node's rlink
  listener and forwards that realm. Each node forwards its own local traffic to the peer
  (`forward_local_events` / `forward_local_invocations` true) and does not re-forward
  what it receives (`forward_remote_*` false), so the two links form a full mesh without
  looping an event or an invocation back;
- the `rlink` **role** on each application realm, granting `{publish, subscribe, call,
  register}` under the `gladiators.` prefix.

Distinct ports let both nodes run on one host for the smoke test: node A listens for
clients on `8080` and for the rlink on `8081`; node B on `8082` (clients) and `8083`
(rlink). Node A's rlinks dial node B's `8083`; node B's dial node A's `8081`.

The effect: because the workload's subscriptions **and** registrations are **exact**, a
**tick** published on `application1` at node A reaches the `application1` members at node
B, and a **call** to a procedure registered on `application1` at node A is routed to its
callee at node B — and the reverse, and likewise for `application2`. `public` is not
meshed and is local to each node.

### The link's transport

Stated as plain target facts (the authoritative values are in the two configs):

- the client-facing transports and the rlink both run over **TLS**;
- the rlink authenticates with **WAMP-Cryptosign**.

### A cross-node caller's disclosed identity

A plain behaviour of the SUT's mesh, worth stating because the workload records it: when
a call is **forwarded across the rlink**, the callee sees the forwarded caller's
**authrole** (`device`) disclosed in place of its **authid**. Same-node calls disclose
the caller's authid as usual. The workload notes this on cross-node calls (its
`check_echo` records `router reported another caller authid`); the payload and the
callee are correct, so it is a property of the link, not a match violation, and the
referee scores a call from the **result digest** (the callee's answer flag), not from
this call-side note.

## The three build-gates (verified against the built configs)

An rlink forwards an exact subscription or registration, but **not** a prefix/wildcard
one. For the exact-URI v1 workload to mesh correctly, three properties must hold of the
committed configs. Each was checked against `nodeA/.crossbar/config.json` and
`nodeB/.crossbar/config.json` as built (and is re-checked by
[`verify_target.py`](verify_target.py) at build time, and again at the top of
[`smoke.sh`](smoke.sh)):

1. **The application realms are rlink-forwarded.** On **both** nodes, each of
   `application1` and `application2` has an `rlinks` entry that dials the peer's rlink
   listener over TLS, with `forward_local_events` and `forward_local_invocations` true —
   so this node's events and invocations reach the peer. *(Present on both nodes for
   both realms; no change was needed.)*

2. **The `rlink` role grants publish + subscribe AND register + call** on the forwarded
   realms. On **both** nodes, the `rlink` role on **both** application realms allows all
   four of `{publish, subscribe, call, register}` under the `gladiators.` prefix. Both
   pairs are required: subscribe/publish carry cross-node **PubSub**, register/call carry
   cross-node **RPC**; a link role missing either pair would silently drop one paradigm.
   *(Present on both nodes for both realms; no change was needed.)*

3. **Exact URIs throughout.** The workload registers `gladiators.<name>.echo` and
   subscribes to `gladiators.tick` and `gladiators.attacker.probe` — all **exact**, no
   prefix/wildcard subscriptions or registrations (verified in
   [`client/workload.py`](client/workload.py); its `--self-test` asserts the URI
   scheme). This is what makes **both** ticks and echo calls forward across the rlink.
   Role *permissions* stay prefix-based (a role may act anywhere under `gladiators.`);
   only the clients' own subscriptions and registrations are exact.

All three gates held on the seeded configs for **both nodes** and **both application
realms**, so **no config edit was required** — the build only added the client code, the
referee, the smoke harness and this documentation. The live smoke run confirms the
consequence: ticks and calls both cross the rlink in both directions (below).

## The workload across the mesh

The client is [`client/workload.py`](client/workload.py), the same exact-URI program as
v1, with one addition: a `--node` label recorded in each client's `start` and `join`
records, so every log says which router the client connected to. What else differs is
only **which node each client connects to**, chosen at deployment so that every
application realm has members on **both** routers — a realm member reaches a realm member
on the other node only through the rlink. The split:

| node | clients |
|---|---|
| **A** | `client01` `client02` `client03` (public), `client04` `client05` (application1), `client07` (application2) |
| **B** | `client06` (application1), `client08` `client09` (application2) |

So `application1 = {04, 05 @A, 06 @B}` and `application2 = {07 @A, 08, 09 @B}` each span
the mesh, while `public = {01, 02, 03 @A}` sits together on node A (it is not meshed, so
its members must share a node). Every member's ticks **and** peer-calls to a member on
the other node are forwarded across the rlink.

### Keys

There is no key material in this repository.

- **The TLS key and certificate** of each node (`<node>/.crossbar/server.key`,
  `server.crt`) are generated per deployment. Each node must also **trust the other
  node's certificate** for the rlink: the rlink transport names
  `ca_certificates: ["peer.crt"]`, so a deployment places the peer's certificate at
  `<node>/.crossbar/peer.crt`. For a local run, make a self-signed `localhost` pair per
  node and copy each node's `server.crt` to the other's `peer.crt`.
- **The node keys** (`<node>/.crossbar/key.priv`, `key.pub`) are generated by
  Crossbar.io (`crossbar keys --cbdir <node>/.crossbar`). Each node's **public** key goes
  into the other node's rlink-listener `authorized_keys`. A public key is not key
  material; the private node key never leaves its node. The committed configs carry
  example node public keys as placeholders (`a0a0…`, `b0b0…`).
- **The Cryptosign principals** `client04`–`client09` carry example public keys; each
  deployment writes its own clients' public keys into **both** node configs.

All of this is generated at smoke time and is `.gitignore`d
([`.gitignore`](.gitignore)): only `config.json` and source are tracked.

## VERIFY-at-build: the target is the mesh it claims to be

[`verify_target.py`](verify_target.py) reads both node configs and refuses (exit
non-zero) unless the target actually has the property a contestant is told to attack:
two routers, each application realm carrying an `rlink` role with call **and** register
(and publish/subscribe), an `rlinks` entry forwarding each application realm to the peer
over TLS, and a TLS rlink listener that authorizes a peer key. It reads only the
published configuration and is a target-correctness check, not an exploit. Standard
library only.

```console
python3 targets/crossbar/two-node/verify_target.py
```

## Smoke test

[`smoke.sh`](smoke.sh) runs the whole two-node target on one machine. It:

1. self-tests the client, then runs `verify_target.py` on the committed configs;
2. copies both node directories, generates a throwaway TLS pair per node (cross-trusted
   as each other's `peer.crt`), a node key per node, and throwaway client keys, and
   writes each node's public key into the other's rlink-listener `authorized_keys`, the
   client keys into both nodes, and the four ports into both configs;
3. starts both routers on the epoll reactor and waits for the web page of each over TLS
   and for the two router-to-router links to come up;
4. starts the nine clients **split across the two nodes** (every application realm on
   both), so a realm member's ticks and calls to a member on the other node cross the
   rlink — the **cross-node PubSub and RPC** that prove the mesh, asserted in **both
   directions** (node A ↔ node B);
5. requires the routers to refuse an unknown Cryptosign key and an anonymous client in
   `application1`;
6. injects three events on `application1` at node A that no member published
   ([`smoke_inject.py`](smoke_inject.py)) and stands up an RPC rogue that registers
   `gladiators.attacker.echo` and answers with a fabricated flag
   ([`smoke_rogue.py`](smoke_rogue.py)); the exact subscription/registration forwarding
   carries both to the application1 member on node B as well;
7. requires every client's own-realm ticks and matched call results, cross-node delivery
   both ways for PubSub and RPC, no errors, and every flag only in its own record; then
   runs the referee's `verify_flags.py`, `verify_events.py` and `verify_calls.py`, which
   must find the injections at the `application1` members and **nothing** at
   `application2` (realm isolation).

From the client venv and a Crossbar.io executable:

```console
uv sync --frozen --project targets/crossbar/two-node/client
PYTHON=targets/crossbar/two-node/client/.venv/bin/python \
CROSSBAR=~/work/autobahn-crossbar/crossbar/.venvs/cpy314/bin/crossbar \
DURATION=120 \
    targets/crossbar/two-node/smoke.sh
```

`smoke.sh` takes `PYTHON` (the client's venv python), `CROSSBAR`, `DURATION` (default
120) and the four ports `PORT_A_CLIENT` / `PORT_A_RLINK` / `PORT_B_CLIENT` /
`PORT_B_RLINK` (defaults 8080/8081/8082/8083) from the environment, and refuses to start
when a port is taken. To keep it off the host's network, run it in its own network
namespace (`unshare -rn --pid --fork --mount-proc …`). A `just smoke-two-node` wrapper
(mirroring `just smoke-rpc`) can be added to the repository `justfile`.

---

Code and configuration in this directory are MIT ([LICENSE](../../../LICENSE)); this
README is CC BY 4.0 ([LICENSE-docs](../../../LICENSE-docs)).
