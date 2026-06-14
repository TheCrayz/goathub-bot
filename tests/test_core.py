"""Dependency-freie Tests: Kapital-Cap-Sizing + Signal-Parser + bcrypt-Max.
Run:
    PYTHONPATH=. python3 tests/test_core.py
"""
import os
# Phase 4: bcrypt-Max-Test importiert app.auth → app.config → braucht Env.
# M-14 (2026-06-12): ENCRYPTION_KEY wird zur Laufzeit generiert statt als
# Literal — der alte hardcoded Key war ein VALIDER Fernet-Key im öffentlichen
# Repo (jeder hätte damit verschlüsselte Agent-Keys lesen können, falls er je
# in prod landete).
from cryptography.fernet import Fernet
os.environ.setdefault("JWT_SECRET", "test-only-secret-not-prod-1234567890abcdef")
os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("REGISTRATION_OPEN", "true")  # Tests legen User via /api/register an

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


def test_num_rejects_nonfinite():
    """2026-06-13 H-15: _num akzeptierte 'nan'/'inf' (float() parst sie), aber
    das sind keine echten Levels. NaN ist besonders gefährlich, weil JEDER
    Vergleich (auch `nan < MIN_CONFIDENCE`) False ist → ein Signal mit
    confidence=NaN passierte das Confidence-Gate. Nicht-endliche Werte müssen
    wie ein fehlendes Feld behandelt werden (None)."""
    import math
    from app.parser import _num
    for bad in ("nan", "NaN", "-nan", "inf", "Inf", "+inf", "-inf", "infinity"):
        assert _num(bad) is None, f"_num({bad!r}) should be None, got {_num(bad)!r}"
    # Endliche Werte bleiben unangetastet
    assert _num("0.85") == 0.85
    assert _num("-0.5") == -0.5
    assert _num("0") == 0.0

    # Und der eigentliche Effekt: ein Confidence-NaN trifft das Gate jetzt nicht
    # mehr. Vorher trug die Signal-confidence NaN; das Gate
    # `confidence is not None and confidence < MIN_CONFIDENCE` ließ es durch
    # (beide Teile True bzw. False-aber-not-None). Jetzt parst der Parser NaN zu
    # None → das Gate wird übersprungen (None = "keine Confidence angegeben").
    embed = {"title": "LONG BTC/USDT — NEW_TRADE", "description": "Signal `nanconf`",
             "fields": [{"name": "Action", "value": "NEW_TRADE"},
                        {"name": "Direction", "value": "LONG"},
                        {"name": "Entry", "value": "100"},
                        {"name": "Stop Loss", "value": "90"},
                        {"name": "Confidence", "value": "nan"}]}
    s = parse_signal(embed)
    assert s is not None
    assert s.confidence is None, f"NaN confidence must parse to None, got {s.confidence!r}"
    # Sanity: ein NaN-Entry/SL macht das NEW_TRADE jetzt ungültig (Pflichtfelder).
    embed_bad_entry = dict(embed)
    embed_bad_entry["fields"] = [{"name": "Action", "value": "NEW_TRADE"},
                                 {"name": "Direction", "value": "LONG"},
                                 {"name": "Entry", "value": "inf"},
                                 {"name": "Stop Loss", "value": "90"}]
    assert parse_signal(embed_bad_entry) is None
    print("num-nonfinite: OK -> nan/inf → None, confidence=NaN trifft das Gate nicht mehr")


