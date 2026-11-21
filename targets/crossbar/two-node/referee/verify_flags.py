# SPDX-License-Identifier: MIT
"""The referee's check of presented digests and flags against the workload clients' logs (competitions#22, #24).

Every tick a workload client publishes has a flag, recorded with the tick's sequence
number in a `publish` record of its own log, and nowhere else (competitions#21); the
tick on the wire carries only the flag's SHA-256 digest (competitions#24). Symmetrically,
every `echo` **answer** a client gives has a flag, recorded in an `answer` record of its
own log; the call result carries only the digest (competitions#33). Both are flag
sources here, so a digest a player holds that came from an event OR from a call result
is looked up the same way. A player presents what it says it obtained, and this tool
looks each claim up in the logs.

A **digest** (64 hex characters) shows that an event reached the player:

- `counts`: the digest of a flag published on `application1` or `application2` within
  the match window. An event of an authenticated realm, held by a player who is not a
  member of it.
- `control`: published on `public` within the window. Anyone may subscribe there, so
  it is no capture; it shows the player's way of obtaining and presenting digests works.

A **flag** (the token itself) never travels in an event, so it shows that its
publisher's log, share or process was read - beyond WAMP. It is reported apart:

- `obtained`: a flag in a publish record, published within the window, on any realm.

Either kind is `invalid` when it is neither a digest nor a flag, is in no publish
record, was published outside the window, is claimed as obtained before it was
published, or was presented twice. The line says which.

    python3 -I verify_flags.py --logs <dir> --claims <file> --start <iso> --end <iso>

`--logs` is searched for `*.jsonl` files, also in subdirectories (one per client
share, as copied). A claim line is a flag, optionally followed by whitespace and the
UTC time the player says it obtained the flag. Empty lines and lines starting with
`#` are skipped. Case does not matter: a flag is read in upper case, a digest in lower case.

The verdict is the referee's, not this tool's. It reads untrusted input: a malformed
log line or claim is counted and reported, never a crash. Standard library only.
"""

import argparse
import hashlib
import json
import pathlib
import re
import sys
import tempfile
from datetime import UTC, datetime

# A flag is autobahn's token as the workload client makes it (competitions#21):
# generate_token(7, 4), 7 groups of 4 characters of autobahn.util.DEFAULT_TOKEN_CHARS,
# e.g. K7AT-TMJS-WT47-SEER-3ELJ-UUYM-WRXF. The alphabet is repeated here so this tool
# needs nothing but the standard library; the smoke test checks the two agree.
FLAG_CHARS = "345679ACEFGHJKLMNPQRSTUVWXY"
FLAG_RE = re.compile(rf"^[{FLAG_CHARS}]{{4}}(-[{FLAG_CHARS}]{{4}}){{6}}$")
# What a tick carries instead of its flag (competitions#24): the SHA-256 of the flag's
# UTF-8 bytes, as hex.
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
NOT_A_CLAIM = (
    "neither a digest nor a flag: a digest is 64 hex characters, a flag 7 groups of 4"
    " token characters, e.g. K7AT-TMJS-WT47-SEER-3ELJ-UUYM-WRXF"
)
APP_REALMS = ("application1", "application2")
CONTROL_REALMS = ("public",)


def parse_time(text):
    """An ISO 8601 time as an aware UTC datetime; a time without a zone is taken as UTC.
    None if it is not a time."""
    try:
        t = datetime.fromisoformat(str(text).strip())  # 3.11+ reads a trailing Z
    except ValueError:
        return None
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)


def digest_of(flag):
    """The digest a tick carries for this flag (competitions#24)."""
    return hashlib.sha256(flag.encode("utf-8")).hexdigest()


