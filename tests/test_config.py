from pathlib import Path

import pytest

from cnmaestro_migrate import config

BASE = ["--cloud-url", "https://eu-w1-s7-abc.cloud.cambiumnetworks.com/ACME123/#/dashboard", "--account", "ACME123",
        "--controller", "cnmaestro.example.com/", "--onprem-user", "admin", "--device-address", "203.0.113.10",
        "--network", "Main Office"]
ENV = {"CNM_SID": "s%3Asid", "CNM_ONPREM_PASSWORD": "pw"}


def no_prompt(label):
    raise AssertionError("must not prompt")


def load(argv, env=ENV, prompt=no_prompt, interactive=False):
    return config.load_settings(argv, env=env, prompt=prompt, interactive=interactive)


def test_settings_from_flags_and_environment(tmp_path):
    s = load(BASE + ["--workdir", str(tmp_path)])
    assert s.cloud_host == "eu-w1-s7-abc.cloud.cambiumnetworks.com"
    assert s.account == "ACME123"
    assert s.controller == "https://cnmaestro.example.com"
    assert s.onprem_user == "admin" and s.device_address == "203.0.113.10"
    assert s.sid == "s%3Asid" and s.onprem_password == "pw"
    assert s.networks == ("Main Office",) and s.site is None
    assert s.wait_minutes == 15 and s.login_timeout == 400
    assert not (s.execute or s.insecure or s.device_verify_cert or s.include_switches or s.allow_xv2_fw62)
    assert s.workdir == tmp_path


def test_default_workdir():
    assert load(BASE).workdir == Path("cnmaestro-migration")


def test_all_options():
    s = load(BASE + ["--network", "Warehouse", "--site", "Lobby", "--include-switches", "--allow-xv2-fw62",
                     "--wait-minutes", "5", "--login-timeout", "120", "--insecure", "--device-verify-cert", "--execute"])
    assert s.networks == ("Main Office", "Warehouse") and s.site == "Lobby"
    assert s.include_switches and s.allow_xv2_fw62 and s.insecure and s.device_verify_cert and s.execute
    assert (s.wait_minutes, s.login_timeout) == (5, 120)


def test_missing_secrets_are_prompted_in_a_terminal():
    asked = []
    s = load(BASE, env={}, prompt=lambda label: asked.append(label) or "secret", interactive=True)
    assert s.sid == "secret" and s.onprem_password == "secret" and len(asked) == 2


def test_environment_wins_over_the_prompt():
    assert load(BASE, env=ENV, prompt=no_prompt, interactive=True).sid == "s%3Asid"


def test_missing_secret_without_terminal_names_the_variable():
    with pytest.raises(config.ConfigError, match="CNM_ONPREM_PASSWORD"):
        load(BASE, env={"CNM_SID": "x"})


def test_empty_prompt_answer_is_an_error():
    with pytest.raises(config.ConfigError, match="empty"):
        load(BASE, env={}, prompt=lambda label: "", interactive=True)


@pytest.mark.parametrize("flag", ["--sid", "--password", "--onprem-password", "--onprem"])
def test_there_is_no_flag_for_secrets_and_no_abbreviation(flag):
    with pytest.raises(SystemExit):
        load(BASE + [flag, "x"])


@pytest.mark.parametrize("given, expected", [
    ("203.0.113.10", "203.0.113.10"),
    ("https://203.0.113.10/", "203.0.113.10"),
    ("cnm.example.com:8443", "cnm.example.com:8443"),
    ("https://cnm.example.com/some/path", "cnm.example.com"),
])
def test_device_address_is_normalised(given, expected):
    argv = [a if a != "203.0.113.10" else given for a in BASE]
    assert load(argv).device_address == expected


def test_cloud_url_without_scheme_is_accepted():
    argv = [a if not a.startswith("https://eu-w1") else "eu-w1-s7-abc.cloud.cambiumnetworks.com" for a in BASE]
    assert load(argv).cloud_host == "eu-w1-s7-abc.cloud.cambiumnetworks.com"


def test_unusable_url_is_an_error():
    argv = [a if a != "cnmaestro.example.com/" else "https://" for a in BASE]
    with pytest.raises(config.ConfigError, match="--controller"):
        load(argv)
