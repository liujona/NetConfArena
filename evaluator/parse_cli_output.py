import re
import ipaddress
import json
from anytree import Node
from ntc_templates.parse import parse_output

from environment.device_profile import IS_SONIC
from .parse_sonic import parse_sonic_cli_output

def build_anytree_from_indent(text, indent_size=1):
    lines = [line.rstrip() for line in text.strip().splitlines()]
    root = Node("ROOT")  # Root node.
    stack = [(0, root)]  # Entries are (depth, node).

    for line in lines:
        if not line.strip():
            continue

        stripped = line.lstrip()
        indent_level = (len(line) - len(stripped)) // indent_size
        current_node = Node(stripped)

        # Return to the appropriate parent without emptying the stack.
        while stack and stack[-1][0] >= indent_level:
            stack.pop()

        # Fall back to root if the stack becomes empty.
        if not stack:
            stack.append((0, root))

        parent_node = stack[-1][1]
        current_node.parent = parent_node
        stack.append((indent_level, current_node))

    return root

def find_node_by_name(root, keyword, exact=True):
    for node in root.descendants:
        name = node.name.strip()
        if (exact and name == keyword) or (not exact and keyword in name):
            return node
    return None

def find_all_nodes_by_name(root, keyword, exact=True):
    nodes = []
    for node in root.descendants:
        name = node.name.strip()
        if (exact and name == keyword) or (not exact and keyword in name):
            nodes.append(node)
    return nodes

def add_default_mask_if_missing(prefix: str) -> str:
    """Add the classful default mask to an IPv4 prefix without one."""
    if '/' in prefix:
        return prefix  # A mask is already present.

    ip = ipaddress.IPv4Address(prefix)
    first_octet = int(str(ip).split('.')[0])

    if 1 <= first_octet <= 126:
        return f"{prefix}/8"
    elif 128 <= first_octet <= 191:
        return f"{prefix}/16"
    elif 192 <= first_octet <= 223:
        return f"{prefix}/24"
    else:
        # Class D multicast or another address class.
        return f"{prefix}/32"

def parse_config_ip_route(output):
    result = []
    for line in output.splitlines():
        parts = line.strip().split()
        if len(parts) < 4 or parts[0] != "ip" or parts[1] != "route":
            continue  # Skip invalid lines.

        destination_ip = parts[2]
        subnet_mask = parts[3]
        nexthop_ip = parts[4] if len(parts) > 4 else None

        # Use ipaddress to calculate the CIDR representation.
        network = ipaddress.IPv4Network((destination_ip, subnet_mask), strict=False)
        route = {
            "destination": str(network),
            "nexthop_ip": nexthop_ip
        }

        for part in parts[5:]:
            if part.lower() == "permanent":
                route["type"] = "permanent"
            if part.isdigit():
                route["distance"] = part

        result.append(route)
    
    return result

def parse_ip_route_detail(text):
    results = []
    lines = text.strip().split('\n')
    
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        
        # Match the start of a route entry.
        route_match = re.match(r'^Routing entry for (\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}/\d{1,2})(?:, supernet)?', line)
        if route_match:
            current_record = {
                'network': route_match.group(1),
                'protocol': '',
                'distance': '',
                'metric': '',
                'nexthop_ip': '',
                'nexthop_if': '',
                'learned_from': '',
                'type': '',
                'description': ''
            }
            
            # Continue until protocol information is found.
            i += 1
            while i < len(lines):
                line = lines[i].rstrip()
                
                # Match protocol, administrative distance, and metric.
                protocol_match = re.match(r'^\s+Known via "(.+)", distance (\d+), metric (\d+)(?:, (.+))?', line)
                if protocol_match:
                    current_record['protocol'] = protocol_match.group(1).split()[0].lower()  # Protocol only.
                    current_record['distance'] = protocol_match.group(2)
                    current_record['metric'] = protocol_match.group(3)
                    if protocol_match.group(4):
                        current_record['description'] = protocol_match.group(4)
                    i += 1
                    break
                i += 1
            
            # Continue to next-hop information.
            record_added = False
            while i < len(lines):
                line = lines[i].rstrip()
                
                # Skip the "Routing Descriptor Blocks:" heading.
                if re.match(r'^\s+Routing Descriptor Blocks:', line):
                    i += 1
                    continue
                
                # First next-hop format, including an interface. learned_from
                # is IOS's "from <peer>" source before recursive resolution and
                # can differ from nexthop_ip.
                nexthop_match1 = re.match(r'^\s+\* (\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})(?:, from (\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}), \S+ ago,)? via (\S+)', line)
                if nexthop_match1:
                    current_record['nexthop_ip'] = nexthop_match1.group(1)
                    if nexthop_match1.group(2):
                        current_record['learned_from'] = nexthop_match1.group(2)
                    current_record['nexthop_if'] = nexthop_match1.group(3)
                    i += 1
                    continue

                # Second next-hop format, optionally with from/permanent and no interface.
                nexthop_match2 = re.match(r'^\s+\* (\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})(?:, from (\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}))?(?:, \S+ ago)?(?:, (permanent))?', line)
                if nexthop_match2:
                    current_record['nexthop_ip'] = nexthop_match2.group(1)
                    if nexthop_match2.group(2):
                        current_record['learned_from'] = nexthop_match2.group(2)
                    if nexthop_match2.group(3):
                        current_record['type'] = nexthop_match2.group(3)
                    i += 1
                    continue
                
                # Match directly connected interface information.
                direct_match = re.match(r'^\s+\* directly connected, via (\S+)', line)
                if direct_match:
                    current_record['nexthop_ip'] = direct_match.group(1)
                    i += 1
                    continue
                
                # Match metric details and finish the record.
                metric_match = re.match(r'^\s+Route metric is (\d+), traffic share count is \d+', line)
                if metric_match:
                    current_record['metric'] = metric_match.group(1)
                    results.append(current_record)
                    record_added = True
                    i += 1
                    break
                
                # Stop when the next route entry begins.
                if re.match(r'^Routing entry for', line):
                    if not record_added:
                        results.append(current_record)
                    break
                
                i += 1
            
            # At EOF, append the current record if it has not been added.
            if i >= len(lines) and not record_added and current_record['network']:
                results.append(current_record)
        else:
            i += 1
    
    return results

