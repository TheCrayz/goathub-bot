#!/usr/bin/env python3
"""SQLite → PostgreSQL Migration für GoatHub Bot.

2026-06-04 Restposten #8: Wenn die Beta wächst (>50 aktive Tester, oder Bot-Cycle
mit hoher Frequenz), wird SQLite zum Single-Writer-Bottleneck. Dieses Skript
kopiert die komplette goathub.db nach Postgres — eine einmalige Aktion vor
dem Stichtag.

Vorgehen:
  1. Postgres-DB einrichten (siehe README unten).
  2. `pg_database_url=postgresql+psycopg2://user:pw@host:5432/dbname` setzen
     oder als CLI-Arg übergeben.
  3. Skript ausführen:
       python3 scripts/migrate_sqlite_to_postgres.py \\
         --sqlite-path /var/www/goathub-bot/goathub.db \\
         --postgres-url postgresql+psycopg2://goathub:PW@127.0.0.1:5432/goathub
  4. Anschließend in /var/www/goathub-bot/.env:
       DATABASE_URL=postgresql+psycopg2://goathub:PW@127.0.0.1:5432/goathub
     ändern und `systemctl restart goathub`.

Idempotenz: Skript prüft pro Tabelle vorhandene IDs und SKIPPED bereits
übertragene Rows — kann sicher mehrfach laufen (z. B. erster Probelauf
mit Read-Only-Pause, zweiter mit Final-Cutover).

README für Postgres-Setup auf gh-srv:
  apt install postgresql-15
  sudo -u postgres createuser --pwprompt goathub
  sudo -u postgres createdb -O goathub goathub
  # Test:
  psql -h 127.0.0.1 -U goathub -d goathub -c '\dt'
  # In .env:
  pip install psycopg2-binary
"""
import argparse
import os
import sys
from datetime import datetime

# Allow direct script execution from any cwd
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _connect(url):
    from sqlalchemy import create_engine
    return create_engine(url, future=True)


