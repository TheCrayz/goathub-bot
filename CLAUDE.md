# GoatHub Trading Bot — Agent Guide

Multi-user FastAPI backend + dashboard that mirrors Discord trading signals onto
Hyperliquid perps for each connected user. Real money on mainnet — correctness
and safety beat cleverness everywhere.

## FastAPI conventions

Follow the **`fastapi-patterns` skill** (`.agents/skills/fastapi-patterns/SKILL.md`)
for new FastAPI code: Pydantic v2 schemas, dependency injection, defensive JWT
parsing, transactional service methods, `401` vs `403` separation, deterministic
`.order_by()` on every paginated/list query, and typed `response_model`s on new
endpoints. Read it before adding or changing routes, schemas, or auth.

## Deliberate divergences from the skill — do NOT "fix" these

These look like the skill's anti-patterns but are intentional. Changing them
would break the app:

- **Sync `def` route handlers + sync SQLAlchemy `Session`.** FastAPI runs sync
  routes in a threadpool, so this does not block the event loop. The app is
  **single-process by design** (the `fcntl` trader-lock in `app/main.py`, plus
  in-memory dedup/throttle/lock caches in `app/engine.py` all assume exactly one
  process). Do **not** migrate to async SQLAlchemy or run `uvicorn --workers >1`.
- **`async def` routes must keep DB and blocking I/O out of the handler body.**
  If a route genuinely needs to be `async` (e.g. it `await`s HL calls), wrap any
  DB work and blocking SDK calls in `asyncio.to_thread(...)` — never call
  `db.query()/commit()` directly on the loop. See `referral_status` /
  `set_referrer` for the correct pattern.
- **`email: str` + regex validation instead of `EmailStr`** — avoids the
  `email-validator` dependency on purpose (see `app/schemas.py`).
- **Hand-rolled migrations in `app/db.py` (`init_db` / `_migrate_add_fk`)**
  instead of Alembic — intentional for this deploy model.
- **Naive-UTC datetimes** are a documented convention (`app/models.py`); store
  naive UTC, fix timezone at the serialization boundary.

## Layout

- `app/main.py` — app factory, lifespan, auth/settings/wallet/dashboard routes.
- `app/admin.py` — admin router (`/api/admin/*`, gated by `current_admin_user`).
- `app/auth.py` — password hashing (SHA256→bcrypt v2) + JWT (`current_user`).
- `app/engine.py` — trading engine (signal handling, order placement, watchers).
- `app/hyperliquid_exec.py` / `app/sync.py` — HL execution + position reconcile.
- `app/models.py` / `app/schemas.py` / `app/db.py` — ORM, Pydantic, DB setup.
- `tests/` — pytest; shared in-memory SQLite wired in `tests/conftest.py`.

## Workflow

- Tests: `pytest` (CI gates the auto-deploy — keep it green).
- Trading-path changes touch real money: prefer rejecting (`422`/`400`) over
  silent clamping/coercing of user input.
