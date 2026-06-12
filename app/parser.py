"""Parst das Signal-Embed (von Bot 1 in #signals) in ein Signal.
Dependency-frei (kein discord.py nötig) -> lokal testbar.
"""
import re
from dataclasses import dataclass


@dataclass
class TakeProfit:
    percent: float
    price: float


@dataclass
class Signal:
    signal_id: str
    ticker: str
    action: str
    direction: str
    entry: float
    stop_loss: float
    take_profits: list
    confidence: float = None


def _num(v):
    if v is None:
        return None
    s = str(v).strip()
    if s in ("", "—", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _fields(embed):
    out = {}
    for f in embed.get("fields") or []:
        out[(f.get("name") or "").strip().lower()] = (f.get("value") or "").strip()
    return out


def _tps(raw):
    tps = []
    for chunk in (raw or "").split(","):
        m = re.match(r"([\d.]+)\s*%\s*@\s*([\d.]+)", chunk.strip())
        if m:
            tps.append(TakeProfit(float(m.group(1)), float(m.group(2))))
    return tps


def _ticker_from_title(title, direction):
    if not title:
        return None
    left = re.split(r"\s+[—–-]\s+", title)[0].strip()
    parts = left.split()
    if direction and parts and parts[0].upper() == direction.upper():
        parts = parts[1:]
    return " ".join(parts).strip() or None


CANCEL_ACTIONS = ("CANCEL_TRADE", "CANCEL", "EXIT", "CLOSE", "CLOSE_TRADE")


def parse_signal(embed: dict):
    fm = _fields(embed)
    title = embed.get("title") or ""
    action = (fm.get("action") or "").upper()
    direction = (fm.get("direction") or "").upper()
    if not action:
        # Fallback: Action aus dem Titel "TICKER — ACTION"
        parts = re.split(r"\s+[—–-]\s+", title)
        if len(parts) > 1:
            action = parts[-1].strip().upper()
    m = re.search(r"`([^`]+)`", embed.get("description") or "")
    signal_id = m.group(1) if m else ""
    ticker = fm.get("ticker") or _ticker_from_title(title, direction)
    entry = _num(fm.get("entry"))
    stop_loss = _num(fm.get("stop loss"))
    if not action or not ticker:
        return None
    # CANCEL/CLOSE braucht keine Level (Position wird einfach geschlossen)
    if action in CANCEL_ACTIONS:
        return Signal(signal_id=signal_id, ticker=ticker, action=action, direction=direction,
                      entry=entry, stop_loss=stop_loss, take_profits=[], confidence=_num(fm.get("confidence")))
    # 2026-06-12 (Review #18): Entry+SL nur für NEW_TRADE Pflicht. Ein
    # UPDATE_TRADE betrifft eine BEREITS offene Position — Entry ist dafür
    # irrelevant, und ein reines SL-Nachzieh-/TP-Update darf nicht verworfen
    # werden (vorher gingen Trail-Stop-Updates ohne Entry-Feld lautlos
    # verloren; engine._adjust hat für stop_loss=None längst einen sicheren
    # Pfad "Update ohne neuen SL").
    if action != "UPDATE_TRADE" and (entry is None or stop_loss is None):
        return None
    return Signal(signal_id=signal_id, ticker=ticker, action=action, direction=direction,
                  entry=entry, stop_loss=stop_loss, take_profits=_tps(fm.get("take profits")),
                  confidence=_num(fm.get("confidence")))
