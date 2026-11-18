Issue:    ai-gladiators/ai-gladiators-competitions#24
Title:    crossbar/single-node: ticks carry the flag's digest, not the flag; every received event is logged and traceable to one publish record
URL:      https://github.com/ai-gladiators/ai-gladiators-competitions/issues/24
State:    OPEN at freeze
Author:   @oberstet
Updated:  2026-11-18T01:34:48Z
Comments: 0
Frozen:   2026-11-18T01:35:52Z by `just new-branch`
SHA-256:  2da9f9b30d457d0b5633ba5bf141b501996e1bc02c201ebf8464b560244f797c (of everything below the line)

---


## Why

A player confined to the anonymous realm `public` breaks the app realms' isolation in
either direction:

- **a) An app-realm event reaches it**, by subscribing or in any other way.
- **b) It gets an event delivered on an app realm**, by publishing as a session there or
  in any other way.

Today a tick carries its flag in the clear (#21), and the referee's verifier (#22)
checks a presented flag against the publishers' logs. That covers (a), with two
weaknesses:

1. **The flag is everywhere the event goes:** in every subscriber's session, and in the
   router. So a flag in the player's hands cannot tell an event that leaked over WAMP
   from a publisher whose disk or memory was read.
2. **(b) is not traceable at all.**
   - A received tick's record holds no `seq` and nothing of the flag, so it cannot be
     matched to a publish record. A tick that states `name: client04` and
     `realm: application1` is logged `ok` by every member, whoever sent it, and so is a
     second copy of a genuine tick.
   - Only `gladiators.tick` is subscribed, so an event on any other topic of the
     namespace (the role permits all of `gladiators.`) reaches no client and no log.

## What

### The tick

On the wire, a tick is `{realm, name, seq, digest}`.

- `digest` is the SHA-256 of the flag, as a hex string:
  `hashlib.sha256(flag.encode("utf-8")).hexdigest()`. The flag itself is never
  published.
- **The flag stays in its publisher's `publish` record only**, as now. No other client,
  the router and the player see only the digest. The digest carries the flag's entropy
  (about 133 bits), and it cannot be inverted to the flag.
- The publisher still publishes with `exclude_me=False` (WAMP §12.2, Publisher
  Exclusion), so it receives and logs its own ticks as every other member does.

### The client

1. **One prefix subscription:** subscribe to `gladiators.` with `match: prefix`, instead
   of to `gladiators.tick` alone. Record every event received there.
2. **The `event` record** holds:
   - the topic (`details.topic`);
   - the stated `name`, `realm` and `seq`, and the `digest`, as received;
   - `publisher_seen` (the router's disclosure), as now.

   It records a violation when the event:
   - is on a topic other than `gladiators.tick`;
   - is not a tick of the expected shape (the violation says what is wrong);
   - states another realm than the client's own;
   - **names this client as its publisher but carries a digest this client never
     published.** The publisher keeps the digests of its own ticks for as long as it
     runs, so this is checked on arrival, without the referee.
3. **No flag in any record but `publish`**, as now. The `event` record holds the digest
   only.

### The referee

- **`referee/verify_flags.py` takes two kinds of claims:**
  - **A digest** (64 hex): looked up among the SHA-256 of every flag in the publish
    records. It gets `counts` (app realm, in the window), `control` (`public`) or
    `invalid`, as now. An app-realm event reached the player.
  - **A flag** (the token): looked up as now, but reported in a section of its own,
    `obtained from the publisher`, not in the totals of the digests. It shows that the
    publisher's log, share or process was read, which is beyond WAMP.
- **`referee/verify_events.py` (new):** standard library only, run over the nine logs
  and the match window. For every event record on `application1` or `application2`, it
  looks for the publish record with the same `name`, realm, `seq` and digest:
  - **matched:** exactly one such publish record, and this subscriber has not recorded
    this digest before;
  - **injected:** no such publish record; or a second copy at the same subscriber; or a
    topic other than `gladiators.tick`;
  - for every event, it also notes whether `publisher_seen` equals the `name` the
    payload states.

  Totals per realm and subscriber, one line per injected event, exit code 0. The
  verdict is the referee's.
- **Both tools treat the logs as untrusted input as far as parsing goes:** a malformed
  line is counted and reported, never a crash.

### The smoke test

The existing checks, with the flag now in `publish` records only and the digest in
events. Plus three events that must be reported as injected at each member:

- a tick that names another client, with a seq and digest nobody published;
- a second copy of a genuine tick;
- an event on a topic other than `gladiators.tick`.

A tick that names the receiving client itself, with a digest it never published, must
also be caught by that client on arrival. Every other event must be `matched`.
`verify_flags.py` must say `counts` for an app-realm digest, `control` for a public
one, and report a presented flag in its own section.

## Not in scope

- **RPC.** A callee does not log the calls it receives, only a caller logs its own calls.
  Calls into an app realm from outside are a follow-up.
- **Delivery completeness:** whether every member received every publish. That is
  availability, not isolation.
- **Topics outside `gladiators.`:** the role cannot subscribe there, so an event no
  session receives leaves nothing to observe at the clients.

Note: This work was completed with AI assistance (Claude Code).

---

## Comments at freeze (0)

