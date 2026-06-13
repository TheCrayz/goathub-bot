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

    def covered_stop_size(self, coin):
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
    print("\nALLE ENGINE-TESTS BESTANDEN ✅")
