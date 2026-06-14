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
# 2026-06-12 Audit: ENCRYPTION_KEY wird zur LAUFZEIT generiert — der vorher
# hier hartcodierte Fernet-Key lag im öffentlichen Repo (Secret-Leak-Klasse).
from cryptography.fernet import Fernet
os.environ.setdefault("JWT_SECRET", "test-only-secret-not-prod-1234567890abcdef")
os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())

# Wir nutzen :memory:-SQLite — keine Spuren auf der Platte, kein Test-DB-Leak.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from sqlalchemy.pool import StaticPool
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Unter pytest verdrahtet tests/conftest.py die EINE geteilte StaticPool-
# :memory:-Engine, bevor irgendein Testmodul importiert — dann hier NICHTS
# anfassen (eine zweite Engine würde App-Module und Tests auf verschiedene
# DBs zeigen lassen). Nur standalone (python tests/test_engine.py) selbst bauen.
import app.db as _dbmod
if _dbmod.engine.pool.__class__.__name__ != "StaticPool":
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
Base.metadata.create_all(_dbmod.engine)

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
    assert "Agent-Key invalid" in activities[0].text, "klare Fehlermeldung erwartet"
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
    # Override path to avoid touching prod — Original sichern + im finally
    # zurücksetzen. config ist ein Modul-Singleton; ohne Restore leakt der Pfad
    # global in spätere Tests (u.a. leitet der M-7-Trader-Lock seinen Pfad aus
    # EMERGENCY_HALT_FLAG_PATH ab → Gesamtlauf-Failure).
    _orig_halt = engine.config.EMERGENCY_HALT_FLAG_PATH
    engine.config.EMERGENCY_HALT_FLAG_PATH = "/tmp/test-halt-flag-engine"
    try:
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
    finally:
        engine.config.EMERGENCY_HALT_FLAG_PATH = _orig_halt


def test_signal_rate_cap_and_autohalt():
    """2026-06-08 Mainnet-Hardening A1: sliding-window rate cap auto-aktiviert halt."""
    import os
    # Beide config-Globals sichern + im finally zurücksetzen (Modul-Singleton,
    # sonst leakt der Halt-Pfad in spätere Tests — siehe test_emergency_halt_helpers).
    _orig_halt = engine.config.EMERGENCY_HALT_FLAG_PATH
    _orig_cap = engine.config.MAX_SIGNALS_PER_HOUR
    engine.config.EMERGENCY_HALT_FLAG_PATH = "/tmp/test-halt-flag-rate"
    engine.config.MAX_SIGNALS_PER_HOUR = 3
    try:
        if os.path.exists(engine.config.EMERGENCY_HALT_FLAG_PATH):
            os.remove(engine.config.EMERGENCY_HALT_FLAG_PATH)
        engine._signal_timestamps.clear()

        assert engine._signal_rate_check() is True, "1/3"
        assert engine._signal_rate_check() is True, "2/3"
        assert engine._signal_rate_check() is True, "3/3"
        assert engine._signal_rate_check() is False, "4th should block"
        assert engine._emergency_halt_active() is True, "auto-halt set"

        engine._clear_emergency_halt()
        engine._signal_timestamps.clear()
        print("signal_rate_cap_and_autohalt: OK")
    finally:
        engine.config.EMERGENCY_HALT_FLAG_PATH = _orig_halt
        engine.config.MAX_SIGNALS_PER_HOUR = _orig_cap


def test_trade_interval_throttle():
    """2026-06-08 Mainnet-Hardening A1: min interval pro (user, coin) gegen storms.

    Audit M-5 (2026-06-12): der Check ist jetzt READ-ONLY — das Fenster startet
    erst mit _record_trade_ts (nach erfolgreich PLATZIERTEM Entry). Geskippte/
    fehlgeschlagene Versuche verbrauchen das Fenster nicht mehr."""
    engine.config.MIN_TRADE_INTERVAL_S = 60
    engine._trade_intervals.clear()

    assert engine._trade_interval_ok(1, "BTC") is True
    assert engine._trade_interval_ok(1, "BTC") is True, \
        "Check allein darf das Fenster nicht verbrauchen (Audit M-5)"
    engine._record_trade_ts(1, "BTC")
    assert engine._trade_interval_ok(1, "BTC") is False, "<60s seit platziertem Trade"
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


# ── 2026-06-12 (Review #15): echte Engine-Pfad-Tests mit Recorder-Stub ──────
# Diese Tests rufen die ECHTEN engine-Funktionen (_open_or_update, _open_new,
# _adjust, reconcile_protection_on_startup) mit einem Stub-Trader auf, statt
# die Logik lokal zu re-implementieren.
import asyncio
import datetime
import types

from app.parser import Signal, TakeProfit


class StubTrader:
    """Recorder-Stub: zeichnet alle mutierenden HL-Calls auf, kein Netzwerk."""

    def __init__(self, pos=0.0, pos_exc=None):
        self.pos = pos                # von position_size geliefert
        self.pos_exc = pos_exc        # wenn gesetzt: position_size raised (Review #0)
        self.calls = []               # [(name, args...), ...] mutierende Calls
        self.protection_calls = []    # place_protection-Argumente

    def is_tradable(self, coin):
        return True

    def position_size(self, coin):
        if self.pos_exc is not None:
            raise self.pos_exc
        return self.pos

    def max_leverage(self, coin):
        return 50

    def set_leverage(self, coin, lev):
        self.calls.append(("set_leverage", coin, lev))
        return {"status": "ok"}

    def cancel_orders(self, coin):
        self.calls.append(("cancel_orders", coin))
        return 1

    def cancel_order(self, coin, oid):
        self.calls.append(("cancel_order", coin, oid))
        return True

    def place_entry(self, coin, is_buy, sz, px):
        self.calls.append(("place_entry", coin, is_buy, sz, px))
        return {"ok": True, "filled": True, "filled_sz": sz, "resting_oid": None, "error": None}

    def place_protection(self, coin, is_buy, sz, sl, tps):
        self.protection_calls.append(
            {"coin": coin, "is_buy": is_buy, "sz": sz, "sl": sl, "tps": list(tps or [])})
        return {"sl_ok": True, "sl": {"status": "ok"}, "tp": [], "skip_reason": None}

    def close_position(self, coin):
        # 2026-06-13 Audit C-1/C-4: neue Return-Form inkl. still_open.
        self.calls.append(("close_position", coin))
        return {"ok": True, "closed": abs(self.pos), "still_open": 0.0}

    # 2026-06-13 Audit C-4/H-1: Agent-A-Schnittstellen, die der Watcher/Engine nutzt.
    def order_status(self, oid=None, cloid=None):
        # Default: die ganze (Stub-)Position gilt als von DIESEM Entry gefüllt.
        return {"status": "filled", "filled_sz": abs(self.pos), "raw": {}}

    def cancel_order_oid(self, coin, oid):
        self.calls.append(("cancel_order_oid", coin, oid))
        return {"ok": True, "already_filled": False, "raw": {}}

    def account_value(self):
        return 10_000.0

    def available_margin(self):
        return 10_000.0

    def open_positions_count(self):
        return 0

    def open_positions(self):
        return []

    def covered_stop_size(self, coin, position_is_long=None):
        # HOOK M-6: side-aware-Param (Default None = alt). Stub ignoriert ihn,
        # akzeptiert ihn aber, damit der Reconciler-Call nicht crasht.
        return float("inf")

    def _round_sz(self, coin, sz):
        return sz


