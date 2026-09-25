from cnmaestro_migrate import plan

ADDR = "203.0.113.10"


def test_override_lines_for_access_points_and_switches():
    assert plan.override_lines("wi-fi", ADDR) == ["no management cambium-remote validate-server-cert",
                                                  f"management cambium-remote url https://{ADDR}"]
    assert plan.override_lines("sw", ADDR) == ["no cnmaestro validate-cert", f"cnmaestro url https://{ADDR}"]


def test_override_lines_keep_certificate_validation_on_request():
    assert plan.override_lines("wi-fi", ADDR, verify_cert=True) == [f"management cambium-remote url https://{ADDR}"]
    assert plan.override_lines("sw", "cnm.example.com", verify_cert=True) == ["cnmaestro url https://cnm.example.com"]


def test_merge_adv_appends_and_keeps_existing_lines():
    old = "!\nwireless radio 1\nrates min-unicast 9\n!"
    new = plan.merge_adv(old, plan.override_lines("wi-fi", ADDR))
    assert new.startswith(old)
    assert f"management cambium-remote url https://{ADDR}" in new


def test_merge_adv_is_idempotent_and_replaces_an_old_address():
    once = plan.merge_adv(None, plan.override_lines("wi-fi", "198.51.100.1"))
    twice = plan.merge_adv(once, plan.override_lines("wi-fi", ADDR))
    assert twice.count("cambium-remote url") == 1 and ADDR in twice and "198.51.100.1" not in twice
    assert plan.merge_adv(twice, plan.override_lines("wi-fi", ADDR)) == twice


def test_fix_antgain_only_from_the_second_radio_on():
    src = {"radio": {"radio": [{"antgain": "${ANTENNA_GAIN_24=5}"}, {"antgain": "${ANTENNA_GAIN_24=5}"},
                               {"antgain": "${ANTENNA_GAIN_24=3}"}, {"antgain": "${ANTENNA_GAIN_5=5}"}]}, "basic": {"x": 1}}
    out = plan.fix_antgain({"src": src, "types": ["t"]})
    assert [r["antgain"] for r in out["src"]["radio"]["radio"]] == [
        "${ANTENNA_GAIN_24=5}", "${ANTENNA_GAIN_5=5}", "${ANTENNA_GAIN_5=3}", "${ANTENNA_GAIN_5=5}"]
    assert out["src"]["basic"] == {"x": 1} and out["types"] == ["t"]
    assert src["radio"]["radio"][1]["antgain"] == "${ANTENNA_GAIN_24=5}"      # input untouched


def test_fix_antgain_without_radio_list_is_unchanged():
    for p in ({"src": {"variant": 0}}, {"src": None}, {}):
        assert plan.fix_antgain(p) == p


def test_stale_sections_reports_only_changed_sections():
    current = {"src": {"basic": {"a": 1}, "radio": {"radio": [{"x": 1}]}}, "policies": {"wlan": ["A"]}, "adv": None}
    desired = {"src": {"basic": {"a": 2}, "radio": {"radio": [{"x": 1, "acs_poll_interval": "06:00"}]}},
               "policies": {"wlan": ["A"]},
               "adv": f"!\nno management cambium-remote validate-server-cert\nmanagement cambium-remote url https://{ADDR}\n!"}
    # radio: only a key the controller drops on import; adv: only the tool's own lines → neither counts
    assert plan.stale_sections(current, desired) == {"src": {"basic": {"a": 2}}}


def test_stale_sections_detects_policies_lists_and_customer_overrides():
    current = {"src": {"vlan": {"list": [1, 2]}}, "policies": {"wlan": ["A"]}, "adv": None}
    desired = {"src": {"vlan": {"list": [1, 2, 3]}}, "policies": {"wlan": ["A", "B"]}, "adv": "!\nsnmp enable\n!"}
    assert plan.stale_sections(current, desired) == {"src": {"vlan": {"list": [1, 2, 3]}}, "policies": {"wlan": ["A", "B"]},
                                                     "adv": "!\nsnmp enable\n!"}


def test_stale_sections_equal_is_empty():
    d = {"src": {"basic": {"a": 1}}, "policies": None, "adv": None}
    assert plan.stale_sections(d, d) == {}


def test_describe_changes():
    assert plan.describe_changes({"src": {"radio": {}, "basic": {}}, "policies": {}}) == ["src.radio", "src.basic", "policies"]


def test_sync_job_matches_the_ui():
    assert plan.sync_job(["00:00:5e:00:53:01"]) == {
        "targets": [], "limit": 10, "error": False, "note": "", "state": 1, "overwrite": True,
        "desc": "1 device(s)", "devices": {"includeList": ["00:00:5e:00:53:01"]}}
