#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Smoke test of the Crossbar.io single-node PubSub + RPC target (crossbar-v1), on one machine:
#   1. copy the node directory, generate a throwaway TLS pair and six client keys,
#      and write the public keys into the copy's config.json;
#   2. start Crossbar.io (epoll reactor) and the nine clients for DURATION seconds,
#      and print what both ends run on;
#   3. require the router to refuse an unknown key and an anonymous client in an
#      authenticated realm;
#   4. PubSub injection (smoke_inject.py): events on application1 that no member
#      published (a forged tick on gladiators.tick, an event on the pre-known
#      attacker topic) - every member must record them as violations;
#   5. RPC rogue (smoke_rogue.py): register the pre-known attacker procedure on
#      application1 and answer with a fabricated flag - the application1 members'
#      probes must then succeed unexpectedly, and application2's must still fail;
#   6. require every client log to show its own-realm ticks and calls, each call
#      result's digest matched to the callee's answer record, no errors, and every
#      flag in its own publish or answer record only;
#   7. run the referee's tools: verify_flags.py (a PubSub digest and an RPC digest
#      both count; a public digest is a control; a flag is obtained), verify_events.py
#      (the injected events at the application1 members), and verify_calls.py (the
#      rogue-answered calls injected at application1, none at application2).
#
# Environment: PYTHON (the client's locked venv: uv sync --frozen --project client), CROSSBAR
# (the crossbar executable), DURATION (default 120), PORT (default 8080),
# KEEP=1 to keep the temporary directory.
#
# It starts a server on PORT. To keep it off the host's network, run it in its
# own network namespace, e.g. `unshare -rn --pid --fork --mount-proc ./smoke.sh`
# (loopback is brought up below when it is down). It refuses to start when
# PORT is taken, or when PYTHON or CROSSBAR cannot run it.

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"
CROSSBAR="${CROSSBAR:-crossbar}"
DURATION="${DURATION:-120}"
export CROSSBAR_REACTOR=epoll
PORT="${PORT:-8080}"

work=""
node=""
cb_pid=""

cleanup() {
    local rc=$?
    if [ -n "${cb_pid}" ] && kill -0 "${cb_pid}" 2>/dev/null; then
        kill "${cb_pid}" 2>/dev/null || true
        wait "${cb_pid}" 2>/dev/null || true
    fi
    if [ -z "${work}" ]; then
        return
    fi
    if [ "${KEEP:-0}" = "1" ] || [ "${rc}" -ne 0 ]; then
        echo "kept: ${work}"
    else
        rm -rf "${work}"
    fi
}
trap cleanup EXIT

# loopback inside a fresh network namespace starts down
if command -v ip >/dev/null 2>&1 && ! ip -o link show lo | grep -q "UP"; then
    ip link set lo up
fi

# Fail at once, in plain words, when the environment cannot run the test.
if ! command -v "${PYTHON}" >/dev/null 2>&1; then
    echo "FAIL: PYTHON=${PYTHON} is not an executable; set PYTHON to the python of the client's venv, made with: uv sync --frozen --project targets/crossbar/single-node-rpc/client (see README)" >&2
    exit 1
fi
missing="$("${PYTHON}" -c '
import importlib.util
print(" ".join(m for m in ("autobahn", "cbor2", "nacl") if importlib.util.find_spec(m) is None))
' 2>/dev/null)" || missing="(python failed to run)"
if [ -n "${missing}" ]; then
    echo "FAIL: PYTHON=${PYTHON} cannot import: ${missing}; set PYTHON to the python of the client's venv, made with: uv sync --frozen --project targets/crossbar/single-node-rpc/client (see README)" >&2
    exit 1
fi
if ! command -v "${CROSSBAR}" >/dev/null 2>&1; then
    echo "FAIL: CROSSBAR=${CROSSBAR} is not an executable; set CROSSBAR to the crossbar of a venv with Crossbar.io installed (see README)" >&2
    exit 1
fi
# The router listens on all IPv4 interfaces at PORT. If that port is taken, it
# cannot start, and the readiness probe below would talk to the other listener.
if ! "${PYTHON}" -I -c '
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(("0.0.0.0", int(sys.argv[1])))
    s.listen(1)
except OSError:
    sys.exit(1)
finally:
    s.close()
