# GoatHub Trading Bot — Community Copy-Trading-Plattform

Eigenständiges Multi-User-Produkt (NICHT TradingHub). Nutzer melden sich an,
verbinden ihre **eigene Hyperliquid-Wallet**, setzen Risk-Settings + **Kapital-Cap**
und sehen live **PNL / Positionen / Bot-Aktivität**. Ein Signal aus `#signals`
(von Bot 1, privat) wird auf **jedem aktiven Nutzer-Konto** ausgeführt. Jede Order
trägt einen **Builder-Code** → Plattform-Betreiber verdient % der Gebühren.

## Architektur
- **Backend:** FastAPI + SQLite (SQLAlchemy). `app/main.py` (API + Dashboard-Seite).
- **Auth:** E-Mail/Passwort (bcrypt) + JWT (`app/auth.py`).
- **Keys:** HL-Agent-Key pro Nutzer **verschlüsselt** (Fernet, `app/crypto.py`).
- **Engine:** `app/engine.py` — 1 Signal → alle aktiven Nutzer (Sizing mit Kapital-Cap,
  Risk-Checks, Entry + SL/TP, Builder-Code). `app/discord_listener.py` liest `#signals`.
- **Dashboard:** `app/dashboard.html` (Single-Page, Login/Settings/Wallet/PNL/Aktivität).

## Zwei Modi (Schalter `ENABLE_LISTENER`)
- `false` → nur API + Dashboard (kein Live-Trading) — ideal für localhost/Entwicklung.
- `true` → Discord-Listener + Engine aktiv → echtes (Test-/Live-)Trading.

## Lokal starten
```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp .env.example .env   # JWT_SECRET + ENCRYPTION_KEY ausfüllen (Befehle stehen drin)
PYTHONPATH=. venv/bin/python tests/test_core.py        # Logik-Tests
venv/bin/uvicorn app.main:app --reload --port 8000     # http://localhost:8000
```

## Sicherheit / Hinweise
- Agent-Keys (keine Auszahlungsrechte), verschlüsselt at rest. Nie Master-Keys speichern.
- Referral: Nutzer geben einmalig `approve_builder_fee` in der HL-UI frei.
- Erst Testnet (`HL_TESTNET=true`), dann Mainnet. Keine Finanzberatung.
