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
# 2026-06-12 Key-Rotation (#43): optionaler ALTER Fernet-Key. Bei Rotation:
# neuen Key nach ENCRYPTION_KEY, alten nach ENCRYPTION_KEY_OLD — decrypt
# probiert beide (MultiFernet in crypto.py), encrypt nutzt nur den neuen.
# Danach können Agent-Keys nach und nach re-encrypted und _OLD entfernt werden.
ENCRYPTION_KEY_OLD = _g("ENCRYPTION_KEY_OLD", "")

# 2026-06-12 M-15: generalisierte Rotation via ENCRYPTION_KEYS — kommasepariert,
# NEUESTER Key ZUERST. encrypt() nutzt immer den ersten Key, decrypt() probiert
# alle der Reihe nach (MultiFernet in crypto.py). Wenn ENCRYPTION_KEYS gesetzt
# ist, gewinnt es; sonst rückwärtskompatibel ENCRYPTION_KEY (+ optional
# ENCRYPTION_KEY_OLD aus #43).
_keys_csv = _g("ENCRYPTION_KEYS", "")
if _keys_csv:
    ENCRYPTION_KEYS = [k.strip() for k in _keys_csv.split(",") if k.strip()]
else:
    ENCRYPTION_KEYS = [k for k in (ENCRYPTION_KEY, ENCRYPTION_KEY_OLD) if k]

# 2026-06-12 #43: ENCRYPTION_KEY beim Import validieren — spiegelbildlich zum
# JWT_SECRET-Check oben. Vorher flog ein fehlender/kaputter Key erst zur
# Laufzeit in crypto._fernet() — schlimmstenfalls MITTEN im Signal, wenn die
# Engine Agent-Keys decrypten will → Trading für ALLE User still kaputt.
# Jetzt: Service startet gar nicht erst ohne gültigen Fernet-Key.
def _validate_fernet(name, key):
    from cryptography.fernet import Fernet
    try:
        Fernet(key.encode() if isinstance(key, str) else key)
    except Exception as e:
        raise RuntimeError(
            f"{name} fehlt oder ist kein gültiger Fernet-Key ({e}). In der .env setzen, z.B.:\n"
            f"  {name}=$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')\n"
            "Ohne gültigen Key können HL-Agent-Keys nicht ver-/entschlüsselt werden.")


if not ENCRYPTION_KEYS:
    _validate_fernet("ENCRYPTION_KEY", "")   # → RuntimeError "fehlt …"
for _idx, _key in enumerate(ENCRYPTION_KEYS):
    _validate_fernet(f"ENCRYPTION_KEYS[{_idx}]" if _keys_csv else
                     ("ENCRYPTION_KEY" if _idx == 0 else "ENCRYPTION_KEY_OLD"), _key)
# Konsistenz: config.ENCRYPTION_KEY ist IMMER der neueste (= encrypt-)Key,
# egal ob er aus ENCRYPTION_KEYS oder ENCRYPTION_KEY kam.
ENCRYPTION_KEY = ENCRYPTION_KEYS[0]

# Hyperliquid
# 2026-06-13 H-17: HL_TESTNET STRIKT parsen (fail-closed). Vorher machte _b()
# aus JEDEM nicht erkannten Wert False → ein Tippfehler wie HL_TESTNET="ture"
# oder "True " landete STILL auf MAINNET (= echtes Geld, gefährliche Richtung).
# Jetzt: nur explizit erkannte true/false-Werte sind erlaubt; ein gesetzter,
# aber unparsebarer Wert lässt den Import hart fehlschlagen (wie der
# JWT_SECRET-/Fernet-Hard-Fail oben). Ein FEHLENDES HL_TESTNET nimmt weiter
# sauber den Default (testnet=true).
_TRUE_VALS = ("1", "true", "yes", "on")
_FALSE_VALS = ("0", "false", "no", "off")


def _strict_bool(n, d):
    raw = _g(n)
    if raw is None:
        return d          # nicht gesetzt → Default (kein Hard-Fail)
    s = str(raw).strip().lower()
    if s in _TRUE_VALS:
        return True
    if s in _FALSE_VALS:
        return False
    raise RuntimeError(
        f"{n}={raw!r} ist kein gültiger Boolean. Erlaubt sind "
        f"{_TRUE_VALS + _FALSE_VALS}. Ein Tippfehler hier (z.B. 'ture') würde "
        f"sonst STILL auf Mainnet schalten — der Service startet deshalb nicht.")


HL_TESTNET = _strict_bool("HL_TESTNET", True)

# Beim Import gut sichtbar loggen, auf welchem Netz wir laufen — eine
# Fehl-Konfiguration soll im Boot-Log sofort ins Auge springen.
import logging as _net_logging
_net_logging.getLogger("goathub.config").warning(
    "NET=%s (HL_TESTNET=%s)", "TESTNET" if HL_TESTNET else "MAINNET", HL_TESTNET)

# Referral / Builder-Code (Michaels Gebühren-Anteil)
BUILDER_ADDRESS = _g("BUILDER_ADDRESS")      # deine HL-Adresse (braucht >=100 USDC perps)
BUILDER_FEE = _g("BUILDER_FEE", "0.05%")     # f bis 0.1% (perps max)

# Hyperliquid-Referral: User verknüpfen ihren HL-Account über Michaels Code.
REFERRAL_CODE = _g("REFERRAL_CODE", "TRETAGHUNBERG")
REFERRAL_LINK = _g("REFERRAL_LINK", "https://app.hyperliquid.xyz/join/TRETAGHUNBERG")

