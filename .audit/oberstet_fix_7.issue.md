Issue:    ai-gladiators/ai-gladiators-competitions#7
Title:    crossbar/single-node: smoke.sh hangs when PORT is taken, and fails with a traceback without autobahn
URL:      https://github.com/ai-gladiators/ai-gladiators-competitions/issues/7
State:    OPEN at freeze
Author:   @oberstet
Updated:  2026-11-13T18:31:25Z
Comments: 0
Frozen:   2026-11-13T18:35:34Z by `just new-branch`
SHA-256:  173187fbf5827213a358d5add8e40670d98e6b20a1e99213cef1360cf6c244ad (of everything below the line)

---

## What happened

On a machine that already runs a router on port 8080, `just smoke` printed `==> start Crossbar.io` and then nothing more, without ever finishing.

1. The smoke test's own router cannot bind the port and exits.
2. The readiness loop asks `https://localhost:${PORT}/` with `curl -s` and no timeout. The request reaches the *other* listener, which accepts the TCP connection but never completes the TLS handshake, so curl waits forever.
3. The loop checks whether the smoke router is still alive (`kill -0`) only after curl returns, so that check never runs.

A second start-up failure: with the default `PYTHON=python3` and no autobahn installed for it, `just smoke` ends in a Python traceback (`ModuleNotFoundError: No module named 'autobahn'`) from the client self-test. Nothing says that `PYTHON` and `CROSSBAR` must point to an environment with autobahn and Crossbar.io. The README says it, but the failure doesn't.

## Change

`targets/crossbar/single-node/smoke.sh`:

- Before starting the router, refuse when something already listens on `PORT`. Exit with an error that names the port and suggests `PORT=<free port> just smoke`.
- Give the readiness probe a timeout (`curl --max-time 5`), so a dead router or a foreign listener ends in the existing `FAIL: the web page is not served over TLS` path. It must not hang.
- Before the self-test, check the environment: `PYTHON` can import `autobahn`, and `CROSSBAR` is an executable. If either check fails, exit with a plain message that names the variable, the value it had, and what it needs. For example: `PYTHON=python3 cannot import autobahn; set PYTHON and CROSSBAR to a venv with autobahn and crossbar (see README)`.
- README: the paragraph that already names `PORT` (and the namespace advice) also says that a taken port is refused.

## Acceptance

- With another listener on the port, `just smoke` fails within seconds and names the port.
- With `PYTHON` pointing to a Python without autobahn, or `CROSSBAR` unset and not on the PATH, `just smoke` fails at once with that message and no traceback.
- On a free port, with both variables set, `just smoke` passes as before.

Note: This work was completed with AI assistance (Claude Code).

---

## Comments at freeze (0)

