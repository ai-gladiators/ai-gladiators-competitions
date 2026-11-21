#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Smoke test of the Crossbar.io two-node mesh PubSub + RPC target (crossbar-v2), on one machine:
#   1. copy both node directories to a temporary directory;
#   2. generate a throwaway TLS pair per node (each node trusts the other's certificate
#      as the rlink peer), a node key per node (Crossbar.io's own key.priv/key.pub), and
#      six throwaway client keys;
#   3. write each node's public key into the OTHER node's rlink-listener authorized_keys,
#      the client public keys into both nodes' auth blocks, and the four ports into both
#      configs;
#   4. start both Crossbar.io nodes (epoll reactor) and wait for the web page of each
#      over TLS and for the two router-to-router links to come up;
#   5. start the nine clients SPLIT ACROSS THE TWO NODES, so every application realm has
#      members on both routers: a realm member on node A reaches a realm member on node B
#      only through the rlink. Because the workload uses EXACT URIs, both ticks (PubSub)
#      and echo calls (RPC) are forwarded across the link, so the clients' cross-node
#      events AND cross-node calls prove the mesh forwards both paradigms;
#   6. require the routers to refuse an unknown key and an anonymous client in an
#      authenticated realm;
#   7. PubSub injection at node A (smoke_inject.py): a second copy, a forged tick, an
#      attacker-topic event -- all received by the application1 members, and (exact subs
#      forward) across the rlink by the node-B member too;
#   8. RPC rogue at node A (smoke_rogue.py): register gladiators.attacker.echo on
#      application1 and answer with a fabricated flag -- the application1 members' probes
#      then succeed unexpectedly, on both nodes, and application2's still fail;
#   9. require every client log to show its own-realm ticks and calls, cross-node
#      delivery in BOTH directions for PubSub and RPC, each call result's digest matched
#      to the callee's answer record, no errors, and every flag in its own publish or
#      answer record only;
#  10. run the referee's tools: verify_flags.py (a PubSub digest and an RPC digest both
#      count; a public digest is a control; a flag is obtained), verify_events.py (the
#      injected events at the application1 members, none at application2) and
#      verify_calls.py (the rogue-answered calls injected at application1, none at
#      application2).
#
# Environment: PYTHON (the client's locked venv: uv sync --frozen --project client),
# CROSSBAR (the crossbar executable), DURATION (default 120), PORT_A_CLIENT (8080),
# PORT_A_RLINK (8081), PORT_B_CLIENT (8082), PORT_B_RLINK (8083), KEEP=1 to keep the
# temporary directory.
#
# It starts listeners on four ports. To keep them off the host's network, run it in its
# own network namespace, e.g. `unshare -rn --pid --fork --mount-proc ./smoke.sh`
# (loopback is brought up below when it is down). It refuses to start when a port is
# taken, or when PYTHON or CROSSBAR cannot run it.

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"
CROSSBAR="${CROSSBAR:-crossbar}"
DURATION="${DURATION:-120}"
if [ "${DURATION%.*}" -lt 40 ]; then
    echo "FAIL: DURATION=${DURATION} is too low; the rogue-injection assertion needs a window of at least 40 s to overlap the probe cadence reliably (default 120). Set DURATION=40 or higher." >&2
    exit 1
fi
export CROSSBAR_REACTOR=epoll
PORT_A_CLIENT="${PORT_A_CLIENT:-8080}"
PORT_A_RLINK="${PORT_A_RLINK:-8081}"
PORT_B_CLIENT="${PORT_B_CLIENT:-8082}"
PORT_B_RLINK="${PORT_B_RLINK:-8083}"

work=""
pid_a=""
pid_b=""