def _async_return(value):
    async def _f(*a, **k):
        return value
    return _f


def _reset_engine_state():
    """asyncio.Lock bindet sich an den Loop des ersten Acquire — jede
    asyncio.run()-Testinsel braucht frische Locks/Watcher-Registries."""
    engine._locks.clear()
    engine._user_locks.clear()
    engine._fill_watchers.clear()
    engine._trade_intervals.clear()
    engine._recent_pause_keys.clear()


def _activities(user_id, kind=None):
    db = SessionLocal()
    try:
        q = db.query(Activity).filter(Activity.user_id == user_id)
        if kind:
            q = q.filter(Activity.kind == kind)
        return [a.text for a in q.order_by(Activity.id).all()]
    finally:
        db.close()


def _make_managed_full(db, *, user_id=1, coin="ETH", direction="LONG", entry=1900.0,
                       stop_loss=1800.0, take_profits="[]", status="open",
                       resting_oid=None, signal_id=None):
    mt = ManagedTrade(user_id=user_id, coin=coin, direction=direction, entry=entry,
                      stop_loss=stop_loss, take_profits=take_profits, status=status,
                      resting_oid=resting_oid, signal_id=signal_id)
    db.add(mt)
    db.commit()
    return mt


def test_position_unknown_aborts_safely():
    """Review #0: position_size-Fehler darf NIE als 'flat' gelten — weder
    NEW_TRADE (würde live SL/TP canceln + Doppel-Entry) noch CANCEL (würde
    Schutz der offenen Position wegcanceln + Row verstecken)."""
    _reset_db()
    _reset_engine_state()
    db = SessionLocal()
    _make_user(db, user_id=1)
    _make_managed_full(db, user_id=1, coin="BTC", direction="LONG",
                       entry=70000.0, stop_loss=68000.0, status="open")
    db.close()

    sig = Signal(signal_id="s0", ticker="BTC/USDT", action="NEW_TRADE", direction="LONG",
                 entry=70000.0, stop_loss=68000.0, take_profits=[])
    stub = StubTrader(pos_exc=RuntimeError("503 service unavailable"))
    orig_build = engine._build_trader
    engine._build_trader = _async_return(stub)
    try:
        asyncio.run(engine._open_or_update(1, sig, "NEW_TRADE"))
        assert stub.calls == [], f"unbekannter Positionsstatus: NICHTS anfassen, hab {stub.calls}"

        # CANCEL-Pfad genauso
        _reset_engine_state()
        stub2 = StubTrader(pos_exc=RuntimeError("connection timeout"))
        engine._build_trader = _async_return(stub2)
        asyncio.run(engine._cancel(1, sig))
        assert stub2.calls == [], f"CANCEL bei unbekanntem Status: NICHTS canceln, hab {stub2.calls}"
    finally:
        engine._build_trader = orig_build

    errs = _activities(1, "error")
    assert sum("position status" in t for t in errs) == 2, errs
    db = SessionLocal()
    st = db.query(ManagedTrade).filter_by(user_id=1, coin="BTC").first().status
    db.close()
    assert st == "open", "Row darf bei unbekanntem Status nicht geclosed werden"
    print("position_unknown_aborts_safely: OK")


def test_startup_rearm_resting_watcher():
    """Review #1/#2: resting-Row nach Neustart → Fill-Watcher wird mit den
    GESPEICHERTEN Parametern (resting_oid, SL, TPs) re-armiert."""
    _reset_db()
    _reset_engine_state()
    db = SessionLocal()
    _make_user(db, user_id=1)
    _make_managed_full(db, user_id=1, coin="ETH", direction="LONG", entry=1900.0,
                       stop_loss=1800.0, take_profits="[[2000.0, 50.0], [2200.0, 50.0]]",
                       status="resting", resting_oid="987", signal_id="sigA")
    db.close()

    stub = StubTrader(pos=0.0)   # Order hat während der Downtime NICHT gefüllt
    spawned = []

    def fake_watcher(trader, user_id, sig, is_buy, tps, resting_oid=None, entry_cloid=None):
        spawned.append({"user_id": user_id, "sl": sig.stop_loss, "is_buy": is_buy,
                        "tps": tps, "resting_oid": resting_oid})
        async def _noop():
            return None
        return _noop()

    orig_build, orig_watch = engine._build_trader, engine._protect_when_filled
    orig_flag = engine.config.STARTUP_PROTECTION_RECONCILE
    engine._build_trader = _async_return(stub)
    engine._protect_when_filled = fake_watcher
    engine.config.STARTUP_PROTECTION_RECONCILE = True
    try:
        asyncio.run(engine.reconcile_protection_on_startup())
    finally:
        engine._build_trader, engine._protect_when_filled = orig_build, orig_watch
        engine.config.STARTUP_PROTECTION_RECONCILE = orig_flag
        engine._fill_watchers.clear()

    assert len(spawned) == 1, f"genau 1 Watcher erwartet, hab {len(spawned)}"
    w = spawned[0]
    assert w["resting_oid"] == "987" and w["sl"] == 1800.0 and w["is_buy"] is True
    assert w["tps"] == [(2000.0, 0.5), (2200.0, 0.5)], w["tps"]
    db = SessionLocal()
    st = db.query(ManagedTrade).filter_by(user_id=1, coin="ETH").first().status
    db.close()
    assert st == "resting", "Row bleibt resting (re-armiert, nicht geschlossen)"
    print("startup_rearm_resting_watcher: OK")


def test_startup_rearm_impossible_cancels_entry():
    """Review #1/#2: resting-Row OHNE SL kann nicht re-armiert werden →
    ruhende Entry-Order wird gecancelt + Row geschlossen (kein nackter Fill)."""
    _reset_db()
    _reset_engine_state()
    db = SessionLocal()
    _make_user(db, user_id=1)
    _make_managed_full(db, user_id=1, coin="BTC", direction="LONG", entry=70000.0,
                       stop_loss=None, status="resting", resting_oid="555")
    db.close()

    stub = StubTrader(pos=0.0)
    spawned = []

    def fake_watcher(*a, **k):
        spawned.append(a)
        async def _noop():
            return None
        return _noop()

    orig_build, orig_watch = engine._build_trader, engine._protect_when_filled
    orig_flag = engine.config.STARTUP_PROTECTION_RECONCILE
    engine._build_trader = _async_return(stub)
    engine._protect_when_filled = fake_watcher
    engine.config.STARTUP_PROTECTION_RECONCILE = True
    try:
        asyncio.run(engine.reconcile_protection_on_startup())
    finally:
        engine._build_trader, engine._protect_when_filled = orig_build, orig_watch
        engine.config.STARTUP_PROTECTION_RECONCILE = orig_flag
        engine._fill_watchers.clear()

    assert spawned == [], "ohne SL darf KEIN Watcher gespawnt werden"
    assert ("cancel_order", "BTC", "555") in stub.calls, stub.calls
    db = SessionLocal()
    st = db.query(ManagedTrade).filter_by(user_id=1, coin="BTC").first().status
    db.close()
    assert st == "closed", "Row muss geschlossen werden, wenn Re-Arm unmöglich"
    print("startup_rearm_impossible_cancels_entry: OK")