' "${PORT}"; then
    echo "FAIL: port ${PORT} is already taken on this machine; set PORT to a free port, or run in a network namespace (see README)" >&2
    exit 1
fi

# the temporary directory exists only once the checks above have passed
work="$(mktemp -d "${TMPDIR:-/tmp}/gladiators-smoke.XXXXXX")"
node="${work}/node"

echo "==> client self-test"
"${PYTHON}" "${here}/client/workload.py" --self-test

echo "==> node directory copy: ${node}"
mkdir -p "${node}"
cp -r "${here}/.crossbar" "${here}/web" "${node}/"
rm -f "${node}/.crossbar/server.key" "${node}/.crossbar/server.crt"

echo "==> throwaway TLS pair for localhost"
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
    -keyout "${node}/.crossbar/server.key" -out "${node}/.crossbar/server.crt" \
    -days 2 -subj "/CN=localhost" -addext "subjectAltName=DNS:localhost" 2>/dev/null
chmod 600 "${node}/.crossbar/server.key"

echo "==> throwaway client keys"
mkdir -p "${work}/keys" "${work}/logs"
for i in 04 05 06 07 08 09; do
    "${PYTHON}" "${here}/client/workload.py" --generate-key "${work}/keys/client${i}.key" \
        > "${work}/keys/client${i}.pub"
done
"${PYTHON}" "${here}/client/workload.py" --generate-key "${work}/keys/stranger.key" > /dev/null

# write the public keys into both auth blocks (RawSocket and WebSocket) of the copy
"${PYTHON}" -I - "${node}/.crossbar/config.json" "${work}/keys" "${PORT}" <<'EOF'
import json, sys, pathlib
cfg_path, keys, port = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), int(sys.argv[3])
cfg = json.loads(cfg_path.read_text())
tr = cfg["workers"][0]["transports"][0]
tr["endpoint"]["port"] = port
for auth in (tr["rawsocket"]["auth"], tr["websocket"]["ws"]["auth"]):
    for authid, p in auth["cryptosign"]["principals"].items():
        p["authorized_keys"] = [(keys / f"{authid}.pub").read_text().strip()]
cfg_path.write_text(json.dumps(cfg, indent=4) + "\n")
EOF

echo "==> start Crossbar.io"
# unbuffered, so the log file is current while the router runs (it is checked below)
PYTHONUNBUFFERED=1 "${CROSSBAR}" start --cbdir "${node}/.crossbar" > "${work}/crossbar.log" 2>&1 &
cb_pid=$!
for _ in $(seq 1 60); do
    # the router listens on IPv4 only: never let "localhost" reach a listener on ::1
    if curl -s --max-time 5 --resolve "localhost:${PORT}:127.0.0.1" --cacert "${node}/.crossbar/server.crt" "https://localhost:${PORT}/" > "${work}/index.html" 2>/dev/null; then
        break
    fi
    if ! kill -0 "${cb_pid}" 2>/dev/null; then
        echo "FAIL: Crossbar.io exited during startup" >&2
        tail -n 40 "${work}/crossbar.log" >&2
        exit 1
    fi
    sleep 1
done
if ! grep -q "AI Gladiators" "${work}/index.html"; then
    echo "FAIL: the web page is not served over TLS on port ${PORT}" >&2
    tail -n 40 "${work}/crossbar.log" >&2
    exit 1
fi
echo "    web page served over TLS"
# the router worker's log line can trail its first answer: give it a moment
for _ in $(seq 1 20); do
    grep -q "Running as PID [0-9]* on [A-Za-z]*-EPollReactor" "${work}/crossbar.log" && break
    sleep 0.5
done
if ! grep -q "Running as PID [0-9]* on [A-Za-z]*-EPollReactor" "${work}/crossbar.log"; then
    echo "FAIL: the router is not running on the epoll reactor" >&2
    exit 1
fi

echo "==> router stack"
grep -o "Running as PID [0-9]* on [A-Za-z]*-[A-Za-z]*Reactor" "${work}/crossbar.log" | sort -u | sed 's/^/    /'
# the interpreter Crossbar.io runs on is the one in its launcher's shebang
cb_python="$(head -n 1 "$(command -v "${CROSSBAR}")" | sed 's/^#!//')"
"${cb_python}" -I - <<'PYEOF'
import platform, ssl
from importlib.metadata import version, PackageNotFoundError
def ver(d):
    try:
        return version(d)
    except PackageNotFoundError:
        return None
