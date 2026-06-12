"""Web-/API-Layer Regressions-Tests (2026-06-12 Audit-Fixes).

Sichert die Security-Fixes im Web-Layer gegen Kaputt-Refactoring:

  M-9  — Register lehnt den reservierten OAuth-Namespace ab
         (@goathub.internal + discord_-Local-Part) → kein Signup-DoS
         gegen Discord-Logins.
  LOW-7 — Per-Account-Lockout: >=5 Fehl-Logins in Folge sperren den
         Account 15 min (auch mit richtigem Passwort), Erfolg resettet.
  H-C  — /api/admin/test-signal: ohne "confirm": true → 400; kaputte
         Typen/Werte → 422 (Pydantic); bestätigter Dispatch schreibt
         einen Activity-Audit-Eintrag.
  M-13 — Wallet-Wechsel resettet builder_approved (Approval ist on-chain
         an die alte Master-Adresse gebunden).
  LOW-8 — /api/refresh verweigert Sessions älter als 30 Tage (orig_iat).
  LOW-10 — /api/health ist nur noch minimale Liveness-Probe.

Run:
    PYTHONPATH=. venv/bin/python -m pytest tests/test_web.py -q
"""
import os
import time

# Test-only env so wir die echten config-Checks (JWT/ENCRYPTION) passieren.
# MUSS vor jedem app.*-Import stehen (config.py validiert beim Import).
os.environ.setdefault("JWT_SECRET", "test-only-secret-not-prod-1234567890abcdef")
from cryptography.fernet import Fernet  # noqa: E402
os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ["ENABLE_LISTENER"] = "false"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
# Großzügige per-IP-Limits, damit die Tests nicht in slowapi's IP-Limit laufen
# — der Per-Account-Lockout (LOW-7) wird separat und gezielt getestet.
os.environ["LOGIN_RATE_LIMIT"] = "1000/minute"
os.environ["REGISTER_RATE_LIMIT"] = "1000/minute"

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

# Unter pytest verdrahtet tests/conftest.py die EINE geteilte StaticPool-
# :memory:-Engine — dann hier NICHTS anfassen. Nur standalone selbst bauen.
import app.db as _dbmod  # noqa: E402
if _dbmod.engine.pool.__class__.__name__ != "StaticPool":
    _test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    _dbmod.engine = _test_engine
    _dbmod.SessionLocal = sessionmaker(bind=_test_engine, autoflush=False, autocommit=False, future=True)

from app.db import Base, SessionLocal  # noqa: E402
from app.models import Activity, User  # noqa: E402
Base.metadata.create_all(_dbmod.engine)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# app.main NACH dem DB-Setup importieren (init_db läuft nicht — lifespan wird
# ohne Context-Manager nicht betreten, Tabellen kommen aus create_all oben).
import app.main as main  # noqa: E402


@pytest.fixture()
def client():
    """Frischer TestClient pro Test + Reset des In-Memory-States:
    slowapi-Limiter-Storage (sonst leaken Limits zwischen Tests) und der
    LOW-7-Lockout-Dict."""
    main.limiter.reset()
    main._failed_logins.clear()
    return TestClient(main.app)


# ── Helpers ─────────────────────────────────────────────────────────────────
def _register(client, email, password="secret123"):
    return client.post("/api/register", json={"email": email, "password": password})


def _login(client, email, password):
    return client.post("/api/login", json={"email": email, "password": password})


def _auth_headers(resp):
    return {"Authorization": "Bearer " + resp.json()["access_token"]}


def _make_admin(client, email):
    """Registriert einen User und flippt is_admin direkt in der DB."""
    r = _register(client, email, "adminpw123")
    assert r.status_code == 200, r.text[:200]
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.email == email).first()
        u.is_admin = True
        db.commit()
    finally:
        db.close()
    return _auth_headers(r)


def _user_by_email(email):
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.email == email).first()
        if u is not None:
            db.refresh(u)
            db.expunge(u)
        return u
    finally:
        db.close()


# ── M-9: reservierter OAuth-Namespace ───────────────────────────────────────
def test_register_rejects_internal_domain(client):
    r = _register(client, "discord_999888777@goathub.internal")
    assert r.status_code == 400, r.text[:200]
    assert "reserved" in r.json()["detail"].lower()


