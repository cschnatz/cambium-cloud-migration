import json

import pytest
import requests

from cnmaestro_migrate import onprem as onprem_mod
from cnmaestro_migrate.onprem import ControllerError, ControllerLoginError, ControllerSessionError, OnPrem
from fakes import FakeResp, FakeSession

URL = "https://cnm.example.com"


def controller(fs, **kw):
    return OnPrem(URL, "admin", "pw", session=fs, **kw)


def logins(fs):
    return sum(1 for c in fs.calls if c[1].endswith("/cn-srv/login"))


# --- login rule ---------------------------------------------------------------

def test_no_login_before_first_use():
    fs = FakeSession()
    controller(fs)
    assert fs.calls == []


def test_login_happens_once_for_many_calls():
    fs = FakeSession()
    o = controller(fs)
    o.create({"kind": "network", "name": "Main Office", "payload": None})
    o.queue()
    o.managed()
    o.sync(["aa"])
    assert logins(fs) == 1
    method, url, kw, headers = fs.calls[-1]
    assert url == f"{URL}/0/cn-srv/config/jobs"
    assert headers["Authorization"] == "Bearer T" and headers["x-cidx"] == "0" and headers["X-XSRF-TOKEN"] == "XS"


def test_login_uses_the_configured_timeout():
    fs = FakeSession()
    controller(fs, login_timeout=123).queue()
    login = next(c for c in fs.calls if c[1].endswith("/cn-srv/login"))
    assert login[2]["timeout"] == 123 and login[2]["json"] == {"username": "admin", "password": "pw"}


@pytest.mark.parametrize("answer", [requests.Timeout("slow"), requests.ConnectionError("refused"),
                                    FakeResp(status=504, raw={}),
                                    FakeResp(text="<html>proxy</html>", content_type="text/html"),
                                    FakeResp(raw={"error": "no token"})])
def test_failed_login_is_not_retried_by_later_calls(answer):
    fs = FakeSession(responses={"/cn-srv/login": answer})
    o = controller(fs)
    with pytest.raises(ControllerLoginError, match="wait"):
        o.queue()
    with pytest.raises(ControllerLoginError):
        o.managed()
    assert logins(fs) == 1 and len(fs.calls) == 1


def test_close_after_failed_login_sends_nothing():
    fs = FakeSession(responses={"/cn-srv/login": FakeResp(status=504, raw={})})
    o = controller(fs)
    with pytest.raises(ControllerLoginError):
        o.queue()
    o.close()
    assert len(fs.calls) == 1


def test_rejected_session_is_not_renewed():
    fs = FakeSession(responses={"/onboarding/devices": FakeResp(status=401, raw={})})
    o = controller(fs)
    with pytest.raises(ControllerSessionError, match="again"):
        o.queue()
    assert logins(fs) == 1


def test_close_without_login_sends_nothing():
    fs = FakeSession()
    controller(fs).close()
    assert fs.calls == []


def test_close_logs_out_once():
    fs = FakeSession()
    o = controller(fs)
    o.queue()
    o.close()
    o.close()
    assert sum(1 for c in fs.calls if c[1] == f"{URL}/cn-srv/logout") == 1
    assert fs.calls[-1][3]["X-XSRF-TOKEN"] == "XS"


def test_context_manager_logs_out_on_error():
    fs = FakeSession()
    with pytest.raises(ValueError):
        with controller(fs) as o:
            o.queue()
            raise ValueError("boom")
    assert fs.calls[-1][1] == f"{URL}/cn-srv/logout"


def test_tls_verification_on_by_default_and_off_on_request():
    on, off = FakeSession(), FakeSession()
    controller(on)
    controller(off, verify=False)
    assert on.verify is True and off.verify is False


# --- objects -------------------------------------------------------------------

def test_wlan_and_profile_are_multipart_imports():
    fs = FakeSession()
    o = controller(fs)
    o.create({"kind": "wlan", "name": "Guest", "payload": {"policy_type": "wlan", "src": {}}})
    o.create({"kind": "profile", "name": "AP", "payload": {"policies": {"wlan": ["Guest"]}}})
    (m1, u1, k1, _), (m2, u2, k2, _) = fs.calls[-2:]
    assert u1 == f"{URL}/0/cn-srv/config/policies/import" and k1["data"] == {"name": "Guest"}
    assert json.loads(k1["files"]["jsonFile"][1]) == {"policy_type": "wlan", "src": {}}
    assert u2 == f"{URL}/0/cn-srv/config/profiles/import" and k2["data"] == {"name": "AP"}


def test_wlan_without_import_format_is_rejected():
    with pytest.raises(ControllerError, match="import format"):
        controller(FakeSession()).create({"kind": "wlan", "name": "Guest", "payload": {"src": {}}})


def test_network_and_site_are_created():
    fs = FakeSession()
    o = controller(fs)
    o.create({"kind": "network", "name": "Main Office", "payload": None})
    o.create({"kind": "site", "name": "Main Office / Lobby", "network": "Main Office", "site": "Lobby", "payload": None})
    (_, u1, k1, _), (_, u2, k2, _) = fs.calls[-2:]
    assert u1.endswith("/0/cn-srv/config/network") and k1["json"] == {"nid": "Main Office"}
    assert u2.endswith("/0/cn-srv/config/site")
    assert k2["json"] == {"nid": "Main Office", "tid": "Lobby", "addr": "", "zapHost": "", "loc": [0, 0]}


