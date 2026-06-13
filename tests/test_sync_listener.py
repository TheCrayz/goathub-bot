"""Audit-Fixes 2026-06-12 (Listener + Sync) — Regressions-Tests.

Abgedeckt:
  M-7 / LOW-4 — discord_listener: Backfill-Helper (_backfill_missed) holt
      verpasste Messages nach Reconnect und schickt ALLE Embeds jeder Message
      (in Reihenfolge) durch denselben Pfad wie live; SIGNAL_BACKFILL-Toggle.
      (M-6, backoff-Reset, lebt im on_ready-Closure — kein isolierter Test.)
  B-#5 / H-6 — sync: 2-Strike-Regel flippt erst nach N leeren HL-Antworten;
      clear_strikes resettet die Zählung.
  LOW-5 — sync: Flip auf 'closed' cancelt verwaiste Trigger-Orders, aber NIE
      wenn inzwischen eine neue, nicht-geschlossene Row existiert.
  Review #3 — sync: Coverage-Reconciler (engine.reconcile_stop_coverage)
      läuft jede N-te Sync-Iteration.
  Parser — CANCEL via Action-Feld UND Titel-Fallback, fehlende Felder → None,
      malformed Zahl ("1,234.5") → dokumentiert fail-closed.

KEIN Netz: HL + Discord komplett gemockt / gefaked.

Run:
    PYTHONPATH=. venv/bin/python -m pytest tests/test_sync_listener.py -q
"""
import asyncio
import os
import types

import pytest

# Test-only env so wir die echten config-Checks (JWT/ENCRYPTION) passieren.
# Key wird zur Laufzeit generiert — NIE hartcodiert (Secret-Leak-Klasse).
from cryptography.fernet import Fernet
os.environ.setdefault("JWT_SECRET", "test-only-secret-not-prod-1234567890abcdef")
os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Unter pytest verdrahtet tests/conftest.py die EINE geteilte StaticPool-
# :memory:-Engine — dann hier NICHTS anfassen. Nur standalone selbst bauen.
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

from app.db import Base, SessionLocal
from app.models import Activity, ManagedTrade, User

Base.metadata.create_all(_dbmod.engine)

# App-Module NACH dem DB-Setup importieren.
import app.discord_listener as dl
import app.engine as engine
import app.hyperliquid_exec as hl_exec
import app.parser as parser
import app.sync as sync


# ── Helpers ─────────────────────────────────────────────────────────────────
def _make_user(db, *, user_id=1, bot_active=True, hl_account_address="0x12E3",
               hl_api_secret_enc="enc"):
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
                  stop_loss=68000.0, status="open", resting_oid=None,
                  entry_oid=None, entry_cloid=None, updated_at=None):
    mt = ManagedTrade(
        user_id=user_id, coin=coin, direction=direction, entry=entry,
        stop_loss=stop_loss, take_profits="[]", status=status,
        resting_oid=resting_oid, entry_oid=entry_oid, entry_cloid=entry_cloid,
    )
    db.add(mt)
    db.commit()
    # updated_at hat onupdate=utcnow → nach dem commit explizit zurückdatieren
    # (H-2-Tests brauchen eine Row, die älter als der Watcher-Grace-Timer ist).
    if updated_at is not None:
        mt.updated_at = updated_at
        db.commit()
    return mt


def _reset_db():
    """Cleanly truncate alles + In-Memory-State für nächsten Test."""
    db = SessionLocal()
    try:
        db.query(Activity).delete()
        db.query(ManagedTrade).delete()
        db.query(User).delete()
        db.commit()
    finally:
        db.close()
    sync._stale_counter.clear()
    # Python 3.9: asyncio.Lock bindet den Loop bei Erzeugung — Locks aus einem
    # früheren asyncio.run() wären im nächsten Loop kaputt.
    engine._locks.clear()


def _embed_dict(*, title="", description="", fields=None):
    return {"title": title, "description": description, "fields": fields or []}


# ── Parser ──────────────────────────────────────────────────────────────────
def test_parser_cancel_via_action_field():
    """CANCEL über das Action-Feld — braucht KEINE Entry/SL-Level."""
    sig = parser.parse_signal(_embed_dict(
        description="Signal `abc-123`",
        fields=[{"name": "Action", "value": "CANCEL_TRADE"},
                {"name": "Ticker", "value": "BTC/USDT"}],
    ))
    assert sig is not None
    assert sig.action == "CANCEL_TRADE"
    assert sig.ticker == "BTC/USDT"
    assert sig.signal_id == "abc-123"
    assert sig.take_profits == []


