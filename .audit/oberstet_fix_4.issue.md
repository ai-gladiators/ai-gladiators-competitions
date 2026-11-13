Issue:    ai-gladiators/ai-gladiators-competitions#4
Title:    crossbar/single-node: authorize client04's public key
URL:      https://github.com/ai-gladiators/ai-gladiators-competitions/issues/4
State:    OPEN at freeze
Author:   @oberstet
Updated:  2026-11-13T03:19:11Z
Comments: 0
Frozen:   2026-11-13T03:19:45Z by `just new-branch`
SHA-256:  0385479620d6d395e6e81078d9b7abc5a8d7fbd879bc58b979a153ab048fc4ec (of everything below the line)

---

## Why

The first deployed Cryptosign client, `client04` (realm `application1`), generated its own key on its first start. As designed (README, "Keys"), the router refuses it while its principal's `authorized_keys` is empty:

```
{"op": "refused", "error": "wamp.error.not_authorized",
 "message": "extra.pubkey provided does not match any one of authorized_keys for the principal", ...}
```

Everything before that works: TLS to the router and the WAMP handshake. The stack the client reports is CPython 3.14.7, AutobahnPython 26.11.1, asyncio on EpollSelector, OpenSSL 3.5.8, CBOR, and WebSocket over TLS with no extensions.

The private key never left the client. Its public key:

```
09b2ab118c29d021604b9f04f488a3e86dd82710dfe25f485c5d79d18b9b505b
```

## Change

- `targets/crossbar/single-node/.crossbar/config.json`: put this key into `authorized_keys` of principal `client04` in **both** transports' `cryptosign` blocks (the RawSocket one and the WebSocket one). Every other principal stays empty.
- `README.md`, "Keys": principals start with empty `authorized_keys`, and a deployment adds each client's public key as the client generates it. A public key in `config.json` is not key material: the private key stays with its client.
- `smoke.sh` is unaffected: it replaces `authorized_keys` in its own copy of the configuration.

## Acceptance

- `config.json` is valid JSON. Both `client04` principals hold exactly this key, and every other principal's list is empty.
- `just smoke` passes.
- Once the router runs this commit, `client04` logs `join` instead of `refused`.

Note: This work was completed with AI assistance (Claude Code).

---

## Comments at freeze (0)

