"""The migration of one or more networks, step by step.

Nothing is changed before the pre-checks pass. From the cloud commit on, the cloud can no longer reach the
moved devices; the cloud deletion at the end cannot be undone and needs the user to type the account ID.
"""
import datetime
import json
import os
import time
from pathlib import Path

from . import plan
from .cloud import CloudError
from .onprem import ControllerError
from .ports import port_mismatches, port_plan

POLL_SECONDS = 30


def safe_name(name):
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)


def prepare_workdir(path):
    """The workdir holds WLAN passphrases and device admin passwords in clear text: owner access only."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    for sub in ("logs", "snapshots", "ports"):
        (path / sub).mkdir(exist_ok=True)
    return path


class Log:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, msg):
        line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
        print(line, flush=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def _dump(path, obj):
    path.write_text(json.dumps(obj, indent=1, ensure_ascii=False), encoding="utf-8")


def write_snapshot(directory, data):
    """Keep what the cloud said before anything changes."""
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("devices", "profiles", "wlans", "sites"):
        _dump(directory / f"{name}.json", getattr(data, name))
    for sub, exports in (("profiles", data.profile_exports), ("wlans", data.wlan_exports)):
        (directory / sub).mkdir(exist_ok=True)
        for name, payload in exports.items():
            _dump(directory / sub / f"{safe_name(name)}.json", payload)


def backup_ports(cloud, directory, macs):
    """Save each switch's ports before any change; fall back to an earlier backup when the cloud no longer
    returns them (after the cloud deletion). Returns (port plans by switch MAC, switches without backup)."""
    directory.mkdir(parents=True, exist_ok=True)
    plans, missing = {}, []
    for mac in macs:
        f = directory / f"{safe_name(mac)}.json"
        try:
            ports = cloud.ports(mac)
        except CloudError:
            ports = []
        if ports:
            _dump(f, ports)
        elif f.exists():
            ports = json.loads(f.read_text(encoding="utf-8"))
        if ports:
            plans[mac] = port_plan(mac, ports)
        else:
            missing.append(mac)
    return plans, missing


def wait_for(log, label, macs, probe, minutes):
    """Poll probe() (→ set of MACs in the wanted state) until all of `macs` are there or time is up.
    Probes at least once, so a zero wait still reports the current state."""
    end, want = time.time() + minutes * 60, set(macs)
    while True:
        done = probe() & want
        log(f"{label}: {len(done)}/{len(want)}")
        if done == want or time.time() >= end:
            return done
        time.sleep(POLL_SECONDS)


def apply_steps(steps, onprem, log):
    """Create missing objects, bring existing WLANs and profiles up to the fresh cloud state, stop at the first error."""
    for i, st in enumerate(steps, 1):
        if onprem.exists(st):
            changes = onprem.stale(st)
            if changes:
                onprem.update(st, changes)
                log(f"  {i}/{len(steps)} updated: {st['kind']} {st['name']} ({', '.join(plan.describe_changes(changes))})")
            else:
                log(f"  {i}/{len(steps)} up to date: {st['kind']} {st['name']}")
        else:
            onprem.create(st)
            log(f"  {i}/{len(steps)} created: {st['kind']} {st['name']}")


def run(s, cloud, onprem, confirm):
    """Migrate every network of the settings with the same two sessions. Stops after an incomplete network.
    Exit code 1 if a network ended incomplete or failed its pre-checks."""
    failed = False
    for i, network in enumerate(s.networks):
        status = migrate_network(s, network, cloud, onprem, confirm)
        print(f"==== RESULT {network}: {status}", flush=True)
        failed = failed or status == "precheck_failed"
        if plan.chain_stops(status):
            rest = list(s.networks[i + 1:])
            if rest:
                print(f"==== STOPPED after {network} — networks not touched: {rest}", flush=True)
            return 1
    return 1 if failed else 0


def migrate_network(s, network, cloud, onprem, confirm):
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log = Log(s.workdir / "logs" / f"{safe_name(s.account)}_{safe_name(network)}_{stamp}.log")

    # 1. read fresh
    log(f"Reading cloud account {s.account} ...")
    data = cloud.read_account(network)
    write_snapshot(s.workdir / "snapshots" / stamp / safe_name(s.account), data)
    try:
        sel = plan.select(data.devices, network, s.site, s.include_switches)
    except plan.NetworkNotFound as e:
        log(f"ABORT: {e}")
        return "precheck_failed"
    steps = plan.config_steps(sel, data.sites, data.profile_exports, data.wlan_exports)
    by_mac = {d["mac"]: d for d in sel.devices}
    targets = {m: plan.target_of(d) for m, d in by_mac.items()}
    # A skipped switch may still wait in the queue from an earlier run; adopting it without its port backup
    # would lose its port VLANs, and the cloud deletion could not be undone.
    skipped = {d["mac"] for d in sel.skipped_switches}
    adoptable = set(by_mac) - skipped

    # 2. back up switch ports
    port_plans, port_missing = {}, []
    if s.include_switches:
        port_plans, port_missing = backup_ports(cloud, s.workdir / "ports",
                                                [d["mac"] for d in sel.devices if d.get("mode") == "sw"])

    # 3. show the plan
    log(f"Account {s.account} / network {sel.network}" + (f" / site {s.site}" if s.site else "")
        + f" → devices will use https://{s.device_address}")
    log(f"  migrate now: {len(sel.migrate)} device(s): "
        + ", ".join(f"{d['mac']} {d.get('model')} → {plan.profile_of(d)}" for d in sel.migrate))
    for label, rows in (("offline, stays in the cloud for now", sel.offline),
                        ("no profile, needs SSH or someone on site", sel.no_profile),
                        ("switch, only with --include-switches", sel.skipped_switches)):
        if rows:
            log(f"  {label}: " + ", ".join(f"{d['mac']} {d.get('model')}" for d in rows))
    if sel.follow_later:
        log("  NOTE offline on an affected profile — they get the new address when they come online and wait in the "
            "controller's onboarding queue; a later run with the same arguments adopts them: "
            + ", ".join(d["mac"] for d in sel.follow_later))
    for mac, pp in port_plans.items():
        log(f"  switch {mac}: ports backed up, {len(pp)} with own settings")
    log(f"  cloud profiles that get the override: {sorted(sel.profiles)}")
    for st in steps:
        if not onprem.exists(st):
            state = "create"
        else:
            changes = onprem.stale(st)
            state = f"stale → update: {', '.join(plan.describe_changes(changes))}" if changes else "up to date"
        log(f"  controller {st['kind']:8} {st['name']}: {state}")

    # 4. pre-checks — stop before anything is changed
    moving = {d["mac"] for d in sel.migrate}
    for mac in sorted(set(port_missing) - moving):
        log(f"  WARNING switch {mac}: no port backup — check its ports on the controller after adoption")
    stop = plan.prechecks(sel, data.export_errors, port_missing, s.allow_xv2_fw62)
    if stop:
        for reason in stop:
            log(f"ABORT: {reason}")
        return "precheck_failed"
    queued_now = lambda: {q["mac"] for q in onprem.queue()}
    if not sel.migrate and not queued_now() & adoptable:
        log("Nothing to migrate.")
        return "nothing"
    if not s.execute:
        log("Dry run — add --execute to migrate.")
        return "dry_run"

    # 5. prepare the controller
    apply_steps(steps, onprem, log)
    log("Controller configuration is in place.")

    # 6. switch the management address, phase by phase
    phases = plan.override_phases(sel.profiles, plan.mesh_roles(steps)) if sel.migrate else []
    if len(phases) > 1:
        log("  order: " + " → ".join(f"{label} ({', '.join(sorted(profs))})" for label, profs in phases))
    moved = []
    for i, (label, profs) in enumerate(phases):
        missing = sorted(set(moved) - queued_now())
        if missing:
            log(f"WARNING {label} NOT switched — the previous phase has not arrived yet: {missing}. "
                f"{label} stay in the cloud; run the same command again later.")
            break
        changed = False
        for prof, mode in profs.items():
            if cloud.set_override(prof, plan.override_lines(mode, s.device_address, s.device_verify_cert)):
                changed = True
                log(f"Cloud override set: {prof!r} ({mode})")
            else:
                log(f"Cloud override already present: {prof!r} ({mode})")
        macs = [d["mac"] for d in sel.migrate if plan.profile_of(d) in profs]
        moved += macs
        if changed:
            cloud.commit()
            log(f"Cloud commit ({label}) — devices fetch the new address. From now on the cloud cannot reach them.")
        elif label != plan.PHASE_SWITCHES and macs:
            # repeated run: the override is there but the devices still talk to the cloud → push again
            ok = cloud.sync_config(macs)
            log(f"No commit needed — sync job for devices not moved yet {'started' if ok else 'FAILED'}: {len(macs)}")
        if label == plan.PHASE_SWITCHES:
            if cloud.sync_config(macs):
                log(f"Cloud sync job for switches started: {macs}")
            else:
                log(f"WARNING cloud sync job for switches failed: {macs} — click \"Sync Config\" on the switch in the cloud UI")
        if i < len(phases) - 1:
            wait_for(log, f"{label} in the controller's onboarding queue (before {phases[i + 1][0]})", macs,
                     queued_now, s.wait_minutes)

    # 7. wait, assign, approve — every device of the selection in the queue, including stragglers of earlier runs
    wait_for(log, "in the controller's onboarding queue", moved, queued_now, s.wait_minutes)
    queued = queued_now()
    arrived = sorted(queued & adoptable)
    if queued & skipped:
        log(f"  NOTE switch(es) in the controller's onboarding queue, not adopted without --include-switches "
            f"(their port VLANs would be lost): {sorted(queued & skipped)}")
    for m in sorted(set(moved) - set(arrived)):
        log(f"  NOT arrived: {m} — do not delete it; check it via SSH or on site (README, Recovery)")
    if not arrived:
        log("No device arrived — stopping without deletion.")
        return "incomplete"
    no_site, ports_written = [], set()
    for m in arrived:
        payload = plan.onboard_payload(by_mac[m], targets[m], targets[m]["site"])
        try:
            onprem.assign(m, payload)
        except ControllerError as e:
            if not payload["tid"] or "not found" not in str(e):
                raise
            payload = {**payload, "tid": "", "isSite": False}
            onprem.assign(m, payload)
            no_site.append(m)
        if port_plans.get(m):
            try:
                onprem.set_ports(port_plans[m])      # before approval, so the first push already carries them
                ports_written.add(m)
                log(f"  ports stored before approval: {m} ({len(port_plans[m])})")
            except ControllerError as e:
                log(f"  ports not accepted yet ({str(e)[:80]}) — written after adoption")
        onprem.approve(m)
        log(f"Controller: assigned and approved {m} → network {payload['nid']!r}, "
            f"site {payload['tid'] or '(none)'!r}, profile {targets[m]['profile']!r}")
    if no_site:
        log(f"  NOTE assigned without site (the controller did not find the site): {no_site}")

    # 8. delete from the cloud: check, show, confirm, delete, look again
    payload, check = cloud.validate_delete(arrived)
    log(f"Cloud deletion check: {str(check)[:500]}")
    blockers = plan.deletion_blockers(check)
    if blockers:
        log(f"ABORT before deletion, the cloud reports: {blockers}")
        return "incomplete"
    print(f"\n{len(arrived)} device(s) will now be DELETED from cloud account {s.account} (cannot be undone): {arrived}")
    if no_site:
        print(f"Of these, assigned without site on the controller: {no_site}")
    if confirm(f"Type the account ID to confirm ({s.account}): ").strip() != s.account:
        log("Cloud deletion cancelled — the devices wait unmanaged in the controller's onboarding queue "
            "(ERR_OWNER_DIFFERENT). Run the same command again later.")
        return "incomplete"
    cloud.bulk_delete(payload)
    log(f"Cloud deletion started: {arrived}")
    gone = wait_for(log, "removed from the cloud", arrived, lambda: set(arrived) - cloud.still_present(arrived), 5)
    if set(arrived) - gone:
        log(f"  still in the cloud: {sorted(set(arrived) - gone)} — check in the cloud UI")

    # 9. verify adoption: network, site, profile, online, synced; switch ports
    resynced, port_state = {}, {}

    def adopted():
        devs = [d for d in onprem.managed() if d.get("mac") in targets]
        retry = [d["mac"] for d in devs if plan.needs_resync(d) and len(resynced.get(d["mac"], [])) < 3
                 and time.time() - max(resynced.get(d["mac"], [0])) > 180]
        if retry:
            onprem.sync(retry)
            for m in retry:
                resynced.setdefault(m, []).append(time.time())
            log(f"  controller sync triggered again: {retry}")
        good = {d["mac"] for d in devs if not plan.verify(d, targets[d["mac"]])}
        for m in sorted(good & set(port_plans)):
            pp = port_plans[m]
            if not pp:
                continue
            if m not in ports_written:
                onprem.set_ports(pp)
                ports_written.add(m)
                onprem.sync([m])
                log(f"  ports written and synced: {m} ({len(pp)} ports)")
                good.discard(m)
                continue
            port_state[m] = port_mismatches(pp, onprem.ports(m))
            if port_state[m]:
                good.discard(m)
                if len(resynced.get(m, [])) < 3 and time.time() - max(resynced.get(m, [0])) > 120:
                    onprem.sync([m])
                    resynced.setdefault(m, []).append(time.time())
                    log(f"  ports differ, sync triggered: {m}: {port_state[m][:5]}")
        return good

    done = wait_for(log, "correctly managed on the controller", arrived, adopted, s.wait_minutes + 10)
    managed = onprem.managed()
    for d in managed:
        if d.get("mac") in set(arrived) - done:
            log(f"  {d['mac']}: {plan.verify(d, targets[d['mac']])}")
    for m, mm in port_state.items():
        if mm and m not in done:
            log(f"  PORTS {m} differ — check them on the controller: {mm}")
    missing = set(arrived) - {d.get("mac") for d in managed}
    if missing:
        log(f"  not yet in the controller's device tree: {sorted(missing)} (it syncs with Cambium every 5 minutes)")
    log(f"DONE: {len(done)}/{len(arrived)} adopted correctly.")
    return "done" if len(done) == len(arrived) and not set(moved) - set(arrived) else "incomplete"
