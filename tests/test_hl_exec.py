"""Tests für den Exchange-Wrapper (app/hyperliquid_exec.py) + Retry-Breaker
(app/hl_retry.py) — Stresstest-Audit 2026-06-13.

KEIN Netz: HyperliquidTrader wird via __new__ gebaut (umgeht den Netzwerk-
__init__ mit meta()-Call) und mit Fake-SDK-Objekten bestückt. Jeder Test treibt
genau ein Audit-Szenario:
  C-1  close_position: statuses-error → ok=False+still_open; None → ok=True;
       Erfolg → ok=True+closed; Partial-IoC → ok=False+still_open.
  C-2  place_entry/place_protection: duplicate-cloid reject → Erfolg.
  H-6  cancel_orders/cancel_order_oid: "already filled" → kein Cancel/ already_filled.
  order_status: Parse filled/open/partial/canceled/unknown.
  _round_sz: ABRUNDEN (floor) statt round-half-even (DOGE szDecimals=0).

Lauf:  cd /Users/michael/goathub-bot && PYTHONPATH=. venv/bin/python -m pytest tests/test_hl_exec.py -q
"""
import os
import sys

import pytest

# Standalone-Fallback (python tests/test_hl_exec.py) — im pytest-Lauf erledigt das conftest.
os.environ.setdefault("JWT_SECRET", "test-only-secret-not-prod-1234567890abcdef")
try:
    from cryptography.fernet import Fernet
    os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
except Exception:
    pass
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ENABLE_LISTENER", "false")

import app.hl_retry as hl_retry
from app.hyperliquid_exec import HyperliquidTrader


# ---------------------------------------------------------------------------
# Fake-SDK-Bausteine
# ---------------------------------------------------------------------------
def _ok(statuses):
    """HL-Order/Cancel-Antwort mit gegebenem statuses[]."""
    return {"status": "ok", "response": {"data": {"statuses": statuses}}}


class FakeInfo:
    """Minimal-Fake für die info-Calls, die der Wrapper nutzt."""
    def __init__(self, position_seq=None, order_status_resp=None, mids=None):
        # position_seq: Liste signierter szi, die position_size nacheinander liefert
        # (für die close_position-Re-Read-Bestätigung). Letzter Wert bleibt sticky.
        self._position_seq = list(position_seq) if position_seq is not None else [0.0]
        self._order_status_resp = order_status_resp
        self._mids = mids or {}
        self.user_state_calls = 0

    def _next_szi(self):
        if len(self._position_seq) > 1:
            return self._position_seq.pop(0)
        return self._position_seq[0] if self._position_seq else 0.0

    def user_state(self, address):
        self.user_state_calls += 1
        szi = self._next_szi()
        if szi is None:
            raise RuntimeError("info down")
        return {"assetPositions": [{"position": {"coin": "DOGE", "szi": str(szi)}}]}

    def all_mids(self):
        return self._mids

    def query_order_by_cloid(self, address, cloid):
        return self._order_status_resp

    def query_order_by_oid(self, address, oid):
        return self._order_status_resp


class FakeExchange:
    def __init__(self, *, close_resp=None, close_raises=None, cancel_resp=None,
                 cancel_raises=None):
        self._close_resp = close_resp
        self._close_raises = close_raises
        self._cancel_resp = cancel_resp
        self._cancel_raises = cancel_raises
        self.cancel_calls = []

    def market_close(self, coin, slippage=None):
        if self._close_raises is not None:
            raise self._close_raises
        return self._close_resp

    def cancel(self, coin, oid):
        self.cancel_calls.append(oid)
        if self._cancel_raises is not None:
            raise self._cancel_raises
        if callable(self._cancel_resp):
            return self._cancel_resp(oid)
        return self._cancel_resp


