# evaluator/parse_sonic.py
import re
import ipaddress
import json


def _vtysh_inner(command):
    """'sudo vtysh -c "show ip route 1.2.3.0/24"' -> 'show ip route 1.2.3.0/24'."""
    m = re.search(r'-c\s+"(.+?)"\s*$', command.strip())
    return m.group(1).strip() if m else command.strip()


def parse_frr_ip_route_detail(text):
    """Parse `show ip route <prefix|addr>` (FRR): one record per nexthop."""
    records, entry, got_nh = [], None, False

    def close():
        if entry is not None and not got_nh:
            records.append(dict(entry, nexthop_ip="", nexthop_if=""))

    for raw in text.splitlines():
        line = raw.strip()
        m = re.match(r'^Routing entry for (\S+)', line)
        if m:
            close()
            entry, got_nh = {"network": m.group(1), "protocol": "", "distance": ""}, False
            continue
        if entry is None:
            continue
        m = re.match(r'^Known via "([^"]+)", distance (\d+)', line)
        if m:
            entry["protocol"], entry["distance"] = m.group(1).lower(), m.group(2)
            continue
        m = re.match(r'^[>*qrbto\s]*(\d+\.\d+\.\d+\.\d+),\s*via\s+(\S+?)(?:,|$)', line)
        if m:
            records.append(dict(entry, nexthop_ip=m.group(1), nexthop_if=m.group(2)))
            got_nh = True
            continue
        m = re.match(r'^\*?\s*directly connected,\s*(\S+?)(?:,|$)', line)
        if m:
            records.append(dict(entry, nexthop_ip="", nexthop_if=m.group(1)))
            got_nh = True
    close()
    return records


def parse_frr_route_table(text):
    """Parse the full `show ip route` table. The code letter is kept as the
    protocol, so the evaluator's convergence check ('C'/'L' = local) still works."""
    routes = []
    for raw in text.splitlines():
        m = re.match(r'^([A-Z])[>*a-z\s]*\s*(\d+\.\d+\.\d+\.\d+/\d+)\b', raw.strip())
        if m:
            routes.append({"protocol": m.group(1), "network": m.group(2)})
    return routes


def parse_frr_running_routes(text):
    """Parse 'ip route <prefix> <nexthop> [distance]' lines from running-config."""
    out = []
    for raw in text.splitlines():
        p = raw.strip().split()
        if len(p) < 3 or p[0] != "ip" or p[1] != "route":
            continue
        try:
            net = str(ipaddress.IPv4Network(p[2], strict=False))
        except ValueError:
            continue
        rec = {"destination": net, "nexthop_ip": None, "distance": None}
        for tok in p[3:]:
            if rec["nexthop_ip"] is None and re.fullmatch(r'\d+\.\d+\.\d+\.\d+', tok):
                rec["nexthop_ip"] = tok
            elif tok.isdigit():
                rec["distance"] = tok
        out.append(rec)
    return out


def _json_blob(output):
    s, e = output.find("{"), output.rfind("}")
    if s == -1 or e <= s:
        return {}
    try:
        return json.loads(output[s:e + 1])
    except json.JSONDecodeError:
        return {}

def _area(a):
    a = str(a)
    if a.count(".") == 3 and a.startswith("0.0.0."):
        return a.split(".")[-1]
    return a

def parse_ospf_interface_json(output):        # "show ip ospf interface json"
    data = _json_blob(output)
    ifs = data.get("interfaces", data)         # wrapped in newer FRR
    rows = []
    for name, i in ifs.items():
        if not isinstance(i, dict) or i.get("ospfEnabled") is False or "area" not in i:
            continue
        rows.append({"interface": name, "area": _area(i["area"]),
                     "cost": i.get("cost"), "state": i.get("state"),
                     "ip_address": i.get("ipAddress")})
    return rows

def parse_ospf_neighbor_json(output):         # "show ip ospf neighbor json"
    data = _json_blob(output)
    nbrs = data.get("neighbors", data)
    rows = []
    for nid, entries in nbrs.items():
        for n in ([entries] if isinstance(entries, dict) else entries):
            rows.append({"neighbor_id": nid,
                         "state": str(n.get("nbrState") or n.get("state") or "").split("/")[0],
                         "ip_address": n.get("address") or n.get("ifaceAddress"),
                         "interface": str(n.get("ifaceName", "")).split(":")[0],
                         "priority": n.get("priority")})
    return rows

def parse_frr_bgp_neighbors_json(output):     # "show ip bgp neighbors json"
    data = _json_blob(output)
    rows = []
    for ip, info in data.items():
        if not isinstance(info, dict):
            continue
        rows.append({
            "neighbor": ip,
            "remote_as": str(info.get("remoteAs", "")),
            "bgp_state": info.get("bgpState", "")
        })
    return rows