from cryptography.hazmat.backends.openssl import backend
print(f"    python        {platform.python_implementation()} {platform.python_version()}")
print(f"    TLS           Twisted via pyOpenSSL {ver('pyopenssl')} and cryptography {ver('cryptography')}: {backend.openssl_version_text()}")
print(f"    ssl module    {ssl.OPENSSL_VERSION} (not used for the router's TLS)")
print(f"    crossbar      {ver('crossbar')}, autobahn {ver('autobahn')}, twisted {ver('twisted')}, txaio {ver('txaio')}, cbor2 {ver('cbor2')}, pynacl {ver('pynacl')}")
print(f"    os            {platform.platform()}")
PYEOF

realm_of() {
    case "$1" in
        client01|client02|client03) echo public ;;
        client04|client05|client06) echo application1 ;;
        *) echo application2 ;;
    esac
}
peers_of() {
    case "$1" in
        client01) echo client02,client03 ;; client02) echo client01,client03 ;; client03) echo client01,client02 ;;
        client04) echo client05,client06 ;; client05) echo client04,client06 ;; client06) echo client04,client05 ;;
        client07) echo client08,client09 ;; client08) echo client07,client09 ;; client09) echo client07,client08 ;;
    esac
}

echo "==> nine clients for ${DURATION} s (+ stop grace)"
# the match window the verifier is given starts here: no flag is older
smoke_start="$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"
pids=()
n=0
for c in client01 client02 client03 client04 client05 client06 client07 client08 client09; do
    # alternate the transports: every third client uses RawSocket
    if [ $((n % 3)) -eq 0 ]; then url="rss://localhost:${PORT}"; else url="wss://localhost:${PORT}/ws"; fi
    n=$((n + 1))
    keyarg=()
    if [ -f "${work}/keys/${c}.key" ]; then keyarg=(--key "${work}/keys/${c}.key"); fi
    "${PYTHON}" "${here}/client/workload.py" --name "${c}" --realm "$(realm_of "${c}")" \
        --url "${url}" --trust "${node}/.crossbar/server.crt" "${keyarg[@]}" \
        --peers "$(peers_of "${c}")" --seed smoke --log "${work}/logs/${c}.jsonl" \
        --duration "${DURATION}" --peer-wait 60 > "${work}/logs/${c}.out" 2>&1 &
    pids+=($!)
done

echo "==> refusals: an unknown key, and an anonymous client in an authenticated realm"
"${PYTHON}" "${here}/client/workload.py" --name client04 --realm application1 \
    --url "wss://localhost:${PORT}/ws" --trust "${node}/.crossbar/server.crt" \
    --key "${work}/keys/stranger.key" --seed smoke --log "${work}/logs/stranger.jsonl" \
    --duration 5 > "${work}/logs/stranger.out" 2>&1 || true
"${PYTHON}" "${here}/client/workload.py" --name intruder --realm application1 \
    --url "wss://localhost:${PORT}/ws" --trust "${node}/.crossbar/server.crt" \
    --seed smoke --log "${work}/logs/intruder.jsonl" \
    --duration 5 > "${work}/logs/intruder.out" 2>&1 || true

echo "==> PubSub injection on application1: a forged tick, a second copy, an attacker-topic event"
"${PYTHON}" "${here}/smoke_inject.py" --name client04 --realm application1 \
    --key "${work}/keys/client04.key" --forge-as client05 \
    --url "wss://localhost:${PORT}/ws" --trust "${node}/.crossbar/server.crt" \
    --out "${work}/inject.json" > "${work}/inject.out" 2>&1 || true

echo "==> RPC rogue on application1: register gladiators.attacker.echo, answer with a fake flag"
if [ "${DURATION%.*}" -gt 40 ]; then rogue_hold=30; else rogue_hold=$(( ${DURATION%.*} / 2 )); fi
"${PYTHON}" "${here}/smoke_rogue.py" --name client04 --realm application1 \
    --key "${work}/keys/client04.key" --claim-as client05 --hold "${rogue_hold}" \
    --url "wss://localhost:${PORT}/ws" --trust "${node}/.crossbar/server.crt" \
    --out "${work}/rogue.json" > "${work}/rogue.out" 2>&1 &
