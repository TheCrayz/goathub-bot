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

from cryptography.fernet import InvalidToken

from app import config
from app.crypto import decrypt
from app.db import SessionLocal
from app.models import Activity, ManagedTrade, User
from app.parser import CANCEL_ACTIONS, parse_signal

# Phase 2 #27 (2026-06-02): per-coin performance cache. {(addr,coin) → (ts, stats)}.
_percoin_cache: dict = {}

log = logging.getLogger("goathub.engine")

# 2026-06-08 Mainnet-Hardening A1/A2: Rate-Counter + Alert-Throttle.
# In-Memory (lost on restart, OK — counters reset = fail-open, safer than
# false-block; emergency-halt-flag persistent in file).
import os
_signal_timestamps: list = []                          # sliding-window aller Signal-Empfänge
_trade_intervals: dict = {}                            # (user_id, coin) -> last_trade_ts
_alert_throttle: dict = {}                             # (user_id, coin) -> last_alert_ts


def _emergency_halt_active() -> bool:
    """True wenn die Halt-Datei existiert. Datei statt env-var weil zur Laufzeit toggelbar."""
    try:
        return os.path.exists(config.EMERGENCY_HALT_FLAG_PATH)
    except Exception:
        return False


def _set_emergency_halt(reason: str = ""):
    """Setze die Halt-Datei mit Begründung. Idempotent."""
    try:
        with open(config.EMERGENCY_HALT_FLAG_PATH, "w") as f:
            f.write(f"{time.time()}\n{reason}\n")
        log.error("EMERGENCY_HALT activated: %s", reason)
    except Exception as e:
        log.error("Failed to set EMERGENCY_HALT flag: %s", e)


def _clear_emergency_halt():
    """Entferne die Halt-Datei. Bot reagiert wieder auf Signale."""
    try:
        if os.path.exists(config.EMERGENCY_HALT_FLAG_PATH):
            os.remove(config.EMERGENCY_HALT_FLAG_PATH)
        log.info("EMERGENCY_HALT cleared")
    except Exception as e:
        log.error("Failed to clear EMERGENCY_HALT flag: %s", e)


def _post_alert(text: str, key: tuple = None):
    """Schickt eine Discord-Alert wenn ALERT_WEBHOOK_URL gesetzt ist.
    `key` (z.B. (user_id, coin)) → Throttle: max 1 Alert pro key pro
    ALERT_THROTTLE_S Sekunden. None = nicht throttled (für globale Halts).
    Nicht-blockierend (best-effort, swallowt Fehler).
    """
    url = config.ALERT_WEBHOOK_URL
    if not url:
        return
    if key is not None:
        now = time.time()
        last = _alert_throttle.get(key, 0)
        if now - last < config.ALERT_THROTTLE_S:
            return
        _alert_throttle[key] = now

    def _send():
        try:
            import httpx
            httpx.post(url, json={"content": text[:1900]}, timeout=2.5)
        except Exception as e:
            log.warning("alert webhook failed: %s", e)

    # 2026-06-12 (Review #23): vorher lief httpx.post SYNCHRON auf dem Event-Loop
    # — der Kommentar log "non-blocking", real stallte jeder Error-Alert alle
    # Trading-Coroutinen bis zu 2.5s (bei Error-Stürmen serialisiert hinter
    # Discord-Latenz). Jetzt: läuft ein Loop → Executor-Thread (echtes
    # fire-and-forget); kein Loop (Sync-Kontext, z.B. to_thread-Worker) →
    # inline wie bisher (blockiert dann nur den Worker-Thread, nicht den Loop).
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        try:
            loop.run_in_executor(None, _send)
            return
        except Exception as e:
            log.warning("alert webhook dispatch failed: %s", e)
    _send()


def _signal_rate_check() -> bool:
    """True wenn signal-rate unter MAX_SIGNALS_PER_HOUR liegt. False = blocken.
    Bei block: setze EMERGENCY_HALT (kann manuell wieder gecleart werden).
    Sliding-window über die letzten 3600s.
    """
    now = time.time()
    # Garbage-collect alte timestamps (>1h alt)
    cutoff = now - 3600
    _signal_timestamps[:] = [t for t in _signal_timestamps if t > cutoff]
    if len(_signal_timestamps) >= config.MAX_SIGNALS_PER_HOUR:
        msg = (f"🚨 EMERGENCY_HALT: {len(_signal_timestamps)} signals in last hour "
               f"(cap {config.MAX_SIGNALS_PER_HOUR}). Auto-halt activated. "
               f"Untersuche signal-bot, dann manuell DELETE {config.EMERGENCY_HALT_FLAG_PATH}.")
        _set_emergency_halt(msg)
        _post_alert(msg)
        return False
    _signal_timestamps.append(now)
    return True


def _trade_interval_ok(user_id: int, coin: str) -> bool:
    """True wenn der letzte Trade für (user, coin) länger als MIN_TRADE_INTERVAL_S her ist.

    2026-06-12 (Audit M-5): NUR prüfen, NICHT mehr stempeln. Vorher wurde der
    Timestamp schon beim Check gesetzt — ein geskippter/fehlgeschlagener Entry
    (Margin-Skip, Filter, HL-Reject) verbrauchte das 60s-Fenster und ein
    korrigiertes Re-Emit desselben Signals wurde verworfen. Der Stempel kommt
    jetzt aus _record_trade_ts, erst NACH erfolgreicher Entry-Platzierung.
    """
    key = (user_id, coin)
    now = time.time()
    last = _trade_intervals.get(key, 0)
    return now - last >= config.MIN_TRADE_INTERVAL_S


def _record_trade_ts(user_id: int, coin: str):
    """Audit M-5: Throttle-Fenster erst starten, wenn der Entry wirklich
    platziert wurde (filled ODER resting). Caller: _open_new."""
    _trade_intervals[(user_id, coin)] = time.time()

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

# C2 (2026-06-09): Ein Lock pro user_id. Serialisiert ALLE Entries eines Users,
# damit der Aggregat-Margin-Cap (C1) atomar greift — sonst lesen N parallele
# Signale verschiedener Coins denselben Margin-/Positions-Stand VOR dem ersten
# Fill und überrennen jeden Cap gemeinsam. IMMER vor _lock_for(user,coin)
# nehmen (konsistente Reihenfolge user->coin => kein Deadlock).
_user_locks = {}

def _user_lock_for(user_id):
    lk = _user_locks.get(user_id)
    if lk is None:
        lk = asyncio.Lock()
        _user_locks[user_id] = lk
    return lk


# 2026-06-12 (Review #6): genau EIN Fill-Watcher pro (user, coin). Vorher lief ein
# alter Watcher nach einem zweiten NEW_TRADE weiter, hielt den Fill des NEUEN
# Entries für "seinen" und setzte SL/TP des ALTEN Signals (+ überschrieb die
# managed_trade-Row mit veralteten Werten → Ratchet-Baseline korrupt).
_fill_watchers: dict = {}        # (user_id, coin) -> asyncio.Task


def _register_fill_watcher(user_id, coin, task):
    _fill_watchers[(user_id, coin)] = task

    def _cleanup(t, key=(user_id, coin)):
        if _fill_watchers.get(key) is t:
            _fill_watchers.pop(key, None)
    task.add_done_callback(_cleanup)


