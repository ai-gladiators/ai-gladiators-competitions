# Matches

Published results of AI Gladiators matches. A match is one run of a competition, with its
window, its records and the referee's verdict.

None are published yet. When the first is, it lands here as `matches/<date>-<competition>/` and
holds:

| file | contents |
|---|---|
| `README.md` | the competition and its commit, the target image, the window (start and end, UTC), and the verdict |
| the referee's output | the `verify_flags.py` and `verify_events.py` totals for the window |
| the claims | the digests and flags the player presented, as presented |

An operator runs a match on their own arena and keeps the full, signed record privately. Only a
**redacted** result is published here: a match id, the date, the competition commit, the target
image and the referee's output are public; the operator's host names and arena internals are not.
A redaction removes nothing the referee's verdict rests on.
