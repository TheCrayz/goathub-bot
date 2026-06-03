"""Background-Loop: managed_trades gegen live Hyperliquid-State reconcilen.

Phase 6+ (2026-06-03): Fix für den Stale-Row-Bug, der am Morgen des 2026-06-03
entdeckt wurde (SOL row id=24 blieb 5h `open` nachdem HL die Position autonom
via SL-Trigger geschlossen hatte). Vorher merkte goathub die Realität erst, wenn
das nächste Signal für dieses Coin durchkam und `position_size(coin)` 0 zurückgab.

Pollt alle SYNC_INTERVAL_S Sekunden für jeden verbundenen User die HL-Positions
und schließt managed_trades, die HL nicht mehr kennt (autonom via SL/TP exited).

Symmetrisch zum signal-bot's eigenem sync.py, aber für HL statt MEXC.
"""
import asyncio
import logging
import os

from app import config
from app.db import SessionLocal
from app.models import Activity, ManagedTrade, User

log = logging.getLogger("goathub.sync")

SYNC_INTERVAL_S = int(os.getenv("POSITION_SYNC_INTERVAL_S", "60"))


async def position_sync_loop():
    """Endlosschleife — startet im lifespan() neben dem Discord-Listener."""
    log.info("position_sync_loop started (interval=%ds)", SYNC_INTERVAL_S)
    # Beim ersten Boot 10 s warten, damit andere Init-Tasks (DB, Discord) durch sind.
    await asyncio.sleep(10)
    while True:
        try:
            await _reconcile_all_users()
        except Exception as e:
            log.exception("position-sync iteration failed: %s", e)
        await asyncio.sleep(SYNC_INTERVAL_S)


async def _reconcile_all_users():
    """One full reconcile pass over all users with at least one open managed_trade."""
    db = SessionLocal()
    try:
        # Effizient: nur User, die a) Wallet verbunden haben, b) >=1 non-closed managed_trade
        user_ids_with_open = {
            row[0] for row in
            db.query(ManagedTrade.user_id).filter(ManagedTrade.status != "closed").distinct().all()
        }
        users = (
            db.query(User)
              .filter(User.id.in_(user_ids_with_open) if user_ids_with_open else False,
                      User.hl_account_address != "")
              .all()
        )
    finally:
        db.close()

    if not users:
        return
    log.debug("position-sync: checking %d user(s)", len(users))
    for u in users:
        try:
            await _reconcile_one_user(u.id, u.hl_account_address)
        except Exception as e:
            log.warning("position-sync user %d failed: %s", u.id, e)


async def _reconcile_one_user(user_id: int, address: str):
    """Reconcile open managed_trades of ONE user gegen HL-Positions."""
    # Snapshot von DB
    db = SessionLocal()
    try:
        open_mts = (
            db.query(ManagedTrade)
              .filter(ManagedTrade.user_id == user_id, ManagedTrade.status != "closed")
              .all()
        )
        # Detach: wir lesen nur, schreiben mit fresh-query unten
        open_coins = {(mt.id, mt.coin) for mt in open_mts}
    finally:
        db.close()

    if not open_coins:
        return

    # HL Info API — read-only, kein Decrypt nötig
    try:
        from hyperliquid.info import Info
        from hyperliquid.utils import constants
        url = constants.TESTNET_API_URL if config.HL_TESTNET else constants.MAINNET_API_URL
        info = Info(url, skip_ws=True)
        state = await asyncio.to_thread(info.user_state, address)
    except Exception as e:
        log.warning("HL fetch failed for user %d: %s", user_id, e)
        return

    # HL-Positions als coin → abs(size) Map
    hl_positions: dict[str, float] = {}
    for p in state.get("assetPositions", []):
        pos = p.get("position", {})
        coin = pos.get("coin")
        try:
            sz = abs(float(pos.get("szi", 0) or 0))
        except (TypeError, ValueError):
            sz = 0.0
        if coin and sz > 0:
            hl_positions[coin] = sz

    # Jede open managed_trade prüfen
    db = SessionLocal()
    try:
        closed_count = 0
        for mt_id, coin in open_coins:
            mt = db.get(ManagedTrade, mt_id)
            if mt is None or mt.status == "closed":
                continue
            if coin in hl_positions:
                continue  # HL hat sie noch → ok
            # HL hat sie NICHT mehr aber DB sagt open → reconcile
            mt.status = "closed"
            db.add(Activity(
                user_id=user_id,
                kind="close",
                text=(f"{coin}: autonom auf HL geschlossen — Position-Sync hat stale DB-Status "
                      f"(managed_trade id={mt_id}) auf 'closed' angepasst"),
            ))
            closed_count += 1
        if closed_count > 0:
            db.commit()
            log.info("position-sync user=%d: %d stale managed_trade(s) auf 'closed' geflippt",
                     user_id, closed_count)
    finally:
        db.close()
