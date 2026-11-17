# SPDX-License-Identifier: MIT
"""The referee's check of presented flags against the workload clients' logs (competitions#22).

Every tick a workload client publishes carries a flag, and the client records that
flag, with the tick's sequence number, in a `publish` record of its own log
(competitions#21). A player presents flags it says it obtained. This tool looks each
one up in the logs and says, per claim:

- `counts`: published on `application1` or `application2` within the match window. A
  flag of an authenticated realm, held by a player who is not a member of it.
- `control`: published on `public` within the window. Anyone may subscribe there, so
  it is no capture; it shows the player's way of obtaining and presenting flags works.
- `invalid`: not a flag, in no publish record, published outside the window, claimed
  before it was published, or presented twice. The line says which.

    python3 -I verify_flags.py --logs <dir> --claims <file> --start <iso> --end <iso>

`--logs` is searched for `*.jsonl` files, also in subdirectories (one per client
share, as copied). A claim line is a flag, optionally followed by whitespace and the
UTC time the player says it obtained the flag. Empty lines and lines starting with
`#` are skipped.

The verdict is the referee's, not this tool's. It reads untrusted input: a malformed
log line or claim is counted and reported, never a crash. Standard library only.
"""

import argparse
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


def load_logs(root):
    """Index every flag in a publish record under `root`.

    Returns (index, stats): index maps flag -> list of {file, line, name, realm, seq,
    ts, ok}; stats counts files, lines, publish records with a flag, and malformed
    lines (with the first few named)."""
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
                if rec.get("op") != "publish" or "flag" not in rec:
                    continue
                flag = rec.get("flag")
                ts = parse_time(rec.get("ts")) if rec.get("ts") is not None else None
                if not (isinstance(flag, str) and FLAG_RE.match(flag)) or ts is None:
                    stats["malformed"] += 1
                    if len(stats["malformed_at"]) < 5:
                        stats["malformed_at"].append(f"{path}:{n}")
                    continue
                stats["flags"] += 1
                index.setdefault(flag, []).append(
                    {
                        "file": str(path),
                        "line": n,
                        "name": rec.get("name"),
                        "realm": rec.get("realm"),
                        "seq": rec.get("seq"),
                        "ts": ts,
                        "ok": rec.get("ok"),
                    }
                )
    return index, stats


def read_claims(path):
    """[(line number, flag text, claimed time or None, problem or None)]."""
    claims = []
    with open(path, "rb") as f:
        for n, raw in enumerate(f, 1):
            text = raw.decode("utf-8", errors="replace").strip()
            if not text or text.startswith("#"):
                continue
            parts = text.split()
            flag = parts[0].upper()  # a flag typed in lower case is the same flag
            when, problem = None, None
            if len(parts) > 2:
                problem = "more than a flag and a time on the line"
            elif len(parts) == 2:
                when = parse_time(parts[1])
                if when is None:
                    problem = f"the claimed time {parts[1][:40]!r} is not an ISO 8601 time"
            claims.append((n, flag, when, problem))
    return claims


def judge(claims, index, start, end):
    """[(line, flag, result, detail, record or None)] in claim order."""
    out = []
    seen = {}
    for n, flag, when, problem in claims:
        shown = flag[:64]
        if problem:
            out.append((n, shown, "invalid", problem, None))
            continue
        if not FLAG_RE.match(flag):
            out.append((n, shown, "invalid", "not a flag: a flag is 7 groups of 4 token characters, e.g. K7AT-TMJS-WT47-SEER-3ELJ-UUYM-WRXF", None))
            continue
        if flag in seen:
            out.append((n, flag, "invalid", f"presented before, on line {seen[flag]}", None))
            continue
        seen[flag] = n
        recs = index.get(flag)
        if not recs:
            out.append((n, flag, "invalid", "in no publish record of the logs", None))
            continue
        if len(recs) > 1:
            where = ", ".join(f"{r['file']}:{r['line']}" for r in recs[:3])
            out.append(
                (n, flag, "invalid", f"in {len(recs)} publish records ({where}); a flag is published once", None)
            )
            continue
        rec = recs[0]
        if not start <= rec["ts"] <= end:
            out.append((n, flag, "invalid", "published outside the match window", rec))
            continue
        if when is not None and when < rec["ts"]:
            out.append((n, flag, "invalid", "claimed as obtained before it was published", rec))
            continue
        if rec["realm"] in APP_REALMS:
            out.append((n, flag, "counts", "", rec))
        elif rec["realm"] in CONTROL_REALMS:
            out.append((n, flag, "control", "", rec))
        else:
            out.append((n, flag, "invalid", f"published on an unknown realm {rec['realm']!r}", rec))
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
    totals = {}
    for n, flag, result, detail, rec in results:
        line = f"{n:5d} {flag} {result}"
        if rec is not None:
            line += f" {rec['name']} {rec['realm']} seq={rec['seq']} published={rec['ts'].isoformat()}"
            if rec["ok"] is False:
                line += " (publish failed at the client)"
        if detail:
            line += f": {detail}"
        print(line, file=out)
        key = (result, rec["realm"] if rec is not None and result != "invalid" else "-")
        totals[key] = totals.get(key, 0) + 1
    print("totals:", file=out)
    for (result, realm), count in sorted(totals.items()):
        print(f"  {result:8} {realm:13} {count}", file=out)