rogue_pid=$!

rc=0
for p in "${pids[@]}"; do
    wait "${p}" || rc=1
done
wait "${rogue_pid}" 2>/dev/null || true
if [ "${rc}" -ne 0 ]; then
    echo "FAIL: a client exited non-zero" >&2
fi

echo "==> check the logs"
"${PYTHON}" -I - "${work}/logs" "${here}/referee/verify_flags.py" "${work}/inject.json" "${work}/rogue.json" <<'EOF' || rc=1
import collections, hashlib, json, re, sys, pathlib
logs = pathlib.Path(sys.argv[1])
fail = []
inject = json.loads(pathlib.Path(sys.argv[3]).read_text()) if pathlib.Path(sys.argv[3]).exists() else {"error": "no report"}
rogue = json.loads(pathlib.Path(sys.argv[4]).read_text()) if pathlib.Path(sys.argv[4]).exists() else {"error": "no report"}
if "error" in inject:
    fail.append(f"the injector failed: {inject['error']}")
if "error" in rogue:
    fail.append(f"the rogue failed: {rogue['error']}")
elif not rogue.get("answered"):
    fail.append("the rogue registered but answered no probe: the probe window did not overlap it")

from autobahn.util import DEFAULT_TOKEN_CHARS
m = re.search(r'^FLAG_CHARS = "([^"]*)"$', (pathlib.Path(sys.argv[2]).read_text()), re.M)
if not m or m.group(1) != DEFAULT_TOKEN_CHARS:
    fail.append(f"the verifier's FLAG_CHARS is not autobahn's DEFAULT_TOKEN_CHARS")
g = f"[{DEFAULT_TOKEN_CHARS}]{{4}}"
flagform = re.compile(rf"^{g}(-{g}){{6}}$")

def records(name):
    p = logs / f"{name}.jsonl"
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []

REALM = {**{f"client0{i}": "public" for i in (1,2,3)},
         **{f"client0{i}": "application1" for i in (4,5,6)},
         **{f"client0{i}": "application2" for i in (7,8,9)}}

producer_flags = {}    # flag -> (name, realm, op): every publish/answer flag, from its own record
others_text = []       # every record that is not a publish/answer, as text, for the leak scan
app1_rogue_hits = 0
app2_rogue_hits = 0
app1_pub_digest = app1_ans_digest = pub_control_digest = app_flag_value = None

start = next((r for r in records("client02") if r["op"] == "start"), None)
if start:
    print("    client stack (client02):")
    for k, v in start["stack"].items():
        print(f"      {k:22} {v}")

for i in range(1, 10):
    name = f"client{i:02d}"
    realm = REALM[name]
    recs = records(name)
    joins = [r for r in recs if r["op"] == "join"]
    ws = [r for r in recs if r["op"] == "workload_start"]
    calls = [r for r in recs if r["op"] == "call"]
    probes = [r for r in recs if r["op"] == "probe_call"]
    events = [r for r in recs if r["op"] == "event"]
    pubs = [r for r in recs if r["op"] == "publish"]
    answers = [r for r in recs if r["op"] == "answer"]
    bad = [r for r in recs if r.get("error") and r["op"] not in ("probe_call",)]
    call_integrity = [r for r in calls if r.get("violation") in ("payload changed", "answered by another callee", "router reported another caller authid")]
    rogue_hits = [r for r in probes if r.get("violation") and "rogue registration" in r["violation"]]
    nspc = [r for r in probes if r.get("outcome") == "wamp.error.no_such_procedure"]
    lat = sorted(r["latency_ms"] for r in calls if r.get("ok") and isinstance(r.get("latency_ms"), (int, float)))
    p99 = lat[int(0.99 * (len(lat) - 1))] if lat else None
    print(f"    {name} {realm:13} joins={len(joins)} calls={len(calls)} answers={len(answers)} "
          f"events={len(events)} pubs={len(pubs)} probes={len(probes)}(nspc={len(nspc)},rogue={len(rogue_hits)}) p99_ms={p99}")
    if len(joins) != 1: fail.append(f"{name}: joined {len(joins)} times")
    if len(ws) != 1 or ws and ws[0].get("missing"): fail.append(f"{name}: workload_start {ws}")
    if not calls: fail.append(f"{name}: no calls")
    if not events: fail.append(f"{name}: no ticks received")
    if not pubs: fail.append(f"{name}: no ticks published")
    if not answers: fail.append(f"{name}: no echo answers given")
    if not probes: fail.append(f"{name}: never probed the attacker procedure")
    if bad: fail.append(f"{name}: {len(bad)} error records, e.g. {bad[0]}")
    if call_integrity: fail.append(f"{name}: {len(call_integrity)} broken call results, e.g. {call_integrity[0]}")
    for r in pubs + answers:
        f = r.get("flag")
        if not (isinstance(f, str) and flagform.match(f)):
            fail.append(f"{name}: a {r['op']} record without a valid flag: {r}")
        elif f in producer_flags:
            fail.append(f"{name}: flag {f} produced twice, also by {producer_flags[f]}")
        else:
            producer_flags[f] = (name, realm, r["op"])
    others_text.append(json.dumps([r for r in recs if r["op"] not in ("publish", "answer")]))
    if realm == "application1": app1_rogue_hits += len(rogue_hits)
    if realm == "application2": app2_rogue_hits += len(rogue_hits)
    # pick claim sources
    if realm == "application1" and pubs and app1_pub_digest is None:
        app1_pub_digest = hashlib.sha256(pubs[0]["flag"].encode()).hexdigest()
        app_flag_value = pubs[0]["flag"]
    if realm == "application1" and answers and app1_ans_digest is None:
        app1_ans_digest = hashlib.sha256(answers[0]["flag"].encode()).hexdigest()
    if realm == "public" and pubs and pub_control_digest is None:
        pub_control_digest = hashlib.sha256(pubs[0]["flag"].encode()).hexdigest()

