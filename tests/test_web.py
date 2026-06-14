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
# Standalone-Run: Registrierung offen, damit _register Test-User anlegen kann.
os.environ.setdefault("REGISTRATION_OPEN", "true")

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


def test_registration_invite_only_returns_403(client):
    """2026-06-14 Invite-only: bei REGISTRATION_OPEN=False ist /api/register zu
    (403), bevor irgendeine Email-Validierung greift — Zugang nur via Discord-
    Supporter oder vom Team angelegte Accounts (app.manage)."""
    from app import config
    orig = config.REGISTRATION_OPEN
    config.REGISTRATION_OPEN = False
    try:
        r = _register(client, "rando@example.com")
        assert r.status_code == 403, r.text[:200]
        assert "invite-only" in r.json()["detail"].lower()
    finally:
        config.REGISTRATION_OPEN = orig


def _oauth_callback(client, monkeypatch, discord_id, username="rando", state="st"):
    """Discord-OAuth-Callback durchspielen: exchange_code/get_discord_user mocken,
    CSRF-state-Cookie setzen, Callback aufrufen."""
    import app.main as main
    async def fake_exchange(code):
        return {"access_token": "tok"}
    async def fake_user(tok):
        return {"id": discord_id, "username": username, "avatar": ""}
    monkeypatch.setattr(main, "exchange_code", fake_exchange)
    monkeypatch.setattr(main, "get_discord_user", fake_user)
    client.cookies.set("discord_oauth_state", state)
    return client.get(f"/auth/callback?code=c&state={state}")


def test_oauth_invite_only_blocks_new_signup(client, monkeypatch):
    """CRITICAL-Fix (Review 2026-06-14): bei invite-only (REGISTRATION_OPEN=False)
    + unkonfiguriertem Rollen-Gate legt der Discord-OAuth-Callback KEINEN neuen
    User an — sonst wäre die Invite-only-Sperre über OAuth komplett umgehbar."""
    from app import config
    from app.models import User
    from app.db import SessionLocal
    monkeypatch.setattr(config, "DISCORD_GUILD_ID", "")
    monkeypatch.setattr(config, "DISCORD_BOT_TOKEN", None)
    monkeypatch.setattr(config, "REGISTRATION_OPEN", False)
    _oauth_callback(client, monkeypatch, "9000000001")
    with SessionLocal() as db:
        assert db.query(User).filter(User.discord_id == "9000000001").first() is None, \
            "OAuth-Callback darf bei invite-only KEINEN neuen User anlegen"


def test_oauth_open_registration_creates_user(client, monkeypatch):
    """Gegenprobe: mit REGISTRATION_OPEN=True legt der Callback einen User an."""
    from app import config
    from app.models import User
    from app.db import SessionLocal
    monkeypatch.setattr(config, "DISCORD_GUILD_ID", "")
    monkeypatch.setattr(config, "DISCORD_BOT_TOKEN", None)
    monkeypatch.setattr(config, "REGISTRATION_OPEN", True)
    _oauth_callback(client, monkeypatch, "9000000002", username="openuser")
    with SessionLocal() as db:
        assert db.query(User).filter(User.discord_id == "9000000002").first() is not None, \
            "bei offener Registrierung muss der OAuth-Callback den User anlegen"


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


# ── H-17: HL_TESTNET strikt parsen (fail-closed) ────────────────────────────
# config.py validiert beim IMPORT — und app.config ist in diesem Prozess längst
# importiert (gecacht). Um das Import-Verhalten unter verschiedenen
# HL_TESTNET-Werten zu prüfen, importieren wir app.config je in einem frischen
# Subprozess mit kontrollierter Env. JWT_SECRET/ENCRYPTION_KEY müssen gesetzt
# sein (config validiert die zuerst), sonst träfe der falsche Hard-Fail.
import subprocess  # noqa: E402
import sys  # noqa: E402


