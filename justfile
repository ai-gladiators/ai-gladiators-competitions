set shell := ["bash", "-uc"]
import '.cicd/workflow.just'

# Show the project banner and list the recipes.
default:
    #!/usr/bin/env bash
    set -e
    GIT_REV=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
    echo ""
    echo "==============================================================================="
    echo "                           AI Gladiators: Competitions                         "
    echo ""
    echo "     The public match ledger: competition definitions, targets, results        "
    echo ""
    echo "   Repository:  ai-gladiators/ai-gladiators-competitions                       "
    echo "   Git Version: ${GIT_REV}                                                     "
    echo "   Copyright:   AI Gladiators                                                  "
    echo "   License:     MIT (code) + CC BY 4.0 (docs)                                  "
    echo "==============================================================================="
    echo ""
    just --list
    echo ""

# Copy a competition's target + referee into ai-gladiators-briefing from its
# allowlist manifest (competitions/<name>.instantiate.toml), with provenance; stages,
# never commits. Land the briefing change via its own Way-A branch + review.
#
#   just instantiate crossbar-v0
#   just instantiate crossbar-v0 ../ai-gladiators-briefing
#
# Materialize a competition's contestant-facing files into ai-gladiators-briefing (allowlist only).
instantiate competition briefing_path="../ai-gladiators-briefing":
    @python3 "{{justfile_directory()}}/tools/instantiate.py" "{{competition}}" --briefing "{{briefing_path}}"

# Verify a briefing copy has not drifted from the recorded source (args: competition, briefing path).
instantiate-check competition briefing_path="../ai-gladiators-briefing":
    @python3 "{{justfile_directory()}}/tools/instantiate.py" "{{competition}}" --check --briefing "{{briefing_path}}"

# Check that every committed uv.lock matches its pyproject.toml (no network needed when it does).
check:
    #!/usr/bin/env bash
    set -euo pipefail
    rc=0
    for lock in $(git ls-files '*uv.lock'); do
        dir="$(dirname "${lock}")"
        if uv lock --check --offline --project "${dir}" >/dev/null 2>&1; then
            echo "OK: ${lock} matches ${dir}/pyproject.toml"
        else
            echo "FAIL: ${lock} does not match ${dir}/pyproject.toml; run 'uv lock' in ${dir} and commit both" >&2
            rc=1
        fi
    done
    exit "${rc}"

# The client runs from its own venv, made here from its lock (uv sync --frozen); the
# router is the `crossbar` executable named by the first argument (default: on PATH).
#
#   just smoke ~/work/autobahn-crossbar/crossbar/.venvs/cpy314/bin/crossbar 18080
#
# Smoke-test the Crossbar.io single-node target: router plus nine clients for 120 s (args: crossbar, port; env DURATION).
smoke crossbar="crossbar" port="8080":
    #!/usr/bin/env bash
    set -euo pipefail
    client="{{justfile_directory()}}/targets/crossbar/single-node/client"
    uv sync --frozen --quiet --project "${client}"
    venv="${UV_PROJECT_ENVIRONMENT:-${client}/.venv}"
    export PYTHON="${venv}/bin/python" CROSSBAR="{{crossbar}}" PORT="{{port}}"
    "{{justfile_directory()}}/targets/crossbar/single-node/smoke.sh"
