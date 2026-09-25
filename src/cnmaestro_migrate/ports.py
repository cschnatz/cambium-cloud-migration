"""cnMatrix switch port VLANs.

Port settings live per port, not in the switch profile, so moving a switch loses them unless they are
copied. The cloud stops returning them once the device is deleted from the cloud account.
"""
PORT_SECTIONS = ("security", "physical")


def port_config(port):
    """Cloud port → controller port config. None means nothing to copy: a default port (PVID 1, no own
    settings) or an auto-attach port, which the switch configures by itself."""
    cfg = port.get("config") or {}
    if str((cfg.get("action") or {}).get("vlanName", "")).startswith("#CambiumAutoVlanClient"):
        return None
    net = {k: v for k, v in (cfg.get("network") or {}).items() if k != "normalizedVlans"}
    native = port.get("nativeVlanId")
    if not net and native in (None, 1):
        net = None
    elif not net:
        net = {"vlans": str(native)}
    out = {}
    if net is not None:
        vl = net.get("vlans")
        net["vlans"] = ",".join(str(v) for v in vl) if isinstance(vl, list) else str(vl if vl is not None else native)
        net["nativeVlan"] = str(net.get("nativeVlan", native if native is not None else 1))
        out["network"] = net
    out.update({k: cfg[k] for k in PORT_SECTIONS if cfg.get(k)})
    return out or None


def port_plan(switch_mac, ports):
    """Body entries for the controller's PUT config/ports; pmac (the switch MAC) is required there."""
    return [{"mac": p["mac"], "pmac": switch_mac, "config": c} for p in ports if (c := port_config(p))]


def _vlan_set(value):
    return {x.strip() for x in str(value or "").split(",") if x.strip()}


def port_mismatches(plan, controller_ports):
    """Target (plan) against the controller: stored value (config.network) and live value (nativeVlanId)."""
    by_mac, out = {p["mac"].lower(): p for p in controller_ports}, []
    for entry in plan:
        want = entry["config"].get("network")
        cur = by_mac.get(entry["mac"].lower())
        if not want:
            continue
        label = f"P{cur['ifIndex']}" if cur else entry["mac"]
        have = (cur or {}).get("config", {}).get("network")
        if not have:
            out.append(f"{label}: not stored on the controller")
        elif _vlan_set(have.get("vlans")) != _vlan_set(want.get("vlans")) or str(have.get("nativeVlan")) != want["nativeVlan"]:
            out.append(f"{label}: stored {have.get('nativeVlan')}/{have.get('vlans')} instead of "
                       f"{want['nativeVlan']}/{want['vlans']}")
        elif str(cur.get("nativeVlanId")) != want["nativeVlan"]:
            out.append(f"{label}: device PVID {cur.get('nativeVlanId')} instead of {want['nativeVlan']}")
    return out
