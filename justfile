set shell := ["bash", "-uc"]
import '.cicd/workflow.just'

# List the recipes
default:
    @just --list

# Smoke-test the Crossbar.io single-node target: router plus nine clients for 120 s (env: PYTHON, CROSSBAR, DURATION; see its README).
smoke:
    ./targets/crossbar/single-node/smoke.sh