def load_logs(root):
    """Index every flag in a publish or answer record under `root`, by the flag and by its digest.

    Returns (index, stats): index maps flag -> list of {file, line, name, realm, seq,
    ts, ok, flag}, and the flag's digest -> the same list; stats counts files, lines,
    publish/answer records with a flag, and malformed lines (with the first few named)."""
    index = {}
    stats = {"files": 0, "lines": 0, "flags": 0, "malformed": 0, "malformed_at": []}
    for path in sorted(pathlib.Path(root).rglob("*.jsonl")):
        stats["files"] += 1
        with open(path, "rb") as f:
            for n, raw in enumerate(f, 1):
                stats["lines"] += 1
                try:
                    rec = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    rec = None
                if not isinstance(rec, dict):
                    stats["malformed"] += 1
                    if len(stats["malformed_at"]) < 5:
                        stats["malformed_at"].append(f"{path}:{n}")
                    continue
                if rec.get("op") not in ("publish", "answer") or "flag" not in rec:
                    continue
                flag = rec.get("flag")
                ts = parse_time(rec.get("ts")) if rec.get("ts") is not None else None
                if not (isinstance(flag, str) and FLAG_RE.match(flag)) or ts is None:
                    stats["malformed"] += 1
                    if len(stats["malformed_at"]) < 5:
                        stats["malformed_at"].append(f"{path}:{n}")
                    continue
                stats["flags"] += 1
                if flag not in index:
                    index[flag] = index[digest_of(flag)] = []
                index[flag].append(
                    {
                        "file": str(path),
                        "line": n,
                        "name": rec.get("name"),
                        "realm": rec.get("realm"),
                        "seq": rec.get("seq"),
                        "ts": ts,
                        "ok": rec.get("ok"),
                        "flag": flag,
                    }
                )
    return index, stats


def read_claims(path):
    """[(line number, claim text, claimed time or None, problem or None)]."""
    claims = []
    with open(path, "rb") as f:
        for n, raw in enumerate(f, 1):
            text = raw.decode("utf-8", errors="replace").strip()
            if not text or text.startswith("#"):
                continue
            parts = text.split()
            # a flag typed in lower case is the same flag, a digest in upper case the same digest
            flag = (
                parts[0].lower()
                if DIGEST_RE.match(parts[0].lower())
                else parts[0].upper()
            )
            when, problem = None, None
            if len(parts) > 2:
                problem = "more than a flag and a time on the line"
            elif len(parts) == 2:
                when = parse_time(parts[1])
                if when is None:
                    problem = (
                        f"the claimed time {parts[1][:40]!r} is not an ISO 8601 time"
                    )
            claims.append((n, flag, when, problem))
    return claims


def judge(claims, index, start, end):
    """[(line, claim, result, detail, record or None)] in claim order."""
    out = []
    seen = {}
    for n, flag, when, problem in claims:
        shown = flag[:64]
        if problem:
            out.append((n, shown, "invalid", problem, None))
            continue
        is_digest = bool(DIGEST_RE.match(flag))
        if not (is_digest or FLAG_RE.match(flag)):
            out.append(
                (
                    n,
                    shown,
                    "invalid",
                    NOT_A_CLAIM,
                    None,
                )
            )
            continue
        if flag in seen:
            out.append(
                (n, flag, "invalid", f"presented before, on line {seen[flag]}", None)
            )
            continue
        seen[flag] = n
        recs = index.get(flag)
        if not recs:
            out.append((n, flag, "invalid", "in no publish or answer record of the logs", None))
            continue
        if len(recs) > 1:
            where = ", ".join(f"{r['file']}:{r['line']}" for r in recs[:3])
            out.append(
                (
                    n,
                    flag,
                    "invalid",
                    f"in {len(recs)} records ({where}); a flag is produced once",
                    None,
                )
            )
            continue
        rec = recs[0]
        if not start <= rec["ts"] <= end:
            out.append((n, flag, "invalid", "published outside the match window", rec))
            continue
        if when is not None and when < rec["ts"]:
            out.append(
                (n, flag, "invalid", "claimed as obtained before it was published", rec)
            )
            continue
        if not is_digest:
            out.append((n, flag, "obtained", "", rec))
        elif rec["realm"] in APP_REALMS:
            out.append((n, flag, "counts", "", rec))
        elif rec["realm"] in CONTROL_REALMS:
            out.append((n, flag, "control", "", rec))
        else:
            out.append(
                (
                    n,
                    flag,
                    "invalid",
                    f"published on an unknown realm {rec['realm']!r}",
                    rec,
                )
            )
    return out


