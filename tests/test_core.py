"""Dependency-freie Tests: Kapital-Cap-Sizing + Signal-Parser + bcrypt-Max.
Run:
    PYTHONPATH=. python3 tests/test_core.py
"""
import os
# Phase 4: bcrypt-Max-Test importiert app.auth → app.config → braucht Env.
os.environ.setdefault("JWT_SECRET", "test-only-secret-not-prod-1234567890abcdef")
os.environ.setdefault("ENCRYPTION_KEY", "AlwIc3vOpO5xZ8sxqr1Z5kvr1WnQqJg5MZ-ITZkqTeo=")

from app.sizing import size_trade
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
    """Phase 4: 72-Byte-Collision-Schutz. >72 Bytes muss abgelehnt werden,
    72 Bytes selbst muss funktionieren (Grenzwert)."""
    from app.auth import hash_pw, verify_pw, PasswordTooLongError, MAX_PW_BYTES

    # 72 ASCII Bytes — am Limit, muss funktionieren
    pw_max = "a" * MAX_PW_BYTES
    h = hash_pw(pw_max)
    assert h and len(h) > 50
    assert verify_pw(pw_max, h) is True

    # 73 Bytes — über dem Limit, muss ablehnen
    try:
        hash_pw("a" * (MAX_PW_BYTES + 1))
        assert False, "expected PasswordTooLongError on 73-byte input"
    except PasswordTooLongError:
        pass

    # UTF-8 multibyte: 36 emoji-Zeichen × 4 Bytes = 144 Bytes — auch ablehnen
    try:
        hash_pw("🔥" * 36)
        assert False, "expected PasswordTooLongError on multi-byte UTF-8 input"
    except PasswordTooLongError:
        pass

    # verify_pw mit zu langem PW: muss False zurückgeben (kein crash)
    assert verify_pw("a" * (MAX_PW_BYTES + 1), h) is False

    print("bcrypt-max: OK -> 72-byte hashes, >72 raises, multibyte counted in bytes")


if __name__ == "__main__":
    test_cap_limits_capital()
    test_no_cap_uses_full()
    test_parser()
    test_bcrypt_max_length()
    print("\nALLE CORE-TESTS BESTANDEN ✅")
