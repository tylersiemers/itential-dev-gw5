#!/usr/bin/env python3
"""IAG5 python-script service: SSH health check for Inventory Manager nodes.

Nodes come from either:
  * --nodes: a JSON string of Inventory Manager nodes, in any wrapper the
    platform returns (a list, {"data": [...]}, {"result": {"data": [...]}}),
    e.g. the stringified output of InventoryManager.getNodesByInventory; or
  * stdin {"inventory_nodes": [...]} when the platform passes runService's
    `inventory` through (newer Gateway Manager versions).
For each node this connects with netmiko (itential_platform = netmiko
device_type), runs one command, and reports reachability. Always prints one
JSON object and exits 0.
"""
import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor


def check(node, command):
    attrs = node.get("attributes") or {}
    opts = ((attrs.get("itential_driver_options") or {}).get("netmiko") or {})
    result = {"name": node.get("name"), "host": attrs.get("itential_host"),
              "platform": attrs.get("itential_platform"), "reachable": False,
              "output": "", "error": ""}
    try:
        from netmiko import ConnectHandler
        conn = ConnectHandler(
            device_type=attrs.get("itential_platform"),
            host=attrs.get("itential_host"),
            username=attrs.get("itential_user"),
            password=attrs.get("itential_password"),
            port=int(opts.get("port", 22)),
            conn_timeout=int(opts.get("conn_timeout", 30)),
        )
        try:
            out = conn.send_command(command, read_timeout=60)
        finally:
            conn.disconnect()
        result["reachable"] = True
        result["output"] = "\n".join(out.strip().splitlines()[:15])
    except Exception as exc:  # report every failure per device, never crash
        result["error"] = f"{type(exc).__name__}: {exc}"[:500]
    return result


def find_nodes(obj):
    """Return the first list of node dicts (items with "attributes") found in obj."""
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
    parser.add_argument("--command", default="")
    parser.add_argument("--nodes", default="")
    args, _ = parser.parse_known_args()
    command = args.command or "show version"   # unset decorator props arrive as ""

    nodes, source = [], ""
    try:
        if args.nodes.strip():
            nodes, source = find_nodes(json.loads(args.nodes)), "param"
        if not nodes and not sys.stdin.isatty():
            raw = sys.stdin.read()
            if raw.strip():
                nodes, source = find_nodes(json.loads(raw)), "stdin"
    except ValueError as exc:
        print(json.dumps({"success": False, "error": f"could not parse nodes JSON: {exc}"}))
        return
    if not nodes:
        print(json.dumps({"success": False, "error": "no inventory nodes received (pass --nodes or an inventory)"}))
        return

    with ThreadPoolExecutor(max_workers=min(10, len(nodes))) as pool:
        devices = list(pool.map(lambda n: check(n, command), nodes))
    up = sum(d["reachable"] for d in devices)
    print(json.dumps({
        "success": True,
        "healthy": up == len(devices),
        "command": command,
        "node_source": source,
        "summary": {"total": len(devices), "up": up, "down": len(devices) - up},
        "down_devices": [d["name"] for d in devices if not d["reachable"]],
        "devices": devices,
    }))


if __name__ == "__main__":
    main()