# a producer's flag must never appear in any other kind of record, under any key
text = "".join(others_text)
leaked = [f for f in producer_flags if f in text]
if leaked:
    fail.append(f"{len(leaked)} flags appear outside their publish/answer record, e.g. {leaked[0]}")

# RPC rogue detected on application1, and realm isolation held for application2
if app1_rogue_hits < 3:
    fail.append(f"application1 members recorded only {app1_rogue_hits} rogue-answered probes (expected the rogue to answer their probes)")
if app2_rogue_hits != 0:
    fail.append(f"application2 members recorded {app2_rogue_hits} rogue answers: the rogue on application1 reached application2 (realm isolation broke)")

# refusals, as in crossbar-v0
for name, reason in (("stranger", "wamp.error.not_authorized"), ("intruder", "wamp.error.no_such_role")):
    recs = records(name)
    refused = [r for r in recs if r["op"] == "refused"]
    if any(r["op"] == "join" for r in recs):
        fail.append(f"{name}: the router let it join application1")
    elif not refused or any(r["error"] != reason for r in refused):
        fail.append(f"{name}: expected refusals with {reason}, got {[r.get('error') for r in refused]}")
    else:
        print(f"    {name}: refused {len(refused)}x with {reason}")

# claims for verify_flags: a PubSub digest and an RPC digest (both count), a control, a flag, a non-claim
for label, val in (("app1 publish digest", app1_pub_digest), ("app1 answer digest", app1_ans_digest),
                   ("public control digest", pub_control_digest), ("an app flag value", app_flag_value)):
    if val is None:
        fail.append(f"could not find {label} for the claims")
if not fail:
    (logs.parent / "claims.txt").write_text("\n".join([
        app1_pub_digest, app1_ans_digest, pub_control_digest, app_flag_value, "-".join(["0000"] * 7)
    ]) + "\n")

print(f"    producer flags: {len(producer_flags)} (publish + answer), none outside its own record; "
      f"rogue-answered probes: application1={app1_rogue_hits}, application2={app2_rogue_hits}")
if fail:
    print("FAIL:", *fail, sep="\n  ", file=sys.stderr)
    sys.exit(1)
print("OK: nine clients, own-realm ticks and matched call results, no errors; "
      "the rogue was detected on application1 and isolated from application2; both intruders refused")
EOF

