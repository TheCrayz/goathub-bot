"""Multi-User-Execution: ein Signal aus #signals -> Aktion auf JEDEM aktiven Nutzer-
Konto, je mit dessen Settings + Kapital-Cap + Builder-Code (Referral).

Aktionen (von Bot 1):
  NEW_TRADE     -> Position eröffnen (Entry + SL + TP)         [oder anpassen, falls schon offen]
  UPDATE_TRADE  -> bestehende Position anpassen (SL/TP nachziehen) [oder öffnen, falls noch nichts da]
  CANCEL_TRADE  -> Position schließen + alle Orders canceln
  HOLD          -> nichts tun

Schwere HL-Imports passieren lazy (via to_thread), damit das Modul ohne
hyperliquid-SDK importierbar bleibt.
"""
import asyncio
import json
import logging
import time

from app import config
from app.crypto import decrypt
from app.db import SessionLocal
from app.models import Activity, ManagedTrade, User
from app.parser import CANCEL_ACTIONS, parse_signal

log = logging.getLogger("goathub.engine")

# Laufende Tasks festhalten (sonst kann der GC sie mitten im Trade abräumen)
_tasks = set()

def _spawn(coro):
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
    return t

# Ein Lock pro (user_id, coin) -> verhindert Doppel-Position bei schnellen/doppelten Signalen
_locks = {}

def _lock_for(user_id, coin):
    k = (user_id, coin)
    lk = _locks.get(k)
    if lk is None:
        lk = asyncio.Lock()
        _locks[k] = lk
    return lk


def coin_of(t):
    return (t or "").split("/")[0].strip().upper()


def _log_activity(user_id, kind, text):
    db = SessionLocal()
    try:
        db.add(Activity(user_id=user_id, kind=kind, text=str(text)[:500]))
        db.commit()
    except Exception as e:
        log.warning("activity log failed: %s", e)
    finally:
        db.close()


def _save_managed(user_id, coin, sig, status, resting_oid=None):
    db = SessionLocal()
    try:
        mt = (db.query(ManagedTrade)
              .filter(ManagedTrade.user_id == user_id, ManagedTrade.coin == coin,
                      ManagedTrade.status != "closed")
              .order_by(ManagedTrade.id.desc()).first())
        if mt is None:
            mt = ManagedTrade(user_id=user_id, coin=coin)
            db.add(mt)
        if sig.direction:
            mt.direction = sig.direction
        if sig.entry is not None:
            mt.entry = sig.entry
        if sig.stop_loss is not None:
            mt.stop_loss = sig.stop_loss
        mt.take_profits = json.dumps([[tp.price, tp.percent] for tp in (sig.take_profits or [])])
        mt.status = status
        if resting_oid is not None:
            mt.resting_oid = str(resting_oid)
        if sig.signal_id:
            mt.signal_id = sig.signal_id
        db.commit()
    except Exception as e:
        log.warning("save managed: %s", e)
    finally:
        db.close()


def _close_managed(user_id, coin):
    db = SessionLocal()
    try:
        for mt in (db.query(ManagedTrade)
                   .filter(ManagedTrade.user_id == user_id, ManagedTrade.coin == coin,
                           ManagedTrade.status != "closed").all()):
            mt.status = "closed"
        db.commit()
    except Exception as e:
        log.warning("close managed: %s", e)
    finally:
        db.close()


def _builder():
    if config.BUILDER_ADDRESS:
        from app.hyperliquid_exec import fee_to_int
        return {"b": config.BUILDER_ADDRESS, "f": fee_to_int(config.BUILDER_FEE)}
    return None


async def _build_trader(u):
    from app.hyperliquid_exec import HyperliquidTrader
    builder = _builder() if u.builder_approved else None
    secret = decrypt(u.hl_api_secret_enc)
    return await asyncio.to_thread(
        lambda: HyperliquidTrader(secret_key=secret, account_address=u.hl_account_address,
                                  testnet=config.HL_TESTNET, builder=builder))


def _get_user(user_id):
    db = SessionLocal()
    try:
        return db.get(User, user_id)
    finally:
        db.close()


