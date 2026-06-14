"""HL-API Retry-Wrapper (2026-06-08 Mainnet-Hardening A5).

Standalone module ohne eth_account-dependency, damit lokal testbar.
Wird von hyperliquid_exec.py importiert.
"""
import logging
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("goathub.hl_retry")

# M-10 (2026-06-13): DEDIZIERTER Alert-Executor, entkoppelt vom default
# ThreadPoolExecutor. Problem: blockierende `time.sleep`-Retries (laufen in
# asyncio.to_thread → default-Executor, ~min(32, cpu+4) bzw. auf einer 2-vCPU-VPS
# klein) UND die Discord-Alert-Posts teilten sich denselben Pool. Ein Retry-Sturm
# (HL-429) belegt dann alle default-Worker mit schlafenden Threads → ein
# gleichzeitiger Alert-Storm hungert aus, und Lock-Holder, die auf einen
# to_thread-Slot warten, stallen die Engine. Ein eigener kleiner Pool nur für
# fire-and-forget-Alerts garantiert, dass Alerts immer rausgehen, ohne dem
# Trading-Pfad Threads zu klauen (und umgekehrt). max_workers klein halten —
# Alerts sind I/O-leicht und sollen nie viele Slots binden.
ALERT_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="hl-alert")


def submit_alert(fn, *args, **kwargs):
    """Fire-and-forget einen Alert-Send auf dem dedizierten Alert-Executor
    ausführen (M-10). Schluckt Submit-Fehler (Pool-Shutdown beim Teardown), damit
    ein Alert nie den Caller crasht. Engine._post_alert nutzt das statt
    loop.run_in_executor(None, …), um den default-Pool nicht mit dem Trading-
    Pfad zu teilen."""
    try:
        return ALERT_EXECUTOR.submit(fn, *args, **kwargs)
    except Exception as e:  # z.B. RuntimeError nach Shutdown
        log.warning("submit_alert failed (%s) — running inline", e)
        try:
            fn(*args, **kwargs)
        except Exception as e2:
            log.warning("inline alert failed: %s", e2)
        return None

# Patterns die als "transient" gelten und retried werden sollen.
_TRANSIENT_PATTERNS = (
    "rate limit", "rate_limit", "429", "503", "502", "504",
    "timeout", "timed out", "connection", "temporarily unavailable",
    "service unavailable", "internal server error", "bad gateway",
)

# Patterns die speziell ein Rate-Limit (429) signalisieren — lösen den
# prozessweiten Breaker aus (H-12).
_RATE_LIMIT_PATTERNS = ("rate limit", "rate_limit", "429", "too many requests")

# H-12 (2026-06-13): Prozessweiter Rate-Limit-Breaker. Wenn HL 429t, hämmern
# bisher ALLE Caller (sync, watcher, jeder User-Entry) den schon überlasteten
# Endpoint weiter und verschlimmern den Brownout. Wir merken uns hier EINEN
# prozessweiten "gesperrt bis"-Zeitpunkt (monotone Uhr).
# STAND 2026-06-14 (Review): der State wird von hl_retry bei 429 GESETZT
# (note_rate_limit), aber von handle_signal/sync noch NICHT abgefragt — die
# eigentliche Defer-Logik ist noch nicht verdrahtet (Follow-up). Bis dahin
# schützt nur der Per-Call-Backoff in hl_retry(), KEIN prozessweiter Breaker.
_BACKOFF_CAP = 30.0        # max. Backoff-Sekunden für einen einzelnen 429-Retry
_rate_limited_until = 0.0  # monotonic deadline; 0 = nicht gesperrt
_rate_lock = threading.Lock()


def is_rate_limit_error(err) -> bool:
    """True wenn der Error-String spezifisch nach Rate-Limit (429) aussieht."""
    s = str(err).lower()
    return any(p in s for p in _RATE_LIMIT_PATTERNS)


def note_rate_limit(retry_after: float = None):
    """Prozessweiten Rate-Limit-Breaker setzen: HL ist bis jetzt+retry_after
    gesperrt. retry_after None → konservativer Default (Backoff-Cap). Nur
    verlängern, nie verkürzen (mehrere Threads können parallel 429 sehen)."""
    delay = _BACKOFF_CAP if retry_after is None else max(0.0, float(retry_after))
    deadline = _time.monotonic() + delay
    global _rate_limited_until
    with _rate_lock:
        if deadline > _rate_limited_until:
            _rate_limited_until = deadline