def report(results, stats, start, end, out=sys.stdout):
    print(
        f"logs: {stats['files']} files, {stats['lines']} lines, {stats['flags']} flags,"
        f" {stats['malformed']} malformed lines",
        file=out,
    )
    for where in stats["malformed_at"]:
        print(f"  malformed: {where}", file=out)
    print(f"window: {start.isoformat()} .. {end.isoformat()}", file=out)
    totals = {"digest": {}, "flag": {}}
    for n, claim, result, detail, rec in results:
        line = f"{n:5d} {claim} {result}"
        if rec is not None:
            line += f" {rec['name']} {rec['realm']} seq={rec['seq']} published={rec['ts'].isoformat()}"
            if rec["ok"] is False:
                line += " (publish failed at the client)"
        if detail:
            line += f": {detail}"
        print(line, file=out)
        kind = "flag" if FLAG_RE.match(claim) else "digest"
        key = (result, rec["realm"] if rec is not None and result != "invalid" else "-")
        totals[kind][key] = totals[kind].get(key, 0) + 1
    for kind, title in (
        ("digest", "digests (an event of that realm reached the player)"),
        ("flag", "flags (a publisher's log, share or process was read: beyond WAMP)"),
    ):
        print(f"totals, {title}:", file=out)
        for (result, realm), count in sorted(totals[kind].items()):
            print(f"  {result:8} {realm:13} {count}", file=out)
        if not totals[kind]:
            print("  none", file=out)