def test_tp_side_validation():
    """2026-06-13 M-18: TPs gegen entry+direction validieren. Falsch-seitige TPs
    (SHORT-TP über Entry / LONG-TP unter Entry) sind in Wahrheit Stops und
    würden in place_protection still gefiltert → die Position verlöre lautlos
    ihre Gewinnmitnahme. Der Parser splittet sie jetzt nach dropped_tps, damit
    die Engine eine klare Skip-Activity bauen kann."""
    # LONG: TP MUSS über Entry. 110 ist valide, 90 ist falsch-seitig.
    embed_long = {"title": "LONG BTC/USDT — NEW_TRADE", "description": "Signal `tpl`",
                  "fields": [{"name": "Action", "value": "NEW_TRADE"},
                             {"name": "Direction", "value": "LONG"},
                             {"name": "Entry", "value": "100"},
                             {"name": "Stop Loss", "value": "95"},
                             {"name": "Take Profits", "value": "50%@110, 50%@90"}]}
    s = parse_signal(embed_long)
    assert s is not None
    assert len(s.take_profits) == 1 and s.take_profits[0].price == 110, \
        [(t.percent, t.price) for t in s.take_profits]
    assert len(s.dropped_tps) == 1 and s.dropped_tps[0].price == 90

    # SHORT: TP MUSS unter Entry. 90 valide, 110 falsch-seitig.
    embed_short = {"title": "SHORT ETH/USDT — NEW_TRADE", "description": "Signal `tps`",
                   "fields": [{"name": "Action", "value": "NEW_TRADE"},
                              {"name": "Direction", "value": "SHORT"},
                              {"name": "Entry", "value": "100"},
                              {"name": "Stop Loss", "value": "105"},
                              {"name": "Take Profits", "value": "50%@90, 50%@110"}]}
    s = parse_signal(embed_short)
    assert len(s.take_profits) == 1 and s.take_profits[0].price == 90
    assert len(s.dropped_tps) == 1 and s.dropped_tps[0].price == 110

    # Alle TPs korrekt → dropped leer
    embed_ok = {"title": "LONG SOL/USDT — NEW_TRADE", "description": "Signal `tpo`",
                "fields": [{"name": "Action", "value": "NEW_TRADE"},
                           {"name": "Direction", "value": "LONG"},
                           {"name": "Entry", "value": "100"},
                           {"name": "Stop Loss", "value": "95"},
                           {"name": "Take Profits", "value": "50%@110, 50%@120"}]}
    s = parse_signal(embed_ok)
    assert len(s.take_profits) == 2 and s.dropped_tps == []

    # UPDATE_TRADE ohne Entry-Feld → Seite nicht bestimmbar → konservativ ALLE
    # durchreichen (nichts droppen).
    embed_upd = {"title": "ETH update", "description": "Signal `tpu`",
                 "fields": [{"name": "Action", "value": "UPDATE_TRADE"},
                            {"name": "Direction", "value": "LONG"},
                            {"name": "Ticker", "value": "ETH/USDT"},
                            {"name": "Stop Loss", "value": "95"},
                            {"name": "Take Profits", "value": "50%@110, 50%@90"}]}
    s = parse_signal(embed_upd)
    assert s is not None and len(s.take_profits) == 2 and s.dropped_tps == []
    print("tp-side-validation: OK -> falsch-seitige TPs landen in dropped_tps")


def test_title_fallback_action_strict():
    """2026-06-13 M-19: Title-Fallback-Action gehärtet. Vorher löste JEDER Titel,
    dessen letztes '—'-Segment zufällig ein CANCEL-Token war, einen CANCEL mit
    geratenem Ticker aus. Jetzt: explizites action-Feld bevorzugt; Title-Fallback
    nur bei striktem 'TICKER — ACTION'-Schema (genau 2 Segmente) + BEKANNTEM
    Action-Token."""
    # Freitext-Titel, dessen letztes Segment 'close' ist, OHNE action-Feld →
    # früher: CANCEL mit geratenem Ticker. Jetzt: nicht parsebar (None), weil
    # >2 Segmente / kein striktes Schema.
    bad = {"title": "BTC long zone — watch — close to invalidation",
           "description": "Signal `x`", "fields": []}
    assert parse_signal(bad) is None, "mehrdeutiger Freitext darf KEINEN CANCEL raten"

    # Striktes Schema 'TICKER — CANCEL' mit explizitem Direction-Feld → ok.
    good = {"title": "BTC/USDT — CANCEL", "description": "Signal `y`",
            "fields": [{"name": "Direction", "value": "LONG"}]}
    s = parse_signal(good)
    assert s is not None and s.action == "CANCEL" and s.ticker == "BTC/USDT"

    # Explizites action-Feld gewinnt IMMER über den Titel (hier Titel-Segment
    # 'CLOSE', Feld 'NEW_TRADE') → es bleibt NEW_TRADE.
    explicit = {"title": "ETH/USDT — CLOSE", "description": "Signal `z`",
                "fields": [{"name": "Action", "value": "NEW_TRADE"},
                           {"name": "Direction", "value": "LONG"},
                           {"name": "Entry", "value": "100"},
                           {"name": "Stop Loss", "value": "95"}]}
    s = parse_signal(explicit)
    assert s is not None and s.action == "NEW_TRADE"

    # Unbekanntes Schluss-Token im Titel (kein Action-Wort) → keine Action geraten.
    unknown = {"title": "BTC/USDT — invalidation", "description": "Signal `w`", "fields": []}
    assert parse_signal(unknown) is None
    print("title-fallback-action: OK -> nur striktes TICKER—ACTION + bekanntes Token")