cleanup() {
    local rc=$?
    for pid in "${pid_a}" "${pid_b}"; do
        if [ -n "${pid}" ] && kill -0 "${pid}" 2>/dev/null; then
            kill "${pid}" 2>/dev/null || true
            wait "${pid}" 2>/dev/null || true
        fi
    done
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
    echo "FAIL: PYTHON=${PYTHON} is not an executable; set PYTHON to the python of the client's venv, made with: uv sync --frozen --project targets/crossbar/two-node/client (see README)" >&2
    exit 1
fi
missing="$("${PYTHON}" -c '
import importlib.util
print(" ".join(m for m in ("autobahn", "cbor2", "nacl") if importlib.util.find_spec(m) is None))
' 2>/dev/null)" || missing="(python failed to run)"
if [ -n "${missing}" ]; then
    echo "FAIL: PYTHON=${PYTHON} cannot import: ${missing}; set PYTHON to the python of the client's venv, made with: uv sync --frozen --project targets/crossbar/two-node/client (see README)" >&2
    exit 1
fi
if ! command -v "${CROSSBAR}" >/dev/null 2>&1; then
    echo "FAIL: CROSSBAR=${CROSSBAR} is not an executable; set CROSSBAR to the crossbar of a venv with Crossbar.io installed (see README)" >&2
    exit 1
fi
for port in "${PORT_A_CLIENT}" "${PORT_A_RLINK}" "${PORT_B_CLIENT}" "${PORT_B_RLINK}"; do
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
' "${port}"; then
        echo "FAIL: port ${port} is already taken on this machine; set the PORT_* env vars to free ports, or run in a network namespace (see README)" >&2
        exit 1
    fi
done

# the temporary directory exists only once the checks above have passed
work="$(mktemp -d "${TMPDIR:-/tmp}/gladiators-smoke2.XXXXXX")"

echo "==> client self-test"
"${PYTHON}" "${here}/client/workload.py" --self-test

echo "==> VERIFY-at-build: the committed configs are the mesh they claim to be"
"${PYTHON}" -I "${here}/verify_target.py"

echo "==> node directory copies under ${work}"
for n in nodeA nodeB; do
    mkdir -p "${work}/${n}"
    cp -r "${here}/${n}/.crossbar" "${work}/${n}/"
    rm -f "${work}/${n}/.crossbar/server.key" "${work}/${n}/.crossbar/server.crt"
done
cp -r "${here}/web" "${work}/web"

echo "==> throwaway TLS pair per node, cross-trusted as rlink peer.crt"
for n in nodeA nodeB; do
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
        -keyout "${work}/${n}/.crossbar/server.key" -out "${work}/${n}/.crossbar/server.crt" \
        -days 2 -subj "/CN=localhost" -addext "subjectAltName=DNS:localhost" 2>/dev/null
    chmod 600 "${work}/${n}/.crossbar/server.key"
done
cp "${work}/nodeB/.crossbar/server.crt" "${work}/nodeA/.crossbar/peer.crt"
cp "${work}/nodeA/.crossbar/server.crt" "${work}/nodeB/.crossbar/peer.crt"

echo "==> node keys (Crossbar.io generates key.priv/key.pub)"
"${CROSSBAR}" keys --cbdir "${work}/nodeA/.crossbar" >/dev/null 2>&1 || true
"${CROSSBAR}" keys --cbdir "${work}/nodeB/.crossbar" >/dev/null 2>&1 || true
pub_a="$(grep public-key-ed25519 "${work}/nodeA/.crossbar/key.pub" | awk '{print $2}')"
pub_b="$(grep public-key-ed25519 "${work}/nodeB/.crossbar/key.pub" | awk '{print $2}')"
if [ -z "${pub_a}" ] || [ -z "${pub_b}" ]; then
    echo "FAIL: could not read generated node public keys" >&2
    exit 1
fi

echo "==> throwaway client keys"
mkdir -p "${work}/keys" "${work}/logs"
for i in 04 05 06 07 08 09; do
    "${PYTHON}" "${here}/client/workload.py" --generate-key "${work}/keys/client${i}.key" \
        > "${work}/keys/client${i}.pub"
done
"${PYTHON}" "${here}/client/workload.py" --generate-key "${work}/keys/stranger.key" > /dev/null

# Patch each node config: client-facing port, client principal keys (both auth
# blocks), the rlink-listener port + the peer node key it authorizes, and each
# rlink's peer endpoint port + url. All keys are public; no secret is written here.
patch_node() {
    "${PYTHON}" -I - "$@" <<'EOF'
import json, sys, pathlib
cfg_path = pathlib.Path(sys.argv[1])
keys = pathlib.Path(sys.argv[2])
client_port = int(sys.argv[3])
rlink_listen_port = int(sys.argv[4])
peer_rlink_port = int(sys.argv[5])
peer_pubkey = sys.argv[6]
cfg = json.loads(cfg_path.read_text())
worker = cfg["workers"][0]

# client principal public keys, in both the RawSocket and WebSocket auth blocks
def set_client_keys(auth):
    for authid, p in auth["cryptosign"]["principals"].items():
        if authid.startswith("client"):
            p["authorized_keys"] = [(keys / f"{authid}.pub").read_text().strip()]

for t in worker["transports"]:
    if t.get("type") == "universal":
        t["endpoint"]["port"] = client_port
        set_client_keys(t["rawsocket"]["auth"])
        set_client_keys(t["websocket"]["ws"]["auth"])
    elif t.get("type") == "rawsocket":
        # the rlink listener
        t["endpoint"]["port"] = rlink_listen_port
        t["auth"]["cryptosign"]["principals"]["rlink"]["authorized_keys"] = [peer_pubkey]

# each rlink dials the peer's rlink listener
for realm in worker["realms"]:
    for link in realm.get("rlinks", []):
        link["transport"]["endpoint"]["port"] = peer_rlink_port
        link["transport"]["url"] = f"rss://localhost:{peer_rlink_port}"

cfg_path.write_text(json.dumps(cfg, indent=4) + "\n")
EOF
}
patch_node "${work}/nodeA/.crossbar/config.json" "${work}/keys" "${PORT_A_CLIENT}" "${PORT_A_RLINK}" "${PORT_B_RLINK}" "${pub_b}"
patch_node "${work}/nodeB/.crossbar/config.json" "${work}/keys" "${PORT_B_CLIENT}" "${PORT_B_RLINK}" "${PORT_A_RLINK}" "${pub_a}"

echo "==> start both Crossbar.io nodes"
PYTHONUNBUFFERED=1 "${CROSSBAR}" start --cbdir "${work}/nodeA/.crossbar" > "${work}/nodeA.log" 2>&1 &
pid_a=$!
PYTHONUNBUFFERED=1 "${CROSSBAR}" start --cbdir "${work}/nodeB/.crossbar" > "${work}/nodeB.log" 2>&1 &
pid_b=$!

wait_web() {
    local port="$1" crt="$2" name="$3" pid="$4"
    for _ in $(seq 1 60); do
        if curl -s --max-time 5 --resolve "localhost:${port}:127.0.0.1" --cacert "${crt}" "https://localhost:${port}/" > "${work}/${name}.index.html" 2>/dev/null; then
            break
        fi
        if ! kill -0 "${pid}" 2>/dev/null; then
            echo "FAIL: ${name} exited during startup" >&2
            tail -n 40 "${work}/${name}.log" >&2
            exit 1
        fi
        sleep 1
    done
    if ! grep -q "AI Gladiators" "${work}/${name}.index.html"; then
        echo "FAIL: ${name} does not serve the web page over TLS on port ${port}" >&2
        tail -n 40 "${work}/${name}.log" >&2
        exit 1
    fi
    echo "    ${name} web page served over TLS on ${port}"
}
wait_web "${PORT_A_CLIENT}" "${work}/nodeA/.crossbar/server.crt" "nodeA" "${pid_a}"
wait_web "${PORT_B_CLIENT}" "${work}/nodeB/.crossbar/server.crt" "nodeB" "${pid_b}"

echo "==> wait for the two router-to-router links"
# each node starts two rlinks (application1, application2) and then its remote
# session on the peer becomes ready; wait for the remote-session-ready lines.
for _ in $(seq 1 60); do
    a="$(grep -c "rlink remote session ready" "${work}/nodeA.log" 2>/dev/null || true)"
    b="$(grep -c "rlink remote session ready" "${work}/nodeB.log" 2>/dev/null || true)"
    if [ "${a}" -ge 2 ] && [ "${b}" -ge 2 ]; then
        break
    fi
    if ! kill -0 "${pid_a}" 2>/dev/null || ! kill -0 "${pid_b}" 2>/dev/null; then
        echo "FAIL: a node exited while the rlinks were coming up" >&2
        tail -n 40 "${work}/nodeA.log" "${work}/nodeB.log" >&2
        exit 1
    fi
    sleep 1
done
if [ "$(grep -c 'rlink remote session ready' "${work}/nodeA.log" 2>/dev/null || true)" -lt 2 ] || \
   [ "$(grep -c 'rlink remote session ready' "${work}/nodeB.log" 2>/dev/null || true)" -lt 2 ]; then
    echo "FAIL: the router-to-router links did not come up on both realms of both nodes" >&2
    grep -iE "rlink|router-to-router|session ready|Deny|denied|refused" "${work}/nodeA.log" "${work}/nodeB.log" | tail -40 >&2
    exit 1
fi
echo "    both nodes: rlinks ready on application1 and application2"

for name in nodeA nodeB; do
    grep -q "Running as PID [0-9]* on [A-Za-z]*-EPollReactor" "${work}/${name}.log" || {
        echo "FAIL: ${name} is not running on the epoll reactor" >&2; exit 1; }
done

echo "==> router stack"
grep -h -o "Running as PID [0-9]* on [A-Za-z]*-[A-Za-z]*Reactor" "${work}"/node*.log | sort -u | sed 's/^/    /'

# client -> node assignment. Each APPLICATION realm spans both nodes, so a realm's
# members can only reach each other through the rlink (the mesh is exercised, for both
# PubSub and RPC, and proven). The `public` realm is NOT meshed (only the application
# realms are forwarded), so its three members all sit on node A -- otherwise they could
# not see each other.
#   node A (client port A): client01 client02 client03 client04 client05 client07
#   node B (client port B): client06 client08 client09
# application1 = {04,05 @A, 06 @B}; application2 = {07 @A, 08,09 @B}; public = {01,02,03 @A}
node_label_of() {
    case "$1" in
        client01|client02|client03|client04|client05|client07) echo nodeA ;;
        *) echo nodeB ;;
    esac
}
node_port_of() {
    case "$1" in
        client01|client02|client03|client04|client05|client07) echo "${PORT_A_CLIENT}" ;;
        *) echo "${PORT_B_CLIENT}" ;;
    esac
}
node_crt_of() {
    case "$1" in
        client01|client02|client03|client04|client05|client07) echo "${work}/nodeA/.crossbar/server.crt" ;;
        *) echo "${work}/nodeB/.crossbar/server.crt" ;;
    esac
}
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

