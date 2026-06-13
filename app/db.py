"""SQLAlchemy-Setup (SQLite default)."""
import logging

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import declarative_base, sessionmaker

from app import config

log = logging.getLogger("goathub.db")

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
    # 2026-06-12 #26/#35: vorher schluckte `except Exception: pass` JEDEN
    # Fehler — gedacht nur für "duplicate column name". Ein "database is
    # locked" (Deploy-Restart-Overlap, parallele sqlite3-Shell) oder Disk-
    # Error wurde still verworfen → App bootet mit fehlender Spalte → jede
    # User-Query crasht später mit "no such column" OHNE Hinweis auf die
    # gescheiterte Migration. Jetzt: existierende Spalten vorab per Inspector
    # überspringen; unerwartete Fehler werden laut geloggt und re-raised
    # (Service-Start bricht ab statt mit kaputtem Schema weiterzulaufen).
    from sqlalchemy import text
    existing_cols = {c["name"] for c in inspect(engine).get_columns("users")}
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
            if col in existing_cols:
                continue
            try:
                conn.execute(text(f"ALTER TABLE users ADD COLUMN {col} {typedef}"))
                conn.commit()
            except OperationalError as e:
                # M-18: NUR OperationalError fangen, und davon NUR die erwartete
                # Duplicate-Column-Race (zweiter Prozess war schneller) schlucken.
                # Locked DB, Disk-Error etc. werden laut geloggt und re-raised —
                # alles andere (Nicht-OperationalError) propagiert ungefangen.
                if "duplicate column" in str(e).lower():
                    continue
                log.error("init_db: ALTER TABLE users ADD COLUMN %s fehlgeschlagen: %s", col, e)
                raise
        # 2026-06-13 C-4 (Ownership-Modell): pro managed_trade die Bot-eigene
        # Entry-Order-ID/-Cloid + die vom BOT gefüllte Menge persistieren, damit
        # _adjust/_cancel/Watcher/Sync NUR Bot-attribuierte Positionen/Orders
        # anfassen — nie eine manuelle User-Position. Alle nullable OHNE Default:
        # NULL = Legacy-Row von vor dieser Änderung → konservativer Fallback im
        # Engine-Code (bot_filled_sz IS NULL ⇒ ownership "unknown", alte Logik).
        # bot_filled_sz ist MoneyDecimal = TEXT auf SQLite (wie die Preis-Spalten).
        mt_cols = {c["name"] for c in inspect(engine).get_columns("managed_trades")}
        for col, typedef in [
            ("entry_oid", "VARCHAR"),
            ("entry_cloid", "VARCHAR"),
            ("bot_filled_sz", "VARCHAR"),
        ]:
            if col in mt_cols:
                continue
            try:
                conn.execute(text(f"ALTER TABLE managed_trades ADD COLUMN {col} {typedef}"))
                conn.commit()
            except OperationalError as e:
                if "duplicate column" in str(e).lower():
                    continue
                log.error("init_db: ALTER TABLE managed_trades ADD COLUMN %s fehlgeschlagen: %s", col, e)
                raise
        # 2026-06-12 #44: Composite-Index für den Idempotenz-Lookup des
        # Token-Scrapers (6-Spalten-Filter, lief vorher als Full-Scan pro
        # Log-Zeile alle 5 Minuten). ts+model+prompt+output selektiert genug.
        try:
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_tu_dedup ON token_usage (ts, model, prompt, output)"))
            conn.commit()
        except Exception as e:
            log.error("init_db: CREATE INDEX ix_tu_dedup fehlgeschlagen: %s", e)
            raise

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
            except Exception as e:
                # 2026-06-12 #26: nicht mehr still schlucken — Bootstrap-Fail
                # (z.B. locked DB) soll sichtbar sein. Kein re-raise: ein
                # fehlgeschlagenes Admin-Promote ist beim nächsten Start
                # retry-bar und darf den Service-Start nicht verhindern.
                log.error("init_db: INITIAL_ADMIN_EMAIL-Bootstrap fehlgeschlagen: %s", e)


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
        # M-18: kein bare except:pass mehr — nur "already exists" ist harmlos.
        try:
            conn.execute(text(f"CREATE INDEX ix_{table_name}_{column} ON {table_name}({column})"))
        except OperationalError as e:
            if "already exists" in str(e).lower():
                pass
            else:
                log.error("_migrate_add_fk: CREATE INDEX ix_%s_%s fehlgeschlagen: %s",
                          table_name, column, e)
                raise


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
