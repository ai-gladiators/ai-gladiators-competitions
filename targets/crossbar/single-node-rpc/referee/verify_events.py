# SPDX-License-Identifier: MIT
"""The referee's check that every event the app-realm clients received was published by a member (competitions#24).

Every tick a workload client publishes is in a `publish` record of its own log, with
its sequence number and its flag (competitions#21). On the wire, the tick carries the
flag's SHA-256 digest instead of the flag. Every client subscribes to every topic under
`gladiators.` and records each event it receives: the topic, the publisher, realm, seq
and digest the event states, and the publisher the router disclosed.

For every `event` record of a client on `application1` or `application2` within the
match window, this tool looks for the publish record of that digest and says:

- `matched`: the publish record exists, states the same publisher, realm and seq, was
  published on the subscriber's realm, and this subscriber has not recorded that
  digest before.
- `injected`: any other case - a topic other than `gladiators.tick`, no digest, a
  digest in no publish record, a publish record that states otherwise, one published
  on another realm, or a second copy at the same subscriber. The line says which.

For every event, it also notes whether the router's `publisher_seen` equals the
publisher the event states.

    python3 -I verify_events.py --logs <dir> --start <iso> --end <iso>

`--logs` is searched for `*.jsonl` files, also in subdirectories. Totals per realm and
subscriber, one line per injected event, exit code 0. The verdict is the referee's, not
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

TICK = "gladiators.tick"
APP_REALMS = ("application1", "application2")
SHOWN_INJECTED = 200  # injected events printed one per line; the rest are counted


def parse_time(text):
    """An ISO 8601 time as an aware UTC datetime; a time without a zone is taken as UTC.
    None if it is not a time."""
    try:
        t = datetime.fromisoformat(str(text).strip())
    except ValueError:
        return None
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)


def digest_of(flag):
    """The digest a tick carries for this flag (competitions#24)."""
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


def load_publishes(root, stats):
    """The publish records under `root`, by the digest of their flag: digest -> list of
    {name, realm, seq, ts, seen_by}."""
    index = {}
    for _path, _n, rec in records(root, stats):
        if rec["op"] != "publish" or not isinstance(rec.get("flag"), str):
            continue
        stats["publishes"] += 1
        index.setdefault(digest_of(rec["flag"]), []).append(
            {
                "name": rec.get("name"),
                "realm": rec.get("realm"),
                "seq": rec.get("seq"),
                "seen_by": set(),
            }
        )
    return index


def judge_event(rec, index):
    """(result, reason, publisher_seen differs) for one event record of an app-realm
    subscriber. Marks a matched publish record as seen by this subscriber."""
    me = rec.get("name")
    differs = rec.get("publisher_seen") != rec.get("peer")
    if rec.get("topic") != TICK:
        return "injected", f"on topic {rec.get('topic')!r}", differs
    digest = rec.get("digest")
    if not isinstance(digest, str) or digest not in index:
        return (
            "injected",
            "its digest is in no publish record" if digest else "no digest",
            differs,
        )
    pubs = index[digest]
    if len(pubs) > 1:
        return (
            "injected",
            f"its digest is in {len(pubs)} publish records; a flag is published once",
            differs,
        )
    pub = pubs[0]
    stated = (rec.get("peer"), rec.get("from_realm"), rec.get("seq"))
    published = (pub["name"], pub["realm"], pub["seq"])
    if stated != published:
        return (
            "injected",
            f"states {stated}, but the digest was published as {published}",
            differs,
        )
    if pub["realm"] != rec.get("realm"):
        return (
            "injected",
            f"published on {pub['realm']}, received on {rec.get('realm')}",
            differs,
        )
    if me in pub["seen_by"]:
        return (
            "injected",
            "a second copy: this subscriber received this digest before",
            differs,
        )
    pub["seen_by"].add(me)
    return "matched", "", differs


def verify(root, start, end, out=sys.stdout):
    """Check every app-realm event in the window and print the report; return the totals
    {(realm, subscriber): {"matched": n, "injected": n, "publisher_seen differs": n}}."""
    stats = {
        "files": len(list(pathlib.Path(root).rglob("*.jsonl"))),
        "malformed": 0,
        "malformed_at": [],
        "publishes": 0,
    }
    index = load_publishes(root, stats)
    stats["malformed"], stats["malformed_at"] = (
        0,
        [],
    )  # the second pass counts them again
    totals, injected = {}, []
    for path, n, rec in records(root, stats):
        if rec["op"] != "event" or rec.get("realm") not in APP_REALMS:
            continue
        ts = parse_time(rec.get("ts"))
        if ts is None:
            stats["malformed"] += 1
            continue
        result, reason, differs = judge_event(rec, index)
        # every event is judged, so a copy before the window makes one in it a second copy;
        # only the window's are counted
        if not start <= ts <= end:
            continue
        t = totals.setdefault(
            (rec["realm"], rec.get("name")),
            {"matched": 0, "injected": 0, "publisher_seen differs": 0},
        )
        t[result] += 1
        t["publisher_seen differs"] += differs
        if result == "injected":
            injected.append((path, n, rec, reason))
    print(
        f"logs: {stats['files']} files, {stats['publishes']} publish records, {stats['malformed']} malformed lines",
        file=out,
    )
    for where in stats["malformed_at"]:
        print(f"  malformed: {where}", file=out)
    print(f"window: {start.isoformat()} .. {end.isoformat()}", file=out)
    for path, n, rec, reason in injected[:SHOWN_INJECTED]:
        print(
            f"injected {rec.get('name')} {rec.get('realm')} {rec.get('ts')} topic={rec.get('topic')}"
            f" states {rec.get('peer')}/{rec.get('from_realm')}/seq={rec.get('seq')}"
            f" publisher_seen={rec.get('publisher_seen')} ({path.name}:{n}): {reason}",
            file=out,
        )
    if len(injected) > SHOWN_INJECTED:
        print(
            f"... and {len(injected) - SHOWN_INJECTED} more injected events", file=out
        )
    print("totals:", file=out)
    print(
        f"  {'realm':13} {'subscriber':10} {'matched':>8} {'injected':>8} {'publisher_seen differs':>22}",
        file=out,
    )
    for (realm, name), t in sorted(
        totals.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]))
    ):
        print(
            f"  {realm:13} {name!s:10} {t['matched']:8d} {t['injected']:8d} {t['publisher_seen differs']:22d}",
            file=out,
        )
    if not totals:
        print("  no app-realm events in the window", file=out)
    return totals


