"""Zentrale Test-Konfiguration — EINE geteilte In-Memory-DB für alle Testdateien.

Vorher hat jede Testdatei beim Import ihre eigene StaticPool-:memory:-Engine in
app.db verdrahtet. Im selben pytest-Prozess binden die App-Module SessionLocal
aber genau EINMAL (beim ersten Import) — je nach Datei-Kombination zeigten
App-Code und Tests dann auf VERSCHIEDENE DBs (order-abhängige Failures, z.B.
test_trading_paths rot nur im Gesamtlauf).

pytest importiert conftest.py VOR allen Testmodulen — deshalb ist HIER der
einzige Ort, der env + DB verdrahtet. Die Testdateien behalten nur einen
idempotenten Fallback für Standalone-Ausführung (python tests/test_x.py).
"""
import os
import tempfile

from cryptography.fernet import Fernet

# Env MUSS vor jedem app.*-Import stehen (config.py validiert beim Import).
os.environ.setdefault("JWT_SECRET", "test-only-secret-not-prod-1234567890abcdef")
os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ENABLE_LISTENER", "false")
# Tests legen ihre Test-User via /api/register an → Registrierung im Test offen.
# Die Invite-only-Sperre (Prod-Default) wird separat gezielt getestet.
os.environ.setdefault("REGISTRATION_OPEN", "true")
# Halt-Flag auf testeigenen Pfad — Tests dürfen nie ein echtes Flag sehen/löschen.
os.environ.setdefault(
    "EMERGENCY_HALT_FLAG_PATH",
    os.path.join(tempfile.gettempdir(), "goathub-test-halt-%d" % os.getpid()),
)

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import app.db as _dbmod  # noqa: E402

_test_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
    future=True,
)
_dbmod.engine = _test_engine
_dbmod.SessionLocal = sessionmaker(
    bind=_test_engine, autoflush=False, autocommit=False, future=True
)

from app.db import Base  # noqa: E402
import app.models  # noqa: F401,E402  (registriert alle Tabellen an Base)

Base.metadata.create_all(_test_engine)

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_shared_state():
    """Nach jedem Test: DB-Rows + Engine-Globals leeren (kein Cross-Test-Leak)."""
    yield
    import app.engine as engine
    from app import config

    with _test_engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
    for d in (
        engine._percoin_cache,
        engine._trade_intervals,
        engine._alert_throttle,
        engine._recent_pause_keys,
        engine._fill_watchers,
        engine._locks,
        engine._user_locks,
    ):
        d.clear()
    del engine._signal_timestamps[:]
    # H-12 (2026-06-14): prozessweiter Rate-Limit-Breaker ist In-Memory-Globalstate
    # wie die engine-Caches. Nach jedem Test zurücksetzen, sonst leakt ein in einem
    # Test gesetzter Breaker (z.B. test_hl_exec note_rate_limit(5)) in den nächsten
    # Test — seit der H-12-Verdrahtung würde der Sync-Loop dort fälschlich deferren.
    import app.hl_retry as _hlr
    _hlr._rate_limited_until = 0.0
    try:
        os.remove(config.EMERGENCY_HALT_FLAG_PATH)
    except OSError:
        pass
