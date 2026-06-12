"""Background-Loop: managed_trades gegen live Hyperliquid-State reconcilen.

Phase 6+ (2026-06-03): Fix für den Stale-Row-Bug, der am Morgen des 2026-06-03
entdeckt wurde (SOL row id=24 blieb 5h `open` nachdem HL die Position autonom
via SL-Trigger geschlossen hatte). Vorher merkte goathub die Realität erst, wenn
das nächste Signal für dieses Coin durchkam und `position_size(coin)` 0 zurückgab.

Pollt alle SYNC_INTERVAL_S Sekunden für jeden verbundenen User die HL-Positions
und schließt managed_trades, die HL nicht mehr kennt (autonom via SL/TP exited).

Symmetrisch zum signal-bot's eigenem sync.py, aber für HL statt MEXC.

2026-06-12 Audit LOW-5: Flip auf 'closed' cancelt jetzt best-effort die
verwaisten TP/SL-Trigger-Orders des Coins auf HL — vorher blieben die bis zum
cancel_orders-Sweep des nächsten NEW_TRADE liegen (Orderbuch-/UI-Müll, und ein
liegen gebliebener reduce-only-Stop hätte eine SPÄTERE Position desselben Coins
am falschen Level anschneiden können).
"""
import asyncio
import datetime
import logging
import os

from app import config
from app.db import SessionLocal
from app.models import Activity, ManagedTrade, User

log = logging.getLogger("goathub.sync")

SYNC_INTERVAL_S = int(os.getenv("POSITION_SYNC_INTERVAL_S", "60"))
# 2026-06-12 (Review #3): jede N-te Sync-Iteration läuft zusätzlich der
# Stop-Coverage-Reconciler (engine.reconcile_stop_coverage). Vorher lief der
# NUR beim Prozess-Start — ein per Gap durch den Slippage-Cap konsumierter
# Stop ließ die Position bis zum nächsten Deploy nackt. 5 × 60s = alle ~5 min.
COVERAGE_RECONCILE_EVERY_N = max(1, int(os.getenv("STOP_COVERAGE_RECONCILE_EVERY_N", "5")))

# 2026-06-04 audit-fix (B-#5): 2-strike-rule gegen API-Latency-false-positives.
# Wenn HL einmalig wegen Timeout/Partial-Response leeren assetPositions returnt,
# darf das KEIN managed_trade auf 'closed' flippen — sonst macht der nächste
# Signal-Trade-Cycle eine doppelte Position. Wir merken uns pro (user_id, coin,
# mt_id) wieviele aufeinanderfolgende Runs HL die Position als weg gemeldet hat
# und flippen erst nach _STALE_STRIKES_NEEDED Runs hintereinander.
_STALE_STRIKES_NEEDED = int(os.getenv("POSITION_SYNC_STRIKES", "2"))
_stale_counter: dict[tuple[int, str, int], int] = {}


def clear_strikes(user_id, coin):
    """H-6 (2026-06-12): alle Stale-Strikes für (user, coin) löschen. Wird von der
    Engine aufgerufen, wenn sie eine NEUE Position öffnet — die managed_trade-Row
    wird evtl. wiederverwendet (gleiche mt_id), und ein getragener Strike der ALTEN
    Positions-Generation würde sonst (mit einem einzigen transient-leeren HL-Read)
    die neue, echt-live Position fälschlich auf 'closed' flippen."""
    for k in [k for k in _stale_counter if k[0] == user_id and k[1] == coin]:
        _stale_counter.pop(k, None)