echo "==> nine clients across both nodes for ${DURATION} s (+ stop grace)"
smoke_start="$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"
pids=()
n=0
for c in client01 client02 client03 client04 client05 client06 client07 client08 client09; do
    port="$(node_port_of "${c}")"
    crt="$(node_crt_of "${c}")"
    label="$(node_label_of "${c}")"
    if [ $((n % 3)) -eq 0 ]; then url="rss://localhost:${port}"; else url="wss://localhost:${port}/ws"; fi
    n=$((n + 1))
    keyarg=()
    if [ -f "${work}/keys/${c}.key" ]; then keyarg=(--key "${work}/keys/${c}.key"); fi
    "${PYTHON}" "${here}/client/workload.py" --name "${c}" --realm "$(realm_of "${c}")" \
        --url "${url}" --node "${label}" --trust "${crt}" "${keyarg[@]}" \
        --peers "$(peers_of "${c}")" --seed smoke --log "${work}/logs/${c}.jsonl" \
        --duration "${DURATION}" --peer-wait 60 > "${work}/logs/${c}.out" 2>&1 &
    pids+=($!)
done

echo "==> refusals at node A: an unknown key, and an anonymous client in an authenticated realm"
"${PYTHON}" "${here}/client/workload.py" --name client04 --realm application1 \
    --url "wss://localhost:${PORT_A_CLIENT}/ws" --node nodeA --trust "${work}/nodeA/.crossbar/server.crt" \
    --key "${work}/keys/stranger.key" --seed smoke --log "${work}/logs/stranger.jsonl" \
    --duration 5 > "${work}/logs/stranger.out" 2>&1 || true