def test_builder_fee_validation():
    """2026-06-13 M-22: BUILDER_FEE wird jetzt beim config-Import validiert
    (Boot-Fail bei Garbage), nicht erst lazy beim ersten Builder-Order. Die
    Validierungs-Logik (config._validate_builder_fee) spiegelt
    hyperliquid_exec.fee_to_int."""
    from app.config import _validate_builder_fee
    assert _validate_builder_fee("0.05%") == 50
    assert _validate_builder_fee("0.1%") == 100
    assert _validate_builder_fee("0") == 0
    # Garbage → Boot-Fail (RuntimeError, nicht still 0)
    for bad in ("abc", "", "  ", "x%"):
        try:
            _validate_builder_fee(bad)
            assert False, f"_validate_builder_fee({bad!r}) sollte RuntimeError werfen"
        except RuntimeError:
            pass
    # Zu hoch (häufiger '5%'-Tippfehler, real Perps-Max 0.1%) → Boot-Fail
    try:
        _validate_builder_fee("5%")
        assert False, "5% sollte RuntimeError werfen (über 0.1% Perps-Max)"
    except RuntimeError:
        pass
    # Negativ → Boot-Fail
    try:
        _validate_builder_fee("-0.01%")
        assert False, "negativ sollte RuntimeError werfen"
    except RuntimeError:
        pass
    print("builder-fee-validation: OK -> valide → bps, Garbage/zu-hoch/negativ → RuntimeError")


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
    # 2026-06-13 L-5: auto_leverage FLOORt jetzt (int(chosen)) statt banker's
    # round — der gewählte Hebel bleibt damit immer ≤ safe_lev×conf (nie +0.49
    # darüber). Erwartungen unten entsprechend auf floor angepasst.
    # SL 2% from entry, confidence 0.90, max 50 → safe=25, *0.90=22.5 → 22 (floor)
    lev, reason = auto_leverage(entry=100, stop_loss=98, confidence=0.90, max_cap=50)
    assert lev == 22, f"expected 22x, got {lev} ({reason})"

    # SL 1% from entry, confidence 0.95 → safe=50, *0.95=47.5 → 47 (floor, war 48 banker)
    lev, _ = auto_leverage(entry=100, stop_loss=99, confidence=0.95, max_cap=50)
    assert lev == 47, f"expected 47x (floor), got {lev}"

    # Very tight SL 0.4% → safe = 125, capped at 50 (even with full conf)
    lev, _ = auto_leverage(entry=100, stop_loss=99.6, confidence=1.0, max_cap=50)
    assert lev == 50, f"expected 50x (capped), got {lev}"

    # Wide SL 10% → safe = 5, conf 0.80 → 4
    lev, _ = auto_leverage(entry=100, stop_loss=90, confidence=0.80, max_cap=50)
    assert lev == 4, f"expected 4x, got {lev}"

    # No confidence → conf_floor (0.5) — SL 2% safe=25 × 0.5 = 12.5 → 12 (floor)
    lev, _ = auto_leverage(entry=100, stop_loss=98, confidence=None, max_cap=50)
    assert lev == 12, f"expected 12x (no conf), got {lev}"

    # User-cap niedriger als safe — z.B. cap 10x für konservativen User, SL 1%, conf 0.95
    # safe=50, *0.95=47.5 → floor 47 → capped at 10
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


