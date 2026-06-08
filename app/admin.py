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


# 2026-06-08 Mainnet-Hardening A3: Panic-Halt-Endpoints.
# Notfall-Switch um SOFORT alle User zu pausieren + EMERGENCY_HALT setzen
# damit handle_signal jeden weiteren Signal ignoriert. Single-Click in
# unter 5 Sekunden.
@router.get("/halt-status")
def admin_halt_status(_: User = Depends(current_admin_user)):
    """Aktueller Status: ist Emergency-Halt aktiv? Wie viele user bot_active?"""
    from app.engine import _emergency_halt_active
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        active = db.query(User).filter(User.bot_active.is_(True)).count()
        total = db.query(User).count()
    finally:
        db.close()
    halt = _emergency_halt_active()
    halt_reason = None
    if halt:
        try:
            with open(config.EMERGENCY_HALT_FLAG_PATH) as f:
                halt_reason = f.read()[:500]
        except Exception:
            pass
    return {
        "emergency_halt_active": halt,
        "halt_reason": halt_reason,
        "users_bot_active": active,
        "users_total": total,
    }


@router.post("/halt")
def admin_halt(admin: User = Depends(current_admin_user), db: Session = Depends(get_db)):
    """🚨 PANIC-HALT: pause ALL bot_active users + set EMERGENCY_HALT-Flag.

    Idempotent. Sofort-Stop für die ganze Plattform — handle_signal ignoriert
    alle weiteren Signale bis /halt/clear aufgerufen wird ODER der Flag-File
    manuell gelöscht.
    """
    from app.engine import _set_emergency_halt
    paused = db.query(User).filter(User.bot_active.is_(True)).update({"bot_active": False})
    db.add(Activity(
        user_id=admin.id, kind="error",
        text=f"🚨 PANIC-HALT triggered by admin {admin.email or admin.discord_username}: "
             f"{paused} user(s) paused + EMERGENCY_HALT-Flag gesetzt."
    ))
    db.commit()
    _set_emergency_halt(reason=f"Manuell durch admin {admin.email or admin.discord_username}")
    return {"ok": True, "users_paused": paused, "emergency_halt": True}


@router.post("/halt/clear")
def admin_halt_clear(admin: User = Depends(current_admin_user), db: Session = Depends(get_db)):
    """Hebe EMERGENCY_HALT auf. User selbst müssen sich danach wieder
    aktivieren (bot_active) — wir setzen es NICHT automatisch zurück."""
    from app.engine import _clear_emergency_halt
    _clear_emergency_halt()
    db.add(Activity(
        user_id=admin.id, kind="order",
        text=f"EMERGENCY_HALT cleared by admin {admin.email or admin.discord_username}. "
             f"User müssen sich selbst wieder aktivieren."
    ))
    db.commit()
    return {"ok": True, "emergency_halt": False}


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
        # 2026-06-04 (#6): t.entry/stop_loss sind Decimal — als String serialisieren
        # damit FastAPI's JSON-Encoder nicht in float-Drift fällt; Frontend
        # behandelt sie eh als Display-Text.
        out.append({
            "id": t.id,
            "user_id": t.user_id,
            "coin": t.coin,
            "direction": t.direction,
            "entry": str(t.entry) if t.entry is not None else None,
            "stop_loss": str(t.stop_loss) if t.stop_loss is not None else None,
            "take_profits": tps,
            "status": t.status,
            "signal_id": t.signal_id,
            "created_at": t.created_at.isoformat(timespec="seconds") if t.created_at else None,
            "updated_at": t.updated_at.isoformat(timespec="seconds") if t.updated_at else None,
        })
    return out