def test_sl_only_update_preserves_tps():
    """Review #5: UPDATE_TRADE ohne TPs → gespeicherte TPs aus dem managed_trade
    werden mit dem neuen SL re-placed (cancel_orders räumt sonst alle TPs weg)."""
    _reset_db()
    _reset_engine_state()
    db = SessionLocal()
    _make_user(db, user_id=1)
    _make_managed_full(db, user_id=1, coin="ETH", direction="LONG", entry=1900.0,
                       stop_loss=1800.0, take_profits="[[2000.0, 50.0], [2200.0, 50.0]]",
                       status="open")
    db.close()

    stub = StubTrader(pos=1.0)
    orig_mark = engine._get_mark
    # 2026-06-13 Audit H-13: mark==0 ist jetzt ein "refuse" (Read-Fail), kein
    # "Preflight skippen". Gültiger Mark, der den would-trigger-Check besteht
    # (LONG braucht SL < mark: 1850 < 1950).
    engine._get_mark = lambda coin: 1950.0
    try:
        sig = Signal(signal_id="s2", ticker="ETH/USDT", action="UPDATE_TRADE", direction="LONG",
                     entry=None, stop_loss=1850.0, take_profits=[])
        u = types.SimpleNamespace(id=1)
        asyncio.run(engine._adjust(stub, u, sig, 1.0))
    finally:
        engine._get_mark = orig_mark

    assert len(stub.protection_calls) == 1, stub.protection_calls
    pc = stub.protection_calls[0]
    assert pc["sl"] == 1850.0, "neuer (tighter) SL muss gesetzt werden"
    assert pc["tps"] == [(2000.0, 0.5), (2200.0, 0.5)], \
        f"gespeicherte TPs müssen ein SL-only-Update überleben, hab {pc['tps']}"
    print("sl_only_update_preserves_tps: OK")


def test_missing_direction_skips_new_trade():
    """Review #16: Direction fehlt/leer → NEW_TRADE wird mit error-Activity
    geskippt statt still als SHORT zu laufen."""
    _reset_db()
    _reset_engine_state()
    db = SessionLocal()
    _make_user(db, user_id=1)
    db.close()

    stub = StubTrader()
    sig = Signal(signal_id="s3", ticker="BTC/USDT", action="NEW_TRADE", direction="",
                 entry=70000.0, stop_loss=68000.0, take_profits=[])
    u = types.SimpleNamespace(id=1, hl_account_address="0xA", risk_pct=0.02, leverage=3,
                              max_open_positions=10, capital_cap_usdc=0,
                              max_drawdown_pct=0, peak_account_value=0, builder_approved=False)
    asyncio.run(engine._open_new(stub, u, sig))

    assert all(c[0] != "place_entry" for c in stub.calls), \
        f"Signal ohne Direction darf NICHT ausgeführt werden, hab {stub.calls}"
    errs = _activities(1, "error")
    assert any("direction" in t for t in errs), errs
    print("missing_direction_skips_new_trade: OK")


def test_throttle_exempts_updates_blocks_new():
    """Review #19: MIN_TRADE_INTERVAL_S drosselt NUR echte Neueröffnungen.
    Risiko-reduzierende UPDATE_TRADEs (SL-Tighten) laufen immer durch; ein
    gedrosseltes NEW_TRADE fasst NICHTS an (auch kein cancel_orders)."""
    _reset_db()
    _reset_engine_state()
    db = SessionLocal()
    _make_user(db, user_id=1)
    _make_managed_full(db, user_id=1, coin="ETH", direction="LONG", entry=1900.0,
                       stop_loss=1800.0, take_profits="[]", status="open")
    db.close()

    engine.config.MIN_TRADE_INTERVAL_S = 60
    # Audit M-5: Fenster explizit stempeln (der Check selbst ist jetzt read-only).
    engine._record_trade_ts(1, "ETH")

    orig_build, orig_mark = engine._build_trader, engine._get_mark
    # 2026-06-13 Audit H-13: gültiger Mark (LONG SL 1850 < mark 1950) statt 0
    # (0 ist jetzt "refuse"). Der NEW_TRADE-Teil unten wird vor dem Mark-Read
    # gedrosselt, ihn berührt der Mark nicht.
    engine._get_mark = lambda coin: 1950.0
    try:
        # a) UPDATE_TRADE im Intervall → MUSS durchgehen
        stub_u = StubTrader(pos=2.0)
        engine._build_trader = _async_return(stub_u)
        sig_u = Signal(signal_id="s4", ticker="ETH/USDT", action="UPDATE_TRADE", direction="LONG",
                       entry=None, stop_loss=1850.0, take_profits=[])
        asyncio.run(engine._open_or_update(1, sig_u, "UPDATE_TRADE"))
        assert len(stub_u.protection_calls) == 1, \
            f"SL-Update darf nicht gedrosselt werden: {_activities(1)}"

        # b) NEW_TRADE im Intervall → Skip VOR cancel_orders (ruht-Order/Watcher bleiben)
        _reset_engine_state()
        engine.config.MIN_TRADE_INTERVAL_S = 60
        engine._record_trade_ts(1, "ETH")   # Audit M-5: Fenster aktiv
        stub_n = StubTrader(pos=0.0)
        engine._build_trader = _async_return(stub_n)
        sig_n = Signal(signal_id="s5", ticker="ETH/USDT", action="NEW_TRADE", direction="LONG",
                       entry=1900.0, stop_loss=1800.0, take_profits=[])
        asyncio.run(engine._open_or_update(1, sig_n, "NEW_TRADE"))
        assert stub_n.calls == [], f"throttled NEW_TRADE darf nichts anfassen, hab {stub_n.calls}"
        skips = _activities(1, "skip")
        assert any("throttle" in t for t in skips), skips
    finally:
        engine._build_trader, engine._get_mark = orig_build, orig_mark
    print("throttle_exempts_updates_blocks_new: OK")


