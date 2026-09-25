import pytest

from cnmaestro_migrate import plan
from cnmaestro_migrate.cloud import Cloud, CloudAuthError, CloudError
from cnmaestro_migrate.paging import paged
from fakes import FakeResp, FakeSession, device

HOST = "eu-w1-s7-abc.cloud.example.com"
BASE = f"https://{HOST}/ACME/cn-srv"


def cloud(session):
    return Cloud(HOST, "ACME", "SID", session=session)


def test_session_carries_sid_account_and_base_path():
    fs = FakeSession()
    c = cloud(fs)
    assert c.base == BASE and fs.cookies.get("sid") == "SID"
    c.call("GET", "tree/devices")
    method, url, _, headers = fs.calls[-1]
    assert (method, url) == ("GET", f"{BASE}/tree/devices")
    assert headers["x-cidx"] == "ACME" and headers["X-Requested-With"] == "XMLHttpRequest"


def test_writes_carry_the_xsrf_token():
    fs = FakeSession()
    cloud(fs).commit()
    method, url, _, headers = fs.calls[-1]
    assert (method, url) == ("PUT", f"{BASE}/config/commit") and headers["X-XSRF-TOKEN"] == "XS"


def test_401_explains_sid_and_cluster():
    fs = FakeSession(responses={"/tree/devices": FakeResp(status=401, raw={})})
    with pytest.raises(CloudAuthError, match="CNM_SID") as e:
        cloud(fs).call("GET", "tree/devices")
    assert "--cloud-url" in str(e.value)


def test_non_json_answer_is_a_clear_error():
    fs = FakeSession(responses={"/tree/devices": FakeResp(text="<html>login</html>", content_type="text/html")})
    with pytest.raises(CloudError, match="not JSON"):
        cloud(fs).call("GET", "tree/devices")


def test_http_error_keeps_the_status():
    fs = FakeSession(responses={"/tree/devices": FakeResp(status=404, raw={"error": "gone"})})
    with pytest.raises(CloudError) as e:
        cloud(fs).call("GET", "tree/devices")
    assert e.value.status == 404


def test_paged_reads_until_a_short_page():
    pages = {0: [1] * 100, 100: [2] * 3}
    seen = []
    def call(method, path):
        seen.append(path)
        return {"data": {"rows": pages[int(path.split("offset=")[1])]}}
    assert len(paged(call, "stats/x?a=1", "rows")) == 103
    assert seen == ["stats/x?a=1&limit=100&offset=0", "stats/x?a=1&limit=100&offset=100"]


def test_read_account_exports_only_the_networks_profiles_and_their_wlans():
    fs = FakeSession(
        lists={"tree/devices": [device("aa", "Main Office", "", "AP"), device("bb", "Main Office", "", "AP2", online=False),
                                device("xx", "Warehouse", "", "W")],
               "config/profiles": [{"name": "AP"}, {"name": "AP2"}, {"name": "W"}],
               "config/policies": [{"name": "Guest"}], "stats/sites": [{"nid": "Main Office", "tid": "Lobby"}]},
        responses={"/config/profiles/AP/export": FakeResp(data={"policies": {"wlan": ["Guest"]}}),
                   "/config/profiles/AP2/export": FakeResp(status=500, raw={"error": "boom"}),
                   "/config/policies/Guest/export": FakeResp(data={"policy_type": "wlan"})})
    data = cloud(fs).read_account("Main Office")
    assert len(data.devices) == 3 and data.sites == [{"nid": "Main Office", "tid": "Lobby"}]
    assert data.profile_exports == {"AP": {"policies": {"wlan": ["Guest"]}}}
    assert data.wlan_exports == {"Guest": {"policy_type": "wlan"}}
    assert len(data.export_errors) == 1 and "AP2" in data.export_errors[0]
    assert not any("/config/profiles/W/export" in c[1] for c in fs.calls)


def test_set_override_reports_whether_it_changed():
    lines = plan.override_lines("wi-fi", "203.0.113.10")
    fresh = FakeSession(responses={"/config/profiles/AP%20One/export": FakeResp(data={"adv": None, "types": ["t"]})})
    assert cloud(fresh).set_override("AP One", lines) is True
    put = next(c for c in fresh.calls if c[0] == "PUT")
    assert put[1] == f"{BASE}/config/profiles/AP%20One" and put[2]["json"]["types"] == ["t"]
    done = FakeSession(responses={"/config/profiles/AP/export": FakeResp(data={"adv": plan.merge_adv(None, lines), "types": ["t"]})})
    assert cloud(done).set_override("AP", lines) is False
    assert not any(c[0] == "PUT" for c in done.calls)


def test_sync_config_posts_a_job():
    fs = FakeSession(responses={"/config/jobs": FakeResp(data={"success": True})})
    assert cloud(fs).sync_config(["aa"]) is True
    method, url, kw, _ = fs.calls[-1]
    assert (method, url, kw["json"]["devices"]) == ("POST", f"{BASE}/config/jobs", {"includeList": ["aa"]})


@pytest.mark.parametrize("answer", [FakeResp(data={"success": False}), FakeResp(status=400, raw={"error": "x"})])
def test_sync_config_failure_is_reported_not_raised(answer):
    assert cloud(FakeSession(responses={"/config/jobs": answer})).sync_config(["aa"]) is False


def test_ports_reads_the_port_list():
    fs = FakeSession(responses={"/stats/devices/AA/ports": FakeResp(data={"ports": [{"ifIndex": 1}]})})
    assert cloud(fs).ports("AA") == [{"ifIndex": 1}]


def test_delete_validates_first_then_bulk_deletes():
    fs = FakeSession(responses={"/onboarding/devices/validate-deletion": FakeResp(data={"invalid": {}})})
    c = cloud(fs)
    payload, check = c.validate_delete(["aa", "bb"])
    assert payload["includeList"] == ["aa", "bb"] and payload["isSingleDeviceDelete"] is False and check == {"invalid": {}}
    c.bulk_delete(payload)
    method, url, kw, _ = fs.calls[-1]
    assert url == f"{BASE}/onboarding/devices/ownership/bulk-operate"
    assert kw["json"]["includeList"] == ["aa", "bb"] and kw["json"]["deregisterCbrs"] is True


def test_still_present_treats_404_as_gone():
    fs = FakeSession(responses={"/stats/devices/aa": FakeResp(data={"devices": [{"mac": "aa"}]}),
                                "/stats/devices/bb": FakeResp(status=404, raw={})})
    assert cloud(fs).still_present(["aa", "bb"]) == {"aa"}
