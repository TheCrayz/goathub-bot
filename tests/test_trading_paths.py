"""Trading-Pfad-Tests mit Fake-Trader — Audit 2026-06-12 (H-A/H-B/M-1/M-2/M-5).

Deckt die kritischen Geld-Pfade der Engine OHNE Netzwerk ab:

  (1) _open_new Happy-Path: Entry + Protection platziert, managed_trade gespeichert,
      Throttle-Stempel (M-5) erst NACH erfolgreicher Platzierung.
  (2) H-A: SL-Platzierung scheitert UND Emergency-Close scheitert → KEIN
      "geschlossen"-Claim, Row bleibt "open", UNGETHROTTLEDER Alert feuert.
  (3) M-1: set_leverage-Fail → Entry wird übersprungen (kein Trade mit
      unbestätigtem Hebel), Throttle-Fenster NICHT verbraucht.
  (4) H-B: _protect_when_filled bricht via Generation-Check (signal_id) ab,
      wenn ein neueres Signal die managed_trade-Row übernommen hat.
  (5) M-2: Schutz-Orders (SL + TP) tragen eine Cloid (Retry-Idempotenz).
  (6) H-3: _round_px erfüllt BEIDE HL-Preisregeln (5 sig figs UND
      max 6−szDecimals Dezimalstellen) für low-price-Coins wie DOGE.

Run:
    PYTHONPATH=. venv/bin/python -m pytest tests/test_trading_paths.py -q
    PYTHONPATH=. python3 tests/test_trading_paths.py
"""
import os
# Test-only env (wie tests/test_engine.py) — Key wird zur Laufzeit generiert,
# NIE hartcodiert (der alte hartcodierte Key lag im öffentlichen Repo).
from cryptography.fernet import Fernet
os.environ.setdefault("JWT_SECRET", "test-only-secret-not-prod-1234567890abcdef")
os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from sqlalchemy.pool import StaticPool
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# DB-Setup: läuft diese Datei zusammen mit tests/test_engine.py, hat die schon
# eine StaticPool-:memory:-Engine in app.db verdrahtet — die MÜSSEN wir
# wiederverwenden (app.engine hat SessionLocal beim Import gebunden; eine
# zweite Engine würde test_engine's Fixtures unsichtbar machen). Standalone
# bauen wir sie selbst auf.
import app.db as _dbmod
if _dbmod.engine.pool.__class__.__name__ != "StaticPool":
    _test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    _dbmod.engine = _test_engine
    _dbmod.SessionLocal = sessionmaker(bind=_test_engine, autoflush=False,
                                       autocommit=False, future=True)

from app.models import Activity, ManagedTrade, User
from app.models import ProcessedSignal
from app.db import Base, SessionLocal
Base.metadata.create_all(_dbmod.engine)

# Engine importieren NACH dem DB-Setup
import asyncio
import types

import app.engine as engine
from app.parser import Signal, TakeProfit


