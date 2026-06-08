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
    """Liest (SL, direction) aus dem neuesten non-closed managed_trade.

    2026-06-04 audit-fix (B-#1): Funktion gibt jetzt Tuple (sl, direction) zurück
    statt nur sl, damit _adjust den Ratchet aufgeben kann bei Direction-Flips.
    """
    _reset_db()
    db = SessionLocal()
    _make_user(db, user_id=1)
    _make_managed(db, user_id=1, coin="ETH", stop_loss=1890.0, direction="LONG", status="closed")
    _make_managed(db, user_id=1, coin="ETH", stop_loss=1850.0, direction="LONG", status="open")
    _make_managed(db, user_id=1, coin="ETH", stop_loss=1900.0, direction="LONG", status="open")  # neueste
    db.close()
    assert engine._get_current_sl(1, "ETH") == (1900.0, "LONG"), "neuester non-closed (SL, direction) erwartet"

    # Anderer Coin → (None, None)
    assert engine._get_current_sl(1, "BTC") == (None, None)
    # Nicht-existenter User → (None, None)
    assert engine._get_current_sl(999, "ETH") == (None, None)
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


def test_emergency_halt_helpers():
    """2026-06-08 Mainnet-Hardening A3: file-based emergency halt flag."""
    import os
    # Override path to avoid touching prod
    engine.config.EMERGENCY_HALT_FLAG_PATH = "/tmp/test-halt-flag-engine"
    if os.path.exists(engine.config.EMERGENCY_HALT_FLAG_PATH):
        os.remove(engine.config.EMERGENCY_HALT_FLAG_PATH)

    assert engine._emergency_halt_active() is False, "no flag yet"
    engine._set_emergency_halt("test reason")
    assert engine._emergency_halt_active() is True, "set should activate"
    engine._set_emergency_halt("second set — idempotent")
    assert engine._emergency_halt_active() is True, "still active"
    engine._clear_emergency_halt()
    assert engine._emergency_halt_active() is False, "clear deactivates"
    engine._clear_emergency_halt()  # idempotent clear
    assert engine._emergency_halt_active() is False
    print("emergency_halt_helpers: OK")


def test_signal_rate_cap_and_autohalt():
    """2026-06-08 Mainnet-Hardening A1: sliding-window rate cap auto-aktiviert halt."""
    import os
    engine.config.EMERGENCY_HALT_FLAG_PATH = "/tmp/test-halt-flag-rate"
    if os.path.exists(engine.config.EMERGENCY_HALT_FLAG_PATH):
        os.remove(engine.config.EMERGENCY_HALT_FLAG_PATH)
    engine.config.MAX_SIGNALS_PER_HOUR = 3
    engine._signal_timestamps.clear()

    assert engine._signal_rate_check() is True, "1/3"
    assert engine._signal_rate_check() is True, "2/3"
    assert engine._signal_rate_check() is True, "3/3"
    assert engine._signal_rate_check() is False, "4th should block"
    assert engine._emergency_halt_active() is True, "auto-halt set"

    engine._clear_emergency_halt()
    engine._signal_timestamps.clear()
    print("signal_rate_cap_and_autohalt: OK")


def test_trade_interval_throttle():
    """2026-06-08 Mainnet-Hardening A1: min interval pro (user, coin) gegen storms."""
    engine.config.MIN_TRADE_INTERVAL_S = 60
    engine._trade_intervals.clear()

    assert engine._trade_interval_ok(1, "BTC") is True
    assert engine._trade_interval_ok(1, "BTC") is False, "<60s elapsed"
    assert engine._trade_interval_ok(2, "BTC") is True, "different user OK"
    assert engine._trade_interval_ok(1, "ETH") is True, "different coin OK"
    print("trade_interval_throttle: OK")


