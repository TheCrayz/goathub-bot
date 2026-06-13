"""GoatHub Trading Bot — Multi-User-Plattform (Backend + Dashboard)."""
import asyncio
import logging
import os
import secrets
import threading
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session
import httpx
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app import config
from app.auth import (MAX_PW_BYTES, PasswordTooLongError, current_user, hash_pw,
                      make_token, needs_rehash, request_token_payload, verify_pw,
                      _user_identity)
from app.db import get_db, init_db
from app.models import Activity, ManagedTrade, User
from app.schemas import Login, Register, SettingsIn, WalletIn
from app.discord_oauth import exchange_code, get_discord_user, get_guild_member, has_required_role


# ── Rate limiting (Phase 1, 2026-06-02, gefixt 2026-06-04 + 2026-06-12) ─────
# 2026-06-04 KRITISCH: Rate-Limit-Bypass via X-Forwarded-For Spoofing entdeckt!
# Vorher: erster Wert aus X-Forwarded-For — vom Client KOMPLETT kontrollierbar.
# 2026-06-12 #7: zweite Lücke geschlossen — X-Real-IP wurde von JEDEM Peer
# akzeptiert. Solange uvicorn auf 0.0.0.0:8000 lauschte, konnte jeder, der den
# Port direkt erreicht (am Proxy vorbei), pro Request eine frische X-Real-IP
# erfinden → eigener Rate-Limit-Bucket pro Request → Login-Brute-Force trotz
# 10/5min-Limit. Jetzt: Proxy-Header werden NUR vertraut, wenn der direkte
# TCP-Peer localhost ist (= Caddy auf derselben Maschine; uvicorn wird im
# systemd-Unit auf 127.0.0.1 gebunden). Jeder andere Peer = seine eigene IP.
# 2026-06-13 Review-Fix: limiter + _client_ip nach app/ratelimit.py verschoben
# (Zirkular-Import app.main ↔ app.admin). Re-Export hier für Backwards-Compat.
from app.ratelimit import (  # noqa: F401
    _TRUSTED_PROXY_PEERS, _client_ip, limiter,
    LOGIN_RATE_LIMIT, REGISTER_RATE_LIMIT,
)

OAUTH_STATE_COOKIE = "discord_oauth_state"
OAUTH_STATE_TTL_S = 600  # 10 min, plenty for a sane user flow

# Phase 2 #18 (2026-06-02): hybrid Session-Cookie zusätzlich zum Bearer-Token.
# - Bearer-Token bleibt für Backwards-Compat (alte clients + curl/dev) erhalten.
# - Cookie ist httpOnly + Secure + SameSite=Lax → kann nicht via XSS gelesen werden.
# - Dashboard-JS bevorzugt Cookie und schreibt KEIN localStorage mehr.
# - `current_user` (in auth.py) liest beide Wege.
SESSION_COOKIE = "ght_session"
SESSION_COOKIE_TTL_S = 60 * 60 * 24 * 7  # 7 Tage, matches JWT_EXPIRE_HOURS=168


def _set_session_cookie(response, jwt_token: str):
    """Setzt das Session-Cookie sicher (httpOnly/Secure/Lax)."""
    response.set_cookie(
        SESSION_COOKIE,
        jwt_token,
        max_age=SESSION_COOKIE_TTL_S,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )

# 2026-06-12 #52: %(name)s im Format — vorher konnte man in journalctl nicht
# unterscheiden, ob eine Zeile von goathub.engine, goathub.sync, goathub.hl
# oder goathub.listener kam (der README-Incident-Workflow braucht genau das).
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("goathub")


# ── M-7: Single-Trader-Guard (exklusiver Prozess-Lock) ──────────────────────
# 2026-06-13 M-7: Der ganze Koordinationslayer (Dedup, Per-User/Coin-Locks,
# Throttle) nimmt GENAU EINEN Prozess an — nichts erzwang das bisher. Startet
# uvicorn mit `--workers 2`, oder überlappen sich beim Deploy alter + neuer
# Prozess, laufen ZWEI Discord-Listener + ZWEI Sync-Loops → der ProcessedSignal-
# Dedup ist NICHT prozessübergreifend atomar → DOPPELTE Trades (echtes Geld).
# Fix: vor dem Start von Listener+Sync einen exklusiven fcntl-flock auf eine
# Lock-Datei nehmen. Hält ein anderer Prozess den Lock schon → in DIESEM Prozess
# Listener+Sync NICHT starten (API/Dashboard laufen normal weiter) + log.error.
# Der fd MUSS offen bleiben (Schließen gibt den Lock frei) → Modul-Global; beim
# Shutdown sauber freigeben.
_trader_lock_fd = None


def _trader_lock_path():
    """Lock-Datei neben dem EMERGENCY_HALT-Flag (gleiches nur-goathub-schreibbares
    Verzeichnis, gleiche Fallback-Logik). getattr-Fallback, falls config das Feld
    (noch) nicht hat."""
    halt_path = getattr(config, "EMERGENCY_HALT_FLAG_PATH", None)
    if halt_path:
        return os.path.join(os.path.dirname(halt_path) or ".", "goathub.lock")
    # Fallback: dasselbe Schema wie config.py (var/lib bevorzugt, sonst /tmp).
    for d in ("/var/lib/goathub", "/tmp"):
        if os.path.isdir(d) and os.access(d, os.W_OK):
            return os.path.join(d, "goathub.lock")
    return "/tmp/goathub.lock"


def _acquire_trader_lock():
    """Versucht den exklusiven Trader-Lock zu nehmen. True = wir halten ihn
    (Listener+Sync dürfen starten), False = ein anderer Prozess hält ihn schon
    (nur API/Dashboard). Auf Plattformen ohne fcntl (z.B. Windows — prod ist
    Linux) fail-open: kein Lock, Verhalten wie vor M-7 (best-effort)."""
    global _trader_lock_fd
    try:
        import fcntl
    except ImportError:
        log.warning("M-7: fcntl nicht verfügbar (kein POSIX) — Single-Trader-Guard "
                    "deaktiviert, Listener/Sync starten ungeschützt.")
        return True
    path = _trader_lock_path()
    try:
        fd = open(path, "w")
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        # LOCK_NB → BlockingIOError, wenn ein anderer Prozess den Lock hält.
        # Andere OSError (Pfad nicht schreibbar) ebenfalls konservativ als
        # "nicht halten" behandeln, damit nicht doch zwei Trader laufen.
        log.error("M-7: Trader-Lock %s NICHT erhalten (%s) — ein anderer goathub-"
                  "Prozess tradet bereits. Listener+Sync in DIESEM Prozess NICHT "
                  "gestartet (API/Dashboard laufen weiter, KEIN Doppel-Trading).",
                  path, e)
        try:
            fd.close()
        except Exception:
            pass
        return False
    # Lock gehalten — fd offen halten (sonst fällt der Lock) + PID reinschreiben.
    try:
        fd.seek(0)
        fd.truncate()
        fd.write(str(os.getpid()))
        fd.flush()
    except Exception:
        pass
    _trader_lock_fd = fd
    log.info("M-7: Trader-Lock %s gehalten (pid=%d) — dieser Prozess tradet.",
             path, os.getpid())
    return True


def _release_trader_lock():
    """Lock beim Shutdown sauber freigeben (flock + fd schließen)."""
    global _trader_lock_fd
    fd = _trader_lock_fd
    _trader_lock_fd = None
    if fd is None:
        return
    try:
        import fcntl
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        fd.close()
    except Exception:
        pass