# ── Fake-Trader (Recorder, kein Netzwerk) ────────────────────────────────────
class FakeTrader:
    """Zeichnet alle mutierenden HL-Calls auf; Verhalten per Konstruktor steuerbar."""

    def __init__(self, *, pos=0.0, entry_filled=True, resting_oid="777",
                 sl_ok=True, close_ok=True, lev_ok=True, balance=10_000.0):
        self.pos = pos
        self.entry_filled = entry_filled
        self.resting_oid = resting_oid
        self.sl_ok = sl_ok
        self.close_ok = close_ok
        self.lev_ok = lev_ok
        self.balance = balance
        self.calls = []               # [(name, args...)] mutierende Calls
        self.protection_calls = []    # place_protection-Argumente

    # Reads (nicht aufgezeichnet)
    def is_tradable(self, coin):
        return True

    def position_size(self, coin):
        return self.pos

    def max_leverage(self, coin):
        return 50

    def account_value(self):
        return self.balance

    def available_margin(self):
        return self.balance

    def open_positions_count(self):
        return 0

    # Mutationen (aufgezeichnet)
    def set_leverage(self, coin, lev):
        self.calls.append(("set_leverage", coin, lev))
        if self.lev_ok:
            return {"status": "ok"}
        return {"status": "err", "response": "update_leverage final fail (Stub)"}

    def place_entry(self, coin, is_buy, sz, px):
        self.calls.append(("place_entry", coin, is_buy, sz, px))
        if self.entry_filled:
            return {"ok": True, "filled": True, "filled_sz": sz,
                    "resting_oid": None, "error": None}
        return {"ok": True, "filled": False, "filled_sz": 0.0,
                "resting_oid": self.resting_oid, "error": None}

    def place_protection(self, coin, is_buy, sz, sl, tps):
        self.protection_calls.append(
            {"coin": coin, "is_buy": is_buy, "sz": sz, "sl": sl, "tps": list(tps or [])})
        if self.sl_ok:
            return {"sl_ok": True, "sl": {"status": "ok"}, "tp": [], "skip_reason": None}
        return {"sl_ok": False, "sl": {"status": "err", "response": "tick size (Stub)"},
                "tp": [], "skip_reason": None}

    def close_position(self, coin):
        self.calls.append(("close_position", coin))
        if self.close_ok:
            return {"ok": True, "closed": abs(self.pos)}
        return {"ok": False, "closed": 0.0, "error": "market_close final fail (Stub)"}

    def cancel_orders(self, coin):
        self.calls.append(("cancel_orders", coin))
        return 1

    def cancel_order(self, coin, oid):
        self.calls.append(("cancel_order", coin, oid))
        return True


# ── Helpers ─────────────────────────────────────────────────────────────────
def _fake_user(user_id=1):
    """Leichtgewichtiger User-Ersatz für _open_new (kein ORM-Detach-Theater)."""
    return types.SimpleNamespace(
        id=user_id, hl_account_address="0xABC", risk_pct=0.02, leverage=10,
        max_open_positions=10, capital_cap_usdc=0,
        max_drawdown_pct=0, peak_account_value=0, builder_approved=False)


def _sig(action="NEW_TRADE", signal_id="sigX", coin="ETH", direction="LONG",
         entry=2000.0, stop_loss=1900.0, tps=()):
    return Signal(signal_id=signal_id, ticker=f"{coin}/USDT", action=action,
                  direction=direction, entry=entry, stop_loss=stop_loss,
                  take_profits=[TakeProfit(percent=pct, price=px) for px, pct in tps])


def _reset_all():
    """DB + Engine-In-Memory-State pro Test sauber (Locks sind loop-gebunden)."""
    db = SessionLocal()
    try:
        db.query(Activity).delete()
        db.query(ManagedTrade).delete()
        db.query(ProcessedSignal).delete()
        db.query(User).delete()
        db.commit()
    finally:
        db.close()
    engine._locks.clear()
    engine._user_locks.clear()
    engine._fill_watchers.clear()
    engine._trade_intervals.clear()
    engine._recent_pause_keys.clear()
    engine._percoin_cache.clear()
    engine.config.ALERT_WEBHOOK_URL = ""


def _activities(user_id, kind=None):
    db = SessionLocal()
    try:
        q = db.query(Activity).filter(Activity.user_id == user_id)
        if kind:
            q = q.filter(Activity.kind == kind)
        return [a.text for a in q.order_by(Activity.id).all()]
    finally:
        db.close()


def _managed(user_id, coin):
    db = SessionLocal()
    try:
        return (db.query(ManagedTrade)
                .filter(ManagedTrade.user_id == user_id, ManagedTrade.coin == coin)
                .order_by(ManagedTrade.id.desc()).first())
    finally:
        db.close()