def test_register_rejects_goathub_internal_any_localpart(client):
    # Die GANZE Domain ist reserviert, nicht nur discord_-Adressen.
    r = _register(client, "someone@goathub.internal")
    assert r.status_code == 400, r.text[:200]


def test_register_rejects_discord_localpart_any_domain(client):
    # M-9 defensiv: discord_-Local-Part ist überall reserviert.
    r = _register(client, "discord_123456@gmail.com")
    assert r.status_code == 400, r.text[:200]
    assert "reserved" in r.json()["detail"].lower()


# ── LOW-7: Per-Account-Lockout ──────────────────────────────────────────────
def test_account_lockout_after_failed_logins(client):
    email = "lockme@test.local"
    assert _register(client, email, "rightpw123").status_code == 200

    for _ in range(5):
        r = _login(client, email, "wrongpw!")
        assert r.status_code == 401, r.text[:200]

    # 6. Versuch — sogar mit RICHTIGEM Passwort → gesperrt (429)
    r = _login(client, email, "rightpw123")
    assert r.status_code == 429, r.text[:200]
    assert "locked" in r.json()["detail"].lower()

    # Lock abgelaufen (künstlich zurückdrehen) → Login geht wieder,
    # Erfolg räumt den Zähler komplett weg.
    main._failed_logins[email]["locked_until"] = 0
    r = _login(client, email, "rightpw123")
    assert r.status_code == 200, r.text[:200]
    assert email not in main._failed_logins


def test_failed_logins_below_threshold_do_not_lock(client):
    email = "almost@test.local"
    assert _register(client, email, "rightpw123").status_code == 200
    for _ in range(4):
        assert _login(client, email, "wrongpw!").status_code == 401
    # 4 < 5 → kein Lock, richtiger Login klappt sofort
    assert _login(client, email, "rightpw123").status_code == 200


# ── H-C: /api/admin/test-signal ─────────────────────────────────────────────
def test_test_signal_without_confirm_is_400(client):
    hdrs = _make_admin(client, "admin-confirm@test.local")
    body = {"type": "NEW_TRADE", "asset": "SOL/USDT", "direction": "LONG",
            "entry": 140, "stop_loss": 135}
    r = client.post("/api/admin/test-signal", json=body, headers=hdrs)
    assert r.status_code == 400, r.text[:200]
    assert "confirm" in r.json()["detail"].lower()


def test_test_signal_bad_types_are_422(client):
    hdrs = _make_admin(client, "admin-types@test.local")
    bad_payloads = [
        # action außerhalb des Enums
        {"confirm": True, "type": "YOLO_TRADE", "asset": "SOL/USDT",
         "direction": "LONG", "entry": 140, "stop_loss": 135},
        # direction außerhalb des Enums
        {"confirm": True, "type": "NEW_TRADE", "asset": "SOL/USDT",
         "direction": "SIDEWAYS", "entry": 140, "stop_loss": 135},
        # entry kein float
        {"confirm": True, "type": "NEW_TRADE", "asset": "SOL/USDT",
         "direction": "LONG", "entry": "not-a-number", "stop_loss": 135},
        # negativer Preis
        {"confirm": True, "type": "NEW_TRADE", "asset": "SOL/USDT",
         "direction": "LONG", "entry": -5, "stop_loss": 135},
        # SL auf der falschen Seite (LONG braucht SL UNTER Entry)
        {"confirm": True, "type": "NEW_TRADE", "asset": "SOL/USDT",
         "direction": "LONG", "entry": 140, "stop_loss": 150},
        # confidence außerhalb [0, 1]
        {"confirm": True, "type": "NEW_TRADE", "asset": "SOL/USDT",
         "direction": "LONG", "entry": 140, "stop_loss": 135, "confidence": 1.5},
        # kaputtes asset-Format
        {"confirm": True, "type": "NEW_TRADE", "asset": "<script>",
         "direction": "LONG", "entry": 140, "stop_loss": 135},
    ]
    for payload in bad_payloads:
        r = client.post("/api/admin/test-signal", json=payload, headers=hdrs)
        assert r.status_code == 422, f"{payload} → {r.status_code}: {r.text[:200]}"