@router.get("/cost")
def admin_cost(_: User = Depends(current_admin_user)):
    """Gemini-API-Cost — zwei Datenquellen:
      A) Heuristic-Estimate aus bot_logs CYCLE-COMPLETE-Zeilen (immer da)
      B) Real-Cost aus bot.log TOKEN_USAGE-Zeilen (seit 2026-06-03 deployed)
    """
    import re
    import sqlite3
    sb_db = os.getenv(
        "SIGNALBOT_DB_PATH",
        "/var/lib/docker/volumes/tradinghub-signalbeta_signalbeta-data/_data/charthub.db",
    )
    sb_log = os.getenv(
        "SIGNALBOT_LOG_PATH",
        "/var/lib/docker/volumes/tradinghub-signalbeta_signalbeta-data/_data/logs/bot.log",
    )
    out = {
        "signalbot_db_found": os.path.exists(sb_db),
        "signalbot_log_found": os.path.exists(sb_log),
        "cycles": [],
        "estimates": {},
        "real": {},
        "error": None,
    }
    # ── A) Heuristic-Estimate aus CYCLE-COMPLETE ───────────────────────────
    if os.path.exists(sb_db):
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
            rows = []
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
                "ts": ts, "processed": p, "hold": h, "signals_total": s,
                "new_trade": nw, "update_trade": up, "cancel_trade": ca,
                "ai_skip": sk, "ai_timeout": to,
                "scrape_fail": sf, "scrape_timeout": st,
                "pro_calls_estimate": s + sk,
            })
        if out["cycles"]:
            n = len(out["cycles"])
            total_flash = sum(c["processed"] for c in out["cycles"])
            total_pro = sum(c["pro_calls_estimate"] for c in out["cycles"])
            cost_flash = total_flash * 0.001
            cost_pro = total_pro * 0.02
            out["estimates"] = {
                "cycles_sampled": n,
                "flash_calls_total": total_flash,
                "pro_calls_total_est": total_pro,
                "usd_total_est": round(cost_flash + cost_pro, 2),
                "usd_per_cycle_avg": round((cost_flash + cost_pro) / max(1, n), 4),
                "usd_per_week_extrapolated": round((cost_flash + cost_pro) / max(1, n) * 168, 2),
                "note": "Heuristic estimate; see 'real' section for actual TOKEN_USAGE data.",
            }
    # ── B) Real-Cost aus TOKEN_USAGE-Zeilen ──────────────────────────────────
    if os.path.exists(sb_log):
        try:
            # Phase 6+ (2026-06-03): parse TOKEN_USAGE lines added in vision.py.
            # Format: 'TOKEN_USAGE model=gemini-2.5-flash prompt=2242 output=41 thoughts=0 cached=0'
            tu_rx = re.compile(
                r"TOKEN_USAGE\s+model=(\S+)\s+prompt=(\d+)\s+output=(\d+)\s+thoughts=(\d+)\s+cached=(\d+)"
            )
            # Gemini Pricing (2025): per million tokens
            PRICING = {
                "gemini-2.5-flash": {"in": 0.075,  "out": 0.30,  "thought": 0.30, "cached": 0.01875},
                "gemini-2.5-pro":   {"in": 1.25,   "out": 10.00, "thought": 10.00, "cached": 0.3125},
            }
            per_model = {}
            # Read up to last 10 MB of the log (avoid OOM on huge logs)
            import io
            sz = os.path.getsize(sb_log)
            with open(sb_log, "rb") as f:
                if sz > 10 * 1024 * 1024:
                    f.seek(sz - 10 * 1024 * 1024)
                tail = f.read().decode("utf-8", errors="ignore")
            for line in tail.splitlines():
                m = tu_rx.search(line)
                if not m:
                    continue
                model, prompt, output, thoughts, cached = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
                if model not in per_model:
                    per_model[model] = {"calls": 0, "prompt": 0, "output": 0, "thoughts": 0, "cached": 0, "usd": 0.0}
                rec = per_model[model]
                rec["calls"] += 1
                rec["prompt"] += prompt
                rec["output"] += output
                rec["thoughts"] += thoughts
                rec["cached"] += cached
                p = PRICING.get(model, {"in": 0, "out": 0, "thought": 0, "cached": 0})
                # Uncached prompt = prompt - cached (we report total prompt but charge only the non-cached delta)
                uncached_prompt = max(0, prompt - cached)
                rec["usd"] += (
                    uncached_prompt * p["in"] / 1_000_000
                    + cached * p["cached"] / 1_000_000
                    + output * p["out"] / 1_000_000
                    + thoughts * p["thought"] / 1_000_000
                )
            total_calls = sum(r["calls"] for r in per_model.values())
            total_usd = sum(r["usd"] for r in per_model.values())
            out["real"] = {
                "per_model": {k: {**v, "usd": round(v["usd"], 4)} for k, v in per_model.items()},
                "total_calls": total_calls,
                "total_usd_so_far": round(total_usd, 4),
                "note": "Real token counts from vision.py TOKEN_USAGE logs (last 10MB of bot.log).",
            }
        except Exception as e:
            out["real"] = {"error": str(e)[:200]}
    return out


