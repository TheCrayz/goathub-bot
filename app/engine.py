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

# Phase 2 #27 (2026-06-02): per-coin performance cache. {(addr,coin) → (ts, stats)}.
_percoin_cache: dict = {}

log = logging.getLogger("goathub.engine")

# Laufende Tasks festhalten (sonst kann der GC sie mitten im Trade abräumen)
_tasks = set()

def _spawn(coro):
    t = asyncio.create_task(coro)
    _tasks.add(t)
    def _on_done(task):
        _tasks.discard(task)
        if not task.cancelled():
            exc = task.exception()
            # 2026-06-04 audit-fix: vorher wurden Exceptions in spawn'd Tasks
            # schweigend verschluckt; das hat versteckte Trade-Verluste möglich
            # gemacht. Jetzt landen sie als ERROR im Log mit Stacktrace.
            if exc is not None:
                log.error("spawned task crashed: %r", exc, exc_info=exc)
    t.add_done_callback(_on_done)
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
        # Activity-Verlust ist Error-Level: alle Trader/Admin-Beobachtung läuft
        # über Activities (audit log). Wenn das hier failt, fliegt die ganze
        # Observability blind.
        log.error("activity log failed: %s", e)
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
        # 2026-06-04 audit-fix: TP-Liste NUR überschreiben wenn das Signal
        # tatsächlich TPs enthält. Vorher hat ein UPDATE_TRADE-Signal ohne TPs
        # (z. B. nur SL-Adjust) die DB-Spalte auf "[]" gesetzt und damit die
        # ursprüngliche TP-Historie verworfen.
        if sig.take_profits:
            mt.take_profits = json.dumps([[tp.price, tp.percent] for tp in sig.take_profits])
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


def _get_current_sl(user_id, coin):
    """Aktueller SL + Direction aus dem letzten offenen managed_trade.

    Returns (sl, direction) tuple, oder (None, None) wenn kein offener Trade.

    2026-06-04 audit-fix (B-#1): vorher nur SL ohne Direction. Wenn der DB-Eintrag
    noch ein altes LONG ist und HL inzwischen SHORT zeigt (z. B. nach manuellem
    Re-Open des Users), hat der Ratchet die falsche Richtung verglichen. Jetzt
    gibt der Caller (_adjust) den Ratchet auf wenn DB-Direction != HL-Direction.
    """
    db = SessionLocal()
    try:
        mt = (db.query(ManagedTrade)
              .filter(ManagedTrade.user_id == user_id, ManagedTrade.coin == coin,
                      ManagedTrade.status != "closed")
              .order_by(ManagedTrade.id.desc()).first())
        if mt is None:
            return (None, None)
        # 2026-06-04 (#6): mt.stop_loss ist jetzt Decimal (MoneyDecimal).
        # Cast zu float am Boundary — Engine-internes Math arbeitet weiter
        # mit float (sig.stop_loss kommt vom Parser als float).
        sl = float(mt.stop_loss) if mt.stop_loss is not None else None
        return (sl, (mt.direction or "").upper() or None)
    finally:
        db.close()


def _pause_user_bad_key(user_id, err_msg, reason="invalid_key"):
    """Bot AUS schalten wenn Agent-Key kaputt ODER nicht autorisiert.
    Genau EINE Activity-Zeile statt 100 Tracebacks pro Tag.

    reason="invalid_key"   → Key-Format kaputt (42 statt 66 chars etc)
    reason="not_authorized" → Key technisch ok aber HL kennt ihn nicht
                              (User hat ExtraAgent revoked / nicht autorisiert)
    """
    db = SessionLocal()
    try:
        u = db.get(User, user_id)
        if not u or not u.bot_active:
            return  # idempotent — schon pausiert oder weg
        u.bot_active = False
        if reason == "not_authorized":
            text = (
                "Bot pausiert: Agent-Key ist gültig, aber NICHT als ExtraAgent "
                "auf Hyperliquid autorisiert (HL: 'User or API Wallet does not exist'). "
                "Im HL UI → API → 'Approve API Wallet' für den existierenden Agent klicken, "
                "ODER neuen Agent generieren und im Dashboard speichern. Dann Bot manuell wieder aktivieren."
            )
        else:
            text = (
                "Agent-Key ungültig (sieht aus wie eine 42-Zeichen-Adresse, "
                "erwartet wird der 66-Zeichen-Agent-Key). Bot pausiert. "
                "Im Dashboard den korrekten Agent-Key neu speichern, dann selbst wieder aktivieren."
            )
        db.add(Activity(user_id=user_id, kind="error", text=text))
        db.commit()
        log.warning("user %s auto-paused (%s): %s", user_id, reason, err_msg[:120])
    except Exception as e:
        log.error("auto-pause failed for user %s: %s", user_id, e)
    finally:
        db.close()