"${PYTHON}" "${here}/client/workload.py" --name intruder --realm application1 \
    --url "wss://localhost:${PORT_A_CLIENT}/ws" --node nodeA --trust "${work}/nodeA/.crossbar/server.crt" \
    --seed smoke --log "${work}/logs/intruder.jsonl" \
    --duration 5 > "${work}/logs/intruder.out" 2>&1 || true

echo "==> PubSub injection on application1 at node A: a forged tick, a second copy, an attacker-topic event"
# Inject as client04 (node A) and replay/forge a node-A member (client05), so the three
# injected events reach the node-A application1 members directly; because the workload's
# subscriptions are exact, the router also forwards them across the rlink to client06 on
# node B (the cross-node reach of an injection).
"${PYTHON}" "${here}/smoke_inject.py" --name client04 --realm application1 \
    --key "${work}/keys/client04.key" --forge-as client05 --replay-from client05 \
    --url "wss://localhost:${PORT_A_CLIENT}/ws" --trust "${work}/nodeA/.crossbar/server.crt" \
    --out "${work}/inject.json" > "${work}/inject.out" 2>&1 || true

echo "==> RPC rogue on application1 at node A: register gladiators.attacker.echo, answer with a fake flag"
if [ "${DURATION%.*}" -gt 40 ]; then rogue_hold=30; else rogue_hold=$(( ${DURATION%.*} / 2 )); fi
"${PYTHON}" "${here}/smoke_rogue.py" --name client04 --realm application1 \
    --key "${work}/keys/client04.key" --claim-as client05 --hold "${rogue_hold}" \
    --url "wss://localhost:${PORT_A_CLIENT}/ws" --trust "${work}/nodeA/.crossbar/server.crt" \
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

