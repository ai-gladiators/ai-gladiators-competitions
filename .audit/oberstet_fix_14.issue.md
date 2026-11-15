Issue:    ai-gladiators/ai-gladiators-competitions#14
Title:    crossbar/single-node: the client declares its dependencies in pyproject.toml and locks them in uv.lock
URL:      https://github.com/ai-gladiators/ai-gladiators-competitions/issues/14
State:    OPEN at freeze
Author:   @oberstet
Updated:  2026-11-15T19:57:41Z
Comments: 0
Frozen:   2026-11-15T20:03:59Z by `just new-branch`
SHA-256:  61f04af1b8b9b2578ac801cb72b6b078f2510f61f40ad63ac8bb9c5cd9da34a6 (of everything below the line)

---

## Why

`targets/crossbar/single-node/client/requirements.txt` asks for `autobahn[asyncio,encryption,serialization]>=24.4.2`. Every install resolves that line, and its dependencies, against PyPI at that moment. Two installs a few hours apart can run different software: a deployment of the nine clients of this target ran cbor2 6.2.1 on three clients and 6.3.0 on six, because cbor2 6.3.0 was uploaded in the middle of the deployment.

A match must run the same software on every client. The target is the right owner of that set: the workload's code and the packages it was tested with belong in one commit, reviewed together, visible in a diff. The images that run the clients then install the locked set and nothing else.

This also prepares workloads that need more than AutobahnPython (for example an industrial protocol library): they add the dependency to the client's project and update the lock.

## Change

1. `client/pyproject.toml`: a project that is not a package (`[tool.uv] package = false`).
   - `requires-python` names the interpreter series the clients run (3.14).
   - The dependencies: `autobahn[asyncio,encryption,serialization]==<release>`, pinned to the exact AutobahnPython release the clients run (today 26.11.1). An image for this target refuses a lock whose autobahn release differs from the one it bakes, so the release is stated here, not only in the lock.
2. `client/uv.lock`, generated with `uv lock` and committed. It covers Linux x86-64; other platforms may resolve too, but are not required.
3. `client/requirements.txt` removed. The README ("Each client runs …", the smoke test's `PYTHON`) and `smoke.sh`'s messages say how to make the venv instead:

   ```
   uv sync --frozen --project targets/crossbar/single-node/client
   ```

   `--frozen` installs the lock as committed, and fails if the lock no longer matches `pyproject.toml`.
4. A `just` recipe that fails if the lock is out of date (`uv lock --check`), run by `just check` and in CI. A dependency change without a new lock is then refused before review.

## Acceptance

- `uv lock --check` passes on the committed files. `just check` runs it.
- `just smoke` passes with `PYTHON` from a venv made by `uv sync --frozen`.
- `uv export --frozen --no-dev` lists autobahn at exactly the release in `pyproject.toml`, and every package with a hash.
- No `requirements.txt` remains under `client/`, and no file refers to it.

## Not in scope

- How the images install the lock: that is the image build's business (aaiare collection).
- The router (`targets/crossbar/single-node/.crossbar/`): it runs Crossbar.io from its own image.

Note: This work was completed with AI assistance (Claude Code).

---

## Comments at freeze (0)

