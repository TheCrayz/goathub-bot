"""Parst das Signal-Embed (von Bot 1 in #signals) in ein Signal.
Dependency-frei (kein discord.py nötig) -> lokal testbar.
"""
import math
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
    # 2026-06-13 M-18: TPs, die der Parser als offensichtlich falsch-seitig
    # (vs entry+direction) ODER absurd verworfen hat. Bleibt leer im Normalfall.
    # Die Engine kann daraus eine klare Skip-Activity bauen (statt dass die
    # falschen TPs erst in place_protection still gefiltert werden). KEIN
    # Pflichtfeld der Konstruktion → default leere Liste via __post_init__.
    dropped_tps: list = None

    def __post_init__(self):
        if self.dropped_tps is None:
            self.dropped_tps = []


def _num(v):
    if v is None:
        return None
    s = str(v).strip()
    if s in ("", "—", "-"):
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    # 2026-06-13 H-15: "nan"/"inf"/"-inf" parsen als float, sind aber keine
    # echten Levels. NaN ist besonders gefährlich: `nan < MIN_CONFIDENCE` ist
    # False → ein Signal mit confidence=NaN passierte das Confidence-Gate und
    # tradete (verifiziert). Nicht-endliche Werte fallen hier sauber raus
    # (entry/SL/confidence/TP) → behandeln wie ein fehlendes Feld.
    if not math.isfinite(f):
        return None
    return f


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
            price = float(m.group(2))
            # 2026-06-13 M-18: nicht-endliche/<=0 Preise sind keine echten
            # Levels (regex lässt z.B. "0" durch) — gleich hier rauswerfen.
            if math.isfinite(price) and price > 0:
                tps.append(TakeProfit(float(m.group(1)), price))
    return tps


def _split_tps_by_side(tps, entry, direction):
    """M-18 (2026-06-13): TPs gegen entry+direction validieren. Ein Take-Profit
    liegt per Definition in Gewinn-Richtung:
        LONG  → TP-Preis MUSS über dem Entry liegen,
        SHORT → TP-Preis MUSS unter dem Entry liegen.
    Falsch-seitige TPs (z.B. SHORT-TP über Entry) sind in Wahrheit Stops und
    würden in place_protection als reduce-only-Order auf der falschen Seite
    landen → die Engine soll sie nicht weiterreichen. Wir splitten in
    (valid, dropped) statt hart zu droppen, damit die Engine eine klare
    Skip-Activity bauen kann.

    Ohne brauchbaren Entry/Direction (z.B. UPDATE ohne Entry-Feld) lässt sich
    die Seite nicht bestimmen → konservativ ALLE als valid durchreichen
    (nichts kaputt machen; place_protection hat sein eigenes Sicherheitsnetz).
    """
    if not tps:
        return [], []
    if entry is None or not math.isfinite(entry) or entry <= 0 or direction not in ("LONG", "SHORT"):
        return list(tps), []
    valid, dropped = [], []
    is_long = (direction == "LONG")
    for tp in tps:
        ok = (tp.price > entry) if is_long else (tp.price < entry)
        (valid if ok else dropped).append(tp)
    return valid, dropped


def _ticker_from_title(title, direction):
    if not title:
        return None
    left = re.split(r"\s+[—–-]\s+", title)[0].strip()
    parts = left.split()
    if direction and parts and parts[0].upper() == direction.upper():
        parts = parts[1:]
    return " ".join(parts).strip() or None


CANCEL_ACTIONS = ("CANCEL_TRADE", "CANCEL", "EXIT", "CLOSE", "CLOSE_TRADE")
ENTRY_ACTIONS = ("NEW_TRADE", "UPDATE_TRADE")
# 2026-06-13 M-19: nur DIESE Tokens darf der Title-Fallback als Action
# akzeptieren. Vorher reichte JEDES letzte "—"-Segment, das zufällig ein
# CANCEL-Token war (z.B. "BTC long zone — close to invalidation"), um einen
# CANCEL mit geratenem Ticker auszulösen. Whitelist = bekannte Aktionen +
# HOLD (no-op, wird in der Engine ignoriert).
KNOWN_ACTIONS = frozenset(CANCEL_ACTIONS + ENTRY_ACTIONS + ("HOLD",))