def test_test_signal_confirmed_dispatches_and_writes_audit(client, monkeypatch):
    """Happy-Path: confirm=true → handle_signal wird (gemockt) aufgerufen,
    eine prominente Activity-Zeile mit Auslöser + Signal landet in der DB."""
    import app.engine as engine

    calls = []

    async def _fake_handle_signal(embed):
        calls.append(embed)

    monkeypatch.setattr(engine, "handle_signal", _fake_handle_signal)
    hdrs = _make_admin(client, "admin-dispatch@test.local")
    body = {"confirm": True, "type": "NEW_TRADE", "asset": "SOL/USDT",
            "direction": "LONG", "entry": 140, "stop_loss": 135,
            "take_profits": [{"price": 150, "percent": 100}],
            "signal_id": "pytest-001", "confidence": 0.9}
    r = client.post("/api/admin/test-signal", json=body, headers=hdrs)
    assert r.status_code == 200, r.text[:200]
    assert r.json()["ok"] is True
    assert len(calls) == 1   # Engine wurde genau einmal getriggert

    db = SessionLocal()
    try:
        row = db.query(Activity).order_by(Activity.id.desc()).first()
        assert row is not None
        assert "TEST-SIGNAL" in row.text
        assert "admin-dispatch@test.local" in row.text
        assert "pytest-001" in row.text
    finally:
        db.close()


# ── M-13: Wallet-Wechsel resettet builder_approved ──────────────────────────
def test_wallet_change_resets_builder_approved(client):
    from eth_account import Account

    email = "wallet@test.local"
    r = _register(client, email, "walletpw123")
    assert r.status_code == 200
    hdrs = _auth_headers(r)

    agent = Account.create()
    agent_key = agent.key.hex()
    if not agent_key.startswith("0x"):
        agent_key = "0x" + agent_key
    addr1 = "0x" + "11" * 20
    addr2 = "0x" + "22" * 20

    r = client.post("/api/wallet", json={"hl_account_address": addr1,
                                         "hl_api_secret": agent_key}, headers=hdrs)
    assert r.status_code == 200, r.text[:200]

    # Flag setzen wie nach erfolgreichem On-Chain-Approval
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.email == email).first()
        u.builder_approved = True
        db.commit()
    finally:
        db.close()

    # Gleiche Adresse erneut speichern (z. B. Agent-Key-Rotation) → Flag BLEIBT
    r = client.post("/api/wallet", json={"hl_account_address": addr1,
                                         "hl_api_secret": agent_key}, headers=hdrs)
    assert r.status_code == 200, r.text[:200]
    assert _user_by_email(email).builder_approved is True

    # ANDERE Master-Adresse → Approval ist on-chain an die alte gebunden → reset
    r = client.post("/api/wallet", json={"hl_account_address": addr2,
                                         "hl_api_secret": agent_key}, headers=hdrs)
    assert r.status_code == 200, r.text[:200]
    assert _user_by_email(email).builder_approved is False


# ── LOW-8: absolute Session-Lebensdauer via orig_iat ────────────────────────
def test_refresh_rejects_sessions_older_than_30_days(client):
    from app.auth import make_token, _user_identity

    email = "oldsession@test.local"
    r = _register(client, email, "refreshpw123")
    assert r.status_code == 200

    # Frische Session → Refresh klappt
    r2 = client.post("/api/refresh", headers=_auth_headers(r))
    assert r2.status_code == 200, r2.text[:200]

    # Token mit orig_iat von vor 31 Tagen (exp selbst ist frisch) → 401
    u = _user_by_email(email)
    old_orig = int(time.time()) - 31 * 24 * 3600
    stale = make_token(u.id, getattr(u, "token_version", 0), _user_identity(u),
                       orig_iat=old_orig)
    r3 = client.post("/api/refresh", headers={"Authorization": "Bearer " + stale})
    assert r3.status_code == 401, r3.text[:200]
    assert "maximum age" in r3.json()["detail"].lower()


# ── LOW-10: /api/health minimal ─────────────────────────────────────────────
def test_health_is_minimal(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    # Exakte Shape: KEIN listener-Status, KEIN testnet/mainnet-Leak mehr —
    # die Details stehen auth-gated in /api/admin/health.
    assert r.json() == {"status": "ok"}
