#!/usr/bin/env python3
"""IAG5 python-script service: plan / apply an Itential → NetBox device sync.

Part of itential-syncto-netbox. Input is the credential-free device list that
the device-facts service returns (each device's `site` filled in by the
workflow), plus the list of sites this run covers.

  --devices  JSON string: list of device records, or device-facts' whole output
  --sites    JSON string: list of site names covered by this run
  --dry_run  "true" = plan only (no writes); anything else = apply

Env:  NETBOX_URL   (runtime.env)
      NETBOX_TOKEN (Gateway5 secret netbox-sync-token)

Rules (customer-spec.md / solution-design.md):
  * only objects tagged `itential-sync` are updated or marked stale;
    a same-name device without the tag is a conflict and is skipped
  * nothing is ever deleted
  * a field is written only when its value differs (repeat run = 0 writes)
  * synced devices in the run's sites that are missing from the run are set
    to status offline + tag `itential-stale`
Always prints one JSON object and exits 0.
"""
import argparse
import datetime
import json
import os
import re
import sys
import time

import requests

SYNC_TAG = "itential-sync"
STALE_TAG = "itential-stale"
LAST_SYNC_CF = "itential_last_sync"
TIMEOUT = 30

PLATFORMS = {
    # itential_platform: (manufacturer, platform name)
    "cisco_xr": ("Cisco", "Cisco IOS XR"),
    "cisco_ios": ("Cisco", "Cisco IOS"),
    "cisco_nxos": ("Cisco", "Cisco NX-OS"),
    "arista_eos": ("Arista", "Arista EOS"),
    "juniper_junos": ("Juniper", "Juniper Junos"),
    "nokia_srl": ("Nokia", "Nokia SR Linux"),
}


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:100] or "unknown"


class NetBox:
    def __init__(self, url, token, dry_run):
        self.url = url.rstrip("/") + "/api"
        scheme = "Bearer" if token.startswith("nbt_") else "Token"
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"{scheme} {token}",
                               "Content-Type": "application/json",
                               "Accept": "application/json"})
        self.dry_run = dry_run
        self.writes = 0
        self._fake_id = 0

    def _call(self, method, path, **kw):
        for attempt in (1, 2):  # retry once on 5xx / connection problems
            try:
                r = self.s.request(method, self.url + path, timeout=TIMEOUT, **kw)
            except requests.RequestException:
                if attempt == 2:
                    raise
                time.sleep(2)
                continue
            if r.status_code >= 500 and attempt == 1:
                time.sleep(2)
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
            return r.json() if r.content else {}

    def list(self, path, **params):
        # In a dry run, objects that would be created carry negative ids; nothing
        # in NetBox can reference them yet, so a filter on one matches nothing.
        if any(isinstance(v, int) and v < 0 for v in params.values()):
            return []
        params.setdefault("limit", 1000)
        out, data = [], self._call("GET", path, params=params)
        out.extend(data.get("results", []))
        while data.get("next"):
            data = self.s.get(data["next"], timeout=TIMEOUT).json()
            out.extend(data.get("results", []))
        return out

    def first(self, path, **params):
        res = self.list(path, **params)
        return res[0] if res else None

    def create(self, path, body):
        if self.dry_run:
            self._fake_id -= 1
            return dict(body, id=self._fake_id, _planned=True)
        self.writes += 1
        return self._call("POST", path, json=body)

    def update(self, path, obj_id, body):
        if self.dry_run or obj_id < 0:
            return None
        self.writes += 1
        return self._call("PATCH", f"{path}{obj_id}/", json=body)


