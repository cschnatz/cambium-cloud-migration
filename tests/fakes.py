"""Test doubles for the two UI APIs. All data is made up."""
import json

import requests


class FakeResp:
    def __init__(self, status=200, data=None, raw=None, text=None, content_type="application/json"):
        self.status_code, self.ok = status, status < 400
        if text is not None:
            self._body, self.text = None, text
        else:
            self._body = raw if raw is not None else {"data": data if data is not None else {}}
            self.text = json.dumps(self._body)
        self.headers = {"content-type": content_type}

    def json(self):
        if self._body is None:
            raise ValueError("not JSON")
        return self._body


class FakeSession:
    """Records every request. Answers login, user/me (sets the XSRF cookie) and paged GET lists.

    lists: {"tree/devices": [rows]} — a GET whose URL contains the key returns the rows on offset 0.
    responses: {"/path/suffix": FakeResp or Exception} — checked first, matched against the URL without query.
    """

    def __init__(self, lists=None, responses=None):
        self.calls, self.headers = [], {}
        self.cookies = requests.cookies.RequestsCookieJar()
        self.lists, self.responses = lists or {}, responses or {}
        self.verify = True

    def post(self, url, **kw):
        return self.request("POST", url, **kw)

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw, dict(self.headers)))
        path = url.split("?")[0]
        for suffix, resp in self.responses.items():
            if path.endswith(suffix):
                if isinstance(resp, Exception):
                    raise resp
                return resp
        if path.endswith("/cn-srv/login"):
            return FakeResp(raw={"token": "T"})
        if path.endswith("/cn-srv/user/me"):
            self.cookies.set("XSRF-TOKEN", "XS")
            return FakeResp()
        if method == "GET":
            for key, rows in self.lists.items():
                if key in url:
                    name = url.split("/cn-srv/")[1].split("?")[0].split("/")[-1]
                    return FakeResp(data={name: rows if "offset=0" in url else []})
        return FakeResp()

    def writes(self):
        """Changing requests, without login and logout."""
        return [c for c in self.calls if c[0] in ("POST", "PUT", "DELETE")
                and not c[1].split("?")[0].endswith(("/cn-srv/login", "/cn-srv/logout"))]


def device(mac, nid, tid="", prof=None, mode="wi-fi", online=True, model="E410", fw="6.6.0"):
    """A cloud device row as returned by tree/devices, reduced to the fields the tool reads."""
    return {"mac": mac, "model": model, "mode": mode, "nid": nid, "tid": tid, "sys": {"online": online},
            "mgmt": {"actSw": fw}, "config": {"profile": prof} if prof else {}}
