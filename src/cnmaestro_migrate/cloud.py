"""cnMaestro Cloud, through the internal API its web UI uses (undocumented; see docs/api-notes.md).

Authentication is the browser's session cookie `sid` of the cluster that hosts the account.
"""
import time
import urllib.parse
from dataclasses import dataclass, field

import requests

from .paging import paged
from .plan import merge_adv, profile_of, sync_job

DELAY = 0.3          # pause after each read, to stay gentle with the cloud
UA = "Mozilla/5.0"
PROFILE_FIELDS = "name,description,types,mode,src,adv,policies,is_default,last_edited,deviceCount,macs,auto_sync"
WLAN_FIELDS = "name,mode,type,description,src,adv,vars_obj,uid,shared,deviceCount,last_edited,profiles"


class CloudError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class CloudAuthError(Exception):
    """The cloud rejected the session cookie."""


@dataclass
class AccountData:
    devices: list
    profiles: list
    wlans: list
    sites: list
    profile_exports: dict = field(default_factory=dict)    # name → export "data" (the controller's import format)
    wlan_exports: dict = field(default_factory=dict)
    export_errors: list = field(default_factory=list)


def _q(name):
    return urllib.parse.quote(name, safe="")


class Cloud:
    def __init__(self, host, account, sid, session=None):
        self.account = account
        self.base = f"https://{host}/{account}/cn-srv"
        self.s = session or requests.Session()
        self.s.headers.update({"Accept": "application/json, text/plain, */*", "User-Agent": UA, "x-cidx": account,
                               "Referer": f"https://{host}/", "X-Requested-With": "XMLHttpRequest"})
        self.s.cookies.set("sid", sid, domain=host)

    def call(self, method, path, **kw):
        if not self.s.cookies.get("XSRF-TOKEN"):
            self.s.get(f"{self.base}/user/me", timeout=30)      # sets the XSRF cookie that writes need
        token = self.s.cookies.get("XSRF-TOKEN")
        if token:
            self.s.headers["X-XSRF-TOKEN"] = token
        r = self.s.request(method, f"{self.base}/{path}", timeout=60, **kw)
        if method == "GET":
            time.sleep(DELAY)
        if r.status_code == 401:
            raise CloudAuthError("the cloud rejected the session cookie (401). Log in to the cloud in the browser, "
                                 "copy a fresh sid into CNM_SID, and check that --cloud-url is the cluster that "
                                 "cookie belongs to.")
        if not r.ok:
            raise CloudError(r.status_code, f"cloud {method} {path}: {r.status_code} {r.text[:200]}")
        if not r.text:
            return {}
        try:
            return r.json()
        except ValueError:
            raise CloudError(r.status_code, f"cloud {method} {path}: answer is not JSON — is --cloud-url the "
                                            "cluster host that serves the account?") from None

    def read_account(self, network):
        """Lists of the whole account, plus single exports of the profiles used in `network` and their WLANs."""
        data = AccountData(
            devices=paged(self.call, "tree/devices", "devices"),
            profiles=paged(self.call, f"config/profiles?sortedBy=name&fields={PROFILE_FIELDS}", "profiles"),
            wlans=paged(self.call, f"config/policies?sortedBy=name&fields={WLAN_FIELDS}", "policies"),
            sites=paged(self.call, "stats/sites", "sites"))
        wanted = {profile_of(d) for d in data.devices if d.get("nid") == network} - {""}
        for name in sorted(wanted):
            self._export(data, "profile", "profiles", name, data.profile_exports)
        wlans = {w for p in data.profile_exports.values() for w in ((p.get("policies") or {}).get("wlan") or [])}
        for name in sorted(wlans):
            self._export(data, "WLAN", "policies", name, data.wlan_exports)
        return data

    def _export(self, data, label, resource, name, into):
        try:
            into[name] = self.call("GET", f"config/{resource}/{_q(name)}/export")["data"]
        except CloudError as e:
            data.export_errors.append(f"{label} {name!r}: {e}")

    def set_override(self, profile, lines):
        """Put the controller address into the profile's override. False if it was already there."""
        data = self.call("GET", f"config/profiles/{_q(profile)}/export")["data"]
        adv = merge_adv(data.get("adv"), lines)
        if adv == (data.get("adv") or ""):
            return False
        self.call("PUT", f"config/profiles/{_q(profile)}", json={"adv": adv, "types": data["types"]})
        return True

    def commit(self):
        self.call("PUT", "config/commit")

    def sync_config(self, macs):
        """Push the configuration to the devices now. Switches do not pick up a commit by themselves.
        A failure is only reported: the commit has already gone out."""
        try:
            return bool(self.call("POST", "config/jobs", json=sync_job(macs)).get("data", {}).get("success"))
        except CloudError:
            return False

    def ports(self, mac):
        return self.call("GET", f"stats/devices/{mac}/ports").get("data", {}).get("ports") or []

    def validate_delete(self, macs):
        """First half of the deletion, as in the UI: validate-deletion changes nothing."""
        payload = {"action": "deleteOnboardingDevice", "includeList": list(macs), "includeSns": [], "excludeList": [],
                   "search": {}, "isSingleDeviceDelete": len(macs) == 1}
        return payload, self.call("POST", "onboarding/devices/validate-deletion", json=payload).get("data")

    def bulk_delete(self, payload):
        """Second half: remove the devices from the account. Cannot be undone."""
        self.call("POST", "onboarding/devices/ownership/bulk-operate",
                  json={**payload, "deleteCbrsClaimedChildDevice": True, "deletePTokenOnboardedChildDevices": True,
                        "deregisterCbrs": True})

    def still_present(self, macs):
        present = set()
        for m in macs:
            try:
                if self.call("GET", f"stats/devices/{m}").get("data", {}).get("devices"):
                    present.add(m)
            except CloudError as e:
                if e.status != 404:          # 404: no longer in the account
                    raise
        return present