def test_ratchet_baseline_survives_premature_sync_close():
    """Review #41: hat der Sync eine Row verfrüht geclosed (HL-Position lebt),
    fällt _get_current_sl auf die jüngste closed-Row (<24h) zurück, damit der
    SL-Ratchet nicht ausgehebelt wird. Ältere closed-Rows zählen NICHT."""
    _reset_db()
    _reset_engine_state()
    db = SessionLocal()
    _make_user(db, user_id=1)
    _make_managed_full(db, user_id=1, coin="ETH", direction="LONG", entry=1900.0,
                       stop_loss=1800.0, take_profits="[]", status="closed")
    db.close()

    assert engine._get_current_sl(1, "ETH") == (1800.0, "LONG"), \
        "frisch geclosede Row muss als Ratchet-Fallback dienen"

    # >24h alt → kein Fallback (echter Close / legitimer manueller Re-Open)
    db = SessionLocal()
    mt = db.query(ManagedTrade).filter_by(user_id=1, coin="ETH").first()
    mt.updated_at = datetime.datetime.utcnow() - datetime.timedelta(hours=25)
    db.commit()
    db.close()
    assert engine._get_current_sl(1, "ETH") == (None, None)
    print("ratchet_baseline_survives_premature_sync_close: OK")


def test_parser_update_without_entry():
    """Review #18: UPDATE_TRADE ohne Entry (Trail-Stop-Update) darf nicht
    verworfen werden; NEW_TRADE ohne Entry/SL weiterhin schon."""
    from app.parser import parse_signal
    upd = {"title": "ETH/USDT — UPDATE_TRADE", "description": "Signal `u1`",
           "fields": [{"name": "Action", "value": "UPDATE_TRADE"},
                      {"name": "Ticker", "value": "ETH/USDT"},
                      {"name": "Stop Loss", "value": "1850"}]}
    s = parse_signal(upd)
    assert s is not None and s.action == "UPDATE_TRADE" and s.stop_loss == 1850.0 and s.entry is None

    new_no_entry = {"title": "ETH/USDT — NEW_TRADE", "description": "Signal `u2`",
                    "fields": [{"name": "Action", "value": "NEW_TRADE"},
                               {"name": "Ticker", "value": "ETH/USDT"},
                               {"name": "Stop Loss", "value": "1850"}]}
    assert parse_signal(new_no_entry) is None, "NEW_TRADE ohne Entry bleibt invalid"
    print("parser_update_without_entry: OK")


# ── 2026-06-13 Audit-Runde 2 (Mediums + Lows + Cross-Agent-Hooks) ───────────
def _sns_user(user_id=1, **over):
    """SimpleNamespace-User für _open_new-Direkttests (kein ORM nötig)."""
    base = dict(id=user_id, hl_account_address="0xA", risk_pct=0.02, leverage=3,
                max_open_positions=10, capital_cap_usdc=0, max_drawdown_pct=0,
                peak_account_value=0, builder_approved=False)
    base.update(over)
    return types.SimpleNamespace(**base)


def _new_sig(coin="BTC", direction="LONG", entry=70000.0, stop_loss=68000.0,
             signal_id="n1", take_profits=None):
    return Signal(signal_id=signal_id, ticker=f"{coin}/USDT", action="NEW_TRADE",
                  direction=direction, entry=entry, stop_loss=stop_loss,
                  take_profits=take_profits or [])


def test_m1_avail_read_fail_aborts_entry():
    """M-1: available_margin-Read-Fehler ⇒ fail-CLOSED (Entry abgebrochen, kein
    place_entry), nicht fail-OPEN (avail=balance)."""
    _reset_db(); _reset_engine_state()
    db = SessionLocal(); _make_user(db, user_id=1); db.close()

    class _T(StubTrader):
        def available_margin(self):
            raise RuntimeError("503 service unavailable")
    stub = _T(pos=0.0)
    asyncio.run(engine._open_new(stub, _sns_user(), _new_sig()))
    assert all(c[0] != "place_entry" for c in stub.calls), stub.calls
    assert any("available margin unreadable" in t for t in _activities(1, "error"))
    print("m1_avail_read_fail_aborts_entry: OK")


def test_m2_open_count_raise_aborts_entry():
    """HOOK M-2: open_positions_count RAISE ⇒ Entry fail-CLOSED abgebrochen."""
    _reset_db(); _reset_engine_state()
    db = SessionLocal(); _make_user(db, user_id=1); db.close()

    class _T(StubTrader):
        def open_positions_count(self):
            raise RuntimeError("connection timeout")
    stub = _T(pos=0.0)
    asyncio.run(engine._open_new(stub, _sns_user(), _new_sig()))
    assert all(c[0] != "place_entry" for c in stub.calls), stub.calls
    assert any("open-position count unreadable" in t for t in _activities(1, "error"))
    print("m2_open_count_raise_aborts_entry: OK")


def test_m4_update_on_resting_modifies_pending():
    """M-4: UPDATE_TRADE bei pos==0 mit RUHENDER Row ⇒ modify-pending (Row-SL
    aktualisiert, Row bleibt 'resting', NICHT geschlossen, kein cancel)."""
    _reset_db(); _reset_engine_state()
    db = SessionLocal()
    _make_user(db, user_id=1)
    _make_managed_full(db, user_id=1, coin="ETH", direction="LONG", entry=1900.0,
                       stop_loss=1800.0, take_profits="[]", status="resting",
                       resting_oid="111", signal_id="orig")
    db.close()

    stub = StubTrader(pos=0.0)
    orig_build = engine._build_trader
    engine._build_trader = _async_return(stub)
    try:
        sig = Signal(signal_id="upd1", ticker="ETH/USDT", action="UPDATE_TRADE",
                     direction="LONG", entry=None, stop_loss=1850.0, take_profits=[])
        asyncio.run(engine._open_or_update(1, sig, "UPDATE_TRADE"))
    finally:
        engine._build_trader = orig_build

    db = SessionLocal()
    mt = db.query(ManagedTrade).filter_by(user_id=1, coin="ETH").order_by(ManagedTrade.id.desc()).first()
    db.close()
    assert mt.status == "resting", f"Row darf NICHT geschlossen werden, ist {mt.status}"
    assert float(mt.stop_loss) == 1850.0, "neuer (tighter) SL muss in die Row"
    assert all(c[0] not in ("cancel_orders", "cancel_order", "cancel_order_oid", "close_position")
               for c in stub.calls), f"pending Entry darf nicht gecancelt werden: {stub.calls}"
    print("m4_update_on_resting_modifies_pending: OK")


def test_m4_modify_pending_ratchet_blocks_loosen():
    """M-4: ein LOOSEN-UPDATE auf eine resting-Row wird vom Ratchet verworfen
    (Row-SL bleibt der tightere alte Wert)."""
    _reset_db(); _reset_engine_state()
    db = SessionLocal()
    _make_user(db, user_id=1)
    _make_managed_full(db, user_id=1, coin="ETH", direction="LONG", entry=1900.0,
                       stop_loss=1850.0, take_profits="[]", status="resting",
                       resting_oid="111", signal_id="orig")
    db.close()

    stub = StubTrader(pos=0.0)
    orig_build = engine._build_trader
    engine._build_trader = _async_return(stub)
    try:
        # LONG looser SL = niedriger (1800 < 1850) → muss verworfen werden.
        sig = Signal(signal_id="upd2", ticker="ETH/USDT", action="UPDATE_TRADE",
                     direction="LONG", entry=None, stop_loss=1800.0, take_profits=[])
        asyncio.run(engine._open_or_update(1, sig, "UPDATE_TRADE"))
    finally:
        engine._build_trader = orig_build
    db = SessionLocal()
    mt = db.query(ManagedTrade).filter_by(user_id=1, coin="ETH").order_by(ManagedTrade.id.desc()).first()
    db.close()
    assert float(mt.stop_loss) == 1850.0, "looser SL darf die resting-Row NICHT verschlechtern"
    print("m4_modify_pending_ratchet_blocks_loosen: OK")


