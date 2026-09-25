import json
import stat

from cnmaestro_migrate import migrate
from cnmaestro_migrate.cloud import AccountData
from cnmaestro_migrate.config import Settings
from cnmaestro_migrate.onprem import OnPrem
from cnmaestro_migrate.plan import is_online, profile_of
from fakes import FakeSession, device


def settings(tmp_path, **kw):
    base = dict(cloud_host="eu.example.com", account="ACME", sid="S", controller="https://cnm.example.com",
                onprem_user="u", onprem_password="p", device_address="203.0.113.10", networks=("Main Office",),
                site=None, include_switches=False, allow_xv2_fw62=False, wait_minutes=0, login_timeout=400,
                insecure=False, device_verify_cert=False, workdir=tmp_path, execute=True)
    base.update(kw)
    return Settings(**base)


def account(*devices):
    devices = list(devices) or [device("aa", "Main Office", "Lobby", "AP")]
    profiles = {profile_of(d) for d in devices} - {""}
    return AccountData(devices=devices, profiles=[{"name": p} for p in sorted(profiles)], wlans=[{"name": "Guest"}],
                       sites=[{"nid": "Main Office", "tid": "Lobby"}],
                       profile_exports={p: {"policies": {"wlan": ["Guest"]}, "types": []} for p in profiles},
                       wlan_exports={"Guest": {"policy_type": "wlan", "src": {"basic": {}}}})


class FakeController:
    def __init__(self):
        self.calls, self.queued, self.have, self.assigned = [], set(), set(), {}

    def exists(self, step):
        return step["name"] in self.have

    def stale(self, step):
        return {}

    def create(self, step):
        self.calls.append(("create", step["kind"], step["name"]))
        self.have.add(step["name"])

    def update(self, step, changes):
        self.calls.append(("update", step["name"]))

    def queue(self):
        return [{"mac": m} for m in sorted(self.queued)]

    def assign(self, mac, payload):
        self.calls.append(("assign", mac))
        self.assigned[mac] = payload

    def approve(self, mac):
        self.calls.append(("approve", mac))

    def managed(self):
        return [{"mac": m, "nid": p["nid"], "tid": p["tid"], "sys": {"online": True},
                 "config": {"profile": p["config"]["tName"], "synced": True}} for m, p in self.assigned.items()]

    def set_ports(self, entries):
        self.calls.append(("ports", len(entries)))

    def ports(self, mac):
        return []

    def sync(self, macs):
        self.calls.append(("sync", tuple(macs)))
        return True


class FakeCloud:
    """Devices on a profile with the override appear in the controller queue after the commit, if `arrive`."""

    def __init__(self, data, controller, arrive=True, ports=None):
        self.data, self.controller, self.arrive, self.port_rows = data, controller, arrive, ports
        self.calls, self.overrides, self.deleted = [], {}, set()

    def read_account(self, network):
        self.calls.append(("read", network))
        return self.data

    def set_override(self, profile, lines):
        self.calls.append(("override", profile, tuple(lines)))
        changed = self.overrides.get(profile) != lines
        self.overrides[profile] = lines
        return changed

    def commit(self):
        self.calls.append(("commit",))
        if self.arrive:
            self.controller.queued |= {d["mac"] for d in self.data.devices
                                       if profile_of(d) in self.overrides and is_online(d)}

    def sync_config(self, macs):
        self.calls.append(("sync", tuple(macs)))
        return True

    def ports(self, mac):
        return self.port_rows or []

    def validate_delete(self, macs):
        self.calls.append(("validate", tuple(macs)))
        return {"includeList": list(macs)}, {"invalid": {}}

    def bulk_delete(self, payload):
        self.calls.append(("delete", tuple(payload["includeList"])))
        self.deleted |= set(payload["includeList"])

    def still_present(self, macs):
        return set(macs) - self.deleted


def typed(answer):
    prompts = []
    def confirm(prompt):
        prompts.append(prompt)
        return answer
    confirm.prompts = prompts
    return confirm


def kinds(calls):
    return [c[0] for c in calls]


# --- workdir ---------------------------------------------------------------------

def test_workdir_is_private(tmp_path):
    wd = migrate.prepare_workdir(tmp_path / "wd")
    assert stat.S_IMODE(wd.stat().st_mode) == 0o700
    assert all((wd / sub).is_dir() for sub in ("logs", "snapshots", "ports"))


def test_port_backup_falls_back_to_an_earlier_file(tmp_path):
    port = {"ifIndex": 2, "mac": "p2", "nativeVlanId": 85, "config": {"network": {"vlans": [85]}}}
    (tmp_path / "sw2.json").write_text(json.dumps([port]))
    cloud = FakeCloud(account(), FakeController(), ports=None)
    plans, missing = migrate.backup_ports(cloud, tmp_path, ["sw1", "sw2"])
    assert missing == ["sw1"] and plans["sw2"][0]["config"]["network"]["nativeVlan"] == "85"


# --- one network -----------------------------------------------------------------