def parse_ip_protocols_rip(output):
    result = []
    
    tree_root = build_anytree_from_indent(output)
    proto_nodes = find_all_nodes_by_name(tree_root, 'Routing Protocol is', exact=False)
    
    if not proto_nodes:
        return result
    
    for proto_node in proto_nodes:
        # Process only RIP.
        match = re.search(r'Routing Protocol is "(.*?)"', proto_node.name)
        if not match or not match.group(1).split()[0].lower() == "rip":
            continue
            
        protocol = {
            "incoming_update_filter": [],
            "outgoing_update_filter": [],
            "incoming_metric": [],
            "outgoing_metric": [],
            "redistribute": [],
            "neighbors": [],
            "auto_summary": None,
            "address_summarization": [],
            "networks": [],
            "passive_interfaces": [],
            "information_sources": [],
            "distance": None,
            "distance_overrides": []
        }

        # outgoing update filter
        out_filter_node = find_node_by_name(proto_node, "Outgoing update filter list", exact=False)
        if out_filter_node:
            if "not set" not in out_filter_node.name:
                protocol["outgoing_update_filter"].append(out_filter_node.name.strip())
            else:
                for child in out_filter_node.children:
                    protocol["outgoing_update_filter"].append(child.name.strip())

        # incoming update filter
        in_filter_node = find_node_by_name(proto_node, "Incoming update filter list", exact=False)
        if in_filter_node:
            if "not set" not in in_filter_node.name:
                protocol["incoming_update_filter"].append(in_filter_node.name)
            else:
                for child in in_filter_node.children:
                     protocol["incoming_update_filter"].append(child.name.strip())

        # incoming metric
        in_metric_node = find_node_by_name(proto_node, "Incoming routes", exact=False)
        if in_metric_node:
            match1 = re.search(r'Incoming routes will have (\d+) added to metric if on list (\d+)', in_metric_node.name)
            if match1:
                protocol["incoming_metric"].append(in_metric_node.name.strip())
            match2 = re.search(r'Incoming routes in (\S+) will have (\d+) added to metric if on list (\d+)', in_metric_node.name)
            if match2:
                protocol["incoming_metric"].append(in_metric_node.name.strip())

        # outgoing metric
        out_metric_node = find_node_by_name(proto_node, "Outgoing routes", exact=False)
        if out_metric_node:
            match1 = re.search(r'Outgoing routes will have (\d+) added to metric if on list (\d+)', out_metric_node.name)
            if match1:
                protocol["outgoing_metric"].append(out_metric_node.name.strip())
            match2 = re.search(r'Outgoing routes in (\S+) will have (\d+) added to metric if on list (\d+)', out_metric_node.name)
            if match2:
                protocol["outgoing_metric"].append(out_metric_node.name.strip())
        
        # redistribute
        redist_node = find_node_by_name(proto_node, "Redistributing", exact=False)
        if redist_node:
            # Extract redistribution information from the node name.
            redist_text = redist_node.name.strip()
            if "Redistributing:" in redist_text:
                # Extract content after "Redistributing:".
                redist_content = redist_text.split("Redistributing:", 1)[1].strip()
                if redist_content:
                    # Split multiple protocols on commas.
                    protocols = [p.strip() for p in redist_content.split(',')]
                    for proto in protocols:
                        if proto:
                            protocol["redistribute"].append(proto.split()[0].lower())

        # neighbors
        neighbors_node = find_node_by_name(proto_node, "Neighbor(s):", exact=False)
        if neighbors_node:
            for child in neighbors_node.children:
                protocol["neighbors"].append(child.name.strip())

        # auto_summary
        auto_summary_node = find_node_by_name(proto_node, "Automatic network summarization", exact=False)
        if auto_summary_node:
            protocol["auto_summary"] = not ("not in effect" in auto_summary_node.name)

        # Group address_summarization by interface.
        address_summ_node = find_node_by_name(proto_node, "Address Summarization:", exact=False)
        if address_summ_node:
            for child in address_summ_node.children:
                protocol["address_summarization"].append(child.name.strip())
        
        # networks
        network_node = find_node_by_name(proto_node, "Routing for Networks:", exact=False)
        if network_node:
            for child in network_node.children:
                protocol["networks"].append(child.name.strip())

        # passive interfaces
        passive_node = find_node_by_name(proto_node, "Passive Interface(s):", exact=False)
        if passive_node:
            for child in passive_node.children:
                protocol["passive_interfaces"].append(child.name.strip())

        # neighbors
        neighbor_node = find_node_by_name(proto_node, "Routing Information Sources:", exact=False)
        if neighbor_node:
            for child in neighbor_node.children:
                protocol["information_sources"].append(child.name.strip())

        # distance
        distance_node = find_node_by_name(proto_node, "Distance:", exact=False)
        if distance_node:
            match = re.search(r'\(default is (\d+)\)', distance_node.name)
            if match:
                protocol["distance"] = int(match.group(1))

            for child in distance_node.children:
                    protocol["distance_overrides"].append(child.name.strip())

        result.append(protocol)

    return result

def parse_ip_protocols_ospf(output):
    result = []
    
    tree_root = build_anytree_from_indent(output)
    proto_nodes = find_all_nodes_by_name(tree_root, 'Routing Protocol is', exact=False)
    
    if not proto_nodes:
        return result
    
    for proto_node in proto_nodes:
        # Process only OSPF.
        match = re.search(r'Routing Protocol is "(.*?)"', proto_node.name)
        if not match or not match.group(1).split()[0].lower().startswith("ospf"):
            continue
            
        protocol = {
            "router-id": None,
            "incoming_update_filter": [],
            "outgoing_update_filter": [],
            "redistribute": [],
            "reference_bandwidth": None,
            "distance": None,
            "distance_overrides": [],
        }
        # outgoing update filter
        out_filter_node = find_node_by_name(proto_node, "Outgoing update filter list", exact=False)
        if out_filter_node:
            if "not set" not in out_filter_node.name:
                protocol["outgoing_update_filter"].append(out_filter_node.name.strip())
            else:
                for child in out_filter_node.children:
                    protocol["outgoing_update_filter"].append(child.name.strip())

        # incoming update filter
        in_filter_node = find_node_by_name(proto_node, "Incoming update filter list", exact=False)
        if in_filter_node:
            if "not set" not in in_filter_node.name:
                protocol["incoming_update_filter"].append(in_filter_node.name)
            else:
                for child in in_filter_node.children:
                     protocol["incoming_update_filter"].append(child.name.strip())
        
        # router-id
        router_id_node = find_node_by_name(proto_node, "Router ID", exact=False)
        if router_id_node:
            protocol["router_id"] = router_id_node.name.split("Router ID")[1].strip()

        # redistribute
        redist_node = find_node_by_name(proto_node, "Redistributing", exact=False)
        if redist_node:
            for child in redist_node.children:
                protocol["redistribute"].append(child.name.strip())

        # reference bandwidth
        ref_bw_node = find_node_by_name(proto_node, "Reference bandwidth", exact=False)
        if ref_bw_node:
            m = re.search(r'Reference bandwidth unit is\s+(\d+)\s*mbps', ref_bw_node.name, re.I)
            if m:
                protocol["reference_bandwidth"] = int(m.group(1))

        result.append(protocol)

    return result

