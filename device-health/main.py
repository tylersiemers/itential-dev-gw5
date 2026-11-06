#!/usr/bin/env python3
"""IAG5 python-script service: SSH health check for Inventory Manager nodes.

Gateway5 passes the nodes chosen in runService's `inventory` on stdin as
{"inventory_nodes": [{"name", "attributes", "tags"}, ...]}. For each node this
connects with netmiko (itential_platform = netmiko device_type), runs one
command, and reports reachability. Always prints one JSON object and exits 0.
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--command", default="")
    args, _ = parser.parse_known_args()
    command = args.command or "show version"   # unset decorator props arrive as ""

    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
        nodes = (json.loads(raw) if raw.strip() else {}).get("inventory_nodes") or []
    except ValueError as exc:
        print(json.dumps({"success": False, "error": f"bad stdin: {exc}"}))
        return
    if not nodes:
        print(json.dumps({"success": False, "error": "no inventory nodes received"}))
        return

    with ThreadPoolExecutor(max_workers=min(10, len(nodes))) as pool:
        devices = list(pool.map(lambda n: check(n, command), nodes))
    up = sum(d["reachable"] for d in devices)
    print(json.dumps({
        "success": True,
        "healthy": up == len(devices),
        "command": command,
        "summary": {"total": len(devices), "up": up, "down": len(devices) - up},
        "down_devices": [d["name"] for d in devices if not d["reachable"]],
        "devices": devices,
    }))


if __name__ == "__main__":
    main()
