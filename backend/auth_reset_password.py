"""CLI: reset the shared dashboard password.

Usage (interactive):
    cd /home/kotak/kotak-dashboard
    python -m backend.auth_reset_password
    # prompts for new password, twice, hidden via getpass.

Usage (non-interactive, used by tests):
    KOTAK_AUTH_FILE=/tmp/auth.json \\
      python -m backend.auth_reset_password --non-interactive
    # reads two lines from stdin instead of getpass.

Side effect: writes new hash to auth file AND bumps session_version,
so every existing logged-in browser is force-logged-out.
"""
import getpass
import sys

from backend.auth import hash_password
from backend.auth_storage import default_auth_path, read_auth, write_auth

MIN_PASSWORD_LEN = 8


def _prompt(non_interactive):
    if non_interactive:
        return input(), input()
    return (
        getpass.getpass("New password: "),
        getpass.getpass("Confirm new password: "),
    )


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    non_interactive = "--non-interactive" in argv

    pw1, pw2 = _prompt(non_interactive)
    if pw1 != pw2:
        print("Passwords do not match. Aborting.", file=sys.stderr)
        return 2
    if len(pw1) < MIN_PASSWORD_LEN:
        print(
            f"Password must be at least {MIN_PASSWORD_LEN} characters. Aborting.",
            file=sys.stderr,
        )
        return 3

    path = default_auth_path()
    state = read_auth(path)
    new_version = int(state.get("session_version", 0)) + 1
    write_auth(path, password_hash=hash_password(pw1), session_version=new_version)
    print(
        f"Password reset. session_version is now {new_version}. "
        f"All existing sessions are invalidated."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
