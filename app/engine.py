"""Multi-User-Execution: ein Signal aus #signals -> Order auf JEDEM aktiven Nutzer-
Konto, je mit dessen Settings + Kapital-Cap + Builder-Code (Referral).

Schwere HL-Imports passieren lazy in _run_user (via to_thread), damit das Modul
ohne hyperliquid-SDK importierbar bleibt.
"""
import asyncio
import logging
import time

from app import config
from app.crypto import decrypt
from app.db import SessionLocal
from app.models import Activity, User
from app.parser import parse_signal

log = logging.getLogger("goathub.engine")


def _log_activity(user_id, kind, text):
    db = SessionLocal()
    try:
        db.add(Activity(user_id=user_id, kind=kind, text=str(text)[:500]))
        db.commit()
    except Exception as e:
        log.warning("activity log failed: %s", e)
    finally:
        db.close()


def _builder():
    if config.BUILDER_ADDRESS:
        from app.hyperliquid_exec import fee_to_int
        return {"b": config.BUILDER_ADDRESS, "f": fee_to_int(config.BUILDER_FEE)}
    return None


async def handle_signal(embed: dict):
    sig = parse_signal(embed)
    if sig is None or sig.action != "NEW_TRADE":
        return
    if sig.confidence is not None and sig.confidence < config.MIN_CONFIDENCE:
        return

    db = SessionLocal()
    try:
        users = (db.query(User)
                 .filter(User.bot_active.is_(True), User.hl_api_secret_enc != "")
                 .all())
        user_ids = [u.id for u in users]
    finally:
        db.close()

    log.info("Signal %s %s -> %d aktive Nutzer", sig.direction, sig.ticker, len(user_ids))
    for uid in user_ids:
        asyncio.create_task(_run_user(uid, sig))


async def _run_user(user_id, sig):
    db = SessionLocal()
    try:
        u = db.get(User, user_id)
    finally:
        db.close()
    if not u:
        return
    try:
        from app.hyperliquid_exec import HyperliquidTrader
        builder = _builder() if u.builder_approved else None
        secret = decrypt(u.hl_api_secret_enc)
        trader = await asyncio.to_thread(
            lambda: HyperliquidTrader(secret_key=secret, account_address=u.hl_account_address,
                                      testnet=config.HL_TESTNET, builder=builder))

        balance = await asyncio.to_thread(trader.account_value)
        if balance <= 0:
            _log_activity(user_id, "skip", "kein handelbares Guthaben")
            return
        open_pos = await asyncio.to_thread(trader.open_positions_count)
        if open_pos >= u.max_open_positions:
            _log_activity(user_id, "skip", f"max. Positionen ({open_pos}/{u.max_open_positions})")
            return

        from app.sizing import size_trade
        plan = size_trade(account_value=balance, capital_cap=u.capital_cap_usdc,
                          risk_pct=u.risk_pct, entry=sig.entry, stop_loss=sig.stop_loss,
                          leverage=u.leverage)
        if plan.notional < config.MIN_NOTIONAL_USDC:
            _log_activity(user_id, "skip", f"Notional {plan.notional:.2f} < {config.MIN_NOTIONAL_USDC}")
            return

        is_buy = (sig.direction == "LONG")
        tps = [(tp.price, tp.percent / 100.0) for tp in sig.take_profits]
        await asyncio.to_thread(trader.set_leverage, sig.ticker, u.leverage)
        entry = await asyncio.to_thread(trader.place_entry, sig.ticker, is_buy, plan.qty, sig.entry)
        if not entry["ok"]:
            _log_activity(user_id, "error", f"Entry-Fehler {sig.ticker}: {entry.get('error')}")
            return

        if entry["filled"]:
            await asyncio.to_thread(trader.place_protection, sig.ticker, is_buy,
                                    entry["filled_sz"] or plan.qty, sig.stop_loss, tps)
            _log_activity(user_id, "order",
                          f"{sig.direction} {sig.ticker} eröffnet (qty {entry['filled_sz'] or plan.qty:.6g}), SL+TP gesetzt")
        else:
            _log_activity(user_id, "order", f"{sig.direction} {sig.ticker}: Limit ruht @ {sig.entry}, warte auf Fill")
            asyncio.create_task(_protect_when_filled(trader, user_id, sig, is_buy, tps, entry.get("resting_oid")))
    except Exception as e:
        log.exception("user %s: %s", user_id, e)
        _log_activity(user_id, "error", str(e))


async def _protect_when_filled(trader, user_id, sig, is_buy, tps, oid):
    deadline = time.monotonic() + config.ENTRY_FILL_TIMEOUT_S
    while time.monotonic() < deadline:
        await asyncio.sleep(config.ENTRY_POLL_S)
        try:
            psz = await asyncio.to_thread(trader.position_size, sig.ticker)
        except Exception:
            continue
        if abs(psz) > 0:
            await asyncio.to_thread(trader.place_protection, sig.ticker, is_buy, abs(psz), sig.stop_loss, tps)
            _log_activity(user_id, "order", f"{sig.ticker} gefüllt (qty {abs(psz):.6g}) — SL+TP gesetzt")
            return
    _log_activity(user_id, "skip", f"{sig.ticker} nicht gefüllt — kein Trade")
