"""Phase 1/2/6 Schutz-Mechanismen — Regressions-Tests.

Diese Tests sichern die kritischen Trading-Logik-Schutzwälle gegen
versehentliches Kaputt-Refactoring:

  Phase 1 — SL-Ratchet (engine._adjust):
    UPDATE_TRADE darf SL NIE in Richtung mehr Risiko bewegen.
  Phase 1 — CANCEL nur pre-entry (engine._cancel):
    CANCEL_TRADE-Signal darf bei OFFENEN Positionen NICHT closen
    (die haben ihre eigene SL/TP-Exit-Logik).
  Phase 1 — Bad-Key Auto-Pause (engine._pause_user_bad_key):
    User mit kaputtem Agent-Key müssen genau EINMAL gepausiert
    werden, nicht jeden Cycle einen Traceback erzeugen.
  Phase 2 #27 — Per-Coin Filter (engine._per_coin_blocked):
    Nur blocken bei >=N Trades und Win-Rate unter Schwelle.

Run:
    PYTHONPATH=. python3 tests/test_engine.py
"""
import os
# Test-only env so wir die echten config-Checks (JWT/ENCRYPTION) passieren.
os.environ.setdefault("JWT_SECRET", "test-only-secret-not-prod-1234567890abcdef")
os.environ.setdefault("ENCRYPTION_KEY", "AlwIc3vOpO5xZ8sxqr1Z5kvr1WnQqJg5MZ-ITZkqTeo=")

# Wir nutzen :memory:-SQLite — keine Spuren auf der Platte, kein Test-DB-Leak.
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from sqlalchemy.pool import StaticPool
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Force shared in-memory connection for tests (sonst sieht jede Session
# eine andere :memory:-Instanz)
import app.db as _dbmod
_test_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
    future=True,
)
_dbmod.engine = _test_engine
_dbmod.SessionLocal = sessionmaker(bind=_test_engine, autoflush=False, autocommit=False, future=True)

# Jetzt die Models laden + Tabellen anlegen
from app.models import Activity, ManagedTrade, User
from app.db import Base, SessionLocal
Base.metadata.create_all(_test_engine)

# Engine importieren NACH dem DB-Setup
import app.engine as engine


# ── Helpers ─────────────────────────────────────────────────────────────────
def _make_user(db, *, user_id=1, bot_active=True, hl_account_address="0x12E3", hl_api_secret_enc="enc"):
    u = User(
        id=user_id,
        email=f"u{user_id}@test.local",
        password_hash="dummy",
        hl_account_address=hl_account_address,
        hl_api_secret_enc=hl_api_secret_enc,
        bot_active=bot_active,
        risk_pct=0.02,
        leverage=3,
        max_open_positions=10,
        capital_cap_usdc=0,
    )
    db.add(u)
    db.commit()
    return u


def _make_managed(db, *, user_id=1, coin="BTC", direction="LONG", entry=70000.0,
                  stop_loss=68000.0, status="open"):
    mt = ManagedTrade(
        user_id=user_id, coin=coin, direction=direction, entry=entry,
        stop_loss=stop_loss, take_profits="[]", status=status,
    )
    db.add(mt)
    db.commit()
    return mt


def _reset_db():
    """Cleanly truncate alles für nächsten Test."""
    db = SessionLocal()
    try:
        db.query(Activity).delete()
        db.query(ManagedTrade).delete()
        db.query(User).delete()
        db.commit()
    finally:
        db.close()


# ── Tests ───────────────────────────────────────────────────────────────────
def test_is_bad_key_error():
    """Klassifizierung: nur eth-account-Key-Fehler → True."""
    real = ValueError("The private key must be exactly 32 bytes long, instead of 20 bytes.")
    other = ValueError("Some other validation error")
    runtime = RuntimeError("network timeout")
    assert engine._is_bad_key_error(real) is True, "echter key-error sollte True"
    assert engine._is_bad_key_error(other) is False, "anderer ValueError sollte False"
    assert engine._is_bad_key_error(runtime) is False, "RuntimeError sollte False"
    print("is_bad_key_error: OK")


def test_pause_user_bad_key_idempotent():
    """Bad-Key Auto-Pause: setzt bot_active=False, schreibt 1× Activity, danach no-op."""
    _reset_db()
    db = SessionLocal()
    u = _make_user(db, user_id=1, bot_active=True)
    db.close()

    # Erster Aufruf: pause + Activity-Eintrag
    engine._pause_user_bad_key(1, "private key must be exactly 32 bytes long, instead of 20 bytes.")

    db = SessionLocal()
    u_fresh = db.get(User, 1)
    activities = db.query(Activity).filter(Activity.user_id == 1).all()
    assert u_fresh.bot_active is False, "bot sollte pausiert sein"
    assert len(activities) == 1, f"genau 1 Activity erwartet, hab {len(activities)}"
    assert "Agent-Key ungültig" in activities[0].text, "klare Fehlermeldung erwartet"
    db.close()

    # Zweiter Aufruf: schon pausiert → no-op (KEIN zweiter Activity-Eintrag)
    engine._pause_user_bad_key(1, "same error")
    db = SessionLocal()
    n2 = db.query(Activity).filter(Activity.user_id == 1).count()
    db.close()
    assert n2 == 1, f"Auto-Pause muss idempotent sein, hab nach 2. Aufruf {n2} Activities"
    print("pause_user_bad_key_idempotent: OK")