def _cancel_fill_watcher(user_id, coin):
    """Alten Watcher für (user, coin) canceln, bevor ein neuer Trade-Pfad die
    Orders anfasst. Caller hält das (user, coin)-Lock → der Watcher kann dabei
    nicht mitten in einer eigenen Mutation stecken (er nimmt dasselbe Lock)."""
    t = _fill_watchers.pop((user_id, coin), None)
    if t is not None and not t.done():
        t.cancel()


def _watcher_row_current(user_id, coin, resting_oid, signal_id=None):
    """2026-06-12 (Review #6): Watcher validiert vor JEDER Mutation, dass seine
    managed_trade-Row noch die aktuelle ist (gleiche resting_oid, nicht closed).
    False → der Watcher ist von einem neueren Signal/CANCEL überholt worden und
    darf nicht mehr handeln.

    2026-06-12 (Audit H-B): zusätzlich Generation-Check via signal_id. Die
    resting_oid allein reicht nicht: füllt der NEUE Entry sofort, schreibt
    _save_managed(status='open') KEINE neue resting_oid in die Row — die alte
    bliebe stehen und der stale Watcher hielte sich für aktuell und würde
    SL/TP des ALTEN Signals gegen die NEUE Position setzen."""
    db = SessionLocal()
    try:
        mt = (db.query(ManagedTrade)
              .filter(ManagedTrade.user_id == user_id, ManagedTrade.coin == coin,
                      ManagedTrade.status != "closed")
              .order_by(ManagedTrade.id.desc()).first())
        if mt is None:
            return False
        if signal_id and mt.signal_id and str(mt.signal_id) != str(signal_id):
            return False
        if resting_oid is not None and str(mt.resting_oid or "") != str(resting_oid):
            return False
        return True
    except Exception as e:
        # Bei DB-Fehler lieber weiterlaufen lassen (Schutz nachlegen ist das
        # sicherere Default) als den Watcher blind zu beenden.
        log.warning("watcher row check failed user=%s coin=%s: %s", user_id, coin, e)
        return True
    finally:
        db.close()


def coin_of(t):
    return (t or "").split("/")[0].strip().upper()


def _log_activity(user_id, kind, text):
    # 2026-06-12 (Review #24, bewusst NICHT gefixt): die DB-Helper hier laufen
    # synchron auf dem Event-Loop (busy_timeout=5s ⇒ Worst-Case-Stall pro Call).
    # Voller Umbau auf to_thread/aiosqlite ist vor Mainnet out of scope — bei
    # 3 Beta-Testern + SQLite-WAL ist Contention minimal; Umbau vor Skalierung.
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
    # 2026-06-08 Mainnet-Hardening A2: error-activity zusätzlich an Discord-Webhook.
    # Throttled per (user_id, ersten 20 chars des text) damit nicht der gleiche
    # Fehler 100× in einer Stunde alerted wird.
    if kind == "error":
        throttle_key = (user_id, str(text)[:30])
        _post_alert(f"🚨 [user {user_id}] {text}", key=throttle_key)


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


def _signal_already_done(user_id, signal_id):
    """C3 (2026-06-09): True, wenn (user, signal_id) bereits erfolgreich
    ausgeführt wurde (persistent, überlebt Restart)."""
    if not signal_id:
        return False
    from app.models import ProcessedSignal
    db = SessionLocal()
    try:
        return (db.query(ProcessedSignal)
                .filter(ProcessedSignal.user_id == user_id,
                        ProcessedSignal.signal_id == signal_id).first() is not None)
    except Exception as e:
        log.warning("signal dedup check: %s", e)
        return False   # fail-open: lieber evtl. doppelt als gar nicht traden
    finally:
        db.close()


def _mark_signal_done(user_id, signal_id):
    """C3: (user, signal_id) als ausgeführt markieren. Idempotent dank UNIQUE."""
    if not signal_id:
        return
    from app.models import ProcessedSignal
    db = SessionLocal()
    try:
        db.add(ProcessedSignal(user_id=user_id, signal_id=signal_id))
        db.commit()
    except Exception:
        db.rollback()   # UNIQUE-Verletzung = schon vorhanden, ok
    finally:
        db.close()


def _unmark_signal_done(user_id, signal_id):
    """Audit LOW-1 (2026-06-12): Dedup-Marke wieder entfernen, wenn der Entry
    NIE gefüllt wurde und der Watcher die ruhende Order cancelt. Vorher blieb
    die Marke stehen — ein Re-Emit desselben signal_id wäre für immer
    übersprungen worden, obwohl real nie getradet wurde."""
    if not signal_id:
        return
    from app.models import ProcessedSignal
    db = SessionLocal()
    try:
        (db.query(ProcessedSignal)
         .filter(ProcessedSignal.user_id == user_id,
                 ProcessedSignal.signal_id == signal_id)
         .delete())
        db.commit()
    except Exception as e:
        log.warning("signal un-dedup failed: %s", e)
        db.rollback()
    finally:
        db.close()


