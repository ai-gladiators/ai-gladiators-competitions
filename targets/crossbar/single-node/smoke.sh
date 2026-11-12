#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Smoke test of the Crossbar.io single-node target, on one machine:
#   1. copy the node directory to a temporary directory;
#   2. generate a throwaway TLS pair and six throwaway client keys, and write
#      the public keys into that copy's config.json;
#   3. start Crossbar.io and the nine clients for DURATION seconds;
#   4. require every client log to show calls and ticks of its own realm, no
#      errors and no violations; and require the router to refuse an unknown
#      key and an anonymous client in an authenticated realm.
#
# Environment: PYTHON (with autobahn, see client/requirements.txt), CROSSBAR
# (the crossbar executable), DURATION (default 30), PORT (default 8080),
# KEEP=1 to keep the temporary directory.
#
# It starts a server on PORT. To keep it off the host's network, run it in its
# own network namespace, e.g. `unshare -rn --pid --fork --mount-proc ./smoke.sh`
# (loopback is brought up below when it is down).

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"
CROSSBAR="${CROSSBAR:-crossbar}"
DURATION="${DURATION:-30}"
PORT="${PORT:-8080}"

work="$(mktemp -d "${TMPDIR:-/tmp}/gladiators-smoke.XXXXXX")"
node="${work}/node"
cb_pid=""

cleanup() {
    if [ -n "${cb_pid}" ] && kill -0 "${cb_pid}" 2>/dev/null; then
        kill "${cb_pid}" 2>/dev/null || true
        wait "${cb_pid}" 2>/dev/null || true
    fi
    if [ "${KEEP:-0}" = "1" ]; then
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
"${CROSSBAR}" start --cbdir "${node}/.crossbar" > "${work}/crossbar.log" 2>&1 &
cb_pid=$!
for _ in $(seq 1 60); do
    if curl -s --cacert "${node}/.crossbar/server.crt" "https://localhost:${PORT}/" > "${work}/index.html" 2>/dev/null; then
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

echo "==> nine clients for ${DURATION} s"
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
        --duration "${DURATION}" > "${work}/logs/${c}.out" 2>&1 &
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

rc=0
for p in "${pids[@]}"; do
    wait "${p}" || rc=1
done
if [ "${rc}" -ne 0 ]; then
    echo "FAIL: a client exited non-zero" >&2
fi

echo "==> check the logs"
"${PYTHON}" -I - "${work}/logs" <<'EOF' || rc=1
import json, sys, pathlib
logs = pathlib.Path(sys.argv[1])
fail = []
def records(name):
    p = logs / f"{name}.jsonl"
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []
for i in range(1, 10):
    name = f"client{i:02d}"
    recs = records(name)
    calls = [r for r in recs if r["op"] == "call"]
    events = [r for r in recs if r["op"] == "event"]
    joins = [r for r in recs if r["op"] == "join"]
    bad = [r for r in recs if r.get("error") or r.get("violation")]
    lat = sorted(r["latency_ms"] for r in calls if r.get("ok"))
    p99 = lat[int(0.99 * (len(lat) - 1))] if lat else None
    print(f"    {name} {recs[0]['realm'] if recs else '?':13} joins={len(joins)} calls={len(calls)} "
          f"events={len(events)} bad={len(bad)} p99_ms={p99}")
    if not joins: fail.append(f"{name}: never joined")
    if not calls: fail.append(f"{name}: no calls")
    if not events: fail.append(f"{name}: no ticks received")
    for r in bad[:3]: fail.append(f"{name}: {r}")
for name, reason in (("stranger", "wamp.error.not_authorized"), ("intruder", "wamp.error.no_such_role")):
    recs = records(name)
    refused = [r for r in recs if r["op"] == "refused"]
    if any(r["op"] == "join" for r in recs):
        fail.append(f"{name}: the router let it join application1")
    elif not refused or any(r["error"] != reason for r in refused):
        fail.append(f"{name}: expected refusals with {reason}, got {[r.get('error') for r in refused]}")
    else:
        print(f"    {name}: refused {len(refused)}x with {reason}")
if fail:
    print("FAIL:", *fail, sep="\n  ", file=sys.stderr)
    sys.exit(1)
print("OK: nine clients, own-realm ticks only, no errors, no violations; both intruders refused")
EOF

if [ "${rc}" -ne 0 ]; then
    tail -n 40 "${work}/crossbar.log" >&2
    KEEP=1
fi
exit "${rc}"
