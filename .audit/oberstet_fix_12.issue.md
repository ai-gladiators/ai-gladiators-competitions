Issue:    ai-gladiators/ai-gladiators-competitions#12
Title:    crossbar/single-node: authorize the public keys of client05 to client09
URL:      https://github.com/ai-gladiators/ai-gladiators-competitions/issues/12
State:    OPEN at freeze
Author:   @oberstet
Updated:  2026-11-15T02:37:49Z
Comments: 0
Frozen:   2026-11-15T02:38:23Z by `just new-branch`
SHA-256:  207b73f6c6dfc9f931a86743e44ebad89701fe773e4f025eada6ad53d99a041a (of everything below the line)

---

## Why

All nine clients of the target are now deployed. client01–03 (realm `public`, anonymous) passed their barrier and run their workload. client04 is authorized (competitions#4) and waits for its peers client05 and client06 (competitions#6).

The other five Cryptosign clients generated their keys on their first start. As designed (README, "Keys"), the router refuses them while their principals' `authorized_keys` are empty. Since competitions#10 they back off to one attempt every 300 s:

```
{"attempt": 296, "error": "wamp.error.not_authorized", "message": "extra.pubkey provided does not match any one of authorized_keys for the principal", "name": "client05", "next_s": 300.0, "ok": false, "op": "refused", "realm": "application1", ...}
```

The private keys never left their clients. The public keys:

| principal | realm | public key |
|---|---|---|
| client05 | application1 | `4f0b05f9d6fd5092e1a1d16b1cc68ca4c9f23c638ef198f17aaa20317232a1ac` |
| client06 | application1 | `6239a79c07f832f9a4997441f7fbb697a5a7264228b403d1cbfc3a57fbd26b92` |
| client07 | application2 | `4071d850ce56f79c17720bf5ab9e54c90c1d52180b7101a2e68b2d5947e8eb38` |
| client08 | application2 | `18698cceb8a83d9949658a7e6b1d41b17396a5806ffab265bb11c7c4dcbcb24c` |
| client09 | application2 | `d76e311bcf353ae194589911ee9d9ebf35444cb0c0262d9f8031573dcb2486ba` |

## Change

- `targets/crossbar/single-node/.crossbar/config.json`: put each key into `authorized_keys` of its principal, in **both** transports' `cryptosign` blocks (the RawSocket one and the WebSocket one). client04 keeps its key. After this change, no principal's list is empty.
- `smoke.sh` is unaffected: it replaces `authorized_keys` in its own copy of the configuration.

## Acceptance

- `config.json` is valid JSON. In both blocks, each of client04–09 holds exactly one key: client04 its existing key, client05–09 the key in the table.
- `just smoke` passes.
- Once the router runs this commit, each of client05–09 logs `join` within 300 s of the router's start, instead of `refused`. Then all six Cryptosign clients log `workload_start` with `missing: []`.

Note: This work was completed with AI assistance (Claude Code).

---

## Comments at freeze (0)