# ── API-Tests (2026-06-12 #12/#13/#21): Settings-Validierung + reserved email ─
# TestClient OHNE Context-Manager → lifespan (Listener/Sync-Loops) läuft NICHT,
# wir testen rein die Request-Validierung. current_user wird mit einem
# In-Memory-User überschrieben; die 422-Pfade erreichen die DB nie.

def _api_client_with_fake_user():
    from fastapi.testclient import TestClient
    from app import auth
    from app.main import app
    from app.models import User

    fake = User(
        id=1, email="tester@example.com", password_hash="x",
        hl_account_address="", hl_api_secret_enc="",
        risk_pct=0.005, leverage=20, max_open_positions=5, capital_cap_usdc=0,
        bot_active=True, builder_approved=False, token_version=0, is_admin=False,
        max_drawdown_pct=0.30, peak_account_value=0.0,
    )

    class _StubDB:
        """update_settings ruft nur db.commit() — Rejects erreichen die DB nie."""
        def commit(self):
            pass

    from app.db import get_db
    app.dependency_overrides[auth.current_user] = lambda: fake
    app.dependency_overrides[get_db] = lambda: _StubDB()
    return TestClient(app), app


def test_settings_validation_rejects():
    """#12/#13: out-of-range + explizite null-Werte → 422 statt Silent-Clamp."""
    client, app = _api_client_with_fake_user()
    try:
        bad_payloads = [
            {"risk_pct": 1.0},              # User meinte "1 %" — FRACTION wäre 0.01
            {"risk_pct": 0},                # (0, 0.05] — 0 ist raus
            {"risk_pct": 0.051},            # über 5 %/Trade
            {"leverage": 0},                # [1, 50]
            {"leverage": 51},
            {"max_open_positions": 0},      # [1, 20]
            {"max_open_positions": 21},
            {"capital_cap_usdc": -1},       # [0, 10M]
            {"capital_cap_usdc": 10_000_001},
            {"max_drawdown_pct": 0.96},     # [0, 0.95]
            {"risk_pct": None},             # explizites null (#13: war vorher 0-coerce)
            {"capital_cap_usdc": None},     # geleertes Cap-Feld darf NICHT uncappen
        ]
        for payload in bad_payloads:
            r = client.put("/api/settings", json=payload)
            assert r.status_code == 422, f"{payload} → {r.status_code} (expected 422): {r.text[:200]}"

        # Gültige Werte (inkl. Grenzen) + bot_active-only-Toggle gehen weiter durch.
        r = client.put("/api/settings", json={"risk_pct": 0.05, "leverage": 1,
                                              "max_open_positions": 20, "capital_cap_usdc": 0})
        assert r.status_code == 200, r.text[:200]
        assert r.json()["settings"]["risk_pct"] == 0.05   # FRACTION bleibt FRACTION
        r = client.put("/api/settings", json={"bot_active": False})
        assert r.status_code == 200, r.text[:200]
        assert r.json()["bot_active"] is False
        print("settings-validation: OK -> 12 Rejects (422), Grenzen + bot_active-Toggle gehen durch")
    finally:
        app.dependency_overrides.clear()


def test_register_reserved_email_blocked():
    """#21: discord_<id>@goathub.internal ist der synthetische OAuth-Namespace —
    Registrierung damit wäre ein gezielter Signup-DoS gegen Discord-Logins."""
    client, app = _api_client_with_fake_user()
    try:
        r = client.post("/api/register", json={
            "email": "discord_123456789@goathub.internal", "password": "secret123"})
        assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text[:200]}"
        assert "reserved" in r.json()["detail"].lower()
        print("register-reserved-email: OK -> @goathub.internal wird mit 400 abgelehnt")
    finally:
        app.dependency_overrides.clear()


if __name__ == "__main__":
    test_cap_limits_capital()
    test_no_cap_uses_full()
    test_parser()
    test_num_rejects_nonfinite()
    test_tp_side_validation()
    test_title_fallback_action_strict()
    test_builder_fee_validation()
    test_bcrypt_max_length()
    test_auto_leverage()
    test_settings_validation_rejects()
    test_register_reserved_email_blocked()
    print("\nALLE CORE-TESTS BESTANDEN ✅")
