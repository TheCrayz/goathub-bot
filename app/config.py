"""Konfiguration aus Umgebung / .env."""
import os
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


def _g(n, d=None):
    v = os.environ.get(n)
    return v if v not in (None, "") else d


def _b(n, d="false"):
    return str(_g(n, d)).strip().lower() in ("1", "true", "yes", "on")


def _f(n, d):
    try:
        return float(_g(n, d))
    except (TypeError, ValueError):
        return float(d)


def _i(n, d):
    v = _g(n, d)
    try:
        return int(str(v).strip())           # exakt (große IDs!)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return int(d)


DATABASE_URL = _g("DATABASE_URL", "sqlite:///./goathub.db")
JWT_SECRET = _g("JWT_SECRET", "")
if not JWT_SECRET or JWT_SECRET == "dev-insecure-change-me":
    raise RuntimeError(
        "JWT_SECRET fehlt oder ist der unsichere Default. In der .env setzen, z.B.:\n"
        "  JWT_SECRET=$(openssl rand -hex 32)\n"
        "Ohne sicheres Secret sind alle Login-Tokens fälschbar (Account-Übernahme).")
JWT_EXPIRE_HOURS = _i("JWT_EXPIRE_HOURS", 24)   # 2026-06-08 B4: 24h statt 168h (war 7 Tage). Refresh-Endpoint extends.
ENCRYPTION_KEY = _g("ENCRYPTION_KEY")        # Fernet-Key; nötig zum Speichern der HL-Agent-Keys

# Hyperliquid
HL_TESTNET = _b("HL_TESTNET", "true")

# Referral / Builder-Code (Michaels Gebühren-Anteil)
BUILDER_ADDRESS = _g("BUILDER_ADDRESS")      # deine HL-Adresse (braucht >=100 USDC perps)
BUILDER_FEE = _g("BUILDER_FEE", "0.05%")     # f bis 0.1% (perps max)

# Signal-Quelle (#signals von Bot 1, privat)
DISCORD_BOT_TOKEN = _g("DISCORD_BOT_TOKEN")
SIGNALS_CHANNEL_ID = _i("SIGNALS_CHANNEL_ID", 0)
ENABLE_LISTENER = _b("ENABLE_LISTENER", "false")   # für localhost-Test ohne Discord = false

# 2026-06-12 DEMO_MODE: NUR fürs lokale Frontend-/iPhone-Testen. Wenn an, liefert
# /api/dashboard realistische MOCK-Daten statt echte HL-/DB-Abfragen — man kann
# das UI gefahrlos ansehen, komplett getrennt vom Live-Bot. Sicherheits-Riegel:
# greift NUR wenn ENABLE_LISTENER=false (Demo + Live-Trading schließen sich aus).
DEMO_MODE = _b("DEMO_MODE", "false") and not ENABLE_LISTENER

# Defaults für neue Nutzer
# 2026-06-08 Mainnet-Hardening B1: konservative Defaults für neue User.
# Können per env-var höher gesetzt werden (Power-User), neue User starten
# mit den Werten unten = safe. Vorher waren die testnet-aggressiv.
DEFAULT_RISK_PCT = _f("DEFAULT_RISK_PCT", 0.005)   # 0.5% statt 1% — half so aggressive
DEFAULT_LEVERAGE = _f("DEFAULT_LEVERAGE", 20)     # Max-Cap 20 für neue User (war 50). Auto-Lev passt sich an SL+conf an.
DEFAULT_MAX_OPEN = _i("DEFAULT_MAX_OPEN", 5)      # max 5 concurrent positions statt 10
MIN_NOTIONAL_USDC = _f("MIN_NOTIONAL_USDC", 10)
MIN_CONFIDENCE = _f("MIN_CONFIDENCE", 0.75)
ENTRY_FILL_TIMEOUT_S = _i("ENTRY_FILL_TIMEOUT_S", 300)   # 5 min (Phase 2 von 15→5 Min)
ENTRY_POLL_S = _i("ENTRY_POLL_S", 4)                     # alle 4s pollen (war 6)

# Phase 2 #27 (2026-06-02): per-Coin Auto-Filter.
# Wenn ein Coin nach >=PERCOIN_MIN_TRADES echten Trade-Events (Partial-Fills
# geclustert) auf dem User-Konto unter PERCOIN_MIN_WINRATE liegt, werden
# NEUE Trades für dieses Coin geskippt. UPDATE/CANCEL bleiben unberührt.
# Default 10 Trades / 30 % — beides env-tunbar. Wer den Filter komplett
# aus will setzt PERCOIN_MIN_TRADES auf eine sehr große Zahl (z. B. 99999).
PERCOIN_MIN_TRADES = _i("PERCOIN_MIN_TRADES", 10)
PERCOIN_MIN_WINRATE = _f("PERCOIN_MIN_WINRATE", 0.30)
PERCOIN_CACHE_TTL_S = _i("PERCOIN_CACHE_TTL_S", 600)     # HL-fills nur alle 10 Min neu ziehen

