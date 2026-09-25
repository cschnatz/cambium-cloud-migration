from cnmaestro_migrate import plan
from fakes import device


def test_config_steps_in_import_order_with_antenna_gain_fix():
    devices = [device("aa", "Main Office", "Lobby", "AP"), device("bb", "Main Office", "", "AP2", online=False),
               device("xx", "Warehouse", "Dock", "W")]
    sel = plan.select(devices, "Main Office")
    profile_exports = {"AP": {"policies": {"wlan": ["Guest", "Staff"]},
                              "src": {"radio": {"radio": [{"antgain": "${ANTENNA_GAIN_24=5}"}, {"antgain": "${ANTENNA_GAIN_24=5}"}]}}},
                       "AP2": {"policies": {"wlan": ["Guest"]}}}
    wlan_exports = {"Guest": {"policy_type": "wlan"}, "Staff": {"policy_type": "wlan"}}
    sites = [{"nid": "Main Office", "tid": "Garden"}, {"nid": "Warehouse", "tid": "Dock"}]
    steps = plan.config_steps(sel, sites, profile_exports, wlan_exports)
    assert [(s["kind"], s["name"]) for s in steps] == [
        ("wlan", "Guest"), ("wlan", "Staff"), ("profile", "AP"), ("profile", "AP2"), ("network", "Main Office"),
        ("site", "Main Office / Garden"), ("site", "Main Office / Lobby")]
    assert steps[2]["payload"]["src"]["radio"]["radio"][1]["antgain"] == "${ANTENNA_GAIN_5=5}"
    assert steps[0]["payload"] == {"policy_type": "wlan"}
    assert (steps[-1]["network"], steps[-1]["site"], steps[-1]["payload"]) == ("Main Office", "Lobby", None)


def test_config_steps_skip_objects_whose_export_failed():
    sel = plan.select([device("aa", "N", "", "AP")], "N")
    steps = plan.config_steps(sel, [], {}, {})
    assert [(s["kind"], s["name"]) for s in steps] == [("network", "N")]


def test_mesh_roles_come_from_wlan_content_not_from_names():
    steps = [
        {"kind": "wlan", "name": "W base", "payload": {"src": {"basic": {"mesh_mode": "base"}}}},
        {"kind": "wlan", "name": "W client", "payload": {"src": {"basic": {"mesh_mode": "client"}}}},
        {"kind": "wlan", "name": "W", "payload": {"src": {"basic": {"mesh_mode": ""}}}},
        # the name says "client", the content is a mesh base
        {"kind": "profile", "name": "MeshClient", "payload": {"policies": {"wlan": ["W base", "W"]}}},
        {"kind": "profile", "name": "Client", "payload": {"policies": {"wlan": ["W client", "W"]}}},
        {"kind": "profile", "name": "Plain", "payload": {"policies": {"wlan": ["W"]}}},
        {"kind": "profile", "name": "Empty", "payload": {"policies": None}},
    ]
    assert plan.mesh_roles(steps) == {"MeshClient": "base", "Client": "client"}


def test_override_phases_access_points_before_switches():
    profiles = {"SW": "sw", "AP2": "wi-fi", "AP1": "wi-fi"}
    assert plan.override_phases(profiles) == [("APs", {"AP1": "wi-fi", "AP2": "wi-fi"}), ("switches", {"SW": "sw"})]
    assert plan.override_phases({"AP": "wi-fi"}) == [("APs", {"AP": "wi-fi"})]
    assert plan.override_phases({"SW": "sw"}) == [("switches", {"SW": "sw"})]


def test_override_phases_with_mesh():
    profiles = {"SW": "sw", "PB": "wi-fi", "PC": "wi-fi", "P": "wi-fi"}
    assert plan.override_phases(profiles, {"PB": "base", "PC": "client"}) == [
        ("APs", {"P": "wi-fi", "PC": "wi-fi"}), ("mesh bases", {"PB": "wi-fi"}), ("switches", {"SW": "sw"})]
    assert plan.override_phases({"PB": "wi-fi"}, {"PB": "base"}) == [("mesh bases", {"PB": "wi-fi"})]
