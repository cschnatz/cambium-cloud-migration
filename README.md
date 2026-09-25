# cnMaestro Cloud → On-Premises migration

Cambium only guarantees cnMaestro Cloud until 2026-10-01. `cnmaestro-migrate` moves the devices of one cloud
network to your own cnMaestro On-Premises 6.0 controller: it copies the WLANs and profiles, points the devices to
the controller, adopts them there and removes them from the cloud account.

> **Warning**
>
> - This tool uses **undocumented internal web-UI APIs** of cnMaestro Cloud and cnMaestro On-Premises 6.0. They can
>   change without notice.
> - The final step **deletes devices from the cloud account. This cannot be undone.**
> - After the cloud commit the cloud can no longer reach the moved devices.
> - This project is not affiliated with or endorsed by Cambium Networks.
> - It is provided as is, without warranty of any kind (MIT license).
> - Practise with a single non-critical device first.

## What you need

- Python 3.10 or newer.
- A running cnMaestro On-Premises 6.0 controller that the devices reach on TCP 443.
- A controller user with admin rights.
- Admin access to the cloud account.

## Install

```
pipx install git+https://github.com/cschnatz/cambium-cloud-migration
```

Or, inside a virtualenv:

```
pip install git+https://github.com/cschnatz/cambium-cloud-migration
```

## Prepare the controller

- **Device address.** Choose an address the devices reach directly: a public IP, or a DNS name that is **not**
  behind an HTTP proxy such as Cloudflare's proxied mode. Devices do not get through a proxy. Pass it as
  `--device-address`.
- **Cambium ID.** The controller's Cambium ID may contain only letters and digits. An underscore made the
  controller's API and UI answer 404.
- **Certificate.** The controller's default certificate is self-signed. The tool therefore turns certificate
  validation off on the devices, unless you pass `--device-verify-cert`. The tool's own API calls need `--insecure`
  until the controller has a trusted certificate.

## Get the cloud session cookie

The tool reads and changes the cloud with your browser session.

1. Log in to cnMaestro Cloud in the browser and switch to the account.
2. Open the developer tools → **Application** (Chrome, Edge) or **Storage** (Firefox) → **Cookies**.
3. Select the host of the cluster. The address bar shows it, e.g. `eu-w1-s7-….cloud.cambiumnetworks.com`.
4. Copy the value of `sid`.
5. `export CNM_SID='…'` (PowerShell: `$env:CNM_SID='…'`).

Use that same cluster address as `--cloud-url`. A cookie is valid only for its cluster. Log out of the cloud when
you are finished — that invalidates the cookie.

## Quickstart

Show the plan (dry run). **Without `--execute` nothing is changed.**

```
export CNM_SID='…'
export CNM_ONPREM_PASSWORD='…'        # or leave it out and type it when asked
cnmaestro-migrate \
  --cloud-url https://eu-w1-s7-abc.cloud.cambiumnetworks.com \
  --account ACME123 \
  --controller https://cnmaestro.example.com \
  --onprem-user admin \
  --device-address 203.0.113.10 \
  --insecure \
  --network "Main Office"
```

Migrate: the same command with `--execute`.

```
cnmaestro-migrate … --network "Main Office" --execute
```

Before the cloud deletion the tool asks you to type the account ID. For unattended runs, pipe it in:

```
echo ACME123 | cnmaestro-migrate … --execute
```

Anything other than the exact account ID — including an empty or closed input — cancels the deletion. There is
deliberately no `--yes` flag.

## Options

| Flag | Default | Meaning |
|---|---|---|
| `--cloud-url` | required | Any URL of the cloud cluster that hosts the account, e.g. copied from the browser address bar |
| `--account` | required | Cambium ID of the cloud account |
| `--controller` | required | Base URL of the on-premises controller |
| `--onprem-user` | required | Controller user name |
| `--device-address` | required | IP address or host name the devices will connect to (a scheme or path is removed) |
| `--network` | required | Cloud network name; repeat to migrate several networks with a single controller login |
| `--site` | all sites | Only devices of this site |
| `--include-switches` | off | Also move cnMatrix switches with their port VLANs |
| `--allow-xv2-fw62` | off | Move XV2-2 on firmware 6.2 despite the offline risk |
| `--wait-minutes` | `15` | Minutes per waiting step |
| `--login-timeout` | `400` | Seconds to wait for the controller login |
| `--insecure` | off | Do not verify the controller's TLS certificate for the tool's own API calls |
| `--device-verify-cert` | off | Keep certificate validation on the devices (only if they trust the controller certificate) |
| `--workdir` | `./cnmaestro-migration` | Logs, snapshots and switch port backups |
| `--execute` | off | Really migrate (otherwise only show the plan) |

