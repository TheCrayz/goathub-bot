"""Hyperliquid-Ausführung pro Nutzer — inkl. Builder-Code (Referral) auf jeder Order.

Auth: AGENT-Key (keine Auszahlung) + MASTER-Adresse. Builder-Code = Michaels
Gebühren-Anteil. Schwere Imports (eth_account/hyperliquid) bleiben hier; das
Modul wird nur geladen, wenn wirklich ausgeführt wird (Listener/Engine).
"""
import logging
from math import floor, log10

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

log = logging.getLogger("goathub.hl")


def coin_of(t):
    return t.split("/")[0].strip().upper()


def round_sig(x, sig=5):
    return 0.0 if not x else round(x, -int(floor(log10(abs(x)))) + (sig - 1))


def _f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def fee_to_int(fee_str):
    """'0.05%' -> 50 (f:10 == 1bp == 0.01%). Perps-Max 0.1% == 100."""
    try:
        pct = float(str(fee_str).replace("%", "").strip())
        return max(0, min(100, int(round(pct * 1000))))
    except (TypeError, ValueError):
        return 0


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
        """Handelbares Guthaben = Perps-Equity + Spot-USDC (Unified/Cross-Collateral)."""
        perps = spot = 0.0
        try:
            perps = _f(self.info.user_state(self.address).get("marginSummary", {}).get("accountValue"))
        except Exception as e:
            log.warning("user_state: %s", e)
        try:
            for b in self.info.spot_user_state(self.address).get("balances", []):
                if b.get("coin") == "USDC":
                    spot = _f(b.get("total"))
                    break
        except Exception as e:
            log.warning("spot_user_state: %s", e)
        return perps + spot

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

    def _round_sz(self, coin, sz):
        return round(sz, self._sz.get(coin_of(coin), 3))

    def set_leverage(self, coin, lev):
        try:
            return self.exchange.update_leverage(int(round(lev)), coin_of(coin), is_cross=True)
        except Exception as e:
            log.warning("update_leverage(%s): %s", coin, e)
            return {"status": "err", "response": str(e)}

    def _order(self, coin, is_buy, sz, px, otype, reduce_only=False):
        kwargs = {"reduce_only": reduce_only}
        if self.builder:
            kwargs["builder"] = self.builder
        try:
            return self.exchange.order(coin, is_buy, sz, px, otype, **kwargs)
        except TypeError:
            # SDK ohne builder-Param -> ohne Builder erneut
            return self.exchange.order(coin, is_buy, sz, px, otype, reduce_only=reduce_only)

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

    def place_protection(self, coin, is_buy, sz, sl_px, tps):
        coin = coin_of(coin)
        sz = self._round_sz(coin, sz)
        res = {"sl": None, "tp": []}
        res["sl"] = self._order(coin, not is_buy, sz, round_sig(sl_px),
                                {"trigger": {"triggerPx": round_sig(sl_px), "isMarket": True, "tpsl": "sl"}},
                                reduce_only=True)
        for px, frac in (tps or []):
            tp_sz = self._round_sz(coin, sz * frac)
            if tp_sz > 0:
                res["tp"].append(self._order(coin, not is_buy, tp_sz, round_sig(px),
                                 {"trigger": {"triggerPx": round_sig(px), "isMarket": True, "tpsl": "tp"}},
                                 reduce_only=True))
        return res