def parse_frr_bgp_routes_json(output):        # "show ip bgp json"
    data = _json_blob(output)
    routes = data.get("routes", data)
    if not isinstance(routes, dict):
        return []
    return [{"network": prefix} for prefix in routes.keys()]

def parse_sonic_cli_output(command, output):
    inner = _vtysh_inner(command)
    if inner == "show ip ospf interface json":
        return parse_ospf_interface_json(output)
    if inner == "show ip ospf neighbor json":
        return parse_ospf_neighbor_json(output)
    if re.fullmatch(r'show ip route \S+', inner):
        return parse_frr_ip_route_detail(output)
    if inner == "show ip bgp neighbors json":
        return parse_frr_bgp_neighbors_json(output)
    if inner == "show ip bgp json":
        return parse_frr_bgp_routes_json(output)
    if inner == "show ip route":
        return parse_frr_route_table(output)
    if inner == "show running-config":
        return parse_frr_running_routes(output)
    # Unknown command: expose raw lines so `contains`-style checks still work
    return [{"line": l.strip()} for l in output.splitlines() if l.strip()]



# ---- overrides (later definitions win) --------------------------------------
def parse_frr_route_table(text):
    """`show ip route` -> Cisco-shaped records: bare network + prefix_length."""
    routes = []
    for raw in text.splitlines():
        m = re.match(r'^([A-Z])[>*a-z\s]*\s*(\d+\.\d+\.\d+\.\d+)/(\d+)\b', raw.strip())
        if m:
            routes.append({"protocol": m.group(1), "network": m.group(2),
                           "prefix_length": m.group(3)})
    return routes


def parse_frr_bgp_neighbors_json(output):
    data = _json_blob(output)
    rows = []
    for ip, info in data.items():
        if not isinstance(info, dict) or "bgpState" not in info:
            continue
        rows.append({
            "neighbor": ip,
            "remote_as": str(info.get("remoteAs", "")),
            "bgp_state": info.get("bgpState", ""),
            "localhost_ip": info.get("hostLocal", ""),
            "remote_ip": info.get("hostForeign", ip),
        })
    return rows


def parse_frr_bgp_routes_json(output):
    routes = _json_blob(output).get("routes")
    if not isinstance(routes, dict):
        return []
    return [{"network": prefix} for prefix in routes]


def parse_frr_bgp_summary_json(output):
    data = _json_blob(output)
    for afi in data.values():
        if isinstance(afi, dict) and "as" in afi:
            return [{"protocol": "bgp", "as_number": str(afi["as"]),
                     "local_as": str(afi["as"]), "router_id": afi.get("routerId", "")}]
    return []


def parse_frr_bgp_prefix_json(output):
    """`show ip bgp A.B.C.D/len json`; empty list when the prefix is absent."""
    data = _json_blob(output)
    paths = data.get("paths")
    if not paths:
        return []
    pfx = str(data.get("prefix", ""))
    if "/" not in pfx and data.get("prefixLen") is not None:
        pfx = f"{pfx}/{data['prefixLen']}"
    rows = []
    for p in paths:
        nhs = p.get("nexthops") or [{}]
        rows.append({"network": pfx, "next_hop": nhs[0].get("ip", ""),
                     "as_path": (p.get("aspath") or {}).get("string", ""),
                     "local_pref": str(p.get("locPrf", "")),
                     "origin": p.get("origin", "")})
    return rows


def parse_ping(output):
    m = re.search(r'(\d+(?:\.\d+)?)% packet loss', output)
    pct = int(round(100 - float(m.group(1)))) if m else 0
    return [{"success_pct": str(pct)}]


def parse_sonic_cli_output(command, output):
    inner = _vtysh_inner(command)
    if inner.startswith("ping "):
        return parse_ping(output)
    if inner == "show ip ospf interface json":
        return parse_ospf_interface_json(output)
    if inner == "show ip ospf neighbor json":
        return parse_ospf_neighbor_json(output)
    if inner == "show ip bgp summary json":
        return parse_frr_bgp_summary_json(output)
    if re.fullmatch(r'show ip bgp \d+\.\d+\.\d+\.\d+/\d+ json', inner):
        return parse_frr_bgp_prefix_json(output)
    if inner.startswith("show ip bgp neighbors") and inner.endswith("json"):
        return parse_frr_bgp_neighbors_json(output)
    if inner == "show ip bgp json":
        return parse_frr_bgp_routes_json(output)
    if re.fullmatch(r'show ip route \S+', inner):
        return parse_frr_ip_route_detail(output)
    if inner == "show ip route":
        return parse_frr_route_table(output)
    if inner == "show running-config":
        return parse_frr_running_routes(output)
    return [{"line": l.strip()} for l in output.splitlines() if l.strip()]
