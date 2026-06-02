"""DB-Modelle."""
import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text

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

    # Session-Invalidierung (Phase 1, 2026-06-02): jeder JWT trägt `tv`, das
    # gegen diese Spalte verglichen wird. Wird beim Logout (und Passwort-
    # änderung) bumped → alle alten Tokens verlieren ihre Gültigkeit, auch
    # wenn jemand sie per XSS gestohlen hatte.
    token_version = Column(Integer, default=0, nullable=False)

    # Phase 3 (2026-06-02): Admin-Flag — Voraussetzung für /api/admin/*. Wer
    # is_admin=True ist, sieht das Admin-Panel im Dashboard und kann andere
    # Nutzer pausieren / Errors einsehen. Default False; per SQL gesetzt.
    is_admin = Column(Boolean, default=False, nullable=False)


class Activity(Base):
    __tablename__ = "activity"
    id = Column(Integer, primary_key=True)
    # Phase 4 (2026-06-02): FK + CASCADE — bei User-Delete fallen Activity-Zeilen
    # automatisch mit raus. Auf alten SQLite-DBs ohne FK greift die Migration
    # in db.py (recreate-table-with-fk).
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    ts = Column(DateTime, default=datetime.datetime.utcnow)
    kind = Column(String)            # signal | order | update | close | skip | error
    text = Column(Text)


class ManagedTrade(Base):
    """Ein laufend verwalteter Trade pro Nutzer+Coin (NEW -> UPDATE -> CLOSE)."""
    __tablename__ = "managed_trades"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    coin = Column(String, index=True, nullable=False)   # Basis-Coin, z.B. "ETH"
    direction = Column(String, default="")              # LONG | SHORT
    entry = Column(Float, nullable=True)
    stop_loss = Column(Float, nullable=True)
    take_profits = Column(Text, default="")             # JSON: [[px, percent], ...]
    status = Column(String, default="resting")          # resting | open | closed
    resting_oid = Column(String, nullable=True)
    signal_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)