def test_parser_cancel_via_title_fallback():
    """Kein Action-Feld → Action + Ticker aus dem Titel 'TICKER — ACTION'."""
    sig = parser.parse_signal(_embed_dict(title="BTC/USDT — CANCEL"))
    assert sig is not None
    assert sig.action == "CANCEL"
    assert sig.ticker == "BTC/USDT"
    assert sig.entry is None and sig.stop_loss is None


def test_parser_missing_fields_return_none():
    # Komplett leeres Embed → kein Action, kein Titel → None
    assert parser.parse_signal(_embed_dict()) is None
    # Action ohne Ticker → None
    assert parser.parse_signal(_embed_dict(
        fields=[{"name": "Action", "value": "NEW_TRADE"}])) is None
    # NEW_TRADE ohne Entry → None
    assert parser.parse_signal(_embed_dict(
        fields=[{"name": "Action", "value": "NEW_TRADE"},
                {"name": "Ticker", "value": "ETH/USDT"},
                {"name": "Stop Loss", "value": "1200"}])) is None
    # NEW_TRADE ohne Stop Loss → None
    assert parser.parse_signal(_embed_dict(
        fields=[{"name": "Action", "value": "NEW_TRADE"},
                {"name": "Ticker", "value": "ETH/USDT"},
                {"name": "Entry", "value": "1300"}])) is None


def test_parser_update_trade_without_entry_is_accepted():
    """Review #18 (dokumentiertes Verhalten): UPDATE_TRADE braucht kein Entry —
    reine SL-Trail-Updates dürfen nicht verworfen werden."""
    sig = parser.parse_signal(_embed_dict(
        fields=[{"name": "Action", "value": "UPDATE_TRADE"},
                {"name": "Ticker", "value": "ETH/USDT"},
                {"name": "Stop Loss", "value": "1250"}]))
    assert sig is not None
    assert sig.entry is None
    assert sig.stop_loss == 1250.0


def test_parser_malformed_number_fails_closed():
    """DOKUMENTIERTES Verhalten: '1,234.5' (Tausender-Komma) ist für float()
    nicht parsebar → _num liefert None → ein NEW_TRADE mit so einem Entry wird
    KOMPLETT verworfen (fail-closed: lieber kein Trade als ein falscher Preis).
    Würde Bot 1 jemals Tausender-Kommas senden, muss _num erweitert werden."""
    assert parser._num("1,234.5") is None
    sig = parser.parse_signal(_embed_dict(
        fields=[{"name": "Action", "value": "NEW_TRADE"},
                {"name": "Ticker", "value": "BTC/USDT"},
                {"name": "Direction", "value": "LONG"},
                {"name": "Entry", "value": "1,234.5"},
                {"name": "Stop Loss", "value": "1200"}]))
    assert sig is None


def test_parser_new_trade_full_roundtrip():
    sig = parser.parse_signal(_embed_dict(
        description="`sig-9`",
        fields=[{"name": "Action", "value": "NEW_TRADE"},
                {"name": "Ticker", "value": "SOL/USDT"},
                {"name": "Direction", "value": "LONG"},
                {"name": "Entry", "value": "100"},
                {"name": "Stop Loss", "value": "95"},
                {"name": "Take Profits", "value": "50% @ 110, 50% @ 120"},
                {"name": "Confidence", "value": "0.9"}]))
    assert sig is not None
    assert (sig.entry, sig.stop_loss, sig.confidence) == (100.0, 95.0, 0.9)
    assert [(tp.percent, tp.price) for tp in sig.take_profits] == [(50.0, 110.0), (50.0, 120.0)]


# ── Listener: Embeds + Backfill (M-7 / LOW-4) ───────────────────────────────
def _fake_embed(title, description="`id-x`", fields=()):
    return types.SimpleNamespace(
        title=title, description=description,
        fields=[types.SimpleNamespace(name=n, value=v) for n, v in fields])


def _fake_msg(msg_id, embeds=()):
    return types.SimpleNamespace(id=msg_id, embeds=list(embeds))


