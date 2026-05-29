"""SQLAlchemy-Setup (SQLite default)."""
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app import config

_connect = {"check_same_thread": False} if config.DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(config.DATABASE_URL, connect_args=_connect, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


def init_db():
    from app import models  # noqa: F401  (Tabellen registrieren)
    Base.metadata.create_all(engine)
    # Migrate: add Discord columns to existing tables if they don't exist yet
    from sqlalchemy import text
    with engine.connect() as conn:
        for col, typedef in [
            ("discord_id", "VARCHAR"),
            ("discord_username", "VARCHAR"),
            ("discord_avatar", "VARCHAR"),
            ("created_at", "DATETIME"),
        ]:
            try:
                conn.execute(text(f"ALTER TABLE users ADD COLUMN {col} {typedef}"))
                conn.commit()
            except Exception:
                pass  # column already exists


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
