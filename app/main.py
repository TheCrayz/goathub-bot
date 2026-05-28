"""GoatHub Trading Bot — Multi-User-Plattform (Backend + Dashboard)."""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
import httpx

from app import config
from app.auth import current_user, hash_pw, make_token, verify_pw
from app.db import get_db, init_db
from app.models import Activity, User
from app.schemas import Login, Register, SettingsIn, WalletIn
from app.discord_oauth import exchange_code, get_discord_user, get_guild_member, has_required_role

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


# ── Auth ─────────────────────────────────────────────────────────────────────
@app.post("/api/register")
def register(body: Register, db: Session = Depends(get_db)):
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
    return {"access_token": make_token(u.id), "token_type": "bearer"}


@app.post("/api/login")
def login(body: Login, db: Session = Depends(get_db)):
    u = db.query(User).filter(User.email == body.email.strip().lower()).first()
    if not u or not verify_pw(body.password, u.password_hash):
        raise HTTPException(401, "Wrong email or password")
    return {"access_token": make_token(u.id), "token_type": "bearer"}


@app.get("/auth/discord")
def discord_login():
    """Redirect to Discord OAuth2."""
    scopes = "identify guilds"
    url = (
        f"https://discord.com/oauth2/authorize"
        f"?client_id={config.DISCORD_CLIENT_ID}"
        f"&redirect_uri={config.DISCORD_REDIRECT_URI}"
        f"&response_type=code"
        f"&scope={scopes.replace(' ', '%20')}"
    )
    return RedirectResponse(url)


@app.get("/auth/callback")
async def discord_callback(code: str = None, error: str = None, db: Session = Depends(get_db)):
    """Handle Discord OAuth callback."""
    if error or not code:
        return RedirectResponse("/?error=discord_denied")

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
                return RedirectResponse("/?error=no_role")

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

        jwt_token = make_token(u.id)
        # Redirect to dashboard with token in URL fragment (not logged, not in server logs)
        return RedirectResponse(f"/?token={jwt_token}")

    except Exception as e:
        log.error(f"Discord OAuth error: {e}")
        return RedirectResponse("/?error=oauth_failed")


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
        u.risk_pct = max(0.001, min(0.5, body.risk_pct))
    if body.leverage is not None:
        u.leverage = max(1, min(50, body.leverage))
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
    if not addr.startswith("0x") or len(addr) < 20 or not sec:
        raise HTTPException(400, "Valid MASTER address (0x...) + Agent Key required")
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
    """Read-only Konto-Snapshot über die Info-API (kein Key nötig)."""
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
    return {"balance": round(bal, 2), "positions": positions}


@app.get("/api/dashboard")
def dashboard(u: User = Depends(current_user), db: Session = Depends(get_db)):
    acct = {"balance": None, "positions": []}
    if u.hl_account_address:
        try:
            acct = _snapshot(u.hl_account_address)
        except Exception as e:
            log.warning("snapshot failed: %s", e)
    rows = (db.query(Activity).filter(Activity.user_id == u.id)
            .order_by(Activity.id.desc()).limit(30).all())
    activity = [{"ts": a.ts.isoformat(timespec="seconds") if a.ts else "", "kind": a.kind, "text": a.text}
                for a in rows]
    return {"user": _user_public(u), "account": acct, "activity": activity,
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
