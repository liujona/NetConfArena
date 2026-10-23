import json
import os
import re
import random
import ipaddress
import yaml


TASK_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TASK_DIR)
ALL_TASKS_PATH = os.path.join(TASK_DIR, "all_tasks.json")
CAPABILITIES_PATH = os.path.join(TASK_DIR, "router_capabilities.yaml")


SONIC_NODE_TYPE = "SONiC-VS"
SONIC_MGMT_ADAPTER = 0      # eth0, management; never used for links
SONIC_ADAPTER_COUNT = 8     # the SONiC-VS template has 8 NICs

def _sonic_interface(adapter):
    """Adapter N (N>=1) -> EthernetX, X = (N-1)*4. Verified on the node:
    eth1->Ethernet0 and eth2->Ethernet4 are consistent with the output."""
    return f"Ethernet{(int(adapter) - 1) * 4}"



def derive_sonic_plan(topology_model, params, topology_attributes):
    """Physical plan for topologies where every node is SONiC-VS.
    Each link takes the next free NIC adapter (1..7) on each endpoint.
    Writes {prefix}_{link}_iface and {prefix}_{link}_port_num into params."""
    nodes, used = {}, {}
    for model_node in topology_model.get("nodes", []):
        nid = str(model_node["id"] if isinstance(model_node, dict) else model_node)
        if _node_type(topology_attributes, nid) != SONIC_NODE_TYPE:
            raise ValueError(f"mixed topologies are not supported yet: {nid} is not {SONIC_NODE_TYPE}")
        nodes[nid] = {"kind": "sonic", "slots": {}, "links": {}}
        used[nid] = {SONIC_MGMT_ADAPTER}

    for model_link in topology_model.get("links", []):
        link_id = str(model_link["id"])
        endpoints = [str(e) for e in (model_link.get("nodes") or [])]
        if len(endpoints) != 2:
            raise ValueError(f"link {link_id} must have exactly two endpoints")
        for nid in endpoints:
            adapter = next(a for a in range(1, SONIC_ADAPTER_COUNT) if a not in used[nid])
            used[nid].add(adapter)
            iface = _sonic_interface(adapter)
            prefix = _node_param_prefix(nid)
            params[f"{prefix}_{link_id}_port_num"] = "0"
            params[f"{prefix}_{link_id}_iface"] = iface
            nodes[nid]["links"][link_id] = {"slot": adapter, "interface": iface, "port_number": "0"}

    return {"nodes": nodes}

def _load_router_capabilities():
    with open(CAPABILITIES_PATH, "r") as f:
        return yaml.safe_load(f)


ROUTER_CAPABILITIES = _load_router_capabilities()


def _module_ports(module):
    spec = ROUTER_CAPABILITIES["modules"][module]
    if "ports" in spec:
        return spec["ports"]
    return [
        {
            "port_number": port,
            "media": spec["media"],
            "interface": spec["interface_template"]
        }
        for port in spec.get("port_numbers", [])
    ]


def _module_port_numbers(module):
    return [port["port_number"] for port in _module_ports(module)]


def _capability_module_port_numbers():
    return {
        module: _module_port_numbers(module)
        for module in ROUTER_CAPABILITIES["modules"]
    }


DEFAULT_PARAM_VALUES = {
    "default_module_options": ROUTER_CAPABILITIES["default_module_options"],
    "module_port_numbers": _capability_module_port_numbers()
}



def _pick_random_subnet(pool, prefix_len, excluded, rng, max_retries=200):
    """Pick a random /prefix_len subnet from pool that doesn't overlap excluded list."""
    subnet_size = 2 ** (32 - prefix_len)
    num_subnets = pool.num_addresses // subnet_size
    if num_subnets == 0:
        return None
    for _ in range(max_retries):
        idx = rng.randrange(num_subnets)
        base_int = int(pool.network_address) + idx * subnet_size
        candidate = ipaddress.IPv4Network((base_int, prefix_len), strict=True)
        if not any(candidate.overlaps(e) for e in excluded):
            return candidate
    return None


def _node_param_prefix(node_id):
    node = str(node_id)
    if node.startswith("r") and node[1:].isdigit():
        return f"router{node[1:]}"
    return node


