"""Hyperliquid-Ausführung pro Nutzer — inkl. Builder-Code (Referral) auf jeder Order.

Auth: AGENT-Key (keine Auszahlung) + MASTER-Adresse. Builder-Code = Michaels
Gebühren-Anteil. Schwere Imports (eth_account/hyperliquid) bleiben hier; das
Modul wird nur geladen, wenn wirklich ausgeführt wird (Listener/Engine).
"""
import logging
import threading
from math import floor, log10

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

log = logging.getLogger("goathub.hl")


class HLOutageError(RuntimeError):
    """Raised when both perps + spot info-calls failed — HL is unreachable.

    Engine-Caller should differentiate this from a legitimate 0-Balance:
    OutageError = retry/alert, return 0 = legitimately skip trade.
    """
    pass


# 2026-06-04 audit-fix (B-#12): Module-level Info-Singleton (read-only) statt
# bei jedem _per_coin_stats/sync.py-Call einen frischen Info() aufzubauen. Der
# Konstruktor macht selbst Network-Calls für meta(), das ist Overhead pro Trade.
# Get-Or-Init thread-safe via simple lock; meta() ist cached innerhalb der
# SDK-Instanz, also TTL-Refresh dort nicht nötig (zur Not Service-Restart).
_info_singletons = {}   # key: is_testnet (bool) -> Info instance
_info_lock = threading.Lock()

def get_info(testnet: bool) -> Info:
    """Returns a process-wide singleton Info() instance for the given network.

    Thread-safe via lock. Use for read-only queries (user_state, meta, fills) —
    NOT for sign-able actions (those need the Exchange object with a wallet).
    """
    with _info_lock:
        info = _info_singletons.get(testnet)
        if info is None:
            url = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL
            info = Info(url, skip_ws=True)
            _info_singletons[testnet] = info
        return info


def coin_of(t):
    return t.split("/")[0].strip().upper()


def round_sig(x, sig=5):
    return 0.0 if not x else round(x, -int(floor(log10(abs(x)))) + (sig - 1))


# 2026-06-08 Mainnet-Hardening A5: HL-Retry-Wrapper.
# hl_retry und is_transient_error sind in app/hl_retry.py (standalone, kein
# eth_account-dep, damit lokal testbar). Hier nur Re-Export für convenience.
from app.hl_retry import hl_retry, is_transient_error  # noqa: F401


def _f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def fee_to_int(fee_str):
    """'0.05%' -> 50 (f:10 == 1bp == 0.01%). Perps-Max 0.1% == 100.

    2026-06-04 audit-fix: vorher hat ein BUILDER_FEE='5%' (häufiger Tippfehler,
    real Perps-Max ist 0.1%) lautlos auf 100 gecappt — Operator dachte 5%
    wären aktiv. Jetzt: pct>0.1 raised ValueError mit klarer Message.
    """
    try:
        pct = float(str(fee_str).replace("%", "").strip())
    except (TypeError, ValueError):
        return 0
    if pct < 0:
        return 0
    if pct > 0.1:
        raise ValueError(
            f"BUILDER_FEE={fee_str!r}: Perps Builder-Fee ist auf 0.1% (=100 bps) "
            f"hard-capped. Setze 0.05% oder 0.1%, nicht {pct}%."
        )
    return int(round(pct * 1000))