def _is_bad_key_error(exc):
    """Klassifiziert eine Exception als 'Key kaputt' (vs anderer Fehler).
    Trifft den eth-account-Validator: 'private key must be exactly 32 bytes'."""
    s = str(exc).lower()
    return ("private key must be exactly 32 bytes" in s
            or "unexpected private key length" in s)


def _is_unauthorized_agent_response(resp_text):
    """Erkennt HL's "User or API Wallet 0x... does not exist." Antwort.
    Bedeutet: Key ist gültig, aber HL kennt diesen Agent nicht (mehr).
    User hat ExtraAgent revoked oder nie autorisiert.
    """
    s = str(resp_text or "").lower()
    return "does not exist" in s and ("user or api wallet" in s or "api wallet" in s)


def _per_coin_stats(user_addr: str, coin: str):
    """Per-coin Trade-Event-Stats aus HL-Fills (Phase 2 #27, 2026-06-02).
    Cluster Partial-Fills (≤60s, gleiche Seite) zu Events; return win_rate + count.
    10-Min-Cache pro (user, coin) damit nicht jeder Trade einen Info-Call löst.
    """
    key = (user_addr, coin)
    now = time.time()
    cached = _percoin_cache.get(key)
    if cached and now - cached[0] < config.PERCOIN_CACHE_TTL_S:
        return cached[1]
    try:
        from app.hyperliquid_exec import get_info
        info = get_info(config.HL_TESTNET)
        fills = info.user_fills(user_addr) or []
    except Exception as e:
        log.warning("per-coin stats: HL fetch failed for %s: %s", user_addr[:8], e)
        return None
    fills.sort(key=lambda f: f.get("time", 0))
    events = []
    current = None
    for f in fills:
        if f.get("coin") != coin:
            continue
        pnl = float(f.get("closedPnl", 0) or 0)
        if pnl == 0:
            continue
        t = int(f.get("time", 0) or 0)
        d = f.get("dir", "")
        side = "Long" if "Long" in d else ("Short" if "Short" in d else "?")
        k2 = (coin, side)
        if current and current["key"] == k2 and t - current["t_last"] <= 60_000:
            current["pnl"] += pnl
            current["t_last"] = t
        else:
            if current is not None:
                events.append(current)
            current = {"key": k2, "t": t, "t_last": t, "pnl": pnl}
    if current is not None:
        events.append(current)
    trades = len(events)
    wins = sum(1 for e in events if e["pnl"] > 0)
    win_rate = (wins / trades) if trades else 0.0
    result = {"trades": trades, "wins": wins, "win_rate": win_rate}
    _percoin_cache[key] = (now, result)
    return result


def _per_coin_blocked(user_addr: str, coin: str):
    """True wenn Coin per-coin-filter blocken soll. Liefert (blocked, stats)."""
    s = _per_coin_stats(user_addr, coin)
    if s is None:
        return False, None
    if s["trades"] < config.PERCOIN_MIN_TRADES:
        return False, s
    return s["win_rate"] < config.PERCOIN_MIN_WINRATE, s


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
    # Confidence-Gate für ALLE Aktionen (Phase 1: vorher nur Einstiege).
    # Low-confidence CANCEL hat zuvor offene Positionen mid-trade geschlossen
    # und Verluste festgenagelt — eine der Haupt-Verlustquellen.
    if sig.confidence is not None and sig.confidence < config.MIN_CONFIDENCE:
        log.info("Signal %s %s ignoriert: confidence %.2f < %.2f",
                 action, coin_of(sig.ticker), sig.confidence, config.MIN_CONFIDENCE)
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
            _spawn(_open_or_update(uid, sig, action))


