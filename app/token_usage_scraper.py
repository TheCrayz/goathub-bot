"""Periodisches Scraping der signal-bot TOKEN_USAGE-Zeilen in die goathub-DB.

2026-06-04 Restposten #5: bot.log wird täglich rotiert; ohne Persistenz sind
historische Daten weg sobald die Rotation greift. Wir scrapen alle 5 Minuten
das letzte Stück log und schreiben neue TOKEN_USAGE-Events in token_usage.
Idempotent via Composite-Key (ts, model, prompt, output, thoughts, cached) +
INSERT-OR-IGNORE-ähnliche Semantik (existiert-bereits-check).

Symmetrisch zu sync.py — Loop wird im lifespan() neben Discord-Listener + position_sync_loop gestartet.
"""
import asyncio
import datetime
import logging
import os
import re

from sqlalchemy import and_

from app import config
from app.db import SessionLocal
from app.models import TokenUsage

log = logging.getLogger("goathub.tokens")

SCRAPE_INTERVAL_S = int(os.getenv("TOKEN_USAGE_SCRAPE_INTERVAL_S", "300"))   # 5 min
# 2026-06-12 #54/M-20: Pfad + Schalter kommen jetzt zentral aus config
# (TOKEN_USAGE_LOG_PATH / TOKEN_SCRAPER_ENABLED). Vorher war hier der
# TradingHub-Docker-Volume-Pfad hardcoded und der Loop lief in JEDEM
# Deployment mit — siehe Kommentar in config.py.
SB_LOG_PATH = getattr(config, "TOKEN_USAGE_LOG_PATH", "") or config.SIGNALBOT_LOG_PATH

# M-20: Pfad unlesbar (z.B. Service läuft als goathub statt root, oder das
# Docker-Volume existiert auf diesem Host gar nicht) → genau EINE Warnung,
# danach still. Kein Error-Spam alle 5 Minuten.
_warned_unreadable = False

# Gleiche Pricing-Tabelle wie admin.py — duplicated bewusst um keine Cross-Imports.
PRICING = {
    "gemini-2.5-flash": {"in": 0.075, "out": 0.30, "thought": 0.30, "cached": 0.01875},
    "gemini-2.5-pro":   {"in": 1.25,  "out": 10.00, "thought": 10.00, "cached": 0.3125},
}

# Format z.B.: "2026-06-04 08:16:38,009 - INFO - TOKEN_USAGE model=gemini-2.5-flash prompt=2242 output=44 thoughts=0 cached=588"
_TS_RX = re.compile(r"^(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2})[,.]?(\d{3})?")
_TU_RX = re.compile(
    r"TOKEN_USAGE\s+model=(\S+)\s+prompt=(\d+)\s+output=(\d+)\s+thoughts=(\d+)\s+cached=(\d+)"
)


def _calc_usd(model: str, prompt: int, output: int, thoughts: int, cached: int) -> float:
    p = PRICING.get(model)
    if not p:
        return 0.0
    uncached_prompt = max(0, prompt - cached)
    return (
        uncached_prompt * p["in"] / 1_000_000
        + cached * p["cached"] / 1_000_000
        + output * p["out"] / 1_000_000
        + thoughts * p["thought"] / 1_000_000
    )