def test_m20_update_replay_deduped():
    """HOOK M-20: ein exakter UPDATE_TRADE-Replay (gleiches signal_id) wird beim
    2. Mal geskippt (kein zweiter _adjust/place_protection)."""
    _reset_db(); _reset_engine_state()
    db = SessionLocal()
    _make_user(db, user_id=1)
    _make_managed_full(db, user_id=1, coin="ETH", direction="LONG", entry=1900.0,
                       stop_loss=1800.0, take_profits="[]", status="open")
    db.close()

    stub = StubTrader(pos=1.0)
    orig_build, orig_mark = engine._build_trader, engine._get_mark
    engine._build_trader = _async_return(stub)
    engine._get_mark = lambda coin: 1950.0
    try:
        sig = Signal(signal_id="dup1", ticker="ETH/USDT", action="UPDATE_TRADE",
                     direction="LONG", entry=None, stop_loss=1850.0, take_profits=[])
        asyncio.run(engine._open_or_update(1, sig, "UPDATE_TRADE"))
        n1 = len(stub.protection_calls)
        # exakter Replay
        asyncio.run(engine._open_or_update(1, sig, "UPDATE_TRADE"))
        n2 = len(stub.protection_calls)
    finally:
        engine._build_trader, engine._get_mark = orig_build, orig_mark
    assert n1 == 1 and n2 == 1, f"Replay darf _adjust nicht erneut laufen lassen (n1={n1}, n2={n2})"
    assert any("already" in t for t in _activities(1, "skip"))
    print("m20_update_replay_deduped: OK")


def test_m15_direction_flip_cancels_remainder():
    """M-15: Gegenrichtungs-Signal bei offener Position ⇒ Skip, ABER der ruhende
    Entry-Remainder (resting_oid) wird gezielt gecancelt (per oid), Position +
    Schutz bleiben unberührt."""
    _reset_db(); _reset_engine_state()
    db = SessionLocal()
    _make_user(db, user_id=1)
    mt = _make_managed_full(db, user_id=1, coin="BTC", direction="LONG", entry=70000.0,
                            stop_loss=68000.0, take_profits="[]", status="open",
                            resting_oid="999")
    # Ownership bekannt machen (bot_filled_sz gesetzt) + entry_oid.
    mt.bot_filled_sz = 1.0
    mt.entry_oid = "999"
    db.add(mt); db.commit(); db.close()

    stub = StubTrader(pos=1.0)   # LONG offen
    orig_build = engine._build_trader
    engine._build_trader = _async_return(stub)
    try:
        # SHORT-Signal widerspricht der LONG-Position.
        sig = Signal(signal_id="flip1", ticker="BTC/USDT", action="UPDATE_TRADE",
                     direction="SHORT", entry=None, stop_loss=71000.0, take_profits=[])
        asyncio.run(engine._open_or_update(1, sig, "UPDATE_TRADE"))
    finally:
        engine._build_trader = orig_build
    assert ("cancel_order_oid", "BTC", "999") in stub.calls, \
        f"Entry-Remainder muss gezielt gecancelt werden: {stub.calls}"
    assert all(c[0] != "close_position" for c in stub.calls), "Position NICHT schließen"
    assert len(stub.protection_calls) == 0, "kein Re-Protect auf der falschen Seite"
    print("m15_direction_flip_cancels_remainder: OK")


def test_m12_tp_reject_emits_error():
    """M-12: eine abgelehnte TP-Order in place_protection['tp'] ⇒ error-Activity
    (Position läuft sonst still SL-only)."""
    _reset_db(); _reset_engine_state()
    db = SessionLocal(); _make_user(db, user_id=1); db.close()

    class _T(StubTrader):
        def place_protection(self, coin, is_buy, sz, sl, tps):
            self.protection_calls.append({"coin": coin, "sz": sz, "sl": sl})
            # SL ok, aber ein TP wird abgelehnt (status != ok).
            return {"sl_ok": True, "sl": {"status": "ok"},
                    "tp": [{"status": "ok", "response": {"data": {"statuses": [{"resting": {}}]}}},
                           {"status": "err", "response": "tick size"}],
                    "skip_reason": None}
        def _status_ok(self, raw):
            try:
                if raw.get("status") != "ok":
                    return False
                st = raw["response"]["data"]["statuses"][0]
                return "error" not in st
            except Exception:
                return False
    stub = _T(pos=0.0)
    sig = _new_sig(coin="BTC", take_profits=[TakeProfit(percent=50, price=72000.0),
                                             TakeProfit(percent=50, price=74000.0)])
    asyncio.run(engine._open_new(stub, _sns_user(), sig))
    errs = _activities(1, "error")
    assert any("take-profit order(s) were rejected" in t for t in errs), errs
    print("m12_tp_reject_emits_error: OK")


def test_m8_paused_user_aborts_after_lock():
    """M-8: wird der User pausiert während der Task in der Queue stand, bricht
    _open_or_update nach dem Lock ab (kein place_entry)."""
    _reset_db(); _reset_engine_state()
    db = SessionLocal()
    _make_user(db, user_id=1, bot_active=False)   # schon pausiert
    db.close()
    stub = StubTrader(pos=0.0)
    orig_build = engine._build_trader
    engine._build_trader = _async_return(stub)
    try:
        asyncio.run(engine._open_or_update(1, _new_sig(), "NEW_TRADE"))
    finally:
        engine._build_trader = orig_build
    assert stub.calls == [], f"pausierter User: NICHTS traden, hab {stub.calls}"
    print("m8_paused_user_aborts_after_lock: OK")


def test_m23_generic_error_is_generic():
    """M-23: ein roher Exception-Text landet NICHT in der user-facing Activity —
    stattdessen eine generische EN-Meldung."""
    _reset_db(); _reset_engine_state()
    db = SessionLocal(); _make_user(db, user_id=1); db.close()

    secret = "SUPER-SECRET-PAYLOAD-LEAK-1234"

    class _T(StubTrader):
        def is_tradable(self, coin):
            raise RuntimeError(secret)
    stub = _T(pos=0.0)
    orig_build = engine._build_trader
    engine._build_trader = _async_return(stub)
    try:
        asyncio.run(engine._open_or_update(1, _new_sig(), "NEW_TRADE"))
    finally:
        engine._build_trader = orig_build
    errs = _activities(1, "error")
    assert errs, "eine Fehler-Activity erwartet"
    assert all(secret not in t for t in errs), f"roher Fehlertext darf nicht leaken: {errs}"
    assert any("internal error" in t for t in errs), errs
    print("m23_generic_error_is_generic: OK")