def _interface_name(module, slot, port):
    port_num = int(port)
    for port_spec in _module_ports(module):
        if int(port_spec["port_number"]) == port_num:
            return port_spec["interface"].format(slot=int(slot), port=port_num)
    raise ValueError(f"module {module} does not have port {port}")


def _node_type(topology_attributes, node_id):
    return (
        topology_attributes
        .get("nodes", {})
        .get(str(node_id), {})
        .get("type", "C3600")
    )


def _is_switch_node(topology_attributes, node_id):
    return _node_type(topology_attributes, node_id) == "EthernetSwitch"


def _module_media(module):
    spec = ROUTER_CAPABILITIES["modules"].get(module, {})
    if "media" in spec:
        return spec["media"]
    medias = {port.get("media") for port in spec.get("ports", [])}
    return next(iter(medias)) if len(medias) == 1 else None


def _module_is_layer3(module):
    return bool(ROUTER_CAPABILITIES["modules"].get(module, {}).get("layer3", False))


def _supported_media():
    media = set()
    for module_spec in ROUTER_CAPABILITIES["modules"].values():
        if "ports" in module_spec:
            media.update(port.get("media") for port in module_spec["ports"])
        elif module_spec.get("media"):
            media.add(module_spec["media"])
    media.discard(None)
    return media


def _is_ethernet_module(module):
    return _module_media(module) in {"ethernet", "fastethernet", "gigabitethernet"}


def _normalize_media(media):
    media = str(media or "").lower().replace("-", "").replace("_", "")
    aliases = {
        "fe": "fastethernet",
        "fast": "fastethernet",
        "fastethernet": "fastethernet",
        "ethernet": "ethernet",
        "eth": "ethernet",
        "ge": "gigabitethernet",
        "gigabit": "gigabitethernet",
        "gigabitethernet": "gigabitethernet",
        "serial": "serial"
    }
    return aliases.get(media, media)


def _resolve_template_value(value, params):
    if isinstance(value, str):
        rendered = value
        for key, param_value in params.items():
            rendered = rendered.replace('{{' + key + '}}', str(param_value))
        return rendered
    return value


def _link_media(model_link, params):
    return _normalize_media(_resolve_template_value(model_link.get("media", "ethernet"), params))


def _uses_media_plan(topology_model, topology_attributes):
    if any("media" in link for link in topology_model.get("links", [])):
        return True
    return any(
        _node_type(topology_attributes, str(model_node["id"] if isinstance(model_node, dict) else model_node)) == "C7200"
        for model_node in topology_model.get("nodes", [])
    )


def _node_slot_start(topology_attributes, node_id):
    node_type = _node_type(topology_attributes, node_id)
    return int(ROUTER_CAPABILITIES["platforms"].get(node_type, {}).get("slot_start", 0))


def _module_for_media(topology_attributes, node_id, media, values, rng=None):
    node_type = _node_type(topology_attributes, node_id)
    platform = ROUTER_CAPABILITIES["platforms"].get(node_type, {})
    candidates = platform.get("media_modules", {}).get(media, [])
    if not candidates:
        raise ValueError(f"node {node_id} type {node_type} has no module for media {media}")
    return rng.choice(candidates) if rng and len(candidates) > 1 else candidates[0]