def _parse_ts(line: str):
    """Timestamp-Prefix der Log-Zeile parsen — None wenn keiner da/parsebar.

    2026-06-12 #27: vorher fiel das auf datetime.utcnow() zurück. Fatal für
    die Idempotenz: eine Zeile ohne (intakten) Timestamp bekam bei JEDEM
    5-Minuten-Scan eine NEUE ts → der Existenz-Check griff nie → Duplikat-Rows
    bei jedem Durchlauf. Jetzt: None zurückgeben, Caller skippt die Zeile.
    """
    m = _TS_RX.match(line)
    if not m:
        return None
    try:
        return datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _scrape_once() -> int:
    """Run a single scrape; returns count of newly-inserted rows."""
    global _warned_unreadable
    if not SB_LOG_PATH or not os.path.exists(SB_LOG_PATH) \
            or not os.access(SB_LOG_PATH, os.R_OK):
        if not _warned_unreadable:
            log.warning("token_usage: %s nicht vorhanden/lesbar — Scraper bleibt "
                        "passiv (einmalige Warnung).", SB_LOG_PATH or "<kein Pfad>")
            _warned_unreadable = True
        return 0
    _warned_unreadable = False   # Pfad wieder da (z.B. nach Log-Rotation)

    # Last 2 MB only — scrape läuft alle 5 min, das reicht.
    sz = os.path.getsize(SB_LOG_PATH)
    with open(SB_LOG_PATH, "rb") as f:
        if sz > 2 * 1024 * 1024:
            f.seek(sz - 2 * 1024 * 1024)
        tail = f.read().decode("utf-8", errors="ignore")
        if sz > 2 * 1024 * 1024:
            # 2026-06-12 #27: der Seek landet mitten in einer Zeile — das
            # erste Fragment kann einen abgeschnittenen Timestamp haben,
            # aber trotzdem das TOKEN_USAGE-Pattern matchen (search, nicht
            # match) → Phantom-Row. Erstes Partial-Stück immer verwerfen.
            tail = tail.split("\n", 1)[-1]

    inserted = 0
    db = SessionLocal()
    try:
        for line in tail.splitlines():
            m = _TU_RX.search(line)
            if not m:
                continue
            model = m.group(1)
            prompt = int(m.group(2)); output = int(m.group(3))
            thoughts = int(m.group(4)); cached = int(m.group(5))
            ts = _parse_ts(line)
            if ts is None:
                # 2026-06-12 #27: Zeile ohne parsebaren Timestamp → kein
                # stabiler Dedup-Key möglich. Skippen statt utcnow()-raten.
                continue
            # idempotenz-check: same ts+model+counts → schon drin?
            existing = (
                db.query(TokenUsage.id)
                  .filter(and_(
                      TokenUsage.ts == ts,
                      TokenUsage.model == model,
                      TokenUsage.prompt == prompt,
                      TokenUsage.output == output,
                      TokenUsage.thoughts == thoughts,
                      TokenUsage.cached == cached,
                  ))
                  .first()
            )
            if existing:
                continue
            usd = _calc_usd(model, prompt, output, thoughts, cached)
            db.add(TokenUsage(
                ts=ts, model=model, prompt=prompt, output=output,
                thoughts=thoughts, cached=cached, usd=usd, source="bot.log",
            ))
            inserted += 1
        if inserted > 0:
            db.commit()
    except Exception as e:
        db.rollback()
        log.exception("token_usage scrape failed: %s", e)
    finally:
        db.close()
    return inserted


async def token_usage_scrape_loop():
    # 2026-06-12 #54/M-20: Defensive Doppel-Absicherung — lifespan() startet
    # den Loop nur bei TOKEN_SCRAPER_ENABLED, aber falls jemand die Funktion
    # direkt aufruft: ohne Schalter/Pfad sofort raus statt ewig No-Op-pollen.
    if not getattr(config, "TOKEN_SCRAPER_ENABLED", False) or not SB_LOG_PATH:
        log.info("token_usage_scrape_loop übersprungen — TOKEN_SCRAPER_ENABLED "
                 "ist aus bzw. TOKEN_USAGE_LOG_PATH nicht gesetzt.")
        return
    log.info("token_usage_scrape_loop started (interval=%ds, path=%s)", SCRAPE_INTERVAL_S, SB_LOG_PATH)
    await asyncio.sleep(30)   # boot-delay
    while True:
        try:
            n = await asyncio.to_thread(_scrape_once)
            if n > 0:
                log.info("token_usage: %d new rows persisted", n)
        except Exception as e:
            log.exception("token_usage iteration failed: %s", e)
        await asyncio.sleep(SCRAPE_INTERVAL_S)
