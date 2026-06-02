"""Admin-Router (Phase 3, 2026-06-02).

Liefert die Endpoints für das Admin-Dashboard:
  GET  /api/admin/users        — alle Nutzer (Status, Key-Länge, Trades-Zahl)
  POST /api/admin/users/{id}/pause   — bot_active=False
  POST /api/admin/users/{id}/resume  — bot_active=True (nur wenn Wallet ok)
  GET  /api/admin/activity     — letzte Activity-Zeilen aller Nutzer
  GET  /api/admin/health       — signal-bot + goathub Live-Health

Nur erreichbar für User.is_admin == True (sonst 403).
"""
import datetime
import logging
import os

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import config
from app.auth import current_user
from app.crypto import decrypt
from app.db import SessionLocal, get_db
from app.models import Activity, ManagedTrade, User

log = logging.getLogger("goathub.admin")

router = APIRouter(prefix="/api/admin", tags=["admin"])


def current_admin_user(u: User = Depends(current_user)) -> User:
    """Wirft 403, wenn nicht-Admin den Endpoint trifft."""
    if not bool(getattr(u, "is_admin", False)):
        raise HTTPException(403, "Admin only")
    return u


def _key_len(enc: str | None) -> int:
    """Decrypted-Key-Länge (66 = ok, 42 = Adresse-statt-Key, 0 = leer)."""
    if not enc:
        return 0
    try:
        return len(decrypt(enc))
    except Exception:
        return -1   # encrypted-aber-nicht-entschlüsselbar (defekte Daten)


@router.get("/users")
def admin_users(db: Session = Depends(get_db), _: User = Depends(current_admin_user)):
    """Übersichtstabelle aller Nutzer."""
    out = []
    for u in db.query(User).order_by(User.id).all():
        klen = _key_len(u.hl_api_secret_enc)
        addr = u.hl_account_address or ""
        addr_short = (addr[:6] + ".." + addr[-4:]) if addr else None
        # Trade-Zähler aus managed_trades
        n_open = db.query(ManagedTrade).filter(
            ManagedTrade.user_id == u.id, ManagedTrade.status != "closed"
        ).count()
        n_closed = db.query(ManagedTrade).filter(
            ManagedTrade.user_id == u.id, ManagedTrade.status == "closed"
        ).count()
        # Errors letzte 24h
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=1)
        n_err_24h = db.query(Activity).filter(
            Activity.user_id == u.id, Activity.kind == "error", Activity.ts >= cutoff
        ).count()
        out.append({
            "id": u.id,
            "email": u.email,
            "discord_username": u.discord_username,
            "bot_active": bool(u.bot_active),
            "is_admin": bool(getattr(u, "is_admin", False)),
            "builder_approved": bool(u.builder_approved),
            "wallet_connected": bool(u.hl_api_secret_enc),
            "address_short": addr_short,
            "key_length": klen,
            "key_status": (
                "ok" if klen == 66 else
                "address?!" if klen == 42 else
                "no key" if klen == 0 else
                "error"
            ),
            "open_trades": n_open,
            "closed_trades": n_closed,
            "errors_24h": n_err_24h,
            "created_at": u.created_at.isoformat(timespec="seconds") if u.created_at else None,
        })
    return out


@router.post("/users/{user_id}/pause")
def admin_pause_user(
    user_id: int,
    reason: str = "paused by admin",
    db: Session = Depends(get_db),
    admin: User = Depends(current_admin_user),
):
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(404, "User not found")
    if u.id == admin.id:
        raise HTTPException(400, "Cannot pause yourself via admin API")
    u.bot_active = False
    db.add(Activity(user_id=u.id, kind="error", text=f"Bot pausiert (admin: {admin.email or admin.discord_username}): {reason[:200]}"))
    db.commit()
    return {"ok": True, "user_id": u.id, "bot_active": False}


@router.post("/users/{user_id}/resume")
def admin_resume_user(
    user_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(current_admin_user),
):
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(404, "User not found")
    if not u.hl_api_secret_enc:
        raise HTTPException(400, "User has no wallet connected — cannot resume.")
    if _key_len(u.hl_api_secret_enc) != 66:
        raise HTTPException(400, "User's key looks malformed (length != 66). They need to re-save in their dashboard first.")
    u.bot_active = True
    db.add(Activity(user_id=u.id, kind="order", text=f"Bot wieder aktiviert (admin: {admin.email or admin.discord_username})"))
    db.commit()
    return {"ok": True, "user_id": u.id, "bot_active": True}


@router.get("/activity")
def admin_activity(
    limit: int = 100,
    kind: str | None = None,
    user_id: int | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(current_admin_user),
):
    """Activity über ALLE Nutzer hinweg. Filter optional."""
    q = db.query(Activity)
    if kind:
        q = q.filter(Activity.kind == kind)
    if user_id is not None:
        q = q.filter(Activity.user_id == user_id)
    rows = q.order_by(Activity.id.desc()).limit(max(1, min(limit, 500))).all()
    return [{
        "id": a.id,
        "ts": a.ts.isoformat(timespec="seconds") if a.ts else None,
        "user_id": a.user_id,
        "kind": a.kind,
        "text": a.text,
    } for a in rows]


@router.get("/health")
def admin_health(_: User = Depends(current_admin_user)):
    """Live-Health beider Bots: signal-bot (Docker) + goathub (this).
    Liest das letzte Per-Cycle-Health-Log aus der bot_logs-Tabelle des
    Signal-bots, falls erreichbar — sonst zeigt das was wir lokal wissen.
    """
    out = {
        "goathub": {
            "listener_enabled": config.ENABLE_LISTENER,
            "net": "testnet" if config.HL_TESTNET else "mainnet",
            "builder_configured": bool(config.BUILDER_ADDRESS),
            "builder_fee": config.BUILDER_FEE,
        },
        "signalbot": {"reachable": False, "last_cycle_summary": None, "error": None},
    }
    # Signal-bot DB ist in einem Docker-Volume — Pfad aus env oder Default
    sb_db_path = os.getenv(
        "SIGNALBOT_DB_PATH",
        "/var/lib/docker/volumes/tradinghub-signalbeta_signalbeta-data/_data/charthub.db",
    )
    if os.path.exists(sb_db_path):
        try:
            import sqlite3
            con = sqlite3.connect(f"file:{sb_db_path}?mode=ro", uri=True, timeout=2)
            cur = con.cursor()
            cur.execute(
                "SELECT timestamp, message FROM bot_logs "
                "WHERE message LIKE 'CYCLE COMPLETE:%' "
                "ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
            con.close()
            if row:
                out["signalbot"]["reachable"] = True
                out["signalbot"]["last_cycle_summary"] = {"ts": row[0], "text": row[1]}
        except Exception as e:
            out["signalbot"]["error"] = str(e)[:200]
    else:
        out["signalbot"]["error"] = f"signal-bot DB not found at {sb_db_path}"
    return out
