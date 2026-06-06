"""Risiko-basiertes Sizing MIT Kapital-Cap.

Effektives Kapital = min(echtes Guthaben, Kapital-Cap).  capital_cap=0 -> ganzer Account.
qty = risk% * effektives_Kapital / |entry - SL|.  Hard-Cap gegen zu engen SL.
Dependency-frei -> lokal testbar.
"""
from dataclasses import dataclass


@dataclass
class SizePlan:
    qty: float
    notional: float
    margin: float
    effective_balance: float
    risk_amount: float
    sl_distance: float
    capped: bool = False


def size_trade(*, account_value: float, capital_cap: float, risk_pct: float,
               entry: float, stop_loss: float, leverage: float = 3.0) -> "SizePlan":
    eff = account_value if not capital_cap or capital_cap <= 0 else min(account_value, capital_cap)
    sl = abs(entry - stop_loss)
    if entry <= 0 or sl <= 0:
        raise ValueError("entry > 0 und stop_loss != entry erforderlich")
    risk_amount = eff * risk_pct
    qty = risk_amount / sl
    notional = qty * entry
    max_notional = eff * leverage
    capped = False
    if notional > max_notional:
        capped = True
        notional = max_notional
        qty = notional / entry
    return SizePlan(qty=qty, notional=notional, margin=notional / leverage,
                    effective_balance=eff, risk_amount=risk_amount, sl_distance=sl,
                    capped=capped)


def auto_leverage(*, entry: float, stop_loss: float, confidence=None,
                  max_cap: int = 50, liq_safety: float = 2.0,
                  conf_floor: float = 0.5) -> tuple:
    """Hebel dynamisch wählen aus SL-Distanz + Signal-Confidence.

    Rationale: bei cross-margin liegt die Liquidation ca. bei 1/leverage vom
    Entry weg. Damit die Liquidation NICHT zwischen Entry und SL liegt
    (sondern weit dahinter), muss gelten:

        leverage * sl_distance ≤ 1 / liq_safety

    → safe_leverage = 1 / (sl_distance × liq_safety)

    Beispiel SL bei 2% von Entry, liq_safety=2:
        safe = 1 / (0.02 × 2) = 25x   → SL wäre auf halbem Weg zur Liquidation.

    Confidence aus dem Signal (z.B. 0.85 für Gemini-PRO-Signale) skaliert
    das nach unten — low-conf Signals werden konservativer gehandelt.

    Args:
        entry: Entry-Preis
        stop_loss: SL-Preis
        confidence: Signal-Confidence 0..1, None → conf_floor
        max_cap: User-Maximalhebel (default 50x)
        liq_safety: Wie viel Puffer zwischen SL und Liquidation? 2.0 = SL auf halbem Weg.
        conf_floor: Minimum-Wirkfaktor wenn confidence niedrig/fehlt

    Returns:
        (leverage:int, reason:str) — leverage gerundet auf int, reason für Activity-Log.

    Wirft ValueError bei entry≤0 oder sl=entry (gleicher Check wie size_trade).
    """
    if entry <= 0:
        raise ValueError("entry > 0 erforderlich")
    sl_dist = abs(entry - stop_loss) / entry
    if sl_dist <= 0:
        raise ValueError("stop_loss != entry erforderlich")
    safe_lev = 1.0 / (sl_dist * liq_safety)
    conf = max(conf_floor, float(confidence)) if confidence is not None else conf_floor
    chosen = safe_lev * conf
    lev = max(1, min(int(max_cap), int(round(chosen))))
    sl_pct = sl_dist * 100
    reason = (f"auto-lev: SL={sl_pct:.2f}% × safety {liq_safety}× = safe {safe_lev:.1f}x, "
              f"conf {conf:.2f} → {lev}x (cap {int(max_cap)}x)")
    return lev, reason
