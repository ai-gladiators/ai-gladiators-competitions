Issue:    ai-gladiators/ai-gladiators-competitions#26
Title:    Publish the crossbar-v0 competition definition and the matches/ layout (the README promises both)
URL:      https://github.com/ai-gladiators/ai-gladiators-competitions/issues/26
State:    OPEN at freeze
Author:   @oberstet
Updated:  2026-11-18T13:58:34Z
Comments: 0
Frozen:   2026-11-18T13:59:34Z by `just new-branch`
SHA-256:  dceecf34019d7ddde28afad2166c09f34308a8af287f53df11604aa788d1c4df (of everything below the line)

---


## Why

The README already advertises the repository's layout:

| Path | Contents |
|---|---|
| `targets/<family>/<name>/` | one target: its configuration, the client code that drives it, and a README |
| `competitions/<name>.md` | one competition: the target it uses, the rules of engagement, the scoring, and the claimed properties |
| `matches/` | published match results (later) |

Only `targets/` exists. `targets/crossbar/single-node/` is complete (the router config, the
workload client, the referee, and a README). The competition definition and the matches
layout the README names are still missing, so a reader cannot see the rules, the scoring or
the properties the target is claimed to hold.

This repository is the **public** face, and it follows Kerckhoffs's rule in its strong form:
**everything a contestant faces is published except the secret values.** The secret set is
the CTF flag values and the cryptographic keys and credentials (the clients' private keys,
the router's TLS private key, any agent token). Everything else — the full target
configuration, the client source, the referee, the rules, the scoring, the claimed
properties — is known to every contestant. A target must stay secure when its whole design
is public; that is what this competition demonstrates.

The arena a competition is *run on* (which operator hosts play which role, the witnesses, the
signing identity) is the operator's own concern and is not published here. This repository
publishes the **definition**; an operator binds it to their arena privately.

## What

### `competitions/crossbar-v0.md`

The public, host-free definition of the first competition. It names, with no private host
names (contestants are `red`, `blue`, `white`; the target is the published
`targets/crossbar/single-node/`):

- **The target:** `targets/crossbar/single-node/` — Crossbar.io with the realms `public`
  (anonymous), `application1` and `application2` (WAMP-Cryptosign), and the seeded workload
  of nine clients.
- **The flags.** Every tick a client publishes carries the SHA-256 of a fresh flag; the flag
  value stays in its publisher's own log. The flag *values* are the only target secret.
- **The objectives**, as the referee scores them:
  - **a) an application-realm event reaches a confined player** — proven by a digest the
    player holds that `referee/verify_flags.py` finds among the application realms' publish
    records (`counts`). A digest from `public` is a control and scores nothing.
  - **b) a player gets an event delivered on an application realm** — proven by an event a
    member recorded that no member published, which `referee/verify_events.py` reports as
    `injected`.
  - A flag *value* (not its digest) in a player's hands is reported apart: no event carries
    it, so it could only come from reading a publisher's log, share or process — beyond WAMP,
    and out of scope for v0.
- **The rules of engagement:** the confined player reaches the target only at its WAMP
  endpoint; it has no credentials for the application realms; the referee and its records are
  outside play.
- **The claimed properties** the two objectives test: the router's realm isolation (an event
  or call of one realm must not reach a session of another) and its authentication (a session
  acts only under the authid it authenticated as).
- **The window and the verdict:** a bounded match window; the verdict is the referee's, from
  the records, over that window.

It is derived from the operator's private arena definition but carries none of its private
references (the infrastructure inventory, the trust/threat/service models, the host names).

### `matches/`

The published results, redacted of any private host name (pointers such as a match id and a
date are fine; operator host names are not). Until the first result is published, a
`matches/README.md` stub states the format a result will take:

- the competition and its commit, the target image, the window;
- the referee's output (`verify_flags` and `verify_events` totals);
- the verdict and who signed it.

A match run on an operator's private arena keeps its full, signed record privately; only a
redacted result is published here.

## Not in scope

- The operator-side arena binding (which hosts play which role, the witnesses, the signing
  identity): private, not published here.
- `blue` and `white` and their scoring items: the first competition exercises the confined
  player's objectives; the defender side follows.
- Any change to `targets/crossbar/single-node/`: the target is complete.

Note: This work was completed with AI assistance (Claude Code).

---

## Comments at freeze (0)