def derive_media_physical_plan(topology_model, params, values, topology_attributes=None, randomize=False, seed=None):
    """Infer a physical plan for topologies that need mixed link media."""
    rng = random.Random(f"{seed}:physical") if randomize else None
    topology_attributes = topology_attributes or {}
    module_ports = values["module_port_numbers"]
    physical_nodes = {}
    used_ports = {}
    next_slots = {}

    for model_node in topology_model.get("nodes", []):
        node_id = str(model_node["id"] if isinstance(model_node, dict) else model_node)
        if _node_type(topology_attributes, node_id) == SONIC_NODE_TYPE:
            physical_nodes[node_id] = {"kind": "sonic", "slots": {}, "links": {}}
            used_ports[node_id] = {"adapters": {SONIC_MGMT_ADAPTER}}
            continue
        if _is_switch_node(topology_attributes, node_id):
            physical_nodes[node_id] = {
                "kind": "switch",
                "links": {}
            }
            used_ports[node_id] = {"switch": set()}
            continue
        physical_nodes[node_id] = {
            "slots": {},
            "links": {}
        }
        used_ports[node_id] = {}
        next_slots[node_id] = _node_slot_start(topology_attributes, node_id)

    def allocate_router_endpoint(node_id, link_id, media):
        router_prefix = _node_param_prefix(node_id)
        module = _module_for_media(topology_attributes, node_id, media, values, rng=rng)
        chosen_slot = None
        chosen_port = None

        for slot_key, slot_module in physical_nodes[node_id]["slots"].items():
            if slot_module != module:
                continue
            free_ports = [
                str(port) for port in module_ports[module]
                if str(port) not in used_ports[node_id].setdefault(str(slot_key), set())
            ]
            if free_ports:
                chosen_slot = int(slot_key)
                chosen_port = rng.choice(free_ports) if rng else free_ports[0]
                break

        if chosen_slot is None:
            chosen_slot = next_slots[node_id]
            next_slots[node_id] += 1
            physical_nodes[node_id]["slots"][str(chosen_slot)] = module
            used_ports[node_id][str(chosen_slot)] = set()
            chosen_port = str(module_ports[module][0])

        used_ports[node_id][str(chosen_slot)].add(str(chosen_port))
        params[f"{router_prefix}_slot{chosen_slot}_module"] = module
        params[f"{router_prefix}_{link_id}_port_num"] = str(chosen_port)
        params[f"{router_prefix}_{link_id}_iface"] = _interface_name(module, chosen_slot, chosen_port)
        physical_nodes[node_id]["links"][link_id] = {
            "slot": chosen_slot,
            "interface": params[f"{router_prefix}_{link_id}_iface"],
            "port_number": str(chosen_port)
        }

    def allocate_switch_endpoint(node_id, link_id):
        used = used_ports[node_id]["switch"]
        available_ports = [str(port) for port in range(1, 33) if str(port) not in used]
        if not available_ports:
            raise ValueError(f"switch node {node_id} does not have enough generated ports")
        port_num = available_ports[0]
        used.add(port_num)
        physical_nodes[node_id]["links"][link_id] = {
            "interface": port_num,
            "port_number": port_num,
            "type": "access",
            "vlan": 1
        }

    def allocate_sonic_endpoint(node_id, link_id):
        used = used_ports[node_id]["adapters"]
        adapter = next(a for a in range(1, SONIC_ADAPTER_COUNT) if a not in used)
        used.add(adapter)
        iface = _sonic_interface(adapter)
        prefix = _node_param_prefix(node_id)
        params[f"{prefix}_{link_id}_port_num"] = "0"
        params[f"{prefix}_{link_id}_iface"] = iface
        physical_nodes[node_id]["links"][link_id] = {
            "slot": adapter, "interface": iface, "port_number": "0"}

    for model_link in topology_model.get("links", []):
        link_id = str(model_link["id"])
        endpoints = model_link.get("nodes") or [
            model_link.get("source"),
            model_link.get("destination")
        ]
        if len(endpoints) != 2 or endpoints[0] is None or endpoints[1] is None:
            raise ValueError(f"link {link_id} must have exactly two endpoints")

        media = _link_media(model_link, params)
        if media not in _supported_media():
            raise ValueError(f"link {link_id} has unsupported media {media}")

        for endpoint_node in endpoints:
            if _node_type(topology_attributes, node_id) == SONIC_NODE_TYPE:
                allocate_sonic_endpoint(node_id, link_id)
                continue
            node_id = str(endpoint_node)
            if _is_switch_node(topology_attributes, node_id):
                allocate_switch_endpoint(node_id, link_id)
            else:
                allocate_router_endpoint(node_id, link_id, media)

    return {"nodes": physical_nodes}