# Signal-Quelle (#signals von Bot 1, privat)
DISCORD_BOT_TOKEN = _g("DISCORD_BOT_TOKEN")
SIGNALS_CHANNEL_ID = _i("SIGNALS_CHANNEL_ID", 0)
ENABLE_LISTENER = _b("ENABLE_LISTENER", "false")   # für localhost-Test ohne Discord = false

# 2026-06-12 LOW-12: /docs, /redoc, /openapi.json leaken die komplette
# API-Surface (inkl. Admin-Endpoints) — default AUS, nur lokal auf true.
# main.py liest das via getattr(config, "ENABLE_DOCS", False).
ENABLE_DOCS = _b("ENABLE_DOCS", "false")

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
# 2026-06-13 H-16 (Agent B): absoluter USD-Cap für die NOTIONAL (size × price)
# EINER einzelnen Position. Hard-Limit gegen eine fehlerhafte/feindliche
# Size-Berechnung, die unabhängig von Risk-%/Leverage eine riesige Order
# aufmachen würde. 0 = aus (kein Cap). engine.H-16 erzwingt das beim Sizing.
MAX_NOTIONAL_PER_TRADE = _f("MAX_NOTIONAL_PER_TRADE", 50000)
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
# 2026-06-12 LOW-11: /tmp ist world-writable — JEDER lokale User konnte den
# Bot per `touch /tmp/goathub-emergency-halt` lahmlegen (DoS) bzw. den vom
# Admin gesetzten Halt löschen. Default jetzt /var/lib/goathub/emergency-halt
# (nur goathub-User schreibbar, übersteht PrivateTmp=true im systemd-Unit);
# Fallback auf /tmp NUR wenn das Verzeichnis nicht existiert/anlegbar ist
# (z.B. lokales Dev auf macOS) — dann mit lauter Warnung. Env-Override
# (EMERGENCY_HALT_FLAG_PATH) gewinnt immer.
_halt_env = _g("EMERGENCY_HALT_FLAG_PATH")
if _halt_env:
    EMERGENCY_HALT_FLAG_PATH = _halt_env
else:
    _halt_dir = "/var/lib/goathub"
    try:
        os.makedirs(_halt_dir, exist_ok=True)
    except OSError:
        pass
    if os.path.isdir(_halt_dir) and os.access(_halt_dir, os.W_OK):
        EMERGENCY_HALT_FLAG_PATH = os.path.join(_halt_dir, "emergency-halt")
    else:
        import logging as _logging
        _logging.getLogger("goathub.config").warning(
            "EMERGENCY_HALT_FLAG_PATH: /var/lib/goathub nicht beschreibbar — "
            "Fallback auf world-writable /tmp/goathub-emergency-halt. Auf dem "
            "Server: mkdir -p /var/lib/goathub && chown goathub:goathub /var/lib/goathub")
        EMERGENCY_HALT_FLAG_PATH = "/tmp/goathub-emergency-halt"

# 2026-06-12 #54: Token-Usage-Scraper nur noch per Opt-in. Vorher lief der
# Loop in JEDEM Deployment und pollte einen hardcoded Docker-Pfad aus dem
# SEPARATEN TradingHub-Projekt — auf jedem Host ohne dieses Volume ein
# stiller No-Op (totes Cross-Projekt-Coupling). Jetzt: leer = Loop wird gar
# nicht gestartet (eine INFO-Zeile beim Boot). Auf dem TradingHub-Host in
# der .env setzen, z.B.:
#   SIGNALBOT_LOG_PATH=/var/lib/docker/volumes/tradinghub-signalbeta_signalbeta-data/_data/logs/bot.log
SIGNALBOT_LOG_PATH = _g("SIGNALBOT_LOG_PATH", "")

# 2026-06-12 M-20: kanonische Namen für den Token-Usage-Scraper — main.py und
# admin.py lesen die via getattr(config, ...). TOKEN_USAGE_LOG_PATH überstimmt
# das ältere SIGNALBOT_LOG_PATH (bleibt als Fallback für bestehende .envs);
# Default ist der frühere hardcoded TradingHub-Docker-Pfad.
# TOKEN_SCRAPER_ENABLED default: NUR an, wenn der Pfad beim Start existiert
# UND lesbar ist — auf Hosts ohne das Volume (oder als unprivilegierter
# goathub-User) bleibt der Scraper damit automatisch aus. Explizites
# TOKEN_SCRAPER_ENABLED=true/false in der .env gewinnt immer.
_TOKEN_LOG_DEFAULT = "/var/lib/docker/volumes/tradinghub-signalbeta_signalbeta-data/_data/logs/bot.log"
TOKEN_USAGE_LOG_PATH = _g("TOKEN_USAGE_LOG_PATH") or SIGNALBOT_LOG_PATH or _TOKEN_LOG_DEFAULT
TOKEN_SCRAPER_ENABLED = _b(
    "TOKEN_SCRAPER_ENABLED",
    "true" if (os.path.isfile(TOKEN_USAGE_LOG_PATH)
               and os.access(TOKEN_USAGE_LOG_PATH, os.R_OK)) else "false")

# Discord OAuth2
DISCORD_CLIENT_ID = _g("DISCORD_CLIENT_ID", "1508987342482837524")
DISCORD_CLIENT_SECRET = _g("DISCORD_CLIENT_SECRET", "")   # NUR aus .env — niemals im Code (Secret rotieren!)
DISCORD_REDIRECT_URI = _g("DISCORD_REDIRECT_URI", "https://bot.goathub.network/auth/callback")
DISCORD_REQUIRED_ROLE_ID = _g("DISCORD_REQUIRED_ROLE_ID", "1481638494706204732")
DISCORD_GUILD_ID = _g("DISCORD_GUILD_ID", "")  # filled via .env
