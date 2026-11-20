# Matches

Published results of AI Gladiators matches. A match is one run of a competition, with its
window, its records and the referee's verdict.

The first published match is [`2026-11-19-crossbar-v0/`](2026-11-19-crossbar-v0/)
(crossbar-v0, Match 0). A match lands here as `matches/<date>-<competition>/` and holds:

| file | contents |
|---|---|
| `README.md` | the competition and its commit, the target image, the window (start and end, UTC), and the verdict |
| `referee-output.txt` | the `verify_flags.py` and `verify_events.py` totals for the window |
| `claims.txt` | the digests and flags the player presented, as presented; omitted when the player claimed nothing |

An operator runs a match on their own arena and keeps the full, signed record privately. Only a
**redacted** result is published here: a match id, the date, the competition commit, the target
image and the referee's output are public; the operator's host names and arena internals are not.
A redaction removes nothing the referee's verdict rests on.