def derive_physical_plan(topology_model, params, values, topology_attributes=None, randomize=False, seed=None):
    """Infer a simple slot0 physical plan and expose port/interface params.

    This builder assumes each node uses slot0 for all links. A node's slot0
    module must provide enough ports for all incident links.
    """
    topology_attributes = topology_attributes or {}
    model_node_ids = [str(n["id"] if isinstance(n, dict) else n) for n in topology_model.get("nodes", [])]
    if any(_node_type(topology_attributes, nid) == SONIC_NODE_TYPE for nid in model_node_ids):
        return derive_sonic_plan(topology_model, params, topology_attributes)
    rng = random.Random(f"{seed}:physical") if randomize else None
    topology_attributes = topology_attributes or {}
    if _uses_media_plan(topology_model, topology_attributes):
        return derive_media_physical_plan(
            topology_model,
            params,
            values,
            topology_attributes=topology_attributes,
            randomize=randomize,
            seed=seed
        )
    module_ports = values["module_port_numbers"]
    module_options = [str(module) for module in values["default_module_options"]]
    physical_nodes = {}
    used_ports = {}
    node_ids = []

    for model_node in topology_model.get("nodes", []):
        node_id = str(model_node["id"] if isinstance(model_node, dict) else model_node)
        node_ids.append(node_id)
        if _is_switch_node(topology_attributes, node_id):
            physical_nodes[node_id] = {
                "kind": "switch",
                "links": {}
            }
            used_ports[node_id] = set()
            continue
        link_count = sum(
            1 for link in topology_model.get("links", [])
            if node_id in [str(endpoint) for endpoint in (link.get("nodes") or [])]
        )
        module_param = f"{_node_param_prefix(node_id)}_slot0_module"
        if module_param in params:
            module = params[module_param]
        else:
            available_modules = [
                module for module in module_options
                if len(module_ports[module]) >= link_count
            ]
            if not available_modules:
                raise ValueError(
                    f"node {node_id} has {link_count} links but no default module has enough ports"
                )
            module = None
        physical_nodes[node_id] = {
            "slots": {"0": module},
            "links": {}
        }
        used_ports[node_id] = set()

    # The default slot0 planner uses one shared module type per connected component
    # so every link endpoint is media-compatible.
    adjacency = {node_id: set() for node_id in node_ids}
    for model_link in topology_model.get("links", []):
        endpoints = [str(endpoint) for endpoint in (model_link.get("nodes") or [])]
        if len(endpoints) == 2:
            adjacency[endpoints[0]].add(endpoints[1])
            adjacency[endpoints[1]].add(endpoints[0])

    visited = set()
    for start in node_ids:
        if start in visited:
            continue
        stack = [start]
        component = []
        while stack:
            node_id = stack.pop()
            if node_id in visited:
                continue
            visited.add(node_id)
            component.append(node_id)
            stack.extend(adjacency[node_id] - visited)

        preset_modules = {
            physical_nodes[node_id]["slots"]["0"]
            for node_id in component
            if not _is_switch_node(topology_attributes, node_id)
            and physical_nodes[node_id]["slots"]["0"] is not None
        }
        if len(preset_modules) > 1:
            raise ValueError(f"component {component} has incompatible preset modules")
        if preset_modules:
            module = next(iter(preset_modules))
        else:
            max_links = max(
                sum(
                    1 for link in topology_model.get("links", [])
                    if node_id in [str(endpoint) for endpoint in (link.get("nodes") or [])]
                )
                for node_id in component
                if not _is_switch_node(topology_attributes, node_id)
            )
            available_modules = [
                candidate for candidate in module_options
                if len(module_ports[candidate]) >= max_links
            ]
            if any(_is_switch_node(topology_attributes, node_id) for node_id in component):
                available_modules = [
                    candidate for candidate in available_modules
                    if _is_ethernet_module(candidate)
                ]
            module = rng.choice(available_modules) if rng else available_modules[0]

        for node_id in component:
            if _is_switch_node(topology_attributes, node_id):
                continue
            physical_nodes[node_id]["slots"]["0"] = module
            params[f"{_node_param_prefix(node_id)}_slot0_module"] = module

    for model_link in topology_model.get("links", []):
        link_id = str(model_link["id"])
        endpoints = model_link.get("nodes") or [
            model_link.get("source"),
            model_link.get("destination")
        ]
        if len(endpoints) != 2 or endpoints[0] is None or endpoints[1] is None:
            raise ValueError(f"link {link_id} must have exactly two endpoints")

        endpoint_modules = []
        for endpoint_node in endpoints:
            node_id = str(endpoint_node)
            if _is_switch_node(topology_attributes, node_id):
                endpoint_modules.append("EthernetSwitch")
                continue
            router_prefix = _node_param_prefix(node_id)
            module = params[f"{router_prefix}_slot0_module"]
            endpoint_modules.append(module)

        if "EthernetSwitch" in endpoint_modules:
            router_modules = [
                module for module in endpoint_modules
                if module != "EthernetSwitch"
            ]
            if not all(_is_ethernet_module(module) for module in router_modules):
                raise ValueError(
                    f"link {link_id} connects a switch to non-Ethernet module(s): {endpoint_modules}"
                )
        elif endpoint_modules[0] != endpoint_modules[1]:
            raise ValueError(
                f"link {link_id} has incompatible modules: "
                f"{endpoints[0]}={endpoint_modules[0]}, {endpoints[1]}={endpoint_modules[1]}"
            )

        for endpoint_node in endpoints:
            node_id = str(endpoint_node)
            if _is_switch_node(topology_attributes, node_id):
                available_ports = [
                    str(port) for port in range(1, 33)
                    if str(port) not in used_ports[node_id]
                ]
                port_num = available_ports[0]
                used_ports[node_id].add(port_num)
                physical_nodes[node_id]["links"][link_id] = {
                    "interface": port_num,
                    "port_number": port_num,
                    "type": "access",
                    "vlan": 1
                }
                continue

            router_prefix = _node_param_prefix(node_id)
            module = params[f"{router_prefix}_slot0_module"]
            available_ports = [
                str(port) for port in module_ports[module]
                if str(port) not in used_ports[node_id]
            ]
            if not available_ports:
                raise ValueError(
                    f"node {node_id} slot0 module {module} does not have enough ports"
                )

            port_num = rng.choice(available_ports) if rng else available_ports[0]
            used_ports[node_id].add(port_num)

            port_param = f"{router_prefix}_{link_id}_port_num"
            iface_param = f"{router_prefix}_{link_id}_iface"
            params[port_param] = port_num
            params[iface_param] = _interface_name(module, 0, port_num)
            physical_nodes[node_id]["links"][link_id] = {
                "slot": 0,
                "interface": params[iface_param],
                "port_number": port_num
            }

    return {"nodes": physical_nodes}


