from collections.abc import Generator

from sqlalchemy import MetaData, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.config import get_settings

settings = get_settings()

# This DB instance is shared with an unrelated project (see PROGRESS.md) —
# every table this app owns lives in its own schema so the two never collide.
APP_SCHEMA = "proposal_app"

# Supabase's pooled connection (port 6543) runs pgbouncer in transaction mode,
# which is incompatible with server-side prepared statements and with
# SQLAlchemy's own connection pooling on top of it.
_url = settings.database_url.replace("postgresql://", "postgresql+psycopg://", 1)

engine = create_engine(
    _url,
    poolclass=NullPool,
    connect_args={"prepare_threshold": None},
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    metadata = MetaData(schema=APP_SCHEMA)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
