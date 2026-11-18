#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Smoke test of the Crossbar.io single-node target, on one machine:
#   1. copy the node directory to a temporary directory;
#   2. generate a throwaway TLS pair and six throwaway client keys, and write
#      the public keys into that copy's config.json;
#   3. start Crossbar.io (epoll reactor) and the nine clients for DURATION
#      seconds, and print what both ends actually run on (Python, OpenSSL,
#      library versions, event loop);
#   4. require the router to refuse an unknown key and an anonymous client in
#      an authenticated realm;
#   5. inject three events on application1 that no member published as such (a
#      second copy of a tick, a forged tick, an off-topic event: smoke_inject.py);
#   6. require every client log to show calls and ticks of its own realm, no
#      errors, and exactly the violations the injected events must cause; every
#      flag in its publish record only;
#   7. run the referee's tools: verify_flags.py on digests and a flag, and
#      verify_events.py, which must find the three injected events at each
#      member of application1 and nothing else (competitions#22, #24).
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
    echo "FAIL: PYTHON=${PYTHON} is not an executable; set PYTHON to the python of the client's venv, made with: uv sync --frozen --project targets/crossbar/single-node/client (see README)" >&2
    exit 1
fi
missing="$("${PYTHON}" -c '
import importlib.util
print(" ".join(m for m in ("autobahn", "cbor2", "nacl") if importlib.util.find_spec(m) is None))
' 2>/dev/null)" || missing="(python failed to run)"
if [ -n "${missing}" ]; then
    echo "FAIL: PYTHON=${PYTHON} cannot import: ${missing}; set PYTHON to the python of the client's venv, made with: uv sync --frozen --project targets/crossbar/single-node/client (see README)" >&2
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

echo "==> injected events on application1: a second copy, a forged tick, an off-topic event"
"${PYTHON}" "${here}/smoke_inject.py" --name client04 --realm application1 \
    --key "${work}/keys/client04.key" --forge-as client05 \
    --url "wss://localhost:${PORT}/ws" --trust "${node}/.crossbar/server.crt" \
    --out "${work}/inject.json" > "${work}/inject.out" 2>&1 || true

rc=0
for p in "${pids[@]}"; do
    wait "${p}" || rc=1
done
if [ "${rc}" -ne 0 ]; then
    echo "FAIL: a client exited non-zero" >&2
fi

echo "==> check the logs"
"${PYTHON}" -I - "${work}/logs" "${here}/referee/verify_flags.py" "${work}/inject.json" <<'EOF' || rc=1
import collections, hashlib, json, re, sys, pathlib
logs = pathlib.Path(sys.argv[1])
fail = []
inject = json.loads(pathlib.Path(sys.argv[3]).read_text()) if pathlib.Path(sys.argv[3]).exists() else {"error": "no report"}
if "error" in inject:
    fail.append(f"the injector failed: {inject['error']}")
# the violations the injected events must cause, and no other (competitions#24)
expected = collections.Counter()
if "replayed" in inject:
    for member in ("client04", "client05", "client06"):
        expected[(member, "event on an unexpected topic")] += 1
    forged = "names this client, but it did not publish this digest, or already received it"
    expected[(inject["forged"]["name"], forged)] += 1  # its name, a digest it never made
    expected[(inject["replayed"]["name"], forged)] += 1  # its own tick, a second time
    print(f"    injected as {inject['authid']}: a copy of {inject['replayed']['name']} seq={inject['replayed']['seq']},"
          f" a tick forged as {inject['forged']['name']}, an event on {inject['off_topic']}")
violations = collections.Counter()
from autobahn.util import DEFAULT_TOKEN_CHARS
# the verifier repeats autobahn's token alphabet, to need only the standard library: they must agree
m = re.search(r'^FLAG_CHARS = "([^"]*)"$', pathlib.Path(sys.argv[2]).read_text(), re.M)
if not m or m.group(1) != DEFAULT_TOKEN_CHARS:
    fail.append(f"the verifier's FLAG_CHARS {m and m.group(1)!r} is not autobahn's DEFAULT_TOKEN_CHARS {DEFAULT_TOKEN_CHARS!r}")
