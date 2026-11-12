set shell := ["bash", "-uc"]
import '.cicd/workflow.just'

# List the recipes
default:
    @just --list