# ── Signal-Eingang ───────────────────────────────────────────────────────────
async def handle_signal(embed: dict):
    sig = parse_signal(embed)
    if sig is None:
        return
    action = sig.action.upper()
    if action == "HOLD":
        return
    is_cancel = action in CANCEL_ACTIONS
    is_entry = action in ("NEW_TRADE", "UPDATE_TRADE")
    if not is_cancel and not is_entry:
        return
    # Confidence-Gate nur für Einstiege (NEW/UPDATE)
    if is_entry and sig.confidence is not None and sig.confidence < config.MIN_CONFIDENCE:
        return

    db = SessionLocal()
    try:
        users = (db.query(User)
                 .filter(User.bot_active.is_(True), User.hl_api_secret_enc != "")
                 .all())
        user_ids = [u.id for u in users]
    finally:
        db.close()

    coin = coin_of(sig.ticker)
    log.info("Signal %s %s %s -> %d aktive Nutzer", action, sig.direction, coin, len(user_ids))
    for uid in user_ids:
        if is_cancel:
            _spawn(_cancel(uid, sig))
        else:
            _spawn(_open_or_update(uid, sig))


# ── NEW_TRADE / UPDATE_TRADE ─────────────────────────────────────────────────
async def _open_or_update(user_id, sig):
    u = _get_user(user_id)
    if not u:
        return
    coin = coin_of(sig.ticker)
    async with _lock_for(user_id, coin):                  # serialisiert gleiche (User,Coin)
        try:
            trader = await _build_trader(u)
            if not trader.is_tradable(coin):
                _log_activity(user_id, "skip", f"{coin}: nicht auf Hyperliquid handelbar — übersprungen")
                return
            pos = await asyncio.to_thread(trader.position_size, coin)
            if abs(pos) > 0:
                await _adjust(trader, u, sig, pos)        # Position offen -> SL/TP nachziehen
            else:
                await asyncio.to_thread(trader.cancel_orders, coin)   # evtl. alte Ruhe-Order weg
                await _open_new(trader, u, sig)           # frisch eröffnen
        except Exception as e:
            log.exception("user %s %s: %s", user_id, coin, e)
            _log_activity(user_id, "error", f"{coin}: {e}")


async def _open_new(trader, u, sig):
    from app.sizing import size_trade
    coin = coin_of(sig.ticker)
    balance = await asyncio.to_thread(trader.account_value)
    if balance <= 0:
        _log_activity(u.id, "skip", f"{coin}: kein handelbares Guthaben")
        return
    open_pos = await asyncio.to_thread(trader.open_positions_count)
    if open_pos >= u.max_open_positions:
        _log_activity(u.id, "skip", f"{coin}: max. Positionen ({open_pos}/{u.max_open_positions})")
        return
    plan = size_trade(account_value=balance, capital_cap=u.capital_cap_usdc, risk_pct=u.risk_pct,
                      entry=sig.entry, stop_loss=sig.stop_loss, leverage=u.leverage)
    if plan.notional < config.MIN_NOTIONAL_USDC:
        _log_activity(u.id, "skip", f"{coin}: Notional {plan.notional:.2f} < {config.MIN_NOTIONAL_USDC}")
        return

    is_buy = (sig.direction == "LONG")
    tps = [(tp.price, tp.percent / 100.0) for tp in sig.take_profits]
    await asyncio.to_thread(trader.set_leverage, coin, u.leverage)
    entry = await asyncio.to_thread(trader.place_entry, coin, is_buy, plan.qty, sig.entry)
    if not entry["ok"]:
        _log_activity(u.id, "error", f"Entry-Fehler {coin}: {entry.get('error')}")
        return

    if entry["filled"]:
        sz = entry["filled_sz"] or plan.qty
        prot = await asyncio.to_thread(trader.place_protection, coin, is_buy, sz, sig.stop_loss, tps)
        if not prot.get("sl_ok"):
            # SL konnte NICHT gesetzt werden -> keine ungeschützte Position riskieren -> sofort schließen
            await asyncio.to_thread(trader.close_position, coin)
            await asyncio.to_thread(trader.cancel_orders, coin)
            _log_activity(u.id, "error", f"{coin}: Stop-Loss fehlgeschlagen — Position sofort geschlossen (kein ungeschützter Trade)")
            return
        _log_activity(u.id, "order", f"{sig.direction} {coin} eröffnet (qty {sz:.6g}), SL+TP gesetzt")
        _save_managed(u.id, coin, sig, status="open")
    else:
        _log_activity(u.id, "order", f"{sig.direction} {coin}: Limit ruht @ {sig.entry}, warte auf Fill")
        _save_managed(u.id, coin, sig, status="resting", resting_oid=entry.get("resting_oid"))
        _spawn(_protect_when_filled(trader, u.id, sig, is_buy, tps))


