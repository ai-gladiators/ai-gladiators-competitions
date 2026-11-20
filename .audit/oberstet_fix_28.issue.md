Issue:    ai-gladiators/ai-gladiators-competitions#28
Title:    Give the justfile default recipe a project banner (align with briefing/operator)
URL:      https://github.com/ai-gladiators/ai-gladiators-competitions/issues/28
State:    OPEN at freeze
Author:   @oberstet
Updated:  2026-11-20T14:00:33Z
Comments: 0
Frozen:   2026-11-20T14:01:22Z by `just new-branch`
SHA-256:  bb0c411e9cff32182d5f4b4e6f2a13a683672051a273f6f092fe632cdc3822e0 (of everything below the line)

---


## What

The `justfile` `default` recipe is a bare `@just --list` with no identity:

```
$ just
Available recipes:
    check ...
    smoke ...
```

The sibling ai-gladiators repos (`ai-gladiators-briefing`, `ai-gladiators-operator`)
and the wider estate (cf. `aaiare-fleet-manager`) print a banner first — repo name,
one-line purpose, git revision, copyright, license — then the recipe list. Align
this repo to the same pattern so `just` self-identifies.

## Change

Replace only the `default` recipe. Keep `check`, `smoke`, and every other recipe
as-is. The new default:

```just
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
```

## Notes

- Public repo; the banner carries no host or infrastructure names.
- Cosmetic only — no change to any check, the referee, or published match results.
- `just --list` still renders every recipe's one-line doc beneath the banner.

---

## Comments at freeze (0)

