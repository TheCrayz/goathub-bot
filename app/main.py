"""GoatHub Trading Bot — Multi-User-Plattform (Backend + Dashboard)."""
import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session
import httpx
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app import config
from app.auth import (MAX_PW_BYTES, PasswordTooLongError, current_user, hash_pw,
                      make_token, needs_rehash, verify_pw, _user_identity)
from app.db import get_db, init_db
from app.models import Activity, User
from app.schemas import Login, Register, SettingsIn, WalletIn
from app.discord_oauth import exchange_code, get_discord_user, get_guild_member, has_required_role


# ── Rate limiting (Phase 1, 2026-06-02, gefixt 2026-06-04) ──────────────────
# 2026-06-04 KRITISCH: Rate-Limit-Bypass via X-Forwarded-For Spoofing entdeckt!
# Vorher: erster Wert aus X-Forwarded-For — vom Client KOMPLETT kontrollierbar,
# weil nginx append-mode ($proxy_add_x_forwarded_for) jeden Attacker-Eintrag
# durchlässt und nur DAHINTER den echten Client appendet. 15 verschiedene Fakes
# → 15× kein Block. Jetzt: nginx setzt X-Real-IP zum echten Client-IP — das
# nehmen wir. Fallback: letzter Wert in X-Forwarded-For (= was nginx appendiert
# hat = echter Client). Letzter Fallback: TCP-Peer.
def _client_ip(request: Request) -> str:
    # Primary: X-Real-IP von nginx (proxy_set_header X-Real-IP $remote_addr).
    # nginx-config selbst kontrolliert was hier landet, nicht der Client.
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    # Fallback: letzter Wert in der XFF-Chain (nginx appendet immer als letztes).
    # Achtung: NICHT der erste — der ist attacker-controlled wenn nginx kein
    # XFF-Clearing macht.
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[-1].strip()
    # Letzter Ausweg: direkte TCP-Peer-Address (bei direkt-am-Server, ohne Proxy).
    return get_remote_address(request)


limiter = Limiter(key_func=_client_ip)
LOGIN_RATE_LIMIT = os.getenv("LOGIN_RATE_LIMIT", "10/5minute")
REGISTER_RATE_LIMIT = os.getenv("REGISTER_RATE_LIMIT", "5/5minute")

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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("goathub")


async def _activity_purge_loop():
    """Phase 4 (2026-06-02): Activity-Tabelle TTL-purge täglich.
    Behalte 90 Tage Historie, lösche älter. Verhindert unbeschränktes Wachstum
    (aktuell ~70 rows/Tag = ~25k/Jahr; nach 5 Jahren wird's spürbar).
    """
    import datetime
    PURGE_KEEP_DAYS = int(os.getenv("ACTIVITY_KEEP_DAYS", "90"))
    # Beim ersten Mal nach 60 s starten — nicht direkt beim Service-Start
    # (damit Boot-Logs nicht zugespammt werden), dann täglich.
    await asyncio.sleep(60)
    while True:
        try:
            from app.db import SessionLocal
            db = SessionLocal()
            try:
                cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=PURGE_KEEP_DAYS)
                deleted = db.query(Activity).filter(Activity.ts < cutoff).delete(synchronize_session=False)
                db.commit()
                if deleted:
                    log.info("activity-purge: %d alte Zeilen gelöscht (älter als %d Tage)", deleted, PURGE_KEEP_DAYS)
            finally:
                db.close()
        except Exception as e:
            log.warning("activity-purge fehlgeschlagen: %s", e)
        await asyncio.sleep(86400)  # 1 Tag


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    tasks = []

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

    if config.ENABLE_LISTENER:
        from app.discord_listener import start_listener
        t = asyncio.create_task(start_listener())
        _attach_logger(t, "discord_listener")
        tasks.append(t)
        log.info("Discord-Listener gestartet.")
    else:
        log.info("Listener AUS (ENABLE_LISTENER=false) — API/Dashboard laufen, kein Live-Trading.")
    # Activity-Purge läuft IMMER (auch wenn Listener aus ist) — die Tabelle
    # wächst auch über manuelle Settings-Änderungen, Login-Events etc.
    t = asyncio.create_task(_activity_purge_loop())
    _attach_logger(t, "activity_purge_loop")
    tasks.append(t)
    # Phase 6+ (2026-06-03): Position-Sync-Loop — reconcilet managed_trades
    # gegen die echte HL-Position-State. Verhindert "stale open"-Rows wenn
    # HL via SL/TP autonom schließt (siehe SOL id=24 vom 03:38 UTC).
    from app.sync import position_sync_loop
    t = asyncio.create_task(position_sync_loop())
    _attach_logger(t, "position_sync_loop")
    tasks.append(t)
    # 2026-06-04 Restposten #5: Token-Usage-Scrape-Loop — persistet signal-bot
    # TOKEN_USAGE-Zeilen in unsere DB, damit Historie auch nach Log-Rotation
    # erhalten bleibt. Liest bot.log alle 5 Minuten, idempotent (skip wenn
    # ts+counts schon in DB).
    from app.token_usage_scraper import token_usage_scrape_loop
    t = asyncio.create_task(token_usage_scrape_loop())
    _attach_logger(t, "token_usage_scrape_loop")
    tasks.append(t)
    yield
    for t in tasks:
        t.cancel()