def parse_ip_protocols_bgp(output):
    result = []
    
    tree_root = build_anytree_from_indent(output)
    proto_nodes = find_all_nodes_by_name(tree_root, 'Routing Protocol is', exact=False)
    
    if not proto_nodes:
        return result
    
    for proto_node in proto_nodes:
        # Process only BGP.
        match = re.search(r'Routing Protocol is "(.*?)"', proto_node.name)
        if not match or not match.group(1).split()[0].lower() == "bgp":
            continue
            
        protocol = {
            "protocol": "bgp",
            "as_number": "",
            "igp_sync": None,
            "aggregate_generation": [],
            "maximum_path": None,
            "maximum_paths_ibgp": None,
            "maximum_paths_ebgp": None,
            "routing_info_sources": [],
            "redistribute": [],
            "incoming_update_filter": [],
            "outgoing_update_filter": [],
            "incoming_filter_names": [],
            "outgoing_filter_names": [],
            "dmzlink_bw": False
        }

        # as_number
        if match:
            protocol["as_number"] = match.group(1).split()[1]

        # outgoing update filter
        out_filter_node = find_node_by_name(proto_node, "Outgoing update filter list", exact=False)
        if out_filter_node:
            if "not set" not in out_filter_node.name:
                match = re.search(r'is\s+(\S+)', out_filter_node.name)
                if match:
                    filter_name = match.group(1)
                    protocol["outgoing_update_filter"].append({
                        "interface": "ALL",
                        "filter_type": "access-list",
                        "filter_name": filter_name
                    })
                    protocol["outgoing_filter_names"].append(filter_name)
            else:
                for child in out_filter_node.children:
                    match = re.search(r'(\S+)\s+filtered by \((\S+)\)\s+(\S+)', child.name)
                    if match:
                        filter_name = match.group(3)
                        protocol["outgoing_update_filter"].append({
                            "interface": match.group(1),
                            "filter_type": match.group(2),
                            "filter_name": filter_name
                        })
                        protocol["outgoing_filter_names"].append(filter_name)

        # incoming update filter
        in_filter_node = find_node_by_name(proto_node, "Incoming update filter list", exact=False)
        if in_filter_node:
            if "not set" not in in_filter_node.name:
                match = re.search(r'is\s+(?:\((\S+)\)\s+)?(\S+)', in_filter_node.name)
                if match:
                    filter_type = match.group(1) if match.group(1) else "access-list"
                    filter_name = match.group(2)
                    protocol["incoming_update_filter"].append({
                        "interface": "ALL",
                        "filter_type": filter_type,
                        "filter_name": filter_name
                    })
                    protocol["incoming_filter_names"].append(filter_name)
            else:
                for child in in_filter_node.children:
                    match = re.search(r'(\S+)\s+filtered by \((\S+)\)\s+(\S+)', child.name)
                    if match:
                        filter_name = match.group(3)
                        protocol["incoming_update_filter"].append({
                            "interface": match.group(1),
                            "filter_type": match.group(2),
                            "filter_name": filter_name
                        })
                        protocol["incoming_filter_names"].append(filter_name)
        
        # redistribute
        redist_node = find_node_by_name(proto_node, "Redistributing", exact=False)
        if redist_node:
            redist_text = redist_node.name
            if redist_node.children:
                redist_text += " " + " ".join(child.name for child in redist_node.children)

            for m in re.finditer(r'\b([a-zA-Z0-9_]+)\b', redist_text):
                proto_name = m.group(1).lower()
                if proto_name in ("rip", "ospf", "eigrp", "bgp", "connected", "static"):
                    protocol["redistribute"].append({
                        "protocol": proto_name,
                        "includes_subnets": "subnets" in redist_text.lower()
                    })

        # IGP synchronization
        sync_node = find_node_by_name(proto_node, "IGP synchronization", exact=False)
        if sync_node:
            protocol["igp_sync"] = not ("disabled" in sync_node.name.lower())

        # Unicast Aggregate Generation
        agg_node = find_node_by_name(proto_node, "Unicast Aggregate Generation:", exact=False)
        if agg_node:
            for c in agg_node.children:
                m = re.match(r'(\d+\.\d+\.\d+\.\d+\/\d+)\s+(\S+)', c.name.strip())
                if m:
                    protocol["aggregate_generation"].append(
                        {"prefix": m.group(1), "flags": m.group(2)}
                    )

        # Maximum path. IOS variants can expose plain, EBGP, and IBGP limits differently.
        for maxp in find_all_nodes_by_name(proto_node, "Maximum path", exact=False):
            text = maxp.name
            simple = re.search(r'Maximum path(?:s)?:\s+(\d+)', text, re.I)
            if simple:
                protocol["maximum_path"] = int(simple.group(1))

            for afi, value in re.findall(r'\b(e?i?bgp)\s+(\d+)\b', text, re.I):
                key = "maximum_paths_ibgp" if afi.lower() == "ibgp" else "maximum_paths_ebgp"
                protocol[key] = int(value)

            ibgp = re.search(r'Maximum path(?:s)?.*?iBGP[^0-9]*(\d+)', text, re.I)
            if ibgp:
                protocol["maximum_paths_ibgp"] = int(ibgp.group(1))
            ebgp = re.search(r'Maximum path(?:s)?.*?eBGP[^0-9]*(\d+)', text, re.I)
            if ebgp:
                protocol["maximum_paths_ebgp"] = int(ebgp.group(1))

        # BGP DMZ link bandwidth is not covered by ntc-templates. Keep this
        # deliberately broad because IOS trains format the line differently.
        for node in proto_node.descendants:
            text = node.name.lower()
            if "dmz" in text and ("bw" in text or "bandwidth" in text) and "not" not in text:
                protocol["dmzlink_bw"] = True

        # Routing Information Sources
        ris = find_node_by_name(proto_node, "Routing Information Sources:", exact=False)
        if ris:
            for c in ris.children:
                if re.search(r'Gateway\s+Distance', c.name):
                    continue  # Heading line.
                m = re.match(r'\s*(\S+)\s+(\d+)\s+([\d:]+)', c.name)
                if m:
                    protocol["routing_info_sources"].append(
                        {"gateway": m.group(1),
                         "distance": int(m.group(2)),
                         "last_update": m.group(3)}
                    )

        # BGP Distance（external/internal/local）
        dist_node = find_node_by_name(proto_node, "Distance: external", exact=False)
        if dist_node:
            m = re.search(r'external\s+(\d+)\s+internal\s+(\d+)\s+local\s+(\d+)', dist_node.name)
            if m:
                protocol["distance_external"] = int(m.group(1))
                protocol["distance_internal"] = int(m.group(2))
                protocol["distance_local"]    = int(m.group(3))

        result.append(protocol)

    return result