class HyperliquidTrader:
    def __init__(self, *, secret_key, account_address, testnet=True, builder=None):
        if Account.from_key(secret_key).address.lower() == account_address.lower():
            raise ValueError("account_address == Agent-Adresse — es muss die MASTER-Adresse sein")
        base = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL
        self.address = account_address
        self.builder = builder            # {"b": addr, "f": int} oder None
        self.exchange = Exchange(Account.from_key(secret_key), base, account_address=account_address)
        self.info = Info(base, skip_ws=True)
        self._sz = {a["name"]: a.get("szDecimals", 2) for a in self.info.meta().get("universe", [])}

    def account_value(self):
        """Handelbares Guthaben = Perps-Equity + Spot-USDC (Unified/Cross-Collateral).

        2026-06-04 audit-fix (B-#9): Vorher hat die Funktion bei BEIDEN Calls
        fehlschlagen einen 0-Sentinel zurückgegeben — der Engine-Caller hat das
        als "kein Geld" interpretiert und Trade silently geskippt. Jetzt: wenn
        BEIDE HL-Calls schlagen fehl (echte Outage), HLOutageError raisen damit
        der Engine-Caller differenzieren kann zwischen "kein Geld" (legitimer
        Skip) und "HL nicht erreichbar" (Retry-Logik kann anschlagen, Activity
        meldet HL-Outage, nicht Insufficient-Funds).
        """
        perps = None
        spot = None
        try:
            perps = _f(self.info.user_state(self.address).get("marginSummary", {}).get("accountValue"))
        except Exception as e:
            log.warning("user_state: %s", e)
        try:
            spot = 0.0
            for b in self.info.spot_user_state(self.address).get("balances", []):
                if b.get("coin") == "USDC":
                    spot = _f(b.get("total"))
                    break
        except Exception as e:
            log.warning("spot_user_state: %s", e)
            spot = None
        if perps is None and spot is None:
            raise HLOutageError("Hyperliquid Info-API unreachable (perps+spot both failed)")
        return (perps or 0.0) + (spot or 0.0)

    def available_margin(self):
        """Handelbare USDC-Margin für eine NEUE Position.

        Hyperliquid UNIFIED-Account (Standard; beide GoatHub-User 2026-06-08):
        Spot- und Perps-USDC teilen EINE Collateral-Basis, der Spot⇄Perps-
        Transfer ist deaktiviert, und der alte Perps-`withdrawable` ist dann
        IMMER 0 (es gibt kein isoliertes Perps-Guthaben mehr). Die echte freie
        Margin steht in spotClearinghouseState.tokenToAvailableAfterMaintenance
        für USDC (token 0) — dieser Wert zieht die Maintenance-Margin offener
        Positionen bereits ab und ist deshalb der richtige Pre-Check-Wert.

        Bug-Historie: Vorher las der Pre-Check nur Perps-`withdrawable`. Bei
        Unified-Accounts ist das 0 → JEDER Trade wurde geskippt, obwohl das
        ganze Kapital handelbar im Konto lag (User 2 $1076, User 4 $8.58, beide
        0 offene Positionen, alte Meldung log ein hardcodiertes '6 positions').

        Wir nehmen max(withdrawable, spot_available), damit es für unified UND
        klassische (getrennte) Accounts stimmt: unified → spot_available greift,
        klassisch-mit-Perps-Guthaben → withdrawable greift.
        """
        wd = 0.0
        try:
            wd = _f(self.info.user_state(self.address).get("withdrawable"))
        except Exception as e:
            log.warning("user_state(withdrawable): %s", e)
        spot_avail = 0.0
        try:
            # tokenToAvailableAfterMaintenance: [[tokenId, amountStr], ...]; token 0 == USDC
            for entry in self.info.spot_user_state(self.address).get("tokenToAvailableAfterMaintenance", []):
                if entry and entry[0] == 0:
                    spot_avail = _f(entry[1])
                    break
        except Exception as e:
            log.warning("spot tokenToAvailableAfterMaintenance: %s", e)
        return max(wd, spot_avail)

    def open_positions_count(self):
        try:
            aps = self.info.user_state(self.address).get("assetPositions", [])
            return sum(1 for p in aps if abs(_f(p.get("position", {}).get("szi"))) > 0)
        except Exception:
            return 0

    def position_size(self, coin):
        coin = coin_of(coin)
        try:
            for p in self.info.user_state(self.address).get("assetPositions", []):
                if p.get("position", {}).get("coin") == coin:
                    return _f(p["position"].get("szi"))
        except Exception:
            pass
        return 0.0

    def is_tradable(self, coin):
        return coin_of(coin) in self._sz

    def _round_sz(self, coin, sz):
        return round(sz, self._sz.get(coin_of(coin), 3))

    @staticmethod
    def _status_ok(raw):
        """True, wenn eine Order-Antwort akzeptiert wurde (kein error-Status)."""
        try:
            if not isinstance(raw, dict) or raw.get("status") != "ok":
                return False
            st = raw["response"]["data"]["statuses"][0]
            return "error" not in st
        except Exception:
            return False

    def set_leverage(self, coin, lev):
        # 2026-06-08 A5: 3 retries auf transient errors. Wenn final fail, return
        # err-dict (Engine cancelt entry sauber, statt naked-place).
        try:
            return hl_retry(
                lambda: self.exchange.update_leverage(int(round(lev)), coin_of(coin), is_cross=True),
                max_attempts=3, label=f"update_leverage {coin}",
            )
        except Exception as e:
            log.warning("update_leverage(%s) final fail: %s", coin, e)
            return {"status": "err", "response": str(e)}

    def _order(self, coin, is_buy, sz, px, otype, reduce_only=False):
        kwargs = {"reduce_only": reduce_only}
        if self.builder:
            kwargs["builder"] = self.builder
        # 2026-06-08 A5: reduce_only orders (SL/TP/close) sind must-succeed → 5 retries.
        # Normale entries: 3 retries reichen (User-Signal kann auch nochmal kommen).
        max_attempts = 5 if reduce_only else 3
        label = f"order {coin} {'reduceOnly' if reduce_only else 'entry'}"
        def _do():
            try:
                return self.exchange.order(coin, is_buy, sz, px, otype, **kwargs)
            except TypeError:
                return self.exchange.order(coin, is_buy, sz, px, otype, reduce_only=reduce_only)
        return hl_retry(_do, max_attempts=max_attempts, label=label)

    def place_entry(self, coin, is_buy, sz, px):
        coin = coin_of(coin)
        sz = self._round_sz(coin, sz)
        try:
            raw = self._order(coin, is_buy, sz, round_sig(px), {"limit": {"tif": "Gtc"}})
        except Exception as e:
            return {"ok": False, "filled": False, "resting_oid": None, "filled_sz": 0.0, "error": str(e)}
        out = {"ok": False, "filled": False, "resting_oid": None, "filled_sz": 0.0, "error": None, "raw": raw}
        if not isinstance(raw, dict) or raw.get("status") != "ok":
            out["error"] = raw.get("response") if isinstance(raw, dict) else str(raw)
            return out
        try:
            st = raw["response"]["data"]["statuses"][0]
        except Exception:
            out["error"] = "unparseable"
            return out
        if "filled" in st:
            out.update(ok=True, filled=True, filled_sz=_f(st["filled"].get("totalSz"), sz))
        elif "resting" in st:
            out.update(ok=True, resting_oid=st["resting"].get("oid"))
        elif "error" in st:
            out["error"] = st["error"]
        return out

    def place_protection(self, coin, is_buy, sz, sl_px, tps, slippage_cap=None):
        """Setze SL + TPs als Schutz-Orders.

        Phase 6+ (2026-06-03, H-8): Slippage-Cap. Vorher passte das `px`-Feld bei
        isMarket=true das gleiche wie triggerPx → HL nutzte default-Slippage und SL
        füllte in dünnen Märkten beliebig schlecht (SOL -30 USDC am 2026-06-03 mit
        7.94 % Slippage trotz SL bei 72.5 → exit 66.74). Jetzt setzen wir explizit
        den Worst-Case-Preis SLIPPAGE_CAP schlechter als triggerPx.

        2026-06-05: Preflight SL/TP-vs-Mark-Validation. HL lehnt Trigger-Orders ab
        die SOFORT feuern würden (z.B. SHORT-SL unter aktuellem Mark = würde sofort
        market-close). Signale aus MEXC-Daten driften vs HL-Testnet ständig in
        diese Situation; auf Mainnet kann das bei Volatilität auch passieren.
        Vorher: jeder UPDATE_TRADE → cancel → reject → close → reopen-cycle (wasteful).
        Jetzt: Preflight prüft Side-vs-Mark; invalid SL → return mit sl_ok=False
        UND skip_reason="sl_would_trigger_now", caller skipped Update statt close.
        Invalid TPs werden einzeln gefiltert + geloggt.
        """
        coin = coin_of(coin)
        sz = self._round_sz(coin, sz)
        res = {"sl": None, "tp": [], "sl_ok": False, "skip_reason": None}
        # cap defaults aus app.config; getattr-Fallback damit das Module ohne config importierbar bleibt
        if slippage_cap is None:
            try:
                from app import config as _cfg
                slippage_cap = float(getattr(_cfg, "SL_SLIPPAGE_CAP", 0.02))
            except Exception:
                slippage_cap = 0.02

        # Preflight: aktuellen Mark holen für SL/TP-Side-Check
        try:
            mark = float(self.info.all_mids().get(coin) or 0)
        except Exception:
            mark = 0
        # is_buy hier ist die ENTRY-Richtung (True=LONG, False=SHORT).
        # Trigger-Side-Regel:
        #   LONG SL  muss UNTER mark sein (close-sell wenn Preis fällt)
        #   SHORT SL muss ÜBER mark sein  (close-buy wenn Preis steigt)
        if mark > 0:
            invalid = (is_buy and sl_px >= mark) or ((not is_buy) and sl_px <= mark)
            if invalid:
                side = "LONG" if is_buy else "SHORT"
                res["skip_reason"] = (
                    f"sl_would_trigger_now: {side} SL={sl_px} vs mark={mark} "
                    f"(LONG needs SL<mark, SHORT needs SL>mark)"
                )
                return res  # sl_ok=False, caller entscheidet (skip statt close)

        # Schutz-Order-Richtung = umgekehrt zur Entry-Position
        is_buy_protection = not is_buy
        # Worst-Case-Preis-Logik:
        #   - Sell-side (closing LONG): worst = unter dem Trigger → cap * (1 - x)
        #   - Buy-side (closing SHORT): worst = über dem Trigger → cap * (1 + x)
        sign = 1 if is_buy_protection else -1
        sl_worst = round_sig(sl_px * (1 + sign * slippage_cap))

        res["sl"] = self._order(coin, is_buy_protection, sz, sl_worst,
                                {"trigger": {"triggerPx": round_sig(sl_px), "isMarket": True, "tpsl": "sl"}},
                                reduce_only=True)
        res["sl_ok"] = self._status_ok(res["sl"])
        for px, frac in (tps or []):
            # Gleicher Side-Check für TPs:
            #   LONG TP  muss ÜBER mark sein  (close-sell wenn Preis steigt)
            #   SHORT TP muss UNTER mark sein (close-buy wenn Preis fällt)
            if mark > 0:
                tp_invalid = (is_buy and px <= mark) or ((not is_buy) and px >= mark)
                if tp_invalid:
                    log.warning("TP %s skipped (would-trigger-now vs mark=%s, is_buy=%s)", px, mark, is_buy)
                    continue
            tp_sz = self._round_sz(coin, sz * frac)
            if tp_sz > 0:
                tp_worst = round_sig(px * (1 + sign * slippage_cap))
                res["tp"].append(self._order(coin, is_buy_protection, tp_sz, tp_worst,
                                 {"trigger": {"triggerPx": round_sig(px), "isMarket": True, "tpsl": "tp"}},
                                 reduce_only=True))
        return res

    def cancel_orders(self, coin):
        """Alle offenen Orders (Entry + SL/TP) für einen Coin canceln. -> Anzahl.
        2026-06-08 A5: jeder cancel mit retry (must-succeed-ish).
        """
        coin = coin_of(coin)
        n = 0
        try:
            orders = hl_retry(lambda: self.info.open_orders(self.address),
                              max_attempts=3, label="open_orders")
            for o in orders or []:
                if o.get("coin") == coin and o.get("oid") is not None:
                    try:
                        hl_retry(lambda: self.exchange.cancel(coin, o["oid"]),
                                 max_attempts=5, label=f"cancel {coin}")
                        n += 1
                    except Exception as e:
                        log.warning("cancel %s oid %s final fail: %s", coin, o.get("oid"), e)
        except Exception as e:
            log.warning("open_orders(%s) final fail: %s", coin, e)
        return n

    def close_position(self, coin, slippage_cap=None):
        """Offene Position per Market schließen (reduce-only, ohne Builder-Code).
        Phase 6+ (2026-06-03, H-8): explizite Slippage-Cap statt HL-Default (8 %).
        """
        coin = coin_of(coin)
        psz = self.position_size(coin)
        if abs(psz) == 0:
            return {"ok": True, "closed": 0.0}
        if slippage_cap is None:
            try:
                from app import config as _cfg
                slippage_cap = float(getattr(_cfg, "SL_SLIPPAGE_CAP", 0.02))
            except Exception:
                slippage_cap = 0.02
        try:
            # market_close akzeptiert slippage-Kwarg im HL-SDK (default 0.05).
            # Falls eine SDK-Version den Kwarg nicht hat, fallback.
            try:
                raw = self.exchange.market_close(coin, slippage=slippage_cap)
            except TypeError:
                raw = self.exchange.market_close(coin)
            ok = isinstance(raw, dict) and raw.get("status") == "ok"
            return {"ok": ok, "closed": abs(psz), "raw": raw}
        except Exception as e:
            log.warning("market_close(%s): %s", coin, e)
            return {"ok": False, "closed": 0.0, "error": str(e)}