def _redact(url: str) -> str:
    """LOW-13: Passwort aus einer DB-URL für JEDE Ausgabe maskieren.
    postgresql+psycopg2://user:GEHEIM@host/db → postgresql+psycopg2://user:***@host/db
    """
    import re
    return re.sub(r"(?<=://)([^:/@]+):([^@]+)@", r"\1:***@", url)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--sqlite-path", required=True, help="Pfad zur Source-SQLite-Datei")
    parser.add_argument("--postgres-url", required=True,
                        help="SQLAlchemy-URL für Target-Postgres (postgresql+psycopg2://…)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Nur ausgeben was passieren würde, nichts schreiben.")
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args()

    if not os.path.exists(args.sqlite_path):
        print(f"ERROR: SQLite-Datei nicht gefunden: {args.sqlite_path}")
        sys.exit(2)

    src_url = f"sqlite:///{args.sqlite_path}"
    print(f"[{datetime.utcnow():%H:%M:%S}] Source : {src_url}")
    print(f"[{datetime.utcnow():%H:%M:%S}] Target : {_redact(args.postgres_url)}")
    if args.dry_run:
        print(f"[{datetime.utcnow():%H:%M:%S}] DRY-RUN — nichts wird geschrieben.")

    # M-14: KEIN hardcoded Fallback-Key mehr (der alte Default-Key stand im
    # öffentlichen Repo). Der Key wird hier nicht zum Entschlüsseln gebraucht
    # (Blobs werden 1:1 kopiert), aber config.py validiert ihn beim Import —
    # also denselben Key wie in /var/www/goathub-bot/.env mitgeben:
    #   ENCRYPTION_KEY=$(grep ^ENCRYPTION_KEY= /var/www/goathub-bot/.env | cut -d= -f2-) \
    #     python3 scripts/migrate_sqlite_to_postgres.py …
    if not (os.environ.get("ENCRYPTION_KEY") or os.environ.get("ENCRYPTION_KEYS")):
        print("ERROR: ENCRYPTION_KEY (oder ENCRYPTION_KEYS) muss als Umgebungs-"
              "variable gesetzt sein — Abbruch. Den Key aus der Server-.env nehmen.")
        sys.exit(2)

    # WICHTIG: erst DATABASE_URL setzen, dann models importieren — damit
    # app.db die TARGET-URL nutzt zum schema-creation. Für reines Read
    # nutzen wir separaten Source-Engine.
    os.environ["DATABASE_URL"] = args.postgres_url
    # Test-secret damit der config-import nicht bei JWT_SECRET hard-failt
    os.environ.setdefault("JWT_SECRET", "migration-script-only-ignored")

    from app.db import init_db, engine as target_engine
    from sqlalchemy.orm import sessionmaker
    from app.models import User, Activity, ManagedTrade, TokenUsage, ProcessedSignal

    print(f"[{datetime.utcnow():%H:%M:%S}] Target schema anlegen…")
    if not args.dry_run:
        init_db()

    SrcEngine = _connect(src_url)
    SrcSession = sessionmaker(bind=SrcEngine, future=True)()
    TargetSession = sessionmaker(bind=target_engine, future=True)()

    # Reihenfolge wichtig: User vor Activity/ManagedTrade (FK).
    TABLES = [
        ("users", User, "id"),
        ("activity", Activity, "id"),
        ("managed_trades", ManagedTrade, "id"),
        ("token_usage", TokenUsage, "id"),
        # H-E (2026-06-12): processed_signal MUSS mit — das ist die Replay-
        # Dedup-Tabelle (C3). Ohne sie wäre nach dem Cutover der Dedup-Speicher
        # leer und ein Restart-Replay der letzten Signale würde echte
        # Positionen DOPPELT eröffnen.
        ("processed_signal", ProcessedSignal, "id"),
    ]

    for tbl_name, Model, pk_col in TABLES:
        try:
            rows = SrcSession.query(Model).all()
        except Exception as e:
            print(f"[{datetime.utcnow():%H:%M:%S}] SKIP {tbl_name}: {e}")
            continue

        total = len(rows)
        if total == 0:
            print(f"[{datetime.utcnow():%H:%M:%S}] {tbl_name}: 0 rows — skip")
            continue

        # Existierende IDs im Target finden für idempotency
        existing_ids = set()
        if not args.dry_run:
            try:
                existing_ids = {r[0] for r in TargetSession.query(getattr(Model, pk_col)).all()}
            except Exception:
                pass

        copied = 0
        skipped = 0
        for i, row in enumerate(rows):
            rid = getattr(row, pk_col)
            if rid in existing_ids:
                skipped += 1
                continue
            if args.dry_run:
                copied += 1
                continue
            # Deepcopy via __dict__ aller column-Werte ohne _sa_instance_state
            data = {k: v for k, v in row.__dict__.items() if not k.startswith("_")}
            try:
                TargetSession.add(Model(**data))
            except Exception as e:
                print(f"  row {rid} insert failed: {e}")
                continue
            copied += 1
            if copied % args.batch_size == 0:
                TargetSession.commit()
                print(f"[{datetime.utcnow():%H:%M:%S}] {tbl_name}: {copied}/{total} commited")

        if not args.dry_run:
            TargetSession.commit()
        print(f"[{datetime.utcnow():%H:%M:%S}] {tbl_name}: copied={copied} skipped={skipped} total={total}")

    print(f"[{datetime.utcnow():%H:%M:%S}] DONE.")
    print()
    # LOW-13: URL in der Ausgabe IMMER redacted — das echte Passwort steht
    # sonst in CI-Logs / Shell-History / Scrollback.
    print("Cutover-Schritte:")
    print("  1. Stoppe goathub: ssh gh-srv 'systemctl stop goathub'")
    print(f"  2. Nochmal final-sync laufen lassen (idempotent):")
    print(f"     python3 scripts/migrate_sqlite_to_postgres.py "
          f"--sqlite-path {args.sqlite_path} --postgres-url '{_redact(args.postgres_url)}'")
    print(f"  3. In /var/www/goathub-bot/.env: DATABASE_URL={_redact(args.postgres_url)}")
    print( "     (*** durch das echte Passwort ersetzen)")
    print( "  4. Start: ssh gh-srv 'systemctl start goathub'")
    print( "  5. Verifizieren: dashboard öffnen, /api/admin/users sollte alle User listen.")
    print( "  6. SQLite-Datei zur Sicherheit aufheben für ~1 Woche bevor wegwerfen.")


if __name__ == "__main__":
    main()