echo "==> check the logs (own-realm ticks + cross-node PubSub and RPC across the mesh)"
"${PYTHON}" -I - "${work}/logs" "${here}/referee/verify_flags.py" "${work}/inject.json" "${work}/rogue.json" <<'EOF' || rc=1
import hashlib, json, re, sys, pathlib
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
    fail.append("the verifier's FLAG_CHARS is not autobahn's DEFAULT_TOKEN_CHARS")
g = f"[{DEFAULT_TOKEN_CHARS}]{{4}}"
flagform = re.compile(rf"^{g}(-{g}){{6}}$")

# the client -> node split (must match the bash assignment above)
NODE = {"client01": "A", "client02": "A", "client03": "A", "client04": "A", "client05": "A",
        "client07": "A", "client06": "B", "client08": "B", "client09": "B"}
PEER = re.compile(r"^client0[1-9]$")

def records(name):
    p = logs / f"{name}.jsonl"
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []

REALM = {**{f"client0{i}": "public" for i in (1, 2, 3)},
         **{f"client0{i}": "application1" for i in (4, 5, 6)},
         **{f"client0{i}": "application2" for i in (7, 8, 9)}}

producer_flags = {}    # flag -> (name, realm, op): every publish/answer flag, from its own record
others_text = []       # every record that is not a publish/answer, as text, for the leak scan
app1_rogue_hits = 0
app2_rogue_hits = 0
app1_rogue_nodeB = 0
# cross-node delivery counters (A<->B), for app realms
xpub = {"AtoB": 0, "BtoA": 0}   # a tick published on one node, received as a clean event on the other
xrpc = {"AtoB": 0, "BtoA": 0}   # a call answered by a peer on the other node (payload + callee correct)
rpc_broken = []
app1_pub_digest = app1_ans_digest = pub_control_digest = app_flag_value = None

INTEGRITY = ("payload changed", "answered by another callee")
XNODE_CALLER = "router reported another caller authid"  # the SUT discloses authrole across the rlink

start = next((r for r in records("client02") if r["op"] == "start"), None)
if start:
    print(f"    client stack (client02), node={start.get('node')}:")
    for k, v in start["stack"].items():
        print(f"      {k:22} {v}")

