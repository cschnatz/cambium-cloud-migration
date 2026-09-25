"""Pure migration logic: no network and no file access.

Everything here works on the JSON structures of the cnMaestro Cloud and On-Premises UI APIs.
"""
import re

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
