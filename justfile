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
