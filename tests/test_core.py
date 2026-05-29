"""Dependency-freie Tests: Kapital-Cap-Sizing + Signal-Parser. Run:
    PYTHONPATH=. python3 tests/test_core.py
"""
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


if __name__ == "__main__":
    test_cap_limits_capital()
    test_no_cap_uses_full()
    test_parser()
    print("\nALLE CORE-TESTS BESTANDEN ✅")
