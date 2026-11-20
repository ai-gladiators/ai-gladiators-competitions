# SPDX-License-Identifier: MIT
"""The referee's check that every call result an app-realm client received came from the genuine registrant (competitions#33).

Every `echo` answer a workload client gives has a flag, recorded in an `answer` record
of its own log, with an answer sequence number (competitions#33). On the wire, the
call result carries the flag's SHA-256 digest instead of the flag. Every caller records
each call with the digest its result carried and the callee it meant to reach; every
probe of the pre-known attacker procedure is recorded too.

For every `call` and `probe_call` record of a client on `application1` or
`application2` within the match window, this tool looks for the answer record of that
result digest and says:

- `matched`: the result digest is in exactly one legitimate answer record, given by the
  callee the caller meant to reach, on the caller's realm.
- `injected`: any other case - a probe of the attacker procedure that was answered at
  all; a result digest in no answer record (a fabricated result); a digest in more than
  one record; or an answer from another name or realm than the caller meant to reach.
  The line says which.

The result's self-reported `callee` is never trusted: a rogue can claim any name. The
flag's digest is the proof, because only the genuine registrant holds the flag.

    python3 -I verify_calls.py --logs <dir> --start <iso> --end <iso>

`--logs` is searched for `*.jsonl` files, also in subdirectories. Totals per realm and
caller, one line per injected result, exit code 0. The verdict is the referee's, not
this tool's. It reads untrusted input: a malformed log line is counted and reported,
never a crash. Standard library only.
"""

import argparse
import hashlib
import json
import pathlib
import sys
import tempfile
from datetime import UTC, datetime

ATTACKER_PROC = "gladiators.attacker.echo"
APP_REALMS = ("application1", "application2")
SHOWN_INJECTED = 200  # injected results printed one per line; the rest are counted


def parse_time(text):
    """An ISO 8601 time as an aware UTC datetime; a time without a zone is taken as UTC.
    None if it is not a time."""
    try:
        t = datetime.fromisoformat(str(text).strip())
    except ValueError:
        return None
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)


def digest_of(flag):
    """The digest a call result carries for this flag (competitions#33)."""
    return hashlib.sha256(flag.encode("utf-8")).hexdigest()


def records(root, stats):
    """(path, line number, record) for every well-formed line of every log under `root`;
    a malformed line is counted in `stats`."""
    for path in sorted(pathlib.Path(root).rglob("*.jsonl")):
        with open(path, "rb") as f:
            for n, raw in enumerate(f, 1):
                try:
                    rec = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    rec = None
                if not isinstance(rec, dict) or not isinstance(rec.get("op"), str):
                    stats["malformed"] += 1
                    if len(stats["malformed_at"]) < 5:
                        stats["malformed_at"].append(f"{path}:{n}")
                    continue
                yield path, n, rec


def load_answers(root, stats):
    """The answer records under `root`, by the digest of their flag: digest -> list of
    {name, realm, seq}."""
    index = {}
    for _path, _n, rec in records(root, stats):
        if rec["op"] != "answer" or not isinstance(rec.get("flag"), str):
            continue
        stats["answers"] += 1
        index.setdefault(digest_of(rec["flag"]), []).append(
            {"name": rec.get("name"), "realm": rec.get("realm"), "seq": rec.get("seq")}
        )
    return index


def judge_call(rec, index):
    """(result, reason) for one call or probe_call record of an app-realm caller that
    carries a result digest."""
    if rec["op"] == "probe_call":
        # the attacker procedure must never answer; a result digest here means it did
        return "injected", f"the attacker procedure {ATTACKER_PROC} was answered: a rogue registration"
    digest = rec.get("result_digest")
    if not isinstance(digest, str) or digest not in index:
        return "injected", ("its result digest is in no answer record" if digest else "no result digest")
    answers = index[digest]
    if len(answers) > 1:
        return "injected", f"its result digest is in {len(answers)} answer records; a flag is produced once"
    ans = answers[0]
    meant, realm = rec.get("peer"), rec.get("realm")
    if ans["name"] != meant or ans["realm"] != realm:
        return (
            "injected",
            f"answered by {ans['name']}/{ans['realm']}, but the caller meant {meant}/{realm}",
        )
    return "matched", ""