async def _adjust(trader, u, sig, pos):
    """These hat sich geändert, Position ist offen -> SL/TP auf neue Level nachziehen."""
    coin = coin_of(sig.ticker)
    is_buy = pos > 0
    if sig.stop_loss is None:
        # Update ohne neuen SL -> bestehende Schutz-Orders NICHT anfassen (nie ungeschützt lassen)
        _log_activity(u.id, "update", f"{coin}: Update ohne neuen SL — bestehender Schutz bleibt")
        _save_managed(u.id, coin, sig, status="open")
        return
    tps = [(tp.price, tp.percent / 100.0) for tp in sig.take_profits]
    await asyncio.to_thread(trader.cancel_orders, coin)            # alte SL/TP weg
    prot = await asyncio.to_thread(trader.place_protection, coin, is_buy, abs(pos), sig.stop_loss, tps)
    if not prot.get("sl_ok"):
        await asyncio.to_thread(trader.close_position, coin)
        await asyncio.to_thread(trader.cancel_orders, coin)
        _log_activity(u.id, "error", f"{coin}: SL beim Update fehlgeschlagen — Position geschlossen (kein ungeschützter Trade)")
        return
    _log_activity(u.id, "update", f"{coin}: These angepasst — SL {sig.stop_loss}, TP nachgezogen (qty {abs(pos):.6g})")
    _save_managed(u.id, coin, sig, status="open")


# ── CANCEL_TRADE ─────────────────────────────────────────────────────────────
async def _cancel(user_id, sig):
    u = _get_user(user_id)
    if not u:
        return
    coin = coin_of(sig.ticker)
    async with _lock_for(user_id, coin):
        try:
            trader = await _build_trader(u)
            await asyncio.to_thread(trader.cancel_orders, coin)
            pos = await asyncio.to_thread(trader.position_size, coin)
            if abs(pos) > 0:
                res = await asyncio.to_thread(trader.close_position, coin)
                if res.get("ok"):
                    _log_activity(user_id, "close", f"{coin}: geschlossen — These invalidiert (qty {abs(pos):.6g})")
                else:
                    _log_activity(user_id, "error", f"{coin}: Schließen fehlgeschlagen — {res.get('error')}")
            else:
                _log_activity(user_id, "close", f"{coin}: Order gecancelt — These invalidiert")
            _close_managed(user_id, coin)
        except Exception as e:
            log.exception("cancel user %s %s: %s", user_id, coin, e)
            _log_activity(user_id, "error", f"{coin} cancel: {e}")


# ── Fill-Watch für ruhende Limit-Orders ──────────────────────────────────────
async def _protect_when_filled(trader, user_id, sig, is_buy, tps):
    coin = coin_of(sig.ticker)
    deadline = time.monotonic() + config.ENTRY_FILL_TIMEOUT_S
    while time.monotonic() < deadline:
        await asyncio.sleep(config.ENTRY_POLL_S)
        try:
            psz = await asyncio.to_thread(trader.position_size, coin)
        except Exception:
            continue
        if abs(psz) > 0:
            prot = await asyncio.to_thread(trader.place_protection, coin, is_buy, abs(psz), sig.stop_loss, tps)
            if not prot.get("sl_ok"):
                await asyncio.to_thread(trader.close_position, coin)
                await asyncio.to_thread(trader.cancel_orders, coin)
                _log_activity(user_id, "error", f"{coin}: SL nach Fill fehlgeschlagen — Position geschlossen")
                _close_managed(user_id, coin)
                return
            _log_activity(user_id, "order", f"{coin} gefüllt (qty {abs(psz):.6g}) — SL+TP gesetzt")
            _save_managed(user_id, coin, sig, status="open")
            return
    await asyncio.to_thread(trader.cancel_orders, coin)
    _log_activity(user_id, "skip", f"{coin} nicht gefüllt — kein Trade")
    _close_managed(user_id, coin)
