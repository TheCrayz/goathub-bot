# GoatHub Trading Bot — Multi-User Copy-Trading Platform

A standalone multi-user product (**not** TradingHub). Users sign up, connect their **own Hyperliquid wallet**, set risk settings + a **capital cap**, and watch live **PnL / positions / bot activity**. A signal posted to Discord `#signals` (by "Bot 1", a private signal bot) is executed on **every active user account**. Each order can carry a **builder code** so the platform operator earns a share of fees.

> **Live status (2026-06-12):** Running on **testnet**, listener **on**, builder **off**. One known blocker: two beta testers saved a wallet **address** into the agent-key field — their bots are auto-paused until they re-enter the key (see [Known issues](#known-issues)). New devs: read [Critical gotchas](#critical-gotchas-read-before-touching-wallets) first.

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
PYTHONPATH=. venv/bin/python -m pytest tests/ -q       # full test suite (same as CI)
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
3. **Builder approval is verified on-chain (since Phase 5/6):** the dashboard "confirm" button (`/api/builder-approved`, `main.py`) queries Hyperliquid's `maxBuilderFee` and only sets `builder_approved` if the **on-chain** approval actually covers `BUILDER_FEE` — it is **not** a trust-me DB flag anymore. Users can approve either via the dashboard's MetaMask flow (`/api/builder-approval-submit` relays the EIP-712-signed `approveBuilderFee` to HL) or manually in the Hyperliquid UI and then click confirm. Gotcha: the approval must be signed by the **master address**, not the agent key.
4. `account_value` = perps `marginSummary.accountValue` **+** spot USDC.

---

## Current status (2026-06-12)

- Latest verified UI pass: polished dashboard sections, mobile-friendly metadata, and live trading overview cards.
- Verified locally with `python3 -m pytest -q` → `61 passed` (incl. fake-trader harness for the money paths, web/API security tests, listener/sync tests; shared test DB wiring lives in `tests/conftest.py`).
- Deployment path is `systemd`, not Docker: [goathub.service](goathub.service) runs `uvicorn app.main:app --host 127.0.0.1 --port 8000` (loopback only — Caddy terminates TLS and proxies locally; the unit runs as the dedicated `goathub` user with systemd sandboxing, see comments in the unit file).
- CI/CD auto-deploys on push to `main` (`.github/workflows/deploy.yml` → SSH `git pull` + service restart).
- **Builder off** (`BUILDER_ADDRESS` empty) → 0 builder errors in the last 24h.
- **Active users:** id 2 (`tretaghunberg`, key OK, **trading fine** — ETH long + DOGE short open with SL/TP). ids 3 & 4 active but **broken** (below). All other accounts inactive.

---

## Ultra-Upgrade 2026-06-12

A large multi-agent review + hardening pass landed on the `claude/ultra-upgrade` branch: a structured code review produced a list of confirmed findings (security, correctness, ops, docs), which were then fixed across the codebase. Ops/deploy highlights:

- **systemd hardening:** `goathub.service` now runs as a dedicated `goathub` user (not root) with `NoNewPrivileges`, `ProtectSystem=strict` + scoped `ReadWritePaths`, and binds uvicorn to `127.0.0.1` (Caddy proxies locally). One-time `useradd`/`chown` steps are documented in the unit file.
- **CI/CD:** tests now run via real pytest collection (no more hand-registered `__main__` lists), the deploy makes a timestamped `goathub.db` backup before `git pull`, and the workflow only goes green after `systemctl is-active` + an HTTP `/api/health` check pass. The third-party SSH action is pinned to a commit SHA.
- **Config/deps:** `.env.example` aligned with the hardened risk defaults (`0.005` / `20` / `5`), `pytest` and `eth-account` pinned exactly.
- **Docs:** this README updated to match the implemented on-chain builder approval flow and the bad-key auto-pause (see Known issues #2/#3 below, now fixed).
- **Audit-fix batch (same day, full-codebase audit):** emergency-close results are now verified (naked-position alert instead of a false "closed" log), the fill-watcher takes the per-(user,coin) lock + a signal-generation check, SL/TP orders carry cloids (retry idempotency), undecryptable keys auto-pause like bad keys, the Discord listener resets its backoff and **backfills missed signals after reconnect**, stop-coverage is reconciled periodically (not just at startup), admin `test-signal` needs `confirm:true` + rate limit, per-account login lockout + 30-day absolute session lifetime, `builder_approved` resets on wallet change, `processed_signal` included in the Postgres migration, `ENCRYPTION_KEYS` rotation via MultiFernet, deploys gate on a quiet trading window (`scripts/safe_restart.sh`), and a daily DB backup timer ships with the unit files.

---

## Known issues

| # | Severity | Issue | Fix |
|---|---|---|---|
| 1 | 🔴 High | **Users 3 (`nasenloch63`) & 4 (`og_b1312`) have an address in the agent-key field** (stored key length 42, expected 66). Every signal → `Account.from_key` `ValueError` → traceback. They generate ~all of the ~295 daily exceptions **and never trade.** | Disable their bots (`bot_active=False`) and have them re-enter the correct **66-char Agent key** (not the address). Pre-existing bad data — the save-time validation doesn't fix it retroactively. |
| 2 | ✅ Fixed | ~~Bad agent key raises a full traceback every cycle → log spam.~~ Implemented: `_pause_user_bad_key` (`app/engine.py`) detects a broken **or unauthorized** key, auto-pauses `bot_active`, and writes exactly **one** activity row telling the user to re-enter the 66-char agent key (idempotent, race-safe). | Done — remaining user action is issue #1 (testers must re-enter the correct key). |
| 3 | ✅ Fixed | ~~No on-chain `approveBuilderFee` flow.~~ Implemented: `/api/builder-approval-submit` relays the MetaMask-signed EIP-712 `approveBuilderFee` to HL, and `/api/builder-approved` verifies the approval **on-chain** (`maxBuilderFee`) before setting the flag. See gotcha #3. | Done — just configure `BUILDER_ADDRESS`/`BUILDER_FEE` when enabling builder. |
| 4 | ✅ Fixed | ~~Dashboard shows "Builder: confirmed" while `BUILDER_ADDRESS` is empty.~~ The builder card derives its state from the actual config (disabled mode when `BUILDER_ADDRESS` is empty) and `signBuilderApproval()` only shows "confirmed" after the server verifies on-chain. | Done. |
| 5 | 🟢 Low | Some Discord logins return `/?error=no_role`. The gate is intentionally open if `DISCORD_GUILD_ID` is empty — since 2026-06-12 this is **logged loudly** at startup and per bypassed login instead of silently allowed. | Configure `DISCORD_GUILD_ID` / `DISCORD_REQUIRED_ROLE_ID` before mainnet. |
| 6 | 🟢 Low | Dashboard timezone mismatch (Bot Activity in UTC, fill history in local time). | Normalize to one timezone. |

---

## Next steps to production (mainnet + builder fee)

- [ ] Resolve Known issue #1 (testers 3 & 4 must re-enter their 66-char agent key — the graceful auto-pause from #2 is already live).
- [x] ~~Rotate the compromised Discord client secret and purge it from git history.~~ Done: secret rotated 2026-05 and the history rewritten (only a redacted placeholder remains in `c6d7a82`; full-history sweep 2026-06-12 found no live secrets). Residual: GitHub may still serve pre-rewrite commits as dangling objects by hash — ask GitHub support to run GC if external collaborators join.
- [ ] Create a **separate referral wallet** (≠ any trading account), set as `BUILDER_ADDRESS`, fund with ≥100 USDC perps.
- [ ] Set `BUILDER_FEE` ≤ `0.1%` (e.g. `0.05%`) — the on-chain approval flow (#3) is already shipped; have each user approve via the dashboard.
- [x] ~~HTTPS reverse proxy (Caddy) + Discord OAuth redirect URI for `bot.goathub.network`.~~ Live — Caddy terminates TLS and proxies to `127.0.0.1:8000`.
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
