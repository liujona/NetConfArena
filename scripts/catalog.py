import json, glob, re, os, collections
PH = re.compile(r'\{\{\s*([^}\s]+)\s*\}\}')
def norm(s):
    s = PH.sub('<P>', str(s).strip())
    s = re.sub(r'\b\d{1,3}(?:\.\d{1,3}){3}\b', '<IP>', s)
    s = re.sub(r'\b\d+\b', '<N>', s)
    return re.sub(r'\s+', ' ', s)
def keys(x, out):
    if isinstance(x, dict):
        for k, v in x.items(): out.add(k); keys(v, out)
    elif isinstance(x, list):
        for i in x: keys(i, out)
def lines(d, sec):
    for ls in (d.get(sec) or {}).values():
        if isinstance(ls, list):
            for l in ls: yield l
tasks = {}
for p in sorted(glob.glob("task/*/*.json")):
    try: d = json.load(open(p))
    except Exception: continue
    if isinstance(d, dict) and "groundtruth_configs" in d:
        txt = " ".join(lines(d, "startup_configs")) + " ".join(lines(d, "groundtruth_configs"))
        if "router bgp" in txt:
            tasks[os.path.basename(p)[:-5]] = d
out = []
w = out.append
w("== 1. TASKS (%d) ==" % len(tasks))
for n, d in tasks.items():
    txt = " ".join(lines(d, "groundtruth_configs"))
    feats = [k for k in ("ospf", "eigrp", "rip", "route-map", "prefix-list", "access-list", "community", "vrf", "mpls") if k in txt]
    nn = len(d.get("topology_model", {}).get("nodes", []))
    g = re.sub(r'\s+', ' ', str(d.get("goals", "")))[:260]
    w(f"{n[:42]} | nodes={nn} | {','.join(feats)}\n    GOALS: {g}")
w("\n== 2. CONFIG SHAPES (count = #tasks) ==")
for sec in ("startup_configs", "groundtruth_configs"):
    c = collections.defaultdict(set)
    for n, d in tasks.items():
        for l in lines(d, sec): c[norm(l)].add(n[:2])
    w("-- " + sec)
    for s, ns in sorted(c.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        w(f"{len(ns):3d}  {s}  [{','.join(sorted(ns))}]" if len(ns) <= 4 else f"{len(ns):3d}  {s}")
w("\n== 3. TESTCASE COMMANDS: shape | #tasks | tasks | expected_result key-sets | example ==")
c = collections.defaultdict(lambda: {"t": set(), "k": collections.Counter(), "ex": None, "cmd": None})
for n, d in tasks.items():
    for g in d.get("testcases", []):
        for t in g.get("testcase", []):
            r = c[norm(t.get("command", ""))]
            r["t"].add(n[:2]); r["cmd"] = r["cmd"] or t.get("command")
            ks = set(); keys(t.get("expected_result", {}), ks)
            r["k"][",".join(sorted(ks))] += 1
            r["ex"] = r["ex"] or json.dumps(t.get("expected_result"))[:260]
for s, r in sorted(c.items(), key=lambda kv: (-len(kv[1]["t"]), kv[0])):
    w(f"{len(r['t']):3d} | {s}\n      tasks: {','.join(sorted(r['t']))}\n      keysets: {dict(r['k'].most_common(4))}\n      raw cmd: {r['cmd']}\n      ex: {r['ex']}")
w("\n== 4. PARAMS THAT ARE MASK/LEN/WILDCARD BUT NOT subnet_attr ==")
for n, d in tasks.items():
    for k, v in d.get("params", {}).items():
        if re.search(r'(mask|wildcard|prefix_len|cidr|network)$', k) and v.get("type") != "subnet_attr":
            w(f"{n[:2]} {k}: {json.dumps(v)}")
open("/tmp/catalog.txt", "w").write("\n".join(out))
print("wrote /tmp/catalog.txt", len("\n".join(out)), "bytes")
