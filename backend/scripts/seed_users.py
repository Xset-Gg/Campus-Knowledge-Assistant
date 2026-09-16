"""Create the initial demo accounts (one per role).

    python -m scripts.seed_users

Passwords come from the environment where set, so a real deployment never
carries the demo defaults:
    SEED_ADMIN_PASSWORD, SEED_PROFESSOR_PASSWORD, SEED_STUDENT_PASSWORD
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.auth import hash_password  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.models import User, UserRole  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("seed")

SEED_USERS = [
    {
        "email": "admin@university.edu",
        "password_env": "SEED_ADMIN_PASSWORD",
        "default_password": "admin-change-me",
        "full_name": "Registrar Admin",
        "role": UserRole.admin,
        "department": "Administration",
    },
    {
        "email": "professor@university.edu",
        "password_env": "SEED_PROFESSOR_PASSWORD",
        "default_password": "professor-change-me",
        "full_name": "Dr. Alex Rivera",
        "role": UserRole.professor,
        "department": "Computer Science",
    },
    {
        "email": "student@university.edu",
        "password_env": "SEED_STUDENT_PASSWORD",
        "default_password": "student-change-me",
        "full_name": "Jordan Lee",
        "role": UserRole.student,
        "department": "Computer Science",
    },
]


def main() -> int:
    settings = get_settings()
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    created = 0

    with sessionmaker(bind=engine)() as session:
        for spec in SEED_USERS:
            existing = session.execute(
                select(User).where(User.email == spec["email"])
            ).scalar_one_or_none()
            if existing:
                logger.info("Exists, skipping: %s", spec["email"])
                continue

            password = os.getenv(spec["password_env"], spec["default_password"])
            if password == spec["default_password"] and settings.environment == "production":
                logger.error("Refusing to seed %s with a default password in production", spec["email"])
                return 1

            session.add(
                User(
                    email=spec["email"],
                    hashed_password=hash_password(password),
                    full_name=spec["full_name"],
                    role=spec["role"],
                    department=spec["department"],
                )
            )
            created += 1
            logger.info("Created %s (%s)", spec["email"], spec["role"].value)
        session.commit()

    logger.info("Seed complete — %d user(s) created", created)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
