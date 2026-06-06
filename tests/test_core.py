"""Dependency-freie Tests: Kapital-Cap-Sizing + Signal-Parser + bcrypt-Max.
Run:
    PYTHONPATH=. python3 tests/test_core.py
"""
import os
# Phase 4: bcrypt-Max-Test importiert app.auth → app.config → braucht Env.
os.environ.setdefault("JWT_SECRET", "test-only-secret-not-prod-1234567890abcdef")
os.environ.setdefault("ENCRYPTION_KEY", "AlwIc3vOpO5xZ8sxqr1Z5kvr1WnQqJg5MZ-ITZkqTeo=")

from app.sizing import size_trade, auto_leverage
from app.parser import parse_signal


def test_cap_limits_capital():
    # Account 100.000, aber Cap 1.000 -> es zählt nur 1.000
    p = size_trade(account_value=100000, capital_cap=1000, risk_pct=0.01,
                   entry=100, stop_loss=90, leverage=3)
    assert p.effective_balance == 1000, p.effective_balance
    assert abs(p.risk_amount - 10) < 1e-6          # 1% von 1000
    assert abs(p.qty - 1.0) < 1e-6                 # 10 / (100-90)
    print("cap: OK -> effektiv=%.0f risk=%.2f qty=%.4f" % (p.effective_balance, p.risk_amount, p.qty))


def test_no_cap_uses_full():
    p = size_trade(account_value=500, capital_cap=0, risk_pct=0.02,
                   entry=100, stop_loss=95, leverage=3)
    assert p.effective_balance == 500
    print("no-cap: OK -> effektiv=%.0f" % p.effective_balance)


def test_parser():
    embed = {"title": "SHORT DOGE/USDT — NEW_TRADE", "description": "Signal `abc123`",
             "fields": [{"name": "Action", "value": "NEW_TRADE"},
                        {"name": "Direction", "value": "SHORT"},
                        {"name": "Entry", "value": "0.101"},
                        {"name": "Stop Loss", "value": "0.121"},
                        {"name": "Take Profits", "value": "50%@0.058, 50%@0.0477"},
                        {"name": "Confidence", "value": "0.85"}]}
    s = parse_signal(embed)
    assert s and s.ticker == "DOGE/USDT" and s.direction == "SHORT"
    assert s.entry == 0.101 and s.stop_loss == 0.121 and len(s.take_profits) == 2
    assert s.confidence == 0.85 and s.signal_id == "abc123"
    print("parser: OK ->", s.ticker, s.direction, s.entry, [(t.percent, t.price) for t in s.take_profits])


def test_bcrypt_max_length():
    """2026-06-04 Restposten #3: SHA256-pre-hash macht das 72-Byte-Limit
    obsolet. v2-Hashes (mit 'v2:'-Prefix) akzeptieren beliebig lange PWs,
    legacy-Hashes (ohne Prefix) behalten die alte 72-Byte-Semantik bis sie
    auf Login transparent re-hashed werden."""
    from app.auth import hash_pw, verify_pw, needs_rehash, PasswordTooLongError, MAX_PW_BYTES
    import bcrypt

    # v2-Hash: alle Längen funktionieren
    pw_max = "a" * MAX_PW_BYTES
    h = hash_pw(pw_max)
    assert h.startswith("v2:"), f"expected v2-prefix on new hash, got: {h[:5]}"
    assert verify_pw(pw_max, h) is True
    assert needs_rehash(h) is False

    # v2 mit langem (>72b) PW funktioniert (vorher: PasswordTooLongError)
    pw_long = "🔥" * 200          # 800 Bytes UTF-8
    h_long = hash_pw(pw_long)
    assert h_long.startswith("v2:")
    assert verify_pw(pw_long, h_long) is True
    assert verify_pw("wrong", h_long) is False

    # legacy-Hash (ohne Prefix) — simuliere alten DB-Eintrag
    legacy_h = bcrypt.hashpw(b"oldpw", bcrypt.gensalt()).decode()
    assert needs_rehash(legacy_h) is True
    assert verify_pw("oldpw", legacy_h) is True
    assert verify_pw("wrong", legacy_h) is False

    # legacy + >72b PW: bleibt False (kein crash, einfach falsch)
    assert verify_pw("a" * 100, legacy_h) is False

    # verify_pw mit zu langem PW gegen v2-Hash für *anderes* PW: False
    assert verify_pw("y" * 100, h_long) is False

    print("bcrypt-max: OK -> v2 hashes beliebig lang, legacy weiter unterstützt, needs_rehash detect")


def test_auto_leverage():
    """2026-06-06: Bot wählt Hebel aus SL-Distanz × Confidence, gecappt am User-Max.

    Formel: safe_lev = 1 / (sl_dist * liq_safety) ; chosen = safe × max(conf_floor, confidence)
    Default liq_safety=2 → SL liegt auf halbem Weg zur Liquidation.
    """
    # Python's round() uses banker's rounding (round half to even).
    # SL 2% from entry, confidence 0.90, max 50 → safe=25, *0.90=22.5 → 22 (banker)
    lev, reason = auto_leverage(entry=100, stop_loss=98, confidence=0.90, max_cap=50)
    assert lev == 22, f"expected 22x, got {lev} ({reason})"

    # SL 1% from entry, confidence 0.95 → safe=50, *0.95=47.5 → 48 (banker rounds .5 to even)
    lev, _ = auto_leverage(entry=100, stop_loss=99, confidence=0.95, max_cap=50)
    assert lev == 48, f"expected 48x, got {lev}"

    # Very tight SL 0.4% → safe = 125, capped at 50 (even with full conf)
    lev, _ = auto_leverage(entry=100, stop_loss=99.6, confidence=1.0, max_cap=50)
    assert lev == 50, f"expected 50x (capped), got {lev}"

    # Wide SL 10% → safe = 5, conf 0.80 → 4
    lev, _ = auto_leverage(entry=100, stop_loss=90, confidence=0.80, max_cap=50)
    assert lev == 4, f"expected 4x, got {lev}"

    # No confidence → conf_floor (0.5) — SL 2% safe=25 × 0.5 = 12.5 → 12 (banker)
    lev, _ = auto_leverage(entry=100, stop_loss=98, confidence=None, max_cap=50)
    assert lev == 12, f"expected 12x (no conf), got {lev}"

    # User-cap niedriger als safe — z.B. cap 10x für konservativen User, SL 1%, conf 0.95
    # safe=50, *0.95=48 → capped at 10
    lev, _ = auto_leverage(entry=100, stop_loss=99, confidence=0.95, max_cap=10)
    assert lev == 10, f"expected 10x (cap), got {lev}"

    # SHORT direction (entry < stop_loss) — gleiches SL-distance
    lev, _ = auto_leverage(entry=100, stop_loss=102, confidence=0.90, max_cap=50)
    assert lev == 22, f"SHORT setup should equal LONG mirror: expected 22x, got {lev}"

    # Invalid: entry == sl
    try:
        auto_leverage(entry=100, stop_loss=100, confidence=0.9)
        assert False, "expected ValueError"
    except ValueError:
        pass

    print("auto_leverage: OK -> SL-distance×confidence scaling, max_cap honored, floor & errors handled")


if __name__ == "__main__":
    test_cap_limits_capital()
    test_no_cap_uses_full()
    test_parser()
    test_bcrypt_max_length()
    test_auto_leverage()
    print("\nALLE CORE-TESTS BESTANDEN ✅")