class _FakeChannel:
    def __init__(self, messages):
        self.messages = messages
        self.history_kwargs = None

    def history(self, *, after=None, limit=None, oldest_first=False):
        self.history_kwargs = {"after": after, "limit": limit, "oldest_first": oldest_first}
        after_id = getattr(after, "id", 0) or 0
        msgs = sorted((m for m in self.messages if m.id > after_id), key=lambda m: m.id)
        if not oldest_first:
            msgs = list(reversed(msgs))
        if limit is not None:
            msgs = msgs[:limit]

        async def _gen():
            for m in msgs:
                yield m
        return _gen()


def test_handle_embeds_processes_all_in_order(monkeypatch):
    """LOW-4: ALLE Embeds einer Message gehen an die Engine, in Reihenfolge."""
    received = []

    async def _record(embed_dict):
        received.append(embed_dict)

    monkeypatch.setattr(dl, "handle_signal", _record)
    e1 = _fake_embed("BTC/USDT — NEW_TRADE")
    e2 = _fake_embed("ETH/USDT — CANCEL")
    asyncio.run(dl._handle_embeds([e1, e2]))
    assert [d["title"] for d in received] == ["BTC/USDT — NEW_TRADE", "ETH/USDT — CANCEL"]


def test_handle_embeds_error_does_not_block_rest(monkeypatch):
    """Ein kaputtes Embed darf die restlichen nicht blockieren."""
    received = []

    async def _flaky(embed_dict):
        if embed_dict["title"] == "boom":
            raise RuntimeError("kaputt")
        received.append(embed_dict["title"])

    monkeypatch.setattr(dl, "handle_signal", _flaky)
    asyncio.run(dl._handle_embeds([_fake_embed("boom"), _fake_embed("ok")]))
    assert received == ["ok"]


def test_backfill_missed_feeds_messages_after_anchor(monkeypatch):
    """M-7: nur Messages NACH dem Anker, oldest-first, gecappt, alle Embeds
    durch denselben Handler-Pfad wie live."""
    received = []

    async def _record(embed_dict):
        received.append(embed_dict["title"])

    monkeypatch.setattr(dl, "handle_signal", _record)
    channel = _FakeChannel([
        _fake_msg(8, [_fake_embed("alt — schon live gesehen")]),
        _fake_msg(10, [_fake_embed("sig-A"), _fake_embed("sig-B")]),   # 2 Embeds!
        _fake_msg(11, []),                                             # ohne Embeds
        _fake_msg(12, [_fake_embed("sig-C")]),
    ])
    anchor = types.SimpleNamespace(id=9)

    n, last_id = asyncio.run(dl._backfill_missed(channel, anchor))

    assert (n, last_id) == (3, 12)
    assert received == ["sig-A", "sig-B", "sig-C"]
    assert channel.history_kwargs["oldest_first"] is True
    assert channel.history_kwargs["limit"] == dl._BACKFILL_LIMIT == 50
    assert channel.history_kwargs["after"] is anchor


def test_backfill_missed_propagates_fetch_error():
    """History-Fehler raisen zum Caller (on_ready fängt sie dort und lässt den
    Live-Listener weiterlaufen)."""
    class _BrokenChannel:
        def history(self, **kwargs):
            raise RuntimeError("discord history down")

    with pytest.raises(RuntimeError):
        asyncio.run(dl._backfill_missed(_BrokenChannel(), types.SimpleNamespace(id=1)))


def test_backfill_enabled_toggle(monkeypatch):
    monkeypatch.delenv("SIGNAL_BACKFILL", raising=False)
    assert dl._backfill_enabled() is True          # default: an
    monkeypatch.setenv("SIGNAL_BACKFILL", "false")
    assert dl._backfill_enabled() is False
    monkeypatch.setenv("SIGNAL_BACKFILL", "0")
    assert dl._backfill_enabled() is False
    monkeypatch.setenv("SIGNAL_BACKFILL", "true")
    assert dl._backfill_enabled() is True