class Sync:
    def __init__(self, nb):
        self.nb = nb
        self.cache = {}
        self.bootstrap = []          # supporting objects created / planned

    # ---------------------------------------------------------- lookups
    def ensure(self, path, key, lookup, body):
        """Get-or-create a supporting object (never updated)."""
        ck = (path, key)
        if ck in self.cache:
            return self.cache[ck]
        obj = self.nb.first(path, **lookup)
        if obj is None:
            obj = self.nb.create(path, body)
            self.bootstrap.append({"type": path.strip("/"), "name": key,
                                   "action": "create"})
        self.cache[ck] = obj
        return obj

    def tag(self, name):
        return self.ensure("/extras/tags/", name, {"slug": name},
                           {"name": name, "slug": name,
                            "description": "Managed by itential-syncto-netbox"})

    def ensure_custom_field(self):
        self.ensure("/extras/custom-fields/", LAST_SYNC_CF, {"name": LAST_SYNC_CF},
                    {"name": LAST_SYNC_CF, "label": "Itential last sync", "type": "text",
                     "object_types": ["dcim.device"], "required": False,
                     "description": "Set by itential-syncto-netbox when it changes the device"})

    def site(self, name):
        slug = slugify(name)
        return self.ensure("/dcim/sites/", slug, {"slug": slug},
                           {"name": name, "slug": slug, "status": "active",
                            "tags": [{"slug": SYNC_TAG}]})

    def manufacturer(self, name):
        slug = slugify(name)
        return self.ensure("/dcim/manufacturers/", slug, {"slug": slug},
                           {"name": name, "slug": slug})

    def platform(self, key):
        mfr_name, pname = PLATFORMS.get(key, (key or "Unknown", key or "unknown"))
        mfr = self.manufacturer(mfr_name)
        slug = slugify(pname)
        return self.ensure("/dcim/platforms/", slug, {"slug": slug},
                           {"name": pname, "slug": slug, "manufacturer": mfr["id"]}), mfr

    def device_type(self, mfr, model):
        slug = slugify(model)
        return self.ensure("/dcim/device-types/", f"{mfr['id']}:{slug}",
                           {"manufacturer_id": mfr["id"], "slug": slug},
                           {"manufacturer": mfr["id"], "model": model, "slug": slug})

    def role(self, name):
        slug = slugify(name)
        return self.ensure("/dcim/device-roles/", slug, {"slug": slug},
                           {"name": name, "slug": slug, "color": "9e9e9e"})

    # ---------------------------------------------------------- per device
    def sync_device(self, d, now):
        res = {"name": d.get("name"), "site": d.get("site"), "action": "unchanged",
               "changes": {}, "interfaces": {"create": [], "update": [], "extra_in_netbox": []},
               "ip": None, "flags": [], "error": ""}
        if not d.get("reachable"):
            res["flags"].append("unreachable: synced from inventory data only"
                                + (f" ({d.get('error')})" if d.get("error") else ""))
        facts = d.get("facts") or None
        site = self.site(d["site"])
        plat, mfr = self.platform(d.get("platform"))
        role_name = d.get("role") or ""
        if not role_name:
            n = (d.get("name") or "").lower()
            role_name = "spine" if n.startswith("spine") else "leaf" if n.startswith("leaf") else "unknown"
        role = self.role(role_name)

        existing = None
        if site["id"] > 0:
            existing = self.nb.first("/dcim/devices/", name=d["name"], site_id=site["id"])
        if existing and SYNC_TAG not in {t["slug"] for t in existing.get("tags", [])}:
            res["action"] = "conflict"
            res["flags"].append("device exists in NetBox without tag itential-sync; not changed")
            return res

        if facts and facts.get("model"):
            dtype = self.device_type(mfr, facts["model"])
        elif existing:
            dtype = existing["device_type"]
        else:
            dtype = self.device_type(mfr, f"unknown-{d.get('platform') or 'device'}")

        desired = {"role": role["id"], "device_type": dtype["id"], "platform": plat["id"],
                   "status": "active"}
        if facts and facts.get("serial") is not None:
            desired["serial"] = facts["serial"] or ""

        if existing is None:
            body = dict(desired, name=d["name"], site=site["id"],
                        tags=[{"slug": SYNC_TAG}],
                        custom_fields={LAST_SYNC_CF: now})
            dev = self.nb.create("/dcim/devices/", body)
            res["action"] = "create"
            res["changes"] = {k: [None, v] for k, v in desired.items()}
        else:
            dev = existing
            cur = {"role": (existing.get("role") or {}).get("id"),
                   "device_type": (existing.get("device_type") or {}).get("id"),
                   "platform": (existing.get("platform") or {}).get("id"),
                   "status": (existing.get("status") or {}).get("value"),
                   "serial": existing.get("serial") or ""}
            patch = {k: v for k, v in desired.items() if cur.get(k) != v}
            tags = {t["slug"] for t in existing.get("tags", [])}
            if STALE_TAG in tags:
                patch["tags"] = [{"slug": t} for t in sorted(tags - {STALE_TAG})]
            if patch:
                res["changes"] = {k: [cur.get(k), v] for k, v in patch.items() if k != "tags"}
                if "tags" in patch:
                    res["changes"]["tags"] = [sorted(tags), sorted(tags - {STALE_TAG})]

        # interfaces (only with live facts) + management IP
        self._interfaces = {}
        if facts:
            self.sync_interfaces(dev, facts, res)
        mgmt_ip = self.sync_mgmt_ip(dev, d, facts, res)
        cur_primary = ((existing or {}).get("primary_ip4") or {})
        if mgmt_ip and cur_primary.get("id") != mgmt_ip["id"]:
            res["changes"]["primary_ip4"] = [cur_primary.get("address"), mgmt_ip.get("address")]
            if existing is None:
                self.nb.update("/dcim/devices/", dev["id"], {"primary_ip4": mgmt_ip["id"]})
        if existing is not None:
            patch = {}
            for k, (_, v) in res["changes"].items():
                if k in desired:
                    patch[k] = v
            if "tags" in res["changes"]:
                patch["tags"] = [{"slug": t} for t in res["changes"]["tags"][1]]
            if "primary_ip4" in res["changes"] and mgmt_ip:
                patch["primary_ip4"] = mgmt_ip["id"]
            any_change = bool(patch) or res["interfaces"]["create"] or \
                res["interfaces"]["update"] or (res["ip"] or {}).get("action") in ("create", "update")
            if any_change:
                patch["custom_fields"] = {LAST_SYNC_CF: now}
                self.nb.update("/dcim/devices/", dev["id"], patch)
                res["action"] = "update"
        return res

    def sync_interfaces(self, dev, facts, res):
        current = {}
        if dev["id"] > 0:
            current = {i["name"]: i for i in self.nb.list("/dcim/interfaces/", device_id=dev["id"])}
        seen = set()
        for itf in facts.get("interfaces") or []:
            name = itf["name"]
            seen.add(name)
            want = {"description": itf.get("description") or "",
                    "enabled": bool(itf.get("enabled")),
                    "mgmt_only": bool(itf.get("mgmt_only"))}
            cur = current.get(name)
            if cur is None:
                obj = self.nb.create("/dcim/interfaces/", dict(
                    want, device=dev["id"], name=name, type="other",
                    tags=[{"slug": SYNC_TAG}]))
                current[name] = obj
                res["interfaces"]["create"].append(name)
            else:
                diff = {k: v for k, v in want.items() if cur.get(k) != v}
                if diff:
                    self.nb.update("/dcim/interfaces/", cur["id"], diff)
                    res["interfaces"]["update"].append({"name": name, "changes": diff})
        res["interfaces"]["extra_in_netbox"] = sorted(set(current) - seen)
        self._interfaces = current

    def sync_mgmt_ip(self, dev, d, facts, res):
        mgmt_name, prefix = None, None
        if facts:
            for itf in facts.get("interfaces") or []:
                for p in itf.get("ipv4") or []:
                    if p.split("/")[0] == d.get("host"):
                        mgmt_name, prefix = itf["name"], p
        host = d.get("host")
        if not host:
            return None
        existing_ips = self.nb.list("/ipam/ip-addresses/", address=host)
        if not prefix:
            # No live facts: keep whatever is already there for this host.
            mine = [ip for ip in existing_ips
                    if (ip.get("assigned_object") or {}).get("device", {}).get("id") == dev["id"]]
            if mine:
                res["ip"] = {"address": mine[0]["address"], "action": "unchanged"}
                return mine[0]
            res["flags"].append("management IP not synced: no live facts")
            return None
        itf = self._interfaces.get(mgmt_name)
        if itf is None:
            return None
        ip = next((x for x in existing_ips if x["address"] == prefix), None) or \
            (existing_ips[0] if existing_ips else None)
        if ip is None:
            ip = self.nb.create("/ipam/ip-addresses/", {
                "address": prefix, "status": "active",
                "assigned_object_type": "dcim.interface", "assigned_object_id": itf["id"],
                "tags": [{"slug": SYNC_TAG}], "description": f"{d['name']} management"})
            res["ip"] = {"address": prefix, "interface": mgmt_name, "action": "create"}
            return ip
        assigned = ip.get("assigned_object") or {}
        tags = {t["slug"] for t in ip.get("tags", [])}
        if assigned and (assigned.get("device") or {}).get("id") != dev["id"] \
                and SYNC_TAG not in tags:
            res["flags"].append(f"IP {ip['address']} is assigned to another object; not changed")
            res["ip"] = {"address": ip["address"], "action": "conflict"}
            return None
        diff = {}
        if ip["address"] != prefix:
            diff["address"] = prefix
        if ip.get("assigned_object_id") != itf["id"] or \
                ip.get("assigned_object_type") != "dcim.interface":
            diff.update(assigned_object_type="dcim.interface", assigned_object_id=itf["id"])
        if SYNC_TAG not in tags:
            diff["tags"] = [{"slug": t} for t in sorted(tags | {SYNC_TAG})]
        if diff:
            self.nb.update("/ipam/ip-addresses/", ip["id"], diff)
            res["ip"] = {"address": prefix, "interface": mgmt_name, "action": "update",
                         "changes": sorted(diff)}
        else:
            res["ip"] = {"address": prefix, "interface": mgmt_name, "action": "unchanged"}
        return ip

    # ---------------------------------------------------------- reconcile
    def reconcile(self, sites, run_names, now):
        stale = []
        for name in sites:
            site = self.nb.first("/dcim/sites/", slug=slugify(name))
            if not site:
                continue
            for dev in self.nb.list("/dcim/devices/", site_id=site["id"], tag=SYNC_TAG):
                if (name, dev["name"]) in run_names:
                    continue
                tags = {t["slug"] for t in dev.get("tags", [])}
                status = (dev.get("status") or {}).get("value")
                if status == "offline" and STALE_TAG in tags:
                    stale.append({"name": dev["name"], "site": name, "action": "unchanged"})
                    continue
                self.tag(STALE_TAG)
                self.nb.update("/dcim/devices/", dev["id"], {
                    "status": "offline",
                    "tags": [{"slug": t} for t in sorted(tags | {STALE_TAG})],
                    "custom_fields": {LAST_SYNC_CF: now}})
                stale.append({"name": dev["name"], "site": name, "action": "offline",
                              "changes": {"status": [status, "offline"]}})
        return stale


