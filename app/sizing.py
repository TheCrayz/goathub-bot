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
