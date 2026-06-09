"""DB-Modelle."""
import datetime
import decimal

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.types import TypeDecorator

from app.db import Base


# 2026-06-04 Restposten #6: Decimal-präzision für gespeicherte Preise.
# SQLite mapped Numeric per default auf REAL (float64) → Drift bei wiederholten
# Reads. Mit TypeDecorator speichern wir als TEXT (verlustfrei) und liefern
# Decimal im Python-Layer. Auf Postgres (Mainnet-Migration) mapped Numeric
# direkt auf NUMERIC — kein Code-Change nötig.
class MoneyDecimal(TypeDecorator):
    """Lossless Decimal-Storage: TEXT in SQLite, NUMERIC in Postgres.

    Read: gibt Decimal zurück. Vergleiche `Decimal(x) < float_y` funktionieren
    in Python korrekt (Decimal-Klasse hat __lt__ mit float). Math zwischen
    Decimal und float wirft TypeError; daher in engine.py: explizite
    float()-Konversion am Math-Boundary.
    """
    impl = String
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String())   # TEXT auf SQLite
        return dialect.type_descriptor(Numeric(precision=24, scale=12))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        # Akzeptiert float, int, Decimal, str — alle → str (verlustfrei für die ersten beiden)
        return str(decimal.Decimal(str(value)))

    def process_result_value(self, value, dialect):
        if value is None or value == "":
            return None
        try:
            return decimal.Decimal(str(value))
        except (decimal.InvalidOperation, ValueError):
            return None


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

    # 2026-06-08 Mainnet-Hardening C1: Per-User Max-Drawdown Lifetime-Cap.
    # Wenn account_value < peak * (1 - max_drawdown_pct) → auto-pause + alert.
    # max_drawdown_pct=0 deaktiviert das Feature.
    # peak_account_value wird bei jedem _open_new auf max(peak, current) gehoben.
    max_drawdown_pct = Column(Float, default=0.30, nullable=False)   # 30% default = pretty lenient
    peak_account_value = Column(Float, default=0.0, nullable=False)


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


class TokenUsage(Base):
    """Persisted Gemini API token-usage events.

    2026-06-04 Restposten #5: bot.log wird rotiert; ohne Persistierung sind
    historische Daten weg nach paar Tagen. Wir scrapen TOKEN_USAGE-Zeilen alle
    paar Minuten in den Hintergrund-Loop, INSERT-OR-IGNORE auf (ts, model,
    prompt, output) damit Re-Scan idempotent ist. Admin-Endpoint zeigt
    aggregates aus DB (lang) + live-log (frisch).
    """
    __tablename__ = "token_usage"
    id = Column(Integer, primary_key=True)
    ts = Column(DateTime, index=True, default=datetime.datetime.utcnow)
    model = Column(String, index=True)
    prompt = Column(Integer, default=0)
    output = Column(Integer, default=0)
    thoughts = Column(Integer, default=0)
    cached = Column(Integer, default=0)
    usd = Column(Float, default=0.0)             # berechnet aus Pricing-Tabelle beim Insert
    source = Column(String, default="bot.log")   # bot.log | docker.logs | manual


class ManagedTrade(Base):
    """Ein laufend verwalteter Trade pro Nutzer+Coin (NEW -> UPDATE -> CLOSE)."""
    __tablename__ = "managed_trades"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    coin = Column(String, index=True, nullable=False)   # Basis-Coin, z.B. "ETH"
    direction = Column(String, default="")              # LONG | SHORT
    # 2026-06-04 (#6): MoneyDecimal statt Float — preisgenau und kein Drift.
    entry = Column(MoneyDecimal, nullable=True)
    stop_loss = Column(MoneyDecimal, nullable=True)
    take_profits = Column(Text, default="")             # JSON: [[px, percent], ...]
    status = Column(String, default="resting")          # resting | open | closed
    resting_oid = Column(String, nullable=True)
    signal_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class ProcessedSignal(Base):
    """C3 (2026-06-09): persistenter Dedup-Schlüssel pro (user, signal_id).

    `managed_trades.signal_id` ist last-writer-wins (wird pro Coin überschrieben)
    und taugt NICHT zum Dedup. Diese Tabelle hält jede tatsächlich AUSGEFÜHRTE
    (user, signal_id)-Kombi mit UNIQUE-Constraint. Vor einem NEW_TRADE prüft die
    Engine, ob die Kombi schon existiert → Replay (z.B. nach Restart, wenn der
    In-Memory-Throttle weg ist) wird übersprungen statt doppelt eröffnet.
    Nur bei ERFOLGREICHEM Entry geschrieben — geskippte Signale (Margin/Filter)
    bleiben retry-bar.
    """
    __tablename__ = "processed_signal"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True, nullable=False)
    signal_id = Column(String, index=True, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    __table_args__ = (UniqueConstraint("user_id", "signal_id", name="uq_processed_user_signal"),)
