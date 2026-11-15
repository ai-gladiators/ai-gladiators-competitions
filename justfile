set shell := ["bash", "-uc"]
import '.cicd/workflow.just'

# List the recipes
default:
    @just --list

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

# Smoke-test the Crossbar.io single-node target: router plus nine clients for 120 s (env: PYTHON, CROSSBAR, DURATION, PORT; see its README).
smoke:
    ./targets/crossbar/single-node/smoke.sh