# ── NEW_TRADE / UPDATE_TRADE ─────────────────────────────────────────────────
async def _open_or_update(user_id, sig, action_type="NEW_TRADE"):
    """action_type = "NEW_TRADE" or "UPDATE_TRADE".

    2026-06-04 audit-fix (B-#7): UPDATE_TRADE bei pos==0 ist semantisch ein
    Anpassungs-Signal für eine bereits offene Position, die aber HL nicht (mehr)
    hat — z. B. weil der User manuell geschlossen hat oder ein vorheriger SL
    getriggert wurde. Wir sollten KEINE neue Position aufmachen, nur loggen
    und skippen. Vorher fiel dieser Pfad blind in _open_new und konnte gegen
    den User-Willen einen frischen Trade öffnen.
    """
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
                if action_type == "UPDATE_TRADE":
                    _log_activity(user_id, "skip",
                                  f"{coin}: UPDATE_TRADE ohne offene Position — nicht neu eröffnet "
                                  f"(Original-Position evtl. manuell oder per SL geschlossen).")
                    _close_managed(user_id, coin)         # DB-State aufräumen
                    return
                await asyncio.to_thread(trader.cancel_orders, coin)   # evtl. alte Ruhe-Order weg
                await _open_new(trader, u, sig)           # NEW_TRADE: frisch eröffnen
        except ValueError as e:
            # Bad-Key Auto-Pause (Phase 1): User hat eine 42-char Adresse statt
            # des 66-char Agent-Keys gespeichert. Statt 100 Tracebacks pro Tag
            # einmalig pausieren + klare Activity-Meldung.
            if _is_bad_key_error(e):
                _pause_user_bad_key(user_id, str(e))
                return
            log.exception("user %s %s: %s", user_id, coin, e)
            _log_activity(user_id, "error", f"{coin}: {e}")
        except Exception as e:
            log.exception("user %s %s: %s", user_id, coin, e)
            _log_activity(user_id, "error", f"{coin}: {e}")


