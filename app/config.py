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
JWT_EXPIRE_HOURS = _i("JWT_EXPIRE_HOURS", 168)   # 7 Tage statt 30 (kürzere Token-Lebensdauer)
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

# Defaults für neue Nutzer
DEFAULT_RISK_PCT = _f("DEFAULT_RISK_PCT", 0.01)
DEFAULT_LEVERAGE = _f("DEFAULT_LEVERAGE", 50)   # 2026-06-06: jetzt Max-Cap für Auto-Lev (war 3x fixed)
DEFAULT_MAX_OPEN = _i("DEFAULT_MAX_OPEN", 10)
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

# Discord OAuth2
DISCORD_CLIENT_ID = _g("DISCORD_CLIENT_ID", "1508987342482837524")
DISCORD_CLIENT_SECRET = _g("DISCORD_CLIENT_SECRET", "")   # NUR aus .env — niemals im Code (Secret rotieren!)
DISCORD_REDIRECT_URI = _g("DISCORD_REDIRECT_URI", "https://bot.goathub.network/auth/callback")
DISCORD_REQUIRED_ROLE_ID = _g("DISCORD_REQUIRED_ROLE_ID", "1481638494706204732")
DISCORD_GUILD_ID = _g("DISCORD_GUILD_ID", "")  # filled via .env