if [ "${rc}" -eq 0 ]; then
    echo "==> verify_flags.py: a PubSub digest and an RPC digest both count; a control; a flag; a non-claim"
    "${PYTHON}" -I "${here}/referee/verify_flags.py" --logs "${work}/logs" --claims "${work}/claims.txt" \
        --start "${smoke_start}" --end "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)" > "${work}/verify_flags.txt" || rc=1
    sed 's/^/    /' "${work}/verify_flags.txt"
    n_counts="$(grep -cE '^ +[0-9]+ [0-9a-f]{64} counts client0[4-9] application[12] ' "${work}/verify_flags.txt" || true)"
    n_control="$(grep -cE '^ +[0-9]+ [0-9a-f]{64} control client0[1-3] public ' "${work}/verify_flags.txt" || true)"
    n_obtained="$(grep -cE '^ +[0-9]+ [A-Z0-9-]{34} obtained client0[4-9] application[12] ' "${work}/verify_flags.txt" || true)"
    n_invalid="$(grep -cE '^ +[0-9]+ 0000(-0000){6} invalid' "${work}/verify_flags.txt" || true)"
    if [ "${n_counts}" != 2 ] || [ "${n_control}" != 1 ] || [ "${n_obtained}" != 1 ] || [ "${n_invalid}" != 1 ]; then
        echo "FAIL: expected counts 2 (PubSub+RPC), control 1, obtained 1, invalid 1; got ${n_counts}, ${n_control}, ${n_obtained}, ${n_invalid}" >&2
        rc=1
    else
        echo "OK: the PubSub digest and the RPC digest both count, the public digest is a control, the flag is obtained, the non-claim refused"
    fi
fi

if [ "${rc}" -eq 0 ]; then
    echo "==> verify_events.py: the injected events at the application1 members"
    "${PYTHON}" -I "${here}/referee/verify_events.py" --logs "${work}/logs" \
        --start "${smoke_start}" --end "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)" > "${work}/verify_events.txt" || rc=1
    sed 's/^/    /' "${work}/verify_events.txt"
    if ! "${PYTHON}" -I - "${work}/verify_events.txt" <<'EOF'
import re, sys
rows = {}
for line in open(sys.argv[1]):
    m = re.match(r"^  (application[12]) +(client0[4-9]) +(\d+) +(\d+) +(\d+)$", line)
    if m:
        rows[m.group(2)] = (m.group(1), int(m.group(3)), int(m.group(4)))
# application1 members (04,05,06) must have matched>0 and injected>0 (the forged tick + attacker-topic event);
# application2 members (07,08,09) must have injected==0
ok = (all(c in rows and rows[c][1] > 0 and rows[c][2] > 0 for c in ("client04","client05","client06"))
      and all(c in rows and rows[c][2] == 0 for c in ("client07","client08","client09")))
sys.exit(0 if ok else 1)
EOF
    then
        echo "FAIL: expected injected events at each application1 member, none at application2" >&2
        rc=1
    else
        echo "OK: verify_events finds the injected events at the application1 members, none at application2"
    fi
fi

if [ "${rc}" -eq 0 ]; then
    echo "==> verify_calls.py: the rogue-answered calls injected at application1, none at application2"
    "${PYTHON}" -I "${here}/referee/verify_calls.py" --logs "${work}/logs" \
        --start "${smoke_start}" --end "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)" > "${work}/verify_calls.txt" || rc=1
    sed 's/^/    /' "${work}/verify_calls.txt"
    if ! "${PYTHON}" -I - "${work}/verify_calls.txt" <<'EOF'
import re, sys
rows = {}
for line in open(sys.argv[1]):
    m = re.match(r"^  (application[12]) +(client0[4-9]) +(\d+) +(\d+)$", line)
    if m:
        rows[m.group(2)] = (m.group(1), int(m.group(3)), int(m.group(4)))
# application1 (04,05,06): matched>0 (legit peer calls) and injected>0 (the rogue-answered probes);
# application2 (07,08,09): injected==0 (the rogue was on application1 only)
ok = (all(c in rows and rows[c][1] > 0 and rows[c][2] > 0 for c in ("client04","client05","client06"))
      and all(c in rows and rows[c][2] == 0 for c in ("client07","client08","client09")))
sys.exit(0 if ok else 1)
EOF
    then
        echo "FAIL: expected injected call results at each application1 member, none at application2" >&2
        rc=1
    else
        echo "OK: verify_calls finds the rogue-answered calls injected at the application1 members, none at application2"
    fi
fi

if [ "${rc}" -ne 0 ]; then
    tail -n 40 "${work}/crossbar.log" >&2
    KEEP=1
fi
exit "${rc}"