# Phase 6+ (2026-06-03): SL/TP-Slippage-Cap.
# Bisher passte place_protection px=trigger_px bei isMarket=true → HL hat den
# Default genutzt und SL in dünnen Märkten mit -7.94 % Slippage ausgeführt
# (SOL-Disaster 2026-06-03 03:38 UTC: -30 USDC bei 3.87 SOL). Jetzt setzen wir
# explizit den Worst-Case-Preis 2 % schlechter als der Trigger. Trade-off: bei
# Gaps > 2 % wird die Order nicht gefüllt → seltene naked-position-Restzeit,
# bis Position-Sync sie aufpickt oder manueller Eingriff. Für Mainnet kann
# das tighter sein, für Testnet (dünne Bücher) eher 3 %.
SL_SLIPPAGE_CAP = _f("SL_SLIPPAGE_CAP", 0.02)            # 2 %, env: SL_SLIPPAGE_CAP=0.03 für mehr Toleranz

# 2026-06-08 Mainnet-Hardening A1/A2/A3: Cost-Cap + Alert + Panic-Halt
# Wenn signal-bot amok läuft (z.B. 200 NEW_TRADEs/h), löst jeder Trade
# echte Fees aus. Auf Mainnet → echtes Geld. MAX_SIGNALS_PER_HOUR cappt
# das pro Stunde, MIN_TRADE_INTERVAL_S verhindert Trade-Storms pro
# (user, coin).
MAX_SIGNALS_PER_HOUR = _i("MAX_SIGNALS_PER_HOUR", 30)   # global
MIN_TRADE_INTERVAL_S = _i("MIN_TRADE_INTERVAL_S", 60)   # per (user, coin)

# 2026-06-09 C1: Aggregat-Margin-Cap. NEUE Entries werden geskippt, sobald die
# Gesamt-Margin-Auslastung (totalMarginUsed/accountValue) diesen Anteil erreicht.
# Verhindert blindes Stapeln bis zur Margin-Erschöpfung und lässt einen
# Sicherheitspuffer gegen Liquidationen bei korrelierten Adverse-Moves. Atomar
# erzwungen via per-User-Lock (sonst überrennt ein Signal-Burst den Cap).
# 0.85 = bei 85% Auslastung keine neuen Trades mehr. Höher = aggressiver.
MAX_MARGIN_UTILIZATION = _f("MAX_MARGIN_UTILIZATION", 0.85)

# 2026-06-09 H1: Startup-Reconciler. Nach jedem (Re)Start prüfen, ob jede offene
# HL-Position eine reduce-only Stop-Order hat; fehlt sie (Prozess starb im Fill-
# Fenster), Schutz aus dem managed_trade nachziehen. true = an.
STARTUP_PROTECTION_RECONCILE = _b("STARTUP_PROTECTION_RECONCILE", "true")

# Discord-Webhook URL für Error-Alerts. Leer = aus.
ALERT_WEBHOOK_URL = _g("ALERT_WEBHOOK_URL", "")
ALERT_THROTTLE_S = _i("ALERT_THROTTLE_S", 60)           # max 1 Alert pro 60s pro (user, coin)

# EMERGENCY_HALT-Flag — wenn True, ignoriert handle_signal alle Signale.
# Wird gesetzt durch /api/admin/halt oder durch automatic-Trigger
# (MAX_SIGNALS_PER_HOUR überschritten). Pfad zur Datei statt env-var damit
# wir's zur Laufzeit toggeln können ohne Service-Restart.
EMERGENCY_HALT_FLAG_PATH = _g("EMERGENCY_HALT_FLAG_PATH", "/tmp/goathub-emergency-halt")

# Discord OAuth2
DISCORD_CLIENT_ID = _g("DISCORD_CLIENT_ID", "1508987342482837524")
DISCORD_CLIENT_SECRET = _g("DISCORD_CLIENT_SECRET", "")   # NUR aus .env — niemals im Code (Secret rotieren!)
DISCORD_REDIRECT_URI = _g("DISCORD_REDIRECT_URI", "https://bot.goathub.network/auth/callback")
DISCORD_REQUIRED_ROLE_ID = _g("DISCORD_REQUIRED_ROLE_ID", "1481638494706204732")
DISCORD_GUILD_ID = _g("DISCORD_GUILD_ID", "")  # filled via .env