def _signal_done(user_id, signal_id):
    db = SessionLocal()
    try:
        return (db.query(ProcessedSignal)
                .filter(ProcessedSignal.user_id == user_id,
                        ProcessedSignal.signal_id == signal_id).first() is not None)
    finally:
        db.close()


class _NoCoinFilter:
    """Context-Manager: _per_coin_stats stubben (kein HL-Call, kein Block)."""
    def __enter__(self):
        self._orig = engine._per_coin_stats
        engine._per_coin_stats = lambda addr, coin: None
        return self

    def __exit__(self, *exc):
        engine._per_coin_stats = self._orig
        return False


class _AlertRecorder:
    """Context-Manager: _post_alert aufzeichnen statt HTTP."""
    def __init__(self):
        self.calls = []   # [(text, key)]

    def __enter__(self):
        self._orig = engine._post_alert

        def _rec(text, key=None):
            self.calls.append((text, key))
        engine._post_alert = _rec
        return self

    def __exit__(self, *exc):
        engine._post_alert = self._orig
        return False


# ── (1) Happy-Path ───────────────────────────────────────────────────────────
def test_open_new_happy_path():
    """Entry (filled) + Protection platziert, managed_trade='open' gespeichert,
    signal_id dedup-markiert, Throttle-Stempel (M-5) gesetzt."""
    _reset_all()
    trader = FakeTrader(entry_filled=True)
    sig = _sig(signal_id="hp-1", tps=((2100.0, 50.0), (2200.0, 50.0)))
    with _NoCoinFilter():
        asyncio.run(engine._open_new(trader, _fake_user(1), sig))

    entry_calls = [c for c in trader.calls if c[0] == "place_entry"]
    assert len(entry_calls) == 1, trader.calls
    _, coin, is_buy, sz, px = entry_calls[0]
    assert coin == "ETH" and is_buy is True and px == 2000.0
    # risk 2% × $10k = $200 Risiko / $100 SL-Distanz = 2.0 qty
    assert abs(sz - 2.0) < 1e-9, f"qty {sz} erwartet 2.0"

    assert len(trader.protection_calls) == 1, trader.protection_calls
    pc = trader.protection_calls[0]
    assert pc["sl"] == 1900.0 and pc["sz"] == 2.0
    assert pc["tps"] == [(2100.0, 0.5), (2200.0, 0.5)], pc["tps"]

    mt = _managed(1, "ETH")
    assert mt is not None and mt.status == "open", "managed_trade muss 'open' gespeichert sein"
    assert float(mt.stop_loss) == 1900.0
    assert _signal_done(1, "hp-1"), "signal_id muss dedup-markiert sein (Entry platziert)"
    # Audit M-5: Throttle-Fenster startet erst nach erfolgreicher Platzierung
    assert (1, "ETH") in engine._trade_intervals, "Throttle-Stempel fehlt nach Entry"
    assert any("opened" in t for t in _activities(1, "order")), _activities(1)
    print("open_new_happy_path: OK")


# ── (2) H-A: SL-Fail + Emergency-Close-Fail ─────────────────────────────────
def test_sl_fail_and_emergency_close_fail():
    """H-A: scheitern SL UND Market-Close, darf NIRGENDS 'geschlossen' stehen.
    Row bleibt 'open' (sync beobachtet weiter), ungethrottleder Alert feuert."""
    _reset_all()
    trader = FakeTrader(entry_filled=True, sl_ok=False, close_ok=False)
    sig = _sig(signal_id="ha-1", coin="BTC", entry=70000.0, stop_loss=68000.0)
    with _NoCoinFilter(), _AlertRecorder() as alerts:
        asyncio.run(engine._open_new(trader, _fake_user(1), sig))

    assert ("close_position", "BTC") in trader.calls, "Emergency-Close muss versucht werden"
    mt = _managed(1, "BTC")
    assert mt is not None and mt.status == "open", \
        f"Row MUSS offen bleiben (Position lebt!), ist {mt and mt.status}"

    all_acts = _activities(1)
    assert not any("position closed" in t for t in all_acts), \
        f"darf NIE 'closed' behaupten wenn close fail: {all_acts}"
    errs = _activities(1, "error")
    assert any("EMERGENCY" in t and "unprotected" in t for t in errs), errs

    # Audit H-A: mindestens ein UNGETHROTTLEDER Alert (key=None)
    assert any(key is None and "EMERGENCY" in text for text, key in alerts.calls), \
        f"ungethrottleder EMERGENCY-Alert fehlt: {alerts.calls}"
    print("sl_fail_and_emergency_close_fail: OK")