# 2026-06-04 Audit-Find: /docs, /redoc und /openapi.json waren publik ohne Auth
# erreichbar — kompletter API-Surface inkl. Admin-Endpoints + Query-Params
# leakte raus (curl https://bot.goathub.network/openapi.json → 200, voller
# Schema-Dump). Default jetzt: aus. Wer das Schema lokal in der UI explorieren
# will, setzt ENABLE_DOCS=true in der .env — prod bleibt zu.
_ENABLE_DOCS = os.getenv("ENABLE_DOCS", "false").strip().lower() in ("1", "true", "yes", "on")
app = FastAPI(
    title="GoatHub Trading Bot",
    lifespan=lifespan,
    docs_url="/docs" if _ENABLE_DOCS else None,
    redoc_url="/redoc" if _ENABLE_DOCS else None,
    openapi_url="/openapi.json" if _ENABLE_DOCS else None,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

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
from fastapi.responses import JSONResponse  # für Cookie-setting Response


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


@app.post("/api/login")
@limiter.limit(LOGIN_RATE_LIMIT)
def login(request: Request, body: Login, db: Session = Depends(get_db)):
    u = db.query(User).filter(User.email == body.email.strip().lower()).first()
    if not u or not verify_pw(body.password, u.password_hash):
        raise HTTPException(401, "Wrong email or password")
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
    response = JSONResponse({"ok": True, "message": "Alle bestehenden Sessions invalidiert."})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


# 2026-06-08 Mainnet-Hardening B4: JWT-Refresh-Endpoint.
# JWT_EXPIRE_HOURS ist auf 24h gekürzt (statt 168h = 7d). Dashboard pollt
# diesen Endpoint alle paar Stunden, bekommt einen neuen JWT mit frischem
# exp. Solange der aktuelle noch valid + < 1h vor expiry, wird refreshed.
@app.post("/api/refresh")
@limiter.limit("60/minute")
def refresh_token(request: Request, u: User = Depends(current_user)):
    """Mintet einen neuen JWT für den aktuellen User. Voraussetzung: alter
    JWT noch valid (current_user dependency erfüllt) — bei expired wird's
    durch current_user ohnehin 401."""
    new_tok = make_token(u.id, getattr(u, "token_version", 0), _user_identity(u))
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

        # Find or create user by discord_id
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

        jwt_token = make_token(u.id, getattr(u, "token_version", 0), _user_identity(u))
        # Phase 2 #18 (2026-06-02): NEUE Strategie — Session-Cookie (httpOnly).
        # Das URL-Fragment-Token bleibt zusätzlich für Backward-Compat erhalten,
        # aber das Dashboard wird's nicht mehr in localStorage stecken — der
        # XSS-Exfil-Vektor ist damit zu. Wenn beide Pfade aktiv sind, gewinnt
        # das Cookie in `current_user` (cookie-first).
        resp = RedirectResponse(f"/#token={jwt_token}")
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


@app.get("/api/per-coin-status")
def per_coin_status(u: User = Depends(current_user)):
    """Per-coin filter status für DEN AKTUELLEN USER.

    2026-06-04: Tester sehen jetzt direkt im Dashboard warum ein Coin
    geskippt wird (Win-Rate < Schwelle nach genug Trades), statt nur via
    Admin-Endpoint. Identisches Format wie admin_per_coin aber gefiltert
    auf u.hl_account_address; ein einzelner User-Eintrag in 'users'.
    """
    if not u.hl_account_address:
        return {"connected": False, "min_trades_required": config.PERCOIN_MIN_TRADES,
                "min_winrate": config.PERCOIN_MIN_WINRATE, "coins": []}
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
    return {
        "connected": True,
        "min_trades_required": config.PERCOIN_MIN_TRADES,
        "min_winrate": config.PERCOIN_MIN_WINRATE,
        "coins": coins_out,
    }


# ── Settings & Wallet ────────────────────────────────────────────────────────
# 2026-06-08 Mainnet-Hardening B2: rate-limit auf authenticated state-mutating
# routes (60/min) und auf häufig gepollte read-routes (30/min). Verhindert
# resource-exhaust via authed user, schützt unsere IP gegen HL-info-DoS.
@app.put("/api/settings")
@limiter.limit("60/minute")
def update_settings(request: Request, body: SettingsIn, u: User = Depends(current_user), db: Session = Depends(get_db)):
    if body.risk_pct is not None:
        u.risk_pct = max(0.001, min(0.05, body.risk_pct))   # max 5% Risiko/Trade (war 50%)
    if body.leverage is not None:
        # 2026-06-06: leverage = USER-MAX-CAP für Auto-Leverage.
        # Bot rechnet pro Trade aus SL+Confidence den optimalen Hebel, gecappt
        # an diesem Wert. 50 = HL Perps Max. Niedrigere Werte = User will
        # konservativer fahren (z.B. 10x als persönliches Maximum).
        u.leverage = max(1, min(50, body.leverage))
    if body.max_open_positions is not None:
        u.max_open_positions = max(1, min(50, body.max_open_positions))
    if body.capital_cap_usdc is not None:
        u.capital_cap_usdc = max(0, body.capital_cap_usdc)     # 0 = ganzer Account
    if body.bot_active is not None:
        if body.bot_active and not u.hl_api_secret_enc:
            raise HTTPException(400, "Connect your wallet before activating the bot")
        u.bot_active = body.bot_active
    if body.max_drawdown_pct is not None:
        # 2026-06-08 C1: 0 = disabled, max 0.95 (95% drawdown cap)
        u.max_drawdown_pct = max(0.0, min(0.95, body.max_drawdown_pct))
    db.commit()
    return _user_public(u)


@app.post("/api/wallet")
@limiter.limit("60/minute")
def set_wallet(request: Request, body: WalletIn, u: User = Depends(current_user), db: Session = Depends(get_db)):
    from app.crypto import encrypt
    addr = body.hl_account_address.strip()
    sec = body.hl_api_secret.strip()
    # MASTER-Adresse: 0x + 40 Hex = 42 Zeichen
    if not addr.startswith("0x") or len(addr) != 42:
        raise HTTPException(400, "MASTER address must be 0x + 40 chars (42 total). That's the public address, not the key.")
    # Agent-Key SOFORT validieren (sonst scheitert es erst beim Trade — der häufigste Fehler!)
    try:
        from eth_account import Account
        agent_addr = Account.from_key(sec).address
    except Exception:
        raise HTTPException(400, "Invalid Agent key. It must be the long private key (0x + 64 chars = 66 total) — NOT an address.")
    if agent_addr.lower() == addr.lower():
        raise HTTPException(400, "This key belongs to the MASTER address. You need the separate AGENT key (from the API-wallet 'Generate' box).")
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
def submit_builder_approval(
    body: BuilderApprovalSubmit,
    u: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Phase 6+ (2026-06-03): receive the MetaMask-signed approveBuilderFee
    payload, forward it to HL, verify success, set DB flag.

    Frontend builds the EIP-712 payload, asks MetaMask to sign, gives us
    {action, signature, nonce}. We just relay to HL — HL itself validates
    that the signature comes from the user's master address.
    """
    if not config.BUILDER_ADDRESS:
        raise HTTPException(400, "Server has no BUILDER_ADDRESS configured")
    if not u.hl_account_address:
        raise HTTPException(400, "Connect your wallet first")
    # Sanity: action.builder must match our configured BUILDER_ADDRESS
    action_builder = str(body.action.get("builder", "")).lower()
    if action_builder != config.BUILDER_ADDRESS.lower():
        raise HTTPException(400, f"action.builder mismatch (got {action_builder[:10]}…, expected our builder)")
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
        from app.hyperliquid_exec import fee_to_int
        approved_bps = _query_on_chain_builder_fee(u.hl_account_address, config.BUILDER_ADDRESS)
        required_bps = fee_to_int(config.BUILDER_FEE)
    except Exception as e:
        # HL said ok but on-chain query failed → trust HL, mark approved anyway
        log.warning("post-submit verify failed (HL said ok): %s", e)
        u.builder_approved = True
        db.commit()
        return {"ok": True, "hl_response": hl_resp, "verify_warning": str(e)[:120]}
    if approved_bps < required_bps:
        log.warning("HL said ok but maxBuilderFee=%d < required %d — race condition?", approved_bps, required_bps)
        # Still set the flag — HL clearly accepted; verify might just be slow
        u.builder_approved = True
        db.commit()
        return {"ok": True, "hl_response": hl_resp, "approved_bps": approved_bps, "note": "Verify lagging — HL accepted submission"}
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
    try:
        for b in info.spot_user_state(address).get("balances", []):
            if b.get("coin") == "USDC":
                bal += float(b.get("total", 0) or 0)
    except Exception:
        pass
    positions = []
    for p in st.get("assetPositions", []):
        pos = p.get("position", {})
        if abs(float(pos.get("szi", 0) or 0)) > 0:
            positions.append({"coin": pos.get("coin"), "size": pos.get("szi"),
                              "entry": pos.get("entryPx"), "uPnl": pos.get("unrealizedPnl")})
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
        events = []
        current = None
        for f in fills:
            pnl = float(f.get("closedPnl", 0) or 0)
            if pnl == 0:
                continue  # Open-fills überspringen
            t = int(f.get("time", 0) or 0)
            coin = f.get("coin")
            d = (f.get("dir") or "")
            side = "Long" if "Long" in d else ("Short" if "Short" in d else "?")
            key = (coin, side)
            if current and current["key"] == key and t - current["t_last"] <= 60_000:
                current["pnl"] += pnl
                current["t_last"] = t
            else:
                if current is not None:
                    events.append(current)
                current = {"key": key, "t": t, "t_last": t, "pnl": pnl}
        if current is not None:
            events.append(current)

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
    result = {"balance": round(bal, 2), "positions": positions, "stats": stats}
    _snapshot_cache[address] = (now, result)
    return result


@app.get("/api/dashboard")
@limiter.limit("30/minute")
def dashboard(request: Request, u: User = Depends(current_user), db: Session = Depends(get_db)):
    acct = {"balance": None, "positions": []}
    stats = {"total_pnl": 0.0, "win_rate": 0, "closed_trades": 0,
             "active_trades": 0, "pnl_series": [], "recent": []}
    if u.hl_account_address:
        try:
            snap = _snapshot(u.hl_account_address)
            acct = {"balance": snap["balance"], "positions": snap["positions"]}
            stats = snap["stats"]
        except Exception as e:
            log.warning("snapshot failed: %s", e)
    rows = (db.query(Activity).filter(Activity.user_id == u.id)
            .order_by(Activity.id.desc()).limit(30).all())
    activity = [{"ts": a.ts.isoformat(timespec="seconds") if a.ts else "", "kind": a.kind, "text": a.text}
                for a in rows]
    return {"user": _user_public(u), "account": acct, "stats": stats, "activity": activity,
            "net": "testnet" if config.HL_TESTNET else "mainnet",
            "builder": {"address": config.BUILDER_ADDRESS or "", "fee": config.BUILDER_FEE}}


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
    return {"ok": True, "listener": config.ENABLE_LISTENER, "net": "testnet" if config.HL_TESTNET else "mainnet"}