async def _open_new(trader, u, sig):
    from app.sizing import size_trade
    coin = coin_of(sig.ticker)

    # Phase 2 #27 (2026-06-02): Per-Coin Auto-Filter. Schaut auf den User's
    # Track-Record für DIESES Coin auf Hyperliquid (cached 10 min). Wenn >=10
    # Trades und Win-Rate <30%, wird NEUE Position blockiert — UPDATE/CANCEL
    # bestehender Positionen läuft normal weiter.
    blocked, stats = await asyncio.to_thread(_per_coin_blocked, u.hl_account_address, coin)
    if blocked and stats:
        _log_activity(
            u.id, "skip",
            f"{coin}: per-coin filter — "
            f"win-rate {stats['win_rate']*100:.0f}% über {stats['trades']} Trades "
            f"< {config.PERCOIN_MIN_WINRATE*100:.0f}%-Schwelle — NEW_TRADE skipped"
        )
        return

    try:
        balance = await asyncio.to_thread(trader.account_value)
    except Exception as e:
        # 2026-06-04 audit-fix (B-#9): HL-Outage explizit unterscheiden von
        # "kein Geld" damit Beobachter (Discord-Alert/Admin-Dashboard) sieht
        # was wirklich los ist. Kein Trade-Open ohne verifiziertes Balance.
        from app.hyperliquid_exec import HLOutageError
        if isinstance(e, HLOutageError):
            _log_activity(u.id, "error", f"{coin}: HL Info-API down — Trade-Open abgebrochen ({e})")
        else:
            _log_activity(u.id, "error", f"{coin}: account_value-Fehler ({e}) — Trade-Open abgebrochen")
        return
    if balance <= 0:
        _log_activity(u.id, "skip", f"{coin}: kein handelbares Guthaben")
        return
    open_pos = await asyncio.to_thread(trader.open_positions_count)
    if open_pos >= u.max_open_positions:
        _log_activity(u.id, "skip", f"{coin}: max. Positionen ({open_pos}/{u.max_open_positions})")
        return

    # Phase 6+ (2026-06-03, H-9): Margin Pre-Check.
    # Vorher haben wir blind place_entry aufgerufen und HL's "Insufficient
    # margin"-Reject als error geloggt (2× am 2026-06-03 für BTC bei User 2:
    # 6 offene Positionen × 3× Leverage + Phase-2-SL-Min = kein Platz für BTC).
    # Jetzt berechnen wir das erforderliche Margin im Voraus und skippen sauber.
    # `withdrawable` aus user_state ist die freie Margin — sicherer als
    # accountValue, weil das schon offene Positionen abzieht.
    try:
        st_info = await asyncio.to_thread(trader.info.user_state, trader.address)
        withdrawable = float(st_info.get("withdrawable", 0) or 0)
    except Exception:
        withdrawable = balance  # Fallback — wir lassen es laufen, HL rejected sonst
    sl_distance = abs(sig.entry - sig.stop_loss)
    if sl_distance > 0:
        # Schätzung: required_notional = risk_amount/sl_dist * entry; required_margin = notional/leverage
        risk_amount = balance * u.risk_pct
        est_qty = risk_amount / sl_distance
        est_notional = est_qty * sig.entry
        est_margin = est_notional / max(1, u.leverage)
        # 10% safety puffer für Fees, Spread, Mark-Price-Drift bis zum Order
        if est_margin > withdrawable * 0.9:
            _log_activity(
                u.id, "skip",
                f"{coin}: insufficient margin pre-check — "
                f"need ~{est_margin:.2f} USDC, withdrawable {withdrawable:.2f} "
                f"(6 positions blocken margin) — NEW_TRADE skipped clean"
            )
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
            # 2026-06-05 diag: log die ECHTE HL-Antwort
            sl_resp = prot.get("sl")
            err_detail = str(sl_resp)[:300] if sl_resp else "no response"
            log.error("place_protection sl fail (new) user=%s coin=%s sl=%s sz=%s is_buy=%s resp=%s",
                      u.id, coin, sig.stop_loss, sz, is_buy, err_detail)
            # 2026-06-06: Bad-Agent → auto-pause statt closing (close würde gleich failen)
            if _is_unauthorized_agent_response(err_detail):
                _pause_user_bad_key(u.id, err_detail, reason="not_authorized")
                return
            await asyncio.to_thread(trader.close_position, coin)
            await asyncio.to_thread(trader.cancel_orders, coin)
            _log_activity(u.id, "error",
                          f"{coin}: SL nach Entry fehlgeschlagen — Position geschlossen. "
                          f"HL response: {err_detail[:180]}")
            return
        _log_activity(u.id, "order", f"{sig.direction} {coin} eröffnet (qty {sz:.6g}), SL+TP gesetzt")
        _save_managed(u.id, coin, sig, status="open")
    else:
        _log_activity(u.id, "order", f"{sig.direction} {coin}: Limit ruht @ {sig.entry}, warte auf Fill")
        _save_managed(u.id, coin, sig, status="resting", resting_oid=entry.get("resting_oid"))
        _spawn(_protect_when_filled(trader, u.id, sig, is_buy, tps))


