Issue:    ai-gladiators/ai-gladiators-competitions#2
Title:    first target: Crossbar.io single node, three realms, nine clients with a seeded workload
URL:      https://github.com/ai-gladiators/ai-gladiators-competitions/issues/2
State:    OPEN at freeze
Author:   @oberstet
Updated:  2026-11-12T21:42:38Z
Comments: 0
Frozen:   2026-11-12T21:43:14Z by `just new-branch`
SHA-256:  c30fc7e1c0c9eccbd76d64b7fff95b971086a6facf4be82365bf47703e04f383 (of everything below the line)

---

## Why

The first AI Gladiators competition attacks and defends a single [Crossbar.io](https://crossbar.io) router. Its target must be public, in line with Kerckhoffs's principle: everything except the keys. The target consists of the router configuration, the client code that drives it, and a workload whose logs show whether the router keeps its promises.

## Layout

```
targets/crossbar/single-node/
  README.md               what the target is, how to run it locally
  .crossbar/config.json   the router configuration
  web/index.html          the initial static content of the web share
  client/
    workload.py           the WAMP client each of the nine clients runs
    requirements.txt
  smoke.sh                router plus nine clients on one machine, then check the logs
```

## Router

- **One `universal` transport** on port 8080 over TLS. It carries WAMP over WebSocket (`/ws`), WAMP over RawSocket, and the static web share. The serializer is CBOR.
- **Three realms**, each with three clients:

  | realm | authentication | role | principals |
  |---|---|---|---|
  | `public` | anonymous | `guest` | `client01`, `client02`, `client03` |
  | `application1` | WAMP-Cryptosign | `device` | `client04`, `client05`, `client06` |
  | `application2` | WAMP-Cryptosign | `device` | `client07`, `client08`, `client09` |

  Three realms test two kinds of isolation: between the anonymous realm and an authenticated one, and between two authenticated realms.
- **Permissions are limited to the URI prefix `gladiators.`.** Each role may call, register, publish and subscribe there, and nowhere else.
- **Caller and publisher disclosure is on.** A callee sees the caller's authid as the router reports it. The workload returns that authid, so every caller can check that the router named it correctly.
- **No key material in the repository.** `config.json` refers to `server.key` and `server.crt`, and `.gitignore` excludes them. The README shows how to make a local self-signed pair. A deployment generates its own.
- **`authorized_keys` start empty** for the six Cryptosign principals. The public keys go in once the deployed clients have generated their keys. Until then, the router refuses every Cryptosign client.

## Client (`workload.py`)

The client is written with AutobahnPython on asyncio, using the `Component` API, and reconnects forever.

Arguments:

- `--name` (`client01` to `client09`), `--realm`, `--url`, `--trust` (the router certificate);
- `--key` (a Cryptosign private key; without it the client authenticates anonymously), `--generate-key` (creates the key file with mode 0600 and prints its public key);
- `--peers` (the other clients of its realm), `--seed`, `--rate`, `--log`, `--duration` (optional).

Each client:

1. registers `gladiators.<name>.echo`, which returns `{payload, callee, caller_seen}`;
2. subscribes to `gladiators.tick`;
3. in a loop driven by `--seed`, picks a peer, a payload of 16 to 1024 bytes, and an interval, then:
   - calls that peer's `echo`;
   - publishes a `gladiators.tick` that carries its realm and its name;
4. checks every answer:
   - the payload comes back unchanged;
   - in the authenticated realms, `caller_seen` is its own authid;
   - every tick it receives comes from its own realm.

**Log:** JSON Lines appended to `--log`, with an fsync after each record. A record holds the timestamp, the operation, the peer, the latency in ms, `ok` or the error, and a `violation` field whenever a check fails. Connects and disconnects are logged too. Call latency and error rate come straight out of these logs.

## Smoke test

`smoke.sh` (and a `just smoke` recipe):

1. copies the node directory to a temporary directory;
2. generates a throwaway TLS pair and six throwaway client keys, and writes their public keys into that copy's `config.json`;
3. starts Crossbar.io and the nine clients for 30 seconds;
4. requires every client's log to show successful calls, ticks from its own realm, no errors and no violations.

## Out of scope

- How the clients are deployed, their keys stored, and their logs collected.
- The competition's public rules (`competitions/`).
- Published match results (`matches/`).

## Acceptance

- `just smoke` passes on two machines.
- `config.json` starts with Crossbar.io, and the router serves the web page over TLS.
- The three realms are enforced: a Cryptosign client with an unknown key is refused, and an anonymous client trying to join `application1` is refused.
- The repository contains no private key.

Note: This work was completed with AI assistance (Claude Code).

---

## Comments at freeze (0)