# ── Listener: H-14 toter SIGNALS_CHANNEL_ID → Alert statt stiller Ausfall ────
class _FakeClient:
    """discord.Client-Fake: get_channel (Cache) + fetch_channel (REST, async)
    nach Vorgabe. raises=True → fetch_channel wirft (gelöschter Channel)."""
    def __init__(self, get_result=None, fetch_result=None, fetch_raises=False):
        self._get = get_result
        self._fetch = fetch_result
        self._fetch_raises = fetch_raises
        self.get_calls = 0
        self.fetch_calls = 0

    def get_channel(self, cid):
        self.get_calls += 1
        return self._get

    async def fetch_channel(self, cid):
        self.fetch_calls += 1
        if self._fetch_raises:
            raise RuntimeError("Unknown Channel (404)")
        return self._fetch


def test_h14_resolve_channel_returns_none_when_both_fail():
    """get_channel None + fetch_channel wirft → _resolve_signals_channel liefert
    None (= toter Channel, oben löst das den Alert aus)."""
    client = _FakeClient(get_result=None, fetch_raises=True)
    ch = asyncio.run(dl._resolve_signals_channel(client))
    assert ch is None
    assert client.get_calls == 1 and client.fetch_calls == 1


def test_h14_resolve_channel_get_hit_skips_fetch():
    """get_channel trifft (Cache) → fetch_channel wird gar nicht erst gerufen."""
    sentinel = types.SimpleNamespace(id=42)
    client = _FakeClient(get_result=sentinel)
    ch = asyncio.run(dl._resolve_signals_channel(client))
    assert ch is sentinel
    assert client.fetch_calls == 0


def test_h14_resolve_channel_falls_back_to_fetch():
    """get_channel None, aber fetch_channel liefert (nicht gecacht, aber lebt)."""
    sentinel = types.SimpleNamespace(id=42)
    client = _FakeClient(get_result=None, fetch_result=sentinel)
    ch = asyncio.run(dl._resolve_signals_channel(client))
    assert ch is sentinel
    assert client.get_calls == 1 and client.fetch_calls == 1


def test_h14_dead_channel_posts_alert(monkeypatch):
    """H-14 (Kern): toter Channel (beide Auflösungen scheitern) → _alert ruft
    engine._post_alert. Vorher empfing der Listener SCHWEIGEND null Signale."""
    alerts = []
    monkeypatch.setattr(engine, "_post_alert", lambda text, *a, **k: alerts.append(text))

    # _alert geht über engine._post_alert (lazy import) → Aufruf landet in alerts.
    client = _FakeClient(get_result=None, fetch_raises=True)
    ch = asyncio.run(dl._resolve_signals_channel(client))
    assert ch is None
    # genau die on_ready-Reaktion: bei None alerten
    if ch is None:
        dl._alert("🚨 dead channel test")
    assert len(alerts) == 1 and "dead channel test" in alerts[0]


def test_h14_alert_swallows_post_errors(monkeypatch):
    """_alert ist best-effort: ein Fehler in _post_alert darf nie nach außen
    raisen (sonst stirbt on_ready / der Listener)."""
    def _boom(text, *a, **k):
        raise RuntimeError("webhook down")
    monkeypatch.setattr(engine, "_post_alert", _boom)
    dl._alert("anything")   # darf nicht raisen


# ── Sync: Strike-Logik (B-#5) + LOW-5-Hook ──────────────────────────────────
class _FakeInfo:
    """HL-Info-Fake: user_state liefert die konfigurierten assetPositions."""
    def __init__(self, positions):
        # positions: list of (coin, szi)
        self._positions = positions

    def user_state(self, address):
        return {"assetPositions": [
            {"position": {"coin": c, "szi": str(s)}} for c, s in self._positions]}


def test_stale_strike_flip_after_two_runs_and_leftover_cancel(monkeypatch):
    """B-#5: erst die 2. leere HL-Antwort in Folge flippt auf 'closed'.
    LOW-5: der Flip stößt den Leftover-Order-Cancel für den Coin an."""
    _reset_db()
    db = SessionLocal()
    _make_user(db, user_id=1)
    mt = _make_managed(db, user_id=1, coin="BTC", status="open")
    mt_id = mt.id
    db.close()

    monkeypatch.setattr(hl_exec, "get_info", lambda testnet: _FakeInfo([]))
    cancel_calls = []

    async def _fake_cancel(user_id, coin):
        cancel_calls.append((user_id, coin))

    monkeypatch.setattr(sync, "_cancel_leftover_orders", _fake_cancel)

    # Run 1: Strike 1/2 — Row bleibt offen, kein Cancel
    asyncio.run(sync._reconcile_one_user(1, "0x12E3"))
    db = SessionLocal()
    assert db.get(ManagedTrade, mt_id).status == "open"
    db.close()
    assert sync._stale_counter[(1, "BTC", mt_id)] == 1
    assert cancel_calls == []

    # Run 2: Strike 2/2 — Flip + close-Activity + LOW-5-Cancel
    asyncio.run(sync._reconcile_one_user(1, "0x12E3"))
    db = SessionLocal()
    assert db.get(ManagedTrade, mt_id).status == "closed"
    acts = db.query(Activity).filter(Activity.user_id == 1, Activity.kind == "close").all()
    db.close()
    assert len(acts) == 1 and "BTC" in acts[0].text
    assert cancel_calls == [(1, "BTC")]
    assert (1, "BTC", mt_id) not in sync._stale_counter