def generate_params(param_schema, values=None, overrides=None, randomize=False, seed=None):
    """Resolve a param schema into concrete string values.

    Each schema entry can be:
      - A plain string/number: treated as a fixed default
      - A dict with "type":
          "fixed"         → always use "default"
          "choice"        → pick from "options" (list or YAML key string);
                            "different_from": [param_names] to avoid collisions
          "random_subnet" → random IPv4 subnet; "pool" (CIDR string),
                            "prefix_len" (int, list, or YAML key string);
                            "no_overlap": [private_param_names]
          "subnet_attr"   → derive from a resolved subnet param;
                            "subnet": param_name, "attr": network|netmask|prefixlen|host,
                            "index": 1-based host index (only for attr=host)
          "interface_name"→ derive IOS interface name from module, slot, and port
          "format"        → format string using resolved params;
                            "template": "Serial0/{port_num}" (uses {param} syntax)

    Private params (names starting with _) are resolved but excluded from the returned dict.

    Args:
        param_schema: dict from the "params" section of the task JSON
        values: dict loaded from params.yaml, used to resolve string references in specs
        overrides: dict of explicit param overrides (highest priority, non-private only)
        randomize: if True, generate random values; otherwise use defaults
        seed: random seed for reproducibility (only used when randomize=True)
    Returns:
        dict of non-private param_name → string value
    """
    rng = random.Random(seed) if randomize else None
    overrides = dict(overrides) if overrides else {}
    values = values or {}
    resolved = {}

    pending = list(param_schema.items())
    max_passes = len(pending) + 2

    for _ in range(max_passes):
        if not pending:
            break
        still_pending = []

        for name, spec in pending:
            if name in overrides and not name.startswith("_"):
                resolved[name] = str(overrides[name])
                continue

            if not isinstance(spec, dict):
                spec = {"type": "fixed", "default": spec}

            param_type = spec.get("type", "fixed")

            if param_type == "fixed":
                if "from" in spec:
                    resolved[name] = str(values[spec["from"]])
                else:
                    resolved[name] = str(spec["default"])

            elif param_type == "choice":
                deps = spec.get("different_from", [])
                if not all(d in resolved for d in deps):
                    still_pending.append((name, spec))
                    continue
                excluded_vals = {resolved[d] for d in deps if d in resolved}
                raw_options = spec["options"]
                if isinstance(raw_options, str):
                    raw_options = values[raw_options]
                options = [str(o) for o in raw_options]
                available = [o for o in options if o not in excluded_vals] or options
                if randomize and rng:
                    resolved[name] = rng.choice(available)
                else:
                    default = str(spec.get("default", available[0]))
                    resolved[name] = default

            elif param_type == "random_subnet":
                no_overlap_deps = spec.get("no_overlap", [])
                if not all(d in resolved for d in no_overlap_deps):
                    still_pending.append((name, spec))
                    continue
                pool_spec = spec["pool"]
                if isinstance(pool_spec, str) and pool_spec in values:
                    pool_spec = values[pool_spec]
                pool = ipaddress.IPv4Network(pool_spec, strict=False)
                prefix_len_spec = spec["prefix_len"]
                if isinstance(prefix_len_spec, str):
                    prefix_len_spec = values[prefix_len_spec]
                if isinstance(prefix_len_spec, list):
                    prefix_len = rng.choice(prefix_len_spec) if (randomize and rng) else prefix_len_spec[0]
                else:
                    prefix_len = prefix_len_spec
                excluded_nets = [
                    ipaddress.IPv4Network(resolved[d], strict=False)
                    for d in no_overlap_deps if d in resolved
                ]
                if randomize and rng:
                    chosen = _pick_random_subnet(pool, prefix_len, excluded_nets, rng)
                    resolved[name] = str(chosen) if chosen else str(spec["default"])
                else:
                    resolved[name] = str(spec["default"])

            elif param_type == "subnet_attr":
                subnet_dep = spec["subnet"]
                if subnet_dep not in resolved:
                    still_pending.append((name, spec))
                    continue
                net = ipaddress.IPv4Network(resolved[subnet_dep], strict=False)
                attr = spec["attr"]
                if attr == "network":
                    resolved[name] = str(net.network_address)
                elif attr == "netmask":
                    resolved[name] = str(net.netmask)
                elif attr == "prefixlen":
                    resolved[name] = str(net.prefixlen)
                elif attr == "host":
                    idx = spec.get("index", 1)
                    resolved[name] = str(list(net.hosts())[idx - 1])
                elif attr == "wildcard":
                    resolved[name] = str(net.hostmask)
                else:
                    resolved[name] = str(spec.get("default", ""))

            elif param_type == "lookup":
                key_param = spec["key"]
                if key_param not in resolved:
                    still_pending.append((name, spec))
                    continue
                table = values[spec["table"]]
                resolved[name] = str(table[resolved[key_param]])

            elif param_type == "lookup_choice":
                key_param = spec["key"]
                deps = spec.get("different_from", [])
                if key_param not in resolved or not all(d in resolved for d in deps):
                    still_pending.append((name, spec))
                    continue
                table = values[spec["table"]]
                options = [str(o) for o in table[resolved[key_param]]]
                excluded_vals = {resolved[d] for d in deps if d in resolved}
                available = [o for o in options if o not in excluded_vals] or options
                if randomize and rng:
                    resolved[name] = rng.choice(available)
                else:
                    resolved[name] = str(spec.get("default", available[0]))

            elif param_type == "subnet_nth":
                subnet_dep = spec["subnet"]
                if subnet_dep not in resolved:
                    still_pending.append((name, spec))
                    continue
                net = ipaddress.IPv4Network(resolved[subnet_dep], strict=False)
                new_prefix = spec["new_prefix"]
                n = spec.get("n", 0)
                subnets = list(net.subnets(new_prefix=new_prefix))
                sub = subnets[n]
                attr = spec.get("attr", "network")
                if attr == "network":
                    resolved[name] = str(sub.network_address)
                elif attr == "netmask":
                    resolved[name] = str(sub.netmask)
                elif attr == "prefixlen":
                    resolved[name] = str(sub.prefixlen)
                elif attr == "host":
                    host_idx = spec.get("index", 1)
                    resolved[name] = str(list(sub.hosts())[host_idx - 1])

            elif param_type == "same_as":
                src = spec["param"]
                if src not in resolved:
                    still_pending.append((name, spec))
                    continue
                resolved[name] = resolved[src]

            elif param_type == "interface_name":
                module_param = spec["module"]
                port_param = spec["port"]
                slot_spec = spec.get("slot", 0)

                deps = [module_param, port_param]
                if isinstance(slot_spec, str) and not slot_spec.isdigit():
                    deps.append(slot_spec)
                if not all(d in resolved for d in deps):
                    still_pending.append((name, spec))
                    continue

                module = resolved[module_param]
                slot = resolved[slot_spec] if isinstance(slot_spec, str) and not slot_spec.isdigit() else slot_spec
                port = resolved[port_param]
                resolved[name] = _interface_name(module, slot, port)

            elif param_type == "format":
                template = spec["template"]
                deps = re.findall(r'\{(\w+)\}', template)
                if not all(d in resolved for d in deps):
                    still_pending.append((name, spec))
                    continue
                resolved[name] = template.format(**{d: resolved[d] for d in deps})

            else:
                resolved[name] = str(spec.get("default", ""))

        pending = still_pending

    return {k: v for k, v in resolved.items() if not k.startswith("_")}


