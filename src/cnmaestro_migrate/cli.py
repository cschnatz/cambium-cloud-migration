"""Command-line entry point."""
import atexit
import sys

import requests

from .cloud import Cloud, CloudAuthError, CloudError
from .config import ConfigError, load_settings
from .migrate import prepare_workdir, run
from .onprem import ControllerError, ControllerLoginError, ControllerSessionError, OnPrem

# Errors that end a run with a message instead of a traceback. The run is idempotent: the same command continues.
RUN_ERRORS = (ControllerLoginError, ControllerSessionError, CloudAuthError, CloudError, ControllerError,
              requests.RequestException)


def flush_input():
    """Drop keys pressed while waiting, so a stray Enter cannot answer the deletion prompt."""
    if not sys.stdin or not sys.stdin.isatty():
        return
    try:
        import termios
        termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except ImportError:                  # Windows
        import msvcrt
        while msvcrt.kbhit():
            msvcrt.getwch()


def confirm(prompt):
    """Ask for the account ID. Missing, closed or unreadable input counts as "no", never as consent."""
    if not sys.stdin:
        return ""
    flush_input()
    try:
        return input(prompt)
    except (EOFError, OSError):
        return ""


def main(argv=None):
    try:
        s = load_settings(argv)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    prepare_workdir(s.workdir)
    cloud = Cloud(s.cloud_host, s.account, s.sid)
    onprem = OnPrem(s.controller, s.onprem_user, s.onprem_password, verify=not s.insecure,
                    login_timeout=s.login_timeout)
    atexit.register(onprem.close)
    try:
        with onprem:
            return run(s, cloud, onprem, confirm)
    except RUN_ERRORS as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted — run the same command again to continue", file=sys.stderr)
        return 130