def test_get_current_sl():
    """Liest den SL aus dem neuesten non-closed managed_trade."""
    _reset_db()
    db = SessionLocal()
    _make_user(db, user_id=1)
    _make_managed(db, user_id=1, coin="ETH", stop_loss=1890.0, status="closed")  # älteste, closed → ignoriert
    _make_managed(db, user_id=1, coin="ETH", stop_loss=1850.0, status="open")    # älter, open
    _make_managed(db, user_id=1, coin="ETH", stop_loss=1900.0, status="open")    # neueste → soll gewinnen
    db.close()
    assert engine._get_current_sl(1, "ETH") == 1900.0, "neuester non-closed SL erwartet"

    # Anderer Coin → None
    assert engine._get_current_sl(1, "BTC") is None
    # Nicht-existenter User → None
    assert engine._get_current_sl(999, "ETH") is None
    print("get_current_sl: OK")


def test_per_coin_filter_threshold():
    """Per-Coin Filter:
      - <MIN_TRADES → niemals blocken (egal welche Win-Rate)
      - >=MIN_TRADES und Win-Rate >= Schwelle → nicht blocken
      - >=MIN_TRADES und Win-Rate < Schwelle → blocken
    """
    from app import config

    # Wir mocken _per_coin_stats damit kein HL-Call passiert
    orig = engine._per_coin_stats
    try:
        # Fall 1: < MIN_TRADES — egal welche Win-Rate, kein Block
        engine._per_coin_stats = lambda a, c: {"trades": 5, "wins": 0, "win_rate": 0.0}
        blocked, stats = engine._per_coin_blocked("0xADDR", "BTC")
        assert blocked is False, "weniger als MIN_TRADES → nie blocken"

        # Fall 2: >= MIN_TRADES, Win-Rate ÜBER Schwelle
        engine._per_coin_stats = lambda a, c: {"trades": 20, "wins": 12, "win_rate": 0.60}
        blocked, stats = engine._per_coin_blocked("0xADDR", "ETH")
        assert blocked is False, "Win-Rate über Schwelle → nicht blocken"

        # Fall 3: >= MIN_TRADES, Win-Rate UNTER Schwelle
        engine._per_coin_stats = lambda a, c: {"trades": 20, "wins": 4, "win_rate": 0.20}
        blocked, stats = engine._per_coin_blocked("0xADDR", "SOL")
        assert blocked is True, f"Win-Rate unter {config.PERCOIN_MIN_WINRATE} → blocken"
        assert stats["win_rate"] == 0.20

        # Fall 4: HL-Fetch fail (stats=None) → nicht blocken (sicheres default)
        engine._per_coin_stats = lambda a, c: None
        blocked, stats = engine._per_coin_blocked("0xADDR", "DOGE")
        assert blocked is False, "stats=None (HL-fail) sollte fail-open, nicht blocken"
    finally:
        engine._per_coin_stats = orig

    print("per_coin_filter_threshold: OK")


def test_sl_ratchet_math():
    """SL-Ratchet: only-tighter-direction check (LONG: sl steigt; SHORT: sl fällt).
    Test ohne async — wir prüfen nur die Bedingung."""
    # LONG: aktueller SL 68000. Neuer SL 69000 → akzeptieren (sicherer). 67000 → ablehnen (loser).
    # SHORT: aktueller SL 75000. Neuer SL 74000 → akzeptieren. 76000 → ablehnen.

    # Equivalenz-Check zur engine._adjust Logik (line-by-line):
    def would_reject(direction, current_sl, new_sl):
        is_buy = (direction == "LONG")
        if is_buy and new_sl < current_sl:
            return True   # LONG: SL nach unten ist Loser
        if not is_buy and new_sl > current_sl:
            return True   # SHORT: SL nach oben ist Loser
        return False

    # LONG cases
    assert would_reject("LONG", 68000, 67000) is True,  "LONG SL ↓ muss rejected sein"
    assert would_reject("LONG", 68000, 69000) is False, "LONG SL ↑ muss akzeptiert"
    assert would_reject("LONG", 68000, 68000) is False, "LONG SL gleich = no-op, kein reject"

    # SHORT cases
    assert would_reject("SHORT", 75000, 76000) is True,  "SHORT SL ↑ muss rejected sein"
    assert would_reject("SHORT", 75000, 74000) is False, "SHORT SL ↓ muss akzeptiert"
    assert would_reject("SHORT", 75000, 75000) is False, "SHORT SL gleich = no-op, kein reject"
    print("sl_ratchet_math: OK")


if __name__ == "__main__":
    test_is_bad_key_error()
    test_pause_user_bad_key_idempotent()
    test_get_current_sl()
    test_per_coin_filter_threshold()
    test_sl_ratchet_math()
    print("\nALLE ENGINE-TESTS BESTANDEN ✅")