def _slot_number(slot_key):
    slot = str(slot_key)
    if slot.startswith("slot"):
        slot = slot[4:]
    return int(slot)


def build_topology(topology_model, topology_attributes, physical_plan):
    """Build create_topology-compatible topology from graph model + attributes + physical plan."""
    plan_nodes = physical_plan.get("nodes", {})
    attr_nodes = topology_attributes.get("nodes", {})
    topology_nodes = []
    endpoint_port_ids = {}
    used_ports = set()
    next_port_id = 1

    for model_node in topology_model.get("nodes", []):
        node_id = str(model_node["id"] if isinstance(model_node, dict) else model_node)
        if node_id not in plan_nodes:
            raise ValueError(f"physical_plan missing node {node_id}")
        if node_id not in attr_nodes:
            raise ValueError(f"topology_attributes missing node {node_id}")

        node_plan = plan_nodes[node_id]
        node_attrs = attr_nodes[node_id]
        is_switch = node_attrs["type"] == "EthernetSwitch"
        slots = node_plan.get("slots", {})
        properties = {}
        for slot_key, slot_spec in slots.items():
            slot_num = _slot_number(slot_key)
            if isinstance(slot_spec, dict):
                module = slot_spec["module"]
            else:
                module = slot_spec
            properties[f"slot{slot_num}"] = module

        ports = []
        for link_id, endpoint in node_plan.get("links", {}).items():
            port_number = endpoint["port_number"]
            slot_num = 0 if is_switch else _slot_number(endpoint["slot"])
            port_key = (node_id, slot_num, str(port_number))
            if port_key in used_ports:
                raise ValueError(
                    f"duplicate port on node {node_id}: slot{slot_num}/{port_number}"
                )
            used_ports.add(port_key)

            port_id = next_port_id
            next_port_id += 1
            endpoint_port_ids[(node_id, str(link_id))] = port_id
            port_data = {
                "id": port_id,
                "name": endpoint["interface"],
                "port_number": port_number
            }
            if is_switch:
                port_data["type"] = endpoint.get("type", "access")
                port_data["vlan"] = endpoint.get("vlan", 1)
            else:
                port_data["slot_number"] = slot_num
            ports.append(port_data)

        label = node_attrs.get("label", {})
        if isinstance(label, str):
            label = {"text": label}

        topology_nodes.append({
            "id": node_id,
            "type": node_attrs["type"],
            "label": label,
            "x": node_attrs.get("x", 0),
            "y": node_attrs.get("y", 0),
            "properties": properties,
            "ports": ports
        })

    topology_links = []
    for model_link in topology_model.get("links", []):
        link_id = str(model_link["id"])
        endpoints = model_link.get("nodes") or [
            model_link.get("source"),
            model_link.get("destination")
        ]
        if len(endpoints) != 2 or endpoints[0] is None or endpoints[1] is None:
            raise ValueError(f"link {link_id} must have exactly two endpoints")

        source_node_id = str(endpoints[0])
        destination_node_id = str(endpoints[1])
        source_port_id = endpoint_port_ids.get((source_node_id, link_id))
        destination_port_id = endpoint_port_ids.get((destination_node_id, link_id))
        if source_port_id is None or destination_port_id is None:
            raise ValueError(f"physical_plan missing endpoint for link {link_id}")

        source_module = _endpoint_module(plan_nodes, source_node_id, link_id)
        destination_module = _endpoint_module(plan_nodes, destination_node_id, link_id)
        if "EthernetSwitch" in {source_module, destination_module}:
            router_modules = [
                module for module in (source_module, destination_module)
                if module != "EthernetSwitch"
            ]
            if not all(_is_ethernet_module(module) for module in router_modules):
                raise ValueError(
                    f"link {link_id} connects a switch to non-Ethernet module(s): "
                    f"{source_node_id}={source_module}, {destination_node_id}={destination_module}"
                )
        elif source_module != destination_module:
            raise ValueError(
                f"link {link_id} has incompatible modules: "
                f"{source_node_id}={source_module}, {destination_node_id}={destination_module}"
            )

        topology_links.append({
            "id": model_link["id"],
            "source_node_id": endpoints[0],
            "source_port_id": source_port_id,
            "destination_node_id": endpoints[1],
            "destination_port_id": destination_port_id
        })

    topology = {
        "nodes": topology_nodes,
        "links": topology_links
    }

    for key in ("notes", "ellipses", "rectangles"):
        if key in topology_attributes:
            topology[key] = topology_attributes[key]

    return topology


