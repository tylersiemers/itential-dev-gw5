#!/usr/bin/env python3
"""IAG5 python-script service: collect device facts for Inventory Manager nodes.

Part of itential-syncto-netbox. Input nodes come from
InventoryManager.getNodesByInventory (any wrapper: list, {"data": [...]},
{"result": {"data": [...]}}) passed as the --nodes JSON string, or from stdin
{"inventory_nodes": [...]} when Gateway Manager passes runService's inventory.

For each node it logs in with netmiko (itential_platform = netmiko
device_type) and returns model, serial, OS version and interfaces.

Credentials (itential_user / itential_password / driver options) are used for
the login only. Output carries an allow-list of node fields and never any
credential value. Always prints one JSON object and exits 0.
"""
import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor

ROLE_TAGS = {"spine", "leaf", "border", "core", "edge", "access",
             "distribution", "router", "switch", "firewall"}


# ---------------------------------------------------------------- parsing

def parse_xr_version(text):
    facts = {"model": None, "serial": None, "os_version": None}
    m = re.search(r"Cisco IOS XR Software, Version\s+(\S+)", text)
    if m:
        facts["os_version"] = m.group(1)
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith("cisco ") and "processor" not in line \
                and "IOS XR Software" not in line:
            facts["model"] = line[len("cisco "):].strip()
            break
    m = re.search(r"(?i)serial\s*(?:number|num|no\.?)?\s*[:#]?\s*([A-Z0-9]{6,})", text)
    if m:
        facts["serial"] = m.group(1)
    return facts


def parse_xr_interfaces(text):
    """Parse `show interfaces | include "line protocol|address is|Description:"`."""
    interfaces, cur = [], None
    for line in text.splitlines():
        m = re.match(r"^(\S+) is (.+?), line protocol is", line)
        if m:
            name, state = m.group(1), m.group(2)
            if name.lower().startswith("null"):
                cur = None
                continue
            cur = {"name": name, "description": "",
                   "enabled": "administratively down" not in state,
                   "mac": None, "ipv4": [],
                   "mgmt_only": name.lower().startswith("mgmteth")}
            interfaces.append(cur)
            continue
        if cur is None:
            continue
        m = re.search(r"address is ([0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\.[0-9a-fA-F]{4})", line)
        if m:
            h = m.group(1).replace(".", "").upper()
            cur["mac"] = ":".join(h[i:i + 2] for i in range(0, 12, 2))
        m = re.search(r"Internet address is (\d+\.\d+\.\d+\.\d+/\d+)", line)
        if m:
            cur["ipv4"].append(m.group(1))
        m = re.match(r"^\s+Description:\s*(.*)$", line)
        if m:
            cur["description"] = m.group(1).strip()
    return interfaces


def srl_json(text):
    """SR Linux appends a `--{ running }--` prompt line after JSON output."""
    return json.loads(text.split("--{")[0].strip() or "{}")


def collect_xr(conn):
    facts = parse_xr_version(conn.send_command("show version", read_timeout=60))
    facts["interfaces"] = parse_xr_interfaces(conn.send_command(
        'show interfaces | include "line protocol|address is|Description:"',
        read_timeout=90))
    return facts


def collect_srl(conn):
    chassis = srl_json(conn.send_command(
        "info from state /platform chassis | as json", read_timeout=60))
    ver = conn.send_command("show version", read_timeout=60)
    m = re.search(r"Software Version\s*:\s*(\S+)", ver)
    facts = {"model": chassis.get("type"), "serial": chassis.get("serial-number"),
             "os_version": m.group(1) if m else None, "interfaces": []}
    state = srl_json(conn.send_command(
        "info from state /interface * | as json", read_timeout=120))
    for itf in state.get("interface", []):
        name = itf.get("name", "")
        ipv4 = []
        for sub in itf.get("subinterface", []) or []:
            for addr in ((sub.get("ipv4") or {}).get("address") or []):
                if addr.get("ip-prefix"):
                    ipv4.append(addr["ip-prefix"])
        facts["interfaces"].append({
            "name": name,
            "description": itf.get("description") or "",
            "enabled": itf.get("admin-state") == "enable",
            "mac": (itf.get("ethernet") or {}).get("hw-mac-address"),
            "ipv4": ipv4,
            "mgmt_only": name.startswith("mgmt"),
        })
    return facts


COLLECTORS = {"cisco_xr": collect_xr, "nokia_srl": collect_srl}


# ---------------------------------------------------------------- per node

def node_record(node):
    """Allow-listed, credential-free view of an Inventory Manager node."""
    attrs = node.get("attributes") or {}
    tags = [t for t in (node.get("tags") or []) if isinstance(t, str)]
    role = attrs.get("role")
    if not role:
        role = next((t for t in tags if t.lower() in ROLE_TAGS), None)
    return {"name": node.get("name"), "host": attrs.get("itential_host"),
            "platform": attrs.get("itential_platform"), "site": attrs.get("site"),
            "role": role, "tags": tags, "reachable": False, "error": "", "facts": None}


def collect(node):
    attrs = node.get("attributes") or {}
    rec = node_record(node)
    opts = ((attrs.get("itential_driver_options") or {}).get("netmiko") or {})
    collector = COLLECTORS.get(rec["platform"])
    if collector is None:
        rec["error"] = f"no fact collector for platform {rec['platform']!r}"
        return rec
    try:
        from netmiko import ConnectHandler
        conn = ConnectHandler(
            device_type=rec["platform"], host=rec["host"],
            username=attrs.get("itential_user"), password=attrs.get("itential_password"),
            port=int(opts.get("port", 22)), conn_timeout=int(opts.get("conn_timeout", 30)))
        try:
            rec["facts"] = collector(conn)
        finally:
            conn.disconnect()
        rec["reachable"] = True
    except Exception as exc:  # report per device, never crash the batch
        msg = f"{type(exc).__name__}: {exc}"
        for secret in (attrs.get("itential_password"), attrs.get("itential_user")):
            if secret:
                msg = msg.replace(str(secret), "***")
        rec["error"] = msg[:500]
    return rec


def find_nodes(obj):
    """Return the first list of node dicts (items with "attributes") in obj."""
    if isinstance(obj, list):
        if obj and all(isinstance(i, dict) and "attributes" in i for i in obj):
            return obj
        items = obj
    elif isinstance(obj, dict):
        items = obj.values()
    else:
        return []
    for item in items:
        found = find_nodes(item)
        if found:
            return found
    return []


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--nodes", default="")
    parser.add_argument("--max_workers", default="")
    args, _ = parser.parse_known_args()
    try:
        workers = max(1, min(20, int(args.max_workers or 5)))
    except ValueError:
        workers = 5

    nodes = []
    try:
        if args.nodes.strip():
            nodes = find_nodes(json.loads(args.nodes))
        if not nodes and not sys.stdin.isatty():
            raw = sys.stdin.read()
            if raw.strip():
                nodes = find_nodes(json.loads(raw))
    except ValueError as exc:
        print(json.dumps({"success": False, "error": f"could not parse nodes JSON: {exc}",
                          "devices": []}))
        return
    if not nodes:
        print(json.dumps({"success": False, "error": "no nodes received", "devices": []}))
        return

    with ThreadPoolExecutor(max_workers=workers) as pool:
        devices = list(pool.map(collect, nodes))
    print(json.dumps({"success": True, "count": len(devices),
                      "reachable": sum(d["reachable"] for d in devices),
                      "devices": devices}))


if __name__ == "__main__":
    main()