async def _activity_purge_loop():
    """Phase 4 (2026-06-02): Activity-Tabelle TTL-purge täglich.
    Behalte 90 Tage Historie, lösche älter. Verhindert unbeschränktes Wachstum
    (aktuell ~70 rows/Tag = ~25k/Jahr; nach 5 Jahren wird's spürbar).

    2026-06-12 #44: erweitert auf die drei anderen unbounded Tabellen —
    token_usage (1 Row pro Gemini-Call, hunderte/Tag), managed_trades
    (closed-Rows wurden NIE gelöscht) und processed_signal (1 Row pro
    (user, signal); Dedup muss nur Restarts/Replays überleben, nicht ewig).
    Alles vor der SQLite→Postgres-Migration relevant.
    """
    import datetime
    PURGE_KEEP_DAYS = int(os.getenv("ACTIVITY_KEEP_DAYS", "90"))
    TOKEN_USAGE_KEEP_DAYS = int(os.getenv("TOKEN_USAGE_KEEP_DAYS", "365"))
    CLOSED_TRADES_KEEP_DAYS = int(os.getenv("CLOSED_TRADES_KEEP_DAYS", "90"))
    PROCESSED_SIGNAL_KEEP_DAYS = int(os.getenv("PROCESSED_SIGNAL_KEEP_DAYS", "30"))
    # Beim ersten Mal nach 60 s starten — nicht direkt beim Service-Start
    # (damit Boot-Logs nicht zugespammt werden), dann täglich.
    await asyncio.sleep(60)
    while True:
        try:
            from app.db import SessionLocal
            from app.models import ManagedTrade, ProcessedSignal, TokenUsage
            db = SessionLocal()
            try:
                now = datetime.datetime.utcnow()
                cutoff = now - datetime.timedelta(days=PURGE_KEEP_DAYS)
                deleted = db.query(Activity).filter(Activity.ts < cutoff).delete(synchronize_session=False)
                # 2026-06-12 #44: TTL-Purge der drei restlichen Wachstums-Tabellen.
                del_tu = db.query(TokenUsage).filter(
                    TokenUsage.ts < now - datetime.timedelta(days=TOKEN_USAGE_KEEP_DAYS)
                ).delete(synchronize_session=False)
                # Nur CLOSED Trades purgen — offene/resting Rows sind Live-State!
                del_mt = db.query(ManagedTrade).filter(
                    ManagedTrade.status == "closed",
                    ManagedTrade.updated_at < now - datetime.timedelta(days=CLOSED_TRADES_KEEP_DAYS),
                ).delete(synchronize_session=False)
                del_ps = db.query(ProcessedSignal).filter(
                    ProcessedSignal.created_at < now - datetime.timedelta(days=PROCESSED_SIGNAL_KEEP_DAYS)
                ).delete(synchronize_session=False)
                db.commit()
                if deleted or del_tu or del_mt or del_ps:
                    log.info(
                        "ttl-purge: activity=%d (>%dd), token_usage=%d (>%dd), "
                        "closed managed_trades=%d (>%dd), processed_signal=%d (>%dd)",
                        deleted, PURGE_KEEP_DAYS, del_tu, TOKEN_USAGE_KEEP_DAYS,
                        del_mt, CLOSED_TRADES_KEEP_DAYS, del_ps, PROCESSED_SIGNAL_KEEP_DAYS,
                    )
            finally:
                db.close()
        except Exception as e:
            log.warning("activity-purge fehlgeschlagen: %s", e)
        await asyncio.sleep(86400)  # 1 Tag


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    tasks = []

    # M-10 (2026-06-13, Hook): default-Executor des Loops EXPLIZIT benennen und
    # größer setzen. hl_retry.py hat schon einen dedizierten ALERT_EXECUTOR — aber
    # die blockierenden `time.sleep`-Retries laufen via asyncio.to_thread im
    # DEFAULT-Executor, der auf einer 2-vCPU-VPS nur min(32, cpu+4)=~6 Worker hat.
    # Ein HL-429-Retry-Sturm belegt die mit schlafenden Threads → ein Lock-Holder,
    # der auf einen to_thread-Slot für seinen HL-Call wartet, stallt engine-weit.
    # Ein benannter, größerer Pool gibt Headroom und macht die Threads in journald
    # erkennbar (thread_name_prefix). Best-effort: ein Fehler hier darf den Start
    # nie blockieren. Größe via THREADPOOL_MAX_WORKERS überschreibbar.
    try:
        from concurrent.futures import ThreadPoolExecutor
        _pool_size = int(os.getenv("THREADPOOL_MAX_WORKERS", "32"))
        _default_executor = ThreadPoolExecutor(
            max_workers=max(8, _pool_size), thread_name_prefix="goathub-pool")
        asyncio.get_running_loop().set_default_executor(_default_executor)
        log.info("M-10: default ThreadPoolExecutor gesetzt (max_workers=%d).",
                 max(8, _pool_size))
    except Exception as e:
        log.warning("M-10: default-Executor konnte nicht gesetzt werden (%s) — "
                    "Loop nutzt den Standard-Pool.", e)

    # 2026-06-04 audit-find: vorher hatten die lifespan-tasks keinen
    # done_callback. Wenn ein outer-loop einer dieser Background-Tasks crashed
    # (z.B. unhandled Exception nach Deploy, syntax-Bug, oder corner-case),
    # sterben sie lautlos — keiner sieht's, ungeschützte Positionen denkbar.
    # Jetzt: identisches Pattern wie engine._spawn — task.exception() wird
    # geloggt mit Stacktrace, damit ein crashed Loop sichtbar wird.
    def _attach_logger(task, name):
        def _on_done(t):
            if not t.cancelled():
                exc = t.exception()
                if exc is not None:
                    log.error("lifespan task %r crashed: %r", name, exc, exc_info=exc)
        task.add_done_callback(_on_done)

    # 2026-06-12 M-10: fehlendes Discord-Rollen-Gate LAUT machen. Ohne
    # DISCORD_GUILD_ID + DISCORD_BOT_TOKEN lässt der OAuth-Callback JEDEN
    # Discord-Account rein (bewusst offen während der Beta) — aber das darf
    # nicht still passieren. Zusätzlich loggt der Callback jeden einzelnen
    # Login, der das Gate so umgangen hat.
    if not (config.DISCORD_GUILD_ID and config.DISCORD_BOT_TOKEN):
        log.warning(
            "⚠️ Discord-Rollen-Gate ist AUS (DISCORD_GUILD_ID gesetzt: %s, "
            "DISCORD_BOT_TOKEN gesetzt: %s) — JEDER Discord-Account kann sich "
            "per OAuth einloggen, KEIN Supporter-Rollen-Check!",
            bool(config.DISCORD_GUILD_ID), bool(config.DISCORD_BOT_TOKEN),
        )
    # M-7 (2026-06-13): exklusiven Trader-Lock VOR allen trade-tragenden Tasks
    # nehmen. Nur der Prozess, der ihn hält, startet Listener + Sync + den
    # Startup-Reconciler + den Token-Scraper. Ein zweiter (Deploy-Overlap,
    # `--workers 2`) bekommt den Lock NICHT → läuft API/Dashboard-only und kann
    # KEINE Doppel-Trades auslösen. Listener wird zusätzlich noch über
    # ENABLE_LISTENER gegated (Demo/Read-only-Deploys).
    is_trader = _acquire_trader_lock()
    if is_trader and config.ENABLE_LISTENER:
        from app.discord_listener import start_listener
        t = asyncio.create_task(start_listener())
        _attach_logger(t, "discord_listener")
        tasks.append(t)
        log.info("Discord-Listener gestartet.")
    elif not is_trader:
        log.error("Dieser Prozess hält den Trader-Lock NICHT — Listener/Sync/"
                  "Startup-Reconciler/Token-Scraper bleiben AUS (nur API/Dashboard). "
                  "Ursache prüfen: läuft ein zweiter goathub-Prozess (Deploy-Overlap, "
                  "uvicorn --workers >1)?")
    else:
        log.info("Listener AUS (ENABLE_LISTENER=false) — API/Dashboard laufen, kein Live-Trading.")
    # Activity-Purge läuft IMMER (auch ohne Trader-Lock / mit Listener aus) — die
    # Tabelle wächst auch über manuelle Settings-Änderungen, Login-Events etc.
    # Doppel-Purge in zwei Prozessen ist harmlos (idempotente DELETEs).
    t = asyncio.create_task(_activity_purge_loop())
    _attach_logger(t, "activity_purge_loop")
    tasks.append(t)
    if is_trader:
        # Phase 6+ (2026-06-03): Position-Sync-Loop — reconcilet managed_trades
        # gegen die echte HL-Position-State. Verhindert "stale open"-Rows wenn
        # HL via SL/TP autonom schließt (siehe SOL id=24 vom 03:38 UTC).
        # M-7: NUR im Trader-Prozess (sonst zwei Sync-Loops → Doppel-Schutz-Races).
        from app.sync import position_sync_loop
        t = asyncio.create_task(position_sync_loop())
        _attach_logger(t, "position_sync_loop")
        tasks.append(t)
        # H1 (2026-06-09): einmaliger Startup-Reconciler — zieht fehlende Schutz-
        # Orders (SL/TP) für offene Positionen nach, die ein Restart im Fill-Fenster
        # ungeschützt zurückgelassen haben könnte. Läuft einmal, dann fertig.
        from app.engine import reconcile_protection_on_startup
        t = asyncio.create_task(reconcile_protection_on_startup())
        _attach_logger(t, "startup_protection_reconcile")
        tasks.append(t)
        # 2026-06-04 Restposten #5: Token-Usage-Scrape-Loop — persistet signal-bot
        # TOKEN_USAGE-Zeilen in unsere DB, damit Historie auch nach Log-Rotation
        # erhalten bleibt. Liest bot.log alle 5 Minuten, idempotent (skip wenn
        # ts+counts schon in DB).
        # 2026-06-12 #54: nur noch per Opt-in. Der Loop pollte sonst in JEDEM
        # Deployment einen fremden TradingHub-Docker-Pfad — auf Hosts ohne dieses
        # Volume ein stiller No-Op alle 5 Minuten.
        # 2026-06-13 Review-Fix (Integration): Gate auf TOKEN_SCRAPER_ENABLED —
        # denselben Knopf, den der Loop selbst prüft. Vorher gateten wir hier auf
        # SIGNALBOT_LOG_PATH (Legacy-Alias); wer nur TOKEN_USAGE_LOG_PATH bzw.
        # TOKEN_SCRAPER_ENABLED=true setzte (so dokumentiert es .env.example),
        # bekam einen Scraper, der nie startete.
        if config.TOKEN_SCRAPER_ENABLED:
            from app.token_usage_scraper import token_usage_scrape_loop
            t = asyncio.create_task(token_usage_scrape_loop())
            _attach_logger(t, "token_usage_scrape_loop")
            tasks.append(t)
        else:
            log.info("Token-Usage-Scraper AUS (TOKEN_SCRAPER_ENABLED=false — "
                     "Pfad via TOKEN_USAGE_LOG_PATH/SIGNALBOT_LOG_PATH setzen).")
    yield
    # 2026-06-13 H-4: geordneter Shutdown. Vorher wurden nur die Lifespan-Tasks
    # gecancelt (ohne await) und engine._tasks NIE angefasst → ein Deploy konnte
    # einen Trade-Task zwischen place_entry und _save_managed killen (nackte,
    # unprotected Position auf Mainnet). Jetzt: ZUERST die Listener/Loops canceln
    # (stoppt das Reinkommen neuer Signale → keine neuen Trade-Tasks mehr), DANN
    # die bereits laufenden Trade-Tasks sauber auslaufen lassen.
    from app import engine
    for t in tasks:
        t.cancel()
    # In-flight Trade-Tasks (engine._tasks) drainen. Agent B baut
    # `async def drain_tasks(timeout)` in engine.py — getattr-Fallback, falls
    # die Funktion in diesem Deploy noch nicht existiert (dann wie bisher: nur
    # die Lifespan-Tasks gecancelt, kein Drain).
    drain = getattr(engine, "drain_tasks", None)
    if drain:
        try:
            await drain(timeout=20)
        except Exception as e:
            log.warning("drain_tasks beim Shutdown fehlgeschlagen: %s", e)
    # M-7: Trader-Lock NACH dem Drain freigeben — erst wenn keine Trade-Tasks mehr
    # laufen, darf ein nachfolgender Prozess den Lock (und damit das Traden)
    # übernehmen. No-op, wenn wir den Lock nie hielten (API-only-Prozess).
    _release_trader_lock()