for i in range(1, 10):
    name = f"client{i:02d}"
    realm = REALM[name]
    mynode = NODE[name]
    recs = records(name)
    joins = [r for r in recs if r["op"] == "join"]
    ws = [r for r in recs if r["op"] == "workload_start"]
    calls = [r for r in recs if r["op"] == "call"]
    probes = [r for r in recs if r["op"] == "probe_call"]
    events = [r for r in recs if r["op"] == "event"]
    pubs = [r for r in recs if r["op"] == "publish"]
    answers = [r for r in recs if r["op"] == "answer"]
    bad = [r for r in recs if r.get("error") and r["op"] not in ("probe_call",)]
    rogue_hits = [r for r in probes if r.get("violation") and "rogue registration" in r["violation"]]
    nspc = [r for r in probes if r.get("outcome") == "wamp.error.no_such_procedure"]

    # cross-node PubSub: clean tick events whose stated publisher is a peer on the other node
    for r in events:
        if r.get("op") == "event" and r.get("topic") == "gladiators.tick" and r.get("violation") is None:
            peer = r.get("peer")
            if isinstance(peer, str) and PEER.match(peer) and peer in NODE and NODE[peer] != mynode:
                xpub["AtoB" if mynode == "B" else "BtoA"] += 1  # receiver on B => published on A
    # cross-node RPC: calls answered by a peer on the other node, payload + callee correct
    for r in calls:
        if r.get("result_digest") is None:
            continue
        peer = r.get("peer")
        if isinstance(peer, str) and peer in NODE and NODE[peer] != mynode:
            if r.get("violation") in (None, XNODE_CALLER):
                xrpc["AtoB" if mynode == "A" else "BtoA"] += 1
    rpc_broken += [(name, r.get("peer"), r.get("violation")) for r in calls if r.get("violation") in INTEGRITY]

    lat = sorted(r["latency_ms"] for r in calls if r.get("ok") and isinstance(r.get("latency_ms"), (int, float)))
    p99 = lat[int(0.99 * (len(lat) - 1))] if lat else None
    print(f"    {name} {realm:13} @node{mynode} joins={len(joins)} calls={len(calls)} answers={len(answers)} "
          f"events={len(events)} pubs={len(pubs)} probes={len(probes)}(nspc={len(nspc)},rogue={len(rogue_hits)}) p99_ms={p99}")
    if len(joins) != 1: fail.append(f"{name}: joined {len(joins)} times")
    if joins and joins[0].get("node") != f"node{mynode}": fail.append(f"{name}: join record node={joins[0].get('node')}, expected node{mynode}")
    if len(ws) != 1 or (ws and ws[0].get("missing")): fail.append(f"{name}: workload_start {ws}")
    if not calls: fail.append(f"{name}: no calls")
    if not events: fail.append(f"{name}: no ticks received")
    if not pubs: fail.append(f"{name}: no ticks published")
    if not answers: fail.append(f"{name}: no echo answers given")
    if not probes: fail.append(f"{name}: never probed the attacker procedure")
    if bad: fail.append(f"{name}: {len(bad)} error records, e.g. {bad[0]}")
    for r in pubs + answers:
        f = r.get("flag")
        if not (isinstance(f, str) and flagform.match(f)):
            fail.append(f"{name}: a {r['op']} record without a valid flag: {r}")
        elif f in producer_flags:
            fail.append(f"{name}: flag {f} produced twice, also by {producer_flags[f]}")
        else:
            producer_flags[f] = (name, realm, r["op"])
    others_text.append(json.dumps([r for r in recs if r["op"] not in ("publish", "answer")]))
    if realm == "application1":
        app1_rogue_hits += len(rogue_hits)
        if mynode == "B":
            app1_rogue_nodeB += len(rogue_hits)
    if realm == "application2": app2_rogue_hits += len(rogue_hits)
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

# cross-node PubSub and RPC must both have crossed the rlink in BOTH directions
if xpub["AtoB"] == 0 or xpub["BtoA"] == 0:
    fail.append(f"cross-node PubSub did not traverse the rlink both ways: {xpub} "
                "(an app-realm tick on one node did not reach a member on the other)")
if xrpc["AtoB"] == 0 or xrpc["BtoA"] == 0:
    fail.append(f"cross-node RPC did not traverse the rlink both ways: {xrpc} "
                "(an app-realm call was not answered by a member on the other node)")
if rpc_broken:
    fail.append(f"cross-node RPC integrity broken (wrong payload or callee): {rpc_broken[:3]}")

# RPC rogue detected on application1 (both nodes), and realm isolation held for application2
if app1_rogue_hits < 2:
    fail.append(f"application1 members recorded only {app1_rogue_hits} rogue-answered probes (expected the rogue to answer their probes)")
