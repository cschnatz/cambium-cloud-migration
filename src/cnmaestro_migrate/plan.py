"""Pure migration logic: no network and no file access.

Everything here works on the JSON structures of the cnMaestro Cloud and On-Premises UI APIs.
"""
import re
from dataclasses import dataclass, field

# The override lines this tool writes. A repeated run replaces them instead of adding a second copy,
# and they are not reported as a customer change when comparing profiles.
OWN = re.compile(r"^(no )?(management cambium-remote (url|validate-server-cert)|cnmaestro (url|validate-cert))\b")


def override_lines(mode, address, verify_cert=False):
    """CLI lines for a profile's user-defined override that point its devices to the controller.

    mode is the cloud device mode: "sw" for cnMatrix switches, anything else for access points.
    Unless verify_cert is set, devices stop validating the controller certificate, because a fresh
    controller uses a self-signed one and devices that reject it are stranded after the cloud commit.
    """
    url = f"https://{address}"
    if mode == "sw":
        return ([] if verify_cert else ["no cnmaestro validate-cert"]) + [f"cnmaestro url {url}"]
    return ([] if verify_cert else ["no management cambium-remote validate-server-cert"]) + [
        f"management cambium-remote url {url}"]


def merge_adv(existing, lines):
    """Keep the profile's other override lines, drop this tool's lines from earlier runs, append the new block."""
    keep = []
    for line in (existing or "").splitlines():
        if OWN.match(line.strip()):
            continue
        if line.strip() == "!" and keep and keep[-1].strip() == "!":
            continue                                  # a removed block leaves two "!" behind
        keep.append(line)
    base = "\n".join(keep).strip()
    if base in ("", "!"):
        base = "!"
    elif not base.endswith("!"):
        base += "\n!"
    return base + "\n" + "\n".join(lines) + "\n!"


def fix_antgain(payload):
    """The controller rejects ANTENNA_GAIN_24 on the 5/6 GHz radios ("antenna gain level not supported"),
    although the cloud accepted it. Use the 5 GHz variable from the second radio on and keep the default value."""
    radios = ((payload.get("src") or {}).get("radio") or {}).get("radio")
    if not isinstance(radios, list) or not any("ANTENNA_GAIN_24" in str(r.get("antgain", "")) for r in radios[1:]):
        return payload
    fixed = [r if i == 0 else {**r, "antgain": str(r.get("antgain", "")).replace("ANTENNA_GAIN_24", "ANTENNA_GAIN_5")}
             for i, r in enumerate(radios)]
    src = payload["src"]
    return {**payload, "src": {**src, "radio": {**src["radio"], "radio": fixed}}}


def _differs(cur, des):
    """Compare shared keys only: the controller drops single fields on import (e.g. acs_poll_interval)."""
    if isinstance(cur, dict) and isinstance(des, dict):
        return any(_differs(cur[k], v) for k, v in des.items() if k in cur)
    if isinstance(cur, list) and isinstance(des, list):
        return len(cur) != len(des) or any(_differs(a, b) for a, b in zip(cur, des))
    return cur != des and str(cur) != str(des)


def _customer_adv(adv):
    return [l.strip() for l in (adv or "").splitlines() if l.strip() and l.strip() != "!" and not OWN.match(l.strip())]


def stale_sections(current, desired):
    """What an existing controller object lacks compared with the fresh cloud state: changed src sections
    (whole), policies, customer override lines (this tool's own lines do not count). Empty means up to date."""
    out, cur_src = {}, current.get("src") or {}
    for sec, val in (desired.get("src") or {}).items():
        if sec not in cur_src or _differs(cur_src[sec], val):
            out.setdefault("src", {})[sec] = val
    if _differs(current.get("policies"), desired.get("policies")):
        out["policies"] = desired.get("policies")
    if _customer_adv(current.get("adv")) != _customer_adv(desired.get("adv")):
        out["adv"] = "\n".join(l for l in (desired.get("adv") or "").splitlines() if not OWN.match(l.strip()))
    return out


def describe_changes(changes):
    return [f"src.{k}" for k in (changes.get("src") or {})] + [k for k in changes if k != "src"]


def sync_job(macs):
    """Job body of the UI page "Sync Configuration" with its defaults; the same in cloud and controller."""
    return {"targets": [], "limit": 10, "error": False, "note": "", "state": 1, "overwrite": True,
            "desc": f"{len(macs)} device(s)", "devices": {"includeList": list(macs)}}


class NetworkNotFound(Exception):
    """The requested network has no devices in the cloud account."""


def profile_of(device):
    return (device.get("config") or {}).get("profile") or ""


def is_online(device):
    return bool((device.get("sys") or {}).get("online"))


def target_of(device):
    """Where the device must end up on the controller. Names are kept as they are in the cloud."""
    return {"network": device.get("nid") or "", "site": device.get("tid") or "", "profile": profile_of(device)}


@dataclass
class Selection:
    network: str
    site: str | None
    devices: list = field(default_factory=list)            # every device of the network (and site)
    migrate: list = field(default_factory=list)            # online, with profile: moved in this run
    offline: list = field(default_factory=list)
    no_profile: list = field(default_factory=list)
    skipped_switches: list = field(default_factory=list)
    follow_later: list = field(default_factory=list)       # offline, on an affected profile
    profiles: dict = field(default_factory=dict)           # profile name → device mode, for `migrate`
    shared: dict = field(default_factory=dict)             # profile name → MACs outside the selection


