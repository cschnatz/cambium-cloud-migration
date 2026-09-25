import pytest

from cnmaestro_migrate import plan
from fakes import device


def account(*extra):
    return [device("aa", "Main Office", "Lobby", "AP Profile"),
            device("bb", "Main Office", "Lobby", "AP Profile", online=False),
            device("cc", "Main Office", "Lobby", "Switch Profile", mode="sw"),
            device("dd", "Main Office", "", None),
            device("xx", "Warehouse", "", "Warehouse APs"),
            *extra]


def macs(rows):
    return [d["mac"] for d in rows]


def test_select_sorts_devices_by_what_can_happen_to_them():
    sel = plan.select(account(), "Main Office")
    assert sel.network == "Main Office" and sel.site is None
    assert macs(sel.migrate) == ["aa"]
    assert macs(sel.offline) == ["bb"]
    assert macs(sel.skipped_switches) == ["cc"]
    assert macs(sel.no_profile) == ["dd"]
    assert sel.profiles == {"AP Profile": "wi-fi"}
    assert sorted(macs(sel.devices)) == ["aa", "bb", "cc", "dd"]


def test_select_with_switches():
    sel = plan.select(account(), "Main Office", include_switches=True)
    assert sorted(macs(sel.migrate)) == ["aa", "cc"]
    assert sel.profiles == {"AP Profile": "wi-fi", "Switch Profile": "sw"}


def test_select_reports_a_profile_shared_with_other_devices():
    sel = plan.select(account(device("ee", "Warehouse", "", "AP Profile")), "Main Office")
    assert sel.shared == {"AP Profile": ["ee"]}


def test_select_by_site():
    sel = plan.select(account(device("ff", "Main Office", "Garden", "AP Profile")), "Main Office", site="Garden")
    assert macs(sel.migrate) == ["ff"]
    assert sel.shared == {"AP Profile": ["aa", "bb"]}          # the rest of the network uses the same profile


def test_unknown_network_lists_the_known_ones():
    with pytest.raises(plan.NetworkNotFound, match="Warehouse"):
        plan.select(account(), "Nowhere")


def test_offline_device_on_an_affected_profile_follows_later():
    assert macs(plan.select(account(), "Main Office").follow_later) == ["bb"]


def test_target_keeps_cloud_names():
    assert plan.target_of(device("aa", "Main Office", "Lobby", "AP Profile")) == {
        "network": "Main Office", "site": "Lobby", "profile": "AP Profile"}
    assert plan.target_of({"mac": "aa"}) == {"network": "", "site": "", "profile": ""}


def test_onboard_payload_for_an_access_point_carries_its_variables():
    dev = {"mac": "aa", "mode": "wi-fi", "cfg": {"name": "AP1"},
           "config": {"profile": "AP Profile", "autoIP": False, "vars": {"VLAN_1_IP": "10.0.0.5"}, "wlanVars": None}}
    target = {"network": "Main Office", "site": "Lobby", "profile": "AP Profile"}
    assert plan.onboard_payload(dev, target, site="Lobby") == {
        "name": "AP1", "nid": "Main Office", "tid": "Lobby", "isSite": True, "autoIP": False,
        "config": {"tName": "AP Profile", "vars": {"VLAN_1_IP": "10.0.0.5"}, "wlanVars": {}, "overwrite": True}}


def test_onboard_payload_without_site():
    dev = {"mac": "aa", "mode": "wi-fi", "cfg": {"name": "AP1"}, "config": {}}
    p = plan.onboard_payload(dev, {"network": "N", "site": "Lobby", "profile": "P"}, site="")
    assert p["tid"] == "" and p["isSite"] is False and p["autoIP"] is True and p["config"]["vars"] == {}


def test_onboard_payload_for_a_switch_uses_rules_and_hardware_like_the_ui():
    rules = {"ifVlanIpEntry": [{"ifVlanIpVlanId": 1, "ifVlanIpAddrAllocMethod": 2}], "portCfg": []}
    dev = {"mac": "sw", "mode": "sw", "cfg": {"name": "Core"}, "mgmt": {"hw": "12"},
           "config": {"profile": "P", "vars": {"DISPLAY_NAME": None}, "rules": rules}}
    assert plan.onboard_payload(dev, {"network": "N", "site": "Lobby", "profile": "P"}, site="Lobby") == {
        "name": "Core", "nid": "N", "tid": "Lobby", "isSite": True, "autoIP": True, "hw": "12",
        "config": {"tName": "P", "rules": rules, "overwrite": True}}