if app2_rogue_hits != 0:
    fail.append(f"application2 members recorded {app2_rogue_hits} rogue answers: the rogue on application1 reached application2 (realm isolation broke)")

# refusals, as in crossbar-v1
for cname, reason in (("stranger", "wamp.error.not_authorized"), ("intruder", "wamp.error.no_such_role")):
    recs = records(cname)
    refused = [r for r in recs if r["op"] == "refused"]
    if any(r["op"] == "join" for r in recs):
        fail.append(f"{cname}: the router let it join application1")
    elif not refused or any(r["error"] != reason for r in refused):
        fail.append(f"{cname}: expected refusals with {reason}, got {[r.get('error') for r in refused]}")
    else:
        print(f"    {cname}: refused {len(refused)}x with {reason}")

for label, val in (("app1 publish digest", app1_pub_digest), ("app1 answer digest", app1_ans_digest),
                   ("public control digest", pub_control_digest), ("an app flag value", app_flag_value)):
    if val is None:
        fail.append(f"could not find {label} for the claims")
if not fail:
    (logs.parent / "claims.txt").write_text("\n".join([
        app1_pub_digest, app1_ans_digest, pub_control_digest, app_flag_value, "-".join(["0000"] * 7)
    ]) + "\n")

print(f"    producer flags: {len(producer_flags)} (publish + answer), none outside its own record")
print(f"    cross-node PubSub across the rlink: A->B={xpub['AtoB']} ticks, B->A={xpub['BtoA']} ticks")
print(f"    cross-node RPC   across the rlink: A->B={xrpc['AtoB']} calls, B->A={xrpc['BtoA']} calls (payload+callee correct)")
print(f"    rogue-answered probes: application1={app1_rogue_hits} (of which node B={app1_rogue_nodeB}), application2={app2_rogue_hits}")
if fail:
    print("FAIL:", *fail, sep="\n  ", file=sys.stderr)
    sys.exit(1)
print("OK: nine clients across two nodes; own-realm ticks and matched call results; cross-node PubSub AND RPC "
      "forwarded both ways over the rlink; no errors; the rogue detected on application1 and isolated from "
      "application2; both intruders refused")
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
    echo "==> verify_events.py: the injected events at the application1 members, none at application2"
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
# The injection is on application1 at node A. The node-A application1 members (04,05)
# record the injected events; application2 members (07,08,09) record none. Every
# application1 member must also have matched>0 (legit ticks, cross-node included); the
# node-B member (06) is reported but its injected count is not hard-asserted, as cross-
# node delivery of the injected events depends on rlink timing.
ok = (all(c in rows and rows[c][1] > 0 and rows[c][2] > 0 for c in ("client04", "client05"))
      and "client06" in rows and rows["client06"][1] > 0
      and all(c in rows and rows[c][2] == 0 for c in ("client07", "client08", "client09")))
sys.exit(0 if ok else 1)
EOF
    then
        echo "FAIL: expected injected events at each node-A application1 member, matched at all, none injected at application2" >&2
        rc=1
    else
        echo "OK: verify_events finds the injected events at the node-A application1 members, none at application2"
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
# The rogue is on application1 at node A. The node-A application1 members (04,05) probe
# it directly and record injected call results; application2 (07,08,09) records none.
# Every application1 member has matched>0 (legit peer calls, cross-node included); the
# node-B member (06) is reported, its injected count not hard-asserted (rlink timing).
ok = (all(c in rows and rows[c][1] > 0 and rows[c][2] > 0 for c in ("client04", "client05"))
      and "client06" in rows and rows["client06"][1] > 0
      and all(c in rows and rows[c][2] == 0 for c in ("client07", "client08", "client09")))
sys.exit(0 if ok else 1)
EOF
    then
        echo "FAIL: expected injected call results at each node-A application1 member, matched at all, none at application2" >&2
        rc=1
    else
        echo "OK: verify_calls finds the rogue-answered calls injected at the node-A application1 members, none at application2"
    fi
fi

if [ "${rc}" -ne 0 ]; then
    tail -n 40 "${work}/nodeA.log" "${work}/nodeB.log" >&2
    KEEP=1
fi
exit "${rc}"