def test_exists_knows_existing_objects():
    fs = FakeSession(lists={"config/policies": [{"name": "W1"}], "config/profiles": [{"name": "P1"}],
                            "stats/networks": [{"nid": "N1"}], "stats/sites": [{"nid": "N1", "tid": "S1"}]})
    o = controller(fs)
    assert o.exists({"kind": "wlan", "name": "W1"}) and not o.exists({"kind": "wlan", "name": "W2"})
    assert o.exists({"kind": "profile", "name": "P1"}) and o.exists({"kind": "network", "name": "N1"})
    assert o.exists({"kind": "site", "name": "N1 / S1", "network": "N1", "site": "S1"})
    o.create({"kind": "wlan", "name": "W2", "payload": {"policy_type": "wlan"}})
    assert o.exists({"kind": "wlan", "name": "W2"})


def test_stale_compares_with_the_controller_export():
    fs = FakeSession(responses={"/config/profiles/AP/export": FakeResp(data={"src": {"basic": {"a": 1}}})})
    o = controller(fs)
    assert o.stale({"kind": "profile", "name": "AP", "payload": {"src": {"basic": {"a": 2}}}}) == {"src": {"basic": {"a": 2}}}
    assert o.stale({"kind": "network", "name": "N", "payload": None}) == {}


def test_update_profile_partially_and_commit_in_the_same_session():
    fs = FakeSession()
    o = controller(fs)
    o.update({"kind": "profile", "name": "AP One", "payload": {"types": ["t"], "policies": {"wlan": ["W"]}}},
             {"src": {"radio": {"r": 1}}})
    puts = [(u.split("/cn-srv/")[1], kw.get("json")) for m, u, kw, _ in fs.calls if m == "PUT"]
    assert puts == [("config/profiles/AP%20One", {"src": {"radio": {"r": 1}}, "types": ["t"], "policies": {"wlan": ["W"]}}),
                    ("config/commit", None)]


def test_update_wlan_names_the_policy_type_in_the_fields_parameter():
    fs = FakeSession()
    controller(fs).update({"kind": "wlan", "name": "Guest", "payload": {"types": ["t"]}}, {"src": {"basic": {"ssid": "x"}}})
    puts = [(u.split("/cn-srv/")[1], kw.get("json")) for m, u, kw, _ in fs.calls if m == "PUT"]
    assert puts == [("config/policies/Guest?fields=policy_type:wlan", {"src": {"basic": {"ssid": "x"}}, "types": ["t"]}),
                    ("config/commit", None)]


def test_failed_update_rolls_back():
    fs = FakeSession(responses={"/config/profiles/AP": FakeResp(status=400, raw={"error": "bad"})})
    with pytest.raises(ControllerError):
        controller(fs).update({"kind": "profile", "name": "AP", "payload": {"types": []}}, {"src": {}})
    assert fs.calls[-1][1].endswith("/0/cn-srv/config/rollback")


def test_update_rolls_back_after_a_transport_failure():
    fs = FakeSession(responses={"/config/commit": requests.ReadTimeout("slow")})
    with pytest.raises(requests.ReadTimeout):
        controller(fs).update({"kind": "profile", "name": "AP", "payload": {"types": []}}, {"src": {}})
    assert fs.calls[-1][1].endswith("/0/cn-srv/config/rollback")


def test_html_answer_to_a_write_is_an_error():
    fs = FakeSession(responses={"/config": FakeResp(text="<html>login</html>", content_type="text/html")})
    with pytest.raises(ControllerError, match="HTML"):
        controller(fs).assign("00:00:5E:00:53:01", {"nid": "N"})


def test_empty_answer_to_a_write_is_fine():
    fs = FakeSession(responses={"/config": FakeResp(text="", content_type="text/plain")})
    controller(fs).assign("00:00:5E:00:53:01", {"nid": "N"})


def test_profile_import_waits_for_a_wlan_the_controller_does_not_know_yet(monkeypatch):
    o = controller(FakeSession())
    monkeypatch.setattr(onprem_mod.time, "sleep", lambda s: None)
    answers = [ControllerError(500, "POST config/profiles/import: 500 Could not find all linked WLANs."), {}]
    calls = []
    def call(method, path, **kw):
        calls.append(path)
        r = answers.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    o.call = call
    o.create({"kind": "profile", "name": "AP", "payload": {"policies": {"wlan": ["W"]}}})
    assert calls == ["config/profiles/import", "config/profiles/import"]


def test_other_import_errors_are_not_repeated():
    o = controller(FakeSession())
    def call(method, path, **kw):
        raise ControllerError(400, "POST config/profiles/import: 400 Invalid input")
    o.call = call
    with pytest.raises(ControllerError, match="Invalid input"):
        o.create({"kind": "profile", "name": "AP", "payload": {}})


# --- onboarding and ports ---------------------------------------------------------

def test_assign_and_approve_encode_the_mac():
    fs = FakeSession()
    o = controller(fs)
    o.assign("00:00:5E:00:53:01", {"nid": "N"})
    o.approve("00:00:5E:00:53:01")
    (_, u1, k1, _), (_, u2, k2, _) = fs.calls[-2:]
    assert u1.endswith("/0/cn-srv/onboarding/devices/mac/00%3A00%3A5E%3A00%3A53%3A01/config") and k1["json"] == {"nid": "N"}
    assert u2.endswith("/0/cn-srv/onboarding/devices/mac/00%3A00%3A5E%3A00%3A53%3A01") and k2["json"] == {"isApproved": True}


def test_ports_read_and_write():
    fs = FakeSession()
    o = controller(fs)
    o.set_ports([{"mac": "m", "pmac": "P", "config": {"network": {"vlans": "1"}}}])
    put = next(c for c in fs.calls if c[0] == "PUT")
    assert put[1].endswith("/0/cn-srv/config/ports")
    assert put[2]["json"] == {"ports": [{"mac": "m", "pmac": "P", "config": {"network": {"vlans": "1"}}}]}
    assert o.ports("AA") == []
    assert fs.calls[-1][1].endswith("/0/cn-srv/stats/devices/AA/ports")
