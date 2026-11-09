"""Admin commands.

    python -m app.cli create-platform-admin --email you@example.com [--name "Your Name"]

The password is asked for interactively (or read from PLATFORM_ADMIN_PASSWORD).
"""

import argparse
import asyncio
import getpass
import os
import sys

from sqlalchemy import func, select

from app.config import get_settings
from app.db.models import Base, PlatformAdmin
from app.db.session import make_engine, make_session_factory
from app.onboarding.service import MIN_PASSWORD
from app.security import hash_password


async def create_platform_admin(email: str, name: str | None, password: str) -> str:
    settings = get_settings()
    engine = make_engine(settings.database_url, settings.database_schema)
    if settings.database_url.startswith("sqlite"):
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    try:
        async with make_session_factory(engine)() as db:
            existing = await db.scalar(select(PlatformAdmin).where(func.lower(PlatformAdmin.email) == email.lower()))
            if existing is not None:
                existing.password_hash = hash_password(password)
                if name:
                    existing.name = name
                message = f"Updated password for platform admin {email}"
            else:
                db.add(PlatformAdmin(email=email.lower(), name=name, password_hash=hash_password(password)))
                message = f"Created platform admin {email}"
            await db.commit()
    finally:
        await engine.dispose()
    return message


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create-platform-admin", help="Create (or reset) a VanakkamIQ platform admin login")
    create.add_argument("--email", required=True)
    create.add_argument("--name")
    args = parser.parse_args(argv)

    password = os.getenv("PLATFORM_ADMIN_PASSWORD") or getpass.getpass("Password: ")
    if len(password) < MIN_PASSWORD:
        print(f"Password must be at least {MIN_PASSWORD} characters.", file=sys.stderr)
        return 1
    print(asyncio.run(create_platform_admin(args.email, args.name, password)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
