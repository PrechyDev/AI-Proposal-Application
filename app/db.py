from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.config import get_settings

settings = get_settings()

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
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
