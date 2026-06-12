"""Pydantic-Schemas (E-Mail als str gehalten, um email-validator-Dep zu sparen)."""
from typing import Optional

from pydantic import BaseModel, Field


class Register(BaseModel):
    email: str
    password: str


class Login(BaseModel):
    email: str
    password: str


class SettingsIn(BaseModel):
    """Settings-Update (partial — nur mitgeschickte Felder werden geändert).

    2026-06-12 #12/#13: Bounds-Validierung statt Silent-Clamp. Vorher wurde
    z.B. risk_pct=1.0 (User meinte "1 %", API erwartet FRACTION: 0.01) still
    auf 0.05 = 5 %/Trade geclampt — 10x des Defaults, und das Frontend zeigte
    weiter "1" an. Bei echtem Geld ist Ablehnen (422) die einzig sichere
    Antwort. Einheiten-Vertrag mit dem Frontend: risk_pct bleibt FRACTION
    (0.005 = 0.5 %), Umrechnung Prozent↔Fraction passiert NUR im Frontend.
    Explizit mitgeschicktes null wird in main.update_settings abgewiesen
    (vorher wurde leeres Input-Feld zu 0 coerced → Capital-Cap weg).
    """
    risk_pct: Optional[float] = Field(
        None, gt=0, le=0.05,
        description="Risiko pro Trade als FRACTION (0.005 = 0.5%). Erlaubt: (0, 0.05].")
    leverage: Optional[float] = Field(
        None, ge=1, le=50,
        description="Max-Leverage-Cap des Users. Erlaubt: [1, 50] (HL Perps Max).")
    max_open_positions: Optional[int] = Field(
        None, ge=1, le=20,
        description="Max. gleichzeitig offene Positionen. Erlaubt: [1, 20].")
    capital_cap_usdc: Optional[float] = Field(
        None, ge=0, le=10_000_000,
        description="Budget-Cap in USDC; 0 = ganzer Account. Erlaubt: [0, 10000000].")
    bot_active: Optional[bool] = None
    max_drawdown_pct: Optional[float] = Field(
        None, ge=0, le=0.95,
        description="Lifetime-Drawdown-Cap als FRACTION; 0 = aus. Erlaubt: [0, 0.95].")  # 2026-06-08 C1


class WalletIn(BaseModel):
    hl_account_address: str
    hl_api_secret: str
