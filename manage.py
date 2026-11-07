#!/usr/bin/env python3
"""
Cantinero administration from the command line.

    python3 manage.py probe host.docker.internal 5001   # can Cantinero reach it?
    python3 manage.py backends               # check KOBOLD_URL and COMFYUI_URL
    python3 manage.py users                  # list accounts
    python3 manage.py reset-password ADMIN   # prompts for the new password
    python3 manage.py reset-password alice --create   # …or makes an admin

In a container (compose):

    docker compose exec pantheon python3 manage.py backends
    docker compose exec -it pantheon python3 manage.py reset-password ADMIN

``--password-stdin`` reads the password from standard input instead of
prompting, for scripts. Changing a password does not sign out browsers that
are already signed in.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
USERS = HERE / "data" / "auth" / "users"


def probe(host: str, port: int) -> int:
    """
    Can this container open a TCP connection to ``host:port``? Tells "nothing
    listens there, as seen from here" apart from a backend that answers badly.
    """
    import socket

    try:
        addresses = sorted({info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
    except OSError as exc:
        print(f"{host}: name does not resolve here ({exc})")
        return 1
    try:
        socket.create_connection((host, port), timeout=5).close()
    except OSError as exc:
        print(f"{host} ({', '.join(addresses)}) port {port}: NOT reachable — {exc}")
        if "refused" in str(exc).lower():
            print("  Something answers at that address but nothing listens on the port —\n"
                  "  often a backend listening on 127.0.0.1 only. See INSTALL.md,\n"
                  "  \"Cantinero cannot reach KoboldCPP\".")
        return 1
    print(f"{host} ({', '.join(addresses)}) port {port}: reachable")
    return 0


def backends() -> int:
    """Ask each configured backend the question Pantheon's status dot asks."""
    import json
    import urllib.request

    checks = [
        ("KoboldCPP", os.getenv("KOBOLD_URL", "http://koboldcpp:5001"), "/api/v1/model", True),
        ("ComfyUI", os.getenv("COMFYUI_URL", "http://comfyui:8188"), "/system_stats", False),
    ]
    failed = False
    for label, base, path, required in checks:
        url = base.rstrip("/") + path
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                body = response.read(4096).decode("utf-8", "replace")
            detail = ""
            if path == "/api/v1/model":
                detail = " — model " + str(json.loads(body).get("result", "?"))
            print(f"{label:<9} {base}: OK{detail}")
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            note = "" if required else " (optional: only expression sprites need it)"
            print(f"{label:<9} {base}: NOT reachable — {exc}{note}")
            parts = urlsplit(base)
            if parts.hostname:
                print(f"          try: python3 manage.py probe {parts.hostname} {parts.port or 80}")
            failed = failed or required
    return 1 if failed else 0


def _load_users():
    sys.path.insert(0, str(HERE))
    from models.schema import User

    users = []
    for path in sorted(USERS.glob("*.json")):
        try:
            users.append(User.model_validate_json(path.read_text()))
        except Exception:  # noqa: BLE001 - the app skips unreadable files too
            pass
    return users


def users() -> int:
    for user in sorted(_load_users(), key=lambda u: u.username.lower()):
        print(f"{user.username:<24} {user.role:<6} created {user.created_at}")
    return 0


def reset_password(username: str, create: bool, from_stdin: bool) -> int:
    sys.path.insert(0, str(HERE))
    from models.schema import User
    from services import auth

    # Usernames match case-insensitively, as at login.
    user = next((u for u in _load_users() if u.username.lower() == username.strip().lower()), None)
    existed = user is not None
    if user is None:
        if not create:
            print(f"No user {username!r}; add --create to make an administrator.", file=sys.stderr)
            return 1
        user = User(username=username.strip(), role="admin")
    if from_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass(f"New password for {username}: ")
        if getpass.getpass("Again: ") != password:
            print("The passwords do not match.", file=sys.stderr)
            return 1
    if len(password) < 8:
        print("A password needs at least 8 characters.", file=sys.stderr)
        return 1

    user.salt = auth.new_salt()
    user.password_hash = auth.hash_password(password, user.salt)
    USERS.mkdir(parents=True, exist_ok=True)
    (USERS / f"{user.id}.json").write_text(user.model_dump_json(indent=2))
    print(f"Password changed for {user.username}." if existed else f"Created administrator {user.username}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="manage.py", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    probe_cmd = commands.add_parser("probe", help="check this container can reach host:port")
    probe_cmd.add_argument("host")
    probe_cmd.add_argument("port", type=int)
    commands.add_parser("backends", help="check KOBOLD_URL and COMFYUI_URL answer")
    commands.add_parser("users", help="list accounts")
    reset = commands.add_parser("reset-password", help="set a user's password")
    reset.add_argument("username")
    reset.add_argument("--create", action="store_true", help="create an administrator if missing")
    reset.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    args = parser.parse_args(argv)
    if args.command == "probe":
        return probe(args.host, args.port)
    if args.command == "backends":
        return backends()
    if args.command == "users":
        return users()
    return reset_password(args.username, args.create, args.password_stdin)


if __name__ == "__main__":
    sys.exit(main())