def test_clear_strikes_resets_counter(monkeypatch):
    """H-6: clear_strikes löscht getragene Strikes — ein Flip braucht danach
    wieder die VOLLE Strike-Anzahl."""
    _reset_db()
    db = SessionLocal()
    _make_user(db, user_id=1)
    mt = _make_managed(db, user_id=1, coin="BTC", status="open")
    mt_id = mt.id
    db.close()

    monkeypatch.setattr(hl_exec, "get_info", lambda testnet: _FakeInfo([]))

    async def _noop_cancel(user_id, coin):
        pass

    monkeypatch.setattr(sync, "_cancel_leftover_orders", _noop_cancel)

    asyncio.run(sync._reconcile_one_user(1, "0x12E3"))   # Strike 1
    assert sync._stale_counter[(1, "BTC", mt_id)] == 1
    sync.clear_strikes(1, "BTC")                          # Engine öffnet neu
    assert (1, "BTC", mt_id) not in sync._stale_counter
    asyncio.run(sync._reconcile_one_user(1, "0x12E3"))   # wieder nur Strike 1
    db = SessionLocal()
    assert db.get(ManagedTrade, mt_id).status == "open"
    db.close()
    assert sync._stale_counter[(1, "BTC", mt_id)] == 1


def test_strike_resets_when_position_reappears(monkeypatch):
    """HL meldet die Position wieder → Strike-Counter wird zurückgesetzt."""
    _reset_db()
    db = SessionLocal()
    _make_user(db, user_id=1)
    mt = _make_managed(db, user_id=1, coin="BTC", status="open")
    mt_id = mt.id
    db.close()

    async def _noop_cancel(user_id, coin):
        pass

    monkeypatch.setattr(sync, "_cancel_leftover_orders", _noop_cancel)

    monkeypatch.setattr(hl_exec, "get_info", lambda testnet: _FakeInfo([]))
    asyncio.run(sync._reconcile_one_user(1, "0x12E3"))   # Strike 1 (transient leer)
    monkeypatch.setattr(hl_exec, "get_info", lambda testnet: _FakeInfo([("BTC", 0.5)]))
    asyncio.run(sync._reconcile_one_user(1, "0x12E3"))   # Position wieder da
    assert (1, "BTC", mt_id) not in sync._stale_counter
    db = SessionLocal()
    assert db.get(ManagedTrade, mt_id).status == "open"
    db.close()


# ── Sync: H-2 resting→open Fill-Verifikation via order_status ────────────────
class _FakeTraderOS:
    """Trader-Fake für H-2: order_status liefert den konfigurierten Status,
    cancel_order_oid + position_size sind no-ops (best-effort-Cleanup nach Flip)."""
    def __init__(self, status="filled", pos=0.0):
        self._status = status
        self._pos = pos
        self.order_status_calls = []
        self.cancel_oid_calls = []

    def order_status(self, oid=None, cloid=None):
        self.order_status_calls.append((oid, cloid))
        return {"status": self._status, "filled_sz": 0.5, "raw": {}}

    def cancel_order_oid(self, coin, oid):
        self.cancel_oid_calls.append((coin, oid))
        return {"ok": True, "already_filled": False}

    def position_size(self, coin):
        return self._pos


def _old_ts():
    """updated_at weit jenseits des Watcher-Grace-Fensters (300 + 2×60 = 420s)."""
    import datetime as _dt
    return _dt.datetime.utcnow() - _dt.timedelta(hours=1)