g = f"[{DEFAULT_TOKEN_CHARS}]{{4}}"
flagform = re.compile(rf"^{g}(-{g}){{6}}$")
flags = {}  # flag -> (client, realm, seq): every tick's flag, from the publishers' logs (competitions#21)
others = []  # every record that is not a publish record, of all nine clients
def records(name):
    p = logs / f"{name}.jsonl"
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []
start = next((r for r in records("client02") if r["op"] == "start"), None)
if start:
    print("    client stack (client02):")
    for k, v in start["stack"].items():
        print(f"      {k:22} {v}")
for i in range(1, 10):
    name = f"client{i:02d}"
    recs = records(name)
    calls = [r for r in recs if r["op"] == "call"]
    events = [r for r in recs if r["op"] == "event"]
    joins = [r for r in recs if r["op"] == "join"]
    bad = [r for r in recs if r.get("error")]
    violations.update((name, r["violation"]) for r in recs if r.get("violation"))
    lat = sorted(r["latency_ms"] for r in calls if r.get("ok"))
    p99 = lat[int(0.99 * (len(lat) - 1))] if lat else None
    wsx = joins[0].get("websocket_extensions") if joins else None
    via = "RawSocket" if joins and wsx is None else f"WebSocket ext={wsx}"
    print(f"    {name} {recs[0]['realm'] if recs else '?':13} {via:20} joins={len(joins)} calls={len(calls)} "
          f"events={len(events)} errors={len(bad)} violations={sum(1 for r in recs if r.get('violation'))} p99_ms={p99}")
    if not joins: fail.append(f"{name}: never joined")
    for j in joins:
        if j.get("websocket_extensions"):  # None on RawSocket, [] on WebSocket without extensions
            fail.append(f"{name}: WebSocket extensions negotiated: {j['websocket_extensions']}")
    starts = [r for r in recs if r["op"] == "workload_start"]
    if len(starts) != 1 or starts[0].get("missing"):
        fail.append(f"{name}: expected one workload_start with every peer ready, got {starts}")
    if not calls: fail.append(f"{name}: no calls")
    if not events: fail.append(f"{name}: no ticks received")
    pubs = [r for r in recs if r["op"] == "publish"]
    others += [r for r in recs if r["op"] != "publish"]
    if not pubs: fail.append(f"{name}: no ticks published")
    for r in pubs:
        if not (isinstance(r.get("seq"), int) and isinstance(r.get("flag"), str) and flagform.match(r["flag"])):
            fail.append(f"{name}: a publish record without a seq and a flag of 7 groups of 4: {r}")
        elif r["flag"] in flags:
            fail.append(f"{name}: flag {r['flag']} published twice, also by {flags[r['flag']]}")
        else:
            flags[r["flag"]] = (name, r["realm"], r["seq"])
    seqs = [r.get("seq") for r in pubs]
    if seqs != list(range(1, len(seqs) + 1)):
        fail.append(f"{name}: publish seqs are not 1, 2, 3, ...: {seqs[:10]}")
    for r in bad[:3]: fail.append(f"{name}: {r}")
for name, reason in (("stranger", "wamp.error.not_authorized"), ("intruder", "wamp.error.no_such_role")):
    recs = records(name)
    refused = [r for r in recs if r["op"] == "refused"]
    if any(r["op"] == "join" for r in recs):
        fail.append(f"{name}: the router let it join application1")
    elif not refused or any(r["error"] != reason for r in refused):
        fail.append(f"{name}: expected refusals with {reason}, got {[r.get('error') for r in refused]}")
    elif [(r.get("attempt"), r.get("next_s")) for r in refused] != [(n, 5.0 * 2 ** (n - 1)) for n in range(1, len(refused) + 1)]:
        # each refusal in a row doubles the delay before the next attempt (competitions#10)
        fail.append(f"{name}: expected backing-off refusals, got {[(r.get('attempt'), r.get('next_s')) for r in refused]}")
    else:
        print(f"    {name}: refused {len(refused)}x with {reason}, backing off {[r['next_s'] for r in refused]} s")
if violations != expected:
    fail.append(f"violations: expected {dict(expected)}, got {dict(violations)}")
else:
    print(f"    violations: exactly the {sum(expected.values())} the injected events must cause")
# only the publisher records a flag: no other record of any client holds one, under any key
text = json.dumps(others)
leaked = [f for f in flags if f in text]
if leaked:
    fail.append(f"{len(leaked)} flags appear in records other than their publish record, e.g. {leaked[0]}")
