import json, glob, re, os, collections
PH = re.compile(r'\{\{\s*([^}\s]+)\s*\}\}')
def kind(n):
    n = n.lower()
    for suf, k in (("wildcard","WILD"),("mask","MASK"),("cidr","CIDR"),("prefix_len","LEN"),("network","NET"),("iface","IF"),("name","NAME")):
        if n.endswith(suf): return k
    if re.search(r'(^|_)ip\d?$', n): return "IP"
    if re.search(r'(^|_)as\d?$', n): return "AS"
    return "P"
def norm(s):
    s = PH.sub(lambda m: "<%s>" % kind(m.group(1)), str(s).strip())
    s = re.sub(r'\b\d{1,3}(?:\.\d{1,3}){3}\b', '<IPLIT>', s)
    s = re.sub(r'\b\d+\b', '<N>', s)
    return re.sub(r'\s+', ' ', s)
def keys(x, out):
    if isinstance(x, dict):
        for k, v in x.items(): out.add(k); keys(v, out)
    elif isinstance(x, list):
        for i in x: keys(i, out)
KW = {"bgp":"router bgp","ospf":"router ospf","eigrp":"router eigrp","rip":"router rip","isis":"router isis","static":"ip route","vrf":"vrf","mpls":"mpls","acl":"access-list","nat":"ip nat","hsrp":"standby","vrrp":"vrrp","tunnel":"tunnel","crypto":"crypto","vlan":"vlan","dhcp":"dhcp"}
def lines_of(d, sec):
    for ls in (d.get(sec) or {}).values():
        if isinstance(ls, list):
            for l in ls: yield l
tasks = {}
for p in sorted(glob.glob("task/*/*.json")):
    try: d = json.load(open(p))
    except Exception: continue
    if isinstance(d, dict) and "groundtruth_configs" in d:
        tasks[os.path.basename(p)[:-5]] = d
print("tasks found:", len(tasks))
bgp = []
print("\n== A. features per task ==")
for name, d in tasks.items():
    text = " ".join(l for s in ("startup_configs","groundtruth_configs") for l in lines_of(d, s)).lower()
    flags = [k for k, kw in KW.items() if kw in text]
    if "bgp" in flags: bgp.append(name)
    print(f"{name[:48]:48s} {','.join(flags)}")
print("\n== B. config shapes in BGP tasks (count = #tasks) ==")
for sec in ("startup_configs","groundtruth_configs"):
    c = collections.defaultdict(set)
    for name in bgp:
        for l in lines_of(tasks[name], sec): c[norm(l)].add(name)
    print("--", sec)
    for shape, ns in sorted(c.items(), key=lambda kv: (-len(kv[1]), kv[0])): print(f"{len(ns):3d}  {shape}")
print("\n== C. testcase commands, all tasks ==")
c = collections.defaultdict(set)
for name, d in tasks.items():
    for g in d.get("testcases", []):
        for t in g.get("testcase", []): c[norm(t.get("command",""))].add(name)
for shape, ns in sorted(c.items(), key=lambda kv: (-len(kv[1]), kv[0])): print(f"{len(ns):3d}  {shape}")
print("\n== D. expected_result keys in BGP tasks ==")
kc = collections.Counter()
for name in bgp:
    for g in tasks[name].get("testcases", []):
        for t in g.get("testcase", []):
            ks = set(); keys(t.get("expected_result", {}), ks); kc.update(ks)
print(", ".join(f"{k}({n})" for k, n in kc.most_common()))
