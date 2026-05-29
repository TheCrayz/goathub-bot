# GoatHub Trading Bot — Multi-User Copy-Trading Platform

A standalone multi-user product (**not** TradingHub). Users sign up, connect their **own Hyperliquid wallet**, set risk settings + a **capital cap**, and watch live **PnL / positions / bot activity**. A signal posted to Discord `#signals` (by "Bot 1", a private signal bot) is executed on **every active user account**. Each order can carry a **builder code** so the platform operator earns a share of fees.

> **Live status (2026-05-29):** Running on **testnet**, listener **on**, builder **off**. Deployed commit `a459d6f`. One known blocker: two beta testers saved a wallet **address** into the agent-key field — see [Known issues](#known-issues). New devs: read [Critical gotchas](#critical-gotchas-read-before-touching-wallets) first.

---

## What it does

- Listens to Discord `#signals` and parses each signal (`NEW_TRADE` / `UPDATE_TRADE` / `CANCEL_TRADE` / `HOLD`).
- For every **active** user, sizes the position (risk % + capital cap), checks balance / max-open / min-notional / tradability, and places the entry + SL/TP on **their** Hyperliquid account.
- Reacts to the full managed-trade lifecycle (open / adjust SL-TP / close) based on **live** position & order state.
- Shows each user a dashboard: balance, open positions, PnL stats, and a per-user activity log.

It executes on the user's own account via an **agent key** (no withdrawal rights). It never holds user funds.

---

## Architecture

| Component | File | Notes |
|---|---|---|
| API + dashboard page | `app/main.py` | FastAPI; serves `dashboard.html` at `/` |
| Auth | `app/auth.py` | Email/password (bcrypt) + JWT; Discord OAuth in `main.py` + `app/discord_oauth.py` |
| DB | `app/db.py`, `app/models.py` | SQLAlchemy, SQLite (`goathub.db`). `SessionLocal`, auto-migrate of new columns |
| Key storage | `app/crypto.py` | Per-user agent key encrypted at rest (Fernet) |
| Signal intake | `app/discord_listener.py` | Reads `#signals` (needs Message Content Intent) |
| Parsing | `app/parser.py` | Signal → structured action; accepts `CANCEL` via action field or title fallback |
| Execution engine | `app/engine.py` | 1 signal → all active users; sizing, risk checks, lifecycle, `asyncio.Lock` per (user, coin) |
| Hyperliquid I/O | `app/hyperliquid_exec.py` | Entry, SL/TP (`place_protection`), `cancel_orders`, `close_position`, builder code |
| Dashboard UI | `app/dashboard.html` | Single page: login / settings / wallet / PnL / activity |

**Two modes — switch `ENABLE_LISTENER`:**
- `false` → API + dashboard only (no live trading). Ideal for localhost/dev.
- `true` → Discord listener + engine active → real (test/live) trading.

---

## Run locally

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp .env.example .env          # fill JWT_SECRET + ENCRYPTION_KEY (commands are in the file)
PYTHONPATH=. venv/bin/python tests/test_core.py        # logic tests
venv/bin/uvicorn app.main:app --reload --port 8000     # http://localhost:8000
```

Keep `ENABLE_LISTENER=false` and `HL_TESTNET=true` locally.

---

## Configuration (`.env`)

> Values live only in the server `.env` (gitignored) — **never commit secrets.** Template: `.env.example`.

| Key | Meaning |
|---|---|
| `DATABASE_URL` | default `sqlite:///./goathub.db` |
| `JWT_SECRET` | **hard-fails on startup if empty/default** |
| `ENCRYPTION_KEY` | Fernet key for agent-key encryption |
| `HL_TESTNET` | `true` = testnet, `false` = **mainnet (real money)** |
| `ENABLE_LISTENER` | `true` arms live trading |
| `BUILDER_ADDRESS` | platform fee wallet; **empty = builder off** (current beta state) |
| `BUILDER_FEE` | e.g. `0.05%`; perps max is `0.1%` |
| `SIGNALS_CHANNEL_ID`, `DISCORD_BOT_TOKEN` | signal source |
| `DEFAULT_RISK_PCT` / `DEFAULT_LEVERAGE` / `DEFAULT_MAX_OPEN` / `MIN_NOTIONAL_USDC` / `MIN_CONFIDENCE` | new-user defaults / gates |

---

## Critical gotchas (read before touching wallets)

1. **Address vs. Key — the #1 support issue.**
   - "MASTER Address" = the public wallet **with funds**: `0x` + 40 hex = **42 chars**.
   - "Agent Private Key" = the long key from the API-wallet box: `0x` + 64 hex = **66 chars**.
   - Putting the address into the key field → `ValueError: private key must be exactly 32 bytes ... got 20 bytes`. The order never places.
   - `set_wallet()` now **validates this on save** (length + `Account.from_key`), but **accounts saved before this validation are still broken** in the DB (see Known issues).
2. **Self-builder is forbidden:** `BUILDER_ADDRESS` must never equal a trading account, or orders are rejected ("Builder fee has not been approved").
3. **Builder approval is two-sided:** the dashboard "confirm" button only sets a DB flag; the user must also run `approveBuilderFee` **on-chain** in the Hyperliquid UI. Flag set + no on-chain approval = orders rejected.
4. `account_value` = perps `marginSummary.accountValue` **+** spot USDC.

---

## Current status (2026-05-29)

- Deployed at HEAD `a459d6f` (pre-beta hardening + managed-trade lifecycle). CI/CD auto-deploys on push to `main` (`.github/workflows/deploy.yml` → SSH `git pull` + service restart).
- `systemd` unit `goathub` active; listener connected as `GoatHub Copy Trading Bot#2523`, channel `…294`, **testnet**.
- **Builder off** (`BUILDER_ADDRESS` empty) → 0 builder errors in the last 24h.
- **Active users:** id 2 (`tretaghunberg`, key OK, **trading fine** — ETH long + DOGE short open with SL/TP). ids 3 & 4 active but **broken** (below). All other accounts inactive.

---

## Known issues

| # | Severity | Issue | Fix |
|---|---|---|---|
| 1 | 🔴 High | **Users 3 (`nasenloch63`) & 4 (`og_b1312`) have an address in the agent-key field** (stored key length 42, expected 66). Every signal → `Account.from_key` `ValueError` → traceback. They generate ~all of the ~295 daily exceptions **and never trade.** | Disable their bots (`bot_active=False`) and have them re-enter the correct **66-char Agent key** (not the address). Pre-existing bad data — the save-time validation doesn't fix it retroactively. |
| 2 | 🟡 Med | `engine._run_user` catches the bad key per user but raises a **full traceback every cycle** → log spam. | Detect an unparseable/short key once, **skip the user gracefully**, auto-pause `bot_active`, and surface a dashboard warning ("re-enter your agent key"). |
| 3 | 🟡 Med | **No on-chain `approveBuilderFee` flow.** Dashboard "confirm" sets `builder_approved` only. When builder is re-enabled for mainnet, users with the flag but no on-chain approval will have orders rejected. | Build the on-chain approval step into the dashboard before enabling builder. |
| 4 | 🟢 Low | Dashboard shows "Builder: confirmed" while `BUILDER_ADDRESS` is empty → confusing. | Hide/derive builder status from the actual config. |
| 5 | 🟢 Low | Some Discord logins return `/?error=no_role`. | Confirm the role gate (`DISCORD_GUILD_ID` / `DISCORD_REQUIRED_ROLE_ID`) is configured as intended (it's intentionally open if `DISCORD_GUILD_ID` is empty). |
| 6 | 🟢 Low | Dashboard timezone mismatch (Bot Activity in UTC, fill history in local time). | Normalize to one timezone. |

---

## Next steps to production (mainnet + builder fee)

- [ ] Resolve Known issues #1 and #2 (the tester key problem + graceful skip).
- [ ] **Rotate the compromised Discord client secret and purge it from git history** before inviting external collaborators — it still exists in earlier commits and any collaborator can read history.
- [ ] Create a **separate referral wallet** (≠ any trading account), set as `BUILDER_ADDRESS`, fund with ≥100 USDC perps.
- [ ] Set `BUILDER_FEE` ≤ `0.1%` (e.g. `0.05%`) and ship the on-chain approval flow (#3).
- [ ] HTTPS reverse proxy (Caddy) + Discord OAuth redirect URI for `bot.goathub.network`.
- [ ] Move from SQLite → Postgres before scaling past a handful of users.
- [ ] Only then flip `HL_TESTNET=false`.

---

## Operate & diagnose

```bash
# service
systemctl status goathub --no-pager
journalctl -u goathub -f                       # live logs

# error summary (last 24h)
journalctl -u goathub --since "24 hours ago" --no-pager | grep -ciE "traceback|exception"

# which users have a bad key? (prints key LENGTH, never the key)
cd /var/www/goathub-bot && PYTHONPATH=. venv/bin/python -c "
from app.db import SessionLocal; from app.models import User; from app.crypto import decrypt
for u in SessionLocal().query(User).order_by(User.id):
    n = len(decrypt(u.hl_api_secret_enc)) if u.hl_api_secret_enc else 0
    print(u.id, u.discord_username or u.email, 'active=%s'%u.bot_active, 'keylen=%s'%n, '(66=ok, 42=ADDRESS!)')
"
```

---

## Security notes

- Agent keys only (no withdrawal rights), encrypted at rest. **Never** store master keys.
- `.env` is gitignored; secrets must never be committed.
- Testnet first (`HL_TESTNET=true`), then mainnet. **Not financial advice.**
