"""DB-Modelle."""
import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String, Text

from app.db import Base


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    # Hyperliquid
    hl_account_address = Column(String, default="")    # MASTER-Adresse (öffentlich)
    hl_api_secret_enc = Column(Text, default="")       # AGENT-Key, verschlüsselt (Fernet)

    # Discord OAuth
    discord_id = Column(String, unique=True, nullable=True, default=None)
    discord_username = Column(String, nullable=True, default=None)
    discord_avatar = Column(String, nullable=True, default=None)

    # Settings
    risk_pct = Column(Float, default=0.01)
    leverage = Column(Float, default=3)
    max_open_positions = Column(Integer, default=10)
    capital_cap_usdc = Column(Float, default=0)        # 0 = ganzer Account; sonst Budget-Cap
    bot_active = Column(Boolean, default=False)        # Nutzer schaltet selbst scharf
    builder_approved = Column(Boolean, default=False)  # Referral-Gebühr freigegeben?


class Activity(Base):
    __tablename__ = "activity"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True)
    ts = Column(DateTime, default=datetime.datetime.utcnow)
    kind = Column(String)            # signal | order | skip | error
    text = Column(Text)