def parse_route_map(output):
    result = parse_output(platform="cisco_ios", command="show route-map", data=output)
    for route_map in result:
        route_map["set_clauses"] = [
            normalize_community_text(clause)
            for clause in route_map.get("set_clauses", [])
        ]

        filter_names = []
        filter_types = []
        for clause in route_map.get("match_clauses", []):
            clause_lower = clause.lower()
            if "ip address" not in clause_lower:
                continue

            filter_type = "prefix-list" if "prefix-list" in clause_lower else "access-list"
            _, _, names_part = clause.partition(":")
            if not names_part:
                continue
            for name in re.split(r"[\s,]+", names_part.strip()):
                if not name:
                    continue
                filter_names.append(name)
                filter_types.append(filter_type)

        route_map["match_ip_filter_names"] = filter_names
        route_map["match_ip_filter_types"] = filter_types

    return result

def parse_ip_bgp_neighbors(output):
    result = []
    neighbor = None

    def new_neighbor(ip=None, remote_as=None):
        return {
            "neighbor": ip,
            "neighbor_ip": ip,
            "remote_as": remote_as,
            "bgp_state": None,
            "localhost_ip": None,
            "localhost_port": None,
            "remote_ip": None,
            "remote_port": None,
            "inbound_soft_reconfiguration": False,
            "outbound_soft_reconfiguration": False,
            "incoming_filter": None,
            "incoming_filter_type": None,
            "outgoing_filter": None,
            "outgoing_filter_type": None,
            "condition_map": None,
            "advertise_map": None,
            "non_exist_map": None,
            "conditional_advertise_status": None,
            "send_community": False,
            "send_community_standard": False,
            "send_community_extended": False,
            "dmzlink_bw": False
        }

    def finish_neighbor():
        if neighbor and neighbor.get("neighbor_ip"):
            result.append(neighbor)

    def filter_value(text):
        # Handles IOS lines such as:
        #   Incoming update filter list is 101
        #   Incoming update prefix filter list is PL-IN
        #   Incoming update filter list is (access-list) 101
        m = re.search(r'is\s+(?:\(([^)]+)\)\s+)?(\S+)', text, re.I)
        if not m:
            return None, None
        filter_type = m.group(1)
        filter_name = m.group(2)
        if filter_name.lower() in ("not", "set"):
            return None, None
        return filter_name, filter_type

    lines = output.strip().split('\n')

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        # Match neighbor sections from `show ip bgp neighbors` and the single-neighbor variant.
        neighbor_match = re.match(r'^BGP neighbor is (\d+\.\d+\.\d+\.\d+),\s+(?:vrf \S+,\s+)?remote AS (\d+)', line)
        if neighbor_match:
            finish_neighbor()
            neighbor = new_neighbor(neighbor_match.group(1), neighbor_match.group(2))
            continue

        if neighbor is None:
            continue

        if "Inbound soft reconfiguration allowed" in line:
            neighbor["inbound_soft_reconfiguration"] = True
        elif "Outbound soft reconfiguration allowed" in line:
            neighbor["outbound_soft_reconfiguration"] = True

        state_match = re.search(r'BGP state\s*=\s*(\S+)', line, re.I)
        if state_match:
            neighbor["bgp_state"] = state_match.group(1).rstrip(",")

        local_match = re.search(r'Local host:\s+(\d+\.\d+\.\d+\.\d+),\s+Local port:\s+(\d+)', line, re.I)
        if local_match:
            neighbor["localhost_ip"] = local_match.group(1)
            neighbor["localhost_port"] = local_match.group(2)

        remote_match = re.search(r'(?:Foreign|Remote) host:\s+(\d+\.\d+\.\d+\.\d+),\s+(?:Foreign|Remote) port:\s+(\d+)', line, re.I)
        if remote_match:
            neighbor["remote_ip"] = remote_match.group(1)
            neighbor["remote_port"] = remote_match.group(2)

        incoming_filter_match = re.search(r'Incoming update (?:prefix )?filter list is .+', line, re.I)
        if incoming_filter_match:
            name, filter_type = filter_value(line)
            if name:
                neighbor["incoming_filter"] = name
                neighbor["incoming_filter_type"] = filter_type or ("prefix-list" if "prefix" in line.lower() else "access-list")

        outgoing_filter_match = re.search(r'Outgoing update (?:prefix )?filter list is .+', line, re.I)
        if outgoing_filter_match:
            name, filter_type = filter_value(line)
            if name:
                neighbor["outgoing_filter"] = name
                neighbor["outgoing_filter_type"] = filter_type or ("prefix-list" if "prefix" in line.lower() else "access-list")

        route_map_match = re.search(r'Route map for (incoming|outgoing) advertisements is (\S+)', line, re.I)
        if route_map_match:
            direction = route_map_match.group(1).lower()
            key = "inbound_routemap" if direction == "incoming" else "outbound_routemap"
            neighbor[key] = route_map_match.group(2)

        # IOS examples vary between "Community attribute sent ... (extended)" and
        # "Send community attribute ...". Expose both coarse and extended flags.
        community_line = line.lower()
        if "community" in community_line and "sent" in community_line and "not" not in community_line:
            neighbor["send_community"] = True
            if "extended" in community_line or "both" in community_line:
                neighbor["send_community_extended"] = True
            if "standard" in community_line or "both" in community_line or "extended" not in community_line:
                neighbor["send_community_standard"] = True

        # DMZ link bandwidth is a BGP-neighbor operational attribute, but IOS
        # formats the line inconsistently across versions. Match conservatively.
        dmz_line = line.lower()
        if "dmz" in dmz_line and ("bw" in dmz_line or "bandwidth" in dmz_line) and "not" not in dmz_line:
            neighbor["dmzlink_bw"] = True

        conditional_match = re.search(
            r'(Condition|Non-exist)-map\s+([^,\s]+),\s+Advertise-map\s+([^,\s]+)(?:,\s+status:\s+(\S+))?',
            line,
            re.I
        )
        if conditional_match:
            condition_type = conditional_match.group(1).lower()
            if condition_type == "condition":
                neighbor["condition_map"] = conditional_match.group(2)
            else:
                neighbor["non_exist_map"] = conditional_match.group(2)
            neighbor["advertise_map"] = conditional_match.group(3)
            if conditional_match.group(4):
                neighbor["conditional_advertise_status"] = conditional_match.group(4).rstrip(",")

    finish_neighbor()
    return result