def test_dry_run_changes_nothing(tmp_path):
    c = FakeController()
    cloud = FakeCloud(account(), c)
    assert migrate.migrate_network(settings(tmp_path, execute=False), "Main Office", cloud, c, typed("ACME")) == "dry_run"
    assert kinds(cloud.calls) == ["read"] and c.calls == []
    assert list((tmp_path / "logs").iterdir()) and list((tmp_path / "snapshots").rglob("devices.json"))


def test_failed_precheck_changes_nothing(tmp_path):
    c = FakeController()
    cloud = FakeCloud(account(device("aa", "Main Office", "Lobby", "AP"), device("zz", "Warehouse", "", "AP")), c)
    assert migrate.migrate_network(settings(tmp_path), "Main Office", cloud, c, typed("ACME")) == "precheck_failed"
    assert kinds(cloud.calls) == ["read"] and c.calls == []


def test_unknown_network_is_a_failed_precheck(tmp_path):
    c = FakeController()
    assert migrate.migrate_network(settings(tmp_path), "Nowhere", FakeCloud(account(), c), c, typed("ACME")) == "precheck_failed"


def test_happy_path_order(tmp_path):
    c = FakeController()
    cloud = FakeCloud(account(), c)
    confirm = typed("ACME")
    assert migrate.migrate_network(settings(tmp_path), "Main Office", cloud, c, confirm) == "done"
    assert c.calls[:4] == [("create", "wlan", "Guest"), ("create", "profile", "AP"), ("create", "network", "Main Office"),
                           ("create", "site", "Main Office / Lobby")]
    assert c.calls[4:] == [("assign", "aa"), ("approve", "aa")]
    assert kinds(cloud.calls) == ["read", "override", "commit", "validate", "delete"]
    assert cloud.calls[1][2] == ("no management cambium-remote validate-server-cert",
                                 "management cambium-remote url https://203.0.113.10")
    assert c.assigned["aa"]["nid"] == "Main Office" and c.assigned["aa"]["tid"] == "Lobby" and c.assigned["aa"]["isSite"]
    assert "ACME" in confirm.prompts[0]


def test_device_certificate_validation_can_stay_on(tmp_path):
    c = FakeController()
    cloud = FakeCloud(account(), c)
    migrate.migrate_network(settings(tmp_path, device_verify_cert=True), "Main Office", cloud, c, typed("ACME"))
    assert cloud.calls[1][2] == ("management cambium-remote url https://203.0.113.10",)


def test_wrong_account_typed_deletes_nothing(tmp_path):
    c = FakeController()
    cloud = FakeCloud(account(), c)
    assert migrate.migrate_network(settings(tmp_path), "Main Office", cloud, c, typed("acme")) == "incomplete"
    assert "delete" not in kinds(cloud.calls)


def test_device_that_did_not_arrive_is_not_deleted(tmp_path):
    c = FakeController()
    cloud = FakeCloud(account(), c, arrive=False)
    assert migrate.migrate_network(settings(tmp_path), "Main Office", cloud, c, typed("ACME")) == "incomplete"
    assert "validate" not in kinds(cloud.calls) and "delete" not in kinds(cloud.calls)
    assert not any(k == "assign" for k, *_ in c.calls)


def test_switches_wait_until_the_access_points_arrived(tmp_path):
    c = FakeController()
    port = {"ifIndex": 1, "mac": "p1", "nativeVlanId": 1, "config": {}}
    data = account(device("aa", "Main Office", "Lobby", "AP"), device("sw", "Main Office", "Lobby", "Switch", mode="sw"))
    cloud = FakeCloud(data, c, arrive=False, ports=[port])
    status = migrate.migrate_network(settings(tmp_path, include_switches=True), "Main Office", cloud, c, typed("ACME"))
    assert status == "incomplete"
    assert [x[1] for x in cloud.calls if x[0] == "override"] == ["AP"]


def test_devices_already_waiting_are_adopted_without_a_new_override(tmp_path):
    c = FakeController()
    c.queued.add("bb")                                   # came online after an earlier run
    cloud = FakeCloud(account(device("bb", "Main Office", "Lobby", "AP", online=False)), c)
    assert migrate.migrate_network(settings(tmp_path), "Main Office", cloud, c, typed("ACME")) == "done"
    assert "override" not in kinds(cloud.calls) and ("delete", ("bb",)) in cloud.calls


# --- several networks ---------------------------------------------------------------

def test_incomplete_network_stops_the_chain(tmp_path):
    c = FakeController()
    cloud = FakeCloud(account(), c, arrive=False)
    s = settings(tmp_path, networks=("Main Office", "Warehouse"))
    assert migrate.run(s, cloud, c, typed("ACME")) == 1
    assert [x for x in cloud.calls if x[0] == "read"] == [("read", "Main Office")]


def test_two_networks_share_one_controller_login(tmp_path):
    fs = FakeSession()
    controller = OnPrem("https://cnm.example.com", "u", "p", session=fs)
    data = account(device("aa", "Main Office", "", "AP"), device("bb", "Warehouse", "", "W"))
    s = settings(tmp_path, networks=("Main Office", "Warehouse"), execute=False)
    assert migrate.run(s, FakeCloud(data, controller), controller, typed("ACME")) == 0
    assert sum(1 for call in fs.calls if call[1].endswith("/cn-srv/login")) == 1