def make_trader(info=None, exchange=None, sz=None):
    """HyperliquidTrader ohne Netzwerk-__init__."""
    t = HyperliquidTrader.__new__(HyperliquidTrader)
    t.address = "0x" + "1" * 40
    t.builder = None
    t.info = info if info is not None else FakeInfo()
    t.exchange = exchange if exchange is not None else FakeExchange()
    t._sz = sz or {"DOGE": 0, "BTC": 2, "SOL": 2, "ADA": 3}
    t._max_lev = {}
    return t


@pytest.fixture(autouse=True)
def _reset_breaker():
    """Rate-Limit-Breaker zwischen Tests zurücksetzen (Modul-globaler Zustand)."""
    hl_retry._rate_limited_until = 0.0
    yield
    hl_retry._rate_limited_until = 0.0


# ---------------------------------------------------------------------------
# _round_sz — FLOOR statt round-half-even (H-9-Teil)
# ---------------------------------------------------------------------------
def test_round_sz_floors_doge_szdec0():
    t = make_trader()
    # 3.9 darf NICHT auf 4.0 aufrunden (würde Risk/Margin über das Limit treiben)
    assert t._round_sz("DOGE", 3.9) == 3.0
    assert t._round_sz("DOGE", 3.0) == 3.0
    assert t._round_sz("DOGE", 3.999) == 3.0


def test_round_sz_floors_but_keeps_valid_decimals():
    t = make_trader()
    # Float-Repräsentation darf die letzte gültige Stelle nicht wegfressen
    assert t._round_sz("ADA", 0.3) == 0.3
    assert t._round_sz("SOL", 1.239) == 1.23
    assert t._round_sz("SOL", 1.2) == 1.2


def test_round_sz_zero_and_negative():
    t = make_trader()
    assert t._round_sz("BTC", 0.0) == 0.0
    assert t._round_sz("BTC", -5) == 0.0


# ---------------------------------------------------------------------------
# C-1 — close_position
# ---------------------------------------------------------------------------
def test_close_position_already_flat_returns_ok():
    """psz==0 vor dem Close → schon flat = Erfolg, kein market_close nötig."""
    t = make_trader(info=FakeInfo(position_seq=[0.0]))
    res = t.close_position("DOGE")
    assert res["ok"] is True
    assert res["closed"] == 0.0
    assert res["still_open"] == 0.0


def test_close_position_sdk_returns_none_is_flat_success():
    """SDK market_close gibt None zurück (keine Position gefunden) → Erfolg."""
    # psz beim ersten Read != 0 (sonst kein market_close), danach None vom SDK.
    t = make_trader(
        info=FakeInfo(position_seq=[10.0]),
        exchange=FakeExchange(close_resp=None),
    )
    res = t.close_position("DOGE")
    assert res["ok"] is True
    assert res["still_open"] == 0.0


def test_close_position_ioc_no_match_reports_failure_and_still_open():
    """C-1 Kern: status==ok ABER statuses[].error (IoC konnte nicht matchen) und
    der Re-Read zeigt die Position weiterhin offen → ok=False + still_open>0."""
    no_match = _ok([{"error": "Order could not immediately match against any resting orders."}])
    # Erst-Read 10 (Position da), Re-Read bleibt 10 (nicht geschlossen).
    t = make_trader(
        info=FakeInfo(position_seq=[10.0, 10.0]),
        exchange=FakeExchange(close_resp=no_match),
    )
    res = t.close_position("DOGE")
    assert res["ok"] is False
    assert res["still_open"] == 10.0
    assert res["closed"] == 0.0


def test_close_position_success_confirmed_by_reread():
    """Gefüllt + Re-Read zeigt flat → ok=True, closed=Größe, still_open=0."""
    filled = _ok([{"filled": {"totalSz": "10.0"}}])
    t = make_trader(
        info=FakeInfo(position_seq=[10.0, 0.0]),
        exchange=FakeExchange(close_resp=filled),
    )
    res = t.close_position("DOGE")
    assert res["ok"] is True
    assert res["still_open"] == 0.0
    assert res["closed"] == 10.0


