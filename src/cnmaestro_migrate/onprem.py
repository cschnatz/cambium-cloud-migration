"""cnMaestro On-Premises 6.0, through the internal API its web UI uses (undocumented; see docs/api-notes.md).

Login rule: busy controllers took minutes to answer a login, and piling up sessions made it worse. This client
therefore logs in once, on first use, never retries a failed login, never logs in again mid-run, and logs out
when closed.
"""
import json
import time
import urllib.parse

import requests
import urllib3

from .paging import paged
from .plan import stale_sections, sync_job

PREFIX = "0"          # path prefix and x-cidx value the UI uses
RESOURCE = {"wlan": "policies", "profile": "profiles"}
LOGIN_ADVICE = ("The controller may be busy. Do not start again right away: wait a few minutes, "
                "and make sure no other migration or script is logged in to it.")


class ControllerError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class ControllerLoginError(Exception):
    """The single login of this run failed. Nothing has been changed on the controller by this client."""


class ControllerSessionError(Exception):
    """The controller no longer accepts this run's session."""


def _q(value):
    return urllib.parse.quote(value, safe="")


def _file_name(name):
    return "".join(c if c.isalnum() or c in "-_ ." else "_" for c in name) + ".json"


class OnPrem:
    def __init__(self, base_url, user, password, verify=True, login_timeout=400, session=None):
        self.base = base_url.rstrip("/")
        self._user, self._password = user, password
        self.login_timeout = login_timeout
        self.s = session or requests.Session()
        self.s.verify = verify
        if not verify:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self.s.headers.update({"Accept": "application/json, text/plain, */*", "User-Agent": "Mozilla/5.0"})
        self._state = "new"          # new → open → closed, or new → failed
        self._have = None

    # --- session ---------------------------------------------------------------

    def _login(self):
        if self._state == "failed":
            raise ControllerLoginError("the controller login already failed in this run; not trying again. " + LOGIN_ADVICE)
        if self._state == "closed":
            raise ControllerSessionError("the controller session of this run is closed")
        try:
            r = self.s.post(f"{self.base}/cn-srv/login", json={"username": self._user, "password": self._password},
                            timeout=self.login_timeout)
        except requests.RequestException as e:
            self._state = "failed"
            raise ControllerLoginError(f"controller login did not complete ({e}). {LOGIN_ADVICE}") from e
        if not r.ok:
            self._state = "failed"
            raise ControllerLoginError(f"controller login failed with HTTP {r.status_code}. {LOGIN_ADVICE}")
        self.s.headers.update({"Authorization": "Bearer " + r.json()["token"], "x-cidx": PREFIX})
        self._state = "open"
        self.s.get(f"{self.base}/cn-srv/user/me", timeout=30)     # sets XSRF-TOKEN; writes fail with 403 without it

    def _xsrf(self):
        token = self.s.cookies.get("XSRF-TOKEN")
        if token:
            self.s.headers["X-XSRF-TOKEN"] = token

    def call(self, method, path, **kw):
        if self._state != "open":
            self._login()
        self._xsrf()
        r = self.s.request(method, f"{self.base}/{PREFIX}/cn-srv/{path}", timeout=60, **kw)
        if r.status_code == 401:
            raise ControllerSessionError("the controller rejected this run's session (401). Run the same command "
                                         "again later; it continues where it stopped.")
        if not r.ok:
            raise ControllerError(r.status_code, f"{method} {path}: {r.status_code} {r.text[:200]}")
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else {}

    def close(self):
        """Log out, once. Nothing is sent if this run never logged in or the login failed."""
        if self._state != "open":
            return
        self._state = "closed"
        self._xsrf()
        try:
            self.s.post(f"{self.base}/cn-srv/logout", timeout=30)
        except requests.RequestException:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- configuration objects -----------------------------------------------------

    def _existing(self):
        if self._have is None:
            self._have = {
                "wlan": {p["name"] for p in paged(self.call, "config/policies?fields=name", "policies")},
                "profile": {p["name"] for p in paged(self.call, "config/profiles?fields=name", "profiles")},
                "network": {n["nid"] for n in paged(self.call, "stats/networks", "networks")},
                "site": {(t["nid"], t["tid"]) for t in paged(self.call, "stats/sites", "sites")},
            }
        return self._have

    @staticmethod
    def _key(step):
        return (step["network"], step["site"]) if step["kind"] == "site" else step["name"]

    def exists(self, step):
        return self._key(step) in self._existing()[step["kind"]]

    def stale(self, step):
        if step["kind"] not in RESOURCE:
            return {}
        current = self.call("GET", f"config/{RESOURCE[step['kind']]}/{_q(step['name'])}/export").get("data") or {}
        return stale_sections(current, step["payload"])

    def update(self, step, changes):
        """Partial update plus commit in the same session; an uncommitted edit locks the object for about an hour."""
        payload, name = step["payload"], _q(step["name"])
        body = {**changes, "types": payload.get("types")}
        if step["kind"] == "profile":
            path = f"config/profiles/{name}"
            body.setdefault("policies", payload.get("policies"))       # the controller answers 400 without it
        else:
            path = f"config/policies/{name}?fields=policy_type:wlan"
        try:
            self.call("PUT", path, json=body)
            self.call("PUT", "config/commit")
        except ControllerError:
            try:
                self.call("PUT", "config/rollback")
            except ControllerError:
                pass
            raise

    def create(self, step):
        kind, name = step["kind"], step["name"]
        if kind in RESOURCE:
            if kind == "wlan" and "policy_type" not in step["payload"]:
                raise ControllerError(None, f"WLAN {name!r} is not in the controller's import format")
            files = {"jsonFile": (_file_name(name), json.dumps(step["payload"]), "application/json")}
            for attempt in range(4):
                try:
                    self.call("POST", f"config/{RESOURCE[kind]}/import", data={"name": name}, files=files)
                    break
                except ControllerError as e:     # a WLAN imported a moment ago may not be known yet
                    if kind != "profile" or "linked WLANs" not in str(e) or attempt == 3:
                        raise
                    time.sleep(5 * (attempt + 1))
        elif kind == "network":
            self.call("POST", "config/network", json={"nid": name})
        elif kind == "site":
            self.call("POST", "config/site", json={"nid": step["network"], "tid": step["site"], "addr": "",
                                                   "zapHost": "", "loc": [0, 0]})
        if self._have is not None:
            self._have[kind].add(self._key(step))

    # --- onboarding and devices -----------------------------------------------------

    def queue(self):
        return paged(self.call, "onboarding/devices", "devices")

    def managed(self):
        return paged(self.call, "tree/devices", "devices")

    def assign(self, mac, payload):
        self.call("PUT", f"onboarding/devices/mac/{_q(mac)}/config", json=payload)

    def approve(self, mac):
        self.call("PUT", f"onboarding/devices/mac/{_q(mac)}", json={"isApproved": True})

    def ports(self, mac):
        return self.call("GET", f"stats/devices/{mac}/ports").get("data", {}).get("ports") or []

    def set_ports(self, entries):
        """Store port settings (merged by the controller); they reach the switch with the next sync job."""
        self.call("PUT", "config/ports", json={"ports": entries})

    def sync(self, macs):
        return bool(self.call("POST", "config/jobs", json=sync_job(macs)).get("data", {}).get("success"))
