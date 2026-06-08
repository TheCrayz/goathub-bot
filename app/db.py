"""SQLAlchemy-Setup (SQLite default)."""
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import declarative_base, sessionmaker

from app import config

_connect = {"check_same_thread": False} if config.DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(config.DATABASE_URL, connect_args=_connect, future=True)


# Phase 4 (2026-06-02): SQLite enforced FK-constraints AUS by default.
# Bei jeder neuen Connection PRAGMA setzen.
#
# 2026-06-08 Mainnet-Hardening A4: WAL-Mode + synchronous=NORMAL.
# Vorher (DELETE-journal): jeder Writer hält exklusiven Lock → andere Reader
# blockieren bis Write fertig → bei 4 Background-Loops + N FastAPI-Requests
# = `database is locked` Errors mid-trade. WAL erlaubt parallele Reads
# während Writes laufen, plus mehrere Writer können gleichzeitig commit
# vorbereiten. synchronous=NORMAL = OK für unsere Use-Case (Crashrecovery
# bleibt, nur fsync nach jedem Block statt nach jedem Commit).
if config.DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")  # 5s warten bei locked statt sofort fail
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
            # Phase 3 (2026-06-02): Admin-Flag für /api/admin/*-Endpoints.
            ("is_admin", "BOOLEAN NOT NULL DEFAULT 0"),
            # 2026-06-08 C1: Max-Drawdown-Lifetime-Cap.
            ("max_drawdown_pct", "FLOAT NOT NULL DEFAULT 0.30"),
            ("peak_account_value", "FLOAT NOT NULL DEFAULT 0.0"),
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
    # 2026-06-08 Mainnet-Hardening C3: Admin-Bootstrap.
    # Falls INITIAL_ADMIN_EMAIL gesetzt ist UND der User mit dieser Email
    # existiert UND noch nicht admin ist → is_admin=True setzen.
    # Erspart manuelle SQL-Patches nach Fresh-Deploy.
    import os
    bootstrap_email = (os.getenv("INITIAL_ADMIN_EMAIL") or "").strip().lower()
    if bootstrap_email:
        from sqlalchemy import text as _text
        with engine.connect() as conn:
            try:
                r = conn.execute(_text("UPDATE users SET is_admin=1 WHERE email=:e AND is_admin=0"),
                                 {"e": bootstrap_email})
                conn.commit()
                if r.rowcount > 0:
                    print(f"[init_db] Bootstrap: {bootstrap_email} promoted to is_admin=True")
            except Exception:
                pass


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