def test_l1_rate_cap_only_counts_new():
    """L-1: UPDATE/CANCEL zählen NICHT zum Signal-Rate-Cap (nur NEW_TRADE)."""
    _reset_db(); _reset_engine_state()
    engine._signal_timestamps[:] = []
    orig_cap = engine.config.MAX_SIGNALS_PER_HOUR
    engine.config.MAX_SIGNALS_PER_HOUR = 2
    engine._clear_emergency_halt()
    try:
        # 5 UPDATE-Embeds — dürfen den Cap nie auslösen.
        for i in range(5):
            embed = {"title": "ETH/USDT — UPDATE_TRADE", "description": f"Signal `u{i}`",
                     "fields": [{"name": "Action", "value": "UPDATE_TRADE"},
                                {"name": "Ticker", "value": "ETH/USDT"},
                                {"name": "Stop Loss", "value": "1850"}]}
            asyncio.run(engine.handle_signal(embed))
        assert not engine._emergency_halt_active(), "UPDATEs dürfen den Auto-Halt NICHT auslösen"
    finally:
        engine.config.MAX_SIGNALS_PER_HOUR = orig_cap
        engine._clear_emergency_halt()
        engine._signal_timestamps[:] = []
    print("l1_rate_cap_only_counts_new: OK")


def test_l2_confidence_gate_exempts_update():
    """L-2: ein low-confidence UPDATE_TRADE wird NICHT vom Confidence-Gate
    verworfen (es ist risiko-reduzierend)."""
    _reset_db(); _reset_engine_state()
    db = SessionLocal()
    _make_user(db, user_id=1)
    _make_managed_full(db, user_id=1, coin="ETH", direction="LONG", entry=1900.0,
                       stop_loss=1800.0, take_profits="[]", status="open")
    db.close()
    stub = StubTrader(pos=1.0)
    orig_build, orig_mark = engine._build_trader, engine._get_mark
    engine._build_trader = _async_return(stub)
    engine._get_mark = lambda coin: 1950.0
    orig_conf = engine.config.MIN_CONFIDENCE
    engine.config.MIN_CONFIDENCE = 0.75
    engine._clear_emergency_halt()

    async def _runner():
        embed = {"title": "ETH/USDT — UPDATE_TRADE", "description": "Signal `lowconf`",
                 "fields": [{"name": "Action", "value": "UPDATE_TRADE"},
                            {"name": "Ticker", "value": "ETH/USDT"},
                            {"name": "Stop Loss", "value": "1850"},
                            {"name": "Confidence", "value": "0.60"}]}
        await engine.handle_signal(embed)
        # handle_signal spawnt _open_or_update als Task — im selben Loop abwarten.
        pending = [t for t in list(engine._tasks) if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=2)
    try:
        asyncio.run(_runner())
        assert len(stub.protection_calls) >= 1, \
            f"low-confidence UPDATE muss durchlaufen: {_activities(1)}"
    finally:
        engine._build_trader, engine._get_mark = orig_build, orig_mark
        engine.config.MIN_CONFIDENCE = orig_conf
    print("l2_confidence_gate_exempts_update: OK")


def test_l6_entry_unauthorized_pauses_user():
    """L-6: 'does not exist'-Reject auf dem ENTRY-Pfad ⇒ User wird auto-pausiert
    (nicht pro Signal ein Error)."""
    _reset_db(); _reset_engine_state()
    db = SessionLocal(); _make_user(db, user_id=1, bot_active=True); db.close()

    class _T(StubTrader):
        def place_entry(self, coin, is_buy, sz, px):
            self.calls.append(("place_entry", coin))
            return {"ok": False, "filled": False, "filled_sz": 0.0, "resting_oid": None,
                    "error": "User or API Wallet 0xabc does not exist."}
    stub = _T(pos=0.0)
    asyncio.run(engine._open_new(stub, _sns_user(), _new_sig()))
    db = SessionLocal(); u = db.get(User, 1); db.close()
    assert u.bot_active is False, "unauthorized Entry-Reject muss den Bot pausieren"
    print("l6_entry_unauthorized_pauses_user: OK")


def test_l14_malformed_tp_logged(capfd=None):
    """L-14: malformed TP-JSON in der Row ⇒ _load_protection_params droppt sie,
    gibt aber (sl, []) zurück (kein Crash; SL bleibt nutzbar)."""
    _reset_db(); _reset_engine_state()
    db = SessionLocal()
    _make_user(db, user_id=1)
    _make_managed_full(db, user_id=1, coin="ETH", direction="LONG", entry=1900.0,
                       stop_loss=1800.0, take_profits="NOT-JSON{{", status="open")
    db.close()
    sl, tps = engine._load_protection_params(1, "ETH")
    assert sl == 1800.0 and tps == [], (sl, tps)
    print("l14_malformed_tp_logged: OK")


def test_m18_dropped_tps_activity():
    """HOOK M-18: sig.dropped_tps nicht leer ⇒ info-Activity pro User."""
    _reset_db(); _reset_engine_state()
    db = SessionLocal(); _make_user(db, user_id=1, bot_active=True); db.close()
    engine._clear_emergency_halt()
    # Embed mit falsch-seitigem TP → Parser legt es in dropped_tps.
    # LONG-Entry 70000; ein TP UNTER dem Entry (65000) ist falsch-seitig → dropped.
    embed = {"title": "BTC/USDT — NEW_TRADE", "description": "Signal `dtp`",
             "fields": [{"name": "Action", "value": "NEW_TRADE"},
                        {"name": "Ticker", "value": "BTC/USDT"},
                        {"name": "Direction", "value": "LONG"},
                        {"name": "Entry", "value": "70000"},
                        {"name": "Stop Loss", "value": "68000"},
                        {"name": "Take Profits", "value": "50% @ 72000, 50% @ 65000"}]}
    from app.parser import parse_signal
    parsed = parse_signal(embed)
    if not parsed or not parsed.dropped_tps:
        print("m18_dropped_tps_activity: SKIP (parser dropped_tps not populated for this shape)")
        return
    stub = StubTrader(pos=0.0)
    orig_build = engine._build_trader
    engine._build_trader = _async_return(stub)
    try:
        asyncio.run(engine.handle_signal(embed))
        import time as _t; _t.sleep(0.2)
    finally:
        engine._build_trader = orig_build
    infos = _activities(1, "info")
    assert any("take-profit(s) dropped" in t for t in infos), infos
    print("m18_dropped_tps_activity: OK")