def _load_protection_params(user_id, coin):
    """H1: SL + TP-Liste aus dem letzten offenen managed_trade für (user, coin).
    Returns (sl_float_or_None, [(px, fraction), ...]) — Format wie place_protection."""
    db = SessionLocal()
    try:
        mt = (db.query(ManagedTrade)
              .filter(ManagedTrade.user_id == user_id, ManagedTrade.coin == coin,
                      ManagedTrade.status != "closed")
              .order_by(ManagedTrade.id.desc()).first())
        if mt is None or mt.stop_loss is None:
            return (None, [])
        sl = float(mt.stop_loss)
        tps = []
        if mt.take_profits:
            try:
                for px, pct in json.loads(mt.take_profits):
                    tps.append((float(px), float(pct) / 100.0))
            except Exception:
                tps = []
        return (sl, tps)
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
    import datetime as _dt
    db = SessionLocal()
    try:
        mt = (db.query(ManagedTrade)
              .filter(ManagedTrade.user_id == user_id, ManagedTrade.coin == coin,
                      ManagedTrade.status != "closed")
              .order_by(ManagedTrade.id.desc()).first())
        if mt is None:
            # 2026-06-12 (Review #41): Fallback auf die JÜNGSTE (auch closed) Row.
            # Der Caller (_adjust) ruft uns nur, wenn HL eine OFFENE Position hat —
            # existiert dann keine offene Row, hat höchstwahrscheinlich der
            # Position-Sync sie verfrüht geclosed (2 transiente Leer-Antworten).
            # Ohne Fallback wäre der SL-Ratchet aus und ein gelockerter
            # (risiko-ERHÖHENDER) Signal-SL ginge durch. Zeitfenster 24h: ältere
            # closed-Rows sind mit hoher Wahrscheinlichkeit echte Closes
            # (manueller Re-Open des Users = legitim neuer Trade, kein Ratchet).
            cutoff = _dt.datetime.utcnow() - _dt.timedelta(hours=24)
            mt = (db.query(ManagedTrade)
                  .filter(ManagedTrade.user_id == user_id, ManagedTrade.coin == coin,
                          ManagedTrade.updated_at >= cutoff)
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


_recent_pause_keys: dict = {}  # user_id → ts der letzten Pause-Aktion (Idempotenz)


def _pause_user_bad_key(user_id, err_msg, reason="invalid_key"):
    """Bot AUS schalten wenn Agent-Key kaputt ODER nicht autorisiert.
    Genau EINE Activity-Zeile statt 100 Tracebacks pro Tag.

    reason="invalid_key"   → Key-Format kaputt (42 statt 66 chars etc)
    reason="not_authorized" → Key technisch ok aber HL kennt ihn nicht
                              (User hat ExtraAgent revoked / nicht autorisiert)
    reason="decrypt_failed" → Fernet InvalidToken: ENCRYPTION_KEY passt nicht
                              zum Ciphertext (Key rotiert/falsch) oder die
                              gespeicherten Daten sind korrupt (Audit M-4)

    2026-06-08 C7: Idempotenz-Race-Fix. Bei 2 concurrent signals für gleichen
    kaputten User können beide Tasks _pause_user_bad_key parallel aufrufen.
    `db.get(u).bot_active` zeigt für beide noch True → 2 Activity-Zeilen.
    In-memory suppression-Set verhindert das für die ersten 30s nach Pause.
    """
    now = time.time()
    last = _recent_pause_keys.get(user_id, 0)
    if now - last < 30:
        return  # vor max 30s schon pausiert — concurrent caller, skip
    _recent_pause_keys[user_id] = now
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
        elif reason == "decrypt_failed":
            text = (
                "Bot pausiert: gespeicherter Agent-Key nicht entschlüsselbar "
                "(Schlüssel nicht entschlüsselbar — ENCRYPTION_KEY geändert oder "
                "Daten korrupt). Bitte Agent-Key im Dashboard neu eingeben, "
                "dann Bot selbst wieder aktivieren."
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
    # 2026-06-08 Mainnet-Hardening A1+A3: EMERGENCY_HALT-Check zuerst.
    # File-basiert damit zur Laufzeit toggelbar (durch /api/admin/halt
    # oder durch _signal_rate_check bei Rate-Storm).
    if _emergency_halt_active():
        log.warning("Signal ignoriert: EMERGENCY_HALT aktiv (file %s exists)",
                    config.EMERGENCY_HALT_FLAG_PATH)
        return
    sig = parse_signal(embed)
    if sig is None:
        # Audit LOW-3 (2026-06-12): nicht mehr stumm verwerfen — sonst sind
        # Format-Drifts von Bot 1 unsichtbar und Signale verschwinden spurlos.
        log.warning("Signal nicht parsebar — verworfen (embed title=%r)",
                    (embed or {}).get("title"))
        return
    action = sig.action.upper()
    if action == "HOLD":
        return
    is_cancel = action in CANCEL_ACTIONS
    is_entry = action in ("NEW_TRADE", "UPDATE_TRADE")
    if not is_cancel and not is_entry:
        return
    # 2026-06-08 Mainnet-Hardening A1: Signal-Rate-Cap. Bei Überschreitung →
    # auto-Halt + Discord-Alert + dieses Signal wird verworfen.
    if not _signal_rate_check():
        log.error("Signal %s %s VERWORFEN — auto-halt triggered (rate exceeded)",
                  action, coin_of(sig.ticker))
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
        # 2026-06-12 (Review #19): der MIN_TRADE_INTERVAL_S-Throttle ist von hier
        # in den NEW_TRADE-Pfad von _open_or_update gewandert. Vorher hat er auch
        # risiko-REDUZIERENDE UPDATE_TRADEs verworfen ("NEW_TRADE, 30s später
        # SL-Tighten" → Update weg, Position behielt den weiteren Stop). Der
        # Throttle existiert gegen Entry-Storm-FEES — SL/TP-Anpassungen einer
        # offenen Position kosten nichts und dürfen NIE gedrosselt werden.
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
    # C2 (2026-06-09): per-User-Lock ZUERST (atomarer Aggregat-Margin-Cap),
    # dann per-(User,Coin)-Lock. Reihenfolge user->coin => kein Deadlock.
    async with _user_lock_for(user_id), _lock_for(user_id, coin):
        try:
            trader = await _build_trader(u)
            if not trader.is_tradable(coin):
                _log_activity(user_id, "skip", f"{coin}: nicht auf Hyperliquid handelbar — übersprungen")
                return
            # 2026-06-12 (Review #0): position_size raised jetzt bei API-Fehlern
            # statt 0.0 zu liefern. Unbekannter Positionsstatus = sicherer Abbruch
            # — NIEMALS "flat" annehmen (sonst: cancel der live SL/TP + Doppel-Entry
            # auf eine offene Position).
            try:
                pos = await asyncio.to_thread(trader.position_size, coin)
            except Exception as e:
                _log_activity(user_id, "error",
                              f"{coin}: Positionsstatus nicht lesbar ({str(e)[:160]}) — "
                              f"{action_type} abgebrochen (kein 'flat' angenommen, nichts verändert).")
                return
            if abs(pos) > 0:
                # H-2 (2026-06-12): Gegenrichtungs-Signal auf offener Position NICHT
                # blind in _adjust laufen lassen — _adjust leitet is_buy aus der HL-
                # Position ab und würde SL/TP für die FALSCHE Seite setzen, alle TPs
                # verwerfen und die DB-Richtung korrumpieren. Bei Richtungs-Mismatch:
                # ablehnen + alerten, Position + Schutz bleiben unberührt.
                sig_dir = (sig.direction or "").upper()
                if sig_dir in ("LONG", "SHORT") and (sig_dir == "LONG") != (pos > 0):
                    _log_activity(
                        user_id, "skip",
                        f"{coin}: {sig_dir}-Signal widerspricht offener "
                        f"{'LONG' if pos > 0 else 'SHORT'}-Position — Update abgelehnt "
                        f"(kein Flip, keine falsche Schutz-Seite). Bestehender Schutz bleibt.")
                    return
                await _adjust(trader, u, sig, pos)        # Position offen -> SL/TP nachziehen
            else:
                if action_type == "UPDATE_TRADE":
                    _log_activity(user_id, "skip",
                                  f"{coin}: UPDATE_TRADE ohne offene Position — nicht neu eröffnet "
                                  f"(Original-Position evtl. manuell oder per SL geschlossen).")
                    _close_managed(user_id, coin)         # DB-State aufräumen
                    return
                # 2026-06-12 (Review #19): Throttle HIER (nur echte Neueröffnungen),
                # und VOR cancel_orders — ein gedrosseltes Replay darf den ruhenden
                # Entry + Watcher des Originals nicht wegräumen.
                if not _trade_interval_ok(user_id, coin):
                    log.info("Skip NEW_TRADE %s for user %s — min-trade-interval not elapsed (%ds)",
                             coin, user_id, config.MIN_TRADE_INTERVAL_S)
                    _log_activity(user_id, "skip",
                                  f"{coin}: trade-interval-throttle aktiv (min {config.MIN_TRADE_INTERVAL_S}s), skip.")
                    return
                # 2026-06-12 (Review #6): alten Fill-Watcher VOR cancel_orders beenden,
                # sonst hält er den Fill des NEUEN Entries für seinen und setzt
                # SL/TP des alten Signals.
                _cancel_fill_watcher(user_id, coin)
                await asyncio.to_thread(trader.cancel_orders, coin)   # evtl. alte Ruhe-Order weg
                await _open_new(trader, u, sig)           # NEW_TRADE: frisch eröffnen
        except InvalidToken:
            # Audit M-4 (2026-06-12): falscher ENCRYPTION_KEY / korrupter
            # Ciphertext → decrypt() in _build_trader raised InvalidToken.
            # Das ist KEIN ValueError und fiel bisher in den generischen Handler:
            # voller Traceback bei JEDEM Signal, keine Auto-Pause — gleiche
            # Spam-Klasse wie der alte Bad-Key-Bug. Jetzt: einmalig pausieren.
            _pause_user_bad_key(user_id, "Fernet InvalidToken (decrypt failed)",
                                reason="decrypt_failed")
            return
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

    # 2026-06-12 (Review #16): Direction MUSS exakt LONG oder SHORT sein. Vorher
    # machte `is_buy = (sig.direction == "LONG")` aus jedem fehlenden/umbenannten
    # Direction-Feld (Format-Drift in Bot 1) einen SHORT für ALLE User — inkl.
    # SL auf der falschen Seite → Instant-Reject → Force-Close mit Fees/Slippage.
    direction = (sig.direction or "").strip().upper()
    if direction not in ("LONG", "SHORT"):
        _log_activity(u.id, "error",
                      f"{coin}: Signal ohne gültige Direction ({sig.direction!r}) — "
                      f"NEW_TRADE übersprungen (nicht als SHORT geraten).")
        return

    # C3 (2026-06-09): Replay-Dedup. Wurde dieses signal_id für den User schon
    # erfolgreich ausgeführt (persistent, überlebt Restart) → nicht erneut
    # öffnen. Schützt gegen Doppel-Entries durch re-emittierte Signale nach
    # Neustart, wenn der In-Memory-Throttle weg ist.
    if sig.signal_id and _signal_already_done(u.id, sig.signal_id):
        _log_activity(u.id, "skip",
                      f"{coin}: signal_id {sig.signal_id} bereits ausgeführt — Replay übersprungen")
        return

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

    # 2026-06-08 Mainnet-Hardening C1: Max-Drawdown Lifetime-Cap.
    # Wenn aktueller account_value mehr als max_drawdown_pct unter dem
    # historischen peak liegt → auto-pause + alert. Schützt User vor
    # Streak-Verlusten ("hätt ich mal früher aufgehört").
    # max_drawdown_pct=0 deaktiviert das Feature.
    max_dd = float(getattr(u, "max_drawdown_pct", 0) or 0)
    peak = float(getattr(u, "peak_account_value", 0) or 0)
    if max_dd > 0 and peak > 0:
        threshold = peak * (1 - max_dd)
        if balance < threshold:
            _log_activity(
                u.id, "error",
                f"🚨 MAX-DRAWDOWN-CAP: balance ${balance:.2f} < threshold ${threshold:.2f} "
                f"(peak ${peak:.2f}, cap {max_dd:.0%}). Bot pausiert. Sieh dir die Trades "
                f"an, justiere risk_pct/leverage, dann selbst wieder aktivieren."
            )
            _db = SessionLocal()
            try:
                uu = _db.get(User, u.id)
                if uu:
                    uu.bot_active = False
                    _db.commit()
            finally:
                _db.close()
            return
    # Update peak-tracker (im Code; persist nur wenn höher)
    if balance > peak:
        _db = SessionLocal()
        try:
            uu = _db.get(User, u.id)
            if uu:
                uu.peak_account_value = balance
                _db.commit()
        finally:
            _db.close()

    open_pos = await asyncio.to_thread(trader.open_positions_count)
    if open_pos >= u.max_open_positions:
        _log_activity(u.id, "skip", f"{coin}: max. Positionen ({open_pos}/{u.max_open_positions})")
        return

    # C1 (2026-06-09, korrigiert): Aggregat-Margin-Cap gegen die GESAMT-Equity.
    # util = 1 - frei/equity. WICHTIG: NICHT marginSummary-Ratio — dessen
    # accountValue ist im Unified-Account nur die Perps-Seite und bei viel freiem
    # Spot viel kleiner als die Gesamt-Equity (zeigte 75% statt echter ~50%).
    # `balance` = account_value() = Gesamt-Equity; `avail` wird unten im Margin-
    # Pre-Check wiederverwendet (nur EIN HL-Read).
    try:
        avail = await asyncio.to_thread(trader.available_margin)
    except Exception:
        avail = balance
    util = (1.0 - avail / balance) if balance > 0 else 0.0
    if util >= config.MAX_MARGIN_UTILIZATION:
        _log_activity(
            u.id, "skip",
            f"{coin}: Margin-Auslastung {util*100:.0f}% ≥ Cap {config.MAX_MARGIN_UTILIZATION*100:.0f}% "
            f"(frei ${avail:.0f} von ${balance:.0f}) — kein neuer Entry. NEW_TRADE skipped clean")
        return

    sl_distance = abs(sig.entry - sig.stop_loss)
    # 2026-06-06: Auto-Leverage statt user-fixed. Bot rechnet aus SL-Distanz +
    # Signal-Confidence den optimalen Hebel; u.leverage wird zur Max-Cap.
    # Vorteile: tight-SL setups nutzen ihre Margin-Efficiency, wide-SL setups
    # werden konservativ — Risiko (risk_pct × balance) ist in beiden Fällen
    # gleich, aber margin-usage adaptiert.
    from app.sizing import auto_leverage
    try:
        chosen_lev, lev_reason = auto_leverage(
            entry=sig.entry, stop_loss=sig.stop_loss,
            confidence=sig.confidence, max_cap=int(u.leverage or 50),
        )
    except ValueError as e:
        _log_activity(u.id, "error", f"{coin}: auto-lev failed ({e}) — skipping")
        return

    # 2026-06-12 (Review #17): auf das Asset-Max aus meta clampen (viele Alts
    # cappen bei 3-10x). Sonst rejected HL update_leverage non-transient und der
    # Entry liefe mit dem zuletzt gesetzten (undefinierten) Hebel — Liq-Mathe
    # und Margin-Pre-Check wären ungültig.
    try:
        asset_max = int(getattr(trader, "max_leverage", lambda c: 0)(coin) or 0)
    except Exception:
        asset_max = 0
    if asset_max > 0 and chosen_lev > asset_max:
        lev_reason = f"{lev_reason} — auf Asset-Max {asset_max}x geclampt"
        chosen_lev = asset_max

    # Phase 6+ Margin Pre-Check (2026-06-08, korrigiert für Unified-Accounts).
    # Vorher las der Check nur Perps-`withdrawable`. Bei Hyperliquid Unified-
    # Accounts (Standard, beide GoatHub-User) ist das IMMER 0 — Spot- und Perps-
    # USDC teilen eine Collateral-Basis, ein Transfer existiert nicht. Resultat:
    # JEDER Trade wurde geskippt, obwohl das volle Kapital handelbar war. Die
    # alte Skip-Meldung log sogar ein hardcodiertes "6 positions" (gelogen, es
    # waren 0). trader.available_margin() liest jetzt die echte Unified-
    # Collateral (tokenToAvailableAfterMaintenance) und funktioniert für unified
    # UND klassische Accounts.
    if sl_distance > 0:
        # Schätzung: required_notional = risk_amount/sl_dist * entry; required_margin = notional/leverage
        # Audit LOW-2 (2026-06-12): gleiche Basis wie size_trade — eff =
        # min(balance, capital_cap). Vorher balance*risk_pct: für gecappte User
        # überschätzte das die nötige Margin → falsche "insufficient margin"-Skips.
        cap = float(u.capital_cap_usdc or 0)
        eff = min(balance, cap) if cap > 0 else balance
        risk_amount = eff * u.risk_pct
        est_qty = risk_amount / sl_distance
        est_notional = est_qty * sig.entry
        est_margin = est_notional / max(1, chosen_lev)
        needed = est_margin / 0.9  # 10% Puffer für Fees, Spread, Mark-Drift bis zum Order
        # `avail` kommt aus dem C1-Block oben (ein HL-Read, wiederverwendet).
        if avail < needed:
            _log_activity(
                u.id, "skip",
                f"{coin}: insufficient margin — need ~${est_margin:.2f} "
                f"(mit Puffer ${needed:.2f}), available ${avail:.2f}. "
                f"Funde dein HL-Konto. NEW_TRADE skipped clean"
            )
            return
    plan = size_trade(account_value=balance, capital_cap=u.capital_cap_usdc, risk_pct=u.risk_pct,
                      entry=sig.entry, stop_loss=sig.stop_loss, leverage=chosen_lev)
    if plan.notional < config.MIN_NOTIONAL_USDC:
        _log_activity(u.id, "skip", f"{coin}: Notional {plan.notional:.2f} < {config.MIN_NOTIONAL_USDC}")
        return

    is_buy = (direction == "LONG")
    tps = [(tp.price, tp.percent / 100.0) for tp in sig.take_profits]
    # 2026-06-12 (Review #17): set_leverage-Resultat prüfen. hyperliquid_exec
    # liefert bewusst ein err-dict nach finalem Retry-Fail ("Engine cancelt entry
    # sauber, statt naked-place") — das wurde hier bisher verworfen. Ohne
    # bestätigten Hebel wäre est_margin/Liq-Sicherheit Makulatur → Entry skippen.
    lev_res = await asyncio.to_thread(trader.set_leverage, coin, chosen_lev)
    if not (isinstance(lev_res, dict) and lev_res.get("status") == "ok"):
        _log_activity(u.id, "error",
                      f"{coin}: set_leverage({chosen_lev}x) fehlgeschlagen "
                      f"({str(lev_res)[:160]}) — Entry übersprungen (kein Trade mit unbestätigtem Hebel).")
        return
    _log_activity(u.id, "update", f"{coin}: {lev_reason}")
    entry = await asyncio.to_thread(trader.place_entry, coin, is_buy, plan.qty, sig.entry)
    if not entry["ok"]:
        _log_activity(u.id, "error", f"Entry-Fehler {coin}: {entry.get('error')}")
        return

    # C3 (2026-06-09): Order ist platziert (filled ODER resting) → signal_id als
    # ausgeführt markieren, damit ein Replay (z.B. nach Restart) nicht doppelt
    # öffnet. Geskippte Signale (Margin/Filter, oben) bleiben retry-bar.
    _mark_signal_done(u.id, sig.signal_id)
    # Audit M-5 (2026-06-12): Trade-Interval-Fenster erst JETZT starten — der
    # Entry ist wirklich platziert. Skips/Fails oben verbrauchen das Fenster
    # nicht mehr (ein korrigiertes Re-Emit bleibt handelbar).
    _record_trade_ts(u.id, coin)
    # H-6 (2026-06-12): Stale-Sync-Strikes für diesen Coin löschen — die neue
    # Position kann die managed_trade-Row wiederverwenden; ein getragener Strike
    # der alten Generation darf die neue Live-Position nicht auf 'closed' flippen.
    from app.sync import clear_strikes
    clear_strikes(u.id, coin)

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
            # 2026-06-12 (Review #4): close_position-Resultat prüfen. Vorher wurde
            # bei fehlgeschlagenem Market-Close trotzdem "Position geschlossen"
            # geloggt — niemand hat die offene, UNGESCHÜTZTE Position untersucht.
            close_res = await asyncio.to_thread(trader.close_position, coin)
            if not close_res.get("ok"):
                # Row mit SL-Params offen halten, damit der Coverage-Reconciler
                # (sync-Loop) den Schutz nachlegen kann.
                _save_managed(u.id, coin, sig, status="open")
                msg = (f"NOTFALL: Emergency-Close fehlgeschlagen — Position offen & ungeschützt! "
                       f"{coin}: SL nach Entry fehlgeschlagen UND Market-Close fehlgeschlagen "
                       f"({str(close_res.get('error') or close_res.get('raw'))[:120]}). "
                       f"Sofort manuell prüfen. HL (SL): {err_detail[:120]}")
                # Audit H-A (2026-06-12): UNGETHROTTLEDER Alert (key=None) — dieser
                # Fall darf NIE im (user,coin)-Throttle untergehen.
                _post_alert(f"🚨 [user {u.id}] {msg}", key=None)
                _log_activity(u.id, "error", msg)
                return
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
        # 2026-06-12 (Review #6): Watcher registrieren, damit ein späteres Signal/
        # CANCEL ihn gezielt beenden kann (genau einer pro user+coin).
        t = _spawn(_protect_when_filled(trader, u.id, sig, is_buy, tps, entry.get("resting_oid")))
        _register_fill_watcher(u.id, coin, t)


def _get_mark(coin: str) -> float:
    """Aktueller Mark-Preis (0.0 bei Fehler). BLOCKIERENDER HL-Read — nur via
    asyncio.to_thread aufrufen (Review #9). Als Modul-Funktion testbar/mockbar."""
    try:
        from app.hyperliquid_exec import get_info
        return float(get_info(config.HL_TESTNET).all_mids().get(coin) or 0)
    except Exception:
        return 0.0


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
    if not tps:
        # 2026-06-12 (Review #5): SL-only-Update darf die TP-Abdeckung nicht
        # wegwischen. cancel_orders unten räumt SL UND alle TPs weg; wurden ohne
        # diesen Fallback nur der SL neu gesetzt, war der EINZIGE verbleibende
        # Exit der Stop (CANCEL wird bei offener Position ignoriert) — Trade
        # konnte nur noch im Minus enden. Also: gespeicherte TPs aus dem
        # managed_trade wiederherstellen.
        _sl_stored, tps_stored = _load_protection_params(u.id, coin)
        if tps_stored:
            tps = tps_stored
            log.info("adjust %s/%s: Signal ohne TPs — %d gespeicherte TPs wiederhergestellt",
                     u.id, coin, len(tps))

    # 2026-06-05: Preflight SL-vs-Mark-Check VOR cancel_orders.
    # Wenn SL auf falscher Seite des Mark → HL würde rejecten + wir hätten den
    # alten Schutz weggeräumt. Stattdessen: skip update, alter SL bleibt aktiv.
    # Selbe Logik in place_protection als Sicherheitsnetz wenn jemand direkt
    # ohne den engine-Preflight aufruft.
    # 2026-06-12 (Review #9): via to_thread — der all_mids()-Call ist ein
    # blockierender requests-HTTP-Call; direkt auf dem Event-Loop fror er bei
    # HL-Hängern die GESAMTE Engine (alle User, Dashboard, Sync) ein.
    mark = await asyncio.to_thread(_get_mark, coin)
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

    # 2026-06-12 (Review #6): evtl. noch laufenden Fill-Watcher beenden — nach dem
    # cancel_orders gleich darunter existiert keine Rest-Entry-Order mehr, der
    # Watcher könnte nur noch Doppel-Schutz auf die frische Voll-Abdeckung stapeln.
    _cancel_fill_watcher(u.id, coin)
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
        # H-5 (2026-06-12): wenn der NEUE SL NUR wegen would-trigger-Race abgelehnt
        # wurde (Mark hat sich zwischen Engine-Preflight und dem 2. Mark-Read in
        # place_protection bewegt, nachdem der alte SL schon gecancelt war), die
        # Position NICHT market-closen — den vorherigen (per Ratchet sichereren)
        # Schutz aus dem managed_trade wiederherstellen.
        if skip_reason and "would_trigger" in skip_reason:
            sl_old, tps_old = _load_protection_params(u.id, coin)
            if sl_old is not None:
                reprot = await asyncio.to_thread(trader.place_protection, coin, is_buy, abs(pos), sl_old, tps_old)
                if reprot.get("sl_ok"):
                    _log_activity(u.id, "update",
                                  f"{coin}: SL-Update {sig.stop_loss} würde sofort triggern (Mark-Race) → "
                                  f"vorheriger Schutz (SL {sl_old}) wiederhergestellt, Position NICHT geschlossen.")
                    return
            _log_activity(u.id, "error",
                          f"{coin}: SL-Update would-trigger + Wiederherstellung fehlgeschlagen — Position evtl. "
                          f"UNGESCHÜTZT, bitte manuell prüfen (NICHT auto-geschlossen).")
            return
        # 2026-06-12 (Review #4): close_position-Resultat prüfen statt blind
        # "geschlossen" zu loggen — bei Fail ist die Position OFFEN und der alte
        # Schutz schon weggecancelt.
        close_res = await asyncio.to_thread(trader.close_position, coin)
        if not close_res.get("ok"):
            # Audit H-A (2026-06-12): Row mit SL-Params OFFEN lassen (sync-Loop +
            # Coverage-Reconciler beobachten weiter) + ungethrottleder Alert.
            _save_managed(u.id, coin, sig, status="open")
            msg = (f"NOTFALL: Emergency-Close fehlgeschlagen — Position offen & ungeschützt! "
                   f"{coin}: SL-Update fehlgeschlagen UND Market-Close fehlgeschlagen "
                   f"({str(close_res.get('error') or close_res.get('raw'))[:120]}). "
                   f"Sofort manuell prüfen. HL (SL): {err_detail[:120]}")
            _post_alert(f"🚨 [user {u.id}] {msg}", key=None)
            _log_activity(u.id, "error", msg)
            return
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
            # 2026-06-12 (Review #0): unbekannter Positionsstatus = Abbruch. Vorher
            # las ein transienter API-Fehler pos=0 → cancel_orders riss die SL/TP
            # der real offenen Position weg + _close_managed versteckte die Row
            # vor dem Sync → nackte Position für immer.
            try:
                pos = await asyncio.to_thread(trader.position_size, coin)
            except Exception as e:
                _log_activity(user_id, "error",
                              f"{coin}: Positionsstatus nicht lesbar ({str(e)[:160]}) — "
                              f"CANCEL abgebrochen (keine Orders gecancelt, Schutz bleibt).")
                return

            if abs(pos) > 0:
                # Position OFFEN -> CANCEL ignorieren, SL/TP übernehmen den Exit.
                _log_activity(
                    user_id, "skip",
                    f"{coin}: CANCEL ignoriert — Position offen (qty {abs(pos):.6g}). "
                    f"Exit erfolgt über SL/TP."
                )
                return

            # Position FLAT -> evtl. ruhende Limit-Entry-Order canceln.
            # 2026-06-12 (Review #6): zugehörigen Fill-Watcher zuerst beenden.
            _cancel_fill_watcher(user_id, coin)
            n = await asyncio.to_thread(trader.cancel_orders, coin)
            if n > 0:
                _log_activity(user_id, "close", f"{coin}: pre-entry Limit gecancelt (n={n}) — These invalidiert")
            _close_managed(user_id, coin)
        except InvalidToken:
            # Audit M-4: siehe _open_or_update — undecryptbarer Key pausiert
            # den User statt pro Signal einen Traceback zu spammen.
            _pause_user_bad_key(user_id, "Fernet InvalidToken (decrypt failed)",
                                reason="decrypt_failed")
            return
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
async def _protect_when_filled(trader, user_id, sig, is_buy, tps, resting_oid=None):
    """H2-Fix (2026-06-09): Schutz NACH JEDEM neuen Fill nachlegen (Delta-Add),
    statt nur den ersten Teil-Fill zu schützen und sofort zu returnen.

    Bug-Historie: ein ruhender Limit-Entry kann in mehreren Partial-Fills über
    Sekunden füllen. Der alte Watcher feuerte beim ERSTEN Mini-Fill und sicherte
    nur diese Menge ab → der Rest blieb NACKT (BTC 2026-06-09: SL deckte 0.00138
    von 0.1151 BTC). Jetzt: wächst die Position über die bereits abgedeckte
    Größe, legen wir Schutz für die DELTA-Menge nach. reduce-only Stops/TPs
    stacken bei gleichem Trigger → die Summe deckt die volle Position; KEIN
    cancel → die ruhende Rest-Entry-Order bleibt unangetastet.
    """
    coin = coin_of(sig.ticker)
    deadline = time.monotonic() + config.ENTRY_FILL_TIMEOUT_S
    protected = 0.0   # bereits mit Schutz-Orders abgedeckte Positionsgröße
    stable = 0        # Anzahl Polls ohne neuen Fill (→ Order fertig gefüllt)

    def _protect_delta(psz):
        """Schutz für (psz - protected) nachlegen. Returnt (ok, fatal)."""
        delta = psz - protected
        prot = trader.place_protection(coin, is_buy, delta, sig.stop_loss, tps)
        if prot.get("sl_ok"):
            return True, False
        sl_resp = prot.get("sl"); err = str(sl_resp)[:300] if sl_resp else "no response"
        log.error("place_protection sl fail (watcher) user=%s coin=%s sl=%s delta=%s is_buy=%s resp=%s",
                  user_id, coin, sig.stop_loss, delta, is_buy, err)
        if _is_unauthorized_agent_response(err):
            _pause_user_bad_key(user_id, err, reason="not_authorized")
            return False, True
        # SL fehlgeschlagen → keine ungeschützte Position riskieren → schließen
        # 2026-06-12 (Review #4): Close-Resultat prüfen. Bei Fail Row OFFEN lassen
        # (SL-Params drin) damit der Coverage-Reconciler den Schutz nachlegen kann,
        # und NICHT "geschlossen" behaupten.
        close_res = trader.close_position(coin)
        if not close_res.get("ok"):
            _save_managed(user_id, coin, sig, status="open")
            msg = (f"NOTFALL: Emergency-Close fehlgeschlagen — Position offen & ungeschützt! "
                   f"{coin}: SL nach Fill fehlgeschlagen UND Market-Close fehlgeschlagen "
                   f"({str(close_res.get('error') or close_res.get('raw'))[:120]}). "
                   f"Sofort manuell prüfen. HL (SL): {err[:120]}")
            # Audit H-A: ungethrottleder Alert (key=None) — darf nie untergehen.
            _post_alert(f"🚨 [user {user_id}] {msg}", key=None)
            _log_activity(user_id, "error", msg)
            return False, True
        trader.cancel_orders(coin)
        _log_activity(user_id, "error", f"{coin}: SL nach Fill fehlgeschlagen — Position geschlossen. HL: {err[:180]}")
        _close_managed(user_id, coin)
        return False, True

    async def _exit_stale():
        """2026-06-12 (Review #6): Row gehört nicht mehr diesem Watcher (neueres
        Signal/CANCEL hat übernommen). Eigene Rest-Entry-Order best-effort canceln
        — eine herrenlose ruhende Order würde sonst später NACKT füllen — und
        beenden, ohne fremde Orders anzufassen."""
        if resting_oid:
            await asyncio.to_thread(trader.cancel_order, coin, resting_oid)
        log.info("fill-watcher %s/%s: von neuerem Signal überholt — beendet", user_id, coin)

    while time.monotonic() < deadline:
        await asyncio.sleep(config.ENTRY_POLL_S)
        # 2026-06-12 (Review #10): jede mutierende Iteration läuft unter dem
        # (user, coin)-Lock. Vorher racte der Watcher gegen _adjust/_cancel:
        # sein Delta-SL wurde vom _adjust-cancel weggefegt, Stops stapelten sich,
        # und im Worst-Case market-closte der Watcher-Fail-Pfad eine Position,
        # die _adjust gerade erfolgreich abgesichert hatte.
        async with _lock_for(user_id, coin):
            if not _watcher_row_current(user_id, coin, resting_oid, sig.signal_id):
                await _exit_stale()
                return
            try:
                psz = abs(await asyncio.to_thread(trader.position_size, coin))
            except Exception:
                # Review #0: Status unbekannt ≠ flat — diesen Poll überspringen.
                continue
            if psz > protected * 1.001:
                ok, fatal = await asyncio.to_thread(_protect_delta, psz)
                if fatal:
                    return
                if ok:
                    protected = psz
                    stable = 0
                    _save_managed(user_id, coin, sig, status="open")
                    _log_activity(user_id, "order", f"{coin} gefüllt (qty {psz:.6g}) — SL+TP voll abgedeckt")
            elif protected > 0:
                stable += 1
                if stable >= 2:   # 2 Polls keine neuen Fills → Order fertig gefüllt
                    # H-1 (2026-06-12): evtl. noch ruhenden Entry-Rest (Teil-Fill, Rest
                    # unbefüllt) canceln, damit er nicht SPÄTER nackt füllt. Nur die
                    # Entry-oid — Schutz-Orders bleiben.
                    if resting_oid:
                        await asyncio.to_thread(trader.cancel_order, coin, resting_oid)
                    return
    # Timeout: letzter Delta-Check (last-second fill in der allerletzten Iteration).
    async with _lock_for(user_id, coin):
        if not _watcher_row_current(user_id, coin, resting_oid, sig.signal_id):
            await _exit_stale()
            return
        try:
            psz_final = abs(await asyncio.to_thread(trader.position_size, coin))
        except Exception as e:
            # 2026-06-12 (Review #0): vorher wurde ein Read-Fehler hier als
            # psz_final=0 ("nie gefüllt") behandelt → cancel_orders + Row closed,
            # während ein echter Fill ungeschützt liegen konnte. Jetzt: nichts
            # anfassen, Row offen lassen — Startup-/Coverage-Reconciler übernimmt.
            _log_activity(user_id, "error",
                          f"{coin}: Fill-Watcher-Timeout, Positionsstatus nicht lesbar "
                          f"({str(e)[:120]}) — nichts gecancelt, Reconciler übernimmt.")
            return
        if psz_final > protected * 1.001:
            ok, fatal = await asyncio.to_thread(_protect_delta, psz_final)
            if fatal:
                return
            if ok:
                protected = psz_final
                _save_managed(user_id, coin, sig, status="open")
                _log_activity(user_id, "order", f"{coin}: last-second fill (qty {psz_final:.6g}) — SL+TP nachgesetzt")
        if protected > 0:
            # H-1 (2026-06-12): Watcher endet (Teil-/Voll-Fill) → ruhenden Entry-Rest
            # canceln, sonst füllt der Rest später NACKT (sync.py prüft keine Coverage).
            if resting_oid:
                await asyncio.to_thread(trader.cancel_order, coin, resting_oid)
            return
        # Nie gefüllt → ruhende Order canceln.
        await asyncio.to_thread(trader.cancel_orders, coin)
        _log_activity(user_id, "skip", f"{coin} nicht gefüllt — kein Trade")
        _close_managed(user_id, coin)
        # Audit LOW-1: es wurde NIE getradet → Dedup-Marke freigeben, damit ein
        # Re-Emit desselben signal_id wieder ausgeführt werden kann.
        _unmark_signal_done(user_id, sig.signal_id)


# ── H1: Startup-Reconciler ───────────────────────────────────────────────────
def _active_user_ids():
    db = SessionLocal()
    try:
        return [u.id for u in (db.query(User)
                .filter(User.bot_active.is_(True), User.hl_api_secret_enc != "").all())]
    finally:
        db.close()


async def reconcile_protection_on_startup():
    """H1 (2026-06-09): nach jedem (Re)Start prüfen, ob jede offene HL-Position
    eine reduce-only Stop-Order hat. Fehlt sie — typischer Fall: der Prozess
    starb im Fill-Fenster eines ruhenden Entries, bevor _protect_when_filled
    die Protection setzen konnte, und sync.py überspringt resting-Rows — ziehen
    wir den Schutz aus dem managed_trade nach.

    2026-06-12 (Review #1/#2): zusätzlich werden resting-Rows reconciled. Jeder
    Deploy restartet den Service und killt damit alle In-Memory-Fill-Watcher;
    die ruhende Limit-Order auf HL lebt aber weiter. Vorher gehörte sie danach
    NIEMANDEM mehr (Startup-Pass sah nur offene Positionen, sync.py skippt
    resting) — füllte sie Stunden später, lag eine gehebelte Position dauerhaft
    OHNE SL/TP da (Naked-Position-Klasse des BTC-Incidents 2026-06-09).

    Sicher by design: schließt NIE selbst (würde Verlust gegen User-Willen
    festnageln); kennt es keine SL-Params → lauter ERROR-Alert für manuellen
    Eingriff. Idempotent: tut nichts, wenn schon ein Stop existiert. Per
    STARTUP_PROTECTION_RECONCILE abschaltbar.
    """
    if not config.STARTUP_PROTECTION_RECONCILE:
        return
    user_ids = _active_user_ids()
    log.info("Startup-Reconciler: prüfe Schutz-Orders für %d aktive User", len(user_ids))
    await _rearm_resting_watchers(user_ids)
    await reconcile_stop_coverage(user_ids)


async def _rearm_resting_watchers(user_ids):
    """2026-06-12 (Review #1/#2): Fill-Watcher für resting managed_trades nach
    Neustart wieder aufsetzen — bevorzugt re-armen (gespeicherte resting_oid/
    SL/TPs), sonst (Re-Arm unmöglich: kein SL/Direction in der Row) die ruhende
    Entry-Order auf HL canceln und die Row schließen, damit sie nie nackt füllt."""
    if not user_ids:
        return
    db = SessionLocal()
    try:
        rows = (db.query(ManagedTrade)
                .filter(ManagedTrade.status == "resting",
                        ManagedTrade.user_id.in_(user_ids))
                .all())
        resting = [{
            "user_id": mt.user_id, "coin": mt.coin,
            "direction": (mt.direction or "").strip().upper(),
            "entry": float(mt.entry) if mt.entry is not None else None,
            "stop_loss": float(mt.stop_loss) if mt.stop_loss is not None else None,
            "take_profits": mt.take_profits or "",
            "resting_oid": mt.resting_oid,
            "signal_id": mt.signal_id or "",
        } for mt in rows]
    finally:
        db.close()
    if not resting:
        return
    log.info("Startup-Reconciler: %d resting-Row(s) — re-arme Fill-Watcher", len(resting))
    from app.parser import Signal, TakeProfit
    for r in resting:
        user_id, coin = r["user_id"], r["coin"]
        u = _get_user(user_id)
        if not u:
            continue
        try:
            trader = await _build_trader(u)
        except Exception as e:
            log.warning("rearm: trader build failed user %s: %s", user_id, e)
            _log_activity(user_id, "error",
                          f"{coin}: ruhender Entry nach Neustart, Trader nicht baubar "
                          f"({str(e)[:120]}) — Order evtl. noch live auf HL, bitte manuell prüfen.")
            continue
        try:
            if r["stop_loss"] is None or r["direction"] not in ("LONG", "SHORT"):
                # Re-Arm unmöglich → Entry canceln + Row schließen (sicherste Option).
                if r["resting_oid"]:
                    await asyncio.to_thread(trader.cancel_order, coin, r["resting_oid"])
                else:
                    await asyncio.to_thread(trader.cancel_orders, coin)
                _close_managed(user_id, coin)
                _log_activity(user_id, "error",
                              f"{coin}: ruhender Entry nach Neustart ohne SL/Direction in der DB — "
                              f"Order gecancelt + Row geschlossen (kein nackter Fill möglich).")
                continue
            tps_pairs = []
            if r["take_profits"]:
                try:
                    tps_pairs = [(float(px), float(pct)) for px, pct in json.loads(r["take_profits"])]
                except Exception:
                    tps_pairs = []
            sig = Signal(signal_id=r["signal_id"], ticker=coin, action="NEW_TRADE",
                         direction=r["direction"], entry=r["entry"], stop_loss=r["stop_loss"],
                         take_profits=[TakeProfit(percent=pct, price=px) for px, pct in tps_pairs])
            # Schon während der Downtime (teil)gefüllt? Review #0: Read-Fehler =
            # unbekannt → NICHT blind handeln, nur alerten.
            try:
                pos = await asyncio.to_thread(trader.position_size, coin)
            except Exception as e:
                _log_activity(user_id, "error",
                              f"{coin}: ruhender Entry nach Neustart, Positionsstatus nicht lesbar "
                              f"({str(e)[:120]}) — bitte manuell prüfen.")
                continue
            if abs(pos) > 0:
                # Gefüllt während down: Row auf 'open' (Sync übernimmt sie), Entry-Rest
                # canceln; der Coverage-Pass direkt im Anschluss legt fehlenden Schutz nach.
                _save_managed(user_id, coin, sig, status="open", resting_oid=r["resting_oid"])
                if r["resting_oid"]:
                    await asyncio.to_thread(trader.cancel_order, coin, r["resting_oid"])
                _log_activity(user_id, "update",
                              f"{coin}: ruhender Entry hat während des Neustarts gefüllt "
                              f"(qty {abs(pos):.6g}) — Row auf 'open', Coverage-Reconciler prüft Schutz.")
                continue
            is_buy = r["direction"] == "LONG"
            tps = [(tp.price, tp.percent / 100.0) for tp in sig.take_profits]
            # Row anfassen → updated_at refresht (Grace-Timer der sync-Fill-Detection
            # startet neu, kein Race gegen den frisch re-armierten Watcher).
            _save_managed(user_id, coin, sig, status="resting", resting_oid=r["resting_oid"])
            t = _spawn(_protect_when_filled(trader, user_id, sig, is_buy, tps, r["resting_oid"]))
            _register_fill_watcher(user_id, coin, t)
            _log_activity(user_id, "update",
                          f"{coin}: Fill-Watcher nach Neustart re-armiert "
                          f"(ruhender Entry @ {r['entry']}, SL {r['stop_loss']}).")
        except Exception as e:
            log.exception("rearm failed user %s coin %s: %s", user_id, coin, e)
            _log_activity(user_id, "error",
                          f"{coin}: Resting-Reconcile nach Neustart fehlgeschlagen ({str(e)[:120]}) — "
                          f"ruhende Order evtl. unbewacht, bitte manuell prüfen.")


async def reconcile_stop_coverage(user_ids=None):
    """Coverage-Check: covered_stop_size vs |szi| jeder offenen HL-Position;
    Unter-Deckung wird aus den managed_trade-Params nachgeschützt.

    2026-06-12 (Review #3): aus dem Startup-Only-Pfad extrahiert — läuft jetzt
    zusätzlich PERIODISCH aus sync.position_sync_loop. Vorher lief der Check nur
    einmal pro Prozess-Start: gappt der Preis durch den SL-Slippage-Cap, ist der
    Stop konsumiert und die Position offen-ohne-Schutz; config versprach 'bis
    Position-Sync sie aufpickt', aber der Sync verglich nie Coverage — ein
    wochenlang laufender Bot ließ die Position unbegrenzt nackt. Fehlschläge
    alerten über die error-Activity (→ _post_alert/ALERT_WEBHOOK).
    """
    if user_ids is None:
        user_ids = _active_user_ids()
    for user_id in user_ids:
        u = _get_user(user_id)
        if not u:
            continue
        try:
            trader = await _build_trader(u)
            positions = await asyncio.to_thread(trader.open_positions)
        except Exception as e:
            log.warning("reconcile: trader/positions failed user %s: %s", user_id, e)
            continue
        for p in positions:
            coin = coin_of(p.get("coin"))
            szi = p.get("szi") or 0.0
            if abs(szi) == 0:
                continue
            try:
                sz = abs(szi)
                covered = await asyncio.to_thread(trader.covered_stop_size, coin)
                if covered >= sz * 0.999:
                    continue  # voll per SL gedeckt — nichts zu tun
                uncovered = sz - covered
                sl, tps = _load_protection_params(user_id, coin)
                if sl is None:
                    _log_activity(
                        user_id, "error",
                        f"{coin}: Position qty {sz:.6g} nur zu {covered:.6g} per SL gedeckt, KEIN "
                        f"SL-Preis im managed_trade bekannt — bitte manuell absichern/schließen.")
                    continue
                is_buy = szi > 0
                # Schutz für die FEHLENDE Menge nachlegen (reduce-only stackt mit
                # vorhandenen Stops bei gleichem Trigger → Summe deckt die Position).
                prot = await asyncio.to_thread(trader.place_protection, coin, is_buy, uncovered, sl, tps)
                if prot.get("sl_ok"):
                    _log_activity(
                        user_id, "update",
                        f"{coin}: Reconciler hat Unter-Deckung gefixt — SL/TP für fehlende "
                        f"{uncovered:.6g} nachgelegt (war {covered:.6g}/{sz:.6g}, SL {sl}).")
                else:
                    _log_activity(
                        user_id, "error",
                        f"{coin}: Unter-Deckung ({covered:.6g}/{sz:.6g}) — Schutz nachlegen "
                        f"FEHLGESCHLAGEN ({str(prot.get('sl'))[:120]}). Manuell absichern!")
            except Exception as e:
                log.warning("reconcile: protect failed user %s coin %s: %s", user_id, coin, e)