def self_test():
    """Each result on the case it exists for, and a malformed log line."""
    import io

    def tok(c):
        return "-".join([c * 4] * 7)

    def pub(name, realm, seq, flag, ts="2026-11-18T10:00:00+00:00"):
        return {
            "ts": ts,
            "op": "publish",
            "name": name,
            "realm": realm,
            "ok": True,
            "seq": seq,
            "flag": flag,
        }

    def ev(
        me, peer, seq, flag, ts="2026-11-18T10:00:01+00:00", realm="application1", **kw
    ):
        rec = {
            "ts": ts,
            "op": "event",
            "name": me,
            "realm": realm,
            "topic": TICK,
            "peer": peer,
            "from_realm": realm,
            "seq": seq,
            "digest": digest_of(flag) if flag else None,
            "publisher_seen": peer,
            "ok": True,
            "violation": None,
        }
        rec.update(kw)
        return rec

    A, B, C, X = tok("A"), tok("C"), tok("E"), tok("H")
    early = "2026-11-18T09:00:00+00:00"
    logs4 = [
        pub("client04", "application1", 1, A),
        ev("client04", "client04", 1, A),  # 1 matched: its own tick back
        ev("client04", "client04", 1, A),  # 2 injected: a second copy
        ev("client04", "client05", 9, X),  # 3 injected: no publish record
        ev("client04", "client05", 2, A),  # 4 injected: states another seq
        ev(
            "client04", "client07", 1, B, from_realm="application2"
        ),  # 5 injected: another realm's tick
        ev(
            "client04", None, None, None, topic="gladiators.other"
        ),  # 6 injected: another topic
        ev(
            "client04", "client05", 1, C, publisher_seen="client04"
        ),  # 7 matched, but publisher_seen differs
        ev("client04", None, None, None),  # 8 injected: no digest
        ev("client04", "client05", 1, C, ts=early),  # outside the window: not counted
    ]
    logs5 = [
        pub("client05", "application1", 1, C),
        ev("client05", "client05", 1, C, ts=early),
    ]
    logs7 = [
        pub("client07", "application2", 1, B),
        ev("client07", "client07", 1, B, realm="application2"),
    ]
    logs1 = [ev("client01", "client02", 1, X, realm="public")]  # public: not judged
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        (root / "green5").mkdir()
        (root / "green5" / "client04.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in logs4) + "{not json\n"
        )
        (root / "client05.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in logs5)
        )
        (root / "client07.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in logs7) + "\xff\n"
        )
        (root / "client01.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in logs1)
        )
        buf = io.StringIO()
        start, end = (
            parse_time("2026-11-18T09:30:00Z"),
            parse_time("2026-11-18T11:00:00Z"),
        )
        totals = verify(root, start, end, out=buf)
        text = buf.getvalue()
        assert totals[("application1", "client04")] == {
            "matched": 2,
            "injected": 6,
            "publisher_seen differs": 1,
        }, (
            totals,
            text,
        )
        assert totals[("application2", "client07")] == {
            "matched": 1,
            "injected": 0,
            "publisher_seen differs": 0,
        }
        assert ("public", "client01") not in totals and (
            "application1",
            "client05",
        ) not in totals, totals
        assert "2 malformed lines" in text and "3 publish records" in text, text
        for reason in (
            "a second copy",
            "its digest is in no publish record",
            "but the digest was published as ('client04', 'application1', 1)",
            "published on application2, received on application1",
            "on topic 'gladiators.other'",
            ": no digest",
        ):
            assert reason in text, (reason, text)
        assert text.count("\ninjected ") == 6, text
        # a copy before the window makes the one in the window a second copy
        logs6 = [
            pub("client06", "application1", 1, tok("F")),
            ev("client06", "client06", 1, tok("F"), ts=early),
            ev("client06", "client06", 1, tok("F")),
        ]
        (root / "client06.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in logs6)
        )
        totals = verify(root, start, end, out=io.StringIO())
        assert totals[("application1", "client06")]["injected"] == 1, totals
    print("self-test OK: every result fires on its case")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument(
        "--logs", help="directory of the clients' JSONL logs (searched recursively)"
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
