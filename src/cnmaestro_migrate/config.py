"""Command-line flags, environment and prompts → Settings.

Secrets (the cloud session cookie and the controller password) are never flags, so they do not end up in
shell history or process lists.
"""
import argparse
import getpass
import os
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

ENV_SID = "CNM_SID"
ENV_PASSWORD = "CNM_ONPREM_PASSWORD"


class ConfigError(Exception):
    """The settings are incomplete or unusable."""


@dataclass(frozen=True)
class Settings:
    cloud_host: str
    account: str
    sid: str
    controller: str
    onprem_user: str
    onprem_password: str
    device_address: str
    networks: tuple
    site: str | None
    include_switches: bool
    allow_xv2_fw62: bool
    wait_minutes: int
    login_timeout: int
    insecure: bool
    device_verify_cert: bool
    workdir: Path
    execute: bool


def build_parser():
    p = argparse.ArgumentParser(
        prog="cnmaestro-migrate", allow_abbrev=False,
        description="Move the devices of a cnMaestro Cloud network to a cnMaestro On-Premises 6.0 controller. "
                    "Without --execute nothing is changed.",
        epilog=f"Secrets: the cloud session cookie is read from {ENV_SID}, the controller password from "
               f"{ENV_PASSWORD}; missing values are prompted for without echo.")
    p.add_argument("--cloud-url", required=True, help="any URL of the cloud cluster that hosts the account, "
                                                      "e.g. copied from the browser address bar")
    p.add_argument("--account", required=True, help="Cambium ID of the cloud account")
    p.add_argument("--controller", required=True, help="base URL of the on-premises controller")
    p.add_argument("--onprem-user", required=True, help="controller user name")
    p.add_argument("--device-address", required=True, help="IP address or host name the devices will connect to")
    p.add_argument("--network", required=True, action="append",
                   help="cloud network name; repeat to migrate several networks with a single controller login")
    p.add_argument("--site", help="only devices of this site")
    p.add_argument("--include-switches", action="store_true", help="also move cnMatrix switches with their port VLANs")
    p.add_argument("--allow-xv2-fw62", action="store_true", help="move XV2-2 on firmware 6.2 despite the offline risk")
    p.add_argument("--wait-minutes", type=int, default=15, help="minutes per waiting step (default 15)")
    p.add_argument("--login-timeout", type=int, default=400,
                   help="seconds to wait for the controller login (default 400; busy controllers are slow)")
    p.add_argument("--insecure", action="store_true",
                   help="do not verify the controller's TLS certificate for the tool's own API calls")
    p.add_argument("--device-verify-cert", action="store_true",
                   help="keep certificate validation on the devices (only if they trust the controller certificate)")
    p.add_argument("--workdir", type=Path, default=Path("cnmaestro-migration"),
                   help="logs, snapshots and switch port backups (default ./cnmaestro-migration)")
    p.add_argument("--execute", action="store_true", help="really migrate (otherwise only show the plan)")
    return p


def _url(value, flag):
    parsed = urllib.parse.urlparse(value if "://" in value else "https://" + value)
    if not parsed.hostname:
        raise ConfigError(f"{flag}: cannot read a host name from {value!r}")
    return parsed


def _secret(env, name, label, prompt, interactive):
    value = env.get(name)
    if value:
        return value
    if not interactive:
        raise ConfigError(f"{label} missing: set {name}, or run in a terminal to be prompted")
    value = prompt(f"{label}: ")
    if not value:
        raise ConfigError(f"{label} is empty")
    return value


def load_settings(argv=None, env=None, prompt=getpass.getpass, interactive=None):
    a = build_parser().parse_args(argv)
    env = os.environ if env is None else env
    interactive = sys.stdin.isatty() if interactive is None else interactive
    cloud = _url(a.cloud_url, "--cloud-url")
    controller = _url(a.controller, "--controller")
    device = _url(a.device_address, "--device-address")
    return Settings(
        cloud_host=cloud.hostname,
        account=a.account,
        sid=_secret(env, ENV_SID, "cloud session cookie (sid)", prompt, interactive),
        controller=f"{controller.scheme}://{controller.netloc}",
        onprem_user=a.onprem_user,
        onprem_password=_secret(env, ENV_PASSWORD, "controller password", prompt, interactive),
        device_address=device.netloc,
        networks=tuple(a.network),
        site=a.site,
        include_switches=a.include_switches,
        allow_xv2_fw62=a.allow_xv2_fw62,
        wait_minutes=a.wait_minutes,
        login_timeout=a.login_timeout,
        insecure=a.insecure,
        device_verify_cert=a.device_verify_cert,
        workdir=a.workdir,
        execute=a.execute,
    )
