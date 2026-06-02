"""GoatHub Trading Bot — Multi-User-Plattform (Backend + Dashboard)."""
import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
import httpx
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app import config
from app.auth import current_user, hash_pw, make_token, verify_pw
from app.db import get_db, init_db
from app.models import Activity, User
from app.schemas import Login, Register, SettingsIn, WalletIn
from app.discord_oauth import exchange_code, get_discord_user, get_guild_member, has_required_role


# ── Rate limiting (Phase 1, 2026-06-02) ─────────────────────────────────────
# Honor X-Forwarded-For from the Caddy reverse-proxy so login attempts are
# bucketed by real client IP, not by the bridge IP of whatever proxy sits
# in front of uvicorn.
def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return get_remote_address(request)


limiter = Limiter(key_func=_client_ip)
LOGIN_RATE_LIMIT = os.getenv("LOGIN_RATE_LIMIT", "10/5minute")
REGISTER_RATE_LIMIT = os.getenv("REGISTER_RATE_LIMIT", "5/5minute")

OAUTH_STATE_COOKIE = "discord_oauth_state"
OAUTH_STATE_TTL_S = 600  # 10 min, plenty for a sane user flow

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("goathub")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    task = None
    if config.ENABLE_LISTENER:
        from app.discord_listener import start_listener
        task = asyncio.create_task(start_listener())
        log.info("Discord-Listener gestartet.")
    else:
        log.info("Listener AUS (ENABLE_LISTENER=false) — API/Dashboard laufen, kein Live-Trading.")
    yield
    if task:
        task.cancel()


app = FastAPI(title="GoatHub Trading Bot", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# ── Auth ─────────────────────────────────────────────────────────────────────
@app.post("/api/register")
@limiter.limit(REGISTER_RATE_LIMIT)
def register(request: Request, body: Register, db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    if "@" not in email or len(body.password) < 6:
        raise HTTPException(400, "Valid email + password (min. 6 chars) required")
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(409, "Email already registered")
    u = User(email=email, password_hash=hash_pw(body.password),
             risk_pct=config.DEFAULT_RISK_PCT, leverage=config.DEFAULT_LEVERAGE,
             max_open_positions=config.DEFAULT_MAX_OPEN)
    db.add(u)
    db.commit()
    db.refresh(u)
    return {"access_token": make_token(u.id, getattr(u, "token_version", 0)), "token_type": "bearer"}


@app.post("/api/login")
@limiter.limit(LOGIN_RATE_LIMIT)
def login(request: Request, body: Login, db: Session = Depends(get_db)):
    u = db.query(User).filter(User.email == body.email.strip().lower()).first()
    if not u or not verify_pw(body.password, u.password_hash):
        raise HTTPException(401, "Wrong email or password")
    return {"access_token": make_token(u.id, getattr(u, "token_version", 0)), "token_type": "bearer"}


@app.post("/api/logout")
def logout(u: User = Depends(current_user), db: Session = Depends(get_db)):
    """Server-side logout — bumps token_version so every JWT issued before
    this point stops validating. Even if a token was exfiltrated via XSS, it
    is now useless after the user clicks Logout."""
    u.token_version = int(getattr(u, "token_version", 0) or 0) + 1
    db.commit()
    return {"ok": True, "message": "Alle bestehenden Sessions invalidiert."}


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

        jwt_token = make_token(u.id, getattr(u, "token_version", 0))
        # Token im URL-FRAGMENT (#) — Fragmente werden NICHT an den Server gesendet,
        # landen also nicht in Access-Logs/Proxys/Referer. (vorher ?token= = Leak-Risiko)
        resp = RedirectResponse(f"/#token={jwt_token}")
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
            "settings": {"risk_pct": u.risk_pct, "leverage": u.leverage,
                         "max_open_positions": u.max_open_positions,
                         "capital_cap_usdc": u.capital_cap_usdc}}


@app.get("/api/me")
def me(u: User = Depends(current_user)):
    return _user_public(u)


# ── Settings & Wallet ────────────────────────────────────────────────────────
@app.put("/api/settings")
def update_settings(body: SettingsIn, u: User = Depends(current_user), db: Session = Depends(get_db)):
    if body.risk_pct is not None:
        u.risk_pct = max(0.001, min(0.05, body.risk_pct))   # max 5% Risiko/Trade (war 50%)
    if body.leverage is not None:
        u.leverage = max(1, min(20, body.leverage))          # max 20x Hebel (war 50x)
    if body.max_open_positions is not None:
        u.max_open_positions = max(1, min(50, body.max_open_positions))
    if body.capital_cap_usdc is not None:
        u.capital_cap_usdc = max(0, body.capital_cap_usdc)     # 0 = ganzer Account
    if body.bot_active is not None:
        if body.bot_active and not u.hl_api_secret_enc:
            raise HTTPException(400, "Connect your wallet before activating the bot")
        u.bot_active = body.bot_active
    db.commit()
    return _user_public(u)


@app.post("/api/wallet")
def set_wallet(body: WalletIn, u: User = Depends(current_user), db: Session = Depends(get_db)):
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


@app.post("/api/builder-approved")
def mark_builder_approved(u: User = Depends(current_user), db: Session = Depends(get_db)):
    """Nutzer bestätigt, dass er die Builder-Gebühr in der HL-UI freigegeben hat."""
    u.builder_approved = True
    db.commit()
    return {"ok": True}


# ── Dashboard-Daten (Live-PNL/Positionen + Aktivität) ────────────────────────
def _snapshot(address: str):
    """Read-only Konto-Snapshot + PnL-Statistik über die Info-API (kein Key nötig)."""
    from hyperliquid.info import Info
    from hyperliquid.utils import constants
    info = Info(constants.TESTNET_API_URL if config.HL_TESTNET else constants.MAINNET_API_URL, skip_ws=True)
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
    stats = {"total_pnl": 0.0, "win_rate": 0, "closed_trades": 0,
             "active_trades": len(positions), "pnl_series": [], "recent": []}
    try:
        fills = info.user_fills(address) or []
        fills.sort(key=lambda f: f.get("time", 0))
        cum = 0.0
        closed = wins = 0
        series = []
        for f in fills:
            pnl = float(f.get("closedPnl", 0) or 0)
            cum += pnl
            if pnl != 0:
                closed += 1
                wins += 1 if pnl > 0 else 0
                series.append({"t": int(f.get("time", 0) or 0), "cum": round(cum, 2)})
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
    return {"balance": round(bal, 2), "positions": positions, "stats": stats}


@app.get("/api/dashboard")
def dashboard(u: User = Depends(current_user), db: Session = Depends(get_db)):
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


@app.get("/api/health")
def health():
    return {"ok": True, "listener": config.ENABLE_LISTENER, "net": "testnet" if config.HL_TESTNET else "mainnet"}