def parse_ip_bgp_neighbors_merged(output):
    custom_result = parse_ip_bgp_neighbors(output)
    custom_by_neighbor = {
        item.get("neighbor") or item.get("neighbor_ip"): item
        for item in custom_result
    }

    try:
        ntc_result = parse_output(platform="cisco_ios", command="show ip bgp neighbors", data=output)
    except Exception:
        return custom_result

    for item in ntc_result:
        neighbor_ip = item.get("neighbor") or item.get("neighbor_ip")
        custom_item = custom_by_neighbor.get(neighbor_ip)
        if not custom_item:
            continue

        for key in (
            "inbound_soft_reconfiguration",
            "outbound_soft_reconfiguration",
            "incoming_filter",
            "incoming_filter_type",
            "outgoing_filter",
            "outgoing_filter_type",
            "condition_map",
            "advertise_map",
            "non_exist_map",
            "conditional_advertise_status",
            "send_community",
            "send_community_standard",
            "send_community_extended",
            "dmzlink_bw",
        ):
            if key in custom_item:
                item[key] = custom_item[key]

    return ntc_result

def normalize_community_token(token):
    if not re.fullmatch(r'\d+', token):
        return token

    value = int(token)
    # IOS displays AA:NN communities as decimal unless new-format is enabled.
    if value <= 65535:
        return token
    return f"{value // 65536}:{value % 65536}"

def normalize_community_text(text):
    return " ".join(normalize_community_token(token) for token in text.split())

def parse_ip_community_list(output):
    result = []
    current_type = ""
    current_name = ""

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        header = re.search(
            r'^(?:IP\s+)?(?:Named\s+)?(?:Community|community)\s+'
            r'(?:(standard|expanded)\s+)?list\s+(\S+)',
            line,
            re.I
        )
        if header:
            current_type = (header.group(1) or "standard").lower()
            current_name = header.group(2)
            remainder = line[header.end():].strip()
        else:
            inline_header = re.search(
                r'^(?:(standard|expanded)\s+community-list\s+(\S+)|'
                r'(?:ip\s+)?community-list\s+(?:(standard|expanded)\s+)?(\S+))',
                line,
                re.I
            )
            if inline_header:
                current_type = (inline_header.group(1) or inline_header.group(3) or "standard").lower()
                current_name = inline_header.group(2) or inline_header.group(4)
                remainder = line[inline_header.end():].strip()
            else:
                remainder = line

        entry = re.search(r'\b(permit|deny)\b\s+(.+)$', remainder, re.I)
        if not entry:
            continue

        community = entry.group(2).strip()
        community = re.sub(r'\s+\(\d+\s+matches?\)$', '', community, flags=re.I)
        communities = [normalize_community_token(token) for token in community.split()]
        community = " ".join(communities)
        result.append({
            "community_type": current_type or "standard",
            "list_number": current_name,
            "list_name": current_name,
            "action": entry.group(1).lower(),
            "community": community,
            "communities": communities
        })

    return result

def parse_ip_bgp_prefix_detail(output):
    if re.search(r'not\s+in\s+table|Network not in table|% Network', output, re.I):
        return []

    entries = []
    current = None
    network = ""
    in_update_groups = False

    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue

        indent = len(line) - len(line.lstrip())
        table_match = re.search(r'BGP routing table entry for\s+(\S+)', stripped, re.I)
        if table_match:
            network = table_match.group(1).rstrip(',')
            continue

        if re.match(r'^Advertised to update-groups:', stripped, re.I):
            in_update_groups = True
            continue
        if in_update_groups and indent > 2:
            continue
        in_update_groups = False

        path_match = re.match(r'^\s*(?:\d+(?:\s+\d+)*|\([^)]*\)|local)\s*$', line, re.I)
        if path_match:
            if current:
                entries.append(current)
            current = {
                "network": network,
                "as_path": stripped,
                "communities": []
            }
            continue

        if current is None:
            # Some IOS outputs omit a standalone AS-path line for local paths.
            if re.search(r'\bfrom\s+\d+\.\d+\.\d+\.\d+\b|\bOrigin\b', stripped, re.I):
                current = {
                    "network": network,
                    "as_path": "",
                    "communities": []
                }
            else:
                continue

        next_hop_match = re.match(r'^\s*(\d+\.\d+\.\d+\.\d+)\s+from\s+(\d+\.\d+\.\d+\.\d+)', line)
        if next_hop_match:
            current["next_hop"] = next_hop_match.group(1)
            current["from"] = next_hop_match.group(2)

        community_match = re.search(r'\bCommunity:\s*(.+)$', stripped, re.I)
        if community_match:
            communities = [
                normalize_community_token(token)
                for token in community_match.group(1).split()
            ]
            current["communities"].extend(communities)
            current["community"] = " ".join(current["communities"])

    if current:
        entries.append(current)

    return entries

def parse_ip_ospf_area(output):
    result = []
    
    lines = output.strip().split('\n')
    current_area = {}
    
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        
        # Match the start of an area section.
        area_match = re.match(r'Area\s+(BACKBONE\((\d+)\)|(\d+))', line)
        if area_match:
            # Save the previous area first.
            if current_area:
                result.append(current_area)
            
            # Initialize the new area.
            current_area = {
                "area_id": "",
                "type": "",
                "nssa_translation": None,
                "ranges": []
            }
            
            # Parse area ID and type.
            if area_match.group(1).startswith("BACKBONE"):
                current_area["area_id"] = area_match.group(2)
                current_area["type"] = "backbone"
            else:
                current_area["area_id"] = area_match.group(3)
                current_area["type"] = "normal"
            
            i += 1
            continue
        
        # Detect an NSSA.
        if "It is a NSSA area" in line:
            current_area["type"] = "nssa"
            # Find NSSA translation information.
            j = i + 1
            while j < len(lines) and not re.match(r'Area\s+', lines[j]):
                if "Perform type-7/type-5 LSA translation" in lines[j]:
                    current_area["nssa_translation"] = True
                    break
                elif "No translation" in lines[j]:
                    current_area["nssa_translation"] = False
                    break
                j += 1
        
        # Detect a stub area.
        elif "It is a stub area" in line:
            current_area["type"] = "stub"
        
        # Parse area ranges.
        elif "Area ranges are" in line:
            j = i + 1
            while j < len(lines):
                range_line = lines[j].strip()
                # Stop at the next area or other non-range information.
                if (re.match(r'Area\s+', range_line) or 
                    range_line.startswith("Number of") or

                    range_line.startswith("SPF algorithm") or
                    not range_line):
                    break
                
                # Parse range formats such as "10.0.0.0/16 Advertise".
                range_match = re.match(r'(\d+\.\d+\.\d+\.\d+/\d+)\s+(\w+)', range_line)
                if range_match:
                    current_area["ranges"].append({
                        "prefix": range_match.group(1),
                        "advertise": range_match.group(2).lower() == "advertise"
                    })
                j += 1
            i = j - 1
        
        i += 1
    
    # Append the final area.
    if current_area:
        result.append(current_area)
    
    return result