async def position_sync_loop():
    """Endlosschleife — startet im lifespan() neben dem Discord-Listener."""
    log.info("position_sync_loop started (interval=%ds, coverage every %d runs)",
             SYNC_INTERVAL_S, COVERAGE_RECONCILE_EVERY_N)
    # Beim ersten Boot 10 s warten, damit andere Init-Tasks (DB, Discord) durch sind.
    await asyncio.sleep(10)
    iteration = 0
    while True:
        try:
            await _reconcile_all_users()
        except Exception as e:
            log.exception("position-sync iteration failed: %s", e)
        # 2026-06-12 (Review #3): periodischer Stop-Coverage-Check (Unter-Deckung
        # nach SL-Gap-Through / nackt gefüllten Entry-Resten). Lazy-Import,
        # damit sync.py ohne hyperliquid-SDK importierbar bleibt.
        iteration += 1
        if iteration % COVERAGE_RECONCILE_EVERY_N == 0:
            try:
                from app.engine import reconcile_stop_coverage
                await reconcile_stop_coverage()
            except Exception as e:
                log.exception("stop-coverage reconcile failed: %s", e)
        await asyncio.sleep(SYNC_INTERVAL_S)


async def _reconcile_all_users():
    """One full reconcile pass over all users with at least one open managed_trade.

    2026-06-08 bug-fix: Reconcile ist nur für status='open'. Trades mit
    status='resting' (Limit-Order wartet auf Fill) haben naturgemäß NOCH
    keine HL-Position — die werden vom fill-watch-Loop in engine._protect_when_filled
    verwaltet. Wenn sync auch resting prüft, sieht es 'HL hat nichts' → flippt
    nach 2 strikes fälschlich zu 'closed', noch BEVOR die Limit-Order
    gefüllt werden konnte. Konkret beobachtet: user 5 SUI 2026-06-08 08:09
    → 08:11 (2 strikes × 60s) sync-killed während fill-watch noch lief.
    """
    db = SessionLocal()
    try:
        # Close-Logik bleibt nur für status='open'. 2026-06-12 (Review #1): User
        # mit resting-Rows werden jetzt MIT geprüft — aber nur für die
        # Fill-Detection (resting-Row + HL-Position = Limit hat gefüllt, während
        # kein Watcher mehr lebte, z.B. nach Deploy-Restart), NIE für Strikes.
        user_ids_with_open = {
            row[0] for row in
            db.query(ManagedTrade.user_id)
              .filter(ManagedTrade.status.in_(("open", "resting"))).distinct().all()
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
        mts = (
            db.query(ManagedTrade)
              .filter(ManagedTrade.user_id == user_id,
                      ManagedTrade.status.in_(("open", "resting")))
              .all()
        )
        # Detach: wir lesen nur, schreiben mit fresh-query unten
        open_coins = {(mt.id, mt.coin) for mt in mts if mt.status == "open"}
        # 2026-06-12 (Review #1): resting-Rows für die Fill-Detection mitnehmen
        # (updated_at für den Watcher-Grace-Timer).
        resting_rows = [(mt.id, mt.coin, mt.updated_at) for mt in mts if mt.status == "resting"]
    finally:
        db.close()

    if not open_coins and not resting_rows:
        return

    # HL Info API — read-only, kein Decrypt nötig
    try:
        from app.hyperliquid_exec import get_info
        info = get_info(config.HL_TESTNET)
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

    # 2026-06-12 (Review #41): bevor eine open-Row einen Strike Richtung 'closed'
    # bekommt, ein zweiter, ge-retry-ter Confirm-Read. Ein degraded-API-Fenster
    # konnte vorher mit 2 transienten Leer-Antworten (2 Strikes × 60s) eine LIVE
    # Position auf 'closed' flippen — das nächste UPDATE fand keine offene Row,
    # der SL-Ratchet war aus und ein gelockerter SL ging durch. Schlägt der
    # Confirm-Read fehl → diese Runde GAR keine Strikes (fail-safe).
    strikes_allowed = True
    if any(coin not in hl_positions for (_mt_id, coin) in open_coins):
        try:
            from app.hl_retry import hl_retry
            state2 = await asyncio.to_thread(
                lambda: hl_retry(lambda: info.user_state(address),
                                 max_attempts=2, initial_delay=1.0,
                                 label=f"sync-confirm u{user_id}"))
            for p in (state2 or {}).get("assetPositions", []):
                pos2 = p.get("position", {})
                coin2 = pos2.get("coin")
                try:
                    sz2 = abs(float(pos2.get("szi", 0) or 0))
                except (TypeError, ValueError):
                    sz2 = 0.0
                if coin2 and sz2 > 0:
                    hl_positions[coin2] = sz2
        except Exception as e:
            strikes_allowed = False
            log.warning("position-sync confirm-read failed user %d: %s — keine Strikes diese Runde",
                        user_id, e)

    # 2026-06-12 (Review #1): resting-Rows mit real existierender HL-Position =
    # Limit-Order hat gefüllt, aber kein Fill-Watcher hat sie auf 'open' geflippt
    # (Watcher starb z.B. beim Deploy-Restart). Grace-Periode: erst handeln, wenn
    # sicher KEIN Watcher mehr leben kann (updated_at älter als Watcher-Lifetime),
    # sonst Doppel-Schutz-Race gegen den lebenden Watcher.
    grace = datetime.timedelta(
        seconds=int(getattr(config, "ENTRY_FILL_TIMEOUT_S", 300)) + 2 * SYNC_INTERVAL_S)
    now = datetime.datetime.utcnow()
    filled_resting = [
        (mt_id, coin) for (mt_id, coin, updated_at) in resting_rows
        if coin in hl_positions and (updated_at is None or now - updated_at > grace)
    ]

    # Jede open managed_trade prüfen
    db = SessionLocal()
    flipped_coins = []     # LOW-5: Coins, deren Row hier auf 'closed' geflippt wurde
    resting_cancels = []   # 2026-06-13 Review-Fix: (coin, oid) der auf 'open' geflippten Rows
    try:
        closed_count = 0
        reopened_count = 0
        for mt_id, coin in filled_resting:
            mt = db.get(ManagedTrade, mt_id)
            if mt is None or mt.status != "resting":
                continue
            mt.status = "open"
            # 2026-06-13 Review-Fix: der Limit-Entry kann PARTIAL gefüllt haben —
            # der Rest der Order liegt dann noch im Buch und würde später ohne
            # Watcher/Schutz nachfüllen. Oid merken und nach dem Commit
            # best-effort canceln (gleiches Muster wie LOW-5).
            if mt.resting_oid:
                resting_cancels.append((coin, mt.resting_oid))
            db.add(Activity(
                user_id=user_id,
                kind="error",
                text=(f"{coin}: resting limit entry filled WITHOUT an active fill-watcher "
                      f"(HL position present, managed_trade id={mt_id}) — row set to 'open'; "
                      f"coverage-reconciler adds the missing protection automatically."),
            ))
            reopened_count += 1
        if not strikes_allowed:
            if reopened_count > 0:
                db.commit()
            # 2026-06-13 Review-Fix: NICHT mehr early-return — die resting_cancels
            # unten müssen auch in diesem Pfad laufen. Strike-Logik überspringen.
            open_coins = []
        for mt_id, coin in open_coins:
            mt = db.get(ManagedTrade, mt_id)
            if mt is None or mt.status == "closed":
                # Counter aufräumen — diese Position interessiert uns nicht mehr.
                _stale_counter.pop((user_id, coin, mt_id), None)
                continue
            if coin in hl_positions:
                # HL hat sie wieder → strike-counter reset
                _stale_counter.pop((user_id, coin, mt_id), None)
                continue
            # HL hat sie NICHT — counter erhöhen, aber erst nach N Strikes wirklich closen
            key = (user_id, coin, mt_id)
            strikes = _stale_counter.get(key, 0) + 1
            _stale_counter[key] = strikes
            if strikes < _STALE_STRIKES_NEEDED:
                log.debug("position-sync user=%d coin=%s mt=%d: strike %d/%d, warte",
                          user_id, coin, mt_id, strikes, _STALE_STRIKES_NEEDED)
                continue
            # N-te leere Antwort hintereinander → tatsächlich geschlossen
            mt.status = "closed"
            db.add(Activity(
                user_id=user_id,
                kind="close",
                text=(f"{coin}: closed autonomously on HL — position-sync adjusted stale DB status "
                      f"(managed_trade id={mt_id}, {strikes} strikes) to 'closed'"),
            ))
            _stale_counter.pop(key, None)
            closed_count += 1
            flipped_coins.append(coin)
        if closed_count > 0 or reopened_count > 0:
            db.commit()
            log.info("position-sync user=%d: %d stale auf 'closed', %d resting auf 'open' geflippt",
                     user_id, closed_count, reopened_count)
    finally:
        db.close()

    # LOW-5 (2026-06-12): verwaiste TP/SL-Trigger-Orders der geflippten Coins
    # auf HL canceln. Best-effort NACH dem Commit — ein Cancel-Fehler ändert
    # nichts am (korrekten) Flip; der nächste NEW_TRADE sweept dann wie bisher.
    for coin in flipped_coins:
        await _cancel_leftover_orders(user_id, coin)

    # 2026-06-13 Review-Fix: Rest einer (partial gefüllten) Limit-Entry-Order
    # der auf 'open' geflippten Rows canceln — sonst füllt der Order-Rest später
    # ohne Watcher/Schutz nach. Best-effort wie LOW-5.
    for coin, oid in resting_cancels:
        await _cancel_resting_remainder(user_id, coin, oid)


async def _cancel_leftover_orders(user_id: int, coin: str):
    """LOW-5 (2026-06-12): TP/SL-Trigger-Orders eines gerade auf 'closed'
    geflippten Coins von HL räumen. Braucht (anders als der Read-Pfad oben)
    den vollen Trader, weil cancel eine SIGNIERTE Exchange-Action ist →
    engine._build_trader (lazy import, wie engine selbst clear_strikes lazy
    importiert — kein Modul-Zyklus).

    Race-Schutz: läuft unter engine._lock_for(user, coin) — dasselbe Lock wie
    alle Trade-Pfade — und re-checkt NACH Lock-Erwerb, ob inzwischen ein neues
    Signal eine nicht-geschlossene managed_trade-Row angelegt hat. Wenn ja:
    Finger weg, sonst würden wir die frischen Entry-/Schutz-Orders des neuen
    Trades wegräumen. Best-effort: jeder Fehler wird nur geloggt, der Flip
    bleibt gültig (Fallback = NEW_TRADE-Sweep wie vor diesem Fix)."""
    try:
        from app.engine import _build_trader, _get_user, _lock_for
        u = _get_user(user_id)
        if u is None or not u.hl_api_secret_enc:
            return
        async with _lock_for(user_id, coin):
            db = SessionLocal()
            try:
                live = (db.query(ManagedTrade)
                        .filter(ManagedTrade.user_id == user_id, ManagedTrade.coin == coin,
                                ManagedTrade.status != "closed").first())
            finally:
                db.close()
            if live is not None:
                return  # neues Signal hat den Coin schon übernommen — nicht anfassen
            trader = await _build_trader(u)
            n = await asyncio.to_thread(trader.cancel_orders, coin)
            if n:
                log.info("position-sync user=%d coin=%s: %d verwaiste Order(s) nach Flip gecancelt",
                         user_id, coin, n)
    except Exception as e:
        log.warning("leftover-order-cancel user=%d coin=%s failed: %s", user_id, coin, e)


async def _cancel_resting_remainder(user_id: int, coin: str, oid):
    """2026-06-13 Review-Fix: einzelne (Rest-)Limit-Entry-Order per oid canceln,
    nachdem ihre resting-Row auf 'open' geflippt wurde. Anders als
    _cancel_leftover_orders KEIN Sweep über alle Orders des Coins — die Position
    ist ja live und der Coverage-Reconciler legt gleich SL/TP nach, die wir
    nicht wegräumen dürfen. Best-effort: Fehler nur loggen."""
    try:
        from app.engine import _build_trader, _get_user, _lock_for
        u = _get_user(user_id)
        if u is None or not u.hl_api_secret_enc:
            return
        async with _lock_for(user_id, coin):
            trader = await _build_trader(u)
            await asyncio.to_thread(trader.cancel_order, coin, int(oid))
            log.info("position-sync user=%d coin=%s: Rest der Limit-Entry-Order %s gecancelt",
                     user_id, coin, oid)
    except Exception as e:
        log.warning("resting-remainder-cancel user=%d coin=%s oid=%s failed: %s",
                    user_id, coin, oid, e)
