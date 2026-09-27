from cnmaestro_migrate.ports import port_config, port_mismatches, port_plan

SWITCH = "00:00:5E:00:53:00"


def cport(i, native=1, network=None, action=None, **extra):
    cfg = {"action": action or {"vlanName": ""}, "policy": ""}
    if network is not None:
        cfg["network"] = network
    cfg.update(extra)
    return {"ifIndex": i, "mac": f"00:00:5e:00:53:{i:02x}", "pmac": SWITCH, "nativeVlanId": native, "config": cfg}


def test_converts_cloud_format_to_controller_format():
    p = cport(3, native=85, network={"vlans": [85], "nativeVlan": 85, "accessMode": 1,
                                     "normalizedVlans": [{"start": 85, "end": 85}]})
    assert port_config(p) == {"network": {"vlans": "85", "nativeVlan": "85", "accessMode": 1}}


def test_trunk_with_text_list_and_tagging_is_kept():
    p = cport(4, network={"vlans": "1,10,42", "nativeVlan": "1", "accessMode": 2, "isNativeVlanTagged": False,
                          "normalizedVlans": []})
    assert port_config(p) == {"network": {"vlans": "1,10,42", "nativeVlan": "1", "accessMode": 2, "isNativeVlanTagged": False}}


def test_missing_native_vlan_takes_the_device_pvid():
    assert port_config(cport(5, native=172, network={"vlans": [172], "normalizedVlans": []})) == \
        {"network": {"vlans": "172", "nativeVlan": "172"}}
    # PVID ≠ 1 without any stored network section (set locally): still backed up
    assert port_config(cport(6, native=66)) == {"network": {"vlans": "66", "nativeVlan": "66"}}


def test_security_and_physical_are_kept():
    p = cport(7, network={"vlans": [1]}, security={"dhcpTrustStatus": 1}, physical={"poe": 2})
    assert port_config(p) == {"network": {"vlans": "1", "nativeVlan": "1"}, "security": {"dhcpTrustStatus": 1},
                              "physical": {"poe": 2}}


def test_default_and_empty_auto_attach_ports_are_skipped():
    assert port_config(cport(8)) is None
    assert port_config(cport(9, native=None)) is None
    # pure auto-attach with no stored VLANs: the switch configures it itself → skipped
    assert port_config(cport(10, action={"vlanName": "#CambiumAutoVlanClient_If10", "portMode": 4})) is None


def test_auto_attach_with_stored_vlans_is_applied():
    # Cambium AP uplink: auto-attach action but a real VLAN set must be applied, or the group overwrite
    # strips the port from its VLANs on adoption and the AP goes offline.
    auto = cport(1, native=10, network={"vlans": [1, 10, 11, 12, 14, 16], "nativeVlan": "10", "accessMode": 3,
                                        "normalizedVlans": [{"start": 1, "end": 1}]},
                 action={"vlanName": "#CambiumAutoVlanClient_If1", "portMode": 4, "vlanData": "1,10,11,12,14,16"})
    assert port_config(auto) == {"network": {"vlans": "1,10,11,12,14,16", "nativeVlan": "10", "accessMode": 3}}


def test_port_plan_builds_the_payload_per_switch():
    ports = [cport(1), cport(2, native=85, network={"vlans": [85], "nativeVlan": "85"})]
    assert port_plan(SWITCH, ports) == [
        {"mac": "00:00:5e:00:53:02", "pmac": SWITCH, "config": {"network": {"vlans": "85", "nativeVlan": "85"}}}]


def test_mismatches_compare_stored_and_live_values():
    plan = [{"mac": "a", "pmac": "P", "config": {"network": {"vlans": "85", "nativeVlan": "85"}}},
            {"mac": "b", "pmac": "P", "config": {"network": {"vlans": "1,10", "nativeVlan": "1", "accessMode": 2}}}]
    onprem = [{"mac": "a", "ifIndex": 1, "nativeVlanId": 85, "config": {"network": {"vlans": "85", "nativeVlan": "85"}}},
              {"mac": "b", "ifIndex": 2, "nativeVlanId": 1, "config": {"network": {"vlans": "10,1", "nativeVlan": "1", "accessMode": 2}}}]
    assert port_mismatches(plan, onprem) == []
    onprem[0]["nativeVlanId"] = 1                       # stored, but not yet on the device
    onprem[1]["config"] = {"action": {}}                # not stored at all
    assert port_mismatches(plan, onprem) == ["P1: device PVID 1 instead of 85", "P2: not stored on the controller"]
    onprem[0]["config"]["network"] = {"vlans": "1", "nativeVlan": "1"}
    assert port_mismatches(plan, onprem)[0] == "P1: stored 1/1 instead of 85/85"


def test_svi_sharing_a_port_mac_does_not_shadow_the_switchport():
    # cnMatrix gives Gi0/1 and the vlan/mgmt SVIs the same MAC; the SVI (no network) must not shadow the
    # real switchport, or a correctly applied port reads back as "not stored" and the sync loops forever.
    plan = [{"mac": "aa", "pmac": "P", "config": {"network": {"vlans": "1,10", "nativeVlan": "10", "accessMode": 3}}}]
    controller = [
        {"mac": "AA", "ifIndex": 1, "nativeVlanId": 10,
         "config": {"network": {"vlans": "10,1", "nativeVlan": "10", "accessMode": 3}}},   # Gi0/1, applied
        {"mac": "AA", "ifIndex": 62, "nativeVlanId": None, "config": {"action": {}}},        # vlan1 SVI, no network
        {"mac": "AA", "ifIndex": 63, "nativeVlanId": None, "config": {}},                    # vlan10 SVI, no network
    ]
    assert port_mismatches(plan, controller) == []
    # order-independent: SVIs listed before the switchport
    assert port_mismatches(plan, list(reversed(controller))) == []


def test_mismatches_compare_access_mode_and_native_tagging():
    plan = [{"mac": "a", "pmac": "P", "config": {"network": {"vlans": "1,10", "nativeVlan": "1", "accessMode": 2,
                                                             "isNativeVlanTagged": False}}}]
    onprem = [{"mac": "a", "ifIndex": 4, "nativeVlanId": 1,
               "config": {"network": {"vlans": "1,10", "nativeVlan": "1", "accessMode": "2", "isNativeVlanTagged": False}}}]
    assert port_mismatches(plan, onprem) == []
    onprem[0]["config"]["network"]["accessMode"] = 1
    onprem[0]["config"]["network"]["isNativeVlanTagged"] = True
    assert port_mismatches(plan, onprem) == ["P4: stored accessMode 1 instead of 2",
                                             "P4: stored isNativeVlanTagged True instead of False"]