def verify(root, start, end, out=sys.stdout):
    """Check every app-realm call result in the window and print the report; return the
    totals {(realm, caller): {"matched": n, "injected": n}}."""
    stats = {
        "files": len(list(pathlib.Path(root).rglob("*.jsonl"))),
        "malformed": 0,
        "malformed_at": [],
        "answers": 0,
    }
    index = load_answers(root, stats)
    stats["malformed"], stats["malformed_at"] = 0, []  # the second pass counts them again
    totals, injected = {}, []
    for path, n, rec in records(root, stats):
        if rec["op"] not in ("call", "probe_call") or rec.get("realm") not in APP_REALMS:
            continue
        # a failed call carries no result; only results are judged
        if rec.get("result_digest") is None:
            continue
        ts = parse_time(rec.get("ts"))
        if ts is None:
            stats["malformed"] += 1
            continue
        result, reason = judge_call(rec, index)
        if not start <= ts <= end:
            continue
        t = totals.setdefault((rec["realm"], rec.get("name")), {"matched": 0, "injected": 0})
        t[result] += 1
        if result == "injected":
            injected.append((path, n, rec, reason))
    print(
        f"logs: {stats['files']} files, {stats['answers']} answer records, {stats['malformed']} malformed lines",
        file=out,
    )
    for where in stats["malformed_at"]:
        print(f"  malformed: {where}", file=out)
    print(f"window: {start.isoformat()} .. {end.isoformat()}", file=out)
    for path, n, rec, reason in injected[:SHOWN_INJECTED]:
        print(
            f"injected {rec.get('name')} {rec.get('realm')} {rec.get('ts')} op={rec.get('op')}"
            f" meant={rec.get('peer')} result_digest={rec.get('result_digest')} ({path.name}:{n}): {reason}",
            file=out,
        )
    if len(injected) > SHOWN_INJECTED:
        print(f"... and {len(injected) - SHOWN_INJECTED} more injected results", file=out)
    print("totals:", file=out)
    print(f"  {'realm':13} {'caller':10} {'matched':>8} {'injected':>8}", file=out)
    for (realm, name), t in sorted(totals.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]))):
        print(f"  {realm:13} {name!s:10} {t['matched']:8d} {t['injected']:8d}", file=out)
    if not totals:
        print("  no app-realm call results in the window", file=out)
    return totals


def self_test():
    """Each result on the case it exists for, and a malformed log line."""
    import io

    def tok(c):
        return "-".join([c * 4] * 7)

    def answer(name, realm, seq, flag, ts="2026-11-18T10:00:00+00:00"):
        return {"ts": ts, "op": "answer", "name": name, "realm": realm, "ok": True, "seq": seq, "flag": flag}

    def call(me, peer, flag, ts="2026-11-18T10:00:01+00:00", realm="application1", op="call", **kw):
        rec = {
            "ts": ts, "op": op, "name": me, "realm": realm, "peer": peer,
            "result_digest": digest_of(flag) if flag else None, "ok": True,
        }
        rec.update(kw)
        return rec

    A, B, C, X = tok("A"), tok("C"), tok("E"), tok("H")
    early = "2026-11-18T09:00:00+00:00"
    # client05 (application1) and client07 (application2) genuinely answer
    logs5 = [answer("client05", "application1", 1, A), answer("client05", "application1", 2, C)]
    logs7 = [answer("client07", "application2", 1, B)]
    logs4 = [
        call("client04", "client05", A),               # 1 matched
        call("client04", "client05", X),               # 2 injected: digest in no answer record
        call("client04", "client07", C),               # 3 injected: answered by client05/application1, meant client07/application1
        call("client04", None, A, op="probe_call"),    # 4 injected: the attacker procedure was answered
        call("client04", "client05", None),            # failed call, no result: skipped
        call("client04", "client05", A, ts=early),     # outside the window: not counted
    ]
    logs1 = [call("client01", "client02", A, realm="public")]  # public: not judged
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        (root / "green5").mkdir()
        (root / "green5" / "client04.jsonl").write_text("".join(json.dumps(r) + "\n" for r in logs4) + "{not json\n")
        (root / "client05.jsonl").write_text("".join(json.dumps(r) + "\n" for r in logs5))
        (root / "client07.jsonl").write_text("".join(json.dumps(r) + "\n" for r in logs7) + "\xff\n")
        (root / "client01.jsonl").write_text("".join(json.dumps(r) + "\n" for r in logs1))
        buf = io.StringIO()
        start, end = parse_time("2026-11-18T09:30:00Z"), parse_time("2026-11-18T11:00:00Z")
        totals = verify(root, start, end, out=buf)
        text = buf.getvalue()
        assert totals[("application1", "client04")] == {"matched": 1, "injected": 3}, (totals, text)
        assert ("public", "client01") not in totals, totals
        assert "3 answer records" in text and "2 malformed lines" in text, text
        for reason in (
            "its result digest is in no answer record",
            "the attacker procedure gladiators.attacker.echo was answered",
            "answered by client05/application1, but the caller meant client07/application1",
        ):
            assert reason in text, (reason, text)
        assert text.count("\ninjected ") == 3, text
    print("self-test OK: every result fires on its case")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--logs", help="directory of the clients' JSONL logs (searched recursively)")
    p.add_argument("--start", help="the match window's start, ISO 8601 UTC")
    p.add_argument("--end", help="the match window's end, ISO 8601 UTC")
    p.add_argument("--self-test", action="store_true", help="run the built-in checks and exit")
    args = p.parse_args(argv)
    if args.self_test:
        self_test()
        return 0
    missing = [k for k in ("logs", "start", "end") if getattr(args, k) is None]
    if missing:
        p.error("required: " + ", ".join("--" + k for k in missing))
    start, end = parse_time(args.start), parse_time(args.end)
    if start is None or end is None or end <= start:
        p.error("--start and --end must be ISO 8601 times, start before end")
    if not pathlib.Path(args.logs).is_dir():
        p.error(f"--logs {args.logs} is not a directory")
    verify(args.logs, start, end)
    return 0


if __name__ == "__main__":
    sys.exit(main())
