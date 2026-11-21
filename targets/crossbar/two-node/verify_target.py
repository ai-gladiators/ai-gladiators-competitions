#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""VERIFY-at-build: the crossbar-v2 two-node mesh target is the mesh it claims to be.

A match is only fair if the published target actually has the property a contestant
is told to attack: two routers, joined by a TLS router-to-router link that forwards
BOTH application realms in BOTH directions. A silent misconfiguration — a missing
rlink, an rlink role without publish or subscribe, a listener that does not authorize
the peer — would make a `red` failure meaningless. This check reads both node configs
and refuses (exit non-zero) unless every such property holds. It is a target-correctness
check, not an exploit; it reads only the published configuration.

    python3 verify_target.py                      # checks ./nodeA and ./nodeB
    python3 verify_target.py --root path/to/two-node

Stdlib only.
"""
import argparse
import json
import os
import sys

APP_REALMS = ("application1", "application2")


def load(root, node):
    path = os.path.join(root, node, ".crossbar", "config.json")
    if not os.path.isfile(path):
        raise SystemExit(f"REFUSING: no config at {path}")
    with open(path) as f:
        return json.load(f), path


def worker_of(cfg, path):
    workers = [w for w in cfg.get("workers", []) if w.get("type") == "router"]
    if len(workers) != 1:
        raise SystemExit(f"REFUSING: {path}: expected exactly one router worker, got {len(workers)}")
    return workers[0]


def realm_by_name(worker):
    return {r["name"]: r for r in worker.get("realms", [])}


def role_allows(realm, role_name):
    """Return the merged allow-set of the named role across its permissions."""
    allow = {"publish": False, "subscribe": False, "call": False, "register": False}
    for role in realm.get("roles", []):
        if role.get("name") != role_name:
            continue
        for perm in role.get("permissions", []):
            a = perm.get("allow", {})
            for k in allow:
                if a.get(k):
                    allow[k] = True
    return allow


def endpoint_is_tls(transport):
    ep = transport.get("endpoint", {})
    return isinstance(ep.get("tls"), dict)


def check_node(root, node, peer_rlink_port, fails):
    cfg, path = load(root, node)
    worker = worker_of(cfg, path)
    realms = realm_by_name(worker)

    # 1. both application realms present.
    for name in APP_REALMS:
        if name not in realms:
            fails.append(f"{node}: application realm '{name}' is missing")
            continue
        realm = realms[name]

        # 2. an rlink role on the realm with call AND register (cross-node RPC, this
        #    target's shared-realm property), and publish AND subscribe (the role is a
        #    full mesh-link role).
        allow = role_allows(realm, "rlink")
        if not (allow["call"] and allow["register"]):
            fails.append(
                f"{node}/{name}: role 'rlink' must allow call and register "
                f"(cross-node RPC needs both; got call={allow['call']}, register={allow['register']})"
            )
        if not (allow["publish"] and allow["subscribe"]):
            fails.append(
                f"{node}/{name}: role 'rlink' must allow publish and subscribe "
                f"(got publish={allow['publish']}, subscribe={allow['subscribe']})"
            )

        # 3. an rlinks entry forwarding this realm to the peer, over TLS, forwarding
        #    local traffic (so this node's events reach the peer).
        links = [l for l in realm.get("rlinks", []) if l.get("realm") == name]
        if not links:
            fails.append(f"{node}/{name}: no rlinks entry forwarding realm '{name}' to the peer")
        for link in links:
            if not link.get("forward_local_events"):
                fails.append(f"{node}/{name}: rlink does not forward_local_events (cross-node delivery would not happen)")
            if not link.get("forward_local_invocations"):
                fails.append(f"{node}/{name}: rlink does not forward_local_invocations (cross-node calls would not happen)")
            tr = link.get("transport", {})
            if not endpoint_is_tls(tr):
                fails.append(f"{node}/{name}: the rlink transport is not TLS")
            if tr.get("endpoint", {}).get("port") != peer_rlink_port:
                fails.append(
                    f"{node}/{name}: rlink dials port {tr.get('endpoint', {}).get('port')}, "
                    f"expected the peer rlink-listener port {peer_rlink_port}"
                )

    # 4. a TLS rlink-listener transport with a cryptosign 'rlink' principal, plus a
    #    TLS client-facing transport.
    rlink_listeners = []
    client_facing = []
    for t in worker.get("transports", []):
        auth = t.get("auth") or t.get("rawsocket", {}).get("auth") or {}
        principals = (auth.get("cryptosign") or {}).get("principals", {})
        if "rlink" in principals:
            rlink_listeners.append(t)
        if t.get("type") == "universal":
            client_facing.append(t)

    if not rlink_listeners:
        fails.append(f"{node}: no rlink-listener transport with a cryptosign 'rlink' principal")
    for t in rlink_listeners:
        if not endpoint_is_tls(t):
            fails.append(f"{node}: the rlink-listener transport is not TLS")
        keys = t["auth"]["cryptosign"]["principals"]["rlink"].get("authorized_keys", [])
        if not keys:
            fails.append(f"{node}: the rlink-listener 'rlink' principal authorizes no peer key")

    if not client_facing:
        fails.append(f"{node}: no client-facing (universal) transport")
    for t in client_facing:
        if not endpoint_is_tls(t):
            fails.append(f"{node}: the client-facing transport is not TLS")

    return cfg


def rlink_listen_port(cfg):
    for t in cfg.get("workers", [])[0].get("transports", []):
        auth = t.get("auth") or {}
        if "rlink" in (auth.get("cryptosign") or {}).get("principals", {}):
            return t.get("endpoint", {}).get("port")
    return None


def main():
    ap = argparse.ArgumentParser(description="VERIFY-at-build for the crossbar-v2 two-node mesh target")
    ap.add_argument("--root", default=os.path.dirname(os.path.abspath(__file__)),
                    help="the two-node target directory (holds nodeA/ and nodeB/)")
    args = ap.parse_args()

    cfg_a, _ = load(args.root, "nodeA")
    cfg_b, _ = load(args.root, "nodeB")
    port_a = rlink_listen_port(cfg_a)
    port_b = rlink_listen_port(cfg_b)

    fails = []
    # nodeA's rlinks must dial nodeB's listener, and vice versa.
    check_node(args.root, "nodeA", port_b, fails)
    check_node(args.root, "nodeB", port_a, fails)

    if fails:
        print("FAIL: the two-node target is not the mesh it claims to be:", file=sys.stderr)
        for f in fails:
            print(f"  - {f}", file=sys.stderr)
        sys.exit(1)
    print("OK: two routers, each application realm forwarded both ways over a TLS "
          "WAMP-Cryptosign rlink; client-facing and link transports are TLS")


if __name__ == "__main__":
    main()
