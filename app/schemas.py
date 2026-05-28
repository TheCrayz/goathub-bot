"""Pydantic-Schemas (E-Mail als str gehalten, um email-validator-Dep zu sparen)."""
from typing import Optional

from pydantic import BaseModel


class Register(BaseModel):
    email: str
    password: str


class Login(BaseModel):
    email: str
    password: str


class SettingsIn(BaseModel):
    risk_pct: Optional[float] = None
    leverage: Optional[float] = None
    max_open_positions: Optional[int] = None
    capital_cap_usdc: Optional[float] = None
    bot_active: Optional[bool] = None


class WalletIn(BaseModel):
    hl_account_address: str
    hl_api_secret: str