def parse_ip_ospf_virtual_links(output):
    result =  []

    # Every virtual-link entry starts with "Virtual Link".
    pattern = r'Virtual Link (\S+) to router (\d+\.\d+\.\d+\.\d+) is (\w+)'
    matches = re.findall(pattern, output)

    for _, router_id, state in matches:
        result.append({
            "router_id": router_id,
            "state": state
        })

    return result

def parse_ip_ospf_database_summary(output: str):
    result = []
    
    lines = output.strip().split('\n')
    current_lsa = {}
    current_area = None
    
    for line in lines:
        line = line.strip()
        
        # Skip blank and heading lines while retaining area information.
        if not line or "OSPF Router with ID" in line:
            continue
            
        # Parse area information.
        area_match = re.search(r'Summary Net Link States \(Area (\d+)\)', line)
        if area_match:
            current_area = area_match.group(1)
            continue
            
        # Detect the start of a new LSA entry.
        if line.startswith("Routing Bit Set on this LSA") or line.startswith("LS age:"):
            # Save any current LSA first.
            if current_lsa and 'link_state_id' in current_lsa:
                result.append(current_lsa)
            current_lsa = {}
            if current_area is not None:
                current_lsa['area'] = current_area
            
        # Parse the Link State ID.
        if line.startswith("Link State ID:"):
            id_match = re.search(r'Link State ID:\s*(\d+\.\d+\.\d+\.\d+)', line)
            if id_match:
                current_lsa['link_state_id'] = id_match.group(1)
                
        # Parse the network mask.
        elif line.startswith("Network Mask:"):
            mask_match = re.search(r'Network Mask:\s*(/\d+)', line)
            if mask_match:
                current_lsa['network_mask'] = mask_match.group(1)
    
    # Append the final LSA, if present.
    if current_lsa and 'link_state_id' in current_lsa:
        result.append(current_lsa)
    
    return result

def parse_ip_ospf_database_external(output: str):
    result = []
    
    lines = output.strip().split('\n')
    current_lsa = {}
    
    for line in lines:
        line = line.strip()
        
        # Skip blank and heading lines.
        if not line or "OSPF Router with ID" in line or "Type-5 AS External Link States" in line:
            continue
            
        # Detect the start of a new LSA entry.
        if line.startswith("Routing Bit Set on this LSA") or line.startswith("LS age:"):
            # Save any current LSA first.
            if current_lsa and 'link_state_id' in current_lsa:
                result.append(current_lsa)
            current_lsa = {}
            # External routes have no specific area; omit it or use None.
            
        # Parse the Link State ID.
        if line.startswith("Link State ID:"):
            id_match = re.search(r'Link State ID:\s*(\d+\.\d+\.\d+\.\d+)', line)
            if id_match:
                current_lsa['link_state_id'] = id_match.group(1)
                
        # Parse the network mask.
        elif line.startswith("Network Mask:"):
            mask_match = re.search(r'Network Mask:\s*(/\d+)', line)
            if mask_match:
                current_lsa['network_mask'] = mask_match.group(1)
                
        # Parse the forwarding address.
        elif line.startswith("Forward Address:"):
            forward_match = re.search(r'Forward Address:\s*(\d+\.\d+\.\d+\.\d+)', line)
            if forward_match:
                current_lsa['forward_address'] = forward_match.group(1)
    
    # Append the final LSA, if present.
    if current_lsa and 'link_state_id' in current_lsa:
        result.append(current_lsa)
    
    return result

def parse_mpls_ldp_neighbor(output):
    """Parse `show mpls ldp neighbor`.

    Each LDP session produces one record:

        {"peer_ldp_ident": "2.2.2.2:0", "local_ldp_ident": "1.1.1.1:0",
         "peer_id": "2.2.2.2", "state": "Oper", "downstream": "Downstream",
         "discovery_sources": ["FastEthernet1/0"], "targeted": "No",
         "peer_addresses": ["192.168.12.2", "2.2.2.2"]}

    `discovery_sources` and `peer_addresses` are string lists, so tests can
    assert them directly with the list-subset semantics of `contains_all`.
    """
    result = []
    current = None
    section = None

    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        peer_match = re.match(
            r'^Peer LDP Ident:\s*(\S+?);\s*Local LDP Ident\s*(\S+?)\s*$',
            stripped
        )
        if peer_match:
            if current:
                result.append(current)
            peer_ident = peer_match.group(1)
            local_ident = peer_match.group(2)
            current = {
                "peer_ldp_ident": peer_ident,
                "peer_id": peer_ident.split(':')[0],
                "local_ldp_ident": local_ident,
                "local_id": local_ident.split(':')[0],
                "state": "",
                "downstream": "",
                "targeted": "No",
                "discovery_sources": [],
                "peer_addresses": []
            }
            section = None
            continue

        if current is None:
            continue

        state_match = re.match(r'^State:\s*(\w+)', stripped)
        if state_match:
            current["state"] = state_match.group(1)
            if "Downstream" in stripped:
                current["downstream"] = "Downstream"
            section = None
            continue

        if stripped.startswith("TCP connection:"):
            tcp_match = re.match(
                r'^TCP connection:\s*(\d+\.\d+\.\d+\.\d+)\.\d+\s*-\s*(\d+\.\d+\.\d+\.\d+)\.\d+',
                stripped
            )
            if tcp_match:
                current["remote_transport_ip"] = tcp_match.group(1)
                current["local_transport_ip"] = tcp_match.group(2)
            section = None
            continue

        if stripped.startswith("Up time:"):
            current["up_time"] = stripped.split(':', 1)[1].strip()
            section = None
            continue

        if stripped.startswith("Password:"):
            current["password"] = stripped.split(':', 1)[1].strip()
            section = None
            continue

        if stripped.startswith("LDP discovery sources:"):
            section = "discovery"
            continue

        if stripped.startswith("Addresses bound to peer LDP Ident:") or \
                stripped.startswith("Addresses bound to this peer:"):
            section = "addresses"
            continue

        if section == "discovery":
            if not stripped:
                section = None
                continue
            if stripped.lower().startswith("targeted hello"):
                current["targeted"] = "Yes"
                current["discovery_sources"].append("Targeted Hello")
                continue
            iface_match = re.match(r'^([A-Za-z][\w./-]*)(?:,|$)', stripped)
            if iface_match:
                current["discovery_sources"].append(iface_match.group(1))
                src_match = re.search(r'Src IP addr:\s*(\d+\.\d+\.\d+\.\d+)', stripped)
                if src_match:
                    current.setdefault("discovery_src_ips", []).append(src_match.group(1))
            continue

        if section == "addresses":
            if not stripped:
                section = None
                continue
            addresses = re.findall(r'\d+\.\d+\.\d+\.\d+', stripped)
            if addresses:
                current["peer_addresses"].extend(addresses)
            else:
                section = None
            continue

    if current:
        result.append(current)

    return result