# 2026-06-04 Audit-Find: /docs, /redoc und /openapi.json waren publik ohne Auth
# erreichbar — kompletter API-Surface inkl. Admin-Endpoints + Query-Params
# leakte raus (curl https://bot.goathub.network/openapi.json → 200, voller
# Schema-Dump). Default jetzt: aus. Wer das Schema lokal in der UI explorieren
# will, setzt ENABLE_DOCS=true in der .env — prod bleibt zu.
# 2026-06-12 CROSS-DEP: ENABLE_DOCS zieht zentral nach config.py um —
# defensiv via getattr lesen (funktioniert mit UND ohne den config-Change),
# Fallback bleibt der bisherige env-Read.
_enable_docs_raw = getattr(config, "ENABLE_DOCS", None)
if _enable_docs_raw is None:
    _enable_docs_raw = os.getenv("ENABLE_DOCS", "false")
_ENABLE_DOCS = (_enable_docs_raw if isinstance(_enable_docs_raw, bool)
                else str(_enable_docs_raw).strip().lower() in ("1", "true", "yes", "on"))
app = FastAPI(
    title="GoatHub Trading Bot",
    lifespan=lifespan,
    docs_url="/docs" if _ENABLE_DOCS else None,
    redoc_url="/redoc" if _ENABLE_DOCS else None,
    openapi_url="/openapi.json" if _ENABLE_DOCS else None,
)
app.state.limiter = limiter


# 2026-06-12 #51: eigener 429-Handler. slowapi's Default liefert
# {"error": "Rate limit exceeded: ..."} — das Frontend (dashboard.js/admin.js
# api()) liest aber überall .detail und zeigte dem User nur die nackte "429".
# Jetzt gleiche Shape wie alle anderen Fehler: {"detail": "..."}.
def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
    response = JSONResponse(
        {"detail": f"Too many requests ({exc.detail}) — please wait a few minutes and try again."},
        status_code=429,
    )
    try:
        # Retry-After/X-RateLimit-Header beibehalten (wie slowapi's Default).
        response = request.app.state.limiter._inject_headers(response, request.state.view_rate_limit)
    except Exception:
        pass
    return response


app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)

# Phase 3 (2026-06-02): Admin-Router. is_admin-Gate ist im Modul selbst.
from app.admin import router as admin_router
app.include_router(admin_router)

# 2026-06-04 (Restposten #2): static-mount für /static/dashboard.js etc.
# Erlaubt strikt CSP (script-src 'self' ohne 'unsafe-inline').
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")


# ── Security-Headers (Phase 4, 2026-06-02) ──────────────────────────────────
# Defense-in-depth: setzt Browser-Schutzmechanismen, die das XSS-Risiko
# (auch nach dem Escape-Fix C-4) reduzieren und Clickjacking via iframes
# komplett verbieten.
@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # HSTS: Browser merkt sich, immer HTTPS zu verwenden (Caddy terminiert TLS).
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    # CSP — 2026-06-04 (Restposten #2): script 'unsafe-inline' RAUS. Inline-JS
    # ist nach /static/dashboard.js (und admin.js wenn portiert) gewandert, alle
    # onclick-Handler sind addEventListener-basiert. 'unsafe-inline' für style-src
    # bleibt vorerst (viele style="…"-Attribute, das wäre eigener Refactor); ist
    # weniger gefährliche XSS-Klasse als script-inline.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https://cdn.discordapp.com; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self' https://discord.com"
    )
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    return response


# ── Auth ─────────────────────────────────────────────────────────────────────
@app.post("/api/register")
@limiter.limit(REGISTER_RATE_LIMIT)
def register(request: Request, body: Register, db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    # 2026-06-04 audit-find: vorher hat "@" alleine durchgelassen z.B.
    # "<script>alert(1)</script>@x.com" oder 1MB-langer string. Beides wäre
    # in der DB gelandet (admin.js escapt via esc(), aber DB-Pollution +
    # potenzielle nicht-escapte stellen sind real). Jetzt strikte Validierung.
    import re
    EMAIL_RE = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,24}$")
    if len(email) > 254:
        raise HTTPException(400, "Email too long (max 254 chars per RFC 5321)")
    if not EMAIL_RE.match(email):
        raise HTTPException(400, "Valid email + password (min. 6 chars) required")
    # 2026-06-12 #21: discord_<id>@goathub.internal ist das synthetische
    # Email-Format, das der OAuth-Callback für Discord-User anlegt. Vorher
    # konnte jeder, der eine öffentliche Discord-ID kennt, diese Email
    # vorab registrieren → der erste Discord-Login des Opfers knallte in den
    # UNIQUE-Constraint → permanentes /?error=oauth_failed (gezielter
    # Signup-DoS). Die ganze Domain ist reserviert — niemand Legitimes hat
    # eine echte @goathub.internal-Adresse.
    if email.endswith("@goathub.internal"):
        raise HTTPException(400, "This email domain is reserved — please use your real email address")
    # 2026-06-12 M-9 (defensiv): auch der discord_-Local-Part-Namespace ist
    # reserviert, egal welche Domain dahinter steht — schützt die synthetischen
    # OAuth-Adressen auch dann noch, falls die interne Domain mal umbenannt wird.
    if email.split("@", 1)[0].startswith("discord_"):
        raise HTTPException(400, "Email addresses starting with 'discord_' are reserved — please use your real email address")
    if len(body.password) < 6:
        raise HTTPException(400, "Valid email + password (min. 6 chars) required")
    # Mit BCrypt-v2 (Restposten #3) ist 72-byte Limit weg, aber wir limitieren
    # trotzdem nach oben (gegen Resource-Exhaust via mega-strings).
    if len(body.password) > 512:
        raise HTTPException(400, "Password too long (max 512 chars)")
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(409, "Email already registered")
    try:
        pw_hash = hash_pw(body.password)
    except PasswordTooLongError as e:
        raise HTTPException(400, str(e))
    u = User(email=email, password_hash=pw_hash,
             risk_pct=config.DEFAULT_RISK_PCT, leverage=config.DEFAULT_LEVERAGE,
             max_open_positions=config.DEFAULT_MAX_OPEN)
    db.add(u)
    db.commit()
    db.refresh(u)
    tok = make_token(u.id, getattr(u, "token_version", 0), _user_identity(u))
    response = JSONResponse({"access_token": tok, "token_type": "bearer"})
    _set_session_cookie(response, tok)  # Phase 2 #18: hybrid auth
    return response


# 2026-06-12 LOW-7: Per-ACCOUNT-Lockout zusätzlich zum per-IP-Rate-Limit.
# Das IP-Limit allein reicht nicht: wer den Port direkt erreicht oder viele
# IPs hat, verteilt den Brute-Force — der Account selbst blieb unbegrenzt
# angreifbar. Jetzt: >= FAILED_LOGIN_MAX FEHLGESCHLAGENE Versuche in Folge
# sperren den Account für FAILED_LOGIN_LOCK_S (auch mit richtigem Passwort),
# erfolgreicher Login setzt den Zähler zurück. In-Memory-Dict reicht
# (Single-Process-Deployment, gleiche Klasse wie _snapshot_cache).
_failed_logins: dict = {}    # email → {"fails": int, "locked_until": epoch-s}
# L-9 (2026-06-13): /api/login ist `def` (sync) → FastAPI fährt es im
# Threadpool. Mehrere parallele Fehl-Logins für DASSELBE Konto liefen vorher
# read-modify-write auf _failed_logins ohne Lock → verlorene Inkremente
# schwächten den Lockout (zwei Threads lesen fails=4, schreiben beide 5 statt
# 5→6). Ein einzelnes threading.Lock serialisiert die wenigen Dict-Mutationen
# (kein await dazwischen, vernachlässigbare Contention).
_failed_logins_lock = threading.Lock()
FAILED_LOGIN_MAX = 5
FAILED_LOGIN_LOCK_S = 15 * 60


