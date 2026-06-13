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


def _f(x, d=0.0):
    """Float-Coercion am Boundary (HL-Responses, Decimal-Spalten). None/Garbage → d."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return d

# 2026-06-08 Mainnet-Hardening A1/A2: Rate-Counter + Alert-Throttle.
# In-Memory (lost on restart, OK — counters reset = fail-open, safer than
# false-block; emergency-halt-flag persistent in file).
import os
_signal_timestamps: list = []                          # sliding-window aller Signal-Empfänge
_trade_intervals: dict = {}                            # (user_id, coin) -> last_trade_ts
_alert_throttle: dict = {}                             # (user_id, coin) -> last_alert_ts
# M-5 (2026-06-13): Reconciler-"secure manually"-Alert max 1× pro (user,coin) bis
# Zustandswechsel. Wert = letzter Alert-Grund-String; ändert sich der Grund (oder
# verschwindet die Position/Row), wird der Key gelöscht und erneut gealertet.
_reconcile_alerted: dict = {}                          # (user_id, coin) -> last_alert_reason


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
        # L-10 (2026-06-13): harte Obergrenze gegen unbeschränktes Wachstum
        # zwischen den periodischen TTL-Sweeps (_alert_throttle ist text-gekeyt).
        if len(_alert_throttle) > 5000:
            _alert_throttle.clear()
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
    # Discord-Latenz).
    # HOOK M-10 (2026-06-13): statt loop.run_in_executor(None, …) den DEDIZIERTEN
    # Alert-Executor (hl_retry.submit_alert) nutzen. Der default-Pool wird vom
    # blockierenden Trading-to_thread (inkl. schlafender hl_retry-Retries) geteilt;
    # ein Retry-Sturm belegte sonst alle Worker und Alerts kämen erst verspätet
    # raus (oder umgekehrt: ein Alert-Sturm hungert den Trading-Pfad aus).
    # submit_alert ist fire-and-forget, fängt post-shutdown-Fehler ab und fällt
    # bei Pool-Shutdown auf inline-Ausführung zurück.
    try:
        from app.hl_retry import submit_alert
        submit_alert(_send)
    except Exception as e:
        # Import/submit fehlgeschlagen → niemals den Caller crashen lassen,
        # best-effort inline senden.
        log.warning("alert webhook dispatch failed: %s", e)
        _send()


def prune_memory_dicts():
    """L-10 (2026-06-13): periodischer TTL-Sweep der In-Memory-Dicts. Vor allem
    _alert_throttle ist TEXT-gekeyt ((user_id, text[:30])) → unbeschränkte
    Kardinalität bei wechselnden Fehlertexten; über Wochen wächst das. Auch
    _trade_intervals/_recent_pause_keys/_reconcile_alerted akkumulieren tote
    (user,coin)-Keys. Wird periodisch aus reconcile_stop_coverage aufgerufen
    (alle ~5 Min). Konservativ: nur alte Einträge entfernen, nie laufende Locks/
    Watcher anfassen (die räumt ihr eigener done-callback auf)."""
    now = time.time()
    # _alert_throttle: Einträge älter als 10× ALERT_THROTTLE_S sind sicher tot.
    try:
        ttl = max(600.0, float(config.ALERT_THROTTLE_S) * 10)
    except Exception:
        ttl = 3600.0
    for d, age in ((_alert_throttle, ttl), (_trade_intervals, 24 * 3600.0),
                   (_recent_pause_keys, 3600.0)):
        try:
            for k in [k for k, ts in list(d.items()) if now - (ts or 0) > age]:
                d.pop(k, None)
        except Exception as e:
            log.warning("prune memory dict failed: %s", e)
    # _percoin_cache: (ts, stats)-Tupel — nach TTL droppen.
    try:
        pttl = float(getattr(config, "PERCOIN_CACHE_TTL_S", 600)) * 4
        for k in [k for k, v in list(_percoin_cache.items())
                  if isinstance(v, tuple) and now - (v[0] or 0) > pttl]:
            _percoin_cache.pop(k, None)
    except Exception as e:
        log.warning("prune percoin cache failed: %s", e)
    # _reconcile_alerted: harte Obergrenze (Reason-Strings, kein Timestamp) —
    # bei Überlauf komplett leeren (re-alert bei echter Unter-Deckung ist ok,
    # besser als unbegrenztes Wachstum).
    if len(_reconcile_alerted) > 5000:
        _reconcile_alerted.clear()


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


async def drain_tasks(timeout: float = 10.0):
    """H-4 (2026-06-13): alle laufenden Trade-Tasks + Fill-Watcher beim Shutdown
    sauber cancellen UND awaiten. Agent D ruft das im lifespan-Teardown VOR dem
    Prozess-Exit (nachdem der Halt-Flag gesetzt + der Listener gestoppt ist),
    damit ein Deploy keinen Trade-Task mitten im Fenster zwischen Entry-Ack und
    _save_managed killt (row-less naked-Order-Klasse).

    Vorgehen: erst die Watcher canceln (sie schlafen meist im Poll), dann alle
    _tasks; danach mit Timeout auf Beendigung warten. Best-effort — ein nicht
    fertig werdender Task blockiert den Shutdown nur bis `timeout`.

    Returns: Anzahl der Tasks, auf die gewartet wurde.
    """
    # Watcher-Tasks zuerst gezielt canceln (gehören auch zu _tasks, doppelt
    # cancel ist harmlos).
    for t in list(_fill_watchers.values()):
        if not t.done():
            t.cancel()
    pending = [t for t in list(_tasks) if not t.done()]
    for t in pending:
        t.cancel()
    if not pending:
        return 0
    try:
        await asyncio.wait(pending, timeout=timeout)
    except Exception as e:
        log.warning("drain_tasks wait failed: %s", e)
    still = [t for t in pending if not t.done()]
    if still:
        log.warning("drain_tasks: %d task(s) did not finish within %.1fs", len(still), timeout)
    return len(pending)


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
    # M-9 (2026-06-13): einen evtl. schon registrierten Watcher für (user,coin)
    # VOR dem Überschreiben canceln. Vorher überschrieb das Dict den Eintrag
    # stillschweigend — der alte Watcher lief unregistriert weiter (lief gegen
    # die NEUE Order, Doppel-Schutz). Tritt v.a. im Startup-Rearm auf, wenn ein
    # live Signal denselben (user,coin) parallel zum Rearm-Pass spawnt.
    old = _fill_watchers.get((user_id, coin))
    if old is not None and old is not task and not old.done():
        old.cancel()
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


def _save_managed(user_id, coin, sig, status, resting_oid=None,
                  entry_oid=None, entry_cloid=None, bot_filled_sz=None):
    # 2026-06-13 Audit C-4: entry_oid/entry_cloid/bot_filled_sz mitspeichern
    # (Ownership-Attribution). Alle drei sind None-tolerant: None = "diesen Wert
    # NICHT anfassen" (bestehende Spalte bleibt), damit z.B. ein SL-Update nicht
    # die beim Entry gespeicherte bot_filled_sz auf 0 zurücksetzt.
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
        # 2026-06-13 Audit C-4: Bot-Ownership-Felder.
        if entry_oid is not None:
            mt.entry_oid = str(entry_oid)
        if entry_cloid is not None:
            mt.entry_cloid = str(entry_cloid)
        if bot_filled_sz is not None:
            mt.bot_filled_sz = bot_filled_sz
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


def _close_superseded_resting(user_id, coin):
    """H-3 (2026-06-13): eine durch ein neues NEW_TRADE überholte, noch
    'resting' Row (deren Bot-Entry-Order gerade gecancelt wurde) sauber
    schließen UND ihren Dedup-Mark freigeben — sonst bliebe eine Zombie-Row
    (tote oid, kein Watcher) liegen, die der sync-Loop später auf 'open' flippt
    (H-2). Nur 'resting'-Rows; eine 'open'-Row (live Position) NICHT anfassen."""
    db = SessionLocal()
    old_signal_ids = []
    try:
        rows = (db.query(ManagedTrade)
                .filter(ManagedTrade.user_id == user_id, ManagedTrade.coin == coin,
                        ManagedTrade.status == "resting").all())
        for mt in rows:
            if mt.signal_id:
                old_signal_ids.append(mt.signal_id)
            mt.status = "closed"
        db.commit()
    except Exception as e:
        log.warning("close superseded resting failed user=%s coin=%s: %s", user_id, coin, e)
        db.rollback()
    finally:
        db.close()
    # Dedup der alten Generation freigeben (außerhalb der obigen Session, eigener
    # commit) — ein Re-Emit des ALTEN signal_id bleibt dann handelbar.
    for sid in old_signal_ids:
        _unmark_signal_done(user_id, sid)


def _resting_row_for(user_id, coin):
    """M-4 (2026-06-13): liefert die aktuelle 'resting' Row (Bot-Entry ruht, noch
    nicht gefüllt) für (user, coin) oder None. Dient dazu, ein UPDATE_TRADE bei
    pos==0 als 'modify-pending' zu erkennen statt als 'keine Position → skippen'."""
    db = SessionLocal()
    try:
        return (db.query(ManagedTrade)
                .filter(ManagedTrade.user_id == user_id, ManagedTrade.coin == coin,
                        ManagedTrade.status == "resting")
                .order_by(ManagedTrade.id.desc()).first() is not None)
    except Exception as e:
        log.warning("resting row check failed user=%s coin=%s: %s", user_id, coin, e)
        return False
    finally:
        db.close()


def _modify_pending(user_id, coin, sig):
    """M-4 (2026-06-13): ein UPDATE_TRADE trifft eine NOCH RUHENDE Entry-Order
    (Position pos==0, aber resting-Row + lebender Watcher). Bot 1's Routine
    'NEW_TRADE, 30s später SL-tighten' lief vorher in den 'UPDATE ohne Position'-
    Zweig: der schloss die resting-Row → der Watcher sah seine Row weg
    (_watcher_row_current=False) und cancelte den Entry → der pending Trade war
    tot, signal_id dedup-verbrannt.

    Fix: NUR die Row-Parameter (SL/TP) aktualisieren — Entry-Order + Watcher
    bleiben unberührt. Der laufende Watcher liest seine SL/TPs allerdings aus dem
    Signal, mit dem er gespawnt wurde, NICHT aus der Row; deshalb deckt der
    Watcher den ersten Fill noch mit dem ALTEN SL ab — der periodische Coverage-
    Reconciler + ein evtl. späteres _adjust (sobald die Position offen ist) ziehen
    den neuen, getighteten SL aus der Row nach. KONSERVATIV: ein getighteter SL
    geht so spätestens beim ersten Reconcile-Pass auf die offene Position; der
    pending Entry wird NIE gekillt. resting_oid/entry_oid/cloid/bot_filled_sz
    bleiben unangetastet (None = nicht anfassen).

    SL-Ratchet auch hier: ein UPDATE, das den SL LOCKERT (mehr Risiko), wird NICHT
    in die Row geschrieben (gleiche Schutz-Logik wie _adjust auf offener Position)
    — sonst könnte ein looser Backfill-UPDATE den pending Trade verschlechtern."""
    if sig.stop_loss is not None:
        current_sl, db_direction = _get_current_sl(user_id, coin)
        if current_sl is not None and db_direction in ("LONG", "SHORT"):
            is_buy = db_direction == "LONG"
            loosens = ((is_buy and sig.stop_loss < current_sl)
                       or (not is_buy and sig.stop_loss > current_sl))
            if loosens:
                _log_activity(
                    user_id, "update",
                    f"{coin}: pending SL-update {sig.stop_loss} rejected (would increase "
                    f"risk vs resting SL {current_sl}, {db_direction}) — resting entry kept as-is.")
                return
    _save_managed(user_id, coin, sig, status="resting")


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
    from sqlalchemy.exc import IntegrityError
    db = SessionLocal()
    try:
        db.add(ProcessedSignal(user_id=user_id, signal_id=signal_id))
        db.commit()
    except IntegrityError:
        db.rollback()   # UNIQUE-Verletzung = schon vorhanden, ok (erwartet)
    except Exception as e:
        # L-14 (2026-06-13): NICHT-UNIQUE-DB-Fehler NICHT mehr still schlucken.
        # Ein geschluckter Mark heißt: der Dedup-Eintrag fehlt → ein Replay
        # desselben Signals kann DOPPELT traden. Mindestens lautstark loggen
        # (Alert hängt nicht dran, da das hier auch im Erfolgsfall pro Trade
        # läuft — aber ERROR-Level macht es in journald sichtbar).
        db.rollback()
        log.error("mark_signal_done failed (NON-unique DB error) user=%s sig=%s: %s — "
                  "dedup entry MISSING, replay could double-trade", user_id, signal_id, e)
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


def _adjust_dedup_key(action_type, signal_id):
    """HOOK M-20 (2026-06-13): namespaced Dedup-Key für UPDATE/CANCEL-Replays.
    Nutzt dieselbe ProcessedSignal-Tabelle wie NEW (überlebt Restart), aber mit
    Prefix, damit ein UPDATE-Mark NIE den NEW-Dedup desselben signal_id berührt
    (und umgekehrt). None, wenn kein signal_id → kein Dedup möglich."""
    if not signal_id:
        return None
    prefix = "upd" if (action_type or "").upper() == "UPDATE_TRADE" else "cxl"
    return f"{prefix}:{signal_id}"


def _adjust_already_applied(user_id, action_type, signal_id):
    """HOOK M-20: True, wenn dieses exakte UPDATE/CANCEL-signal_id für den User
    schon angewandt wurde (Backfill/Redelivery-Replay). KONSERVATIV: nur exakte
    Wiederholungen werden geblockt — ein legitimes, neues UPDATE (anderes
    signal_id) läuft immer durch. Bei DB-Fehler fail-open (lieber evtl. doppelt
    anpassen als ein legitimes UPDATE verlieren)."""
    key = _adjust_dedup_key(action_type, signal_id)
    if key is None:
        return False
    return _signal_already_done(user_id, key)


def _mark_adjust_applied(user_id, action_type, signal_id):
    """HOOK M-20: ein erfolgreich angewandtes UPDATE/CANCEL als erledigt
    markieren (namespaced)."""
    key = _adjust_dedup_key(action_type, signal_id)
    if key is not None:
        _mark_signal_done(user_id, key)


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
            except Exception as e:
                # L-14 (2026-06-13): malformed TP-JSON NICHT mehr still droppen —
                # der Caller würde sonst SL-only re-protecten (kein Profit-Taking)
                # ohne dass irgendwo sichtbar ist, dass die gespeicherten TPs
                # unbrauchbar waren. Mindestens WARN-loggen.
                log.warning("load_protection_params: malformed TP JSON user=%s coin=%s (%r): %s "
                            "— TPs dropped, re-protection will be SL-only",
                            user_id, coin, str(mt.take_profits)[:120], e)
                tps = []
        return (sl, tps)
    finally:
        db.close()


def _load_bot_ownership(user_id, coin):
    """2026-06-13 Audit C-4: liest die Bot-Attribution des letzten offenen
    managed_trade für (user, coin).

    Returns dict {entry_oid, entry_cloid, bot_filled_sz, known}:
      - entry_oid/entry_cloid: str|None — die vom Bot platzierte Entry-Order.
      - bot_filled_sz: float — vom Bot gefüllte Größe (>=0).
      - known: bool — True nur wenn die Row die NEUEN Ownership-Spalten gesetzt
        hat (bot_filled_sz IS NOT NULL). False = Pre-Ownership-Row (vor dieser
        Änderung angelegt) ODER keine Row → Caller MUSS konservativ fallback'en
        (nie fremde Orders/Größe zerstören).
    """
    db = SessionLocal()
    try:
        mt = (db.query(ManagedTrade)
              .filter(ManagedTrade.user_id == user_id, ManagedTrade.coin == coin,
                      ManagedTrade.status != "closed")
              .order_by(ManagedTrade.id.desc()).first())
        if mt is None:
            return {"entry_oid": None, "entry_cloid": None, "bot_filled_sz": 0.0, "known": False}
        bfs = getattr(mt, "bot_filled_sz", None)
        known = bfs is not None
        try:
            bot_sz = float(bfs) if bfs is not None else 0.0
        except (TypeError, ValueError):
            bot_sz = 0.0
        return {
            "entry_oid": mt.entry_oid or mt.resting_oid,   # entry_oid bevorzugt, sonst alte resting_oid
            "entry_cloid": getattr(mt, "entry_cloid", None),
            "bot_filled_sz": bot_sz,
            "known": known,
        }
    except Exception as e:
        # Konservativ: bei DB-Fehler "unbekannt" → Caller fällt auf den
        # sicheren Alt-Pfad (nichts Fremdes anfassen).
        log.warning("load bot ownership failed user=%s coin=%s: %s", user_id, coin, e)
        return {"entry_oid": None, "entry_cloid": None, "bot_filled_sz": 0.0, "known": False}
    finally:
        db.close()


def _has_open_bot_row(user_id, coin):
    """2026-06-13 Audit C-4/L-3: existiert eine nicht-geschlossene managed_trade-
    Row für (user, coin)? Dient als Gate für den Legacy-cancel_orders-Sweep —
    ohne Bot-Row gibt es nichts von uns zu canceln, also keine fremden Orders
    anfassen."""
    db = SessionLocal()
    try:
        return (db.query(ManagedTrade)
                .filter(ManagedTrade.user_id == user_id, ManagedTrade.coin == coin,
                        ManagedTrade.status != "closed").first() is not None)
    except Exception as e:
        log.warning("has open bot row check failed user=%s coin=%s: %s", user_id, coin, e)
        return False
    finally:
        db.close()


def _close_succeeded(close_res):
    """2026-06-13 Audit C-1/C-4: close_position liefert nach C-1 jetzt
    {"ok":bool, "closed":float, "still_open":float, ...}. Erfolg = ok==True UND
    (still_open fehlt ODER ≈0). Vorher reichte top-level ok — ein IoC-Close, der
    in einem gappenden Markt NICHT matchte, meldete ok aber ließ die Position
    offen. still_open ist die autoritative Prüfung.

    Rückwärtskompatibel: hat das dict (noch) kein still_open (alte close_position-
    Version / Agent A noch nicht gemerged), zählt ok allein."""
    if not isinstance(close_res, dict):
        return False
    if not close_res.get("ok"):
        return False
    still = close_res.get("still_open")
    if still is None:
        return True   # alte Form ohne still_open → ok allein
    try:
        return abs(float(still)) < 1e-12 or abs(float(still)) <= 1e-9
    except (TypeError, ValueError):
        return True


def _tp_status_ok(trader, raw):
    """M-12 (2026-06-13): True, wenn eine einzelne TP-Order-Antwort akzeptiert
    wurde. Nutzt trader._status_ok (gleiche Logik wie SL); fehlt die Methode (Stub
    in Tests), heuristik: dict mit status=='ok' und kein 'error' im ersten status.
    Bei Unsicherheit (unbekannte Form) konservativ True — wir wollen NICHT
    fälschlich 'TP reject' melden und damit Alert-Lärm erzeugen."""
    checker = getattr(trader, "_status_ok", None)
    if callable(checker):
        try:
            return bool(checker(raw))
        except Exception:
            return True
    try:
        if not isinstance(raw, dict):
            return True
        if raw.get("status") != "ok":
            return False
        st = raw["response"]["data"]["statuses"][0]
        return "error" not in st
    except Exception:
        return True


def _check_tp_results(trader, user_id, coin, prot):
    """M-12 (2026-06-13): die TP-Ladder-Ergebnisse aus place_protection (prot['tp'],
    Liste roher Order-Antworten) prüfen. Vorher wurde NUR sl_ok geprüft — eine
    abgelehnte TP-Order (z.B. would-trigger, tickSize, min-notional) verschwand
    still und die Position lief SL-only (kein Profit-Taking). Bei ≥1 Reject: eine
    error-Activity, damit der User es im Dashboard/Alert sieht. Returnt die Anzahl
    abgelehnter TPs (0 = alles gut)."""
    tp_results = (prot or {}).get("tp") or []
    if not tp_results:
        return 0
    rejected = sum(0 if _tp_status_ok(trader, r) else 1 for r in tp_results)
    if rejected:
        _log_activity(
            user_id, "error",
            f"{coin}: {rejected} of {len(tp_results)} take-profit order(s) were rejected "
            f"by the exchange — position may run SL-only (no profit-taking). "
            f"Check/secure take-profits manually.")
    return rejected


def _close_fail_detail(close_res):
    """Kurzer Fehlertext aus einem close_position-Resultat für Alerts/Activity."""
    if not isinstance(close_res, dict):
        return str(close_res)[:120]
    still = close_res.get("still_open")
    base = str(close_res.get("error") or close_res.get("raw") or "")[:120]
    if still is not None:
        try:
            if abs(float(still)) > 1e-9:
                return f"still_open={still} (close did not fully fill); {base}"[:160]
        except (TypeError, ValueError):
            pass
    return base


async def _cancel_bot_entry(trader, coin, entry_oid):
    """2026-06-13 Audit C-4 / L-3: NUR die Bot-Entry-Order (per oid) canceln,
    nie pauschal alle Orders des Coins (das löschte manuelle User-Orders).
    Kennen wir keine oid (Pre-Ownership-Row ohne resting_oid, oder sofort-fill
    ohne Rest-Order), passiert NICHTS — fail-safe, kein Sweep. cancel_order_oid
    (Agent A) schluckt 'already filled' selbst."""
    if not entry_oid:
        return
    try:
        # Agent A: cancel_order_oid(coin, oid) -> {"ok", "already_filled", "raw"}
        await asyncio.to_thread(trader.cancel_order_oid, coin, entry_oid)
    except AttributeError:
        # Fallback falls cancel_order_oid (Agent A) noch nicht gemerged ist:
        # cancel_order(coin, oid) existiert bereits und ist ebenfalls oid-gezielt.
        try:
            await asyncio.to_thread(trader.cancel_order, coin, entry_oid)
        except Exception as e:
            log.warning("cancel_bot_entry fallback failed coin=%s oid=%s: %s", coin, entry_oid, e)
    except Exception as e:
        log.warning("cancel_bot_entry failed coin=%s oid=%s: %s", coin, entry_oid, e)


def _bot_managed_size(pos, ownership):
    """2026-06-13 Audit C-4: wie viel der LIVE-Position (abs(pos)) der Bot
    managen/market-closen darf.

    - Row kennt Ownership (known=True): min(bot_filled_sz, abs(pos)). Liegt die
      Live-Position ÜBER bot_filled_sz, gehört der Überschuss dem User (manuell
      dazu-getradet) → in Ruhe lassen.
    - Pre-Ownership-Row (known=False): konservativer Fallback = abs(pos) (alte
      Logik), ABER der Caller cancelt Orders nur gezielt per oid, nie pauschal.
    """
    live = abs(float(pos or 0))
    if ownership.get("known"):
        return max(0.0, min(float(ownership.get("bot_filled_sz") or 0.0), live))
    return live


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


def _revalidate_user_after_lock(user_id):
    """M-8 (2026-06-13): User-Row + Halt-Flag NACH dem Lock-Acquire neu prüfen.

    Die User-Row wird VOR den Locks gesnapshottet (handle_signal → _get_user).
    Steht der Task danach in der Lock-Queue, kann zwischenzeitlich ein Admin den
    Bot pausieren/halten ODER der User einen neuen Agent-Key speichern — der
    queue-wartende Task liefe sonst mit ALTEM Key/Settings los und gegen den
    inzwischen geäußerten Pause-Willen. Nach dem Lock: User frisch laden,
    bot_active + globalen Halt erneut prüfen.

    Returns (ok: bool, fresh_user|None):
      - ok=False → abbrechen (pausiert/gehaltet/weg). Caller emittiert KEINE
        Activity (kein Lärm — Pause/Halt sind erwartete Zustände, schon geloggt).
      - ok=True  → fresh_user ist die neu geladene Row (aktueller Key/Settings).
    """
    if _emergency_halt_active():
        log.info("user %s: EMERGENCY_HALT became active while queued — aborting task", user_id)
        return False, None
    fresh = _get_user(user_id)
    if not fresh or not fresh.bot_active or not fresh.hl_api_secret_enc:
        log.info("user %s: paused/removed while queued for lock — aborting task", user_id)
        return False, None
    return True, fresh


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
                "Bot paused: Agent-Key is valid but NOT authorized as an ExtraAgent "
                "on Hyperliquid (HL: 'User or API Wallet does not exist'). "
                "In the HL UI → API → click 'Approve API Wallet' for the existing agent, "
                "OR generate a new agent and save it in the dashboard. Then re-activate the bot manually."
            )
        elif reason == "decrypt_failed":
            text = (
                "Bot paused: stored Agent-Key could not be decrypted "
                "(decryption failed — ENCRYPTION_KEY changed or data corrupted). "
                "Please re-enter the Agent-Key in the dashboard, "
                "then re-activate the bot yourself."
            )
        else:
            text = (
                "Agent-Key invalid (looks like a 42-character address, "
                "but the 66-character Agent-Key is expected). Bot paused. "
                "Save the correct Agent-Key again in the dashboard, then re-activate yourself."
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


def _drawdown_from_losses(user_addr: str, lookback_s: float = 30 * 24 * 3600.0):
    """M-3 (2026-06-13): Summe der realisierten VERLUSTE (negatives closedPnl) aus
    den HL-Fills der letzten `lookback_s`. Dient dazu, einen Equity-Rückgang als
    'durch Trading-Verluste erklärt' (→ Drawdown-Cap greift zu Recht) vs. 'durch
    Auszahlung/Transfer' (→ Drawdown-Cap würde den Bot fälschlich dauerhaft
    pausieren) zu unterscheiden.

    Returns (total_loss: float >=0, ok: bool). ok=False bei Read-Fehler →
    Caller bleibt beim sicheren Default (pausieren), wir senken NIE blind den Cap
    auf einen ungelesenen Stand."""
    try:
        from app.hyperliquid_exec import get_info
        info = get_info(config.HL_TESTNET)
        fills = info.user_fills(user_addr) or []
    except Exception as e:
        log.warning("drawdown-loss read failed for %s: %s", user_addr[:8], e)
        return 0.0, False
    cutoff_ms = (time.time() - lookback_s) * 1000.0
    total_loss = 0.0
    for f in fills:
        try:
            t = float(f.get("time", 0) or 0)
            if t < cutoff_ms:
                continue
            pnl = float(f.get("closedPnl", 0) or 0)
        except (TypeError, ValueError):
            continue
        if pnl < 0:
            total_loss += -pnl
    return total_loss, True


def _maybe_reset_peak_on_withdrawal(user_id, user_addr, peak, balance):
    """M-3 (2026-06-13): KONSERVATIVER Peak-Reset. Liegt der aktuelle account_value
    deutlich unter dem historischen peak, aber die realisierten Trading-VERLUSTE
    erklären den Rückgang NICHT (Differenz stammt aus einer Auszahlung/Transfer),
    dann den peak auf den aktuellen Stand zurücksetzen, statt den Bot dauerhaft
    per Drawdown-Cap zu pausieren.

    Sicher by design: senkt den peak NUR, wenn die Trading-Verluste < 50% des
    Equity-Rückgangs decken (klares Withdrawal-Signal). Bei Read-Fehler ODER wenn
    Verluste den Drop plausibel erklären: NICHTS tun (Cap greift weiter — die
    sichere Richtung). Returnt den (evtl. neuen) peak.

    ⚠️ PRECONDITION: wird NUR aufgerufen, wenn der Account FLAT ist (Aufrufstelle
    prüft trader.open_positions_count()==0). balance enthält unrealisierte PnL —
    bei offener Position würde ein echter Drawdown sonst als Auszahlung
    fehlgedeutet (fail-open). Nicht ohne den Flat-Guard aufrufen."""
    drop = peak - balance
    if drop <= 0:
        return peak
    loss, ok = _drawdown_from_losses(user_addr)
    if not ok:
        return peak  # Read-Fehler → konservativ: Cap nicht entschärfen
    # Verluste erklären den Großteil des Drops → echter Drawdown, Cap behalten.
    if loss >= drop * 0.5:
        return peak
    # Withdrawal-Signal: Drop ist überwiegend NICHT durch Verluste erklärt.
    _db = SessionLocal()
    try:
        uu = _db.get(User, user_id)
        if uu:
            uu.peak_account_value = balance
            _db.commit()
        log.info("M-3: peak reset for user %s (peak $%.2f → $%.2f; drop $%.2f, realized losses "
                 "$%.2f ⇒ withdrawal, not loss)", user_id, peak, balance, drop, loss)
    except Exception as e:
        log.warning("M-3 peak reset failed user %s: %s", user_id, e)
        _db.rollback()
    finally:
        _db.close()
    return balance


def _recent_stopout_cooldown(user_addr: str, coin: str):
    """M-24 (2026-06-13): True, wenn der letzte realisierte VERLUST-Fill (closedPnl<0)
    auf diesem Coin jünger als POST_STOPOUT_COOLDOWN_S ist → ein direktes Re-Entry
    nach einem SL-Stop-out würde sonst nur den generischen 60s-Throttle abwarten
    und in dasselbe Whipsaw-Setup zurückspringen (Fee-Bleed). Verhindert das,
    ohne eine separate State-Schreibung zu brauchen: wir lesen die HL-Fills (wie
    der Per-Coin-Filter), prüfen den jüngsten Loss-Close auf diesem Coin.

    config-Default via getattr (Agent D legt POST_STOPOUT_COOLDOWN_S evtl. an;
    0/None = aus). Liefert (in_cooldown: bool, remaining_s: float). Read-Fehler
    → (False, 0): fail-open, lieber ein evtl. zu frühes Re-Entry als ein
    legitimes Signal dauerhaft blocken."""
    cooldown_s = float(getattr(config, "POST_STOPOUT_COOLDOWN_S", 900) or 0)
    if cooldown_s <= 0:
        return False, 0.0
    try:
        from app.hyperliquid_exec import get_info
        info = get_info(config.HL_TESTNET)
        fills = info.user_fills(user_addr) or []
    except Exception as e:
        log.warning("stop-out cooldown: HL fetch failed for %s: %s", user_addr[:8], e)
        return False, 0.0
    now_ms = time.time() * 1000.0
    last_loss_ms = 0.0
    for f in fills:
        if coin_of(f.get("coin")) != coin:
            continue
        try:
            pnl = float(f.get("closedPnl", 0) or 0)
        except (TypeError, ValueError):
            continue
        if pnl >= 0:
            continue
        t = float(f.get("time", 0) or 0)
        if t > last_loss_ms:
            last_loss_ms = t
    if last_loss_ms <= 0:
        return False, 0.0
    elapsed_s = (now_ms - last_loss_ms) / 1000.0
    if elapsed_s < cooldown_s:
        return True, max(0.0, cooldown_s - elapsed_s)
    return False, 0.0


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
    is_update = action == "UPDATE_TRADE"
    # 2026-06-08 Mainnet-Hardening A1: Signal-Rate-Cap. Bei Überschreitung →
    # auto-Halt + Discord-Alert + dieses Signal wird verworfen.
    # L-1 (2026-06-13): NUR NEW_TRADE-Intents zählen zum Rate-Cap. Vorher zählten
    # auch UPDATE/CANCEL (und Backfill-Replays) — ein Discord-Blip mit vielen
    # UPDATE/CANCEL-Replays konnte den persistenten Auto-Halt auslösen, der dann
    # sogar risiko-REDUZIERENDE UPDATEs blockt bis manuell gecleart. Der Cap zielt
    # auf Entry-STORM-Fees → nur NEW_TRADE belastet ihn. (Backfill-Exemption
    # bräuchte ein Flag aus discord_listener — siehe Notes; UPDATE/CANCEL-Exempt
    # nimmt schon den Großteil der Backfill-Last raus.)
    if action == "NEW_TRADE" and not _signal_rate_check():
        log.error("Signal %s %s VERWORFEN — auto-halt triggered (rate exceeded)",
                  action, coin_of(sig.ticker))
        return
    # Confidence-Gate (Phase 1: vorher nur Einstiege).
    # Low-confidence CANCEL hat zuvor offene Positionen mid-trade geschlossen
    # und Verluste festgenagelt — eine der Haupt-Verlustquellen.
    # L-2 (2026-06-13): UPDATE_TRADE vom Confidence-Gate AUSNEHMEN. Ein UPDATE ist
    # i.d.R. risiko-REDUZIEREND (SL nachziehen); ein low-confidence SL-Tighten
    # (conf 0.6) wurde vorher unsichtbar verworfen und die Position behielt den
    # weiteren Stop. NEW (Risiko aufmachen) + CANCEL (schließt pre-entry) bleiben
    # gated.
    if (not is_update) and sig.confidence is not None and sig.confidence < config.MIN_CONFIDENCE:
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
    # HOOK M-18 (2026-06-13): der Parser legt falsch-seitige TakeProfits (TP auf
    # der falschen Seite des Entry) in sig.dropped_tps ab statt sie still zu
    # verwerfen. Sind welche dabei, EINE klare info-Activity pro User emittieren,
    # damit der Nutzer im Dashboard sieht, dass TPs verworfen wurden (vorher nur
    # eine Log-Zeile serverseitig).
    dropped = getattr(sig, "dropped_tps", None) or []
    n_dropped = len(dropped)
    for uid in user_ids:
        if n_dropped:
            _log_activity(
                uid, "info",
                f"{coin}: {n_dropped} take-profit(s) dropped: wrong side of entry "
                f"(direction {sig.direction or '?'}) — ignored for this signal.")
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
            # M-8 (2026-06-13): nach dem Lock-Acquire User + Halt revalidieren —
            # Admin-Pause/Halt/Wallet-Save während der Task in der Queue stand darf
            # nicht mit altem Key/Settings durchlaufen. fresh = aktuelle Row.
            ok, fresh = _revalidate_user_after_lock(user_id)
            if not ok:
                return
            u = fresh
            trader = await _build_trader(u)
            if not trader.is_tradable(coin):
                _log_activity(user_id, "skip", f"{coin}: not tradable on Hyperliquid — skipped")
                return
            # 2026-06-12 (Review #0): position_size raised jetzt bei API-Fehlern
            # statt 0.0 zu liefern. Unbekannter Positionsstatus = sicherer Abbruch
            # — NIEMALS "flat" annehmen (sonst: cancel der live SL/TP + Doppel-Entry
            # auf eine offene Position).
            try:
                pos = await asyncio.to_thread(trader.position_size, coin)
            except Exception as e:
                _log_activity(user_id, "error",
                              f"{coin}: position status unreadable ({str(e)[:160]}) — "
                              f"{action_type} aborted (not assumed 'flat', nothing changed).")
                return
            if abs(pos) > 0:
                # H-2 (2026-06-12): Gegenrichtungs-Signal auf offener Position NICHT
                # blind in _adjust laufen lassen — _adjust leitet is_buy aus der HL-
                # Position ab und würde SL/TP für die FALSCHE Seite setzen, alle TPs
                # verwerfen und die DB-Richtung korrumpieren. Bei Richtungs-Mismatch:
                # ablehnen + alerten, Position + Schutz bleiben unberührt.
                sig_dir = (sig.direction or "").upper()
                if sig_dir in ("LONG", "SHORT") and (sig_dir == "LONG") != (pos > 0):
                    # M-15 (2026-06-13): die These ist invalidiert (Gegenrichtung).
                    # Ein noch RUHENDER Entry-Remainder (resting_oid) würde sonst bis
                    # zu 300s weiterfüllen und die Position in der ALTEN, jetzt
                    # widerlegten Richtung vergrößern. Deshalb: laufenden Fill-Watcher
                    # beenden + den Bot-Entry-Remainder gezielt per oid canceln
                    # (Position + bestehender Schutz bleiben unberührt — kein Flip,
                    # keine falsche Protection-Seite). Kennen wir keine oid, passiert
                    # nichts (fail-safe).
                    _cancel_fill_watcher(user_id, coin)
                    _mm_own = _load_bot_ownership(user_id, coin)
                    _mm_oid = _mm_own.get("entry_oid")
                    if _mm_oid:
                        await _cancel_bot_entry(trader, coin, _mm_oid)
                        _clear_resting_oid(user_id, coin)
                    _log_activity(
                        user_id, "skip",
                        f"{coin}: {sig_dir} signal contradicts open "
                        f"{'LONG' if pos > 0 else 'SHORT'} position — update rejected "
                        f"(no flip, no wrong protection side). Existing protection kept; "
                        f"any resting entry remainder cancelled.")
                    return
                # HOOK M-20 (2026-06-13): UPDATE-Replay-Dedup. Backfill/Redelivery
                # kann denselben UPDATE_TRADE doppelt zustellen — _adjust würde dann
                # Orders unnötig churnen (cancel+re-place) bzw. eine out-of-order
                # tighter→looser Sequenz triggern. NUR exakte Wiederholungen
                # (gleiches signal_id) blocken; ein legitimes neues UPDATE läuft
                # durch (anderes signal_id). NEW-über-offener-Position NICHT
                # deduppen (kein action_type-Filter darauf — es hat keinen
                # verlässlichen Replay-Marker und ist selten).
                if action_type == "UPDATE_TRADE" and _adjust_already_applied(
                        user_id, action_type, sig.signal_id):
                    _log_activity(user_id, "skip",
                                  f"{coin}: UPDATE_TRADE signal_id {sig.signal_id} already "
                                  f"applied — replay skipped (no order churn).")
                    return
                await _adjust(trader, u, sig, pos)        # Position offen -> SL/TP nachziehen
                if action_type == "UPDATE_TRADE":
                    _mark_adjust_applied(user_id, action_type, sig.signal_id)
            else:
                if action_type == "UPDATE_TRADE":
                    # M-4 (2026-06-13): trifft das UPDATE einen noch RUHENDEN Entry
                    # (resting-Row + lebender Watcher, pos==0), ist es ein
                    # 'modify-pending' (Bot-1-Routine: NEW, kurz darauf SL-tighten) —
                    # NICHT als 'Position weg → aufräumen' behandeln. Nur die
                    # Row-Params (mit Ratchet) updaten; Entry + Watcher bleiben.
                    if _resting_row_for(user_id, coin):
                        _modify_pending(user_id, coin, sig)
                        _log_activity(user_id, "update",
                                      f"{coin}: UPDATE_TRADE applied to resting (unfilled) entry — "
                                      f"SL/TP params updated, pending order kept.")
                        _mark_adjust_applied(user_id, action_type, sig.signal_id)
                        return
                    _log_activity(user_id, "skip",
                                  f"{coin}: UPDATE_TRADE without open position — not re-opened "
                                  f"(original position likely closed manually or via SL).")
                    _close_managed(user_id, coin)         # DB-State aufräumen
                    return
                # 2026-06-12 (Review #19): Throttle HIER (nur echte Neueröffnungen),
                # und VOR cancel_orders — ein gedrosseltes Replay darf den ruhenden
                # Entry + Watcher des Originals nicht wegräumen.
                if not _trade_interval_ok(user_id, coin):
                    log.info("Skip NEW_TRADE %s for user %s — min-trade-interval not elapsed (%ds)",
                             coin, user_id, config.MIN_TRADE_INTERVAL_S)
                    _log_activity(user_id, "skip",
                                  f"{coin}: trade-interval throttle active (min {config.MIN_TRADE_INTERVAL_S}s), skip.")
                    return
                # 2026-06-12 (Review #6): alten Fill-Watcher VOR dem Cancel beenden,
                # sonst hält er den Fill des NEUEN Entries für seinen und setzt
                # SL/TP des alten Signals.
                _cancel_fill_watcher(user_id, coin)
                # 2026-06-13 Audit C-4 / H-3 / L-3: NUR die alte Bot-Entry-Order
                # (per oid) canceln statt cancel_orders(coin) — letzteres löschte
                # auch manuelle ruhende Orders des Users.
                #   - tracked oid vorhanden  → gezielt diese eine Order canceln.
                #   - keine oid, aber eine bestehende (Pre-Ownership-)Bot-Row für
                #     den Coin → Fallback auf den alten Sweep (Legacy-Verhalten,
                #     reiner Bot-Account angenommen).
                #   - GAR keine Bot-Row → NICHTS canceln (fail-safe: keine
                #     manuellen Orders anfassen, es gibt nichts von uns).
                _old_own = _load_bot_ownership(user_id, coin)
                _old_oid = _old_own.get("entry_oid")
                if _old_oid:
                    await _cancel_bot_entry(trader, coin, _old_oid)
                elif _has_open_bot_row(user_id, coin):
                    await asyncio.to_thread(trader.cancel_orders, coin)
                # H-3 (2026-06-13): die superseded resting-Row JETZT sauber schließen
                # (ihre Order ist gerade gecancelt). Vorher cancelte dieser Pfad die
                # alte Order, lief dann in _open_new — skippte dort ein Gate
                # (per-coin-Filter/Margin/min-notional/set_leverage), kam die Row als
                # ZOMBIE 'resting' (tote oid, kein Watcher) zurück und der sync-Loop
                # flippte sie später fälschlich auf 'open' (H-2). Schließen + Dedup
                # der ALTEN Generation freigeben; _open_new legt bei Erfolg eine
                # frische Row an.
                _close_superseded_resting(user_id, coin)
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
            # M-23 (2026-06-13): rohen Exception-Text NICHT in die user-facing
            # Activity/den Discord-Alert f-stringen (kann SDK-Payload/interne
            # Details durchsickern). Detail nur ins log.*; dem User eine kurze,
            # generische EN-Meldung.
            log.exception("user %s %s: %s", user_id, coin, e)
            _log_activity(user_id, "error",
                          f"{coin}: internal error while processing the signal — skipped. "
                          f"Check server logs / contact support if this persists.")
        except Exception as e:
            log.exception("user %s %s: %s", user_id, coin, e)
            _log_activity(user_id, "error",
                          f"{coin}: internal error while processing the signal — skipped. "
                          f"Check server logs / contact support if this persists.")


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
                      f"{coin}: signal without valid direction ({sig.direction!r}) — "
                      f"NEW_TRADE skipped (not guessed as SHORT).")
        return

    # C3 (2026-06-09): Replay-Dedup. Wurde dieses signal_id für den User schon
    # erfolgreich ausgeführt (persistent, überlebt Restart) → nicht erneut
    # öffnen. Schützt gegen Doppel-Entries durch re-emittierte Signale nach
    # Neustart, wenn der In-Memory-Throttle weg ist.
    if sig.signal_id and _signal_already_done(u.id, sig.signal_id):
        _log_activity(u.id, "skip",
                      f"{coin}: signal_id {sig.signal_id} already executed — replay skipped")
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
            f"win-rate {stats['win_rate']*100:.0f}% over {stats['trades']} trades "
            f"< {config.PERCOIN_MIN_WINRATE*100:.0f}% threshold — NEW_TRADE skipped"
        )
        return

    # M-24 (2026-06-13): Post-Stop-Out-Cooldown. Nach einem SL-Stop-out (jüngster
    # realisierter Loss-Fill auf diesem Coin) NICHT sofort nach 60s wieder rein —
    # das gleiche Whipsaw-Setup würde nur Fees bluten. UPDATE/CANCEL laufen weiter
    # (nur NEW_TRADE-Pfad). config POST_STOPOUT_COOLDOWN_S (Default via getattr).
    in_cd, remaining = await asyncio.to_thread(_recent_stopout_cooldown, u.hl_account_address, coin)
    if in_cd:
        _log_activity(
            u.id, "skip",
            f"{coin}: post-stop-out cooldown active (~{remaining/60:.0f} min left after the "
            f"last losing exit) — NEW_TRADE skipped to avoid whipsaw re-entry.")
        return

    try:
        balance = await asyncio.to_thread(trader.account_value)
    except Exception as e:
        # 2026-06-04 audit-fix (B-#9): HL-Outage explizit unterscheiden von
        # "kein Geld" damit Beobachter (Discord-Alert/Admin-Dashboard) sieht
        # was wirklich los ist. Kein Trade-Open ohne verifiziertes Balance.
        from app.hyperliquid_exec import HLOutageError
        if isinstance(e, HLOutageError):
            _log_activity(u.id, "error", f"{coin}: HL Info-API down — trade-open aborted ({e})")
        else:
            _log_activity(u.id, "error", f"{coin}: account_value error ({e}) — trade-open aborted")
        return
    if balance <= 0:
        _log_activity(u.id, "skip", f"{coin}: no tradable balance")
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
            # M-3 (2026-06-13): vor dem Pausieren prüfen, ob der Equity-Rückgang
            # durch eine AUSZAHLUNG (nicht durch Trading-Verluste) entstanden ist.
            # Wenn ja, den peak konservativ auf den aktuellen Stand zurücksetzen
            # und neu schwellen — sonst pausierte eine Auszahlung >30% den Bot für
            # immer.
            # 🔴 M-3-FAIL-OPEN-FIX (2026-06-13, Verify-Runde): Der Reset darf NUR
            # feuern, wenn der Account FLAT ist (keine offene HL-Position). balance
            # = account_value() enthält UNREALISIERTE PnL; bei einer offenen, tief
            # im Minus stehenden Position liefert _drawdown_from_losses ~$0
            # (nichts realisiert) → ein ECHTER Drawdown würde als Auszahlung
            # fehlgedeutet und der Schutz-Cap aufgehoben. Nur im flachen Zustand
            # ist balance = realisierte Equity und das Withdrawal-Signal valide.
            # Read-Fehler / offene Position → konservativ KEIN Reset, Cap greift.
            is_flat = False
            try:
                is_flat = (await asyncio.to_thread(trader.open_positions_count)) == 0
            except Exception:
                is_flat = False
            if is_flat:
                peak = _maybe_reset_peak_on_withdrawal(u.id, u.hl_account_address, peak, balance)
                threshold = peak * (1 - max_dd)
        if balance < threshold:
            _log_activity(
                u.id, "error",
                f"🚨 MAX-DRAWDOWN-CAP: balance ${balance:.2f} < threshold ${threshold:.2f} "
                f"(peak ${peak:.2f}, cap {max_dd:.0%}). Bot paused. Review your trades, "
                f"adjust risk_pct/leverage, then re-activate yourself."
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

    # HOOK M-2 (2026-06-13): open_positions_count RAISED jetzt bei Read-Fehler
    # (statt 0). Ein API-Blip darf den max-open-Gate NICHT fail-OPEN durchwinken
    # (sonst zusammen mit M-1 beide Portfolio-Caps gleichzeitig aus). Raise =
    # unbekannte Positionszahl → fail-CLOSED: Entry sauber abbrechen + Activity.
    try:
        open_pos = await asyncio.to_thread(trader.open_positions_count)
    except Exception as e:
        _log_activity(u.id, "error",
                      f"{coin}: open-position count unreadable ({str(e)[:160]}) — "
                      f"NEW_TRADE aborted (max-open cap can't be verified).")
        return
    if open_pos >= u.max_open_positions:
        _log_activity(u.id, "skip", f"{coin}: max positions reached ({open_pos}/{u.max_open_positions})")
        return

    # C1 (2026-06-09, korrigiert): Aggregat-Margin-Cap gegen die GESAMT-Equity.
    # util = 1 - frei/equity. WICHTIG: NICHT marginSummary-Ratio — dessen
    # accountValue ist im Unified-Account nur die Perps-Seite und bei viel freiem
    # Spot viel kleiner als die Gesamt-Equity (zeigte 75% statt echter ~50%).
    # `balance` = account_value() = Gesamt-Equity; `avail` wird unten im Margin-
    # Pre-Check wiederverwendet (nur EIN HL-Read).
    # M-1 (2026-06-13): available_margin-Read-Fehler war bisher fail-OPEN
    # (avail = balance ⇒ util = 0 ⇒ Util-Cap UND der Margin-Pre-Check weiter
    # unten wurden beide übersprungen). Ein einziger fehlgeschlagener Read ließ
    # damit einen Entry durch, der die 85%-Auslastung gesprengt hätte. Jetzt:
    # fail-CLOSED — wir kennen die freie Margin nicht, also Entry abbrechen.
    try:
        avail = await asyncio.to_thread(trader.available_margin)
    except Exception as e:
        _log_activity(u.id, "error",
                      f"{coin}: available margin unreadable ({str(e)[:160]}) — "
                      f"NEW_TRADE aborted (margin/utilization cap can't be verified).")
        return
    util = (1.0 - avail / balance) if balance > 0 else 0.0
    if util >= config.MAX_MARGIN_UTILIZATION:
        _log_activity(
            u.id, "skip",
            f"{coin}: margin utilization {util*100:.0f}% ≥ cap {config.MAX_MARGIN_UTILIZATION*100:.0f}% "
            f"(free ${avail:.0f} of ${balance:.0f}) — no new entry. NEW_TRADE skipped clean")
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
        _log_activity(u.id, "error", f"{coin}: auto-leverage failed ({e}) — skipping")
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
        lev_reason = f"{lev_reason} — clamped to asset-max {asset_max}x"
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
                f"(with buffer ${needed:.2f}), available ${avail:.2f}. "
                f"Fund your HL account. NEW_TRADE skipped clean"
            )
            return
    plan = size_trade(account_value=balance, capital_cap=u.capital_cap_usdc, risk_pct=u.risk_pct,
                      entry=sig.entry, stop_loss=sig.stop_loss, leverage=chosen_lev)
    if plan.notional < config.MIN_NOTIONAL_USDC:
        _log_activity(u.id, "skip", f"{coin}: notional {plan.notional:.2f} < {config.MIN_NOTIONAL_USDC}")
        return

    # H-16 (2026-06-13): absoluter Notional-Cap pro Trade. risk%×eff und
    # eff×leverage bounden NUR die aggregierte/relative Exposure; ein
    # tight-SL + high-confidence Signal kann trotzdem nahe Asset-Max hebeln
    # und EINE Position = balance×asset_max aufmachen. Cap aus config
    # (Agent D legt MAX_NOTIONAL_PER_TRADE an); 0/None = aus. Über dem Cap:
    # SKIPPEN (sicherer als kürzen — gekürzt würde die Risk-Mathe/SL-Distanz
    # nicht mehr zur Größe passen) + Activity.
    cap_notional = getattr(config, "MAX_NOTIONAL_PER_TRADE", 50000)
    try:
        cap_notional = float(cap_notional or 0)
    except (TypeError, ValueError):
        cap_notional = 0.0
    if cap_notional > 0 and plan.notional > cap_notional:
        _log_activity(
            u.id, "skip",
            f"{coin}: position notional ${plan.notional:.0f} exceeds per-trade cap "
            f"${cap_notional:.0f} (MAX_NOTIONAL_PER_TRADE) — NEW_TRADE skipped (not trimmed).")
        return

    # H-9 (2026-06-13): min-Notional wurde bisher NUR auf plan.notional (vor dem
    # Rounding) geprüft. place_entry rundet die Menge per _round_sz (Agent A
    # floor't auf szDecimals) — eine ab-gerundete Menge kann unter den HL-Min-
    # Notional fallen (oder auf 0 runden) → HL-Reject als generischer
    # "Entry-Fehler". Hier die GE-rundete Menge bestimmen und Notional/Menge
    # erneut prüfen, bevor wir set_leverage/place_entry feuern.
    try:
        rounded_qty = float(trader._round_sz(coin, plan.qty))
    except Exception:
        rounded_qty = float(plan.qty)
    if rounded_qty <= 0:
        _log_activity(u.id, "skip",
                      f"{coin}: qty rounds to 0 at this price (size step) — NEW_TRADE skipped.")
        return
    rounded_notional = rounded_qty * float(sig.entry)
    if rounded_notional < config.MIN_NOTIONAL_USDC:
        _log_activity(
            u.id, "skip",
            f"{coin}: notional after size-rounding {rounded_notional:.2f} "
            f"< {config.MIN_NOTIONAL_USDC} — NEW_TRADE skipped clean.")
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
                      f"{coin}: set_leverage({chosen_lev}x) failed "
                      f"({str(lev_res)[:160]}) — entry skipped (no trade with unconfirmed leverage).")
        return
    _log_activity(u.id, "update", f"{coin}: {lev_reason}")
    entry = await asyncio.to_thread(trader.place_entry, coin, is_buy, plan.qty, sig.entry)
    if not entry["ok"]:
        entry_err = entry.get("error")
        # L-6 (2026-06-13): "User or API Wallet does not exist" auf dem ENTRY-Pfad
        # klassifizieren. Bisher pausierten nur die SL/Protection-Pfade auf diesen
        # Fehler — ein nicht-autorisierter/revoked Agent erzeugte am ENTRY dagegen
        # pro Signal einen neuen Error für immer. Jetzt: einmalig auto-pausieren.
        if _is_unauthorized_agent_response(entry_err):
            _pause_user_bad_key(u.id, str(entry_err)[:160], reason="not_authorized")
            return
        _log_activity(u.id, "error", f"Entry error {coin}: {entry_err}")
        return

    # 2026-06-13 Audit C-4: Bot-Ownership der gerade platzierten Entry-Order.
    # entry_oid = ruhende oid (resting) ODER bei sofort-fill None (keine Rest-
    # order); entry_cloid = client-id (Agent A gibt sie optional im Result
    # zurück; fehlt sie, bleibt None → Watcher pollt per oid). Wird in JEDEN
    # _save_managed-Aufruf unten gereicht.
    entry_oid = entry.get("resting_oid")
    entry_cloid = entry.get("entry_cloid") or entry.get("cloid")

    # H-4 (2026-06-13): Reihenfolge GEHÄRTET gegen Crash zwischen Entry-Ack und
    # Row-Save. Vorher wurde _mark_signal_done VOR dem _save_managed gesetzt —
    # ein Crash dazwischen ließ die Order ohne Row zurück (naked-fill-Klasse,
    # Reconciler findet keine SL-Params) UND verbrannte das signal_id dauerhaft
    # (Re-Emit blockiert). Jetzt: ERST die Row schreiben (status + entry_oid/
    # cloid), DANN markieren/stempeln. clear_strikes (H-6) ebenfalls erst nach
    # dem Save, damit eine evtl. wiederverwendete Row konsistent ist.
    from app.sync import clear_strikes

    if entry["filled"]:
        # 2026-06-13 Audit C-4 + H-1: bot_filled_sz aus der Entry-Antwort (was
        # DIESE Order füllte) — NICHT aus position_size (enthält manuelle Pos).
        sz = entry["filled_sz"] or plan.qty
        # H-4: Row + Ownership SCHREIBEN, bevor wir protecten — bei Crash in
        # place_protection existiert dann eine open-Row mit SL-Params, die der
        # Coverage-Reconciler aufgreifen kann (statt naked-ohne-Row).
        _save_managed(u.id, coin, sig, status="open",
                      entry_oid=entry_oid, entry_cloid=entry_cloid, bot_filled_sz=sz)
        _mark_signal_done(u.id, sig.signal_id)
        _record_trade_ts(u.id, coin)
        clear_strikes(u.id, coin)
        # H-5 (2026-06-13): place_protection in try/except. hl_retry kann nach
        # erschöpften/nicht-transienten Versuchen RAISEN statt sl_ok=False zu
        # liefern — das umging bisher JEDEN Notfallpfad (nur der sl_ok=False-
        # Return war behandelt). Eine Exception wird hier in DENSELBEN Fail-Pfad
        # geroutet wie sl_ok=False (prot = synthetisches sl_ok-False-dict).
        try:
            prot = await asyncio.to_thread(trader.place_protection, coin, is_buy, sz, sig.stop_loss, tps)
        except Exception as e:
            log.error("place_protection raised (new) user=%s coin=%s: %s", u.id, coin, e)
            prot = {"sl_ok": False, "sl": f"exception: {str(e)[:200]}", "skip_reason": None}
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
            # 2026-06-13 Audit C-4 + C-1: close_position liefert jetzt
            # {ok, closed, still_open, ...}. _close_ok prüft ok UND still_open≈0.
            close_res = await asyncio.to_thread(trader.close_position, coin)
            if not _close_succeeded(close_res):
                # Row mit SL-Params offen halten, damit der Coverage-Reconciler
                # (sync-Loop) den Schutz nachlegen kann.
                _save_managed(u.id, coin, sig, status="open")
                msg = (f"EMERGENCY: emergency-close failed — position open & unprotected! "
                       f"{coin}: SL after entry failed AND market-close failed "
                       f"({_close_fail_detail(close_res)}). "
                       f"Check manually immediately. HL (SL): {err_detail[:120]}")
                # Audit H-A (2026-06-12): UNGETHROTTLEDER Alert (key=None) — dieser
                # Fall darf NIE im (user,coin)-Throttle untergehen.
                _post_alert(f"🚨 [user {u.id}] {msg}", key=None)
                _log_activity(u.id, "error", msg)
                return
            # 2026-06-13 Audit C-4 / L-3: nur die Bot-Entry-Order gezielt
            # wegräumen statt pauschal cancel_orders (das löschte manuelle Orders
            # des Users). Nach erfolgreichem close ist die Position flat — ein evtl.
            # ruhender Bot-Entry-Rest wird per oid gecancelt; manuelle Orders bleiben.
            await _cancel_bot_entry(trader, coin, entry_oid)
            _close_managed(u.id, coin)
            _log_activity(u.id, "error",
                          f"{coin}: SL after entry failed — position closed. "
                          f"HL response: {err_detail[:180]}")
            return
        # M-12 (2026-06-13): TP-Ladder-Ergebnisse prüfen — abgelehnte TPs nicht
        # still schlucken (sonst SL-only ohne Profit-Taking).
        _check_tp_results(trader, u.id, coin, prot)
        _log_activity(u.id, "order", f"{sig.direction} {coin} opened (qty {sz:.6g}), SL+TP set")
        # bot_filled_sz erneut sichern (idempotent — der Wert ist bereits gesetzt).
        _save_managed(u.id, coin, sig, status="open",
                      entry_oid=entry_oid, entry_cloid=entry_cloid, bot_filled_sz=sz)
    else:
        # H-4: resting-Row inkl. entry_oid/cloid SCHREIBEN, bot_filled_sz=0
        # (noch nichts gefüllt). Erst danach markieren/Watcher.
        _save_managed(u.id, coin, sig, status="resting", resting_oid=entry_oid,
                      entry_oid=entry_oid, entry_cloid=entry_cloid, bot_filled_sz=0)
        _mark_signal_done(u.id, sig.signal_id)
        _record_trade_ts(u.id, coin)
        clear_strikes(u.id, coin)
        _log_activity(u.id, "order", f"{sig.direction} {coin}: limit resting @ {sig.entry}, waiting for fill")
        # 2026-06-12 (Review #6): Watcher registrieren, damit ein späteres Signal/
        # CANCEL ihn gezielt beenden kann (genau einer pro user+coin).
        t = _spawn(_protect_when_filled(trader, u.id, sig, is_buy, tps, entry_oid,
                                        entry_cloid=entry_cloid))
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
        _log_activity(u.id, "update", f"{coin}: update without new SL — existing protection kept")
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
            f"{coin}: direction flip detected (DB={db_direction}, HL={hl_direction}) — "
            f"SL-ratchet skipped, new SL {sig.stop_loss} will be set."
        )
        current_sl = None  # Ratchet ausschalten, sauber neu setzen
    if current_sl is not None:
        loosens = (is_buy and sig.stop_loss < current_sl) or (not is_buy and sig.stop_loss > current_sl)
        if loosens:
            _log_activity(
                u.id, "update",
                f"{coin}: SL-update {sig.stop_loss} rejected (would increase risk — "
                f"current SL {current_sl}, {hl_direction}). Existing protection kept."
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
    # H-13 (2026-06-13): mark==0 = all_mids-Read fehlgeschlagen. Vorher wurde der
    # would-trigger-Preflight dann STILL übersprungen (`if mark > 0`) und der
    # Adjust cancelte trotzdem den alten SL, um blind neu zu platzieren — ohne
    # den einen Client-Check, der instant-triggernde Stops verhindert. Jetzt:
    # bei mark==0 NICHT canceln/neu-setzen, alter Schutz bleibt aktiv + Alert.
    if mark <= 0:
        _log_activity(
            u.id, "error",
            f"{coin}: SL-update {sig.stop_loss} skipped — mark price unreadable "
            f"(all_mids failed); refusing to cancel & re-place protection blindly. "
            f"Existing protection kept.")
        return
    sl_invalid = (is_buy and sig.stop_loss >= mark) or ((not is_buy) and sig.stop_loss <= mark)
    if sl_invalid:
        side = "LONG" if is_buy else "SHORT"
        _log_activity(
            u.id, "update",
            f"{coin}: SL-update {sig.stop_loss} skipped (would trigger immediately — "
            f"{side} mark={mark}, rule: {'LONG SL<mark' if is_buy else 'SHORT SL>mark'}). "
            f"Existing protection unchanged."
        )
        return  # alter SL bleibt aktiv, kein cancel, keine Position-Close

    # 2026-06-13 Audit C-4: Ownership-Gate. _adjust cancelt unten ALLE Orders
    # des Coins und re-protectet/market-closet die VOLLE Live-Größe. Auf einem
    # Multi-User-Account kann der User aber selbst auf demselben Coin traden —
    # dann enthielte abs(pos) seine manuelle Position UND cancel_orders löschte
    # seinen manuellen SL. Deshalb:
    #   - Live-Position ≈ Bot-Anteil (known ownership): wie bisher voll managen.
    #   - Live-Position > Bot-Anteil (manuell dazu) ODER unbekannte Ownership
    #     bei >0 Bot-Anteil-Zweifel: NICHT destruktiv anfassen → skip + ERROR.
    ownership = _load_bot_ownership(u.id, coin)
    live_sz = abs(float(pos or 0))
    managed_sz = _bot_managed_size(pos, ownership)
    if ownership.get("known"):
        # Toleranz: 0.1% — Rundungen/Mini-Drift sind kein "manuell dazu".
        manual_extra = live_sz - managed_sz
        if managed_sz <= 0:
            # Bot hält (laut Attribution) nichts mehr hier, HL zeigt aber eine
            # Position → die ist manuell. Finger weg.
            _log_activity(
                u.id, "skip",
                f"{coin}: open position ({live_sz:.6g}) is not bot-attributed "
                f"(bot size 0) — UPDATE not applied, manual position left untouched.")
            return
        if manual_extra > managed_sz * 0.001 and manual_extra > 0:
            # Position ist GRÖSSER als der Bot-Anteil → User hat manuell dazu-
            # getradet. Ein cancel_orders + reprotect der vollen Größe würde
            # seinen manuellen SL canceln und seine Größe mit-managen. Fail-safe:
            # nichts zerstören, nur alerten (Bot-Anteil behält seinen Schutz).
            _log_activity(
                u.id, "error",
                f"{coin}: live position {live_sz:.6g} exceeds bot-attributed "
                f"{managed_sz:.6g} (manual position mixed in) — SL-update NOT applied "
                f"(won't touch your manual orders/size). Adjust manually if needed.")
            return

    # 2026-06-12 (Review #6): evtl. noch laufenden Fill-Watcher beenden — nach dem
    # cancel_orders gleich darunter existiert keine Rest-Entry-Order mehr, der
    # Watcher könnte nur noch Doppel-Schutz auf die frische Voll-Abdeckung stapeln.
    _cancel_fill_watcher(u.id, coin)
    # 2026-06-13 Audit C-4: protect_sz = der Bot-Anteil (managed_sz), nicht
    # blind abs(pos). Bei known ownership ist managed_sz == live_sz (oben
    # abgesichert); bei Pre-Ownership-Rows (known=False) Fallback auf live_sz
    # (alte Logik) — dort tracken wir keinen Bot-Anteil, gehen also vom reinen
    # Bot-Account aus wie vor dieser Änderung.
    protect_sz = managed_sz if ownership.get("known") else live_sz
    # 2026-06-13 Audit C-4 (Residual L-3): cancel_orders(coin) räumt hier die
    # ALTEN SL/TP des Bots weg, um sie mit dem neuen SL/TP zu ersetzen. Da oben
    # für known ownership ausgeschlossen ist, dass die Live-Position eine manuelle
    # POSITION enthält (sonst Skip), ist die zu cancelnde Protection bot-eigen.
    # Verbleibendes Restrisiko: hat der User parallel eine manuelle RUHENDE Order
    # (Limit) auf demselben Coin, während die Bot-Position offen ist, würde der
    # Sweep auch die treffen — gezieltes Canceln nur der Bot-Protection bräuchte
    # persistierte Protection-oids (haben wir nicht). Bewusst akzeptiert: ein
    # stehengelassener alter Bot-SL (Alternative) ist gefährlicher.
    await asyncio.to_thread(trader.cancel_orders, coin)            # alte SL/TP weg
    prot = await asyncio.to_thread(trader.place_protection, coin, is_buy, protect_sz, sig.stop_loss, tps)
    if not prot.get("sl_ok"):
        # Falls noch ein anderer Grund fail (tickSize/etc) — log + close-Net.
        sl_resp = prot.get("sl"); skip_reason = prot.get("skip_reason")
        err_detail = skip_reason or (str(sl_resp)[:300] if sl_resp else "no response")
        log.error("place_protection sl fail user=%s coin=%s sl=%s sz=%s is_buy=%s resp=%s",
                  u.id, coin, sig.stop_loss, protect_sz, is_buy, err_detail)
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
                # 2026-06-13 Audit H-5: place_protection im Restore-Pfad in
                # try/except — ein Raise hier (statt sl_ok=False) hätte sonst den
                # generischen Handler getroffen (nur Error-Log) und die Position
                # ungeschützt mit weggecanceltem alten SL zurückgelassen.
                try:
                    reprot = await asyncio.to_thread(trader.place_protection, coin, is_buy, protect_sz, sl_old, tps_old)
                except Exception as e:
                    log.error("restore place_protection raised user=%s coin=%s: %s", u.id, coin, e)
                    reprot = {"sl_ok": False}
                if reprot.get("sl_ok"):
                    _log_activity(u.id, "update",
                                  f"{coin}: SL-update {sig.stop_loss} would trigger immediately (mark race) → "
                                  f"previous protection (SL {sl_old}) restored, position NOT closed.")
                    return
            # M-14 (2026-06-13): would-trigger + restore failed = die Position hat
            # evtl. KEINEN Exit mehr (alter SL gecancelt, neuer abgelehnt, Restore
            # fehlgeschlagen) — das darf NIE im (user,coin)-Text-Throttle untergehen.
            # KONSERVATIV kein Auto-Market-Close (ein transienter Mark-Race würde
            # sonst eine gesunde Position auf den Markt werfen — H-13 fängt den
            # Mark-Read-Fail schon vorher ab); stattdessen UNGETHROTTLETER Alert.
            msg = (f"{coin}: SL-update would-trigger + restore failed — position possibly "
                   f"UNPROTECTED, please check manually NOW (NOT auto-closed).")
            _post_alert(f"🚨 [user {u.id}] {msg}", key=None)
            _log_activity(u.id, "error", msg)
            return
        # 2026-06-12 (Review #4): close_position-Resultat prüfen statt blind
        # "geschlossen" zu loggen — bei Fail ist die Position OFFEN und der alte
        # Schutz schon weggecancelt.
        # 2026-06-13 Audit C-4: NIE mehr als den Bot-Anteil schließen. Oben ist
        # für known ownership bereits sichergestellt, dass live_sz == bot-Anteil
        # (sonst hätten wir geskippt), close_position ist also bot-only. Bei
        # Pre-Ownership-Rows bleibt es beim alten Verhalten (reiner Bot-Account).
        close_res = await asyncio.to_thread(trader.close_position, coin)
        if not _close_succeeded(close_res):
            # Audit H-A (2026-06-12): Row mit SL-Params OFFEN lassen (sync-Loop +
            # Coverage-Reconciler beobachten weiter) + ungethrottleder Alert.
            _save_managed(u.id, coin, sig, status="open")
            msg = (f"EMERGENCY: emergency-close failed — position open & unprotected! "
                   f"{coin}: SL-update failed AND market-close failed "
                   f"({_close_fail_detail(close_res)}). "
                   f"Check manually immediately. HL (SL): {err_detail[:120]}")
            _post_alert(f"🚨 [user {u.id}] {msg}", key=None)
            _log_activity(u.id, "error", msg)
            return
        # 2026-06-13 Audit C-4 / L-3: nur die Bot-Entry-Order gezielt wegräumen.
        await _cancel_bot_entry(trader, coin, ownership.get("entry_oid"))
        _close_managed(u.id, coin)
        _log_activity(u.id, "error",
                      f"{coin}: SL-update failed → position closed. HL response: {err_detail[:180]}")
        return
    # M-12 (2026-06-13): TP-Ladder-Ergebnisse prüfen (auch beim Nachziehen).
    _check_tp_results(trader, u.id, coin, prot)
    _log_activity(u.id, "update", f"{coin}: thesis adjusted — SL {sig.stop_loss}, TP trailed (qty {protect_sz:.6g})")
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
            # HOOK M-20 (2026-06-13): CANCEL-Replay-Dedup. Backfill/Redelivery
            # kann denselben CANCEL_TRADE doppelt zustellen. KONSERVATIV: nur
            # exakte Wiederholungen (gleiches signal_id) skippen; ein neuer CANCEL
            # (anderes signal_id) läuft durch. Verhindert doppelte cancel-Calls /
            # Row-Closes auf Redelivery.
            if _adjust_already_applied(user_id, "CANCEL_TRADE", sig.signal_id):
                _log_activity(user_id, "skip",
                              f"{coin}: CANCEL_TRADE signal_id {sig.signal_id} already "
                              f"applied — replay skipped.")
                return
            # M-8 (2026-06-13): User nach dem Lock neu laden (frischer Key, falls der
            # User während der Queue-Wartezeit den Agent-Key getauscht hat). Globaler
            # Halt / entfernter User → abbrechen. Eine reine Pause (bot_active=False)
            # blockt den CANCEL bewusst NICHT — eine ruhende Pre-Entry-Order
            # aufzuräumen ist auch bei pausiertem Bot sicheres, gewolltes Verhalten.
            if _emergency_halt_active():
                log.info("user %s: EMERGENCY_HALT active — CANCEL aborted", user_id)
                return
            fresh = _get_user(user_id)
            if not fresh or not fresh.hl_api_secret_enc:
                log.info("user %s: removed/no key while queued — CANCEL aborted", user_id)
                return
            u = fresh
            trader = await _build_trader(u)
            # 2026-06-12 (Review #0): unbekannter Positionsstatus = Abbruch. Vorher
            # las ein transienter API-Fehler pos=0 → cancel_orders riss die SL/TP
            # der real offenen Position weg + _close_managed versteckte die Row
            # vor dem Sync → nackte Position für immer.
            try:
                pos = await asyncio.to_thread(trader.position_size, coin)
            except Exception as e:
                _log_activity(user_id, "error",
                              f"{coin}: position status unreadable ({str(e)[:160]}) — "
                              f"CANCEL aborted (no orders cancelled, protection kept).")
                return

            if abs(pos) > 0:
                # Position OFFEN -> CANCEL ignorieren, SL/TP übernehmen den Exit.
                _log_activity(
                    user_id, "skip",
                    f"{coin}: CANCEL ignored — position open (qty {abs(pos):.6g}). "
                    f"Exit happens via SL/TP."
                )
                return

            # Position FLAT -> evtl. ruhende Limit-Entry-Order canceln.
            # 2026-06-12 (Review #6): zugehörigen Fill-Watcher zuerst beenden.
            _cancel_fill_watcher(user_id, coin)
            # 2026-06-13 Audit C-4 / L-3: NUR die Bot-Entry-Order (per oid)
            # canceln, nicht pauschal cancel_orders(coin) — letzteres löschte
            # auch manuelle ruhende Orders des Users. Kennen wir keine oid
            # (Pre-Ownership-Row), passiert nichts (fail-safe, kein Sweep) und
            # wir räumen nur den DB-State auf.
            ownership = _load_bot_ownership(user_id, coin)
            entry_oid = ownership.get("entry_oid")
            if entry_oid:
                await _cancel_bot_entry(trader, coin, entry_oid)
                _log_activity(user_id, "close",
                              f"{coin}: pre-entry limit cancelled (bot order {entry_oid}) — thesis invalidated")
            else:
                _log_activity(user_id, "close",
                              f"{coin}: CANCEL — no bot entry order tracked, nothing cancelled "
                              f"(manual orders left untouched).")
            _close_managed(user_id, coin)
            # HOOK M-20: CANCEL angewandt → markieren (Replay-Schutz). Nur im
            # FLAT-Pfad — bei offener Position wird CANCEL ignoriert (oben schon
            # returned), ein späterer legitimer CANCEL nach SL/TP-Exit soll
            # weiter durchlaufen können.
            _mark_adjust_applied(user_id, "CANCEL_TRADE", sig.signal_id)
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
            # M-23 (2026-06-13): generische EN-Meldung an den User, Detail nur ins log.
            log.exception("cancel user %s %s: %s", user_id, coin, e)
            _log_activity(user_id, "error",
                          f"{coin}: internal error while cancelling — nothing changed. "
                          f"Check server logs / contact support if this persists.")
        except Exception as e:
            log.exception("cancel user %s %s: %s", user_id, coin, e)
            _log_activity(user_id, "error",
                          f"{coin}: internal error while cancelling — nothing changed. "
                          f"Check server logs / contact support if this persists.")


# ── Fill-Watch für ruhende Limit-Orders ──────────────────────────────────────
def _entry_filled_sz(trader, coin, resting_oid, entry_cloid):
    """2026-06-13 Audit H-1/C-4: wie viel DIESE Bot-Entry-Order gefüllt hat —
    via order_status(oid/cloid), NICHT via position_size (das enthielte auch
    manuelle Positionen des Users, die der Watcher dann fälschlich seinem Entry
    zuschriebe). Returnt (filled_sz: float, done: bool, ok: bool):
      - filled_sz: kumulativ von dieser Order gefüllte Menge (>=0).
      - done: True wenn die Order endgültig ist (filled|canceled) — kein Rest
        mehr zu erwarten.
      - ok: False wenn der Status nicht gelesen werden konnte (unknown/Exception)
        → Caller überspringt diesen Poll (NIE als 0/flat interpretieren).
    Fallback: hat der Trader (Agent A) noch kein order_status, lesen wir
    position_size (alte Semantik) — degradiert, aber nie crashend."""
    try:
        st = trader.order_status(oid=resting_oid, cloid=entry_cloid)
    except AttributeError:
        # Agent A's order_status noch nicht gemerged → alte Semantik.
        try:
            return abs(_f(trader.position_size(coin))), False, True
        except Exception:
            return 0.0, False, False
    except Exception as e:
        log.warning("order_status failed coin=%s oid=%s: %s", coin, resting_oid, e)
        return 0.0, False, False
    status = (st or {}).get("status", "unknown")
    filled = _f((st or {}).get("filled_sz"), 0.0)
    if status == "unknown":
        return filled, False, False
    done = status in ("filled", "canceled")
    return abs(filled), done, True


async def _protect_when_filled(trader, user_id, sig, is_buy, tps, resting_oid=None,
                               entry_cloid=None):
    """H2-Fix (2026-06-09): Schutz NACH JEDEM neuen Fill nachlegen (Delta-Add),
    statt nur den ersten Teil-Fill zu schützen und sofort zu returnen.

    2026-06-13 Audit H-1/C-4: der Watcher pollt jetzt order_status(oid/cloid)
    DIESES Entries und schützt NUR die von DIESEM Entry gefüllte Menge — nicht
    mehr jedes Wachstum von position_size (das attribuierte ein manuelles
    Dazu-Kaufen des Users dem Bot-Entry und market-closte es im Fail-Pfad).

    Bug-Historie: ein ruhender Limit-Entry kann in mehreren Partial-Fills über
    Sekunden füllen. Der alte Watcher feuerte beim ERSTEN Mini-Fill und sicherte
    nur diese Menge ab → der Rest blieb NACKT (BTC 2026-06-09: SL deckte 0.00138
    von 0.1151 BTC). reduce-only Stops/TPs stacken bei gleichem Trigger → die
    Summe deckt die volle Position; KEIN cancel → die ruhende Rest-Entry-Order
    bleibt unangetastet.
    """
    coin = coin_of(sig.ticker)
    deadline = time.monotonic() + config.ENTRY_FILL_TIMEOUT_S
    protected = 0.0   # bereits mit Schutz-Orders abgedeckte (Bot-)Fill-Größe
    stable = 0        # Anzahl Polls ohne neuen Fill (→ Order fertig gefüllt)
    entry_px = _f(sig.entry, 0.0)
    min_notional = float(getattr(config, "MIN_NOTIONAL_USDC", 10) or 10)

    def _delta_below_min(delta):
        """H-8: Delta-Notional < HL-Min? Dann KEINEN frischen Stop forcieren
        (HL lehnt sub-$10-Trigger ab) und vor allem NIE force-closen — die
        Mini-Menge dem Coverage-Reconciler / dem nächsten größeren Fill
        überlassen."""
        if entry_px <= 0 or delta <= 0:
            return False
        return (delta * entry_px) < min_notional

    def _protect_delta(target):
        """Schutz für (target - protected) nachlegen. Returnt (ok, fatal).

        2026-06-13 Audit H-8: ist das Delta sub-min, geben wir (False, False)
        zurück — KEIN frischer Stop, KEIN force-close. protected bleibt
        unverändert, der nächste Poll versucht es mit dem dann größeren
        kumulativen Fill erneut; bleibt es dauerhaft klein, deckt der
        Coverage-Reconciler den Rest (oder vergrößert den bestehenden Stop)."""
        delta = target - protected
        if delta <= 0:
            return True, False
        if _delta_below_min(delta):
            log.info("watcher %s/%s: delta %.6g (notional ~$%.2f) < min $%.0f — "
                     "kein frischer Stop, Reconciler übernimmt (H-8)",
                     user_id, coin, delta, delta * entry_px, min_notional)
            return False, False
        prot = trader.place_protection(coin, is_buy, delta, sig.stop_loss, tps)
        if prot.get("sl_ok"):
            return True, False
        sl_resp = prot.get("sl"); err = str(sl_resp)[:300] if sl_resp else "no response"
        log.error("place_protection sl fail (watcher) user=%s coin=%s sl=%s delta=%s is_buy=%s resp=%s",
                  user_id, coin, sig.stop_loss, delta, is_buy, err)
        if _is_unauthorized_agent_response(err):
            _pause_user_bad_key(user_id, err, reason="not_authorized")
            return False, True
        # 2026-06-13 Audit H-8 (Sicherheitsnetz): scheitert das Protect, OBWOHL
        # das Delta sub-min ist (Race), trotzdem NICHT force-closen — der bereits
        # geschützte gesunde Teil bliebe sonst mit-geschlossen. Reconciler-Pfad.
        if _delta_below_min(delta):
            log.warning("watcher %s/%s: sub-min delta protect rejected — kein force-close (H-8)",
                        user_id, coin)
            return False, False
        # SL fehlgeschlagen (echte Größe) → keine ungeschützte Position riskieren → schließen
        # 2026-06-12 (Review #4): Close-Resultat prüfen. Bei Fail Row OFFEN lassen
        # (SL-Params drin) damit der Coverage-Reconciler den Schutz nachlegen kann.
        # 2026-06-13 Audit C-4/C-1: _close_succeeded prüft still_open; nur die
        # Bot-Entry-Order gezielt canceln (kein cancel_orders-Sweep).
        close_res = trader.close_position(coin)
        if not _close_succeeded(close_res):
            _save_managed(user_id, coin, sig, status="open")
            msg = (f"EMERGENCY: emergency-close failed — position open & unprotected! "
                   f"{coin}: SL after fill failed AND market-close failed "
                   f"({_close_fail_detail(close_res)}). "
                   f"Check manually immediately. HL (SL): {err[:120]}")
            # Audit H-A: ungethrottleder Alert (key=None) — darf nie untergehen.
            _post_alert(f"🚨 [user {user_id}] {msg}", key=None)
            _log_activity(user_id, "error", msg)
            return False, True
        if resting_oid:
            trader_cancel = getattr(trader, "cancel_order_oid", None) or trader.cancel_order
            try:
                trader_cancel(coin, resting_oid)
            except Exception as e:
                log.warning("watcher cancel bot entry failed coin=%s oid=%s: %s", coin, resting_oid, e)
        _log_activity(user_id, "error", f"{coin}: SL after fill failed — position closed. HL: {err[:180]}")
        _close_managed(user_id, coin)
        return False, True

    async def _cancel_own_entry():
        """2026-06-13 Audit C-4/L-3: NUR die eigene Bot-Entry-Order (per oid)
        canceln — nie cancel_orders(coin) (würde manuelle Orders + die gerade
        gesetzten Schutz-Orders zerstören)."""
        if resting_oid:
            await _cancel_bot_entry(trader, coin, resting_oid)

    async def _exit_stale():
        """2026-06-12 (Review #6): Row gehört nicht mehr diesem Watcher (neueres
        Signal/CANCEL hat übernommen). Eigene Rest-Entry-Order best-effort canceln
        — eine herrenlose ruhende Order würde sonst später NACKT füllen — und
        beenden, ohne fremde Orders anzufassen."""
        await _cancel_own_entry()
        log.info("fill-watcher %s/%s: von neuerem Signal überholt — beendet", user_id, coin)

    async def _try_protect(target):
        """H-11 (2026-06-13): _protect_delta in try/except — ein Raise (hl_retry
        re-raise) hätte sonst den GANZEN Watcher-Task gekillt und die ruhende
        Rest-Entry-Order verwaist gelassen. Bei Exception: weiter pollen
        (return (False, False)), nicht den Watcher sterben lassen."""
        try:
            return await asyncio.to_thread(_protect_delta, target)
        except Exception as e:
            log.error("watcher _protect_delta raised user=%s coin=%s: %s — weiter pollen (H-11)",
                      user_id, coin, e)
            return False, False

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
            # H-1/C-4: NUR den Fill DIESER Entry-Order pollen (order_status).
            filled, order_done, ok_read = await asyncio.to_thread(
                _entry_filled_sz, trader, coin, resting_oid, entry_cloid)
            if not ok_read:
                # Status unbekannt ≠ flat — diesen Poll überspringen.
                continue
            if filled > protected * 1.001:
                ok, fatal = await _try_protect(filled)
                if fatal:
                    return
                if ok:
                    protected = filled
                    stable = 0
                    # C-4: bot_filled_sz mitschreiben (die vom Bot geschützte Menge).
                    _save_managed(user_id, coin, sig, status="open", bot_filled_sz=protected)
                    _log_activity(user_id, "order", f"{coin} filled (qty {filled:.6g}) — SL+TP covered")
                # ok==False + fatal==False = sub-min Delta (H-8): protected NICHT
                # erhöhen, nächster Poll versucht es mit größerem kumulativem Fill.
            elif order_done or protected > 0:
                stable += 1
                # H-7 (2026-06-13): die Order ist endgültig (filled|canceled) ODER
                # 2 Polls ohne neuen Fill → fertig. Vor dem Beenden ein FINALER
                # order_status-Read + Delta-Protect, falls in der Lücke zwischen
                # vorletztem Poll und Cancel doch noch gefüllt wurde.
                if order_done or stable >= 2:
                    f2, _d2, ok2 = await asyncio.to_thread(
                        _entry_filled_sz, trader, coin, resting_oid, entry_cloid)
                    if ok2 and f2 > protected * 1.001:
                        ok, fatal = await _try_protect(f2)
                        if not fatal and ok:
                            protected = f2
                            _save_managed(user_id, coin, sig, status="open", bot_filled_sz=protected)
                            _log_activity(user_id, "order",
                                          f"{coin}: final fill before close (qty {f2:.6g}) — SL+TP placed")
                        if fatal:
                            return
                    # H-1: evtl. noch ruhenden Entry-Rest canceln (Teil-Fill, Rest
                    # unbefüllt), damit er nicht SPÄTER nackt füllt. Nur die
                    # Entry-oid (cancel_order_oid) — Schutz-/manuelle Orders bleiben.
                    await _cancel_own_entry()
                    return
    # Timeout: letzter Delta-Check (last-second fill in der allerletzten Iteration).
    async with _lock_for(user_id, coin):
        if not _watcher_row_current(user_id, coin, resting_oid, sig.signal_id):
            await _exit_stale()
            return
        f_final, _df, ok_final = await asyncio.to_thread(
            _entry_filled_sz, trader, coin, resting_oid, entry_cloid)
        if not ok_final:
            # 2026-06-12 (Review #0): Read-Fehler ≠ "nie gefüllt" — nichts
            # anfassen, Row offen lassen → Startup-/Coverage-Reconciler übernimmt.
            _log_activity(user_id, "error",
                          f"{coin}: fill-watcher timeout, entry status unreadable "
                          f"— nothing cancelled, reconciler takes over.")
            return
        if f_final > protected * 1.001:
            ok, fatal = await _try_protect(f_final)
            if fatal:
                return
            if ok:
                protected = f_final
                _save_managed(user_id, coin, sig, status="open", bot_filled_sz=protected)
                _log_activity(user_id, "order", f"{coin}: last-second fill (qty {f_final:.6g}) — SL+TP placed")
        if protected > 0:
            # H-1: Watcher endet (Teil-/Voll-Fill) → ruhenden Entry-Rest canceln
            # (nur die Bot-oid), sonst füllt der Rest später NACKT.
            await _cancel_own_entry()
            return
        # Nie gefüllt → NUR die eigene ruhende Bot-Order canceln (C-4/L-3:
        # kein cancel_orders-Sweep, der manuelle Orders zerstören würde).
        await _cancel_own_entry()
        _log_activity(user_id, "skip", f"{coin} not filled — no trade")
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
    await _cancel_open_row_remainders(user_ids)
    await reconcile_stop_coverage(user_ids)


async def _cancel_open_row_remainders(user_ids):
    """M-13 (2026-06-13): nach einem Restart kann eine Row 'open' sein (Position
    lebt, Sync übernimmt sie) UND trotzdem noch einen lebenden Entry-REMAINDER
    tragen (resting_oid gesetzt — der ruhende Rest einer teilgefüllten Limit-
    Order). Der Rearm-Pass scannt nur status=='resting' → dieser Remainder gehört
    NIEMANDEM mehr: kein Watcher, und sync.py flippt offene Rows nicht. Füllte er
    Stunden später nach, wüchse die Position über den geschützten Bot-Anteil
    hinaus (naked-Delta ≤5 Min pro Fill bis der Coverage-Reconciler ihn aufpickt).

    KONSERVATIV: den ruhenden Entry-Remainder gezielt per oid canceln (kein
    cancel_orders-Sweep → manuelle Orders bleiben), dann resting_oid in der Row
    löschen, damit der Coverage-Reconciler den bereits gefüllten Bot-Anteil
    sauber absichert. Die Position selbst bleibt unberührt."""
    if not user_ids:
        return
    db = SessionLocal()
    try:
        rows = (db.query(ManagedTrade)
                .filter(ManagedTrade.status == "open",
                        ManagedTrade.resting_oid.isnot(None),
                        ManagedTrade.user_id.in_(user_ids))
                .all())
        open_rem = [{"user_id": mt.user_id, "coin": mt.coin,
                     "resting_oid": mt.resting_oid,
                     "entry_oid": getattr(mt, "entry_oid", None) or mt.resting_oid}
                    for mt in rows]
    finally:
        db.close()
    if not open_rem:
        return
    log.info("Startup-Reconciler: %d open-Row(s) mit Entry-Remainder — canceln (M-13)", len(open_rem))
    for r in open_rem:
        user_id, coin = r["user_id"], r["coin"]
        u = _get_user(user_id)
        if not u:
            continue
        try:
            trader = await _build_trader(u)
        except Exception as e:
            log.warning("M-13: trader build failed user %s: %s", user_id, e)
            continue
        try:
            async with _user_lock_for(user_id), _lock_for(user_id, coin):
                # Remainder gezielt canceln (C-4/L-3: nur die Bot-oid).
                await _cancel_bot_entry(trader, coin, r["entry_oid"] or r["resting_oid"])
                # resting_oid in der Row löschen, damit kein späterer Pass ihn
                # erneut für einen ruhenden Entry hält. _clear_resting_oid setzt
                # nur dieses Feld; Status/Ownership/SL bleiben.
                _clear_resting_oid(user_id, coin)
                _log_activity(user_id, "update",
                              f"{coin}: leftover resting entry remainder after restart cancelled "
                              f"(open position kept; coverage-reconciler verifies protection).")
        except Exception as e:
            log.warning("M-13: cancel open-row remainder failed user %s coin %s: %s", user_id, coin, e)


def _clear_resting_oid(user_id, coin):
    """M-13 (2026-06-13): resting_oid der jüngsten offenen Row für (user,coin) auf
    None setzen (Entry-Remainder wurde gecancelt). Andere Felder bleiben."""
    db = SessionLocal()
    try:
        mt = (db.query(ManagedTrade)
              .filter(ManagedTrade.user_id == user_id, ManagedTrade.coin == coin,
                      ManagedTrade.status != "closed")
              .order_by(ManagedTrade.id.desc()).first())
        if mt is not None:
            mt.resting_oid = None
            db.commit()
    except Exception as e:
        log.warning("clear resting_oid failed user=%s coin=%s: %s", user_id, coin, e)
        db.rollback()
    finally:
        db.close()


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
            # 2026-06-13 Audit C-4: Ownership-Felder mit re-armen, damit der
            # re-armierte Watcher per order_status(cloid/oid) pollt (H-1) und
            # nur den Bot-Anteil managt.
            "entry_oid": getattr(mt, "entry_oid", None) or mt.resting_oid,
            "entry_cloid": getattr(mt, "entry_cloid", None),
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
                          f"{coin}: resting entry after restart, trader not buildable "
                          f"({str(e)[:120]}) — order possibly still live on HL, please check manually.")
            continue
        # M-9 (2026-06-13): den mutierenden Teil pro Row unter dem (user,coin)-Lock
        # (user→coin-Ordering wie _open_or_update → kein Deadlock). Vorher lief der
        # Rearm lock-frei: ein live NEW-Signal für denselben (user,coin) konnte
        # parallel laufen, beide spawnten einen Watcher und _register_fill_watcher
        # überschrieb still (jetzt zusätzlich oben gecancelt). Body in einem Helper.
        try:
            async with _user_lock_for(user_id), _lock_for(user_id, coin):
                await _rearm_one_row(trader, user_id, coin, r, Signal, TakeProfit)
        except Exception as e:
            log.exception("rearm failed user %s coin %s: %s", user_id, coin, e)
            _log_activity(user_id, "error",
                          f"{coin}: resting reconcile after restart failed ({str(e)[:120]}) — "
                          f"resting order possibly unguarded, please check manually.")


async def _rearm_one_row(trader, user_id, coin, r, Signal, TakeProfit):
    """M-9 (2026-06-13): der mutierende Rearm-Körper für EINE resting-Row, unter
    dem (user,coin)-Lock des Callers ausgeführt. Aus _rearm_resting_watchers
    extrahiert, damit die Lock-Klammer nicht 60 Zeilen umbauen muss."""
    if r["stop_loss"] is None or r["direction"] not in ("LONG", "SHORT"):
        # Re-Arm unmöglich → Entry canceln + Row schließen (sicherste Option).
        if r["resting_oid"]:
            await asyncio.to_thread(trader.cancel_order, coin, r["resting_oid"])
        else:
            await asyncio.to_thread(trader.cancel_orders, coin)
        _close_managed(user_id, coin)
        _log_activity(user_id, "error",
                      f"{coin}: resting entry after restart without SL/direction in the DB — "
                      f"order cancelled + row closed (no naked fill possible).")
        return
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
                      f"{coin}: resting entry after restart, position status unreadable "
                      f"({str(e)[:120]}) — please check manually.")
        return
    if abs(pos) > 0:
        # Gefüllt während down: Row auf 'open' (Sync übernimmt sie), Entry-Rest
        # canceln; der Coverage-Pass direkt im Anschluss legt fehlenden Schutz nach.
        # 2026-06-13 Audit C-4: bot_filled_sz aus order_status der Bot-Entry-
        # Order (was DIESE Order füllte), nicht blind aus position_size
        # (könnte manuelle Position enthalten). Fallback: pos.
        bot_fill = abs(pos)
        ofill, _od, ofok = await asyncio.to_thread(
            _entry_filled_sz, trader, coin, r["resting_oid"], r["entry_cloid"])
        if ofok and ofill > 0:
            bot_fill = min(ofill, abs(pos))
        _save_managed(user_id, coin, sig, status="open", resting_oid=r["resting_oid"],
                      entry_oid=r["entry_oid"], entry_cloid=r["entry_cloid"],
                      bot_filled_sz=bot_fill)
        await _cancel_bot_entry(trader, coin, r["entry_oid"] or r["resting_oid"])
        _log_activity(user_id, "update",
                      f"{coin}: resting entry filled during the restart "
                      f"(bot qty {bot_fill:.6g}) — row set to 'open', coverage-reconciler checks protection.")
        return
    is_buy = r["direction"] == "LONG"
    tps = [(tp.price, tp.percent / 100.0) for tp in sig.take_profits]
    # Row anfassen → updated_at refresht (Grace-Timer der sync-Fill-Detection
    # startet neu, kein Race gegen den frisch re-armierten Watcher).
    _save_managed(user_id, coin, sig, status="resting", resting_oid=r["resting_oid"],
                  entry_oid=r["entry_oid"], entry_cloid=r["entry_cloid"], bot_filled_sz=0)
    t = _spawn(_protect_when_filled(trader, user_id, sig, is_buy, tps, r["resting_oid"],
                                    entry_cloid=r["entry_cloid"]))
    _register_fill_watcher(user_id, coin, t)
    _log_activity(user_id, "update",
                  f"{coin}: fill-watcher re-armed after restart "
                  f"(resting entry @ {r['entry']}, SL {r['stop_loss']}).")


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
    # L-10 (2026-06-13): periodischer TTL-Sweep der In-Memory-Dicts — dieser
    # Reconciler läuft alle ~5 Min aus dem sync-Loop, idealer Aufhänger.
    prune_memory_dicts()
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
            # M-5 (2026-06-13): NUR Coins mit einer nicht-geschlossenen managed-Row
            # reconcilen. Auf einem Multi-User-Account kann der User selbst auf
            # einem Coin traden, den der Bot nie angefasst hat — dann existiert
            # KEINE Row, _load_bot_ownership liefert known=False und der alte
            # Fallback (target=|szi|) hätte die manuelle Position als "Bot-Position
            # ohne SL-Params" behandelt und alle 5 Min "secure manually" gealertet
            # (Alert-Fatigue, vergräbt echte Naked-Alerts). Ohne Row → überspringen.
            if not _has_open_bot_row(user_id, coin):
                _reconcile_alerted.pop((user_id, coin), None)  # Zustand sauber
                continue
            try:
                # 2026-06-13 Review-Fix: Coverage-Read + place_protection unter
                # demselben (user,coin)-Lock wie Engine/Watcher. Seit der Loop
                # PERIODISCH läuft (nicht mehr startup-only), konnte er sonst
                # parallel zu _adjust/_protect_when_filled doppelten Schutz
                # nachlegen oder gegen einen gerade laufenden Cancel rennen.
                async with _lock_for(user_id, coin):
                    sz = abs(szi)
                    # 2026-06-13 Audit C-4: NUR den Bot-Anteil absichern. Auf einem
                    # Multi-User-Account kann |szi| eine manuelle Position (oder
                    # manuellen Aufschlag) enthalten — der Reconciler hätte sonst
                    # den SL/TP DES BOT-SIGNALS auf die manuelle Größe geklebt
                    # (H-2-Klasse). target = min(|szi|, bot_filled_sz) wenn die Row
                    # Ownership kennt; sonst (Pre-Ownership-Row) Fallback auf |szi|
                    # wie bisher (Legacy = reiner Bot-Account angenommen).
                    ownership = _load_bot_ownership(user_id, coin)
                    if ownership.get("known"):
                        target = min(sz, float(ownership.get("bot_filled_sz") or 0.0))
                        if target <= 0:
                            # Bot hält hier nichts → die Position ist manuell.
                            # NICHT anfassen (kein Schutz aufzwingen, kein Alert-Spam:
                            # M-5 adressiert den manuellen-Position-Alert separat).
                            log.info("reconcile %s/%s: position not bot-attributed (bot sz 0) — skip (C-4)",
                                     user_id, coin)
                            continue
                    else:
                        target = sz
                    # HOOK M-6 (2026-06-13): position_is_long mitgeben (Agent exec hat
                    # covered_stop_size side-aware gemacht, Default None = alte
                    # side-blinde Logik). szi>0 = LONG → reduce-only SELL-Stops zählen;
                    # szi<0 = SHORT → reduce-only BUY-Stops. Ohne diesen Hook bliebe
                    # der M-6-Fix inaktiv und falsch-seitige/manuelle Stops würden
                    # weiter als Deckung gezählt (echte Unter-Deckung unentdeckt).
                    covered = await asyncio.to_thread(trader.covered_stop_size, coin, szi > 0)
                    if covered >= target * 0.999:
                        _reconcile_alerted.pop((user_id, coin), None)  # M-5: gedeckt → reset
                        continue  # Bot-Anteil voll per SL gedeckt — nichts zu tun
                    uncovered = target - covered
                    if uncovered <= 0:
                        continue
                    sl, tps = _load_protection_params(user_id, coin)
                    akey = (user_id, coin)
                    if sl is None:
                        # M-5: nur EINMAL pro (user,coin) alerten, bis sich der
                        # Zustand ändert (Reason-String). Sonst alle 5 Min derselbe
                        # "secure manually"-Spam.
                        reason = "no_sl_known"
                        if _reconcile_alerted.get(akey) != reason:
                            _reconcile_alerted[akey] = reason
                            _log_activity(
                                user_id, "error",
                                f"{coin}: bot position qty {target:.6g} only covered {covered:.6g} by SL, NO "
                                f"SL price known in managed_trade — please secure/close manually.")
                        continue
                    is_buy = szi > 0
                    # Schutz für die FEHLENDE Menge nachlegen (reduce-only stackt mit
                    # vorhandenen Stops bei gleichem Trigger → Summe deckt die Position).
                    prot = await asyncio.to_thread(trader.place_protection, coin, is_buy, uncovered, sl, tps)
                    if prot.get("sl_ok"):
                        _reconcile_alerted.pop(akey, None)  # gelöst → Zustand reset
                        # M-12: auch hier die TP-Ergebnisse prüfen.
                        _check_tp_results(trader, user_id, coin, prot)
                        _log_activity(
                            user_id, "update",
                            f"{coin}: reconciler fixed under-coverage — added SL/TP for missing "
                            f"{uncovered:.6g} (was {covered:.6g}/{target:.6g}, SL {sl}).")
                    else:
                        # M-5: Reject-Alert ebenfalls 1× pro Zustand throtteln.
                        reason = "protect_failed"
                        if _reconcile_alerted.get(akey) != reason:
                            _reconcile_alerted[akey] = reason
                            _log_activity(
                                user_id, "error",
                                f"{coin}: under-coverage ({covered:.6g}/{target:.6g}) — adding protection "
                                f"FAILED ({str(prot.get('sl'))[:120]}). Secure manually!")
            except Exception as e:
                log.warning("reconcile: protect failed user %s coin %s: %s", user_id, coin, e)
