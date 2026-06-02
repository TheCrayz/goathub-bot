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


@router.get("/trades")
def admin_trades(
    user_id: int | None = None,
    status: str | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    _: User = Depends(current_admin_user),
):
    """Managed-Trades drill-down view. Filter optional."""
    q = db.query(ManagedTrade)
    if user_id is not None:
        q = q.filter(ManagedTrade.user_id == user_id)
    if status:
        q = q.filter(ManagedTrade.status == status)
    rows = q.order_by(ManagedTrade.id.desc()).limit(max(1, min(limit, 500))).all()
    out = []
    for t in rows:
        try:
            tps = (
                [{"price": p, "percent": pct} for p, pct in __import__("json").loads(t.take_profits or "[]")]
                if t.take_profits else []
            )
        except Exception:
            tps = []
        out.append({
            "id": t.id,
            "user_id": t.user_id,
            "coin": t.coin,
            "direction": t.direction,
            "entry": t.entry,
            "stop_loss": t.stop_loss,
            "take_profits": tps,
            "status": t.status,
            "signal_id": t.signal_id,
            "created_at": t.created_at.isoformat(timespec="seconds") if t.created_at else None,
            "updated_at": t.updated_at.isoformat(timespec="seconds") if t.updated_at else None,
        })
    return out


@router.get("/cost")
def admin_cost(_: User = Depends(current_admin_user)):
    """Gemini-API-Cost-Indikatoren aus den Signal-bot Cycle-Logs.
    Liest die letzten N CYCLE-COMPLETE-Zeilen aus der signal-bot bot_logs Tabelle
    und parst die strukturierten Werte (processed/hold/signals/...).
    """
    import re
    import sqlite3
    sb_db = os.getenv(
        "SIGNALBOT_DB_PATH",
        "/var/lib/docker/volumes/tradinghub-signalbeta_signalbeta-data/_data/charthub.db",
    )
    out = {
        "signalbot_db_found": os.path.exists(sb_db),
        "cycles": [],
        "estimates": {},
        "error": None,
    }
    if not os.path.exists(sb_db):
        out["error"] = f"signal-bot DB not at {sb_db}"
        return out
    try:
        con = sqlite3.connect(f"file:{sb_db}?mode=ro", uri=True, timeout=2)
        cur = con.cursor()
        cur.execute(
            "SELECT timestamp, message FROM bot_logs "
            "WHERE message LIKE 'CYCLE COMPLETE:%' "
            "ORDER BY id DESC LIMIT 50"
        )
        rows = cur.fetchall()
        con.close()
    except Exception as e:
        out["error"] = str(e)[:200]
        return out

    rx = re.compile(
        r"processed=(\d+)\s+hold=(\d+)\s+signals=(\d+)\s+\(new=(\d+)\s+upd=(\d+)\s+cancel=(\d+)\)"
        r"\s+ai_skip=(\d+)\s+ai_timeout=(\d+)\s+scrape_fail=(\d+)\s+scrape_timeout=(\d+)"
    )
    for ts, msg in rows:
        m = rx.search(msg or "")
        if not m:
            continue
        p, h, s, nw, up, ca, sk, to, sf, st = map(int, m.groups())
        out["cycles"].append({
            "ts": ts,
            "processed": p, "hold": h, "signals_total": s,
            "new_trade": nw, "update_trade": up, "cancel_trade": ca,
            "ai_skip": sk, "ai_timeout": to,
            "scrape_fail": sf, "scrape_timeout": st,
            "pro_calls_estimate": s + sk,  # rough — escalated to Pro then succeeded or failed
        })

    # Aggregierte Schätzung über die letzten N Cycles
    if out["cycles"]:
        n = len(out["cycles"])
        total_flash = sum(c["processed"] for c in out["cycles"])  # je Cycle = 1 Flash/Coin
        total_pro = sum(c["pro_calls_estimate"] for c in out["cycles"])
        # Kosten-Schätzung mit thinking_budget=0 (Flash) und =1024 (Pro), siehe vision.py Header
        cost_flash = total_flash * 0.001        # ~$0.001 / call (no thinking)
        cost_pro = total_pro * 0.02             # ~$0.02 / call (thinking capped at 1024)
        out["estimates"] = {
            "cycles_sampled": n,
            "flash_calls_total": total_flash,
            "pro_calls_total_est": total_pro,
            "usd_total_est": round(cost_flash + cost_pro, 2),
            "usd_per_cycle_avg": round((cost_flash + cost_pro) / max(1, n), 4),
            "usd_per_week_extrapolated": round((cost_flash + cost_pro) / max(1, n) * 168, 2),  # 1 cycle/h × 168h
            "note": "Estimate based on thinking_budget=0 Flash + 1024 Pro. Actual: check Google Cloud Billing.",
        }
    return out


@router.get("/per-coin")
def admin_per_coin(_: User = Depends(current_admin_user), db: Session = Depends(get_db)):
    """Per-Coin Win/Loss-Statistik je aktivem User aus HL-Fills (cached via engine._per_coin_stats).
    Auch zeigt, welche Coins der per-coin-Filter aktuell blockt."""
    from app import config
    from app.engine import _per_coin_stats
    users = db.query(User).filter(User.bot_active.is_(True)).all()
    out = []
    for u in users:
        if not u.hl_account_address:
            continue
        # Standard-Coins die wir handeln (aus dem signal-bot)
        common = ["BTC", "ETH", "SOL", "BNB", "AVAX", "DOGE", "ADA", "NEAR", "ATOM",
                  "APT", "ARB", "OP", "TIA", "SUI", "INJ", "AAVE"]
        u_out = {
            "user_id": u.id,
            "username": u.discord_username or u.email,
            "address_short": (u.hl_account_address[:6] + ".." + u.hl_account_address[-4:]),
            "coins": [],
        }
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
                u_out["coins"].append({
                    "coin": coin,
                    "trades": stats["trades"],
                    "wins": stats["wins"],
                    "win_rate": round(stats["win_rate"], 3),
                    "blocked": blocked,
                })
        out.append(u_out)
    return {
        "min_trades_required": config.PERCOIN_MIN_TRADES,
        "min_winrate": config.PERCOIN_MIN_WINRATE,
        "users": out,
    }


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