def test_close_position_partial_fill_reports_remaining():
    """Partial-IoC-Fill: statuses meldet 4 gefüllt, Re-Read zeigt 6 offen →
    ok=False (nicht komplett), still_open=6, closed=4."""
    partial = _ok([{"filled": {"totalSz": "4.0"}}])
    t = make_trader(
        info=FakeInfo(position_seq=[10.0, 6.0]),
        exchange=FakeExchange(close_resp=partial),
    )
    res = t.close_position("DOGE")
    assert res["ok"] is False
    assert res["still_open"] == 6.0
    assert res["closed"] == 4.0


def test_close_position_exception_returns_not_ok():
    t = make_trader(
        info=FakeInfo(position_seq=[10.0]),
        exchange=FakeExchange(close_raises=RuntimeError("boom non-transient")),
    )
    res = t.close_position("DOGE")
    assert res["ok"] is False
    assert res["still_open"] == 10.0


# ---------------------------------------------------------------------------
# C-2 — Cloid-Idempotenz: Duplicate-Reject → Erfolg
# ---------------------------------------------------------------------------
def test_place_entry_duplicate_cloid_reject_is_success():
    """Retry traf eine schon akzeptierte Order (Response verloren) → die ERSTE
    Order lebt → ok=True + dedup=True, NICHT FAILED (sonst nackte Order)."""
    dup = _ok([{"error": "Order has invalid cloid: cloid already exists"}])
    t = make_trader(exchange=FakeExchange())
    # _order direkt patchen, damit wir die SDK-Order-Antwort kontrollieren
    t._order = lambda *a, **k: dup
    res = t.place_entry("DOGE", True, 10, 0.1)
    assert res["ok"] is True
    assert res["dedup"] is True
    assert res["cloid"] is not None
    assert res["error"] is None


def test_place_entry_real_error_is_failure():
    """Ein echter (nicht-Cloid) Fehler bleibt ok=False."""
    err = _ok([{"error": "Insufficient margin to place order"}])
    t = make_trader()
    t._order = lambda *a, **k: err
    res = t.place_entry("DOGE", True, 10, 0.1)
    assert res["ok"] is False
    assert "margin" in str(res["error"]).lower()


def test_place_entry_resting_returns_oid_and_cloid():
    resting = _ok([{"resting": {"oid": 555}}])
    t = make_trader()
    t._order = lambda *a, **k: resting
    res = t.place_entry("DOGE", True, 10, 0.1)
    assert res["ok"] is True
    assert res["resting_oid"] == 555
    assert res["cloid"] is not None


def test_place_protection_duplicate_cloid_sl_counts_as_ok():
    """C-2 auf dem reduce-only-SL-Pfad: Duplicate-Cloid-Reject heißt die erste
    SL-Order lebt → sl_ok=True (sonst Notfall-Close einer geschützten Position)."""
    dup = _ok([{"error": "cloid already exists"}])
    t = make_trader(info=FakeInfo(mids={}))  # mark=0 → Preflight übersprungen
    t._order = lambda *a, **k: dup
    res = t.place_protection("DOGE", True, 10, 0.09, [])
    assert res["sl_ok"] is True


# ---------------------------------------------------------------------------
# H-6 — Cancel-Parsing
# ---------------------------------------------------------------------------
def test_cancel_order_oid_already_filled_sets_flag():
    """'already filled' → ok=True (Order ist weg) ABER already_filled=True
    (Position lebt → Caller muss in den Schutzpfad)."""
    filled = _ok([{"error": "Order was already filled"}])
    t = make_trader(exchange=FakeExchange(cancel_resp=filled))
    res = t.cancel_order_oid("DOGE", 123)
    assert res["already_filled"] is True
    assert res["ok"] is True


def test_cancel_order_oid_success():
    t = make_trader(exchange=FakeExchange(cancel_resp=_ok(["success"])))
    res = t.cancel_order_oid("DOGE", 123)
    assert res["ok"] is True
    assert res["already_filled"] is False


def test_cancel_order_oid_no_oid():
    t = make_trader()
    res = t.cancel_order_oid("DOGE", None)
    assert res["ok"] is False
    assert res["already_filled"] is False