@router.get("/cost-history")
def admin_cost_history(
    days: int = 7,
    _: User = Depends(current_admin_user),
    db: Session = Depends(get_db),
):
    """2026-06-04 Restposten #5: Historische Cost-Daten aus DB.

    Vorher: nur was im aktuellen bot.log lag (max ~24h-48h vor Rotation).
    Jetzt: TokenUsage-Tabelle aggregated per day + model, beliebig lang.
    """
    from app.models import TokenUsage
    days = max(1, min(90, days))
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=days)
    rows = (
        db.query(TokenUsage)
          .filter(TokenUsage.ts >= cutoff)
          .all()
    )
    # Aggregate per (date, model)
    per_day_model = {}
    total_usd = 0.0
    for r in rows:
        key = (r.ts.date().isoformat(), r.model)
        a = per_day_model.setdefault(key, {"calls": 0, "prompt": 0, "output": 0, "thoughts": 0, "cached": 0, "usd": 0.0})
        a["calls"] += 1
        a["prompt"] += r.prompt
        a["output"] += r.output
        a["thoughts"] += r.thoughts
        a["cached"] += r.cached
        a["usd"] += r.usd or 0.0
        total_usd += r.usd or 0.0
    series = [
        {"date": k[0], "model": k[1], **{kk: (round(vv, 4) if isinstance(vv, float) else vv) for kk, vv in v.items()}}
        for k, v in sorted(per_day_model.items())
    ]
    return {
        "days": days,
        "total_rows": len(rows),
        "total_usd": round(total_usd, 4),
        "per_day_model": series,
    }


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


@router.post("/test-signal")
async def admin_test_signal(
    body: dict,
    _: User = Depends(current_admin_user),
):
    """2026-06-05: Manuelles Test-Signal direkt durch die Engine schicken,
    ohne auf den signal-bot's 60min-Cycle oder Discord-Channel zu warten.

    Body (JSON, alle Felder die der Parser erwartet — siehe parser.py):
      {
        "type": "NEW_TRADE",                  # action; auch UPDATE_TRADE / CANCEL_TRADE
        "asset": "SOL/USDT",                  # ticker
        "direction": "LONG",                  # LONG | SHORT
        "entry": 140,
        "stop_loss": 135,
        "take_profits": [{"price":150,"percent":50}, {"price":160,"percent":50}],
        "signal_id": "manual-test-001",       # optional, sonst auto-gen
        "confidence": 0.85                    # optional, default = MIN_CONFIDENCE
      }

    Baut einen synthetischen Discord-Embed im Format das parser.parse_signal
    erwartet (title, fields) und ruft handle_signal direkt — alle aktiven
    User mit bot_active=True bekommen das Signal, exakt wie wenn es von
    Bot 1 in #signals gekommen wäre.
    """
    from app.engine import handle_signal
    action = str(body.get("type") or "NEW_TRADE").upper()
    asset = str(body.get("asset") or "")
    if "/" not in asset:
        raise HTTPException(400, "asset muss Format 'COIN/USDT' haben, z.B. 'SOL/USDT'")
    direction = str(body.get("direction") or "LONG").upper()
    entry = body.get("entry")
    stop_loss = body.get("stop_loss")
    tps = body.get("take_profits") or []
    signal_id = str(body.get("signal_id") or f"manual-{int(datetime.datetime.utcnow().timestamp())}")
    confidence = body.get("confidence", 0.90)

    # Format take_profits zu "50% @ 150, 50% @ 160" string (parser-erwartet)
    tps_str = ", ".join(
        f"{t.get('percent', 100)}% @ {t.get('price')}"
        for t in tps if t.get("price")
    )
    # Baue synthetisches Embed im exakten Format das parser.parse_signal liest
    # 2026-06-05 fix: parser erwartet field-names MIT SPACE ('stop loss', 'take profits'),
    # nicht underscore. Vorher: parse_signal → None → silent skip in handle_signal.
    embed = {
        "title": f"{asset.split('/')[0]} — {action}",
        "description": f"manual test signal `{signal_id}`",
        "fields": [
            {"name": "ticker", "value": asset},
            {"name": "action", "value": action},
            {"name": "direction", "value": direction},
            {"name": "entry", "value": str(entry) if entry is not None else ""},
            {"name": "stop loss", "value": str(stop_loss) if stop_loss is not None else ""},
            {"name": "take profits", "value": tps_str},
            {"name": "confidence", "value": str(confidence)},
        ],
    }
    # handle_signal ist async + spawned interne tasks via _spawn
    # (asyncio.create_task) — laufen im FastAPI-Event-Loop weiter, kein
    # await nötig auf die spawned trade-tasks. Wir kehren sofort zurück.
    await handle_signal(embed)
    return {
        "ok": True,
        "dispatched": {
            "action": action, "asset": asset, "direction": direction,
            "entry": entry, "stop_loss": stop_loss, "tps": tps,
            "signal_id": signal_id, "confidence": confidence,
        },
        "note": "Check /api/admin/health or journalctl -u goathub for results. "
                "Engine spawned background tasks per active user — trades may take 5-30s.",
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