def load_devices(raw):
    data = json.loads(raw) if raw.strip() else []
    if isinstance(data, dict):
        data = data.get("devices", [])
    out = []
    for item in data:  # accept nested lists (one list per inventory)
        out.extend(item if isinstance(item, list) else
                   item.get("devices", []) if isinstance(item, dict) and "devices" in item
                   else [item])
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--devices", default="")
    parser.add_argument("--sites", default="")
    parser.add_argument("--dry_run", default="")
    args, _ = parser.parse_known_args()
    dry_run = args.dry_run.strip().lower() != "false"   # safe default: plan only
    try:
        devices = load_devices(args.devices)
        sites = json.loads(args.sites) if args.sites.strip() else []
        if isinstance(sites, str):
            sites = [sites]
    except ValueError as exc:
        print(json.dumps({"success": False, "error": f"bad JSON input: {exc}"}))
        return
    sites = sorted(set(sites) | {d["site"] for d in devices if d.get("site")})
    url, token = os.environ.get("NETBOX_URL", ""), os.environ.get("NETBOX_TOKEN", "")
    if not url or not token:
        print(json.dumps({"success": False, "error": "NETBOX_URL / NETBOX_TOKEN not set"}))
        return
    missing_site = [d.get("name") for d in devices if not d.get("site")]
    if missing_site:
        print(json.dumps({"success": False, "error": f"devices without site: {missing_site}"}))
        return

    nb = NetBox(url, token, dry_run)
    sync = Sync(nb)
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()
    report = {"success": True, "dry_run": dry_run, "started": now, "devices": [],
              "stale": [], "bootstrap": [],
              "summary": {"created": 0, "updated": 0, "unchanged": 0, "offlined": 0,
                          "conflicts": 0, "failed": 0}}
    try:
        sync.tag(SYNC_TAG)
        sync.ensure_custom_field()
    except Exception as exc:
        report.update(success=False, error=f"bootstrap failed: {exc}")
        print(json.dumps(report))
        return

    for d in devices:
        try:
            r = sync.sync_device(d, now)
        except Exception as exc:
            r = {"name": d.get("name"), "site": d.get("site"), "action": "failed",
                 "error": str(exc)[:500], "flags": []}
        report["devices"].append(r)
    try:
        report["stale"] = sync.reconcile(sites, {(d["site"], d["name"]) for d in devices}, now)
    except Exception as exc:
        report["stale_error"] = str(exc)[:500]

    s = report["summary"]
    for r in report["devices"]:
        key = {"create": "created", "update": "updated", "unchanged": "unchanged",
               "conflict": "conflicts", "failed": "failed"}[r["action"]]
        s[key] += 1
    s["offlined"] = sum(1 for x in report["stale"] if x["action"] == "offline")
    report["bootstrap"] = sync.bootstrap
    report["netbox_writes"] = nb.writes
    if s["failed"] or report.get("stale_error"):
        report["success"] = False
    print(json.dumps(report))


if __name__ == "__main__":
    sys.exit(main())