def test_cancel_orders_does_not_count_already_filled():
    """H-6 Kern: cancel_orders zählt eine 'already filled'-Order NICHT als Cancel."""
    class _Info(FakeInfo):
        def open_orders(self, address):
            return [{"coin": "DOGE", "oid": 1}, {"coin": "DOGE", "oid": 2}]

    def cancel_resp(oid):
        if oid == 1:
            return _ok(["success"])                       # echt gecancelt
        return _ok([{"error": "Order was already filled"}])  # NICHT zählen

    t = make_trader(info=_Info(), exchange=FakeExchange(cancel_resp=cancel_resp))
    n = t.cancel_orders("DOGE")
    assert n == 1  # nur oid 1, NICHT oid 2 (gefüllt)


# ---------------------------------------------------------------------------
# order_status — Parsing
# ---------------------------------------------------------------------------
def test_order_status_filled():
    resp = {"status": "order", "order": {"order": {"origSz": "2.0", "sz": "0.0"}, "status": "filled"}}
    t = make_trader(info=FakeInfo(order_status_resp=resp))
    res = t.order_status(cloid="0x" + "a" * 32)
    assert res["status"] == "filled"
    assert res["filled_sz"] == 2.0


def test_order_status_partial():
    resp = {"status": "order", "order": {"order": {"origSz": "2.0", "sz": "0.5"}, "status": "open"}}
    t = make_trader(info=FakeInfo(order_status_resp=resp))
    res = t.order_status(oid=42)
    assert res["status"] == "partial"
    assert res["filled_sz"] == 1.5


def test_order_status_open():
    resp = {"status": "order", "order": {"order": {"origSz": "2.0", "sz": "2.0"}, "status": "open"}}
    t = make_trader(info=FakeInfo(order_status_resp=resp))
    res = t.order_status(oid=42)
    assert res["status"] == "open"
    assert res["filled_sz"] == 0.0


def test_order_status_canceled():
    resp = {"status": "order", "order": {"order": {"origSz": "2.0", "sz": "2.0"}, "status": "canceled"}}
    t = make_trader(info=FakeInfo(order_status_resp=resp))
    res = t.order_status(oid=42)
    assert res["status"] == "canceled"


def test_order_status_unknown_oid():
    t = make_trader(info=FakeInfo(order_status_resp={"status": "unknownOid"}))
    res = t.order_status(oid=999)
    assert res["status"] == "unknown"
    assert res["filled_sz"] == 0.0


def test_order_status_no_args():
    t = make_trader()
    res = t.order_status()
    assert res["status"] == "unknown"


def test_order_status_exception_safe():
    class _Boom(FakeInfo):
        def query_order_by_oid(self, address, oid):
            raise RuntimeError("info boom non-transient")
    t = make_trader(info=_Boom())
    res = t.order_status(oid=1)
    assert res["status"] == "unknown"
    assert res["filled_sz"] == 0.0
    assert "error" in res


# ---------------------------------------------------------------------------
# H-12 — Rate-Limit-Breaker
# ---------------------------------------------------------------------------
def test_breaker_set_and_query():
    assert hl_retry.is_hl_rate_limited() is False
    hl_retry.note_rate_limit(5)
    assert hl_retry.is_hl_rate_limited() is True
    assert hl_retry.hl_rate_limit_remaining() > 0


def test_429_exception_sets_breaker():
    """Ein 429 im hl_retry-Pfad setzt den prozessweiten Breaker."""
    class CE(Exception):
        def __init__(self):
            self.header = {"Retry-After": "3"}
            super().__init__("HTTP 429 rate limit exceeded")

    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise CE()

    with pytest.raises(Exception):
        hl_retry.hl_retry(fn, max_attempts=2, initial_delay=0.0, label="t")
    assert hl_retry.is_hl_rate_limited() is True