async def _adjust(trader, u, sig, pos):
    """These hat sich geändert, Position ist offen -> SL/TP auf neue Level nachziehen.

    SL-RATCHET (Phase 1, 2026-06-02): Ein UPDATE_TRADE darf den SL NUR in
    Richtung 'sicherer' verschieben (LONG: höher; SHORT: niedriger). Loosen
    war die größte Verlustquelle (BNB SL 695→725 = −€25 zusätzlich, DOGE
    SL 0.113→0.172 = 70 % gelockert, NEAR zickzack). Verworfene Updates
    werden geloggt; der bestehende Schutz bleibt unverändert.
    """
    coin = coin_of(sig.ticker)
    is_buy = pos > 0
    if sig.stop_loss is None:
        # Update ohne neuen SL -> bestehende Schutz-Orders NICHT anfassen (nie ungeschützt lassen)
        _log_activity(u.id, "update", f"{coin}: Update ohne neuen SL — bestehender Schutz bleibt")
        _save_managed(u.id, coin, sig, status="open")
        return

    # ── SL-RATCHET — Update verwerfen, wenn es das Risiko erhöht ─────────
    current_sl, db_direction = _get_current_sl(u.id, coin)
    hl_direction = "LONG" if is_buy else "SHORT"
    # 2026-06-04 audit-fix (B-#1): Wenn DB-Direction != HL-Direction (User hat
    # die Position manuell geflippt zwischen den Signalen), gibt der Ratchet
    # auf — der gespeicherte SL gilt für die falsche Richtung und Vergleich
    # wäre semantisch falsch. Frischer Trade-Pfad: kein Ratchet, place_protection
    # setzt den neuen SL direkt.
    if current_sl is not None and db_direction and db_direction != hl_direction:
        _log_activity(
            u.id, "update",
            f"{coin}: Direction-Flip erkannt (DB={db_direction}, HL={hl_direction}) — "
            f"SL-Ratchet übersprungen, neuer SL {sig.stop_loss} wird gesetzt."
        )
        current_sl = None  # Ratchet ausschalten, sauber neu setzen
    if current_sl is not None:
        loosens = (is_buy and sig.stop_loss < current_sl) or (not is_buy and sig.stop_loss > current_sl)
        if loosens:
            _log_activity(
                u.id, "update",
                f"{coin}: SL-Update {sig.stop_loss} abgelehnt (würde Risiko erhöhen — "
                f"aktueller SL {current_sl}, {hl_direction}). Bestehender Schutz bleibt."
            )
            # WICHTIG: managed_trade NICHT überschreiben, sonst zukünftige
            # Ratchet-Checks vergleichen gegen den falschen Wert.
            return

    tps = [(tp.price, tp.percent / 100.0) for tp in sig.take_profits]

    # 2026-06-05: Preflight SL-vs-Mark-Check VOR cancel_orders.
    # Wenn SL auf falscher Seite des Mark → HL würde rejecten + wir hätten den
    # alten Schutz weggeräumt. Stattdessen: skip update, alter SL bleibt aktiv.
    # Selbe Logik in place_protection als Sicherheitsnetz wenn jemand direkt
    # ohne den engine-Preflight aufruft.
    try:
        from app.hyperliquid_exec import get_info
        mark = float(get_info(config.HL_TESTNET).all_mids().get(coin) or 0)
    except Exception:
        mark = 0
    if mark > 0:
        sl_invalid = (is_buy and sig.stop_loss >= mark) or ((not is_buy) and sig.stop_loss <= mark)
        if sl_invalid:
            side = "LONG" if is_buy else "SHORT"
            _log_activity(
                u.id, "update",
                f"{coin}: SL-Update {sig.stop_loss} skipped (würde sofort triggern — "
                f"{side} mark={mark}, regel: {'LONG SL<mark' if is_buy else 'SHORT SL>mark'}). "
                f"Bestehender Schutz bleibt unverändert."
            )
            return  # alter SL bleibt aktiv, kein cancel, keine Position-Close

    await asyncio.to_thread(trader.cancel_orders, coin)            # alte SL/TP weg
    prot = await asyncio.to_thread(trader.place_protection, coin, is_buy, abs(pos), sig.stop_loss, tps)
    if not prot.get("sl_ok"):
        # Falls noch ein anderer Grund fail (tickSize/etc) — log + close-Net.
        sl_resp = prot.get("sl"); skip_reason = prot.get("skip_reason")
        err_detail = skip_reason or (str(sl_resp)[:300] if sl_resp else "no response")
        log.error("place_protection sl fail user=%s coin=%s sl=%s sz=%s is_buy=%s resp=%s",
                  u.id, coin, sig.stop_loss, abs(pos), is_buy, err_detail)
        # 2026-06-06: Wenn HL "User or API Wallet does not exist" returnt → Agent
        # ist nicht autorisiert. close_position würde mit gleichem Fehler scheitern
        # (gleicher Agent). Auto-pause statt im Kreis weiter trying.
        if _is_unauthorized_agent_response(err_detail):
            _pause_user_bad_key(u.id, err_detail, reason="not_authorized")
            return
        await asyncio.to_thread(trader.close_position, coin)
        await asyncio.to_thread(trader.cancel_orders, coin)
        _log_activity(u.id, "error",
                      f"{coin}: SL-Update failed → Position geschlossen. HL response: {err_detail[:180]}")
        return
    _log_activity(u.id, "update", f"{coin}: These angepasst — SL {sig.stop_loss}, TP nachgezogen (qty {abs(pos):.6g})")
    _save_managed(u.id, coin, sig, status="open")


