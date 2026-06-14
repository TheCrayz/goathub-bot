"""GoatHub-Bot — Management-CLI (auf dem Server via Termius nutzen).

Legt "Backdoor"-/Gratis-Accounts an: Email + Passwort, volle Bot-Rechte, KEIN
Discord nötig. Solche User loggen sich unter **/backdoor** (oder dem normalen
Email/Passwort-Login) ein — ohne Discord-Supporter-Rolle.

Beispiele (im Repo-Verzeichnis /var/www/goathub-bot, mit dem venv):

  venv/bin/python -m app.manage create-user --email kollege@example.com --password 'GutesPW123'
  venv/bin/python -m app.manage create-user --email kollege@example.com            # Passwort wird generiert + ausgegeben
  venv/bin/python -m app.manage list-users
  venv/bin/python -m app.manage set-password --email kollege@example.com --password 'NeuesPW'
  venv/bin/python -m app.manage delete-user  --email kollege@example.com
  venv/bin/python -m app.manage make-admin   --email kollege@example.com
"""
from __future__ import annotations

import argparse
import re
import secrets
import sys

from app import config
from app.auth import hash_pw
from app.db import SessionLocal, init_db
from app.models import User

# Gleiche Validierung wie /api/register, damit CLI- und Web-Accounts konsistent sind.
EMAIL_RE = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,24}$")


def _norm_email(email: str) -> str:
    email = (email or "").strip().lower()
    if len(email) > 254 or not EMAIL_RE.match(email):
        sys.exit(f"FEHLER: ungültige Email: {email!r}")
    # 'discord_…' / @goathub.internal gehören den synthetischen OAuth-Accounts.
    if email.endswith("@goathub.internal") or email.split("@", 1)[0].startswith("discord_"):
        sys.exit("FEHLER: 'discord_…' / @goathub.internal sind für Discord-Accounts reserviert.")
    return email


def create_user(args):
    init_db()
    email = _norm_email(args.email)
    generated = not args.password
    pw = args.password or secrets.token_urlsafe(12)
    if len(pw) < 6:
        sys.exit("FEHLER: Passwort muss mind. 6 Zeichen haben.")
    # Bounds spiegeln schemas.SettingsIn — die CLI darf KEINE gefährlichen Werte
    # auf den Geld-Pfad schreiben (sonst Bypass des API-Safety-Caps bei echtem Geld).
    risk = args.risk if args.risk is not None else config.DEFAULT_RISK_PCT
    leverage = args.leverage if args.leverage is not None else config.DEFAULT_LEVERAGE
    max_open = args.max_open if args.max_open is not None else config.DEFAULT_MAX_OPEN
    if not (0 < risk <= 0.05):
        sys.exit(f"FEHLER: --risk muss eine FRACTION in (0, 0.05] sein (0.005 = 0.5%). Bekommen: {risk}")
    if not (1 <= leverage <= 50):
        sys.exit(f"FEHLER: --leverage muss in [1, 50] sein. Bekommen: {leverage}")
    if not (1 <= max_open <= 20):
        sys.exit(f"FEHLER: --max-open muss in [1, 20] sein. Bekommen: {max_open}")
    with SessionLocal() as db:
        if db.query(User).filter(User.email == email).first():
            sys.exit(f"FEHLER: Email {email} ist bereits registriert (set-password nutzen, um das PW zu ändern).")
        u = User(
            email=email,
            password_hash=hash_pw(pw),
            risk_pct=risk,
            leverage=leverage,
            max_open_positions=max_open,
        )
        db.add(u)
        db.commit()
        db.refresh(u)
        print("✅ Gratis-Account angelegt — Login unter /backdoor (oder Email/Passwort), KEIN Discord nötig:")
        print(f"   id:       {u.id}")
        print(f"   email:    {email}")
        print(f"   password: {pw}" + ("   ← generiert: notieren & weitergeben!" if generated else ""))


def list_users(args):
    init_db()
    with SessionLocal() as db:
        users = db.query(User).order_by(User.id).all()
        print(f"{'id':>4}  {'email':<42} {'typ':<9} {'admin':<5} {'bot':<4} created")
        print("-" * 92)
        for u in users:
            kind = "discord" if u.discord_id else "backdoor"
            print(f"{u.id:>4}  {u.email:<42} {kind:<9} {('yes' if u.is_admin else '-'):<5} "
                  f"{('on' if u.bot_active else 'off'):<4} {u.created_at}")
        print(f"\n{len(users)} User gesamt "
              f"({sum(1 for u in users if not u.discord_id)} backdoor / "
              f"{sum(1 for u in users if u.discord_id)} discord).")


def set_password(args):
    init_db()
    email = _norm_email(args.email)
    if len(args.password) < 6:
        sys.exit("FEHLER: Passwort muss mind. 6 Zeichen haben.")
    with SessionLocal() as db:
        u = db.query(User).filter(User.email == email).first()
        if not u:
            sys.exit(f"FEHLER: kein User mit Email {email}.")
        u.password_hash = hash_pw(args.password)
        # Bestehende Sessions/JWTs ungültig machen (token_version bump).
        u.token_version = int(getattr(u, "token_version", 0) or 0) + 1
        db.commit()
        print(f"✅ Passwort für {email} gesetzt (alle alten Sessions abgemeldet).")


def delete_user(args):
    init_db()
    email = _norm_email(args.email)
    with SessionLocal() as db:
        u = db.query(User).filter(User.email == email).first()
        if not u:
            sys.exit(f"FEHLER: kein User mit Email {email}.")
        db.delete(u)
        db.commit()
        print(f"✅ User {email} gelöscht.")


def make_admin(args):
    init_db()
    email = _norm_email(args.email)
    with SessionLocal() as db:
        u = db.query(User).filter(User.email == email).first()
        if not u:
            sys.exit(f"FEHLER: kein User mit Email {email}.")
        u.is_admin = True
        db.commit()
        print(f"✅ {email} ist jetzt Admin.")


def main(argv=None):
    p = argparse.ArgumentParser(prog="app.manage", description="GoatHub-Bot Account-Verwaltung")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create-user", help="Gratis-Account anlegen (Email+Passwort, ohne Discord)")
    c.add_argument("--email", required=True)
    c.add_argument("--password", default=None, help="weglassen → sicheres Passwort wird generiert")
    c.add_argument("--risk", type=float, default=None,
                   help=f"Risk pro Trade als FRACTION (0.005=0.5%%), erlaubt (0,0.05]. Default {config.DEFAULT_RISK_PCT}")
    c.add_argument("--leverage", type=float, default=None,
                   help=f"Hebel-Cap [1,50]. Default {config.DEFAULT_LEVERAGE}")
    c.add_argument("--max-open", type=int, default=None, dest="max_open",
                   help=f"max. offene Positionen [1,20]. Default {config.DEFAULT_MAX_OPEN}")
    c.set_defaults(func=create_user)

    li = sub.add_parser("list-users", help="alle User auflisten")
    li.set_defaults(func=list_users)

    sp = sub.add_parser("set-password", help="Passwort eines Users setzen")
    sp.add_argument("--email", required=True)
    sp.add_argument("--password", required=True)
    sp.set_defaults(func=set_password)

    de = sub.add_parser("delete-user", help="User löschen")
    de.add_argument("--email", required=True)
    de.set_defaults(func=delete_user)

    ad = sub.add_parser("make-admin", help="User zum Admin machen")
    ad.add_argument("--email", required=True)
    ad.set_defaults(func=make_admin)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