def test_h2_resting_flips_only_when_bot_entry_filled(monkeypatch):
    """H-2: resting-Row + HL-Position im Coin flippt NUR dann auf 'open', wenn
    trader.order_status den BOT-Entry als (teil)gefüllt bestätigt."""
    _reset_db()
    db = SessionLocal()
    _make_user(db, user_id=1)
    mt = _make_managed(db, user_id=1, coin="BTC", status="resting",
                       entry_oid="555", entry_cloid="0xabc", updated_at=_old_ts())
    mt_id = mt.id
    db.close()

    # HL meldet eine BTC-Position (kann Bot ODER manuell sein — order_status entscheidet).
    monkeypatch.setattr(hl_exec, "get_info", lambda testnet: _FakeInfo([("BTC", 0.5)]))
    fake = _FakeTraderOS(status="filled")

    async def _fake_build_trader(u):
        return fake

    monkeypatch.setattr(engine, "_build_trader", _fake_build_trader)

    asyncio.run(sync._reconcile_one_user(1, "0x12E3"))

    db = SessionLocal()
    assert db.get(ManagedTrade, mt_id).status == "open"   # verifizierter Bot-Fill → Flip
    db.close()
    # order_status wurde mit der Bot-Order-Kennung aufgerufen
    assert fake.order_status_calls == [("555", "0xabc")]
    # Partial-Rest wird ownership-bewusst per cancel_order_oid geräumt (nicht Sweep)
    assert fake.cancel_oid_calls == [("BTC", 555)]


def test_h2_no_flip_when_order_status_open(monkeypatch):
    """H-2 (Kern): HL hat eine Position im Coin, aber der BOT-Entry ist laut
    order_status noch 'open' (= NICHT gefüllt) → die Position gehört NICHT dem
    Bot (manuelle User-Position). Die resting-Row darf NICHT auf 'open' flippen,
    sonst hängt der Coverage-Reconciler den alten SL/TP an die fremde Position."""
    _reset_db()
    db = SessionLocal()
    _make_user(db, user_id=1)
    mt = _make_managed(db, user_id=1, coin="BTC", status="resting",
                       entry_oid="555", entry_cloid="0xabc", updated_at=_old_ts())
    mt_id = mt.id
    db.close()

    monkeypatch.setattr(hl_exec, "get_info", lambda testnet: _FakeInfo([("BTC", 0.5)]))
    fake = _FakeTraderOS(status="open")   # Bot-Entry ruht noch — kein Bot-Fill

    async def _fake_build_trader(u):
        return fake

    monkeypatch.setattr(engine, "_build_trader", _fake_build_trader)

    asyncio.run(sync._reconcile_one_user(1, "0x12E3"))

    db = SessionLocal()
    row = db.get(ManagedTrade, mt_id)
    acts = db.query(Activity).filter(Activity.user_id == 1).all()
    db.close()
    assert row.status == "resting"        # KEIN Flip
    assert fake.order_status_calls == [("555", "0xabc")]
    assert fake.cancel_oid_calls == []    # nichts geflippt → nichts zu canceln
    # keine "filled WITHOUT watcher"-Activity geschrieben
    assert all("WITHOUT an active fill-watcher" not in (a.text or "") for a in acts)


def test_h2_legacy_row_without_oid_uses_conservative_fallback(monkeypatch):
    """H-2 (dokumentierter Fallback): Alt-Row OHNE entry_oid/entry_cloid (und ohne
    resting_oid) kann nicht verifiziert werden → konservativ wie bisher flippen,
    damit das bestehende Self-Heal nicht still ausfällt."""
    _reset_db()
    db = SessionLocal()
    _make_user(db, user_id=1)
    mt = _make_managed(db, user_id=1, coin="BTC", status="resting",
                       resting_oid=None, entry_oid=None, entry_cloid=None,
                       updated_at=_old_ts())
    mt_id = mt.id
    db.close()

    monkeypatch.setattr(hl_exec, "get_info", lambda testnet: _FakeInfo([("BTC", 0.5)]))

    # _build_trader darf hier GAR NICHT gebraucht werden (kein verifizierbarer
    # Kandidat) — wenn doch aufgerufen, schlägt der Test fehl.
    async def _must_not_build(u):
        raise AssertionError("legacy fallback darf keinen Trader bauen")

    monkeypatch.setattr(engine, "_build_trader", _must_not_build)

    asyncio.run(sync._reconcile_one_user(1, "0x12E3"))

    db = SessionLocal()
    assert db.get(ManagedTrade, mt_id).status == "open"   # konservativer Flip
    db.close()


