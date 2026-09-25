"""Command-line entry point."""
import atexit
import sys

from .cloud import Cloud, CloudAuthError
from .config import ConfigError, load_settings
from .migrate import prepare_workdir, run
from .onprem import ControllerLoginError, ControllerSessionError, OnPrem


def flush_input():
    """Drop keys pressed while waiting, so a stray Enter cannot answer the deletion prompt."""
    if not sys.stdin.isatty():
        return
    try:
        import termios
        termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except ImportError:                  # Windows
        import msvcrt
        while msvcrt.kbhit():
            msvcrt.getwch()


def confirm(prompt):
    """Ask for the account ID. End of input counts as "no", never as consent."""
    flush_input()
    try:
        return input(prompt)
    except EOFError:
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
    except (ControllerLoginError, ControllerSessionError, CloudAuthError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted — run the same command again to continue", file=sys.stderr)
        return 130