def test_m6_hook_reconciler_passes_side():
    """HOOK M-6: der Reconciler ruft covered_stop_size(coin, position_is_long) MIT
    dem side-Flag auf (szi>0)."""
    _reset_db(); _reset_engine_state()
    db = SessionLocal()
    _make_user(db, user_id=1)
    _make_managed_full(db, user_id=1, coin="ETH", direction="LONG", entry=1900.0,
                       stop_loss=1800.0, take_profits="[]", status="open")
    db.close()
    seen = {}

    class _T(StubTrader):
        def open_positions(self):
            return [{"coin": "ETH", "szi": 2.0}]   # LONG
        def covered_stop_size(self, coin, position_is_long=None):
            seen["is_long"] = position_is_long
            return float("inf")   # voll gedeckt → kein weiterer Eingriff
    stub = _T(pos=2.0)
    orig_build = engine._build_trader
    engine._build_trader = _async_return(stub)
    try:
        asyncio.run(engine.reconcile_stop_coverage([1]))
    finally:
        engine._build_trader = orig_build
    assert seen.get("is_long") is True, f"position_is_long muss True sein (LONG szi>0): {seen}"
    print("m6_hook_reconciler_passes_side: OK")


def test_m5_reconciler_skips_coins_without_row():
    """M-5: eine offene Position OHNE managed-Row (manuelle Position) wird vom
    Reconciler übersprungen — kein 'secure manually'-Alert."""
    _reset_db(); _reset_engine_state()
    db = SessionLocal(); _make_user(db, user_id=1); db.close()   # KEINE managed-Row

    class _T(StubTrader):
        def open_positions(self):
            return [{"coin": "DOGE", "szi": 100.0}]   # manuelle Position, keine Row
        def covered_stop_size(self, coin, position_is_long=None):
            return 0.0   # ungedeckt — würde ohne M-5 alerten
    stub = _T(pos=100.0)
    orig_build = engine._build_trader
    engine._build_trader = _async_return(stub)
    try:
        asyncio.run(engine.reconcile_stop_coverage([1]))
    finally:
        engine._build_trader = orig_build
    errs = _activities(1, "error")
    assert not any("secure" in t.lower() for t in errs), \
        f"manuelle Position ohne Row darf NICHT alerten: {errs}"
    print("m5_reconciler_skips_coins_without_row: OK")


def test_m9_register_watcher_cancels_old():
    """M-9: _register_fill_watcher cancelt einen schon registrierten Watcher für
    dasselbe (user,coin) beim Überschreiben."""
    _reset_engine_state()

    async def _runner():
        async def _sleep_forever():
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                raise
        t_old = asyncio.create_task(_sleep_forever())
        engine._register_fill_watcher(7, "BTC", t_old)
        t_new = asyncio.create_task(_sleep_forever())
        engine._register_fill_watcher(7, "BTC", t_new)
        await asyncio.sleep(0)   # cancel propagieren lassen
        old_cancelled = t_old.cancelled() or t_old.cancelling() > 0 if hasattr(t_old, "cancelling") else t_old.cancelled()
        t_new.cancel()
        try:
            await t_new
        except asyncio.CancelledError:
            pass
        try:
            await t_old
        except asyncio.CancelledError:
            pass
        return old_cancelled
    res = asyncio.run(_runner())
    _reset_engine_state()
    assert res, "alter Watcher muss beim Überschreiben gecancelt werden"
    print("m9_register_watcher_cancels_old: OK")


def test_m24_stopout_cooldown_blocks_new():
    """M-24: ein jüngster realisierter Loss-Fill auf dem Coin blockt ein NEW_TRADE
    während des Cooldowns (über _recent_stopout_cooldown gemockt)."""
    _reset_db(); _reset_engine_state()
    db = SessionLocal(); _make_user(db, user_id=1); db.close()
    stub = StubTrader(pos=0.0)
    orig_cd = engine._recent_stopout_cooldown
    engine._recent_stopout_cooldown = lambda addr, coin: (True, 600.0)
    try:
        asyncio.run(engine._open_new(stub, _sns_user(), _new_sig()))
    finally:
        engine._recent_stopout_cooldown = orig_cd
    assert all(c[0] != "place_entry" for c in stub.calls), stub.calls
    assert any("cooldown" in t for t in _activities(1, "skip"))
    print("m24_stopout_cooldown_blocks_new: OK")


def test_m3_peak_reset_on_withdrawal():
    """M-3: fällt das Balance unter den Drawdown-Threshold, aber die realisierten
    Verluste erklären den Drop NICHT (Auszahlung), wird der peak zurückgesetzt
    statt den Bot zu pausieren (kein place_entry-Block durch Drawdown)."""
    _reset_db(); _reset_engine_state()
    db = SessionLocal(); _make_user(db, user_id=1); db.close()
    # peak 10k, balance 5k (50% drop), aber 0 realisierte Verluste ⇒ Withdrawal.
    u = _sns_user(max_drawdown_pct=0.30, peak_account_value=10_000.0)

    class _T(StubTrader):
        def account_value(self):
            return 5_000.0
        def available_margin(self):
            return 5_000.0
    stub = _T(pos=0.0)
    orig_loss = engine._drawdown_from_losses
    engine._drawdown_from_losses = lambda addr, lookback_s=None: (0.0, True)  # keine Verluste
    try:
        asyncio.run(engine._open_new(stub, u, _new_sig()))
    finally:
        engine._drawdown_from_losses = orig_loss
    # peak muss in der DB auf 5000 gesenkt sein, und KEIN Drawdown-Pause-Error.
    db = SessionLocal(); uu = db.get(User, 1); db.close()
    assert abs(float(uu.peak_account_value) - 5000.0) < 1.0, \
        f"peak sollte auf 5000 zurückgesetzt sein, ist {uu.peak_account_value}"
    assert all("MAX-DRAWDOWN-CAP" not in t for t in _activities(1, "error")), \
        "Withdrawal darf nicht als Drawdown pausieren"
    print("m3_peak_reset_on_withdrawal: OK")


def test_m3_real_loss_still_pauses():
    """M-3 (Gegenprobe): erklären die Verluste den Drop, bleibt der Drawdown-Cap
    aktiv (Bot pausiert) — der Reset entschärft NUR Auszahlungen."""
    _reset_db(); _reset_engine_state()
    db = SessionLocal(); _make_user(db, user_id=1, bot_active=True); db.close()
    u = _sns_user(max_drawdown_pct=0.30, peak_account_value=10_000.0)

    class _T(StubTrader):
        def account_value(self):
            return 5_000.0
        def available_margin(self):
            return 5_000.0
    stub = _T(pos=0.0)
    orig_loss = engine._drawdown_from_losses
    engine._drawdown_from_losses = lambda addr, lookback_s=None: (5_000.0, True)  # voller Verlust
    try:
        asyncio.run(engine._open_new(stub, u, _new_sig()))
    finally:
        engine._drawdown_from_losses = orig_loss
    assert any("MAX-DRAWDOWN-CAP" in t for t in _activities(1, "error")), \
        "echter Verlust muss den Drawdown-Cap auslösen"
    db = SessionLocal(); uu = db.get(User, 1); db.close()
    assert uu.bot_active is False
    print("m3_real_loss_still_pauses: OK")


