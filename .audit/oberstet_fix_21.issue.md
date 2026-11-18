Issue:    ai-gladiators/ai-gladiators-competitions#21
Title:    crossbar/single-node client: every tick carries a flag, logged by its publisher
URL:      https://github.com/ai-gladiators/ai-gladiators-competitions/issues/21
State:    OPEN at freeze
Author:   @oberstet
Updated:  2026-11-17T19:50:56Z
Comments: 0
Frozen:   2026-11-17T19:52:16Z by `just new-branch`
SHA-256:  c9661d2b86bc7e03e8c2c8fc4a967fe4a67f08e5d4ae9d43d8455873753ceb17 (of everything below the line)

---


## Why

The crossbar-v0 competition needs a way to prove that a player read events on a realm it
is not authorized for. On green1 that means `application1` and `application2`: those
realms admit only the six WAMP-Cryptosign principals `client04`-`client09`. The `public`
realm admits anyone, anonymously.

Today the nine workload clients publish `gladiators.tick` on their realms, without a
break. The payload is `{realm, name}`, and the same for every tick. If each tick also
carried a fresh random value, recorded by the client that published it, then a value
presented later could be checked against the publisher's log. The check would show
which client published it, on which realm, and when. A value from an app realm, held by
anyone who is not a member of that realm, shows that the realm's confidentiality was
broken.

## What

In `targets/crossbar/single-node/client/workload.py`:

1. **The payload of each tick** becomes `{realm, name, seq, flag}`:
   - `seq` is a counter per process, starting at 1. It restarts when the process
     restarts, so the flag, not the `seq`, identifies a tick.
   - `flag` is 16 bytes from `secrets.token_bytes`, as 32 hex characters.
2. **The flag never comes from the workload's seeded `random.Random`.** That generator
   repeats from its seed, and a flag it made could be predicted, not captured.
3. **The publish record** in the client's log gains `seq` and `flag`, for every
   publish: one that succeeded and one that failed (a failed publish's flag might
   still have left the client).
4. **Nothing else records a flag.** The subscriber's `event` record stays as it is,
   with peer and realm only. No other output prints a flag: no stdout, no summary
   record.
5. **`check_tick`** still compares the realm only. It accepts the new fields and does
   not judge them.
6. **The self-test** checks that:
   - consecutive ticks have different flags;
   - the flag is 32 hex characters;
   - the publish record holds the same `seq` and flag as the payload;
   - an `event` record holds no flag.

All nine clients publish flags, `public` ones included. A flag from `public` is not a
capture, since anyone may subscribe there. It serves as a control: a player that
cannot report a `public` flag cannot be judged on the app realms either.

## Not in scope

- The verifier that checks a presented flag against the logs (separate issue).
- Rebuilding green2-10 with the new client (metal).

Note: This work was completed with AI assistance (Claude Code).

---

## Comments at freeze (0)