# ── (3) M-1: set_leverage-Fail ───────────────────────────────────────────────
def test_set_leverage_failure_skips_entry():
    """M-1: set_leverage liefert status='err' → KEIN place_entry, error-Activity,
    Throttle-Fenster + Dedup-Marke bleiben unverbraucht (Re-Emit handelbar)."""
    _reset_all()
    trader = FakeTrader(lev_ok=False)
    sig = _sig(signal_id="m1-1")
    with _NoCoinFilter():
        asyncio.run(engine._open_new(trader, _fake_user(1), sig))

    assert all(c[0] != "place_entry" for c in trader.calls), \
        f"kein Entry mit unbestätigtem Hebel: {trader.calls}"
    assert trader.protection_calls == []
    errs = _activities(1, "error")
    assert any("set_leverage" in t for t in errs), errs
    assert (1, "ETH") not in engine._trade_intervals, \
        "geskippter Entry darf das Throttle-Fenster nicht verbrauchen (M-5)"
    assert not _signal_done(1, "m1-1"), "geskipptes Signal bleibt retry-bar"
    print("set_leverage_failure_skips_entry: OK")


# ── (4) H-B: Watcher-Generation-Check ────────────────────────────────────────
def test_watcher_aborts_on_generation_change():
    """H-B: hat ein NEUERES Signal die managed_trade-Row übernommen (anderes
    signal_id), bricht der Watcher ab OHNE Protection/cancel_orders/close —
    er darf nur noch seine EIGENE Rest-Entry-Order (oid) wegräumen."""
    _reset_all()
    db = SessionLocal()
    db.add(ManagedTrade(user_id=1, coin="ETH", direction="LONG", entry=2000.0,
                        stop_loss=1900.0, take_profits="[]", status="resting",
                        resting_oid="999", signal_id="sig-NEW"))
    db.commit()
    db.close()

    trader = FakeTrader(pos=0.4)   # Position existiert (die des NEUEN Signals!)
    old_sig = _sig(signal_id="sig-OLD")
    orig_poll = engine.config.ENTRY_POLL_S
    orig_timeout = engine.config.ENTRY_FILL_TIMEOUT_S
    engine.config.ENTRY_POLL_S = 0
    engine.config.ENTRY_FILL_TIMEOUT_S = 2
    try:
        asyncio.run(engine._protect_when_filled(trader, 1, old_sig, True, [],
                                                resting_oid="111"))
    finally:
        engine.config.ENTRY_POLL_S = orig_poll
        engine.config.ENTRY_FILL_TIMEOUT_S = orig_timeout

    assert trader.protection_calls == [], \
        f"stale Watcher darf KEINE (alten) SL/TP gegen die neue Position setzen: {trader.protection_calls}"
    assert ("cancel_orders", "ETH") not in trader.calls, \
        "stale Watcher darf nicht pauschal canceln (würde Schutz des neuen Trades zerstören)"
    assert ("close_position", "ETH") not in trader.calls
    # Erlaubt ist NUR das Wegräumen der eigenen Rest-Entry-Order:
    assert trader.calls in ([("cancel_order", "ETH", "111")], []), trader.calls

    mt = _managed(1, "ETH")
    assert mt.status == "resting" and mt.signal_id == "sig-NEW", \
        "Row des neuen Signals muss unangetastet bleiben"
    print("watcher_aborts_on_generation_change: OK")