# every tick on the wire carried its flag's digest: each member recorded the digests it received
digests = {hashlib.sha256(f.encode("utf-8")).hexdigest() for f in flags}
received = [r for r in others if r["op"] == "event" and r.get("topic") == "gladiators.tick"]
# (the forged tick is the one exception: only the member it names can tell on arrival)
allowed = digests | ({inject["forged"]["digest"]} if "forged" in inject else set())
if not received or not all(r.get("digest") in allowed for r in received if r.get("violation") is None):
    fail.append("an event without a violation carries a digest of no published flag")
# claims for the verifier: a digest of each app realm and of public, one flag, one non-claim
claims = []
for realm in ("application1", "application2", "public"):
    pick = next((f for f, (_, r, _) in flags.items() if r == realm), None)
    if pick is None:
        fail.append(f"no flag published on {realm}")
    else:
        claims.append(hashlib.sha256(pick.encode("utf-8")).hexdigest())
claims.append(next((f for f, (_, r, _) in flags.items() if r == "application2"), ""))  # the flag itself
claims.append("-".join(["0000"] * 7))  # the form of a flag, but 0 is not in the alphabet
(logs.parent / "claims.txt").write_text("\n".join(claims) + "\n")
print(f"    flags: {len(flags)} published, all distinct, none outside its publish record; {len(received)} ticks received")
if fail:
    print("FAIL:", *fail, sep="\n  ", file=sys.stderr)
    sys.exit(1)
print("OK: nine clients, own-realm ticks only, no errors, the injected events seen; both intruders refused")
EOF

if [ "${rc}" -eq 0 ]; then
    echo "==> the referee's verify_flags.py on five claims (competitions#22, #24)"
    "${PYTHON}" -I "${here}/referee/verify_flags.py" --logs "${work}/logs" --claims "${work}/claims.txt" \
        --start "${smoke_start}" --end "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)" > "${work}/verify.txt" || rc=1
    sed 's/^/    /' "${work}/verify.txt"
    n_counts="$(grep -cE '^ +[0-9]+ [0-9a-f]{64} counts client0[4-9] application[12] ' "${work}/verify.txt" || true)"
    n_control="$(grep -cE '^ +[0-9]+ [0-9a-f]{64} control client0[1-3] public ' "${work}/verify.txt" || true)"
    n_obtained="$(grep -cE '^ +[0-9]+ [A-Z0-9-]{34} obtained client0[7-9] application2 ' "${work}/verify.txt" || true)"
    n_invalid="$(grep -cE '^ +[0-9]+ 0000(-0000){6} invalid: neither a digest nor a flag' "${work}/verify.txt" || true)"
    if [ "${n_counts}" != 2 ] || [ "${n_control}" != 1 ] || [ "${n_obtained}" != 1 ] || [ "${n_invalid}" != 1 ]; then
        echo "FAIL: expected counts 2, control 1, obtained 1, invalid 1; got ${n_counts}, ${n_control}, ${n_obtained}, ${n_invalid}" >&2
        rc=1
    else
        echo "OK: the app-realm digests count, the public one is a control, the flag is reported apart, the non-claim refused"
    fi
fi

if [ "${rc}" -eq 0 ]; then
    echo "==> the referee's verify_events.py (competitions#24)"
    "${PYTHON}" -I "${here}/referee/verify_events.py" --logs "${work}/logs" \
        --start "${smoke_start}" --end "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)" > "${work}/events.txt" || rc=1
    sed 's/^/    /' "${work}/events.txt"
    # each member of application1: some matched, exactly three injected; application2: none injected
    if ! "${PYTHON}" -I - "${work}/events.txt" <<'EOF'
import re, sys
rows = {}
for line in open(sys.argv[1]):
    m = re.match(r"^  (application[12]) +(client0[4-9]) +(\d+) +(\d+) +(\d+)$", line)
    if m:
        rows[m.group(2)] = (m.group(1), int(m.group(3)), int(m.group(4)))
want_injected = {"client04": 3, "client05": 3, "client06": 3, "client07": 0, "client08": 0, "client09": 0}
ok = set(rows) == set(want_injected) and all(rows[c][1] > 0 and rows[c][2] == n for c, n in want_injected.items())
sys.exit(0 if ok else 1)
EOF
    then
        echo "FAIL: expected three injected events at each member of application1, none on application2" >&2
        rc=1
    else
        echo "OK: verify_events finds the three injected events at each member of application1, and nothing else"
    fi
fi

if [ "${rc}" -ne 0 ]; then
    tail -n 40 "${work}/crossbar.log" >&2
    KEEP=1
fi
exit "${rc}"