def test_retry_after_parsing():
    class CE(Exception):
        def __init__(self):
            self.header = {"Retry-After": "12"}
    assert hl_retry._retry_after_from_exc(CE()) == 12.0
    assert hl_retry._retry_after_from_exc(RuntimeError("no header")) is None


# ---------------------------------------------------------------------------
# M-2 — open_positions_count RAISES on read fail (fail-closed), nicht 0
# ---------------------------------------------------------------------------
class _CountInfo:
    """Info-Fake für open_positions_count: liefert N offene Positionen oder
    raised (non-transient) bei raises=…."""
    def __init__(self, positions=None, raises=None):
        self._positions = positions or []
        self._raises = raises
        self.calls = 0

    def user_state(self, address):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return {"assetPositions": [{"position": {"coin": c, "szi": str(s)}}
                                   for c, s in self._positions]}


def test_open_positions_count_counts_nonzero():
    t = make_trader(info=_CountInfo(positions=[("BTC", 0.1), ("DOGE", 0.0), ("SOL", -3)]))
    # DOGE szi==0 zählt nicht → 2
    assert t.open_positions_count() == 2


def test_open_positions_count_raises_on_read_fail():
    """M-2 Kern: ein Read-Fehler darf NICHT 0 liefern (max-open-Gate fail-OPEN),
    sondern muss durchreichen → Engine bricht den Entry fail-closed ab."""
    info = _CountInfo(raises=RuntimeError("info down non-transient"))
    t = make_trader(info=info)
    with pytest.raises(RuntimeError):
        t.open_positions_count()
    assert info.calls == 1  # non-transient → kein Retry-Sturm


# ---------------------------------------------------------------------------
# M-6 — covered_stop_size side-aware + read-fail-Eskalation
# ---------------------------------------------------------------------------
class _OrdersInfo:
    """Info-Fake mit frontend_open_orders (+ optional raises für Read-Fail)."""
    def __init__(self, orders=None, raises=None):
        self._orders = orders
        self._raises = raises
        self.calls = 0

    def frontend_open_orders(self, address):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self._orders


def _stop(side, sz, coin="DOGE", reduce_only=True, otype="Stop Market"):
    return {"coin": coin, "side": side, "sz": str(sz),
            "reduceOnly": reduce_only, "orderType": otype}


def test_covered_stop_size_side_aware_long():
    """LONG-Position (position_is_long=True) wird nur von reduce-only SELL-Stops
    (side 'A') gedeckt; ein falsch-seitiger BUY-Stop (side 'B') zählt NICHT."""
    orders = [_stop("A", 5), _stop("B", 7)]  # 5 deckt LONG, 7 ist wrong-side
    t = make_trader(info=_OrdersInfo(orders=orders))
    assert t.covered_stop_size("DOGE", position_is_long=True) == 5.0
    # SHORT würde umgekehrt nur die 7 zählen
    assert t.covered_stop_size("DOGE", position_is_long=False) == 7.0
    # None = alte Semantik: beide Seiten zählen
    assert t.covered_stop_size("DOGE") == 12.0


def test_covered_stop_size_ignores_non_stop_and_non_reduceonly():
    orders = [
        _stop("A", 5),
        _stop("A", 9, reduce_only=False),       # kein reduce-only
        _stop("A", 4, otype="Take Profit Market"),  # TP, kein stop
        {"coin": "BTC", "side": "A", "sz": "99", "reduceOnly": True, "orderType": "Stop Market"},  # anderer coin
    ]
    t = make_trader(info=_OrdersInfo(orders=orders))
    assert t.covered_stop_size("DOGE", position_is_long=True) == 5.0


