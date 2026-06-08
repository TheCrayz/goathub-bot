"""HL-API Retry-Wrapper (2026-06-08 Mainnet-Hardening A5).

Standalone module ohne eth_account-dependency, damit lokal testbar.
Wird von hyperliquid_exec.py importiert.
"""
import logging
import time as _time

log = logging.getLogger("goathub.hl_retry")

# Patterns die als "transient" gelten und retried werden sollen.
_TRANSIENT_PATTERNS = (
    "rate limit", "rate_limit", "429", "503", "502", "504",
    "timeout", "timed out", "connection", "temporarily unavailable",
    "service unavailable", "internal server error", "bad gateway",
)


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
                    if attempt < max_attempts:
                        log.warning("hl_retry [%s] attempt %d/%d soft-fail: %s — sleeping %.1fs",
                                    label, attempt, max_attempts, err_text[:120], delay)
                        _time.sleep(delay)
                        delay *= backoff
                        continue
                    return res  # return the err-response on final attempt
            return res
        except Exception as e:
            last_exc = e
            if not is_transient_error(e):
                raise  # non-transient: fail fast
            if attempt < max_attempts:
                log.warning("hl_retry [%s] attempt %d/%d transient: %s — sleeping %.1fs",
                            label, attempt, max_attempts, str(e)[:120], delay)
                _time.sleep(delay)
                delay *= backoff
                continue
    if last_exc is not None:
        raise last_exc
    return None
