Issue:    ai-gladiators/ai-gladiators-competitions#39
Title:    Lock the Crossbar client workloads to crossbar_env's shared closure (autobahn 26.10.2)
URL:      https://github.com/ai-gladiators/ai-gladiators-competitions/issues/39
State:    OPEN at freeze
Author:   @oberstet
Updated:  2026-11-27T18:38:06Z
Comments: 0
Frozen:   2026-11-27T18:38:39Z by `just new-branch`
SHA-256:  175c682f44c24f2896dca174f047f6268da8439f3e745a6a88396d0eef9f397e (of everything below the line)

---


## Context

The AAIARE collection now builds a single shared image level, `crossbar_env`
(collection#373): crossbar 26.10.1 + autobahn[all] 26.10.2 + the full locked closure
from crossbar's `v26.10.1` `uv.lock`, installed once into `/opt/crossbar/venv`. Every
arena VM extends it, so routers and workload clients run a **byte-identical** WAMP
runtime.

The workload image role (`autobahn_python_workload`) enforces this: before it installs a
target's client lock onto the shared venv, it **refuses** a lock that pins autobahn off
the level's release, or that **moves any package** of the shared level. Today all three
Crossbar client targets resolve a newer closure than `crossbar_env` ships, so a workload
bake (green2-10) would be refused:

| package | client lock (now) | crossbar_env (v26.10.1) |
|---|---|---|
| autobahn | 26.11.1 | **26.10.2** |
| cbor2 | 6.3.0 | 6.1.5 |
| cffi | 2.1.1 | 2.0.0 |
| numpy | 2.5.5 | 2.3.5 |
| pynacl | 1.6.2 | 1.6.1 |
| pyasn1 | 0.6.5 | 0.6.1 |
| … | (≈12 deps newer) | |

## Change

Align the three client targets **down** to `crossbar_env`'s concrete closure. For each of
`targets/crossbar/{single-node,single-node-rpc,two-node}/client/pyproject.toml`:

1. Pin `autobahn[asyncio,encryption,serialization]==26.10.2` (the level's release).
2. Add `environments = ["platform_python_implementation == 'CPython'"]` — the arena
   clients (and `crossbar_env` on the nodes) are CPython-only; this keeps the lock
   consistent with the shared level and avoids cbor2's PyPy(5.9.0)/CPython(6.1.5) split.
3. Add `constraint-dependencies` pinning the client's 25-package closure to
   `crossbar_env`'s concrete freeze, so no transitive dep floats above the shared level.

Then regenerate each `uv.lock`.

## Why down, not up

Pinning the fleet to crossbar's `v26.10.1` closure is the whole point of `crossbar_env`:
one shared, byte-identical WAMP runtime under every arena VM. The routers (green1/green1b)
already run autobahn 26.10.2; aligning the clients there makes the fleet consistent with
**no router rebake**. Bumping `crossbar_env` *up* instead would re-bake the routers and
move the whole v2b baseline, and crossbar 26.11.1 isn't tagged stable yet. This is an
image/attack-surface alignment, not a wire change — the live two-node mesh already shows
26.11.1 clients interoperating with the 26.10.2 routers.

## Verification

Ground truth = `crossbar_env`'s concrete venv freeze, reproduced by `uv sync --frozen` of
`git show v26.10.1:uv.lock` (crossbar) into a cpython-3.14.7 venv + `uv pip freeze` (the
committed lock is a multi-marker superset; only the concrete install is what the bake
checks against).

- `just check` green — every regenerated lock matches its `pyproject.toml` (offline).
- Each client lock is **zero-move** vs `crossbar_env` (25/25 packages, autobahn 26.10.2).
- `uv pip install --dry-run` of each client lock **into the crossbar_env venv** reports
  **"Would make no changes"** — exactly the workload image's pre-install gate. A green2
  bake passes.

## Notes

- The locks are narrowed to CPython (PyPy branches dropped). No PyPy clients exist today
  (the PyPy base is only *declared*); a future PyPy client would extend a PyPy
  `crossbar_env` of its own, so this stays consistent.
- Regenerate these locks from the level's freeze whenever `crossbar_env` is bumped (noted
  in each `pyproject.toml`).

## References

- collection#373 — the `crossbar_env` shared level and the `autobahn_python_workload`
  move-refusal gate.
- Downstream: metal green2 host_vars pin this repo's new commit before baking green2.

---

## Comments at freeze (0)