# ── Sync: LOW-5 _cancel_leftover_orders ─────────────────────────────────────
def test_low5_cancel_leftover_orders_guard_and_cancel(monkeypatch):
    """LOW-5: cancelt NUR wenn keine nicht-geschlossene Row mehr existiert
    (sonst würden die frischen Orders eines neuen Signals weggeräumt).
    C-4 (2026-06-13): zusätzlich nur sweepen, wenn HL den Coin als FLAT meldet —
    eine noch offene Position nach Bot-Close ist manuell und darf nicht sweepen."""
    _reset_db()
    db = SessionLocal()
    _make_user(db, user_id=1)
    db.close()

    cancels = []

    class _FakeTrader:
        def __init__(self, pos=0.0):
            self._pos = pos

        def position_size(self, coin):
            return self._pos

        def cancel_orders(self, coin):
            cancels.append(coin)
            return 2

    flat = {"pos": 0.0}

    async def _fake_build_trader(u):
        return _FakeTrader(pos=flat["pos"])

    monkeypatch.setattr(engine, "_build_trader", _fake_build_trader)

    async def _scenario():
        # Fall 1: neues Signal hat schon wieder eine resting-Row → KEIN Cancel
        db = SessionLocal()
        _make_managed(db, user_id=1, coin="BTC", status="resting")
        db.close()
        await sync._cancel_leftover_orders(1, "BTC")
        assert cancels == []

        # Rows alle closed für die nächsten zwei Fälle.
        db = SessionLocal()
        for mt in db.query(ManagedTrade).all():
            mt.status = "closed"
        db.commit()
        db.close()

        # Fall 2 (C-4): Row closed ABER HL zeigt noch Position → manuell → KEIN Sweep
        flat["pos"] = 0.5
        await sync._cancel_leftover_orders(1, "BTC")
        assert cancels == []

        # Fall 3: Row closed UND HL flat → verwaiste Bot-Trigger sweepen
        flat["pos"] = 0.0
        await sync._cancel_leftover_orders(1, "BTC")
        assert cancels == ["BTC"]

    asyncio.run(_scenario())


def test_low5_cancel_swallows_errors(monkeypatch):
    """LOW-5 ist best-effort: ein Trader-/HL-Fehler darf nie nach außen raisen
    (der Flip ist schon committed; Fallback bleibt der NEW_TRADE-Sweep)."""
    _reset_db()
    db = SessionLocal()
    _make_user(db, user_id=1)
    db.close()

    async def _broken_build_trader(u):
        raise RuntimeError("HL down")

    monkeypatch.setattr(engine, "_build_trader", _broken_build_trader)
    asyncio.run(sync._cancel_leftover_orders(1, "BTC"))   # darf nicht raisen


# ── Sync: Coverage-Reconciler-Wiring (Review #3 / Audit M-8) ────────────────
def test_position_sync_loop_runs_coverage_every_nth(monkeypatch):
    """Jede N-te Iteration ruft position_sync_loop den Coverage-Reconciler
    (engine.reconcile_stop_coverage) auf — der Drift 'live Position ohne/mit
    partieller SL-Deckung' wird damit kontinuierlich erkannt, nicht nur beim
    Prozess-Start."""
    calls = {"reconcile": 0, "coverage": 0, "loop_sleeps": 0}

    async def _fake_reconcile_all(*a, **k):
        calls["reconcile"] += 1

    async def _fake_coverage(*a, **k):
        calls["coverage"] += 1

    async def _fake_sleep(seconds, *a, **k):
        if seconds == sync.SYNC_INTERVAL_S:
            calls["loop_sleeps"] += 1
            if calls["loop_sleeps"] >= 4:
                raise asyncio.CancelledError()

    monkeypatch.setattr(sync, "_reconcile_all_users", _fake_reconcile_all)
    monkeypatch.setattr(engine, "reconcile_stop_coverage", _fake_coverage)
    monkeypatch.setattr(sync, "COVERAGE_RECONCILE_EVERY_N", 2)
    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(sync.position_sync_loop())

    assert calls["reconcile"] == 4
    assert calls["coverage"] == 2   # Iteration 2 und 4