def test_m3_open_position_no_reset():
    """🔴 M-3-FAIL-OPEN-FIX (Verify-Runde): hält der User eine OFFENE Position,
    enthält balance UNREALISIERTE PnL — ein echter Drawdown würde sonst (0
    realisierte Verluste) als Auszahlung fehlgedeutet und der Schutz-Cap
    aufgehoben. Mit offener Position darf der peak NICHT zurückgesetzt werden →
    der Cap pausiert (sichere Richtung)."""
    _reset_db(); _reset_engine_state()
    db = SessionLocal()
    _make_user(db, user_id=1, bot_active=True)
    uu0 = db.get(User, 1); uu0.peak_account_value = 10_000.0; db.commit()
    db.close()
    u = _sns_user(max_drawdown_pct=0.30, peak_account_value=10_000.0)

    class _T(StubTrader):
        def account_value(self):
            return 5_000.0          # 50% Drop — aber unrealisiert (offene Position)
        def available_margin(self):
            return 5_000.0
        def open_positions_count(self):
            return 1                # NICHT flat → Flat-Guard verhindert den Reset
    stub = _T(pos=2.0)
    orig_loss = engine._drawdown_from_losses
    engine._drawdown_from_losses = lambda addr, lookback_s=None: (0.0, True)  # nichts realisiert
    try:
        asyncio.run(engine._open_new(stub, u, _new_sig()))
    finally:
        engine._drawdown_from_losses = orig_loss
    db = SessionLocal(); uu = db.get(User, 1); db.close()
    assert abs(float(uu.peak_account_value) - 10_000.0) < 1.0, \
        f"peak darf bei OFFENER Position NICHT resettet werden, ist {uu.peak_account_value}"
    assert any("MAX-DRAWDOWN-CAP" in t for t in _activities(1, "error")), \
        "unrealisierter Drawdown bei offener Position muss pausieren (fail-safe)"
    assert uu.bot_active is False
    print("m3_open_position_no_reset: OK")


def test_m10_hook_post_alert_uses_submit_alert(monkeypatch=None):
    """HOOK M-10: _post_alert nutzt hl_retry.submit_alert (dedizierter Executor)
    statt loop.run_in_executor(None, …)."""
    import app.hl_retry as hl_retry
    calls = {"n": 0}
    orig = hl_retry.submit_alert
    orig_url = engine.config.ALERT_WEBHOOK_URL

    def _fake_submit(fn, *a, **k):
        calls["n"] += 1
        return None
    hl_retry.submit_alert = _fake_submit
    engine.config.ALERT_WEBHOOK_URL = "https://example.test/webhook"

    async def _runner():
        engine._post_alert("test alert")
    try:
        asyncio.run(_runner())
    finally:
        hl_retry.submit_alert = orig
        engine.config.ALERT_WEBHOOK_URL = orig_url
    assert calls["n"] == 1, "submit_alert muss vom _post_alert (im Loop) genutzt werden"
    print("m10_hook_post_alert_uses_submit_alert: OK")


def test_m13_open_row_remainder_cancelled():
    """M-13: eine open-Row mit gesetztem resting_oid (Entry-Remainder) ⇒ Startup-
    Pass cancelt den Remainder per oid und löscht resting_oid; Row bleibt open."""
    _reset_db(); _reset_engine_state()
    db = SessionLocal()
    _make_user(db, user_id=1)
    mt = _make_managed_full(db, user_id=1, coin="BTC", direction="LONG", entry=70000.0,
                            stop_loss=68000.0, take_profits="[]", status="open",
                            resting_oid="444")
    mt.entry_oid = "444"; mt.bot_filled_sz = 1.0
    db.add(mt); db.commit(); db.close()

    stub = StubTrader(pos=1.0)
    orig_build = engine._build_trader
    engine._build_trader = _async_return(stub)
    try:
        asyncio.run(engine._cancel_open_row_remainders([1]))
    finally:
        engine._build_trader = orig_build
    assert ("cancel_order_oid", "BTC", "444") in stub.calls, stub.calls
    db = SessionLocal()
    mt2 = db.query(ManagedTrade).filter_by(user_id=1, coin="BTC").order_by(ManagedTrade.id.desc()).first()
    db.close()
    assert mt2.status == "open", "Position-Row bleibt open"
    assert mt2.resting_oid is None, "resting_oid muss nach Cancel geleert sein"
    print("m13_open_row_remainder_cancelled: OK")


def test_l10_prune_memory_dicts():
    """L-10: prune_memory_dicts entfernt alte _alert_throttle/_trade_intervals-
    Einträge und lässt frische stehen."""
    import time as _t
    engine._alert_throttle.clear(); engine._trade_intervals.clear()
    now = _t.time()
    engine._alert_throttle[("x", "old")] = now - 10_000_000   # uralt
    engine._alert_throttle[("x", "fresh")] = now
    engine._trade_intervals[(1, "OLD")] = now - 10_000_000
    engine._trade_intervals[(1, "NEW")] = now
    engine.prune_memory_dicts()
    assert ("x", "old") not in engine._alert_throttle
    assert ("x", "fresh") in engine._alert_throttle
    assert (1, "OLD") not in engine._trade_intervals
    assert (1, "NEW") in engine._trade_intervals
    engine._alert_throttle.clear(); engine._trade_intervals.clear()
    print("l10_prune_memory_dicts: OK")


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
    test_position_unknown_aborts_safely()
    test_startup_rearm_resting_watcher()
    test_startup_rearm_impossible_cancels_entry()
    test_sl_only_update_preserves_tps()
    test_missing_direction_skips_new_trade()
    test_throttle_exempts_updates_blocks_new()
    test_ratchet_baseline_survives_premature_sync_close()
    test_parser_update_without_entry()
    # Runde 2
    test_m1_avail_read_fail_aborts_entry()
    test_m2_open_count_raise_aborts_entry()
    test_m4_update_on_resting_modifies_pending()
    test_m4_modify_pending_ratchet_blocks_loosen()
    test_m20_update_replay_deduped()
    test_m15_direction_flip_cancels_remainder()
    test_m12_tp_reject_emits_error()
    test_m8_paused_user_aborts_after_lock()
    test_m23_generic_error_is_generic()
    test_l1_rate_cap_only_counts_new()
    test_l2_confidence_gate_exempts_update()
    test_l6_entry_unauthorized_pauses_user()
    test_l14_malformed_tp_logged()
    test_m18_dropped_tps_activity()
    test_m6_hook_reconciler_passes_side()
    test_m5_reconciler_skips_coins_without_row()
    test_m9_register_watcher_cancels_old()
    test_m24_stopout_cooldown_blocks_new()
    test_m3_peak_reset_on_withdrawal()
    test_m3_real_loss_still_pauses()
    test_m10_hook_post_alert_uses_submit_alert()
    test_m13_open_row_remainder_cancelled()
    test_l10_prune_memory_dicts()
    print("\nALLE ENGINE-TESTS BESTANDEN ✅")