def test_covered_stop_size_read_fail_returns_inf_and_escalates():
    """M-6: einzelner Read-Fehler → inf (fail-safe). Ab _COVERED_READ_ALERT_AT
    Fehlern in Folge ESKALIERT der Wrapper (ERROR-Log)."""
    info = _OrdersInfo(raises=RuntimeError("boom non-transient"))
    t = make_trader(info=info)
    import math
    # 2x fail unter der Schwelle → noch WARNING, aber immer inf
    assert math.isinf(t.covered_stop_size("DOGE", position_is_long=True))
    assert math.isinf(t.covered_stop_size("DOGE", position_is_long=True))
    assert t._covered_read_fails["DOGE"] == 2
    # 3. fail → Schwelle erreicht (ERROR), weiterhin inf
    assert math.isinf(t.covered_stop_size("DOGE", position_is_long=True))
    assert t._covered_read_fails["DOGE"] == 3


def test_covered_stop_size_success_resets_fail_counter():
    """Nach einem erfolgreichen Read ist der Fehlerzähler für den Coin weg."""
    info = _OrdersInfo(raises=RuntimeError("boom non-transient"))
    t = make_trader(info=info)
    t.covered_stop_size("DOGE", position_is_long=True)
    assert t._covered_read_fails.get("DOGE") == 1
    # jetzt liefert der Read Orders → Zähler reset
    t.info = _OrdersInfo(orders=[_stop("A", 5)])
    assert t.covered_stop_size("DOGE", position_is_long=True) == 5.0
    assert "DOGE" not in t._covered_read_fails


# ---------------------------------------------------------------------------
# L-4 — _round_px: über 100k Integer-Preise erlauben
# ---------------------------------------------------------------------------
def test_round_px_above_100k_is_integer():
    """BTC-SL bei 104237 darf NICHT auf den 10er gerundet werden (±5 Shift)."""
    t = make_trader(sz={"BTC": 2})
    assert t._round_px("BTC", 104237) == 104237.0
    assert t._round_px("BTC", 104237.6) == 104238.0


def test_round_px_below_100k_keeps_5sigfig_path():
    """Unter 100k bleibt die 5-sig-fig + max-Dezimal-Regel aktiv (kein Regress)."""
    t = make_trader(sz={"ADA": 3})
    # 5 sig figs, max 3 dezimal: 0.34567 → 0.34567 →5g→ 0.34567 → round(,3)=0.346
    assert t._round_px("ADA", 0.34567) == 0.346


# ---------------------------------------------------------------------------
# L-5 — auto_leverage: FLOOR statt round (nie über die safe-Schranke)
# ---------------------------------------------------------------------------
def test_auto_leverage_floors_not_rounds_up():
    from app.sizing import auto_leverage
    # safe_lev = 1/(sl_dist*liq_safety). Wähle Werte, die chosen=24.6 ergeben:
    # sl_dist = 1/(safe*liq_safety). Mit liq_safety=2, conf=1.0 → chosen=safe.
    # entry=100, stop_loss so dass sl_dist*1 = 1/safe → safe=24.6 → sl_dist=0.0203252
    entry = 100.0
    sl_dist = 1.0 / (24.6 * 2.0)      # → safe_lev 24.6 bei liq_safety=2
    stop = entry * (1 - sl_dist)
    lev, _ = auto_leverage(entry=entry, stop_loss=stop, confidence=1.0,
                           liq_safety=2.0, max_cap=50)
    # round(24.6)=25 (zu aggressiv) — floor muss 24 liefern
    assert lev == 24


def test_auto_leverage_min_one():
    from app.sizing import auto_leverage
    # Sehr weiter SL → safe_lev < 1 → floor 0, aber max(1,…) garantiert ≥1
    lev, _ = auto_leverage(entry=100.0, stop_loss=50.0, confidence=1.0, liq_safety=2.0)
    assert lev >= 1


