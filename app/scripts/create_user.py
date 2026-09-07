"""Bootstrap CLI for creating users before the admin UI (build step 4) exists.

Usage:
    poetry run python -m app.scripts.create_user --name "Jane Doe" --email jane@koya.com \
        --password "change-me" --admin --can-create --can-approve
"""

import argparse

from sqlalchemy import select

from app.db import SessionLocal
from app.models import User
from app.security import hash_password


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a user directly in the database.")
    parser.add_argument("--name", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--admin", action="store_true", dest="is_admin")
    parser.add_argument("--can-create", action="store_true", dest="can_create")
    parser.add_argument("--can-approve", action="store_true", dest="can_approve")
    args = parser.parse_args()

    with SessionLocal() as db:
        existing = db.execute(select(User).where(User.email == args.email)).scalar_one_or_none()
        if existing is not None:
            raise SystemExit(f"A user with email {args.email!r} already exists (id={existing.id}).")

        user = User(
            name=args.name,
            email=args.email,
            password_hash=hash_password(args.password),
            can_create=args.can_create,
            can_approve=args.can_approve,
            is_admin=args.is_admin,
            is_active=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        print(f"Created user id={user.id} email={user.email}")


if __name__ == "__main__":
    main()
