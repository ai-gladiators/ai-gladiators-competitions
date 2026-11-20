#!/usr/bin/env python3
"""Materialize a competition's target + referee into the ai-gladiators-briefing
contestant corpus, from an explicit allowlist manifest, with provenance.

This is the only sanctioned way target/referee files move from this repository
(the source of truth, denied from red's corpus) into ai-gladiators-briefing (the
contestant corpus red is handed). It copies ONLY the files a manifest names - never
a directory sweep - so nothing else in this repo (match results, reviews, operator
notes) can reach red's corpus. It stages the result in briefing; it never commits:
the briefing change lands through briefing's own Way-A branch + review.

Usage:
    tools/instantiate.py <competition> [--briefing PATH]   # copy + stage
    tools/instantiate.py <competition> --check [--briefing PATH]   # verify no drift

The manifest is competitions/<competition>.instantiate.toml:

    [[file]]
    src  = "targets/crossbar/single-node/.crossbar/config.json"   # path in THIS repo
    dest = "targets/crossbar/single-node/.crossbar/config.json"   # path in briefing

Provenance is written to briefing at .instantiated/<competition>.toml and records
the source commit and each file's source Git blob id, so --check can confirm the
copy has not drifted.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path


def _die(msg: str) -> None:
    print(f"REFUSING: {msg}", file=sys.stderr)
    raise SystemExit(1)


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    )
    return out.stdout.strip()


def _toplevel(start: Path) -> Path:
    try:
        return Path(_git(start, "rev-parse", "--show-toplevel"))
    except subprocess.CalledProcessError:
        _die(f"not a git repository: {start}")
        raise  # unreachable


def _blob(repo: Path, rel: str) -> str:
    return _git(repo, "hash-object", rel)


def _load_manifest(comp_root: Path, competition: str) -> list[tuple[str, str]]:
    manifest = comp_root / "competitions" / f"{competition}.instantiate.toml"
    if not manifest.is_file():
        _die(f"no manifest for '{competition}': {manifest}")
    data = tomllib.loads(manifest.read_text())
    entries = data.get("file", [])
    if not entries:
        _die(f"manifest names no files: {manifest}")
    pairs: list[tuple[str, str]] = []
    for i, e in enumerate(entries):
        src, dest = e.get("src"), e.get("dest")
        if not src or not dest:
            _die(f"manifest entry {i} missing src or dest: {manifest}")
        pairs.append((src, dest))
    return pairs


def _resolve_dest(briefing_root: Path, dest: str) -> Path:
    # dest must stay inside the briefing tree - no absolute paths, no .. escape.
    if Path(dest).is_absolute():
        _die(f"dest is absolute (must be relative to briefing): {dest}")
    full = (briefing_root / dest).resolve()
    if briefing_root.resolve() not in full.parents and full != briefing_root.resolve():
        _die(f"dest escapes the briefing tree: {dest}")
    return full


def do_copy(competition: str, comp_root: Path, briefing_root: Path) -> None:
    pairs = _load_manifest(comp_root, competition)
    source_commit = _git(comp_root, "rev-parse", "HEAD")
    dirty = bool(_git(comp_root, "status", "--porcelain"))

    records: list[tuple[str, str]] = []  # (dest, blob)
    staged: list[str] = []
    for src, dest in pairs:
        src_abs = comp_root / src
        if not src_abs.is_file():
            _die(f"source file missing: {src}")
        dest_abs = _resolve_dest(briefing_root, dest)
        dest_abs.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_abs, dest_abs)
        records.append((dest, _blob(comp_root, src)))
        staged.append(dest)

    prov_dir = briefing_root / ".instantiated"
    prov_dir.mkdir(parents=True, exist_ok=True)
    prov = prov_dir / f"{competition}.toml"
    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        "# Written by tools/instantiate.py - do not edit by hand.",
        "# Provenance for files copied into this repo from the source of truth.",
        'source_repo   = "ai-gladiators/ai-gladiators-competitions"',
        f'source_commit = "{source_commit}"',
        f"source_clean  = {str(not dirty).lower()}",
        f'instantiated  = "{now}"',
        "",
    ]
    for dest, blob in records:
        lines += ["[[file]]", f'path = "{dest}"', f'blob = "{blob}"', ""]
    prov.write_text("\n".join(lines))

    _git(briefing_root, "add", "--", *staged, str(prov.relative_to(briefing_root)))

    print(f"--> instantiated '{competition}' from competitions@{source_commit[:7]}"
          f"{' (WORKING TREE DIRTY)' if dirty else ''}")
    for dest in staged:
        print(f"    {dest}")
    print(f"--> {len(staged)} file(s) copied and staged in {briefing_root}")
    print(f"--> provenance: {prov.relative_to(briefing_root)}")
    print("--> NOT committed. Land via the briefing Way-A branch + review.")


def do_check(competition: str, briefing_root: Path) -> None:
    prov = briefing_root / ".instantiated" / f"{competition}.toml"
    if not prov.is_file():
        _die(f"no provenance for '{competition}' in briefing: {prov}")
    data = tomllib.loads(prov.read_text())
    rc = 0
    for e in data.get("file", []):
        dest, want = e["path"], e["blob"]
        if not (briefing_root / dest).is_file():
            print(f"MISSING: {dest}", file=sys.stderr)
            rc = 1
            continue
        have = _blob(briefing_root, dest)
        if have == want:
            print(f"OK:    {dest}")
        else:
            print(f"DRIFT: {dest} is {have}, provenance is {want}", file=sys.stderr)
            rc = 1
    print(f"--> {competition}: provenance says competitions@{data['source_commit'][:7]}")
    raise SystemExit(rc)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("competition")
    ap.add_argument("--briefing", default="../ai-gladiators-briefing",
                    help="path to the ai-gladiators-briefing checkout")
    ap.add_argument("--check", action="store_true",
                    help="verify the briefing copy against recorded provenance")
    args = ap.parse_args()

    comp_root = _toplevel(Path(__file__).resolve().parent)
    briefing_in = Path(args.briefing)
    if not briefing_in.is_dir():
        _die(f"briefing checkout not found: {briefing_in}")
    briefing_root = _toplevel(briefing_in)

    if args.check:
        do_check(args.competition, briefing_root)
    else:
        do_copy(args.competition, comp_root, briefing_root)


if __name__ == "__main__":
    main()