# ---------------------------------------------------------------------------
# L-7 — _order: TypeError-Fallback droppt den builder NICHT mehr für
#       beliebige TypeErrors (nur echter kwarg-Probe-Fall)
# ---------------------------------------------------------------------------
class _BuilderExchange:
    """Exchange-Fake, das order()-kwargs aufzeichnet + steuerbare TypeErrors wirft.

    reject_kwargs: Menge von kwarg-Namen, deren Anwesenheit einen
    'unexpected keyword argument'-TypeError auslöst (simuliert eine alte SDK).
    raise_internal: ein TypeError TIEF aus order() (KEIN kwarg-Problem)."""
    def __init__(self, *, reject_kwargs=None, raise_internal=False):
        self.reject_kwargs = set(reject_kwargs or [])
        self.raise_internal = raise_internal
        self.calls = []  # Liste der kwargs-dicts pro order()-Aufruf

    def order(self, coin, is_buy, sz, px, otype, **kwargs):
        self.calls.append(kwargs)
        if self.raise_internal:
            raise TypeError("unsupported operand type(s) for +: 'int' and 'str'")
        for k in self.reject_kwargs:
            if k in kwargs:
                raise TypeError(f"order() got an unexpected keyword argument '{k}'")
        return _ok([{"resting": {"oid": 1}}])


def test_order_internal_typeerror_does_not_drop_builder():
    """L-7 Kern: ein NICHT-kwarg TypeError aus order() muss durchschlagen (raise),
    NICHT still ohne builder neu senden."""
    ex = _BuilderExchange(raise_internal=True)
    t = make_trader(exchange=ex)
    t.builder = {"b": "0x" + "a" * 40, "f": 50}
    with pytest.raises(TypeError):
        t._order("DOGE", True, 10, 0.1, {"limit": {"tif": "Gtc"}})
    # nur EIN Versuch, KEIN builder-loser Re-Send
    assert len(ex.calls) == 1


def test_order_kwarg_probe_fallback_keeps_builder():
    """Echter kwarg-Probe-Fall: SDK kennt cloid= nicht, builder= aber schon →
    Fallback, und der builder bleibt erhalten (wird im Fallback wieder mitgegeben)."""
    ex = _BuilderExchange(reject_kwargs={"cloid"})
    t = make_trader(exchange=ex)
    t.builder = {"b": "0x" + "a" * 40, "f": 50}
    from app.hyperliquid_exec import Cloid
    res = t._order("DOGE", True, 10, 0.1, {"limit": {"tif": "Gtc"}},
                   cloid=Cloid.from_str("0x" + "1" * 32))
    assert t._status_ok(res)
    # 1. Call hatte cloid (warf), 2. Call (Fallback) MUSS builder weiterhin haben
    assert "builder" in ex.calls[-1]
    assert "cloid" not in ex.calls[-1]


def test_order_old_sdk_without_builder_kwarg_falls_back_minimal():
    """Ganz alte SDK ohne builder= UND ohne cloid=kwarg → minimaler Call ohne
    builder (Fee in dieser SDK nicht setzbar), aber Order geht raus (kein Crash)."""
    ex = _BuilderExchange(reject_kwargs={"builder", "cloid"})
    t = make_trader(exchange=ex)
    t.builder = {"b": "0x" + "a" * 40, "f": 50}
    res = t._order("DOGE", True, 10, 0.1, {"limit": {"tif": "Gtc"}})
    assert t._status_ok(res)
    assert "builder" not in ex.calls[-1]


# ---------------------------------------------------------------------------
# M-17 — get_meta: prozessweiter Cache + Retry (nur EIN Read pro Netz)
# ---------------------------------------------------------------------------
def test_get_meta_caches_per_net(monkeypatch):
    import app.hyperliquid_exec as hx

    calls = {"n": 0}

    class _FakeInfoMeta:
        def meta(self):
            calls["n"] += 1
            return {"universe": [{"name": "BTC", "szDecimals": 2, "maxLeverage": 50}]}

    # Cache + Singleton leeren, get_info auf den Fake umbiegen
    hx._meta_cache.clear()
    monkeypatch.setattr(hx, "get_info", lambda testnet: _FakeInfoMeta())
    m1 = hx.get_meta(True)
    m2 = hx.get_meta(True)
    assert m1 is m2                 # gecacht (gleiches Objekt)
    assert calls["n"] == 1          # nur EIN meta()-Read trotz 2 Aufrufen
    hx._meta_cache.clear()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