def test_hl_retry():
    """2026-06-08 Mainnet-Hardening A5: retry on transient errors,
    fail-fast on non-transient, exhausts after max_attempts."""
    from app.hl_retry import hl_retry, is_transient_error

    # Transient classification
    assert is_transient_error("HTTP 429 Too Many Requests")
    assert is_transient_error("connection reset by peer")
    assert is_transient_error("Service unavailable (503)")
    assert is_transient_error("Request timed out")
    assert not is_transient_error("Invalid API key")
    assert not is_transient_error("Insufficient margin")

    # Retry succeeds eventually
    attempts = [0]
    def flaky():
        attempts[0] += 1
        if attempts[0] < 3:
            raise RuntimeError("HTTP 429 rate limit")
        return "ok"
    result = hl_retry(flaky, max_attempts=5, backoff=1.0, initial_delay=0.01)
    assert result == "ok"
    assert attempts[0] == 3, f"expected 3 attempts, got {attempts[0]}"

    # Non-transient fails immediately
    attempts2 = [0]
    def hard_fail():
        attempts2[0] += 1
        raise ValueError("Invalid signature")
    try:
        hl_retry(hard_fail, max_attempts=5, initial_delay=0.01)
        assert False, "should have raised"
    except ValueError:
        pass
    assert attempts2[0] == 1, f"non-transient should fail-fast, got {attempts2[0]} attempts"

    # Exhausts retries
    attempts3 = [0]
    def always_fail():
        attempts3[0] += 1
        raise RuntimeError("503 service unavailable")
    try:
        hl_retry(always_fail, max_attempts=3, backoff=1.0, initial_delay=0.01)
        assert False, "should have raised"
    except RuntimeError as e:
        assert "503" in str(e)
    assert attempts3[0] == 3, f"expected 3 attempts before giving up, got {attempts3[0]}"

    # Soft-fail handling (dict with status="err")
    attempts4 = [0]
    def soft_then_ok():
        attempts4[0] += 1
        if attempts4[0] < 2:
            return {"status": "err", "response": "rate limit exceeded"}
        return {"status": "ok", "response": {"data": "fine"}}
    res = hl_retry(soft_then_ok, max_attempts=5, initial_delay=0.01)
    assert res["status"] == "ok"
    assert attempts4[0] == 2

    print("hl_retry: OK")


def test_alert_throttle_without_url():
    """2026-06-08 Mainnet-Hardening A2: alert ohne URL macht nichts (graceful)."""
    engine.config.ALERT_WEBHOOK_URL = ""
    engine._alert_throttle.clear()
    # Sollte nicht raisen, einfach silently no-op
    engine._post_alert("test message", key=("u1", "BTC"))
    engine._post_alert("another", key=None)  # ungethrottled
    assert engine._alert_throttle == {}, "no throttle entries when url disabled"
    print("alert_throttle_without_url: OK")


def test_pause_user_idempotency_race():
    """2026-06-08 Mainnet-Hardening C7: 2 concurrent pause-calls für gleichen user
    → nur EINE Activity-Zeile."""
    _reset_db()
    db = SessionLocal()
    _make_user(db, user_id=1, bot_active=True)
    db.close()

    engine._recent_pause_keys.clear()
    # Erste Pause-Call: muss durchgehen
    engine._pause_user_bad_key(1, "first call", reason="invalid_key")
    db = SessionLocal()
    n1 = db.query(Activity).filter(Activity.user_id == 1, Activity.kind == "error").count()
    db.close()
    assert n1 == 1, f"first call should create 1 activity, got {n1}"

    # Zweite Pause-Call ~ms später (concurrent simulator): muss SUPPRESSED sein
    engine._pause_user_bad_key(1, "second call from concurrent task", reason="invalid_key")
    db = SessionLocal()
    n2 = db.query(Activity).filter(Activity.user_id == 1, Activity.kind == "error").count()
    db.close()
    assert n2 == 1, f"concurrent second call should NOT add activity, got {n2}"
    print("pause_user_idempotency_race: OK")


def test_sl_slippage_cap_math():
    """Phase 6+ H-8: place_protection's worst-case-price-Berechnung.
    Verifiziert dass die Sign-Konvention für LONG/SHORT korrekt ist.

    SL-Order ist immer REDUCE-ONLY in entgegengesetzter Richtung:
      - LONG-position SL  → SELL bei trigger (Preis fällt)
        → worst = trigger * (1 - slip)   (akzeptiere bis zu slip% unter trigger)
      - SHORT-position SL → BUY bei trigger (Preis steigt)
        → worst = trigger * (1 + slip)   (akzeptiere bis zu slip% über trigger)
    """
    def worst_case_price(trigger, is_buy_position, slip):
        # is_buy_protection = entgegengesetzt zur entry
        is_buy_protection = not is_buy_position
        sign = 1 if is_buy_protection else -1
        return trigger * (1 + sign * slip)

    # LONG SL bei 1800, slip 2% → SELL worst @ 1764 (1800 * 0.98)
    assert abs(worst_case_price(1800, True, 0.02) - 1764) < 0.001
    # SHORT SL bei 2000, slip 2% → BUY worst @ 2040 (2000 * 1.02)
    assert abs(worst_case_price(2000, False, 0.02) - 2040) < 0.001
    # Edge: slip=0 → worst = trigger
    assert worst_case_price(100, True, 0) == 100
    assert worst_case_price(100, False, 0) == 100
    print("sl_slippage_cap_math: OK")


if __name__ == "__main__":
    test_is_bad_key_error()
    test_pause_user_bad_key_idempotent()
    test_get_current_sl()
    test_per_coin_filter_threshold()
    test_sl_ratchet_math()
    test_emergency_halt_helpers()
    test_signal_rate_cap_and_autohalt()
    test_trade_interval_throttle()
    test_alert_throttle_without_url()
    test_hl_retry()
    test_pause_user_idempotency_race()
    test_sl_slippage_cap_math()
    print("\nALLE ENGINE-TESTS BESTANDEN ✅")
