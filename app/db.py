"""SQLAlchemy-Setup (SQLite default)."""
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import declarative_base, sessionmaker

from app import config

_connect = {"check_same_thread": False} if config.DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(config.DATABASE_URL, connect_args=_connect, future=True)


# Phase 4 (2026-06-02): SQLite enforced FK-constraints AUS by default.
# Bei jeder neuen Connection PRAGMA setzen.
if config.DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


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
            # Phase 1 (2026-06-02): JWT-Versionierung für Server-side-Logout.
            ("token_version", "INTEGER NOT NULL DEFAULT 0"),
        ]:
            try:
                conn.execute(text(f"ALTER TABLE users ADD COLUMN {col} {typedef}"))
                conn.commit()
            except Exception:
                pass  # column already exists

    # Phase 4 (2026-06-02): FK-Migration für activity + managed_trades.
    # SQLite kann FK nicht via ALTER TABLE nachträglich anhängen — also
    # recreate-and-copy. Einmal-Aktion; idempotent via FK-Existenz-Check.
    _migrate_add_fk("activity", "user_id", "users(id)", on_delete="CASCADE")
    _migrate_add_fk("managed_trades", "user_id", "users(id)", on_delete="CASCADE")


def _migrate_add_fk(table_name: str, column: str, ref: str, on_delete: str = "CASCADE"):
    """Fügt einer bestehenden SQLite-Tabelle eine FK nachträglich hinzu.
    Vorgehen: temporäre Tabelle mit FK anlegen → Daten kopieren → original
    droppen → temporäre umbenennen. Indizes werden NACH dem rename neu
    angelegt (von create_all bzw. unten explizit).
    """
    from sqlalchemy import text
    insp = inspect(engine)
    if table_name not in insp.get_table_names():
        return
    fks = insp.get_foreign_keys(table_name)
    if any(column in (fk.get("constrained_columns") or []) for fk in fks):
        return  # FK existiert bereits
    cols = insp.get_columns(table_name)
    col_defs = []
    col_names = []
    for c in cols:
        name = c["name"]
        col_names.append(name)
        coltype = c["type"].compile(engine.dialect)
        nullable = "" if c["nullable"] else " NOT NULL"
        default = ""
        if c.get("default") is not None:
            default = f" DEFAULT {c['default']}"
        pk = " PRIMARY KEY" if c.get("primary_key") else ""
        fk_clause = f" REFERENCES {ref} ON DELETE {on_delete}" if name == column else ""
        col_defs.append(f"{name} {coltype}{nullable}{default}{pk}{fk_clause}")
    tmp = f"{table_name}_fkmig"
    col_list = ", ".join(col_names)
    create_sql = f"CREATE TABLE {tmp} (" + ", ".join(col_defs) + ")"
    with engine.begin() as conn:
        conn.execute(text(create_sql))
        conn.execute(text(f"INSERT INTO {tmp} ({col_list}) SELECT {col_list} FROM {table_name}"))
        conn.execute(text(f"DROP TABLE {table_name}"))
        conn.execute(text(f"ALTER TABLE {tmp} RENAME TO {table_name}"))
        # Index auf user_id wiederherstellen (Lookups skalieren)
        try:
            conn.execute(text(f"CREATE INDEX ix_{table_name}_{column} ON {table_name}({column})"))
        except Exception:
            pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
