import io
import subprocess
import sys

import pytest
import requests

from cnmaestro_migrate import cli
from cnmaestro_migrate.cloud import Cloud, CloudError
from cnmaestro_migrate.onprem import ControllerError, ControllerLoginError
from fakes import FakeResp, FakeSession

ARGS = ["--cloud-url", "https://eu.example.com", "--account", "ACME", "--controller", "https://cnm.example.com",
        "--onprem-user", "u", "--device-address", "203.0.113.10", "--network", "Main Office"]


@pytest.fixture
def secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("CNM_SID", "S")
    monkeypatch.setenv("CNM_ONPREM_PASSWORD", "P")
    return ARGS + ["--workdir", str(tmp_path / "wd")]


class RecordingController:
    instances = []

    def __init__(self, *a, **kw):
        self.closed = 0
        RecordingController.instances.append(self)

    def close(self):
        self.closed += 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def test_confirm_returns_the_typed_text(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("ACME\n"))
    assert cli.confirm("type: ") == "ACME"


def test_confirm_treats_end_of_input_as_cancel(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert cli.confirm("type: ") == ""


def test_missing_secret_without_terminal_exits_2(monkeypatch, capsys):
    monkeypatch.delenv("CNM_SID", raising=False)
    monkeypatch.delenv("CNM_ONPREM_PASSWORD", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert cli.main(ARGS) == 2
    assert "CNM_SID" in capsys.readouterr().err


def test_login_failure_exits_2_and_closes(monkeypatch, capsys, secrets):
    RecordingController.instances.clear()
    monkeypatch.setattr(cli, "OnPrem", RecordingController)
    def fail(*a):
        raise ControllerLoginError("controller login failed with HTTP 504. wait a few minutes")
    monkeypatch.setattr(cli, "run", fail)
    assert cli.main(secrets) == 2
    assert "504" in capsys.readouterr().err
    assert RecordingController.instances[0].closed >= 1


def test_exit_code_of_the_run_is_returned(monkeypatch, secrets, tmp_path):
    monkeypatch.setattr(cli, "OnPrem", RecordingController)
    monkeypatch.setattr(cli, "run", lambda *a: 1)
    assert cli.main(secrets) == 1
    assert (tmp_path / "wd" / "logs").is_dir()


def test_module_help_runs():
    out = subprocess.run([sys.executable, "-m", "cnmaestro_migrate", "--help"], capture_output=True, text=True)
    assert out.returncode == 0 and "--device-address" in out.stdout and "CNM_SID" in out.stdout


class UnreadableStdin(io.StringIO):
    """stdin reopened write-only, as GNU nohup does when started from a terminal."""

    def readline(self, *a):
        raise OSError(9, "Bad file descriptor")


def test_confirm_treats_unreadable_input_as_cancel(monkeypatch):
    monkeypatch.setattr(sys, "stdin", UnreadableStdin())
    assert cli.confirm("type: ") == ""


def test_confirm_without_stdin_is_cancel(monkeypatch):
    monkeypatch.setattr(sys, "stdin", None)
    assert cli.confirm("type: ") == ""


def test_closed_stdin_with_missing_secret_exits_2(monkeypatch, capsys):
    monkeypatch.delenv("CNM_SID", raising=False)
    monkeypatch.setattr(sys, "stdin", None)
    assert cli.main(ARGS) == 2
    assert "CNM_SID" in capsys.readouterr().err


def test_html_from_the_cloud_exits_2_with_a_clear_message(monkeypatch, capsys, secrets):
    RecordingController.instances.clear()
    html = FakeSession(responses={"/tree/devices": FakeResp(text="<html>login</html>", content_type="text/html")})
    monkeypatch.setattr(cli, "Cloud", lambda *a: Cloud(*a, session=html))
    monkeypatch.setattr(cli, "OnPrem", RecordingController)
    assert cli.main(secrets) == 2
    err = capsys.readouterr().err
    assert "not JSON" in err and "Traceback" not in err
    assert RecordingController.instances[0].closed >= 1


@pytest.mark.parametrize("error", [CloudError(502, "cloud GET stats/sites: 502 Bad Gateway"),
                                   ControllerError(500, "PUT onboarding/devices/mac/aa: 500 boom"),
                                   requests.ConnectionError("controller unreachable")])
def test_errors_during_the_run_exit_2_and_log_out(monkeypatch, capsys, secrets, error):
    RecordingController.instances.clear()
    monkeypatch.setattr(cli, "OnPrem", RecordingController)
    def fail(*a):
        raise error
    monkeypatch.setattr(cli, "run", fail)
    assert cli.main(secrets) == 2
    assert str(error) in capsys.readouterr().err
    assert RecordingController.instances[0].closed >= 1
