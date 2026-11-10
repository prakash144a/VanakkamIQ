"""Admin commands.

    python -m app.cli create-platform-admin --email you@example.com [--name "Your Name"]

There are no passwords: the admin signs in at /platform/login with a code emailed to them.
"""

import argparse
import asyncio

from sqlalchemy import func, select

from app.config import get_settings
from app.db.models import Base, PlatformAdmin
from app.db.session import make_engine, make_session_factory


async def create_platform_admin(email: str, name: str | None) -> str:
    settings = get_settings()
    engine = make_engine(settings.database_url, settings.database_schema)
    if settings.database_url.startswith("sqlite"):
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    email = email.strip().lower()
    try:
        async with make_session_factory(engine)() as db:
            existing = await db.scalar(select(PlatformAdmin).where(func.lower(PlatformAdmin.email) == email))
            if existing is not None:
                if name:
                    existing.name = name
                message = f"{email} is already a platform admin"
            else:
                db.add(PlatformAdmin(email=email, name=name))
                message = f"Created platform admin {email}: sign in at /platform/login with a code sent to that address"
            await db.commit()
    finally:
        await engine.dispose()
    return message


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create-platform-admin", help="Give an email address platform admin access")
    create.add_argument("--email", required=True)
    create.add_argument("--name")
    args = parser.parse_args(argv)
    print(asyncio.run(create_platform_admin(args.email, args.name)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