def parse_mpls_ldp_bindings(output):
    """Parse `show mpls ldp bindings` and `show mpls ip binding`.

    Each prefix produces one record:

        {"prefix": "1.1.1.1/32", "local_label": "16",
         "remote_peers": ["2.2.2.2:0"], "remote_labels": ["17"]}

    IOS 12.x releases vary between `tib entry`/`lib entry`, `tag:`/`label:`,
    and `tsr:`/`lsr:`. This parser accepts all variants.
    """
    result = []
    current = None

    for raw_line in output.splitlines():
        stripped = raw_line.strip()

        entry_match = re.match(
            r'^(?:tib|lib) entry:\s*(\d+\.\d+\.\d+\.\d+/\d+)',
            stripped,
            flags=re.IGNORECASE
        )
        if entry_match:
            if current:
                result.append(current)
            current = {
                "prefix": entry_match.group(1),
                "local_label": "",
                "remote_peers": [],
                "remote_labels": []
            }
            continue

        if current is None:
            continue

        local_match = re.match(
            r'^local binding:\s*(?:tag|label):\s*(\S+?)\s*$',
            stripped,
            flags=re.IGNORECASE
        )
        if local_match:
            current["local_label"] = local_match.group(1)
            continue

        remote_match = re.match(
            r'^remote binding:\s*(?:tsr|lsr):\s*(\S+?),\s*(?:tag|label):\s*(\S+?)\s*$',
            stripped,
            flags=re.IGNORECASE
        )
        if remote_match:
            current["remote_peers"].append(remote_match.group(1))
            current["remote_labels"].append(remote_match.group(2))
            continue

    if current:
        result.append(current)

    return result


def parse_mpls_forwarding_table(output):
    """Parse `show mpls forwarding-table [<prefix> <mask>]`.

    Each LFIB entry produces:

        {"local_label": "16", "outgoing_label": "Pop tag", "prefix": "2.2.2.2/32",
         "interface": "Fa1/0", "next_hop": "192.168.12.2"}

    The evaluator normalizes `interface`, making IOS abbreviations such as
    `Fa1/0` equivalent to `FastEthernet1/0`. Continuation lines for additional
    next hops inherit the previous entry's local_label and prefix.
    """
    result = []
    last = None

    label_token = r'(Pop tag|Pop Label|No tag|No Label|Untagged|Aggregate|explicit-n\S*|\d+(?:/\d+)?)'
    entry_re = re.compile(
        r'^(\d+)\s+' + label_token +
        r'\s+(\S+(?:\s+\S+)??)\s+(\d+)\s+(?:\[\S+\]\s+)?([A-Za-z][\w./-]*)?\s*(\S+)?\s*$'
    )
    continuation_re = re.compile(
        r'^\s+' + label_token +
        r'\s+(\S+)\s+(\d+)\s+([A-Za-z][\w./-]*)?\s*(\S+)?\s*$'
    )

    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if lowered.startswith(("local", "tag ", "label ", "outgoing")) or \
                lowered.startswith("prefix") or lowered.startswith("bytes"):
            continue

        entry_match = entry_re.match(stripped)
        if entry_match and not line.startswith(" "):
            prefix = entry_match.group(3).strip()
            prefix_flag = ""
            flag_match = re.match(r'^(\S+?)\[(\w+)\]$', prefix)
            if flag_match:
                prefix, prefix_flag = flag_match.group(1), flag_match.group(2)
            record = {
                "local_label": entry_match.group(1),
                "outgoing_label": entry_match.group(2),
                "prefix": prefix,
                "prefix_flag": prefix_flag,
                "bytes_switched": entry_match.group(4),
                "interface": entry_match.group(5) or "",
                "next_hop": entry_match.group(6) or ""
            }
            result.append(record)
            last = record
            continue

        continuation_match = continuation_re.match(line)
        if continuation_match and last is not None:
            result.append({
                "local_label": last["local_label"],
                "outgoing_label": continuation_match.group(1),
                "prefix": last["prefix"],
                "prefix_flag": last["prefix_flag"],
                "bytes_switched": continuation_match.group(3),
                "interface": continuation_match.group(4) or "",
                "next_hop": continuation_match.group(5) or ""
            })

    return result


def parse_mpls_te_tunnels_brief(output):
    """Parse `show mpls traffic-eng tunnels brief`.

    Each tunnel produces one record:

        {"tunnel_name": "R1_t1", "destination": "4.4.4.4", "up_if": "-",
         "down_if": "Fa1/0", "state": "up", "prot": "up"}

    Headend, midpoint, and tail rows share the same format. A same-name record
    on an intermediate node can therefore prove that the LSP traverses it.
    """
    result = []
    for raw_line in output.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.upper().startswith("TUNNEL NAME"):
            continue
        match = re.match(
            r'^(\S+)\s+(\d+\.\d+\.\d+\.\d+)\s+(\S+)\s+(\S+)\s+(\S+?)/(\S+)\s*$',
            stripped
        )
        if match:
            result.append({
                "tunnel_name": match.group(1),
                "destination": match.group(2),
                "up_if": match.group(3),
                "down_if": match.group(4),
                "state": match.group(5),
                "prot": match.group(6)
            })
    return result