# ── CANCEL_TRADE ─────────────────────────────────────────────────────────────
async def _cancel(user_id, sig):
    """CANCEL-Behandlung — NUR pre-entry (Phase 1, 2026-06-02).

    Vorher hat ein CANCEL_TRADE-Signal offene Positionen sofort per Market
    geschlossen, auch wenn die Position underwater war. Das war neben dem
    SL-Loosen die zweite Haupt-Verlustquelle (0/13 Trades trafen TP, 4/13
    wurden per CANCEL im Minus geschlossen). Ab jetzt: bei offener Position
    wird CANCEL ignoriert — der Trade exitiert ausschließlich via SL/TP.
    Pre-entry (Limit ruht, noch nicht gefüllt) wird die Order weiterhin
    gecancelt.
    """
    u = _get_user(user_id)
    if not u:
        return
    coin = coin_of(sig.ticker)
    async with _lock_for(user_id, coin):
        try:
            trader = await _build_trader(u)
            pos = await asyncio.to_thread(trader.position_size, coin)

            if abs(pos) > 0:
                # Position OFFEN -> CANCEL ignorieren, SL/TP übernehmen den Exit.
                _log_activity(
                    user_id, "skip",
                    f"{coin}: CANCEL ignoriert — Position offen (qty {abs(pos):.6g}). "
                    f"Exit erfolgt über SL/TP."
                )
                return

            # Position FLAT -> evtl. ruhende Limit-Entry-Order canceln.
            n = await asyncio.to_thread(trader.cancel_orders, coin)
            if n > 0:
                _log_activity(user_id, "close", f"{coin}: pre-entry Limit gecancelt (n={n}) — These invalidiert")
            _close_managed(user_id, coin)
        except ValueError as e:
            if _is_bad_key_error(e):
                _pause_user_bad_key(user_id, str(e))
                return
            log.exception("cancel user %s %s: %s", user_id, coin, e)
            _log_activity(user_id, "error", f"{coin} cancel: {e}")
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
                # 2026-06-05 diag
                sl_resp = prot.get("sl"); err = str(sl_resp)[:300] if sl_resp else "no response"
                log.error("place_protection sl fail (watcher) user=%s coin=%s sl=%s sz=%s is_buy=%s resp=%s",
                          user_id, coin, sig.stop_loss, abs(psz), is_buy, err)
                # 2026-06-06: Bad-Agent → auto-pause statt closing
                if _is_unauthorized_agent_response(err):
                    _pause_user_bad_key(user_id, err, reason="not_authorized")
                    return
                await asyncio.to_thread(trader.close_position, coin)
                await asyncio.to_thread(trader.cancel_orders, coin)
                _log_activity(user_id, "error", f"{coin}: SL nach Fill fehlgeschlagen — Position geschlossen. HL: {err[:180]}")
                _close_managed(user_id, coin)
                return
            _log_activity(user_id, "order", f"{coin} gefüllt (qty {abs(psz):.6g}) — SL+TP gesetzt")
            _save_managed(user_id, coin, sig, status="open")
            return
    # 2026-06-04 audit-fix (B-#8): Vor dem cancel_orders FINAL prüfen ob die Order
    # nicht doch noch in der allerletzten Iteration gefillt wurde. Sonst Race:
    # zwischen letztem sleep und timeout könnte HL die Order matchen und wir
    # cancellen eine bereits eröffnete Position → naked.
    try:
        psz_final = await asyncio.to_thread(trader.position_size, coin)
    except Exception:
        psz_final = 0
    if abs(psz_final) > 0:
        prot = await asyncio.to_thread(trader.place_protection, coin, is_buy, abs(psz_final), sig.stop_loss, tps)
        if not prot.get("sl_ok"):
            await asyncio.to_thread(trader.close_position, coin)
            await asyncio.to_thread(trader.cancel_orders, coin)
            _log_activity(user_id, "error", f"{coin}: last-second fill, SL fehlgeschlagen → Position geschlossen")
            _close_managed(user_id, coin)
            return
        _log_activity(user_id, "order", f"{coin}: last-second fill (qty {abs(psz_final):.6g}) — SL+TP nachgesetzt")
        _save_managed(user_id, coin, sig, status="open")
        return
    await asyncio.to_thread(trader.cancel_orders, coin)
    _log_activity(user_id, "skip", f"{coin} nicht gefüllt — kein Trade")
    _close_managed(user_id, coin)