def parse_signal(embed: dict):
    fm = _fields(embed)
    title = embed.get("title") or ""
    direction = (fm.get("direction") or "").upper()
    # 2026-06-13 M-19: Explizites action-Feld IMMER bevorzugen. Der
    # Title-Fallback ist nur ein Notnagel und greift jetzt deutlich strenger
    # (siehe unten), weil ein falsch geratener CANCEL eine offene Position
    # schließen kann.
    action = (fm.get("action") or "").upper()
    action_from_title = False
    if not action:
        # Fallback NUR bei striktem "TICKER — ACTION"-Schema: genau ein
        # "—"-Trenner (2 Segmente) und das letzte Segment ist ein BEKANNTES
        # Action-Token. Ein freier Titel mit mehreren Bindestrichen oder
        # einem Nicht-Action-Schlusswort liefert KEINE Action mehr.
        parts = re.split(r"\s+[—–-]\s+", title)
        if len(parts) == 2:
            candidate = parts[-1].strip().upper()
            if candidate in KNOWN_ACTIONS:
                action = candidate
                action_from_title = True
    m = re.search(r"`([^`]+)`", embed.get("description") or "")
    signal_id = m.group(1) if m else ""
    # 2026-06-13 M-19: Ticker bevorzugt aus dem expliziten Feld. Der
    # Title-abgeleitete Ticker wird nur akzeptiert, wenn die Action AUS DEM FELD
    # kam ODER (beim Title-Fallback) ein expliziter Direction-Hinweis das
    # Schema bestätigt — ein geratener Ticker AUS demselben mehrdeutigen Titel,
    # aus dem auch die Action geraten wurde, ist zu unsicher für einen CANCEL.
    ticker = fm.get("ticker")
    if not ticker and not (action_from_title and not direction):
        ticker = _ticker_from_title(title, direction)
    entry = _num(fm.get("entry"))
    stop_loss = _num(fm.get("stop loss"))
    if not action or not ticker:
        return None
    confidence = _num(fm.get("confidence"))
    # CANCEL/CLOSE braucht keine Level (Position wird einfach geschlossen)
    if action in CANCEL_ACTIONS:
        return Signal(signal_id=signal_id, ticker=ticker, action=action, direction=direction,
                      entry=entry, stop_loss=stop_loss, take_profits=[], confidence=confidence)
    # 2026-06-12 (Review #18): Entry+SL nur für NEW_TRADE Pflicht. Ein
    # UPDATE_TRADE betrifft eine BEREITS offene Position — Entry ist dafür
    # irrelevant, und ein reines SL-Nachzieh-/TP-Update darf nicht verworfen
    # werden (vorher gingen Trail-Stop-Updates ohne Entry-Feld lautlos
    # verloren; engine._adjust hat für stop_loss=None längst einen sicheren
    # Pfad "Update ohne neuen SL").
    if action != "UPDATE_TRADE" and (entry is None or stop_loss is None):
        return None
    # 2026-06-13 M-18: TPs gegen entry+direction validieren. Falsch-seitige TPs
    # (würden in place_protection still gefiltert) werden hier rausgesplittet und
    # in dropped_tps abgelegt → die Engine kann eine klare Skip-Activity bauen.
    valid_tps, dropped_tps = _split_tps_by_side(_tps(fm.get("take profits")), entry, direction)
    return Signal(signal_id=signal_id, ticker=ticker, action=action, direction=direction,
                  entry=entry, stop_loss=stop_loss, take_profits=valid_tps,
                  confidence=confidence, dropped_tps=dropped_tps)