def self_test():
    """Each result on the case it exists for: counts, control, every invalid reason,
    a duplicate, and a malformed log line."""
    import io

    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        (root / "green5").mkdir()

        def tok(c):
            return "-".join([c * 4] * 7)

        f_app, f_pub, f_old, f_fail, f_twice = (
            tok("A"),
            tok("C"),
            tok("E"),
            tok("F"),
            tok("G"),
        )
        recs5 = [
            {
                "ts": "2026-11-17T10:00:00+00:00",
                "op": "start",
                "name": "client04",
                "realm": "application1",
            },
            {
                "ts": "2026-11-17T10:05:00+00:00",
                "op": "publish",
                "name": "client04",
                "realm": "application1",
                "ok": True,
                "seq": 1,
                "flag": f_app,
            },
            {
                "ts": "2026-11-17T09:00:00+00:00",
                "op": "publish",
                "name": "client04",
                "realm": "application1",
                "ok": True,
                "seq": 2,
                "flag": f_old,
            },
            {
                "ts": "2026-11-17T10:06:00+00:00",
                "op": "publish",
                "name": "client04",
                "realm": "application1",
                "ok": False,
                "seq": 3,
                "flag": f_fail,
            },
            {
                "ts": "2026-11-17T10:06:00+00:00",
                "op": "event",
                "name": "client04",
                "realm": "application1",
                "ok": True,
                "peer": "client05",
            },
            {
                "ts": "2026-11-17T10:07:00+00:00",
                "op": "publish",
                "name": "client04",
                "realm": "application1",
                "ok": True,
                "seq": 4,
                "flag": f_twice,
            },
        ]
        recs1 = [
            {
                "ts": "2026-11-17T10:05:30+00:00",
                "op": "publish",
                "name": "client01",
                "realm": "public",
                "ok": True,
                "seq": 1,
                "flag": f_pub,
            },
            {
                "ts": "2026-11-17T10:07:00+00:00",
                "op": "publish",
                "name": "client01",
                "realm": "public",
                "ok": True,
                "seq": 2,
                "flag": f_twice,
            },
        ]
        (root / "green5" / "client04.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in recs5) + "{not json\n"
        )
        (root / "client01.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in recs1) + "\xff\n"
        )
        D = digest_of
        claims = root / "claims.txt"
        claims.write_text(
            "\n".join(
                [
                    "# a comment",
                    D(f_app),  # 2 counts
                    D(f_pub) + " 2026-11-17T10:06:00Z",  # 3 control
                    D(tok("H")),  # 4 in no publish record
                    D(f_old),  # 5 outside the window
                    "nonsense",  # 6 neither
                    D(f_app),  # 7 presented before
                    D(
                        f_fail
                    ).upper(),  # 8 counts: a digest in upper case is the same digest
                    f_app[:-1],  # 9 neither
                    D(f_twice),  # 10 published twice
                    f_app,  # 11 obtained: the flag itself
                    f_pub.lower(),  # 12 obtained: a flag in lower case, any realm
                    tok("H"),  # 13 a flag in no publish record
                    D(f_app)[:-1],  # 14 neither: 63 hex characters
                    "",
                ]
            )
        )
        claims2 = root / "claims2.txt"
        claims2.write_text(
            D(f_app)
            + " 2026-11-17T10:04:00Z\n"
            + D(f_pub)
            + " yesterday\n"
            + f_old
            + "\n"
            + tok("0")
            + "\n"
        )
        index, stats = load_logs(root)
        assert stats["files"] == 2 and stats["malformed"] == 2, stats
        assert stats["flags"] == 6, stats
        start, end = (
            parse_time("2026-11-17T10:00:00Z"),
            parse_time("2026-11-17T11:00:00Z"),
        )
        res = judge(read_claims(claims), index, start, end)
        got = [(n, r, detail) for n, _, r, detail, _ in res]
        want = [
            (2, "counts", ""),
            (3, "control", ""),
            (4, "invalid", "in no publish or answer record of the logs"),
            (5, "invalid", "published outside the match window"),
            (6, "invalid", "neither a digest nor a flag"),
            (7, "invalid", "presented before, on line 2"),
            (8, "counts", ""),
            (9, "invalid", "neither a digest nor a flag"),
            (10, "invalid", "in 2 records ("),
            (11, "obtained", ""),
            (12, "obtained", ""),
            (13, "invalid", "in no publish or answer record of the logs"),
            (14, "invalid", "neither a digest nor a flag"),
        ]
        # each detail starts with the reason; a path may follow it
        assert len(got) == len(want) and all(
            gn == wn and gr == wr and gd.startswith(wd) and (wd or not gd)
            for (gn, gr, gd), (wn, wr, wd) in zip(got, want)
        ), got
        assert res[1][4]["name"] == "client01" and res[0][4]["seq"] == 1, res
        assert res[6][4]["ok"] is False and res[6][1] == D(f_fail), res[
            6
        ]  # a failed publish's digest still counts
        assert "a flag is produced once" in res[8][3], res[8]
        assert (
            res[9][4]["realm"] == "application1" and res[10][4]["realm"] == "public"
        ), res
        assert res[10][1] == f_pub, res[10]  # lower case is the same flag
        res2 = judge(read_claims(claims2), index, start, end)
        assert res2[0][2] == "invalid" and "before it was published" in res2[0][3], res2
        assert res2[1][2] == "invalid" and "not an ISO 8601 time" in res2[1][3], res2
        assert res2[2][2] == "invalid" and "outside the match window" in res2[2][3], (
            res2
        )  # a flag, too
        assert res2[3][2] == "invalid" and "neither" in res2[3][3], (
            res2
        )  # 0 is not in the alphabet
        buf = io.StringIO()
        report(res, stats, start, end, out=buf)
        text = buf.getvalue()
        digests, flags = text.split("totals, flags")
        assert (
            "counts   application1  2" in digests
            and "control  public        1" in digests
        ), text
        assert "obtained" not in digests.split("totals, digests")[1], text
        assert (
            "obtained application1  1" in flags and "obtained public        1" in flags
        ), text
        assert "invalid  -             1" in flags, text  # tok("H") as a flag
        assert "(publish failed at the client)" in text, text
    print("self-test OK: every result fires on its case")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument(
        "--logs", help="directory of the clients' JSONL logs (searched recursively)"
    )
    p.add_argument(
        "--claims",
        help="file of presented flags, one per line, optionally with a UTC time",
    )
    p.add_argument("--start", help="the match window's start, ISO 8601 UTC")
    p.add_argument("--end", help="the match window's end, ISO 8601 UTC")
    p.add_argument(
        "--self-test", action="store_true", help="run the built-in checks and exit"
    )
    args = p.parse_args(argv)
    if args.self_test:
        self_test()
        return 0
    missing = [
        k for k in ("logs", "claims", "start", "end") if getattr(args, k) is None
    ]
    if missing:
        p.error("required: " + ", ".join("--" + k for k in missing))
    start, end = parse_time(args.start), parse_time(args.end)
    if start is None or end is None or end <= start:
        p.error("--start and --end must be ISO 8601 times, start before end")
    if not pathlib.Path(args.logs).is_dir():
        p.error(f"--logs {args.logs} is not a directory")
    index, stats = load_logs(args.logs)
    results = judge(read_claims(args.claims), index, start, end)
    report(results, stats, start, end)
    return 0


if __name__ == "__main__":
    sys.exit(main())