def test_deletion_blockers():
    assert plan.deletion_blockers({"invalid": {"cbrsClaimedChildDevicePresent": [], "pTokenOnboardedChildDevicePresent": []}}) == []
    assert plan.deletion_blockers({"invalid": {"pTokenOnboardedChildDevicePresent": [{"mac": "aa"}]}}) == [
        "pTokenOnboardedChildDevicePresent: aa"]
    assert plan.deletion_blockers(None) == []


def test_verify_reports_wrong_network_profile_and_sync():
    target = {"network": "N", "site": "", "profile": "P"}
    assert plan.verify({"nid": "N", "config": {"profile": "P", "synced": True}, "sys": {"online": True}}, target) == []
    problems = plan.verify({"nid": "X", "config": {"profile": "Q", "synced": False}, "sys": {"online": True}}, target)
    assert problems == ["network X instead of N", "profile Q instead of P", "not synced"]


def test_verify_reports_wrong_site_and_offline():
    target = {"network": "N", "profile": "P", "site": "Lobby"}
    ok = {"nid": "N", "tid": "Lobby", "config": {"profile": "P", "synced": True}, "sys": {"online": True}}
    assert plan.verify(ok, target) == []
    assert plan.verify({**ok, "tid": ""}, target) == ["site (none) instead of Lobby"]
    assert plan.verify({**ok, "tid": ""}, {**target, "site": ""}) == []
    assert plan.verify({**ok, "sys": {"online": False}}, target) == ["offline"]


def test_xv2_2_on_firmware_6_2_is_risky():
    assert plan.risky_firmware({"model": "XV2-2", "mgmt": {"actSw": "6.2-r14"}})
    assert not plan.risky_firmware({"model": "XV2-2", "mgmt": {"actSw": "6.6.0.3-r9"}})
    assert not plan.risky_firmware({"model": "E410", "mgmt": {"actSw": "6.2"}})


@pytest.mark.parametrize("dev, expected", [
    ({"sys": {"online": True}, "config": {"synced": False, "syncReason": "Configuration failed: Verification of configuration change failed.  Try syncing again."}}, True),
    ({"sys": {"online": True}, "config": {"synced": False, "syncReason": "Configuration failed: radio2_antgain:Error: antenna gain level not supported"}}, True),
    ({"sys": {"online": True}, "config": {"synced": False, "syncReason": "Device's mapped AP/NSE/Switch Group updated"}}, False),
    ({"sys": {"online": True}, "config": {"synced": True, "syncReason": "Device is synchronized with mapped configuration"}}, False),
    ({"sys": {"online": False}, "config": {"synced": False, "syncReason": "Configuration failed: x"}}, False),
])
def test_resync_only_for_online_devices_with_a_failed_push(dev, expected):
    assert plan.needs_resync(dev) is expected


def test_prechecks_pass_for_a_clean_selection():
    assert plan.prechecks(plan.select(account(), "Main Office"), [], [], False) == []


def test_prechecks_stop_on_each_reason():
    sel = plan.select(account(device("ee", "Warehouse", "", "AP Profile"),
                              device("gg", "Main Office", "", "Mesh", model="XV2-2", fw="6.2-r14"),
                              device("sw", "Main Office", "", "Switch Profile", mode="sw")),
                      "Main Office", include_switches=True)
    stop = plan.prechecks(sel, ["profile 'P': 500"], ["sw", "not-moving"], False)
    assert len(stop) == 4
    assert "cloud export failed" in stop[0] and "profile 'P': 500" in stop[0]
    assert "AP Profile" in stop[1] and "ee" in stop[1]
    assert "port backup" in stop[2] and "'sw'" in stop[2] and "not-moving" not in stop[2]
    assert "XV2-2" in stop[3] and "gg" in stop[3] and "--allow-xv2-fw62" in stop[3]


def test_prechecks_stop_when_a_skipped_device_shares_an_affected_profile():
    sel = plan.select([device("aa", "N", "", "Shared"), device("sw", "N", "", "Shared", mode="sw")], "N")
    stop = plan.prechecks(sel, [], [], False)
    assert len(stop) == 1 and "Shared" in stop[0] and "sw" in stop[0] and "--include-switches" in stop[0]


def test_prechecks_accept_xv2_2_when_allowed():
    sel = plan.select([device("gg", "N", "", "Mesh", model="XV2-2", fw="6.2-r14")], "N")
    assert plan.prechecks(sel, [], [], True) == []


def test_only_an_incomplete_network_stops_the_chain():
    assert not plan.chain_stops("done")
    assert not plan.chain_stops("nothing")
    assert not plan.chain_stops("precheck_failed")
    assert not plan.chain_stops("dry_run")
    assert plan.chain_stops("incomplete")