def self_test():
    """Each result on the case it exists for: counts, control, every invalid reason,
    a duplicate, and a malformed log line."""
    import io

    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        (root / "green5").mkdir()
        def tok(c):
            return "-".join([c * 4] * 7)

        f_app, f_pub, f_old, f_fail, f_twice = tok("A"), tok("C"), tok("E"), tok("F"), tok("G")
        recs5 = [
            {"ts": "2026-11-17T10:00:00+00:00", "op": "start", "name": "client04", "realm": "application1"},
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
        (root / "green5" / "client04.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs5) + "{not json\n")
        (root / "client01.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs1) + "\xff\n")
        claims = root / "claims.txt"
        claims.write_text(
            "\n".join(
                [
                    "# a comment",
                    f_app,
                    f_pub + " 2026-11-17T10:06:00Z",
                    tok("H"),
                    f_old,
                    "nonsense",
                    f_app,
                    f_fail,
                    f_app[:-1],
                    f_twice,
                    "",
                ]
            )
        )
        claims2 = root / "claims2.txt"
        claims2.write_text(
            f_app + " 2026-11-17T10:04:00Z\n" + f_pub + " yesterday\n" + f_fail.lower() + "\n" + tok("0") + "\n"
        )
        index, stats = load_logs(root)
        assert stats["files"] == 2 and stats["malformed"] == 2, stats
        assert stats["flags"] == 6, stats
        start, end = parse_time("2026-11-17T10:00:00Z"), parse_time("2026-11-17T11:00:00Z")
        res = judge(read_claims(claims), index, start, end)
        got = [(n, r, detail) for n, _, r, detail, _ in res]
        want = [
            (2, "counts", ""),
            (3, "control", ""),
            (4, "invalid", "in no publish record of the logs"),
            (5, "invalid", "published outside the match window"),
            (6, "invalid", "not a flag"),
            (7, "invalid", "presented before, on line 2"),
            (8, "counts", ""),
            (9, "invalid", "not a flag"),
            (10, "invalid", "in 2 publish records ("),
        ]
        # each detail starts with the reason; a path may follow it
        assert len(got) == len(want) and all(
            gn == wn and gr == wr and gd.startswith(wd) and (wd or not gd)
            for (gn, gr, gd), (wn, wr, wd) in zip(got, want)
        ), got
        assert "outside the match window" in res[3][3] and "presented before, on line 2" in res[5][3], res
        assert res[1][4]["name"] == "client01" and res[0][4]["seq"] == 1, res
        assert res[6][4]["ok"] is False  # a failed publish's flag still counts
        assert "a flag is published once" in res[8][3], res[8]
        res2 = judge(read_claims(claims2), index, start, end)
        assert res2[0][2] == "invalid" and "before it was published" in res2[0][3], res2
        assert res2[1][2] == "invalid" and "not an ISO 8601 time" in res2[1][3], res2
        assert res2[2][2] == "counts" and res2[2][1] == f_fail, res2  # lower case is the same flag
        assert res2[3][2] == "invalid" and "not a flag" in res2[3][3], res2  # 0 is not in the alphabet
        buf = io.StringIO()
        report(res, stats, start, end, out=buf)
        text = buf.getvalue()
        assert "counts   application1  2" in text and "control  public        1" in text, text
        assert "(publish failed at the client)" in text, text
    print("self-test OK: every result fires on its case")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--logs", help="directory of the clients' JSONL logs (searched recursively)")
    p.add_argument("--claims", help="file of presented flags, one per line, optionally with a UTC time")
    p.add_argument("--start", help="the match window's start, ISO 8601 UTC")
    p.add_argument("--end", help="the match window's end, ISO 8601 UTC")
    p.add_argument("--self-test", action="store_true", help="run the built-in checks and exit")
    args = p.parse_args(argv)
    if args.self_test:
        self_test()
        return 0
    missing = [k for k in ("logs", "claims", "start", "end") if getattr(args, k) is None]
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