@app.post("/api/login")
@limiter.limit(LOGIN_RATE_LIMIT)
def login(request: Request, body: Login, db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    now = _time.time()
    # L-9: Lock-Status atomar lesen.
    with _failed_logins_lock:
        rec = _failed_logins.get(email)
        locked_until = rec.get("locked_until", 0) if rec else 0
    if locked_until > now:
        wait_min = int((locked_until - now) // 60) + 1
        raise HTTPException(
            429, f"Account temporarily locked after too many failed logins — "
                 f"try again in {wait_min} min")
    u = db.query(User).filter(User.email == email).first()
    if not u or not verify_pw(body.password, u.password_hash):
        # L-9: das Inkrement + die Lockout-Entscheidung serialisieren, sonst
        # gehen parallele Fehlversuche desselben Kontos als verlorene Inkremente
        # verloren (zwei Worker schreiben beide 5 statt 5→6).
        with _failed_logins_lock:
            rec = _failed_logins.setdefault(email, {"fails": 0, "locked_until": 0})
            rec["fails"] += 1
            if rec["fails"] >= FAILED_LOGIN_MAX:
                rec["locked_until"] = now + FAILED_LOGIN_LOCK_S
                rec["fails"] = 0
                log.warning("login lockout: account %r für %ds gesperrt nach %d Fehlversuchen in Folge",
                            email, FAILED_LOGIN_LOCK_S, FAILED_LOGIN_MAX)
            # Speicher-Guard gegen Email-Spray: abgelaufene Einträge rauswerfen.
            if len(_failed_logins) > 5000:
                for k in [k for k, v in _failed_logins.items()
                          if v.get("locked_until", 0) < now and k != email]:
                    _failed_logins.pop(k, None)
        raise HTTPException(401, "Wrong email or password")
    with _failed_logins_lock:
        _failed_logins.pop(email, None)   # Erfolg → Zähler/Lock zurücksetzen
    # 2026-06-04 Restposten #3: transparente Migration legacy → v2 (SHA256+bcrypt).
    # Wir haben das Plain-PW gerade verifiziert und im Speicher, also können
    # wir den Hash sofort upgraden. User merkt nichts.
    if needs_rehash(u.password_hash):
        try:
            u.password_hash = hash_pw(body.password)
            db.commit()
        except Exception:
            pass  # rehash-fail darf den login nicht blockieren
    tok = make_token(u.id, getattr(u, "token_version", 0), _user_identity(u))
    response = JSONResponse({"access_token": tok, "token_type": "bearer"})
    _set_session_cookie(response, tok)  # Phase 2 #18: hybrid auth
    return response


@app.post("/api/logout")
def logout(u: User = Depends(current_user), db: Session = Depends(get_db)):
    """Server-side logout — bumps token_version so every JWT issued before
    this point stops validating. Even if a token was exfiltrated via XSS, it
    is now useless after the user clicks Logout. Phase 2 #18: clears the
    httpOnly session cookie too."""
    u.token_version = int(getattr(u, "token_version", 0) or 0) + 1
    db.commit()
    response = JSONResponse({"ok": True, "message": "All existing sessions invalidated."})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


# 2026-06-08 Mainnet-Hardening B4: JWT-Refresh-Endpoint.
# JWT_EXPIRE_HOURS ist auf 24h gekürzt (statt 168h = 7d). Dashboard pollt
# diesen Endpoint alle paar Stunden, bekommt einen neuen JWT mit frischem
# exp. Solange der aktuelle noch valid + < 1h vor expiry, wird refreshed.
# 2026-06-12 LOW-8: absolute Session-Lebensdauer. Vorher konnte eine Session
# via /api/refresh UNBEGRENZT verlängert werden (alle 12h ein frischer JWT).
SESSION_ABS_LIFETIME_S = 30 * 24 * 3600   # 30 Tage


@app.post("/api/refresh")
@limiter.limit("60/minute")
def refresh_token(request: Request, u: User = Depends(current_user)):
    """Mintet einen neuen JWT für den aktuellen User. Voraussetzung: alter
    JWT noch valid (current_user dependency erfüllt) — bei expired wird's
    durch current_user ohnehin 401.

    2026-06-12 LOW-8: orig_iat (Zeitpunkt des ECHTEN Logins) wird beim
    Refresh weitergereicht statt neu gesetzt; ist die Session älter als
    SESSION_ABS_LIFETIME_S, gibt's keinen neuen Token mehr → neu einloggen.
    Alt-Tokens ohne orig_iat-Claim nutzen ihr iat (backward-kompatibel)."""
    payload = request_token_payload(request)
    orig_iat = int(payload.get("orig_iat") or payload.get("iat") or 0)
    if orig_iat and _time.time() - orig_iat > SESSION_ABS_LIFETIME_S:
        raise HTTPException(401, "Session maximum age (30 days) reached — please log in again")
    new_tok = make_token(u.id, getattr(u, "token_version", 0), _user_identity(u),
                         orig_iat=orig_iat or None)
    response = JSONResponse({"access_token": new_tok, "token_type": "bearer"})
    _set_session_cookie(response, new_tok)
    return response


@app.get("/auth/discord")
def discord_login():
    """Redirect to Discord OAuth2 with a CSRF-protecting `state` parameter.

    Phase 1 (2026-06-02): vorher hatte der Flow KEIN state → klassische OAuth-
    CSRF-Lücke. Wir generieren ein zufälliges Token, schicken es per httpOnly-
    Cookie an den Browser UND als state-Param an Discord. Beim Callback
    vergleichen wir Cookie vs. Query-Param — stimmen sie nicht überein, wird
    der Flow verweigert.
    """
    state = secrets.token_urlsafe(32)
    scopes = "identify guilds"
    url = (
        f"https://discord.com/oauth2/authorize"
        f"?client_id={config.DISCORD_CLIENT_ID}"
        f"&redirect_uri={config.DISCORD_REDIRECT_URI}"
        f"&response_type=code"
        f"&scope={scopes.replace(' ', '%20')}"
        f"&state={state}"
    )
    response = RedirectResponse(url)
    response.set_cookie(
        OAUTH_STATE_COOKIE,
        state,
        max_age=OAUTH_STATE_TTL_S,
        httponly=True,
        secure=True,        # Caddy terminiert TLS → HTTPS-only
        samesite="lax",     # erlaubt Top-Level-Navigation vom Discord-Redirect
        path="/auth",
    )
    return response


def _resolve_discord_user(db, discord_id: str, username: str, avatar: str):
    """Find-or-create the User row for a Discord identity and return the
    primitives needed to mint a JWT: (uid, token_version, identity).

    2026-06-13 fastapi-patterns #1: läuft KOMPLETT off-loop (Aufruf via
    asyncio.to_thread im async discord_callback). Vorher liefen db.query/
    commit/refresh direkt im async-Handler → blockierten den Event-Loop
    (das Sync-DB-in-async-Route-Anti-Pattern). ALLE DB-Reads (auch die nach
    dem commit() durch expire_on_commit=True ausgelösten Lazy-Loads von u.id/
    Identity) passieren hier im Thread, deshalb geben wir nur fertige
    Primitives zurück — der Caller fasst die Session danach nicht mehr an."""
    u = db.query(User).filter(User.discord_id == discord_id).first()
    if not u:
        u = User(
            email=f"discord_{discord_id}@goathub.internal",
            password_hash="",
            discord_id=discord_id,
            discord_username=username,
            discord_avatar=avatar,
            risk_pct=config.DEFAULT_RISK_PCT,
            leverage=config.DEFAULT_LEVERAGE,
            max_open_positions=config.DEFAULT_MAX_OPEN,
        )
        db.add(u)
        db.commit()
        db.refresh(u)
    else:
        # Update discord info
        u.discord_username = username
        u.discord_avatar = avatar
        db.commit()
    return u.id, int(getattr(u, "token_version", 0) or 0), _user_identity(u)


@app.get("/auth/callback")
async def discord_callback(request: Request, code: str = None, state: str = None, error: str = None, db: Session = Depends(get_db)):
    """Handle Discord OAuth callback — mit CSRF-state-Check (Phase 1)."""
    # CSRF-Check: state aus dem Query-Param muss mit dem httpOnly-Cookie übereinstimmen.
    expected_state = request.cookies.get(OAUTH_STATE_COOKIE)
    if not expected_state or not state or not secrets.compare_digest(state, expected_state):
        log.warning("OAuth state mismatch (cookie=%s, query=%s)", bool(expected_state), bool(state))
        resp = RedirectResponse("/?error=oauth_state_mismatch")
        resp.delete_cookie(OAUTH_STATE_COOKIE, path="/auth")
        return resp

    if error or not code:
        resp = RedirectResponse("/?error=discord_denied")
        resp.delete_cookie(OAUTH_STATE_COOKIE, path="/auth")
        return resp

    try:
        # Exchange code for token
        token_data = await exchange_code(code)
        access_token = token_data["access_token"]

        # Get Discord user info
        discord_user = await get_discord_user(access_token)
        discord_id = str(discord_user["id"])
        username = discord_user.get("username", "")
        avatar = discord_user.get("avatar", "")

        # Check role if guild ID and bot token are configured
        if config.DISCORD_GUILD_ID and config.DISCORD_BOT_TOKEN:
            member = await get_guild_member(discord_id, config.DISCORD_BOT_TOKEN, config.DISCORD_GUILD_ID)
            if not has_required_role(member, config.DISCORD_REQUIRED_ROLE_ID):
                resp = RedirectResponse("/?error=no_role")
                resp.delete_cookie(OAUTH_STATE_COOKIE, path="/auth")
                return resp
        else:
            # 2026-06-12 M-10: Gate nicht konfiguriert → Login bewusst offen
            # (Beta-Entscheidung), aber jeder Bypass wird EINZELN geloggt,
            # damit das nie wieder still passiert (plus Startup-Warnung in
            # lifespan).
            log.warning(
                "Discord-Login OHNE Rollen-Gate durchgelassen: discord_id=%s "
                "username=%r (DISCORD_GUILD_ID/DISCORD_BOT_TOKEN nicht konfiguriert)",
                discord_id, username,
            )

        # Find or create user by discord_id — off-loop (sync DB in async route).
        uid, token_version, identity = await asyncio.to_thread(
            _resolve_discord_user, db, discord_id, username, avatar)

        jwt_token = make_token(uid, token_version, identity)
        # Phase 2 #18 (2026-06-02): Session-Cookie (httpOnly) ist DER Auth-Weg.
        # 2026-06-12 #20/#31: das Legacy-URL-Fragment (/#token=…) ist raus.
        # Das Dashboard-JS verwirft das Fragment seit Phase 2 ohnehin — übrig
        # blieb nur, dass ein 24h-gültiger JWT in der Browser-History landete
        # (inkl. Cross-Device-Sync) und via location.hash für jedes Script
        # lesbar war. Das Cookie wird auf DIESER Response gesetzt, plain "/"
        # reicht.
        resp = RedirectResponse("/")
        _set_session_cookie(resp, jwt_token)
        resp.delete_cookie(OAUTH_STATE_COOKIE, path="/auth")
        return resp

    except Exception as e:
        log.error(f"Discord OAuth error: {e}")
        resp = RedirectResponse("/?error=oauth_failed")
        resp.delete_cookie(OAUTH_STATE_COOKIE, path="/auth")
        return resp


def _user_public(u: User):
    avatar_url = None
    if u.discord_id and u.discord_avatar:
        avatar_url = f"https://cdn.discordapp.com/avatars/{u.discord_id}/{u.discord_avatar}.png?size=64"
    return {"email": u.email,
            "discord_username": u.discord_username or None,
            "discord_avatar_url": avatar_url,
            "wallet_connected": bool(u.hl_api_secret_enc),
            "hl_account_address": u.hl_account_address, "bot_active": u.bot_active,
            "builder_approved": u.builder_approved,
            "is_admin": bool(getattr(u, "is_admin", False)),   # Phase 3 (2026-06-02)
            "settings": {"risk_pct": u.risk_pct, "leverage": u.leverage,
                         "max_open_positions": u.max_open_positions,
                         "capital_cap_usdc": u.capital_cap_usdc,
                         # 2026-06-08 C1: Drawdown-Cap
                         "max_drawdown_pct": float(getattr(u, "max_drawdown_pct", 0.30) or 0.30),
                         "peak_account_value": float(getattr(u, "peak_account_value", 0) or 0)}}


@app.get("/api/me")
def me(u: User = Depends(current_user)):
    return _user_public(u)


# 2026-06-12 #25: Response-Cache pro Adresse. Ein cold-cache Request löste
# 16 sequenzielle user_fills-Downloads (kompletter, identischer Fill-History
# pro Coin) gegen die HL-Info-API aus — und der Endpoint hatte als einziger
# KEIN Rate-Limit. Ein einzelner pollender User konnte damit die Server-IP
# in HL's Rate-Limit treiben (trifft ALLE User). TTL = PERCOIN_CACHE_TTL_S
# (gleicher Wert wie der (addr, coin)-Cache in engine._per_coin_stats).
_percoin_status_cache: dict = {}    # address → (timestamp, response_dict)


@app.get("/api/per-coin-status")
@limiter.limit("30/minute")
def per_coin_status(request: Request, u: User = Depends(current_user)):
    """Per-coin filter status für DEN AKTUELLEN USER.

    2026-06-04: Tester sehen jetzt direkt im Dashboard warum ein Coin
    geskippt wird (Win-Rate < Schwelle nach genug Trades), statt nur via
    Admin-Endpoint. Identisches Format wie admin_per_coin aber gefiltert
    auf u.hl_account_address; ein einzelner User-Eintrag in 'users'.
    """
    if not u.hl_account_address:
        return {"connected": False, "min_trades_required": config.PERCOIN_MIN_TRADES,
                "min_winrate": config.PERCOIN_MIN_WINRATE, "coins": []}
    cached = _percoin_status_cache.get(u.hl_account_address)
    if cached and _time.time() - cached[0] < config.PERCOIN_CACHE_TTL_S:
        return cached[1]
    from app.engine import _per_coin_stats
    common = ["BTC", "ETH", "SOL", "BNB", "AVAX", "DOGE", "ADA", "NEAR", "ATOM",
              "APT", "ARB", "OP", "TIA", "SUI", "INJ", "AAVE"]
    coins_out = []
    for coin in common:
        try:
            stats = _per_coin_stats(u.hl_account_address, coin)
        except Exception:
            stats = None
        if stats and stats["trades"] > 0:
            blocked = (
                stats["trades"] >= config.PERCOIN_MIN_TRADES
                and stats["win_rate"] < config.PERCOIN_MIN_WINRATE
            )
            coins_out.append({
                "coin": coin,
                "trades": stats["trades"],
                "wins": stats["wins"],
                "win_rate": round(stats["win_rate"], 3),
                "blocked": blocked,
            })
    # Sort: blocked first (warnt User), dann nach trades desc
    coins_out.sort(key=lambda c: (not c["blocked"], -c["trades"]))
    result = {
        "connected": True,
        "min_trades_required": config.PERCOIN_MIN_TRADES,
        "min_winrate": config.PERCOIN_MIN_WINRATE,
        "coins": coins_out,
    }
    _percoin_status_cache[u.hl_account_address] = (_time.time(), result)
    return result


# ── Settings & Wallet ────────────────────────────────────────────────────────
# 2026-06-08 Mainnet-Hardening B2: rate-limit auf authenticated state-mutating
# routes (60/min) und auf häufig gepollte read-routes (30/min). Verhindert
# resource-exhaust via authed user, schützt unsere IP gegen HL-info-DoS.
@app.put("/api/settings")
@limiter.limit("60/minute")
def update_settings(request: Request, body: SettingsIn, u: User = Depends(current_user), db: Session = Depends(get_db)):
    # 2026-06-12 #12/#13: REJECT statt Silent-Clamp/Coerce.
    # (a) Bounds-Verletzungen (z.B. risk_pct=1.0 — User meinte "1 %", API
    #     erwartet FRACTION 0.01) werden jetzt von pydantic (SettingsIn,
    #     schemas.py) mit 422 abgelehnt — vorher clampte der Code still auf
    #     0.05 = 5 %/Trade und das Frontend zeigte weiter "Saved ✓".
    # (b) Explizit mitgeschicktes null → 422. Vorher coercte das Frontend
    #     leere Felder zu 0 — ein geleertes Capital-Cap-Feld hob still den
    #     kompletten Geld-Cap auf. Partial-Updates (Feld WEGLASSEN) bleiben
    #     erlaubt — der bot_active-Toggle schickt weiter nur {bot_active}.
    _MUTABLE = ("risk_pct", "leverage", "max_open_positions",
                "capital_cap_usdc", "bot_active", "max_drawdown_pct")
    for fname in _MUTABLE:
        if fname in body.model_fields_set and getattr(body, fname) is None:
            raise HTTPException(
                422, f"{fname} must not be null — omit the field to keep the current value")
    if body.risk_pct is not None:
        u.risk_pct = body.risk_pct          # FRACTION, (0, 0.05] via Schema erzwungen
    if body.leverage is not None:
        # 2026-06-06: leverage = USER-MAX-CAP für Auto-Leverage.
        # Bot rechnet pro Trade aus SL+Confidence den optimalen Hebel, gecappt
        # an diesem Wert. 50 = HL Perps Max. Niedrigere Werte = User will
        # konservativer fahren (z.B. 10x als persönliches Maximum).
        u.leverage = body.leverage           # [1, 50] via Schema erzwungen
    if body.max_open_positions is not None:
        u.max_open_positions = body.max_open_positions   # [1, 20] via Schema erzwungen
    if body.capital_cap_usdc is not None:
        u.capital_cap_usdc = body.capital_cap_usdc       # [0, 10M] via Schema; 0 = ganzer Account
    if body.bot_active is not None:
        if body.bot_active and not u.hl_api_secret_enc:
            raise HTTPException(400, "Connect your wallet before activating the bot")
        u.bot_active = body.bot_active
    if body.max_drawdown_pct is not None:
        # 2026-06-08 C1: 0 = disabled, max 0.95 (95% drawdown cap)
        u.max_drawdown_pct = body.max_drawdown_pct       # [0, 0.95] via Schema erzwungen
    db.commit()
    return _user_public(u)


@app.post("/api/wallet")
@limiter.limit("60/minute")
def set_wallet(request: Request, body: WalletIn, u: User = Depends(current_user), db: Session = Depends(get_db)):
    from app.crypto import encrypt
    import re
    addr = body.hl_account_address.strip()
    sec = body.hl_api_secret.strip()
    # MASTER-Adresse: 0x + 40 HEX = 42 Zeichen.
    # 2026-06-12 #34: volle Hex-Validierung statt nur Prefix+Länge. Vorher
    # passierte "0xZZZZ…" (42 Zeichen, kein Hex) die Prüfung und wurde
    # gespeichert → jeder info.user_state(addr)-Call scheiterte → Dashboard
    # ohne Balance, Bot tradet nie, User sieht keinen Fehler (gleiche
    # Silent-Broken-Account-Klasse wie das Adresse-im-Key-Feld-Problem).
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", addr):
        raise HTTPException(400, "MASTER address must be 0x + 40 hex chars (42 total). That's the public address, not the key.")
    # Agent-Key SOFORT validieren (sonst scheitert es erst beim Trade — der häufigste Fehler!)
    try:
        from eth_account import Account
        agent_addr = Account.from_key(sec).address
    except Exception:
        raise HTTPException(400, "Invalid Agent key. It must be the long private key (0x + 64 chars = 66 total) — NOT an address.")
    if agent_addr.lower() == addr.lower():
        raise HTTPException(400, "This key belongs to the MASTER address. You need the separate AGENT key (from the API-wallet 'Generate' box).")
    # 2026-06-12 M-13: Wallet-Wechsel invalidiert das Builder-Approval — das
    # Approval ist ON-CHAIN an die ALTE Master-Adresse gebunden. Vorher blieb
    # das Flag stehen → Engine hängte den Builder-Code an, HL lehnte JEDE
    # Order ab ("Builder fee has not been approved") → User verpasste still
    # alle Entries. Gleiche Adresse erneut speichern (Key-Rotation) behält
    # das Flag.
    if (u.hl_account_address or "").lower() != addr.lower():
        u.builder_approved = False
    u.hl_account_address = addr
    u.hl_api_secret_enc = encrypt(sec)
    db.commit()
    return {"ok": True, "wallet_connected": True}


def _query_on_chain_builder_fee(user_addr: str, builder_addr: str) -> int:
    """Frag Hyperliquid nach dem aktuell on-chain approved max-fee für (user, builder).
    Antwort ist in basis points (1bp = 0.01%). 0 = nicht freigegeben.
    Wirft im Fehlerfall (Network, Format) — Caller behandelt."""
    from app.hyperliquid_exec import get_info
    info = get_info(config.HL_TESTNET)
    # SDK exposes `post` for arbitrary Info-API requests. The maxBuilderFee
    # request returns a number (or string number) representing approved bps.
    raw = info.post("/info", {"type": "maxBuilderFee", "user": user_addr, "builder": builder_addr})
    try:
        return int(raw)
    except (TypeError, ValueError):
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return 0


@app.get("/api/builder-status")
def builder_status(u: User = Depends(current_user)):
    """Aktueller on-chain Approval-Status (Phase 5, 2026-06-02).
    Dashboard kann damit anzeigen "✓ on-chain bestätigt 5 bps" vs
    "✗ nicht bestätigt — bitte in HL approveBuilderFee aufrufen".
    """
    from app.hyperliquid_exec import fee_to_int
    out = {
        "configured": bool(config.BUILDER_ADDRESS),
        "builder_address": config.BUILDER_ADDRESS or None,
        "required_bps": fee_to_int(config.BUILDER_FEE) if config.BUILDER_ADDRESS else 0,
        "user_wallet_connected": bool(u.hl_account_address),
        "db_flag": bool(u.builder_approved),
        "on_chain_bps": None,
        "on_chain_ok": False,
        "error": None,
    }
    if not (config.BUILDER_ADDRESS and u.hl_account_address):
        return out
    try:
        bps = _query_on_chain_builder_fee(u.hl_account_address, config.BUILDER_ADDRESS)
        out["on_chain_bps"] = bps
        out["on_chain_ok"] = bps >= out["required_bps"]
    except Exception as e:
        out["error"] = f"HL Info-API: {e}"
    return out


class BuilderApprovalSubmit(BaseModel):
    """Phase 6+ (2026-06-03): payload from the MetaMask-signing-flow im Dashboard.
    Der User hat in MetaMask die EIP-712-Payload signiert, schickt sie über uns
    weiter an HL's /exchange Endpoint, weil HL CORS-restrict + wir DB-flag setzen
    wollen."""
    action: dict   # die HL-Action ({type, hyperliquidChain, signatureChainId, maxFeeRate, builder, nonce})
    signature: dict  # {r, s, v}
    nonce: int


@app.post("/api/builder-approval-submit")
@limiter.limit("10/minute")
def submit_builder_approval(
    request: Request,
    body: BuilderApprovalSubmit,
    u: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Phase 6+ (2026-06-03): receive the MetaMask-signed approveBuilderFee
    payload, forward it to HL, verify success, set DB flag.

    Frontend builds the EIP-712 payload, asks MetaMask to sign, gives us
    {action, signature, nonce}. We just relay to HL — HL itself validates
    that the signature comes from the user's master address.

    2026-06-12 #32/#42 Hardening:
    - action.type MUSS "approveBuilderFee" sein. Vorher war der Endpoint ein
      generisches Relay für JEDE user-signierte Exchange-Action über unsere IP.
    - maxFeeRate darf BUILDER_FEE nicht überschreiten — wir relayen keine
      Approval für mehr Fee als wir konfiguriert verlangen.
    - builder_approved wird AUSSCHLIESSLICH gesetzt, wenn die On-Chain-
      Verifikation approved_bps >= required_bps bestätigt. Vorher wurde der
      Flag auch bei Verify-Fail gesetzt → exakt der Trust-me-Flag-Bug
      (README Known issue #3), den der Endpoint beheben sollte: Engine hängt
      den Builder-Code an, HL lehnt jede Order ab, User verpasst still alle
      Entries.
    - Rate-Limit 10/min: relayed an HL /exchange + macht einen blocking
      Verify-Roundtrip — braucht ein User legitim genau 1x.
    """
    if not config.BUILDER_ADDRESS:
        raise HTTPException(400, "Server has no BUILDER_ADDRESS configured")
    if not u.hl_account_address:
        raise HTTPException(400, "Connect your wallet first")
    from app.hyperliquid_exec import fee_to_int
    # Sanity: nur approveBuilderFee-Actions werden relayed
    action_type = str(body.action.get("type", ""))
    if action_type != "approveBuilderFee":
        raise HTTPException(400, f"action.type must be 'approveBuilderFee' (got {action_type[:32]!r})")
    # Sanity: action.builder must match our configured BUILDER_ADDRESS
    action_builder = str(body.action.get("builder", "")).lower()
    if action_builder != config.BUILDER_ADDRESS.lower():
        raise HTTPException(400, f"action.builder mismatch (got {action_builder[:10]}…, expected our builder)")
    # Sanity: maxFeeRate <= konfigurierte BUILDER_FEE (fee_to_int raised bei >0.1%)
    # 2026-06-13 Review-Fix: maxFeeRate ist PFLICHT. fee_to_int(None/garbage)
    # liefert 0 → "0 <= required" hätte eine Action OHNE maxFeeRate durch-
    # gewunken; HL interpretiert das Feld selbst, wir relayen nur Verifiziertes.
    raw_fee = body.action.get("maxFeeRate")
    if raw_fee in (None, ""):
        raise HTTPException(400, "action.maxFeeRate missing")
    try:
        required_bps = fee_to_int(config.BUILDER_FEE)
        submitted_bps = fee_to_int(raw_fee)
    except ValueError as e:
        raise HTTPException(400, f"Invalid maxFeeRate: {e}")
    if submitted_bps <= 0:
        raise HTTPException(400, f"Invalid maxFeeRate: {str(raw_fee)[:32]!r}")
    if submitted_bps > required_bps:
        raise HTTPException(
            400, f"maxFeeRate too high (got {submitted_bps} bps, our configured fee is "
                 f"{required_bps} bps = {config.BUILDER_FEE}) — refusing to relay")
    # POST to HL /exchange
    import httpx
    hl_url = (
        "https://api.hyperliquid-testnet.xyz/exchange"
        if config.HL_TESTNET
        else "https://api.hyperliquid.xyz/exchange"
    )
    payload = {"action": body.action, "nonce": body.nonce, "signature": body.signature}
    try:
        r = httpx.post(hl_url, json=payload, timeout=15.0)
        hl_resp = r.json()
    except Exception as e:
        raise HTTPException(502, f"HL POST failed: {e}")
    log.info("approveBuilderFee submit for user %s: %s", u.id, hl_resp)
    if hl_resp.get("status") != "ok":
        # HL bounced — return the actual error so user sees something useful
        err = hl_resp.get("response") if isinstance(hl_resp, dict) else str(hl_resp)
        raise HTTPException(400, f"HL rejected approval: {err}")
    # Success on HL — verify on-chain (cache may not be instantly up — give HL 1 s)
    import time
    time.sleep(1)
    try:
        approved_bps = _query_on_chain_builder_fee(u.hl_account_address, config.BUILDER_ADDRESS)
    except Exception as e:
        # 2026-06-12 #32: HL hat die Submission akzeptiert, aber wir KONNTEN
        # nicht verifizieren → Flag NICHT setzen. User klickt "confirm"
        # (/api/builder-approved) erneut, sobald die Info-API antwortet.
        log.warning("post-submit verify failed (HL said ok): %s", e)
        return {"ok": False, "pending": True, "hl_response": hl_resp,
                "detail": "HL accepted the approval, but on-chain verification is unavailable — "
                          "please retry the confirm step in a moment.",
                "verify_warning": str(e)[:120]}
    if approved_bps < required_bps:
        # 2026-06-12 #32: Verifikation sagt NICHT genug bps → Flag NICHT
        # setzen (vorher: gesetzt trotz Fail = Orders würden auf Mainnet
        # abgelehnt). Kann eine lahme HL-Cache-Propagation sein → retry.
        log.warning("HL said ok but maxBuilderFee=%d < required %d — not setting builder_approved", approved_bps, required_bps)
        return {"ok": False, "pending": True, "hl_response": hl_resp,
                "approved_bps": approved_bps, "required_bps": required_bps,
                "detail": "Verification pending — on-chain approval not visible yet. "
                          "Please retry the confirm step in a moment."}
    u.builder_approved = True
    db.commit()
    return {"ok": True, "hl_response": hl_resp, "approved_bps": approved_bps, "required_bps": required_bps}


@app.post("/api/builder-approved")
def mark_builder_approved(u: User = Depends(current_user), db: Session = Depends(get_db)):
    """Nutzer bestätigt, dass er die Builder-Gebühr in der HL-UI freigegeben hat.

    Phase 5 (2026-06-02): vorher war das ein reiner Trust-me-Flag. Der Button
    setzte db.builder_approved=True ohne irgendeine Verifikation — was dazu
    führte, dass die Engine versuchte mit Builder-Code zu traden, HL den Trade
    aber ablehnte mit "Builder fee has not been approved". Jetzt prüfen wir
    on-chain via HL Info-API; nur wenn das echte Approval da ist, wird das
    Flag gesetzt.
    """
    if not config.BUILDER_ADDRESS:
        raise HTTPException(400, "Server has no BUILDER_ADDRESS configured — nothing to confirm.")
    if not u.hl_account_address:
        raise HTTPException(400, "Connect your wallet first.")
    from app.hyperliquid_exec import fee_to_int
    required_bps = fee_to_int(config.BUILDER_FEE)
    try:
        approved_bps = _query_on_chain_builder_fee(u.hl_account_address, config.BUILDER_ADDRESS)
    except Exception as e:
        raise HTTPException(502, f"Could not reach Hyperliquid to verify approval: {e}")
    if approved_bps < required_bps:
        raise HTTPException(
            400,
            (f"On-chain approval not found or insufficient (approved {approved_bps} bps, "
             f"need {required_bps} bps for fee {config.BUILDER_FEE}). "
             f"Open Hyperliquid → Builder Approvals and approve `{config.BUILDER_ADDRESS}` "
             f"for at least {config.BUILDER_FEE}, then click confirm again."),
        )
    u.builder_approved = True
    db.commit()
    return {"ok": True, "approved_bps": approved_bps, "required_bps": required_bps}


# ── Referral (HL-Account über Michaels Code verknüpfen) ──────────────────────
# Selbe Auth/Trader-Beschaffung wie die Builder-Endpoints: engine._build_trader(u)
# baut (async) einen HyperliquidTrader aus dem entschlüsselten Agent-Key des
# Users; darauf laufen referral_state()/set_referrer() (in hyperliquid_exec.py).
@app.get("/api/referral-status")
async def referral_status(u: User = Depends(current_user)):
    """Aktueller Referral-Status des Users.

    Ohne verbundene Wallet kein HL-Call — gibt nur Code/Link zurück. Jede
    Exception landet im error-Feld, der Endpoint wirft nie 500."""
    out = {
        "code": config.REFERRAL_CODE,
        "link": config.REFERRAL_LINK,
        "wallet_connected": bool(u.hl_account_address),
        "referred_by": None,
        "is_ours": False,
        "error": None,
    }
    if not u.hl_account_address:
        return out
    try:
        from app.engine import _build_trader
        trader = await _build_trader(u)
        state = await asyncio.to_thread(trader.referral_state)
        if state.get("error"):
            out["error"] = str(state["error"])
            return out
        referred_by = state.get("referred_by_code")
        out["referred_by"] = referred_by or None
        if referred_by and config.REFERRAL_CODE:
            out["is_ours"] = referred_by.lower() == config.REFERRAL_CODE.lower()
    except Exception as e:
        out["error"] = str(e)
    return out


@app.post("/api/set-referrer")
@limiter.limit("5/minute")
async def set_referrer(request: Request, u: User = Depends(current_user)):
    """Verknüpft den HL-Account des Users mit Michaels Referral-Code.

    Self-Referral (Michaels eigener Account) und "bereits referred" sind
    erwartete Fehlerfälle von HL → sauber als {ok: false, detail} zurück,
    nie 500."""
    if not u.hl_account_address:
        return {"ok": False, "detail": "Connect your wallet before linking a referral."}
    try:
        from app.engine import _build_trader
        trader = await _build_trader(u)
        res = await asyncio.to_thread(trader.set_referrer, config.REFERRAL_CODE)
    except Exception as e:
        return {"ok": False,
                "detail": f"Could not link the referral: {e}. "
                          f"You can also register via the referral link if this keeps failing: {config.REFERRAL_LINK}"}
    if res.get("ok"):
        return {"ok": True, "detail": "Referral linked ✓"}
    return {"ok": False,
            "detail": f"Could not link the referral: {res.get('error')}. "
                      f"If you already have a referrer or this is your own account this is expected — "
                      f"otherwise register via the referral link if this keeps failing: {config.REFERRAL_LINK}"}


# ── Dashboard-Daten (Live-PNL/Positionen + Aktivität) ────────────────────────
# 2026-06-08 Mainnet-Hardening C2: Snapshot-Cache.
# Vorher: jeder /api/dashboard call → 2-3 HL-Info-API-Calls. Bei 30 User
# × 10s poll = 5400 calls/min an HL-Info → Rate-Limit + Latenz.
# Jetzt: per-address Cache mit TTL=10s. Mehrere User mit gleicher Master
# (Familie/Friends) bekommen den gleichen Snapshot ohne extra HL-Call.
import time as _time
_snapshot_cache: dict = {}    # address → (timestamp, snapshot_dict)
_SNAPSHOT_TTL_S = 10


def _snapshot(address: str):
    """Read-only Konto-Snapshot + PnL-Statistik über die Info-API (kein Key nötig).
    Cached per address für _SNAPSHOT_TTL_S Sekunden.
    """
    now = _time.time()
    cached = _snapshot_cache.get(address)
    if cached and now - cached[0] < _SNAPSHOT_TTL_S:
        return cached[1]
    from app.hyperliquid_exec import get_info
    info = get_info(config.HL_TESTNET)
    st = info.user_state(address)
    bal = float(st.get("marginSummary", {}).get("accountValue", 0) or 0)
    # L-16 (2026-06-13): der Spot-USDC-Read wurde vorher mit `except: pass` still
    # geschluckt — schlug spot_user_state fehl (HL-Blip), zeigte das Dashboard eine
    # UNVOLLSTÄNDIGE Balance (nur Perps-Equity ohne freie Spot-USDC) OHNE jeden
    # Hinweis, dass etwas fehlt. Jetzt: Fehler loggen + im Snapshot markieren
    # (spot_balance_ok=False), damit das Frontend die Balance als unvollständig
    # kennzeichnen kann statt eine falsch-zu-niedrige Zahl als Wahrheit zu zeigen.
    spot_balance_ok = True
    try:
        for b in info.spot_user_state(address).get("balances", []):
            if b.get("coin") == "USDC":
                # 2026-06-08 Unified-Account-Fix: nur FREIE Spot-USDC (total−hold).
                # `hold` ist die als Perps-Margin reservierte USDC und steckt schon
                # in marginSummary.accountValue — volles total doppelzählte sie,
                # sobald Positionen offen waren (Dashboard zeigte ~$1582 statt der
                # echten HL-Equity ~$1083). Siehe HyperliquidTrader.account_value.
                bal += float(b.get("total", 0) or 0) - float(b.get("hold", 0) or 0)
    except Exception as e:
        spot_balance_ok = False
        log.warning("spot balance read failed for %s: %s — dashboard balance shown "
                    "WITHOUT free spot USDC (perps equity only)", address, e)
    def _opt_float(v):
        """None/"" → None, sonst float — HL liefert Zahlen als Strings."""
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    positions = []
    unrealized_total = 0.0
    exposure_total = 0.0
    for p in st.get("assetPositions", []):
        pos = p.get("position", {})
        if abs(float(pos.get("szi", 0) or 0)) > 0:
            size = float(pos.get("szi", 0) or 0)
            entry = float(pos.get("entryPx", 0) or 0)
            upnl = float(pos.get("unrealizedPnl", 0) or 0)
            # 2026-06-12 Dashboard-Extension: leverage/liq/mark/margin pro
            # Position durchreichen (Frontend-Kontrakt; alle float|null).
            lev_raw = pos.get("leverage")
            leverage = _opt_float(lev_raw.get("value")) if isinstance(lev_raw, dict) else _opt_float(lev_raw)
            mark_px = _opt_float(pos.get("markPx"))
            if mark_px is None:
                # markPx fehlt in manchen user_state-Antworten → aus
                # positionValue/|szi| ableiten (gleiche Definition).
                pos_val = _opt_float(pos.get("positionValue"))
                mark_px = (pos_val / abs(size)) if (pos_val is not None and size) else None
            positions.append({"coin": pos.get("coin"), "size": pos.get("szi"),
                              "entry": pos.get("entryPx"), "uPnl": pos.get("unrealizedPnl"),
                              "leverage": leverage,
                              "liquidation_px": _opt_float(pos.get("liquidationPx")),
                              "mark_px": mark_px,
                              "margin_used": _opt_float(pos.get("marginUsed"))})
            unrealized_total += upnl
            exposure_total += abs(size * entry)
    # PnL-Statistik aus realisierten Fills (Total PnL, Win-Rate, Verlauf, History)
    # Phase 2 (2026-06-02): Win-Rate auf TRADE-EVENTS basieren, nicht auf einzelnen
    # closing-Fills. Eine Position schließt oft in mehreren Partial-Fills (z. B. BTC
    # in 11 partial-fills @ 14:19); vorher zählten die als 11 separate Trades →
    # Win-Rate 47 % statt tatsächlich 36 %. Wir clustern jetzt nach
    # (coin, side, ≤60s Zeitfenster) zu echten Trade-Events.
    stats = {"total_pnl": 0.0, "win_rate": 0, "closed_trades": 0,
             "active_trades": len(positions), "pnl_series": [], "recent": []}
    try:
        fills = info.user_fills(address) or []
        fills.sort(key=lambda f: f.get("time", 0))

        # 1) pnl_series + cum: pro fill (granular für Chart-Verlauf, OK)
        cum = 0.0
        series = []
        for f in fills:
            pnl = float(f.get("closedPnl", 0) or 0)
            cum += pnl
            if pnl != 0:
                series.append({"t": int(f.get("time", 0) or 0), "cum": round(cum, 2)})

        # 2) Echte Trade-Events: cluster nach (coin, side) innerhalb 60s.
        # 2026-06-09 #WR-Fix: PRO (coin,side)-Key gruppieren, NICHT über einen
        # einzigen rollenden `current`. HL liefert Partial-Fills mehrerer Coins
        # zeitlich verschränkt (BTC, ETH, BTC, ...); der alte Code flushte beim
        # Coin-Wechsel und zerlegte einen Multi-Fill-Close in mehrere "Trades"
        # → falsche Win-Rate/Trade-Anzahl ("0% (1 closed trade)"). Jetzt hält
        # jedes Key sein eigenes laufendes Event.
        events = []
        open_ev = {}  # (coin,side) -> laufendes Event-Dict
        for f in fills:
            pnl = float(f.get("closedPnl", 0) or 0)
            if pnl == 0:
                continue  # Open-fills überspringen
            t = int(f.get("time", 0) or 0)
            coin = f.get("coin")
            d = (f.get("dir") or "")
            side = "Long" if "Long" in d else ("Short" if "Short" in d else "?")
            key = (coin, side)
            ev = open_ev.get(key)
            if ev is not None and t - ev["t_last"] <= 60_000:
                ev["pnl"] += pnl
                ev["t_last"] = t
            else:
                if ev is not None:
                    events.append(ev)
                open_ev[key] = {"key": key, "t": t, "t_last": t, "pnl": pnl}
        events.extend(open_ev.values())

        closed = len(events)
        wins = sum(1 for e in events if e["pnl"] > 0)
        stats["total_pnl"] = round(cum, 2)
        stats["closed_trades"] = closed
        stats["win_rate"] = round(100 * wins / closed) if closed else 0
        stats["pnl_series"] = series
        stats["recent"] = [
            {"t": int(f.get("time", 0) or 0), "coin": f.get("coin"), "dir": f.get("dir"),
             "px": f.get("px"), "pnl": round(float(f.get("closedPnl", 0) or 0), 2)}
            for f in reversed(fills[-15:])
        ]
    except Exception as e:
        log.warning("stats failed: %s", e)
    result = {
        "balance": round(bal, 2),
        "account_value": round(bal, 2),
        "unrealized_pnl": round(unrealized_total, 2),
        "open_exposure": round(exposure_total, 2),
        "open_positions": len(positions),
        "positions": positions,
        "stats": stats,
        # L-16: True = Balance enthält die freie Spot-USDC; False = Spot-Read
        # schlug fehl, Balance ist nur die Perps-Equity (unvollständig).
        "spot_balance_ok": spot_balance_ok,
    }
    _snapshot_cache[address] = (now, result)
    return result


def _demo_dashboard(u):
    """DEMO_MODE-Mock: realistische Zahlen fürs Frontend-/iPhone-Testen, OHNE
    echte HL-/DB-Abfragen (komplett getrennt vom Live-Bot)."""
    import time as _t, random as _r
    now_ms = int(_t.time() * 1000)
    series, cum = [], 0.0
    for i in range(30):
        cum += _r.uniform(-18, 28)
        series.append({"t": now_ms - (29 - i) * 86400000, "cum": round(cum, 2)})
    # 2026-06-12 Dashboard-Extension: Demo-Positionen tragen jetzt dieselben
    # Felder wie der Live-Pfad (leverage/liquidation_px/mark_px/margin_used/
    # stop_loss/take_profits) — damit das Frontend ohne HL-Anbindung demobar ist.
    positions = [
        {"coin": "BTC", "size": "0.085", "entry": "61767.0", "uPnl": "42.10",
         "leverage": 10.0, "liquidation_px": 56210.0, "mark_px": 62262.4, "margin_used": 529.2,
         "stop_loss": 60100.0, "take_profits": [{"price": 63500.0, "percent": 50.0},
                                                {"price": 65800.0, "percent": 50.0}]},
        {"coin": "ETH", "size": "-0.73", "entry": "1691.1", "uPnl": "24.30",
         "leverage": 15.0, "liquidation_px": 1804.6, "mark_px": 1657.8, "margin_used": 80.7,
         "stop_loss": 1645.0, "take_profits": [{"price": 1602.0, "percent": 50.0},
                                               {"price": 1551.0, "percent": 50.0}]},
        {"coin": "SOL", "size": "-14.4", "entry": "64.53", "uPnl": "15.80",
         "leverage": 12.0, "liquidation_px": 69.86, "mark_px": 63.43, "margin_used": 76.1,
         "stop_loss": 66.4, "take_profits": [{"price": 60.1, "percent": 100.0}]},
        {"coin": "SUI", "size": "-2624", "entry": "0.7653", "uPnl": "65.20",
         "leverage": 8.0, "liquidation_px": 0.8612, "mark_px": 0.7404, "margin_used": 251.0,
         "stop_loss": 0.792, "take_profits": [{"price": 0.701, "percent": 60.0},
                                              {"price": 0.658, "percent": 40.0}]},
        {"coin": "DOGE", "size": "-4072", "entry": "0.0867", "uPnl": "-8.40",
         "leverage": 5.0, "liquidation_px": 0.1031, "mark_px": 0.0888, "margin_used": 70.6,
         "stop_loss": None, "take_profits": []},   # bewusst: Frontend muss null/[] abkönnen
    ]
    upnl = round(sum(float(p["uPnl"]) for p in positions), 2)
    exposure = round(sum(abs(float(p["size"]) * float(p["entry"])) for p in positions), 2)
    acct_val = 3868.78
    return {
        "user": _user_public(u),
        "account": {"balance": acct_val, "account_value": acct_val, "unrealized_pnl": upnl,
                    "open_exposure": exposure, "open_positions": len(positions), "positions": positions},
        "stats": {"total_pnl": round(cum, 2), "win_rate": 42, "closed_trades": 12,
                  "active_trades": len(positions), "pnl_series": series,
                  "recent": [
                      {"t": now_ms - 3600000, "coin": "AVAX", "dir": "Open Short", "px": "6.70", "pnl": 0.0},
                      {"t": now_ms - 9000000, "coin": "ETH", "dir": "Close Short", "px": "1624.6", "pnl": 24.27},
                      {"t": now_ms - 18000000, "coin": "VIRTUAL", "dir": "Close Long", "px": "0.5646", "pnl": -110.64},
                  ]},
        "activity": [
            # M-21: Demo-Stempel im selben UTC-markierten ISO-Format wie der
            # Live-Pfad (isoformat()+"Z"), damit das Frontend sie identisch parst.
            {"ts": "2026-06-12T12:00:00Z", "kind": "order", "text": "Opened SHORT SUI (qty 2624), SL+TP set"},
            {"ts": "2026-06-12T11:30:00Z", "kind": "update", "text": "ETH: thesis adjusted — SL 1645, TP trailed"},
            {"ts": "2026-06-12T11:00:00Z", "kind": "skip", "text": "ADA not filled — no trade"},
        ],
        "net": "mainnet",   # Demo zeigt den echten Mainnet-Look
        "builder": {"address": config.BUILDER_ADDRESS or "", "fee": config.BUILDER_FEE},
        "server_time": now_ms,   # 2026-06-12 Dashboard-Extension (wie Live-Pfad)
        "demo": True,
    }


@app.get("/api/dashboard")
@limiter.limit("30/minute")
def dashboard(request: Request, u: User = Depends(current_user), db: Session = Depends(get_db)):
    if config.DEMO_MODE:
        return _demo_dashboard(u)
    acct = {"balance": None, "account_value": None, "unrealized_pnl": None,
            "open_exposure": None, "open_positions": 0, "positions": []}
    stats = {"total_pnl": 0.0, "win_rate": 0, "closed_trades": 0,
             "active_trades": 0, "pnl_series": [], "recent": []}
    if u.hl_account_address:
        try:
            snap = _snapshot(u.hl_account_address)
            # 2026-06-12: ALLE Snapshot-Metriken durchreichen — vorher nur balance
            # +positions, dadurch blieben die neuen Karten (account_value,
            # unrealized_pnl, open_exposure, open_positions) leer.
            # 2026-06-12 Dashboard-Extension: Positionen KOPIEREN bevor wir
            # user-spezifische SL/TP-Daten anhängen — der Snapshot ist per
            # Adresse gecacht und kann von mehreren Usern geteilt werden;
            # In-Place-Mutation würde fremde managed_trades in den Cache
            # schreiben.
            positions = [dict(p) for p in snap.get("positions", [])]
            # Offene ManagedTrades je Coin joinen → SL + TPs pro Position.
            import json as _json
            mt_by_coin = {}
            for t in (db.query(ManagedTrade)
                        .filter(ManagedTrade.user_id == u.id, ManagedTrade.status != "closed")
                        .order_by(ManagedTrade.id).all()):
                mt_by_coin[t.coin] = t
            for p in positions:
                t = mt_by_coin.get(p.get("coin"))
                stop_loss = None
                take_profits = []
                if t is not None:
                    stop_loss = float(t.stop_loss) if t.stop_loss is not None else None
                    try:
                        take_profits = [{"price": float(px), "percent": float(pct)}
                                        for px, pct in _json.loads(t.take_profits or "[]")]
                    except Exception:
                        take_profits = []
                p["stop_loss"] = stop_loss
                p["take_profits"] = take_profits
            acct = {
                "balance": snap.get("balance"),
                "account_value": snap.get("account_value"),
                "unrealized_pnl": snap.get("unrealized_pnl"),
                "open_exposure": snap.get("open_exposure"),
                "open_positions": snap.get("open_positions"),
                "positions": positions,
                # L-16: an die UI durchreichen, damit eine unvollständige Balance
                # (Spot-Read fehlgeschlagen) kennzeichenbar ist statt still falsch.
                "spot_balance_ok": snap.get("spot_balance_ok", True),
            }
            stats = snap["stats"]
        except Exception as e:
            log.warning("snapshot failed: %s", e)
    rows = (db.query(Activity).filter(Activity.user_id == u.id)
            .order_by(Activity.id.desc()).limit(30).all())
    # M-21 (2026-06-13, Hook #6): Activity.ts ist naiv-UTC (models.py-Konvention).
    # `isoformat()` lieferte vorher OHNE Offset (z.B. "2026-06-12T12:00:00") → der
    # Browser (new Date(...)) interpretiert einen offset-losen ISO-String als
    # LOKALzeit, nicht als UTC → die bekannte Dashboard-Zeitverschiebung (#6). Fix
    # an der Serialisierungs-Grenze: "Z" anhängen, damit der Browser den Stempel
    # eindeutig als UTC liest. (models.py bleibt unangetastet — der Bug gehört an
    # die UI-Grenze, nicht in die Zeit-Konvention.)
    activity = [{"ts": (a.ts.isoformat(timespec="seconds") + "Z") if a.ts else "",
                 "kind": a.kind, "text": a.text}
                for a in rows]
    return {"user": _user_public(u), "account": acct, "stats": stats, "activity": activity,
            "net": "testnet" if config.HL_TESTNET else "mainnet",
            "builder": {"address": config.BUILDER_ADDRESS or "", "fee": config.BUILDER_FEE},
            # 2026-06-12 Dashboard-Extension: Server-Uhrzeit (epoch ms) — das
            # Frontend kann damit Zeitstempel konsistent relativieren
            # (Known issue #6: UTC-vs-Lokalzeit-Mix).
            "server_time": int(_time.time() * 1000)}


# ── Dashboard-Seite ──────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def index():
    path = os.path.join(os.path.dirname(__file__), "dashboard.html")
    with open(path, encoding="utf-8") as f:
        return f.read()


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    """Phase 3 (2026-06-02): Admin-Dashboard. Auth-Gate sitzt im Frontend
    (JS prüft /api/me.is_admin), das echte Gate ist in /api/admin/*."""
    path = os.path.join(os.path.dirname(__file__), "admin.html")
    with open(path, encoding="utf-8") as f:
        return f.read()


@app.get("/api/health")
def health():
    """2026-06-12 LOW-10: nur noch minimale Liveness-Probe. listener-Status
    und testnet/mainnet leakten vorher an JEDEN unauthentifizierten Caller —
    beide Felder stehen (auth-gated) in /api/admin/health. Bekannte
    Konsumenten: Deploy-Workflow (curlt nur den 200er) und dashboard.js
    publicStatus (liest jetzt `status`)."""
    return {"status": "ok"}