def is_hl_rate_limited() -> bool:
    """True solange der prozessweite Rate-Limit-Breaker aktiv ist. GEDACHT für
    handle_signal/sync, um teure HL-Aktionen aufzuschieben (H-12) — aktuell aber
    noch von KEINEM Aufrufer konsultiert (Verdrahtung ist Follow-up; siehe
    Kommentar oben). Nicht als 'Schutz aktiv' annehmen, bis verdrahtet."""
    with _rate_lock:
        return _time.monotonic() < _rate_limited_until


def hl_rate_limit_remaining() -> float:
    """Restliche Sperr-Sekunden des Breakers (0.0 wenn nicht gesperrt)."""
    with _rate_lock:
        return max(0.0, _rate_limited_until - _time.monotonic())


def _retry_after_from_exc(err) -> float:
    """Versucht ein Retry-After (Sekunden) aus einer HL-ClientError zu lesen.

    SDK ClientError trägt `.header` (requests-Headers der 4xx-Antwort, siehe
    api.py:38/42) — daraus 'Retry-After' (HTTP-Standard: Sekunden oder HTTP-Datum,
    wir lesen nur die Sekunden-Form). None wenn nicht vorhanden/parsebar.
    """
    header = getattr(err, "header", None)
    if header is None:
        return None
    try:
        # requests.structures.CaseInsensitiveDict unterstützt .get
        val = header.get("Retry-After") if hasattr(header, "get") else None
    except Exception:
        return None
    if val is None:
        return None
    try:
        return float(str(val).strip())
    except (TypeError, ValueError):
        return None  # HTTP-Datum-Form ignorieren wir bewusst (selten bei HL)


def is_transient_error(err) -> bool:
    """True wenn der Error-String wie ein transient HL-Fehler aussieht.
    Used for both Exception messages and dict-response error texts.
    """
    s = str(err).lower()
    return any(p in s for p in _TRANSIENT_PATTERNS)


def hl_retry(fn, *args, max_attempts: int = 3, backoff: float = 1.5,
             initial_delay: float = 0.5, label: str = "", **kwargs):
    """Call fn(*args, **kwargs) with retry on transient errors.

    Returns the value if eventually successful. Raises the LAST exception
    if all attempts exhausted. Non-transient errors raise immediately
    (no retry on auth errors, validation errors, etc).

    Args:
        max_attempts: 3 für normale orders, 5 für must-succeed (SL/close/cancel)
        backoff: exponential factor (delay * backoff after each fail)
        initial_delay: seconds before first retry
        label: optional string for log (e.g. "place_protection sl BTC")

    Behaviour:
        - Raises exception → if transient, retry; if not, raise immediately.
        - Returns dict with status="err" → if transient text, retry; if not,
          return as-is (final attempt also returns err-response).
        - Returns success value → return immediately.
        - H-12: bei 429/Rate-Limit wird der prozessweite Breaker (note_rate_limit)
          gesetzt; der Sleep nutzt Retry-After (aus ClientError-Header) falls da,
          sonst exp. Backoff — beides hart auf _BACKOFF_CAP gedeckelt.
    """
    last_exc = None
    delay = initial_delay
    for attempt in range(1, max_attempts + 1):
        try:
            res = fn(*args, **kwargs)
            # HL responses can be dict with status="err" — soft fail
            if isinstance(res, dict) and res.get("status") == "err":
                err_text = str(res.get("response", res))
                if is_transient_error(err_text):
                    last_exc = RuntimeError(f"HL transient: {err_text[:200]}")
                    if is_rate_limit_error(err_text):
                        note_rate_limit()  # kein Header-Objekt im dict-Pfad
                    if attempt < max_attempts:
                        sleep_for = min(delay, _BACKOFF_CAP)
                        log.warning("hl_retry [%s] attempt %d/%d soft-fail: %s — sleeping %.1fs",
                                    label, attempt, max_attempts, err_text[:120], sleep_for)
                        _time.sleep(sleep_for)
                        delay *= backoff
                        continue
                    return res  # return the err-response on final attempt
            return res
        except Exception as e:
            last_exc = e
            if not is_transient_error(e):
                raise  # non-transient: fail fast
            # H-12: 429 → prozessweiten Breaker setzen, Retry-After bevorzugen.
            retry_after = None
            if is_rate_limit_error(e):
                retry_after = _retry_after_from_exc(e)
                note_rate_limit(retry_after)
            if attempt < max_attempts:
                # Retry-After (gedeckelt) hat Vorrang vor exp. Backoff.
                sleep_for = min(retry_after if retry_after is not None else delay, _BACKOFF_CAP)
                log.warning("hl_retry [%s] attempt %d/%d transient: %s — sleeping %.1fs",
                            label, attempt, max_attempts, str(e)[:120], sleep_for)
                _time.sleep(sleep_for)
                delay *= backoff
                continue
    if last_exc is not None:
        raise last_exc
    return None
