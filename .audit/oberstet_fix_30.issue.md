Issue:    ai-gladiators/ai-gladiators-competitions#30
Title:    instantiate: materialize a competition's target + referee into ai-gladiators-briefing (allowlisted, traceable)
URL:      https://github.com/ai-gladiators/ai-gladiators-competitions/issues/30
State:    OPEN at freeze
Author:   @oberstet
Updated:  2026-11-20T15:15:10Z
Comments: 0
Frozen:   2026-11-20T15:16:22Z by `just new-branch`
SHA-256:  181a685236a7d86f978b2394198030f32dea8af7c35cb0736cd298d517eda68e (of everything below the line)

---


## Why

The three ai-gladiators repos split by trust boundary:

- `ai-gladiators-competitions` — the **source of truth** for target configurations
  and referee code, and the public human match ledger. **Denied from the
  contestant (`red`) corpus.**
- `ai-gladiators-briefing` — the **contestant corpus**: the only ai-gladiators repo
  `red` is handed. For Kerckhoffs, the full target a contestant faces must live
  here.
- `ai-gladiators-operator` — private operator workspace.

So a target authored in competitions must be **copied into briefing before a
match**, and that copy must be **traceable** (which competitions git version it
came from) and **safe** (competitions also holds leak-sensitive material — e.g.
`matches/2026-11-19-crossbar-v0/REVIEW.md` — that must never reach `red`'s corpus).

This issue adds the tooling for that copy. It keeps everything in-sync and
auditable: competitions stays authoritative; briefing holds a materialized,
provenance-stamped view regenerated before each match.

## The mechanism

### 1. A per-competition manifest (the allowlist)

`competitions/<name>.instantiate.toml` declares an **explicit list** of files to
copy, source → destination. This list is both the disclosure boundary and the
record of what a contestant is handed. Example (`crossbar-v0.instantiate.toml`):

```toml
# What ai-gladiators-briefing receives when crossbar-v0 is instantiated.
# Explicit allowlist. Nothing outside this list is ever copied.

[[file]]
src  = "targets/crossbar/single-node/.crossbar/config.json"
dest = "targets/crossbar/single-node/.crossbar/config.json"

[[file]]
src  = "targets/crossbar/single-node/client/workload.py"
dest = "targets/crossbar/single-node/client/workload.py"

[[file]]
src  = "targets/crossbar/single-node/referee/verify_flags.py"
dest = "referee/verify_flags.py"

# ... client pyproject/uv.lock, target README, referee verify_events.py, etc.
```

### 2. One parameterized recipe (not one per competition)

`just instantiate <competition> [briefing_path]`:

1. resolves and records the competition's current commit sha;
2. reads `competitions/<competition>.instantiate.toml`;
3. copies **only** the listed files into the briefing checkout
   (default: sibling `../ai-gladiators-briefing`);
4. writes a provenance file into briefing (see below);
5. **stages** the changes in briefing — it does **not** commit (see §4);
6. refuses on any missing source file, missing briefing checkout, or a `dest`
   that escapes the briefing tree.

A named alias (`instantiate-crossbar-v1` → `instantiate crossbar-v1`) can be added
for `just --list` discoverability, but the logic lives in the one core recipe.

### 3. Provenance (traceability)

`config.json` is JSON and cannot carry a comment, so the "copied from
competitions@`<sha>`" record goes in a **sibling file** the recipe writes into
briefing, listing the source repo, the resolved source commit, and each file's
source path + Git blob id. Reuse the existing `referee/check_drift.sh` pattern
(blob-id comparison) so briefing can self-verify its copy has not drifted from the
pinned source.

### 4. Governance: the briefing commit stays Way-A

The recipe materializes and **stages**; it never auto-commits into `red`'s corpus.
The human lands the briefing changes through briefing's own Way-A fix branch
("instantiate crossbar-v1 from competitions@`<sha>`") with the public-diff review.
The allowlist (§1) is the primary boundary; the review is the backstop.

### Safety boundary (non-negotiable)

- Copy is **allowlist only** — never a directory sweep or glob.
- Nothing from `matches/`, operator notes, or reviews is ever eligible.
- The recipe refuses rather than guesses (missing file, path escape, no briefing
  checkout).

## Deliverables

1. The `instantiate` recipe in `justfile` (manifest-driven, staging-only, with the
   refusals above).
2. The manifest schema (documented inline / in a short `competitions/README`
   section) and a validator if warranted.
3. The provenance writer + `check_drift.sh` regeneration.
4. **Worked example:** ship `crossbar-v0.instantiate.toml` and run it end-to-end to
   materialize the v0 single-node target + referee into briefing with provenance —
   validating the mechanism and making v0 cold-runnable under the corpus rules.

## Sequencing

This resolves the target-placement design (targets stay source-of-truth in
competitions; briefing holds materialized copies). It **precedes** building the
crossbar-v1 two-node target, which is authored in
`competitions/targets/crossbar/two-node/` and then instantiated into briefing via
this recipe.

---

## Comments at freeze (0)