| Environment variable | Meaning |
|---|---|
| `CNM_SID` | Cloud session cookie `sid` |
| `CNM_ONPREM_PASSWORD` | Controller password |

Secrets are never command-line flags. If a variable is missing and the tool runs in a terminal, it asks for the
value without echo.

## What happens

For each `--network`, with one cloud session and one controller session:

1. **Read fresh.** Devices, profiles, WLANs and sites of the account, plus single exports (the controller's import
   format) of the profiles and WLANs the network uses. A copy goes to `workdir/snapshots/<timestamp>/`.
2. **Back up switch ports** (only with `--include-switches`). Each switch's port list goes to
   `workdir/ports/<mac>.json`. If the cloud no longer returns the ports, an earlier backup is used.
3. **Show the plan.** See the table below.
4. **Pre-checks.** The run stops before any change if one of the [abort reasons](#abort-reasons) applies. Without
   `--execute` the run ends here.
5. **Prepare the controller.** WLANs → profiles → network → sites. Existing WLANs and profiles are brought up to the
   fresh cloud state in the changed sections only, and committed at once; on failure the tool rolls back.
6. **Switch the management address, in phases.** Access points and mesh clients → mesh bases → switches. The tool
   adds the controller address to each affected cloud profile's user-defined override and commits. Switches also
   get a cloud sync job, because they do not pick up a commit by themselves. Each phase starts only after the
   previous one has arrived in the controller's onboarding queue; otherwise the remaining phases stay in the cloud.
7. **Wait, assign, approve.** The tool waits for the devices in the onboarding queue, assigns network, site,
   profile and the device variables from the cloud (IP, gateway, DNS, name), writes switch ports and approves.
8. **Delete from the cloud.** The tool asks the cloud whether the devices may be deleted and shows the answer. It
   stops if the cloud reports blockers. Otherwise you type the account ID, and only the devices that **arrived** in
   the queue are deleted. The controller refuses devices still owned by a cloud account (`ERR_OWNER_DIFFERENT`), so
   this step is required.
9. **Verify adoption.** The tool waits until every device is managed, online and synced, in the right network, site
   and profile. A device whose configuration push failed gets another sync (at most three times). For switches it
   compares the stored and live port VLANs.

What the plan lines mean:

| Line | Meaning |
|---|---|
| `migrate now` | Online devices with a profile that move in this run, with their profile |
| `offline, stays in the cloud for now` | Offline devices; they get no push |
| `no profile, needs SSH or someone on site` | The address change goes through the profile, so it cannot reach these devices |
| `switch, only with --include-switches` | cnMatrix switches, see [Special cases](#special-cases) |
| `NOTE offline on an affected profile` | Offline devices that get the new address as soon as they come online |
| `cloud profiles that get the override` | These cloud profiles get the controller address |
| `controller … create` / `up to date` / `stale → update: …` | The object is created, left as it is, or brought up to the cloud state in the listed sections |
| `WARNING …` | Something to check by hand; the run continues |
| `ABORT …` | A reason why the run stops, see [Abort reasons](#abort-reasons) |

Each network ends with `==== RESULT <network>: <status>`:

| Status | Meaning |
|---|---|
| `done` | Every device arrived and is adopted correctly |
| `nothing` | Nothing to migrate |
| `precheck_failed` | Stopped before any change |
| `dry_run` | Plan shown, nothing changed |
| `incomplete` | Something is left half done; the remaining networks are not touched |

| Exit code | Meaning |
|---|---|
| `0` | Every network ended `done`, `nothing` or `dry_run` |
| `1` | A network ended `incomplete` or `precheck_failed` (a failed pre-check does not stop the following networks) |
| `2` | The run stopped on an error: settings missing or unusable, the controller login failed, a session was rejected, or the cloud or controller gave an unusable answer. The message says which; the same command continues where it stopped |
| `130` | Interrupted with Ctrl-C |

Every run writes a log to `workdir/logs/<account>_<network>_<timestamp>.log`. A repeated run with the same arguments
is safe: it continues where the previous one stopped and adopts devices that came online in the meantime.

## Controller logins

Busy controllers took one to several minutes to answer a login, and many open sessions made it worse. The tool
therefore logs in **once** per run and logs out at the end, also after errors and Ctrl-C.

- Put all networks into one call (`--network A --network B`) instead of looping in a shell.
- Never run two migrations against the same controller at the same time.
- A failed login is not retried. Wait a few minutes before you start again.
- `--login-timeout` defaults to 400 seconds for that reason.

## Abort reasons

The run stops **before it changes anything** when:

| Reason | What to do |
|---|---|
| A profile or WLAN export failed | Run again. If it persists, check the account in the cloud |
| A profile is also used by devices outside the selection | The override would move them too; the message lists them. Clone the profile in the cloud and move the other devices to the clone, or migrate the whole network without `--site` |
| A switch to be moved has no port backup | The cloud returned no ports. Switches are not moved without a backup |
| XV2-2 on firmware 6.2 | These went offline after a profile push in the field. Upgrade the firmware first, or pass `--allow-xv2-fw62` and watch them |
| Network not found | The message lists the networks that have devices |

During the run the tool stops without deleting anything if no device arrives in the onboarding queue.

## Special cases

- **Offline devices** get no push, but they are on the same profile as the moved devices. When they come online they
  pick up the new address and wait in the controller's onboarding queue. A later run with the same arguments adopts
  them. A site that is completely offline needs someone on site.
- **Devices without a profile** cannot be reached by the push. Point them to the controller via SSH (see
  [Recovery](#recovery)), then run the same command again.
- **Switches (cnMatrix)** move only with `--include-switches`. Their port settings live per port, not in the
  profile, so the tool backs them up to `workdir/ports/` and writes them on the controller. Move switches only
  with someone on site. Without `--include-switches` a switch that already waits in the controller's onboarding
  queue is neither adopted nor deleted from the cloud.
- **Mesh.** The order is access points and mesh clients → mesh bases → switches. The mesh role comes from the WLAN's
  mesh mode, not from profile names.
- **Names are kept 1:1.** Networks, sites, profiles and WLANs keep their cloud names. Use one cloud account per
  controller, or make names unique first. If an object with the same name already exists on the controller, it is
  updated to the cloud state — and the update is pushed to every device that uses it.

## Recovery

| Phase | State | What to do |
|---|---|---|
| Before the cloud commit | Only new objects on the controller | Nothing; the objects do no harm |
| After the commit, device did not arrive | The device reaches neither cloud nor controller | Point it back to the cloud via SSH (below). Do not delete it |
| Device in the queue, deletion declined | Waits unmanaged on the controller (`ERR_OWNER_DIFFERENT`) | Run the same command again later and confirm, or point it back to the cloud via SSH |
| After the deletion, not adopted | The device belongs to no cloud account | Check its entry in the controller's onboarding queue and wait for the controller's sync with Cambium (every five minutes) |

Point an E-series access point back to the cloud via SSH. The session starts in configuration mode; nothing takes
effect before `apply`:

```
management cambium-remote url https://cloud.cambiumnetworks.com
management cambium-remote validate-server-cert
apply
show management
```

Point it to the controller by hand:

```
no management cambium-remote validate-server-cert
management cambium-remote url https://<device-address>
apply
```

- Add `save` to keep the change after a reboot.
- The access point's admin password is in the profile export under `src.management.admin_password`; the snapshot in
  the workdir has it.
- To remove the override by hand in the cloud, the request must include the profile's `types`; otherwise the cloud
  rejects it.

## Security

- The workdir contains WLAN passphrases and device admin passwords in clear text. The tool creates it with mode
  0700. Never share or commit it.
- Pass secrets only through the environment or the prompt.
- Log out of the cloud afterwards.
- Rotate the device admin passwords after the migration.

## Development

```
pip install -e ".[test]"
pytest
```

The tests use made-up data and never touch a cloud or a controller. Notes on both internal APIs are in
[`docs/api-notes.md`](docs/api-notes.md).

## License

MIT, see [`LICENSE`](LICENSE).