def _endpoint_module(plan_nodes, node_id, link_id):
    node_plan = plan_nodes[node_id]
    if node_plan.get("kind") == "switch":
        return "EthernetSwitch"
    if node_plan.get("kind") == "sonic":
        return "SONiC-ADAPTER"
    endpoint = node_plan.get("links", {}).get(link_id)
    if endpoint is None:
        raise ValueError(f"physical_plan missing endpoint {node_id}/{link_id}")
    slot_num = _slot_number(endpoint["slot"])
    slot_spec = node_plan.get("slots", {}).get(str(slot_num))
    if slot_spec is None:
        slot_spec = node_plan.get("slots", {}).get(f"slot{slot_num}")
    if slot_spec is None:
        raise ValueError(f"physical_plan missing slot{slot_num} for node {node_id}")
    if isinstance(slot_spec, dict):
        return slot_spec["module"]
    return slot_spec


def render_task_file(task_file_path, overrides=None, randomize=False, seed=None):
    """Render a task template file without requiring registration in all_tasks.json.

    Args:
        task_file_path: absolute path or path relative to the repository root
        overrides: dict of param name → value (highest priority)
        randomize: if True, randomly generate param values per schema
        seed: optional random seed for reproducibility
    Returns:
        rendered task_data dict; 'params' section removed, 'resolved_params' added
    """
    if not os.path.isabs(task_file_path):
        task_file_path = os.path.join(REPO_ROOT, task_file_path)
    template_str = open(task_file_path, 'r').read()
    template_data = json.loads(template_str)

    task_dir = os.path.dirname(task_file_path)
    values_path = os.path.join(task_dir, 'params.yaml')
    values = dict(DEFAULT_PARAM_VALUES)
    if os.path.exists(values_path):
        with open(values_path, 'r') as f:
            values.update(yaml.safe_load(f) or {})

    param_schema = template_data.get('params', {})
    params = generate_params(param_schema, values=values, overrides=overrides, randomize=randomize, seed=seed)
    physical_plan = derive_physical_plan(
        template_data.get('topology_model', {}),
        params,
        values,
        topology_attributes=template_data.get('topology_attributes', {}),
        randomize=randomize,
        seed=seed
    )
    public_params = {
        key: params[key]
        for key in param_schema
        if not key.startswith('_') and key in params
    }

    for key, value in params.items():
        template_str = template_str.replace('{{' + key + '}}', str(value))

    task_data = json.loads(template_str)
    task_data.pop('params', None)
    task_data['topology'] = build_topology(
        task_data.pop('topology_model'),
        task_data.pop('topology_attributes'),
        physical_plan
    )
    task_data['resolved_params'] = public_params
    return task_data


def render_task(task_index, overrides=None, randomize=False, seed=None):
    """Load a registered task by index and render its placeholders."""
    # Paths in all_tasks.json are recorded relative to the repository root, so
    # resolve them against the repository root rather than the caller's cwd.
    with open(ALL_TASKS_PATH, 'r') as f:
        task_file_path = json.load(f)[task_index]
    return render_task_file(
        task_file_path,
        overrides=overrides,
        randomize=randomize,
        seed=seed,
    )