# ── (5) M-2: Cloid auf Schutz-Orders ─────────────────────────────────────────
def test_protection_orders_carry_cloid():
    """M-2: SL- und TP-Order laufen mit eigener, eindeutiger Cloid durch _order
    (Retry nach verlorener Response wird idempotent statt Doppel-Trigger)."""
    from app.hyperliquid_exec import HyperliquidTrader

    t = HyperliquidTrader.__new__(HyperliquidTrader)   # __init__ umgehen (kein Netz)
    t._sz = {"ETH": 4}
    t.builder = None
    t.info = types.SimpleNamespace(all_mids=lambda: {})   # mark=0 → kein Preflight-Skip

    orders = []

    def _rec_order(coin, is_buy, sz, px, otype, reduce_only=False, cloid=None):
        orders.append({"coin": coin, "otype": otype, "reduce_only": reduce_only,
                       "cloid": cloid})
        return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 1}}]}}}

    t._order = _rec_order   # Instanz-Attribut shadowt die Methode

    res = t.place_protection("ETH", True, 2.0, 1900.0,
                             [(2100.0, 0.5), (2200.0, 0.5)])
    assert res["sl_ok"] is True
    assert len(orders) == 3, f"1×SL + 2×TP erwartet, hab {len(orders)}"
    cloids = [o["cloid"] for o in orders]
    assert all(c is not None for c in cloids), f"jede Schutz-Order braucht eine Cloid: {orders}"
    assert len(set(str(c) for c in cloids)) == 3, f"Cloids müssen eindeutig sein: {cloids}"
    assert all(o["reduce_only"] for o in orders)
    print("protection_orders_carry_cloid: OK")


# ── (6) H-3: _round_px beide HL-Regeln ───────────────────────────────────────
def test_round_px_hl_rules():
    """HL verlangt max 5 signifikante Stellen UND max (6 − szDecimals)
    Dezimalstellen. Low-price-Coin (DOGE, szDecimals=3) → max 3 Dezimalstellen."""
    from decimal import Decimal
    from app.hyperliquid_exec import HyperliquidTrader

    t = HyperliquidTrader.__new__(HyperliquidTrader)
    t._sz = {"DOGE": 3, "BTC": 5}

    def _decimals(x):
        d = Decimal(str(x)).normalize().as_tuple().exponent
        return max(0, -d)

    def _sigfigs(x):
        return len(Decimal(str(x)).normalize().as_tuple().digits)

    # DOGE (szDec=3 → max 3 Dezimalstellen): 5-sig-fig-Rundung allein ergäbe
    # 0.12346 (5 Dezimalstellen) → HL-Reject. Beide Regeln zusammen: 0.123.
    assert t._round_px("DOGE", 0.123456789) == 0.123
    # Preis nahe 1: 5 sig figs → 1.2346, Dezimal-Regel → 1.235
    assert t._round_px("DOGE", 1.2345678) == 1.235
    # Großer Preis: sig-fig-Regel greift, Dezimal-Regel trivial erfüllt
    assert t._round_px("DOGE", 98765.4321) == 98765.0
    # Property-Check über einen Preisbereich: IMMER beide Regeln erfüllt
    for raw in (0.00012345678, 0.0123456, 0.999999, 3.1415926, 31.49999,
                123.456789, 4321.9876, 99999.99):
        out = t._round_px("DOGE", raw)
        assert _decimals(out) <= 3, f"{raw} → {out}: >3 Dezimalstellen (szDec=3)"
        assert _sigfigs(out) <= 5, f"{raw} → {out}: >5 sig figs"
    # BTC (szDec=5 → max 1 Dezimalstelle)
    assert t._round_px("BTC", 69123.456) == 69123.0
    assert _decimals(t._round_px("BTC", 0.123456)) <= 1
    # Edge: 0/negativ unverändert (defensiv)
    assert t._round_px("DOGE", 0) == 0
    print("round_px_hl_rules: OK")


