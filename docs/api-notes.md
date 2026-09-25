# API notes

## Scope and caveat

Both products are driven through the internal APIs their web UIs use. They are undocumented and can change without
notice. Everything here was observed on cnMaestro Cloud in September 2026 and on cnMaestro On-Premises 6.0.

## cnMaestro Cloud

- **Base:** `https://<cluster>/<CambiumID>/cn-srv/`
- **Authentication:** the browser's session cookie `sid` of that cluster, plus the header `x-cidx: <CambiumID>`.
- **Writes** need the header `X-XSRF-TOKEN` with the value of the `XSRF-TOKEN` cookie. A GET such as `user/me` sets
  the cookie.
- **Lists:** `tree/devices`, `config/profiles`, `config/policies` (WLANs), `stats/sites`; paged with `limit` and
  `offset`.
- **Single export:** `GET config/profiles/<name>/export`, `GET config/policies/<name>/export`. The `data` part is the
  controller's import format. The list format differs and cannot be imported.
- **Override:** `PUT config/profiles/<name>` with `{"adv": "!\n<CLI lines>\n!", "types": <the profile's types>}`, then
  `PUT config/commit`. Clearing `adv` also needs `types`; without them the cloud answers "Cannot update src without
  types".
- **Sync job** (the UI page "Sync Configuration"): `POST config/jobs` with
  `{"targets": [], "limit": 10, "error": false, "note": "", "state": 1, "overwrite": true, "desc": "…",
  "devices": {"includeList": [<MACs>]}}`; success in `data.success`. Switches need this job after a commit; they do
  not pick up a commit by themselves.
- **Switch ports:** `GET stats/devices/<mac>/ports`. Port VLANs live per port in `config.network`
  (`vlans` as list or text, `nativeVlan`, `accessMode`, `isNativeVlanTagged`), the device's live PVID in
  `nativeVlanId`. Also `config.security` and `config.physical`.
- **Deletion:** `POST onboarding/devices/validate-deletion` (changes nothing, reports blockers under `invalid`), then
  `POST onboarding/devices/ownership/bulk-operate`. A deleted device answers 404 on `stats/devices/<mac>`.

## cnMaestro On-Premises 6.0

- **Login:** `POST /cn-srv/login` with `{"username", "password"}` returns `token`.
- **Calls:** `/0/cn-srv/...` with `x-cidx: 0` and `Authorization: Bearer <token>`.
- **XSRF:** `GET /cn-srv/user/me` after the login sets the `XSRF-TOKEN` cookie. Writes without the matching
  `X-XSRF-TOKEN` header answer 403 `INVALID_CSRF`.
- **Logout:** `POST /cn-srv/logout`.
- **Imports:** `POST config/policies/import` and `POST config/profiles/import`, multipart with `name` and `jsonFile`
  (the `data` part of the cloud export). WLANs first: profiles reference WLANs by name. Right after a WLAN import a
  profile import can answer "Could not find all linked WLANs"; retry after a few seconds.
- **Network:** `POST config/network` with `{"nid"}`.
- **Site:** `POST config/site` with `{"nid", "tid", "addr", "zapHost", "loc": [0, 0]}`. `PUT config/sites/<nid>/<tid>`
  only edits and creates nothing.
- **Partial update:** `PUT config/profiles/<name>` with part of `src` plus `types` and `policies` (400 without
  `policies`); the controller merges `src`. WLANs: `PUT config/policies/<name>?fields=policy_type:wlan` with part of
  `src` plus `types`. Then `PUT config/commit` **in the same session**. Without a commit the object stays locked
  ("being edited by another user", about an hour); a commit from a new session answers "Database transaction not
  found". `PUT config/rollback` discards the edit.
- **Onboarding queue:** `GET onboarding/devices`.
- **Assign:** `PUT onboarding/devices/mac/<mac, URL-encoded>/config` with `name`, `nid`, `tid`, `isSite: true`,
  `autoIP` and `config`. Without `isSite` the controller looks for a tower and answers "Site/Tower … not found".
  Access points: `config = {tName, vars, wlanVars, overwrite: true}`; switches: `config = {tName, rules,
  overwrite: true}` plus `hw`, like the UI.
- **Approve:** `PUT onboarding/devices/mac/<mac>` with `{"isApproved": true}`.
- **Ownership:** until the device is deleted from its cloud account the controller reports `ERR_OWNER_DIFFERENT` and
  the UI shows "Device is claimed into another account". The controller checks again every five minutes.
- **Ports:** `PUT config/ports` with `{"ports": [{"mac": <port MAC>, "pmac": <switch MAC>, "config": {"network": …}}]}`,
  values as text. The controller merges them; they reach the switch with the next sync job (`POST config/jobs`).
- Unknown paths answer with the plain text `400 BAD REQUEST`.

## Device CLI

E-series access points (4.x). SSH starts in configuration mode; nothing takes effect before `apply`, and only `save`
keeps it after a reboot.

```
no management cambium-remote validate-server-cert
management cambium-remote url https://<device-address>
apply
show management
```

cnMatrix switches:

```
no cnmaestro validate-cert
cnmaestro url https://<device-address>
```

The tool writes exactly these lines into the profile's user-defined override (`adv`). Without
`--device-verify-cert` the certificate line is included.

## Known issues

- **Antenna gain on 5/6 GHz radios.** Cloud profiles can use `${ANTENNA_GAIN_24=…}` on the 5 GHz radio; the
  controller rejects that ("antenna gain level not supported"). `fix_antgain` uses the 5 GHz variable from the second
  radio on and keeps the default value.
- **"Verification of configuration change failed. Try syncing again."** The device rolls back by itself and stays
  online; another sync fixes it. The tool re-triggers the sync while it waits.
- **XV2-2 on firmware 6.2** went offline after a profile push.
- **Mesh role.** It is taken from the WLAN content (`src.basic.mesh_mode` = `base` / `client`). Profile names are not
  reliable.
- **Switches after the deletion** sometimes stayed offline on the controller. The cause is unknown; have someone on
  site.
- **Controller Cambium ID with `_`** breaks the controller's API and UI (404).
- **Slow controller logins** of one to several minutes on busy controllers.
