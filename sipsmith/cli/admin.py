"""sipsmith-admin — administrative CLI.

Subcommands:
  create-admin   Bootstrap the first admin user. Consumes (and removes) the
                 one-time token written by the installer at
                 /etc/sipsmith/.bootstrap_token.

The CLI is the headless / recovery path. The browser path is the first-run
setup card on /login (see sipsmith/ui/router.py:setup_initial_admin).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

from sqlalchemy import select

from sipsmith.auth.service import hash_password
from sipsmith.database import AsyncSessionLocal
from sipsmith.models.user import Role, User

BOOTSTRAP_TOKEN_PATH = Path("/etc/sipsmith/.bootstrap_token")


async def _create_admin_async(
    username: str,
    email: str,
    password: str,
) -> None:
    async with AsyncSessionLocal() as db:
        existing = await db.scalar(select(User).where(User.username == username))
        if existing is not None:
            print(f"User {username!r} already exists; refusing to overwrite", file=sys.stderr)
            sys.exit(2)

        kwargs = {
            "username": username,
            "email": email,
            "hashed_password": hash_password(password),
            "role": Role.admin,
            "is_active": True,
        }
        # must_change_password is added by the onboarding migration; if the column
        # exists, force a reset on first login. If not, the default takes effect.
        if hasattr(User, "must_change_password"):
            kwargs["must_change_password"] = True

        db.add(User(**kwargs))
        await db.commit()
        print(f"Created admin user {username!r}")


def _cmd_create_admin(args: argparse.Namespace) -> int:
    if not args.skip_token_check:
        if not BOOTSTRAP_TOKEN_PATH.exists():
            print(
                f"No bootstrap token at {BOOTSTRAP_TOKEN_PATH}. If SIPsmith is "
                "already set up, the token has been consumed. Pass "
                "--skip-token-check to override (recovery only).",
                file=sys.stderr,
            )
            return 1
        expected = BOOTSTRAP_TOKEN_PATH.read_text(encoding="utf-8").strip()
        if not args.token or args.token.strip() != expected:
            print("Bootstrap token mismatch.", file=sys.stderr)
            return 1

    username = args.username
    email = args.email or f"{username}@localhost"
    password = args.password
    if password is None:
        password = getpass.getpass("Password: ")
        confirm = getpass.getpass("Confirm:  ")
        if password != confirm:
            print("Passwords do not match.", file=sys.stderr)
            return 1
    if len(password) < 8:
        print("Password must be at least 8 characters.", file=sys.stderr)
        return 1

    asyncio.run(_create_admin_async(username, email, password))

    if BOOTSTRAP_TOKEN_PATH.exists() and not args.skip_token_check:
        try:
            BOOTSTRAP_TOKEN_PATH.unlink()
            print(f"Removed {BOOTSTRAP_TOKEN_PATH}")
        except OSError as exc:
            print(f"Warning: could not remove {BOOTSTRAP_TOKEN_PATH}: {exc}", file=sys.stderr)

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sipsmith-admin")
    sub = parser.add_subparsers(dest="command", required=True)

    ca = sub.add_parser("create-admin", help="Bootstrap the first admin user")
    ca.add_argument("--username", default="admin")
    ca.add_argument("--email", default=None, help="Defaults to <username>@localhost")
    ca.add_argument("--password", default=None, help="If omitted, prompts interactively.")
    ca.add_argument(
        "--token", default=None, help=f"Bootstrap token from {BOOTSTRAP_TOKEN_PATH}."
    )
    ca.add_argument(
        "--skip-token-check",
        action="store_true",
        help="Skip bootstrap-token verification (recovery only).",
    )

    args = parser.parse_args(argv)
    if args.command == "create-admin":
        return _cmd_create_admin(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