# ── (7) Referral-Methoden (HyperliquidTrader.referral_state / set_referrer) ──
def test_referral_state_parses_referred_by():
    """referral_state extrahiert code + addr aus HLs `referredBy`-dict; ein
    fehlendes/None-Feld → (None, None); jede Exception → error-dict (nie raise)."""
    from app.hyperliquid_exec import HyperliquidTrader

    t = HyperliquidTrader.__new__(HyperliquidTrader)   # __init__ umgehen (kein Netz)
    t.address = "0xMASTER"

    # Fall 1: Referrer gesetzt → code + addr werden geparst
    t.info = types.SimpleNamespace(
        query_referral_state=lambda addr: {"referredBy": {"referrer": "0xREF", "code": "GOAT"}})
    r = t.referral_state()
    assert r == {"referred_by_code": "GOAT", "referrer_addr": "0xREF",
                 "raw": {"referredBy": {"referrer": "0xREF", "code": "GOAT"}}}, r

    # Fall 2: kein Referrer (referredBy=None) → beide None, kein error
    t.info = types.SimpleNamespace(query_referral_state=lambda addr: {"referredBy": None})
    r = t.referral_state()
    assert r["referred_by_code"] is None and r["referrer_addr"] is None
    assert "error" not in r, r

    # Fall 3: Exception im Read → fail-safe error-dict, KEIN raise
    def _boom(addr):
        raise RuntimeError("HL down")
    t.info = types.SimpleNamespace(query_referral_state=_boom)
    r = t.referral_state()
    assert r == {"referred_by_code": None, "referrer_addr": None, "error": "HL down"}, r
    print("referral_state_parses_referred_by: OK")


def test_set_referrer_ok_and_failsafe():
    """set_referrer: status='ok' → ok=True; Reject (status='err') → ok=False
    (kein raise); Exception → {"ok": False, "error": …} (HL lehnt z.B. bei
    bereits gesetztem Referrer / Self-Referral ab — darf nie crashen)."""
    from app.hyperliquid_exec import HyperliquidTrader

    t = HyperliquidTrader.__new__(HyperliquidTrader)
    t.address = "0xMASTER"

    # Fall 1: Erfolg → {"status": "ok", "response": {"type": "default"}}
    t.exchange = types.SimpleNamespace(
        set_referrer=lambda code: {"status": "ok", "response": {"type": "default"}})
    res = t.set_referrer("GOAT")
    assert res["ok"] is True and res["raw"]["status"] == "ok", res

    # Fall 2: HL-Reject (z.B. schon ein Referrer gesetzt) → ok=False, kein raise
    t.exchange = types.SimpleNamespace(
        set_referrer=lambda code: {"status": "err", "response": "Referrer already set"})
    res = t.set_referrer("GOAT")
    assert res["ok"] is False and res["raw"]["status"] == "err", res

    # Fall 3: Exception (z.B. Agent-Key darf nicht signieren) → error-dict, KEIN raise
    def _boom(code):
        raise RuntimeError("must be master account")
    t.exchange = types.SimpleNamespace(set_referrer=_boom)
    res = t.set_referrer("GOAT")
    assert res == {"ok": False, "error": "must be master account"}, res
    print("set_referrer_ok_and_failsafe: OK")


if __name__ == "__main__":
    test_open_new_happy_path()
    test_sl_fail_and_emergency_close_fail()
    test_set_leverage_failure_skips_entry()
    test_watcher_aborts_on_generation_change()
    test_protection_orders_carry_cloid()
    test_round_px_hl_rules()
    test_referral_state_parses_referred_by()
    test_set_referrer_ok_and_failsafe()
    print("\nALLE TRADING-PATH-TESTS BESTANDEN ✅")