def parse_xconnect_all(output):
    """Parse `show xconnect all`.

    Each cross-connect produces one record:

        {"xc_state": "UP", "segment1": "ac Fa1/0(Ethernet)", "s1_state": "UP",
         "segment2": "mpls 2.2.2.2:100", "s2_state": "UP"}
    """
    result = []
    for raw_line in output.splitlines():
        stripped = raw_line.strip()
        if not stripped or set(stripped) <= set("-+ "):
            continue
        if stripped.upper().startswith("XC ST"):
            continue
        if stripped.startswith("Legend:") or stripped.startswith("Local"):
            continue
        match = re.match(
            r'^(\S{2})\s+(\S.*?)\s+(UP|DN|AD|IA|SB|RV|NH|--)\s+(\S.*?)\s+(UP|DN|AD|IA|SB|RV|NH|--)\s*$',
            stripped
        )
        if match:
            result.append({
                "xc_state": match.group(1),
                "segment1": match.group(2).strip(),
                "s1_state": match.group(3),
                "segment2": match.group(4).strip(),
                "s2_state": match.group(5)
            })
    return result


def parse_ip_bgp_vpnv4(output):
    """Parse tabular output from VPNv4 BGP show commands.

    Each VPNv4 route produces one record:

        {"rd": "100:1", "vrf": "CUST-A", "status": "*>i", "prefix": "10.2.2.0/24",
         "next_hop": "3.3.3.3", "origin": "i",
         "attributes": "0    100      0 65002 i"}

    Prefixes use `prefix`, not `network`, to avoid the evaluator's classful
    normalization of `network` turning a Class A /24 into /8.

    Metric, LocPrf, and Weight are fixed-width right-aligned columns whose empty
    values cannot be separated reliably from adjacent columns. They are not
    emitted individually. Prefer assertions on `status`, `prefix`, and
    `next_hop`; use raw `attributes` with `contains` or `regex` when necessary.
    """
    result = []
    current_rd = ""
    current_vrf = ""

    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue

        rd_match = re.match(
            r'^Route Distinguisher:\s*(\S+)(?:\s*\(default for vrf\s+(\S+)\))?',
            stripped
        )
        if rd_match:
            current_rd = rd_match.group(1)
            current_vrf = rd_match.group(2) or ""
            continue

        if stripped.startswith(("BGP table version", "Status codes", "Origin codes", "Network")):
            continue

        match = re.match(
            r'^([*sdh][>di\s]?[di\s]?|\s{2,3})\s*'
            r'(\d+\.\d+\.\d+\.\d+(?:/\d+)?)?\s+'
            r'(\d+\.\d+\.\d+\.\d+)\s+'
            r'(\S.*)$',
            line
        )
        if not match:
            continue

        prefix = match.group(2)
        if prefix and '/' not in prefix:
            prefix = add_default_mask_if_missing(prefix)
        attributes = match.group(4).strip()
        tokens = attributes.split()

        result.append({
            "rd": current_rd,
            "vrf": current_vrf,
            "status": (match.group(1) or "").strip(),
            "prefix": prefix or "",
            "next_hop": match.group(3),
            "origin": tokens[-1] if tokens and tokens[-1] in ("i", "e", "?") else "",
            "attributes": attributes
        })

    return result


def parse_cli_output(command, output):

    if IS_SONIC:
        return parse_sonic_cli_output(command, output)
    
    if command == "show running-config | section ip route":
        return parse_config_ip_route(output)
    # Any `show ... | include <pattern>` is asserted line by line. Lines are
    # stripped, so expected values must not carry the device indentation.
    if re.match(r'^show .+ \| include .+', command):
        return [{"line": line.strip()} for line in output.splitlines() if line.strip()]
    if re.match(
        r'^sh(?:ow)?\s+ip\s+r(?:oute)?(?:\s+vrf\s+\S+)?'
        r'\s+\d+\.\d+\.\d+\.\d+(?:\s+\d+\.\d+\.\d+\.\d+)?$',
        command
    ):
        return parse_ip_route_detail(output)
    if re.match(r'^show mpls ldp neighbor(?:\s+\S+)*$', command):
        return parse_mpls_ldp_neighbor(output)
    if re.match(r'^show mpls (?:ldp )?bindings?(?:\s+\S+)*$', command) or \
            re.match(r'^show mpls ip binding(?:\s+\S+)*$', command):
        return parse_mpls_ldp_bindings(output)
    if re.match(r'^show mpls forwarding-table(?:\s+\S+)*$', command):
        return parse_mpls_forwarding_table(output)
    if re.match(r'^show mpls traffic-eng tunnels brief$', command):
        return parse_mpls_te_tunnels_brief(output)
    if re.match(r'^show xconnect all(?:\s+\S+)*$', command):
        return parse_xconnect_all(output)
    if re.match(r'^show ip bgp vpnv4 (?:all|vrf \S+|rd \S+)(?:\s+\d+\.\d+\.\d+\.\d+(?:\s+\d+\.\d+\.\d+\.\d+)?)?$', command):
        return parse_ip_bgp_vpnv4(output)
    if command == "show ip protocols | section rip":
        return parse_ip_protocols_rip(output)
    if command == "show ip protocols | section ospf":
        return parse_ip_protocols_ospf(output)
    if command == "show ip protocols | section bgp":
        return parse_ip_protocols_bgp(output)
    if command == "show route-map":
        return parse_route_map(output)
    if command == "show ip community-list":
        return parse_ip_community_list(output)
    if re.match(r'^show ip bgp \d+\.\d+\.\d+\.\d+(?:\s+\d+\.\d+\.\d+\.\d+)?$', command):
        return parse_ip_bgp_prefix_detail(output)
    # Accept VPNv4/VRF variants as well. PE-CE neighbors in a VRF do not appear
    # in global `show ip bgp neighbors`; the VPNv4 VRF form has the same format.
    if re.match(r'^show ip bgp (?:vpnv4 (?:all|vrf \S+|rd \S+:\S+) )?neighbors(?: \d+\.\d+\.\d+\.\d+)?$', command):
        return parse_ip_bgp_neighbors_merged(output)
    if command == "show ip ospf | begin Area":
        return parse_ip_ospf_area(output)
    if command == "show ip ospf database summary":
        return parse_ip_ospf_database_summary(output)
    if command == "show ip ospf database external":
        return parse_ip_ospf_database_external(output)
    if command == "show ip ospf virtual-links":
        return parse_ip_ospf_virtual_links(output)
    else:
        return parse_output(platform="cisco_ios", command=command, data=output)

if __name__ == "__main__":
    cli_output = '''Loopback0 is up, line protocol is up 
  Internet Address 1.1.1.1/24, Area 0 
  Process ID 1, Router ID 172.16.3.1, Network Type LOOPBACK, Cost: 1
  Loopback interface is treated as a stub Host
'''

    result = parse_cli_output("show ip ospf interface Loopback1", cli_output)
    print(json.dumps(result, indent=2))