def _import_config_with(hl_testnet):
    """Importiert app.config in einem frischen Prozess. hl_testnet=None → Var
    NICHT setzen (Default-Pfad). Gibt (returncode, stdout+stderr) zurück."""
    env = dict(os.environ)
    env["JWT_SECRET"] = "test-only-secret-not-prod-1234567890abcdef"
    env["ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    env.pop("HL_TESTNET", None)
    if hl_testnet is not None:
        env["HL_TESTNET"] = hl_testnet
    code = (
        "import app.config as c;"
        "print('NET=TESTNET' if c.HL_TESTNET else 'NET=MAINNET')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env=env, capture_output=True, text=True,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    return proc.returncode, (proc.stdout + proc.stderr)


def test_hl_testnet_missing_defaults_to_testnet():
    """Fehlendes HL_TESTNET → sauberer Default (testnet=true), KEIN Hard-Fail."""
    rc, out = _import_config_with(None)
    assert rc == 0, f"import should succeed when HL_TESTNET unset:\n{out}"
    assert "NET=TESTNET" in out, out


def test_hl_testnet_recognized_values_parse():
    """Explizit erkannte true/false-Werte werden korrekt geparst."""
    for val, expect in [("true", "NET=TESTNET"), ("1", "NET=TESTNET"),
                        ("false", "NET=MAINNET"), ("0", "NET=MAINNET"),
                        ("FALSE", "NET=MAINNET"), ("On", "NET=TESTNET")]:
        rc, out = _import_config_with(val)
        assert rc == 0, f"HL_TESTNET={val!r} should import cleanly:\n{out}"
        assert expect in out, f"HL_TESTNET={val!r} → expected {expect}:\n{out}"


def test_hl_testnet_garbage_fails_closed():
    """Ein GESETZTER, aber unparsebarer Wert (z.B. 'ture', 'True ' mit Space)
    lässt den Import hart fehlschlagen — statt still auf Mainnet zu schalten."""
    for bad in ["ture", "tru", "maybe", "yesnt", "2", "mainnet"]:
        rc, out = _import_config_with(bad)
        assert rc != 0, f"HL_TESTNET={bad!r} should fail the import (got rc=0):\n{out}"
        assert "HL_TESTNET" in out, out
        assert "Boolean" in out or "RuntimeError" in out, out


# ── Referral: /api/referral-status ──────────────────────────────────────────
def _connect_wallet(client, hdrs):
    """Verbindet eine valide (Master, Agent)-Wallet für den eingeloggten User."""
    from eth_account import Account
    agent = Account.create()
    agent_key = agent.key.hex()
    if not agent_key.startswith("0x"):
        agent_key = "0x" + agent_key
    addr = "0x" + "33" * 20
    r = client.post("/api/wallet", json={"hl_account_address": addr,
                                         "hl_api_secret": agent_key}, headers=hdrs)
    assert r.status_code == 200, r.text[:200]
    return addr


def test_referral_status_without_wallet(client):
    """Ohne verbundene Wallet: kein HL-Call, exakte Shape mit Code/Link aus config."""
    from app import config
    r = _register(client, "ref-nowallet@test.local", "refpw12345")
    assert r.status_code == 200
    rr = client.get("/api/referral-status", headers=_auth_headers(r))
    assert rr.status_code == 200, rr.text[:200]
    body = rr.json()
    assert body == {
        "code": config.REFERRAL_CODE,
        "link": config.REFERRAL_LINK,
        "wallet_connected": False,
        "referred_by": None,
        "is_ours": False,
        "error": None,
    }


def test_referral_status_with_mocked_trader(client, monkeypatch):
    """Mit Wallet + gemocktem Trader, dessen referral_state() unseren Code liefert
    → is_ours=True, wallet_connected=True, kein echter HL-Call."""
    import app.engine as engine
    from app import config

    r = _register(client, "ref-wallet@test.local", "refpw12345")
    assert r.status_code == 200
    hdrs = _auth_headers(r)
    _connect_wallet(client, hdrs)

    class _StubTrader:
        def referral_state(self):
            return {"referred_by_code": config.REFERRAL_CODE,
                    "referrer_addr": "0x" + "ab" * 20, "raw": {}}

    async def _fake_build_trader(u):
        return _StubTrader()

    monkeypatch.setattr(engine, "_build_trader", _fake_build_trader)
    rr = client.get("/api/referral-status", headers=hdrs)
    assert rr.status_code == 200, rr.text[:200]
    body = rr.json()
    assert body["wallet_connected"] is True
    assert body["referred_by"] == config.REFERRAL_CODE
    assert body["is_ours"] is True
    assert body["error"] is None


def test_set_referrer_without_wallet_is_clean(client):
    """POST /api/set-referrer ohne Wallet → {ok: false} mit Hinweis, nie 500."""
    r = _register(client, "ref-set-nowallet@test.local", "refpw12345")
    assert r.status_code == 200
    rr = client.post("/api/set-referrer", headers=_auth_headers(r))
    assert rr.status_code == 200, rr.text[:200]
    body = rr.json()
    assert body["ok"] is False
    assert "wallet" in body["detail"].lower()


# ── M-7: Single-Trader-Guard (exklusiver flock) ─────────────────────────────
def test_m7_trader_lock_acquire_and_release():
    """M-7: erster Acquire hält den Lock (True). Hält ein ANDERER fd denselben
    Pfad exklusiv, liefert ein frischer Acquire False (= API-only, kein
    Doppel-Trading). Release ist sauber + idempotent."""
    fcntl = pytest.importorskip("fcntl")  # POSIX-only; prod ist Linux
    # Sauberer Startzustand.
    main._release_trader_lock()
    assert main._trader_lock_fd is None

    # 1) Wir nehmen den Lock — muss True liefern und den fd offen halten.
    assert main._acquire_trader_lock() is True
    assert main._trader_lock_fd is not None
    path = main._trader_lock_path()

    # 2) Solange WIR ihn halten, simuliert ein zweiter Prozess: derselbe Pfad,
    #    eigener fd. flock(LOCK_EX|LOCK_NB) auf denselben fd-Owner ist re-entrant,
    #    daher echten zweiten fd öffnen und prüfen, dass er NICHT exklusiv geht.
    with open(path, "w") as other:
        with pytest.raises(OSError):
            fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    # 3) Release gibt den Lock frei; ein zweiter fd kann ihn danach nehmen.
    main._release_trader_lock()
    assert main._trader_lock_fd is None
    with open(path, "w") as other:
        fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # darf nicht raisen
        fcntl.flock(other.fileno(), fcntl.LOCK_UN)

    # 4) Doppelter Release ist ein no-op (kein Crash).
    main._release_trader_lock()


def test_m7_second_acquire_is_false_when_held(monkeypatch, tmp_path):
    """M-7 (Kern): hält ein FREMDER Prozess (anderer fd) den Lock exklusiv, liefert
    _acquire_trader_lock False → der Prozess startet Listener/Sync NICHT."""
    fcntl = pytest.importorskip("fcntl")
    main._release_trader_lock()

    lockfile = str(tmp_path / "goathub.lock")
    monkeypatch.setattr(main, "_trader_lock_path", lambda: lockfile)

    # Ein "fremder" Prozess hält den Lock.
    holder = open(lockfile, "w")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        # Unser Acquire muss scheitern (False), ohne zu crashen, und KEINEN fd
        # auf dem Modul-Global zurücklassen.
        assert main._acquire_trader_lock() is False
        assert main._trader_lock_fd is None
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()
        main._release_trader_lock()


# ── L-16 + M-21: Dashboard-Observability ────────────────────────────────────
class _FakeInfoSpotFail:
    """Info-Fake: user_state ok, aber spot_user_state wirft → L-16-Pfad."""
    def __init__(self, spot_fails=True):
        self._spot_fails = spot_fails

    def user_state(self, address):
        return {"marginSummary": {"accountValue": "1000.0"}, "assetPositions": []}

    def spot_user_state(self, address):
        if self._spot_fails:
            raise RuntimeError("spot read down")
        return {"balances": [{"coin": "USDC", "total": "200", "hold": "0"}]}

    def user_fills(self, address):
        return []


def _wallet_for(client, email):
    """User registrieren + Wallet verbinden, headers zurückgeben."""
    from eth_account import Account
    r = _register(client, email, "dashpw12345")
    assert r.status_code == 200
    hdrs = _auth_headers(r)
    agent = Account.create()
    agent_key = agent.key.hex()
    if not agent_key.startswith("0x"):
        agent_key = "0x" + agent_key
    addr = "0x" + "44" * 20
    r = client.post("/api/wallet", json={"hl_account_address": addr,
                                         "hl_api_secret": agent_key}, headers=hdrs)
    assert r.status_code == 200, r.text[:200]
    return hdrs


def test_l16_dashboard_marks_incomplete_balance_on_spot_fail(client, monkeypatch):
    """L-16: schlägt der Spot-USDC-Read fehl, ist die Balance unvollständig
    (nur Perps-Equity). Vorher wurde der Fehler still geschluckt; jetzt markiert
    der Snapshot account.spot_balance_ok=False, damit das Frontend das kennzeichnet."""
    import app.hyperliquid_exec as hl_exec
    import app.main as m
    m._snapshot_cache.clear()
    monkeypatch.setattr(m.config, "DEMO_MODE", False, raising=False)
    monkeypatch.setattr(hl_exec, "get_info", lambda testnet: _FakeInfoSpotFail(spot_fails=True))

    hdrs = _wallet_for(client, "dash-spotfail@test.local")
    r = client.get("/api/dashboard", headers=hdrs)
    assert r.status_code == 200, r.text[:200]
    acct = r.json()["account"]
    assert acct["spot_balance_ok"] is False
    # Balance ist trotzdem da (Perps-Equity), nur als unvollständig markiert.
    assert acct["balance"] == 1000.0


def test_l16_dashboard_spot_ok_when_read_succeeds(client, monkeypatch):
    """L-16: erfolgreicher Spot-Read → spot_balance_ok=True, Balance = Perps + Spot."""
    import app.hyperliquid_exec as hl_exec
    import app.main as m
    m._snapshot_cache.clear()
    monkeypatch.setattr(m.config, "DEMO_MODE", False, raising=False)
    monkeypatch.setattr(hl_exec, "get_info", lambda testnet: _FakeInfoSpotFail(spot_fails=False))

    hdrs = _wallet_for(client, "dash-spotok@test.local")
    r = client.get("/api/dashboard", headers=hdrs)
    assert r.status_code == 200, r.text[:200]
    acct = r.json()["account"]
    assert acct["spot_balance_ok"] is True
    assert acct["balance"] == 1200.0   # 1000 Perps + (200 total − 0 hold) Spot


def test_m21_activity_ts_is_utc_marked(client, monkeypatch):
    """M-21 (Hook #6): Activity-Timestamps werden mit 'Z' (UTC) serialisiert,
    damit der Browser sie NICHT als Lokalzeit liest. Naive-UTC in der DB →
    isoformat()+'Z' an der UI-Grenze."""
    import app.hyperliquid_exec as hl_exec
    import app.main as m
    m._snapshot_cache.clear()
    monkeypatch.setattr(m.config, "DEMO_MODE", False, raising=False)
    monkeypatch.setattr(hl_exec, "get_info", lambda testnet: _FakeInfoSpotFail(spot_fails=False))

    hdrs = _wallet_for(client, "dash-ts@test.local")
    # Eine Activity-Zeile für diesen User anlegen.
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.email == "dash-ts@test.local").first()
        db.add(Activity(user_id=u.id, kind="order", text="opened BTC"))
        db.commit()
    finally:
        db.close()

    r = client.get("/api/dashboard", headers=hdrs)
    assert r.status_code == 200, r.text[:200]
    activity = r.json()["activity"]
    assert activity, "expected at least one activity row"
    assert activity[0]["ts"].endswith("Z"), activity[0]["ts"]
    # Form: ISO-8601 mit UTC-Marker (z.B. 2026-06-13T09:00:00Z)
    assert "T" in activity[0]["ts"]
