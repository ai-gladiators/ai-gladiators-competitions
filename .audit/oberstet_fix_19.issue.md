Issue:    ai-gladiators/ai-gladiators-competitions#19
Title:    crossbar/single-node client: the log's fsync blocks the event loop, and on NFS each record re-sends a page
URL:      https://github.com/ai-gladiators/ai-gladiators-competitions/issues/19
State:    OPEN at freeze
Author:   @oberstet
Updated:  2026-11-16T20:56:07Z
Comments: 0
Frozen:   2026-11-16T20:58:57Z by `just new-branch`
SHA-256:  0627cec3f70a442755ffe8ba0ea7b1d62591158a71c725430ef120eeb5d43a64 (of everything below the line)

---

## What was measured

aaiare-metal#448 counted one workload client's traffic for 72 minutes, by destination and port. The counts add up to the client's network interface counter, to the byte.

- **77 % of what the client sends is its own log:** 51 MiB/h to the NFS export its log lives on. WAMP is 12 MiB/h; the root disk about 3.
- **The log is 4 MB/h of JSON,** but costs 51 MiB/h on the wire.

## Why

`targets/crossbar/single-node/client/workload.py`, `class Log`: every record (194 bytes on average) is written, then `flush()` and `os.fsync()`. The NFS client's statistics for the hour:
- one WRITE RPC per fsync, about 20,000 per hour;
- each WRITE carries 2,243 bytes on average: 11.5 times the record.

Linux's NFS client widens a small write to the whole page it caches, so each fsync re-sends the 4 KiB page from its start to the end of the file.

**The bytes are cheap; the waiting is not.** `Log.write()` runs on the asyncio event loop, so every record blocks the loop for one round trip to the NFS server. When the server is slow, every call and event of the client waits with it. In aaiare-metal#450, three clients had calls of 0.4–3.25 s, all during one minute when a large copy was read from the same NFS export. This mechanism would explain them, but that is not proven. If it holds, a match measures the storage, not the router.

## Change

The guarantee stays the same: a record is durable before the next one is written, and a crash loses at most the record in flight.

1. **Off the event loop.** One writer thread takes records from a queue and writes and syncs each in order; `Log.write()` only enqueues.
   - At `close()`, the queue is drained before the file closes.
   - The record's `ts` is still taken when the event happens, not when it is written.
   - A write or sync error is not swallowed: it ends the client with an error, as today.
2. **One stable write per line instead of write plus fsync.** Open the log with `O_DSYNC` (`os.open(path, O_WRONLY|O_APPEND|O_CREAT|O_DSYNC)`). Linux's NFS client does not widen writes on such a file, so each line should go out alone. That should be confirmed by measurement before relying on it.
3. **`--self-test`** covers:
   - order: a burst of records arrives in the file in order, complete;
   - drain: `close()` returns only after the last record is on disk;
   - error: a failing write ends the client with a non-zero exit.
4. **README:** what the log guarantees, and that writing it does not hold up the client.

## Acceptance

- `just check` (or the repo's equivalent) and `--self-test` pass.
- `just smoke` passes.
- Deployed to the nine clients (aaiare-metal), one hour measured as in aaiare-metal#448:
  - the NFS bytes per record fall to about one record plus RPC overhead;
  - the workload hour's criteria hold: 0 bad, 0 cross-realm, no reconnect, no tick lost;
  - p50 and p99 are no worse.
- A large read from the NFS export during the hour (as in aaiare-metal#450) no longer shows up as call latency. Or, if it still does, that is measured and said.

Note: This work was completed with AI assistance (Claude Code).

---

## Comments at freeze (0)