def select(devices, network, site=None, include_switches=False):
    """Pick the devices of one network (optionally one site) and sort them by what can happen to them."""
    in_network = [d for d in devices if d.get("nid") == network]
    if not in_network:
        known = sorted({d.get("nid") for d in devices if d.get("nid")})
        raise NetworkNotFound(f"network {network!r} has no devices in this account. Networks with devices: {known}")
    sel = Selection(network=network, site=site,
                    devices=[d for d in in_network if site is None or (d.get("tid") or "") == site])
    for d in sel.devices:
        if not is_online(d):
            sel.offline.append(d)
        elif not profile_of(d):
            sel.no_profile.append(d)
        elif d.get("mode") == "sw" and not include_switches:
            sel.skipped_switches.append(d)
        else:
            sel.migrate.append(d)
            sel.profiles[profile_of(d)] = d.get("mode")
    selected = {d["mac"] for d in sel.devices}
    for prof in sel.profiles:
        others = sorted(d["mac"] for d in devices if profile_of(d) == prof and d["mac"] not in selected)
        if others:
            sel.shared[prof] = others
    # The override belongs to the profile, so offline devices on it follow as soon as they come online.
    sel.follow_later = [d for d in sel.offline if profile_of(d) in sel.profiles]
    return sel


def risky_firmware(device):
    """XV2-2 access points on firmware 6.2 went offline after a profile push in the field."""
    return device.get("model") == "XV2-2" and str((device.get("mgmt") or {}).get("actSw", "")).startswith("6.2")


def deletion_blockers(check):
    """Entries of the cloud's validate-deletion answer that speak against deleting (the UI shows them as invalid)."""
    out = []
    for reason, rows in ((check or {}).get("invalid") or {}).items():
        if rows:
            out.append(f"{reason}: " + ", ".join(str(r.get("mac", r)) if isinstance(r, dict) else str(r) for r in rows))
    return out


def verify(device, target):
    """Compare a managed controller device with its target. Empty means correct."""
    c = device.get("config") or {}
    problems = []
    if device.get("nid") != target["network"]:
        problems.append(f"network {device.get('nid')} instead of {target['network']}")
    if c.get("profile") != target["profile"]:
        problems.append(f"profile {c.get('profile')} instead of {target['profile']}")
    if (target.get("site") or "") != (device.get("tid") or ""):
        problems.append(f"site {device.get('tid') or '(none)'} instead of {target.get('site') or '(none)'}")
    if not is_online(device):
        problems.append("offline")
    if not c.get("synced"):
        problems.append("not synced")
    return problems


def needs_resync(device):
    """Online but the configuration push failed (e.g. "Verification of configuration change failed.
    Try syncing again."). Another sync fixes that; a push still in progress is left alone."""
    c = device.get("config") or {}
    return is_online(device) and c.get("synced") is False and "failed" in (c.get("syncReason") or "")


def onboard_payload(device, target, site):
    """Assignment in the controller's onboarding queue: network, site, profile and the device's own variables
    from the cloud. Without the variables the profile defaults apply and an AP can lose its static IP."""
    # isSite as in the UI; without it the controller looks for a tower and answers "Site/Tower ... not found".
    c = device.get("config") or {}
    out = {"name": (device.get("cfg") or {}).get("name") or device["mac"], "nid": target["network"], "tid": site,
           "isSite": bool(site), "autoIP": c.get("autoIP", True)}
    if device.get("mode") == "sw":       # switches: device settings as rules plus hardware version, like the UI
        out["hw"] = (device.get("mgmt") or {}).get("hw")
        out["config"] = {"tName": target["profile"], "rules": c.get("rules") or {}, "overwrite": True}
    else:
        out["config"] = {"tName": target["profile"], "vars": c.get("vars") or {}, "wlanVars": c.get("wlanVars") or {},
                         "overwrite": True}
    return out


def prechecks(selection, export_errors, port_missing, allow_xv2_fw62):
    """Reasons to stop before anything is changed. Empty means go."""
    stop = []
    if export_errors:
        stop.append("cloud export failed: " + "; ".join(export_errors))
    if selection.shared:
        stop.append(f"profile(s) also used by devices outside the selection — the override would move them too: "
                    f"{selection.shared}")
    moving = {d["mac"] for d in selection.migrate}
    lost = [m for m in port_missing if m in moving]
    if lost:
        stop.append(f"no port backup for switch(es) {lost} — the cloud returned no ports; "
                    "switches are not moved without a backup")
    risky = [d["mac"] for d in selection.migrate if risky_firmware(d)]
    if risky and not allow_xv2_fw62:
        stop.append(f"XV2-2 on firmware 6.2 (went offline after a profile push in the field): {risky} — "
                    "upgrade the firmware first or pass --allow-xv2-fw62")
    return stop


def chain_stops(status):
    """Several networks in one run: continue only if the previous one left nothing half done."""
    return status == "incomplete"
