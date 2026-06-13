"""Hyperliquid-Ausführung pro Nutzer — inkl. Builder-Code (Referral) auf jeder Order.

Auth: AGENT-Key (keine Auszahlung) + MASTER-Adresse. Builder-Code = Michaels
Gebühren-Anteil. Schwere Imports (eth_account/hyperliquid) bleiben hier; das
Modul wird nur geladen, wenn wirklich ausgeführt wird (Listener/Engine).
"""
import logging
import threading
from math import floor, log10

import os
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from hyperliquid.utils.types import Cloid

log = logging.getLogger("goathub.hl")

# C-3 (2026-06-13): HTTP-Timeout (Sekunden) für ALLE HL-Calls — Info() + Exchange().
# Param existiert in SDK 0.23.0 (api.py: session.post(..., timeout=self.timeout)).
HL_HTTP_TIMEOUT = 10


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

# M-17 (2026-06-13): prozessweiter meta()-Cache pro Netz. meta() macht bei JEDEM
# Call einen POST (das SDK cached es NICHT, nur die bei __init__ übergebene
# universe). Vorher rief der HyperliquidTrader-Ctor meta() bei JEDEM
# _build_trader OHNE Retry → ein HL-Blip dort raiste und sah im Engine aus wie
# ein User-Key-Fehler (per-User error-Activity, "agent does not exist"-Klasse).
# Jetzt: meta einmal pro Netz holen (via hl_retry), cachen, teilen. Refresh
# nur bei Service-Restart — die szDecimals/maxLeverage-Tabelle ändert sich
# extrem selten (neuer Coin gelistet); zur Not Restart.
_meta_cache = {}        # key: is_testnet (bool) -> meta-dict
_meta_lock = threading.Lock()

def get_meta(testnet: bool) -> dict:
    """Prozessweit gecachtes Perp-meta() für das Netz (M-17). Erstbefüllung via
    hl_retry über das get_info()-Singleton, danach In-Memory geteilt. Raised nur,
    wenn der allererste Read auch nach Retries scheitert (dann hat der Caller
    ohnehin keine szDecimals/maxLeverage und MUSS abbrechen)."""
    with _meta_lock:
        meta = _meta_cache.get(testnet)
        if meta is not None:
            return meta
    info = get_info(testnet)
    meta = hl_retry(lambda: info.meta(), max_attempts=3, label="meta")
    with _meta_lock:
        # Doppelte Befüllung durch parallele Erst-Caller ist harmlos (idempotent).
        _meta_cache[testnet] = meta
    return meta

def get_info(testnet: bool) -> Info:
    """Returns a process-wide singleton Info() instance for the given network.

    Thread-safe via lock. Use for read-only queries (user_state, meta, fills) —
    NOT for sign-able actions (those need the Exchange object with a wallet).
    """
    with _info_lock:
        info = _info_singletons.get(testnet)
        if info is None:
            url = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL
            # C-3 (2026-06-13): HTTP-Timeout. Ohne timeout= blockt requests
            # unbegrenzt auf einer hängenden Verbindung → hl_retry greift NIE,
            # der Thread friert unter Locks ein. Mit Timeout wird ein Hänger zu
            # einer Exception, die retry/Notfall sehen.
            info = Info(url, skip_ws=True, timeout=HL_HTTP_TIMEOUT)
            _info_singletons[testnet] = info
        return info


def coin_of(t):
    return t.split("/")[0].strip().upper()


def round_sig(x, sig=5):
    return 0.0 if not x else round(x, -int(floor(log10(abs(x)))) + (sig - 1))


# 2026-06-08 Mainnet-Hardening A5: HL-Retry-Wrapper.
# hl_retry und is_transient_error sind in app/hl_retry.py (standalone, kein
# eth_account-dep, damit lokal testbar). Hier nur Re-Export für convenience.
from app.hl_retry import (  # noqa: F401
    hl_retry,
    is_transient_error,
    is_hl_rate_limited,
    note_rate_limit,
    submit_alert,
)


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
        # C-3 (2026-06-13): timeout= an BEIDE Clients. Ein hängender Socket im
        # place_protection NACH gefülltem Entry hieße sonst: ungeschützte Position,
        # unbegrenzt, ohne Alert (der Thread friert unter dem Lock ein statt zu erroren).
        self.exchange = Exchange(Account.from_key(secret_key), base,
                                 account_address=account_address, timeout=HL_HTTP_TIMEOUT)
        self.info = Info(base, skip_ws=True, timeout=HL_HTTP_TIMEOUT)
        # M-17 (2026-06-13): meta aus dem prozessweiten, retry-gewrappten Cache
        # statt self.info.meta() bei JEDEM _build_trader (POST ohne Retry → ein
        # HL-Blip sah aus wie ein User-Key-Fehler). get_meta teilt EINEN Read
        # pro Netz über alle Trader; ein transienter Blip wird hier abgefangen.
        meta = get_meta(testnet)
        self._sz = {a["name"]: a.get("szDecimals", 2) for a in meta.get("universe", [])}
        # 2026-06-12 (Review #17): per-Asset maxLeverage aus meta merken. Viele Alts
        # cappen bei 3-10x — auto_leverage darf nie mehr anfragen, sonst rejected HL
        # update_leverage non-transient und der Entry liefe mit altem/undefiniertem Hebel.
        self._max_lev = {}
        for a in meta.get("universe", []):
            try:
                self._max_lev[a["name"]] = int(a.get("maxLeverage") or 0)
            except (TypeError, ValueError):
                pass

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
                    # 2026-06-08 Unified-Account-Fix: NUR die FREIE Spot-USDC
                    # addieren (total − hold), nicht die volle. Bei einem Unified-
                    # Account ist `hold` die als Perps-Margin reservierte USDC —
                    # die steckt bereits in marginSummary.accountValue (=perps).
                    # Volles `total` zu addieren doppelzählte die Margin, sobald
                    # Positionen offen waren → balance ~$1582 statt HL-Equity
                    # ~$1083 → Sizing ~46% zu groß. (total−hold) + perps_AV trifft
                    # HLs autoritativen accountValue und stimmt auch für klassische
                    # Accounts (dort hold=0 → unverändert perps + voller Spot).
                    spot = _f(b.get("total")) - _f(b.get("hold"))
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
        # 2026-06-09 KORREKTUR: freie Spot-USDC = total - hold (hold = bereits als
        # Initial-Margin offener Positionen gebundene USDC). Das ist HLs "available
        # to trade". Vorher tokenToAvailableAfterMaintenance — das zieht nur die
        # MAINTENANCE-Margin ab (Liquidations-Puffer) und ÜBERSCHÄTZT, was für
        # einen NEUEN Entry (Initial-Margin) frei ist, sobald Positionen offen
        # sind (zeigte $3291 statt echter ~$1939). Idle (0 Positionen, hold=0)
        # => total, also identisch zum Unified-Fix für den leeren Fall.
        free_spot = 0.0
        try:
            for b in self.info.spot_user_state(self.address).get("balances", []):
                if b.get("coin") == "USDC":
                    free_spot = _f(b.get("total")) - _f(b.get("hold"))
                    break
        except Exception as e:
            log.warning("spot free USDC: %s", e)
        return max(wd, free_spot)

    def open_positions_count(self):
        """Anzahl offener Perps-Positionen (|szi|>0).

        M-2 (2026-06-13): vorher gab ein Read-Fehler hier 0 zurück — der
        max_open-Gate im Engine sah dann "0 Positionen offen" während eines
        API-Blips und ließ JEDEN Entry durch (fail-OPEN; mit M-1 sind dann
        BEIDE Portfolio-Caps gleichzeitig aus). Jetzt: via hl_retry (3 Versuche)
        lesen; nach finalem Fail die Exception DURCHREICHEN. Der Engine-Caller
        behandelt den Raise fail-closed (Entry abbrechen), statt blind 0 zu
        nehmen. Read ist nur ein Gate-Zähler, kein risk-reduzierender Pfad —
        bei Unsicherheit ist Abbruch die sichere Wahl."""
        state = hl_retry(lambda: self.info.user_state(self.address),
                         max_attempts=3, label="open_positions_count")
        aps = (state or {}).get("assetPositions", [])
        return sum(1 for p in aps if abs(_f(p.get("position", {}).get("szi"))) > 0)

    def open_positions(self):
        """H1: offene Perps-Positionen als [{'coin', 'szi'}], szi signiert."""
        out = []
        try:
            for p in self.info.user_state(self.address).get("assetPositions", []):
                pos = p.get("position", {})
                szi = _f(pos.get("szi"))
                if abs(szi) > 0:
                    out.append({"coin": pos.get("coin"), "szi": szi})
        except Exception as e:
            log.warning("open_positions: %s", e)
        return out

    # M-6 (2026-06-13): Zähler aufeinanderfolgender Read-Fehler PRO Coin für
    # covered_stop_size. Ab _COVERED_READ_ALERT_AT Fehlern in Folge ist der
    # Coverage-Check für diesen Coin effektiv blind (return inf maskiert
    # Unter-Deckung) → wir loggen ERROR (Alert-Webhook im Engine hängt am
    # error-Log-Pfad). Erfolg setzt den Zähler zurück.
    _COVERED_READ_ALERT_AT = 3

    def covered_stop_size(self, coin, position_is_long=None):
        """H1/H2 (2026-06-09): Summe der Größen der reduce-only STOP-Orders für
        `coin`, die die LIVE-Position decken = wie viel durch Stop-Loss(e)
        abgedeckt ist. Der Reconciler vergleicht das mit der Positionsgröße, um
        UNTER-gedeckte Positionen zu erkennen (Partial-Fill-Bug: SL deckte nur den
        ersten Mini-Fill — BTC 2026-06-09: 0.00138 von 0.1151). TP zählt NICHT als
        Schutz.

        M-6 (2026-06-13): zwei Härtungen.
          (1) SIDE-AWARE: `position_is_long` (True=LONG, False=SHORT, None=alt
              wie bisher). Ein Stop, der eine LONG-Position schützt, ist ein
              reduce-only SELL (side 'A'); ein SHORT-Schutz ist ein BUY
              (side 'B'). Vorher zählte JEDER reduce-only Stop des Coins — auch
              manuelle oder falsch-seitige (z.B. ein Stop aus einer früheren
              Gegenposition). Das überschätzte die Deckung → echte Unter-Deckung
              blieb unentdeckt. Caller (reconciler) kennt die Richtung aus szi.
              Trigger-Side-Sanity zusätzlich: LONG-Schutz triggert UNTER dem
              Markt (sl), SHORT-Schutz darüber — wir nutzen primär das side-Feld.
          (2) READ-FAIL-ESCALATION: ein einzelner Read-Fehler bleibt fail-SAFE
              (return inf = 'als gedeckt annehmen', kein fälschliches Nachlegen),
              ABER wiederholte Fehler (≥_COVERED_READ_ALERT_AT in Folge) heißt:
              der Coverage-Check ist BLIND und maskiert evtl. eine nackte
              Position. Dann ERROR-Log (Alert-Webhook hängt im Engine am
              error-Pfad) statt still inf.
        """
        coin = coin_of(coin)
        try:
            orders = hl_retry(lambda: self.info.frontend_open_orders(self.address),
                              max_attempts=3, label=f"covered_stop_size {coin}") or []
        except Exception as e:
            # Fehlerzähler pro Coin hochzählen; ab Schwelle ESKALIEREN (ERROR).
            if not hasattr(self, "_covered_read_fails"):
                self._covered_read_fails = {}
            n = self._covered_read_fails.get(coin, 0) + 1
            self._covered_read_fails[coin] = n
            if n >= self._COVERED_READ_ALERT_AT:
                log.error("covered_stop_size(%s): open-orders read failed %dx in a row — "
                          "coverage check BLIND for this coin, under-coverage may be masked: %s",
                          coin, n, e)
            else:
                log.warning("covered_stop_size(%s): %s", coin, e)
            return float("inf")
        # Erfolg → Fehlerzähler für diesen Coin zurücksetzen.
        if getattr(self, "_covered_read_fails", None):
            self._covered_read_fails.pop(coin, None)
        # Welche close-Side deckt die Position? LONG → reduce-only SELL (side 'A'),
        # SHORT → reduce-only BUY (side 'B'). None = alte Semantik (jede Side).
        want_side = None
        if position_is_long is True:
            want_side = "A"
        elif position_is_long is False:
            want_side = "B"
        total = 0.0
        for o in orders:
            if coin_of(o.get("coin")) != coin:
                continue
            ot = str(o.get("orderType", "")).lower()
            if not (o.get("reduceOnly") and "stop" in ot):
                continue
            if want_side is not None and str(o.get("side", "")).upper() != want_side:
                # falsch-seitiger / manueller Stop → deckt diese Position nicht
                continue
            total += _f(o.get("sz"))
        return total

    def position_size(self, coin):
        """Signierte Positionsgröße für `coin` (0.0 = wirklich flat).

        2026-06-12 (Review #0, KRITISCH): vorher hat ein except hier 0.0
        zurückgegeben — ein einziger transienter Info-API-Fehler ließ die Engine
        eine OFFENE Position für flat halten: cancel_orders riss die live SL/TP
        weg, _open_new legte eine ZWEITE Position obendrauf, _cancel/_watcher
        schlossen die DB-Row → nackte Position ohne Schutz. Jetzt: user_state
        via hl_retry (3 Versuche), nach finalem Fail wird die Exception
        durchgereicht. JEDER Engine-Caller muss "Positionsstatus unbekannt"
        als sicheren Abbruch behandeln — NIEMALS als flat.
        """
        coin = coin_of(coin)
        state = hl_retry(lambda: self.info.user_state(self.address),
                         max_attempts=3, label=f"position_size {coin}")
        for p in (state or {}).get("assetPositions", []):
            if p.get("position", {}).get("coin") == coin:
                return _f(p["position"].get("szi"))
        return 0.0

    def max_leverage(self, coin):
        """2026-06-12 (Review #17): Asset-Max-Hebel aus meta. 0 = unbekannt
        (Caller clampt dann nicht)."""
        return self._max_lev.get(coin_of(coin), 0)

    def is_tradable(self, coin):
        return coin_of(coin) in self._sz

    def _round_sz(self, coin, sz):
        """Größe auf szDecimals ABRUNDEN (floor), nicht round-half-even (H-9).

        round() rundet bei .5 auf — bei szDecimals=0-Coins bis zu +0.5 Einheiten,
        was realisiertes Risiko UND Margin über risk_pct/den Pre-Check-Schätzwert
        treibt (eff×leverage-Cap lautlos gerissen). Floor garantiert sz ≤ angefragt.

        Hinweis: vor dem Floor auf (decimals+4) gerundet, sonst frisst die Float-
        Repräsentation knapp die letzte gültige Stelle (z.B. 0.3*10 = 2.9999996 →
        floor 2 statt 3). min-notional-Recheck auf der gerundeten Zahl macht Agent B.
        """
        sz = _f(sz)
        if sz <= 0:
            return 0.0
        dec = self._sz.get(coin_of(coin), 3)
        scale = 10 ** dec
        return floor(round(sz * scale, 4)) / scale

    def _round_px(self, coin, px):
        """HL-konforme PREIS-Rundung (Audit H-3, 2026-06-12). HL verlangt max 5
        signifikante Stellen UND max (6 − szDecimals) Dezimalstellen (Perps).
        `round_sig` allein (nur 5 sig-figs) erzeugte bei low-price-Coins (szDec=3,
        Preis < ~31 → z.B. ALGO/DOGE/HBAR/SUI/ADA) ungültige Dezimalstellen → HL-
        Reject ('tick size'/'divisible') → SL abgelehnt → Force-Close der frisch
        gefüllten Position. Diese Funktion erzwingt BEIDE Regeln."""
        px = _f(px)
        if px <= 0:
            return px
        # L-4 (2026-06-13): über 100k sind ganzzahlige Preise legal — HLs
        # 5-sig-fig-Regel erzwingt sonst eine Rundung auf den 10er (z.B. BTC-SL
        # 104_237 → 104_240), die den SL bis zu ±5 verschiebt. Bei px≥1e5 also
        # auf Integer runden (max-Dezimalstellen sind dort ohnehin 0).
        if px >= 1e5:
            return float(round(px))
        max_dec = max(0, 6 - self._sz.get(coin_of(coin), 3))
        return round(float(f"{px:.5g}"), max_dec)

    def cancel_order(self, coin, oid):
        """Eine einzelne Order per oid canceln (Audit H-1: Watcher cancelt den
        ruhenden Entry-Rest beim Beenden, OHNE die Schutz-Orders mit-zu-canceln).
        Schluckt Fehler (z.B. Order schon gefüllt/weg) — best effort."""
        if oid is None:
            return False
        coin = coin_of(coin)
        try:
            hl_retry(lambda: self.exchange.cancel(coin, int(oid)), max_attempts=2, label=f"cancel_oid {coin}")
            return True
        except Exception as e:
            log.debug("cancel_order(%s, %s): %s", coin, oid, e)
            return False

    def cancel_order_oid(self, coin, oid):
        """Schnittstelle für Agent B/C — cancelt GENAU EINE Order per oid und
        parst statuses (H-6). Anders als cancel_order (best-effort bool) liefert
        das hier auseinander, OB wirklich gecancelt wurde oder ob die Order schon
        gefüllt war (dann lebt die Position → Caller muss in den Schutzpfad).

        Returns:
            {"ok": bool, "already_filled": bool, "raw": res}
            ok=True  → Order ist jetzt weg (frisch gecancelt ODER schon gecancelt).
            already_filled=True → Order war bereits GEFÜLLT (Position lebt!).
            Bei Exception: {"ok": False, "already_filled": False, "error": str(e)}.
        """
        coin = coin_of(coin)
        if oid is None:
            return {"ok": False, "already_filled": False, "error": "no oid"}
        try:
            res = hl_retry(lambda: self.exchange.cancel(coin, int(oid)),
                           max_attempts=3, label=f"cancel_order_oid {coin}")
        except Exception as e:
            log.warning("cancel_order_oid(%s, %s): %s", coin, oid, e)
            return {"ok": False, "already_filled": False, "error": str(e)}
        st = self._first_status(res)
        # statuses[0] == "success" (String) → sauber gecancelt.
        if st == "success":
            return {"ok": True, "already_filled": False, "raw": res}
        if isinstance(st, dict) and "error" in st:
            filled = self._cancel_already_filled(st)
            done = self._is_already_done_cancel(st)
            # schon gefüllt ODER schon gecancelt = Order ist weg → ok=True,
            # aber already_filled flaggt den gefährlichen Fall.
            return {"ok": done, "already_filled": filled, "raw": res}
        # Unerwartete Form: konservativ ok an _status_ok hängen.
        return {"ok": self._status_ok(res), "already_filled": False, "raw": res}

    def order_status(self, oid=None, cloid=None):
        """Schnittstelle für Agent B/C — Order-Status per oid ODER cloid abfragen.

        Erlaubt dem Aufrufer, einen ambigen/timeout-verlorenen Order-Ausgang
        aufzulösen (C-2), bevor er FAILED klassifiziert. Nutzt info.query_order_by_cloid
        bzw. query_order_by_oid (info.py: type=orderStatus). HL liefert
        {"status":"order","order":{"order":{...},"status":"filled"|"open"|...}}
        oder {"status":"unknownOid"}.

        Returns:
            {"status": "filled"|"open"|"partial"|"canceled"|"unknown",
             "filled_sz": float, "raw": raw}
            Bei Exception: {"status":"unknown","filled_sz":0.0,"error":str(e)}.

        # Testnet-Verifikation ausstehend: exakte orderStatus-Response-Form/Felder
        """
        try:
            if cloid is not None:
                cl = cloid if isinstance(cloid, Cloid) else Cloid.from_str(str(cloid))
                raw = hl_retry(lambda: self.info.query_order_by_cloid(self.address, cl),
                               max_attempts=3, label="order_status cloid")
            elif oid is not None:
                raw = hl_retry(lambda: self.info.query_order_by_oid(self.address, int(oid)),
                               max_attempts=3, label="order_status oid")
            else:
                return {"status": "unknown", "filled_sz": 0.0, "error": "no oid/cloid"}
        except Exception as e:
            log.warning("order_status(oid=%s, cloid=%s): %s", oid, cloid, e)
            return {"status": "unknown", "filled_sz": 0.0, "error": str(e)}
        return self._parse_order_status(raw)

    @staticmethod
    def _parse_order_status(raw):
        """orderStatus-Response → normalisiertes {status, filled_sz, raw}.

        HL-Form (defensiv, jedes Feld kann fehlen):
            {"status":"order","order":{
                "order":{"sz": <rest>, "origSz": <urspr.>, "coin":..., ...},
                "status":"filled"|"open"|"canceled"|"triggered"|"rejected"|...}}
        filled_sz = origSz - sz (rest). Bei "unknownOid"/sonst → unknown/0.
        """
        if not isinstance(raw, dict):
            return {"status": "unknown", "filled_sz": 0.0, "raw": raw}
        if raw.get("status") != "order":
            # z.B. {"status":"unknownOid"}
            return {"status": "unknown", "filled_sz": 0.0, "raw": raw}
        order_wrap = raw.get("order") or {}
        inner_status = str(order_wrap.get("status", "")).lower()
        o = order_wrap.get("order") or {}
        orig = _f(o.get("origSz"))
        rest = _f(o.get("sz"))
        filled_sz = max(0.0, orig - rest)
        if inner_status == "filled":
            status = "filled"
            if filled_sz <= 0 and orig > 0:
                filled_sz = orig  # voll gefüllt, rest evtl. nicht mehr im Feld
        elif inner_status == "open":
            # teilgefüllt vs. ganz offen
            status = "partial" if filled_sz > 0 else "open"
        elif inner_status in ("canceled", "cancelled", "rejected", "marginCanceled".lower()):
            status = "canceled"
        elif inner_status == "triggered":
            # Trigger-Order ausgelöst → behandeln wir wie (teil)gefüllt-fortschreitend
            status = "partial" if 0 < filled_sz < orig else ("filled" if filled_sz >= orig and orig > 0 else "open")
        else:
            status = "unknown"
        return {"status": status, "filled_sz": filled_sz, "raw": raw}

    @staticmethod
    def _statuses(raw):
        """Das statuses[]-Array aus einer HL-Order/Cancel-Antwort holen, oder []."""
        try:
            if not isinstance(raw, dict) or raw.get("status") != "ok":
                return []
            sts = raw["response"]["data"]["statuses"]
            return sts if isinstance(sts, list) else []
        except Exception:
            return []

    @staticmethod
    def _first_status(raw):
        """Erstes statuses[]-Element (kann String 'success' ODER dict sein), oder None."""
        sts = HyperliquidTrader._statuses(raw)
        return sts[0] if sts else None

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

    @staticmethod
    def _is_duplicate_cloid_error(st):
        """C-2: True wenn ein statuses-Element ein Duplicate-Cloid-Reject ist.

        Tritt auf, wenn ein Retry dieselbe Cloid mit einer schon serverseitig
        akzeptierten Order schickt → die ERSTE Order lebt, der Reject ist also
        ein ERFOLG. HL-Wortlaut ist nicht offiziell dokumentiert, daher matchen
        wir mehrere plausible Formulierungen.

        # Testnet-Verifikation ausstehend: HL-Cloid-Dedup-Verhalten
        """
        if not isinstance(st, dict):
            return False
        err = str(st.get("error", "")).lower()
        if not err:
            return False
        return ("cloid" in err and ("already" in err or "duplicate" in err or "exists" in err)) \
            or "duplicate client order id" in err

    @staticmethod
    def _is_already_done_cancel(st):
        """H-6: True wenn ein Cancel-statuses-Element 'Order ist schon weg' meldet
        (gefüllt oder bereits gecancelt) — kein echter Fehler, aber AUCH kein
        'wir haben gerade gecancelt'. Caller muss bei 'filled' den Schutzpfad gehen."""
        if not isinstance(st, dict):
            return False
        err = str(st.get("error", "")).lower()
        if not err:
            return False
        return ("filled" in err) or ("already" in err and ("cancel" in err or "canceled" in err)) \
            or "never placed" in err or "was never placed" in err or "unknown oid" in err

    @staticmethod
    def _cancel_already_filled(st):
        """Spezialfall von _is_already_done_cancel: Order wurde GEFÜLLT (nicht nur
        schon gecancelt). Das ist der gefährliche Fall — die Position lebt."""
        if not isinstance(st, dict):
            return False
        return "filled" in str(st.get("error", "")).lower()

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

    def _order(self, coin, is_buy, sz, px, otype, reduce_only=False, cloid=None):
        kwargs = {"reduce_only": reduce_only}
        if self.builder:
            kwargs["builder"] = self.builder
        # H-4 (2026-06-12): cloid macht den Retry idempotent (HL dedupt per cloid).
        if cloid is not None:
            kwargs["cloid"] = cloid
        # 2026-06-08 A5: reduce_only orders (SL/TP/close) sind must-succeed → 5 retries.
        # Normale entries: 3 retries reichen (User-Signal kann auch nochmal kommen).
        max_attempts = 5 if reduce_only else 3
        label = f"order {coin} {'reduceOnly' if reduce_only else 'entry'}"
        def _do():
            try:
                return self.exchange.order(coin, is_buy, sz, px, otype, **kwargs)
            except TypeError as te:
                # L-7 (2026-06-13): NUR der echte kwarg-Probe-Fall fällt zurück —
                # eine alte SDK-Version, die builder=/cloid= NICHT als kwarg kennt
                # ("unexpected keyword argument 'builder'"). Vorher fing das JEDEN
                # TypeError (auch einen aus dem Inneren von order()/bulk_orders())
                # und re-sendete OHNE den builder → jede solche Order droppte still
                # die Builder-Fee (Michaels Umsatz) UND maskierte echte Bugs.
                # Jetzt: nur bei "unexpected keyword argument" zurückfallen, sonst
                # re-raisen (hl_retry klassifiziert/loggt). Der Fallback BEHÄLT den
                # builder, falls die SDK ihn als POSITIONAL annimmt.
                msg = str(te)
                if "unexpected keyword argument" not in msg:
                    raise
                # Fallback BEHÄLT den builder (Revenue-kritisch) und lässt nur das
                # neuere/optionale cloid weg — eine SDK, die den probe-kwarg nicht
                # kennt, ist alt genug, dass cloid der wahrscheinliche Übeltäter
                # ist; die Builder-Fee darf nie still wegfallen.
                fb_kwargs = {"reduce_only": reduce_only}
                if self.builder:
                    fb_kwargs["builder"] = self.builder
                try:
                    return self.exchange.order(coin, is_buy, sz, px, otype, **fb_kwargs)
                except TypeError as te2:
                    # Auch builder= unbekannt → minimaler Call, aber dann ist die
                    # Builder-Fee in dieser SDK nicht setzbar: WARN, nicht still.
                    if "unexpected keyword argument" not in str(te2):
                        raise
                    log.warning("order(%s): SDK rejects builder kwarg — "
                                "placing WITHOUT builder fee (lost revenue!)", coin)
                    return self.exchange.order(coin, is_buy, sz, px, otype, reduce_only=reduce_only)
        return hl_retry(_do, max_attempts=max_attempts, label=label)

    def place_entry(self, coin, is_buy, sz, px):
        coin = coin_of(coin)
        sz = self._round_sz(coin, sz)
        # H-4 (2026-06-12): cloid PRO Aufruf, über alle Retries konstant → ein Retry
        # nach verlorener Response (Order war serverseitig akzeptiert) ist IDEMPOTENT
        # statt eine zweite Position zu öffnen.
        cloid = Cloid.from_str("0x" + os.urandom(16).hex())
        # C-2: cloid IMMER zurückgeben — der Caller persistiert sie auf der Row und
        # kann einen ambigen/timeout-verlorenen Ausgang später via order_status(cloid)
        # auflösen, statt die LIVE-Order als FAILED zu verwerfen.
        cloid_str = cloid.to_raw()
        try:
            raw = self._order(coin, is_buy, sz, self._round_px(coin, px), {"limit": {"tif": "Gtc"}}, cloid=cloid)
        except Exception as e:
            return {"ok": False, "filled": False, "resting_oid": None, "filled_sz": 0.0,
                    "cloid": cloid_str, "error": str(e)}
        out = {"ok": False, "filled": False, "resting_oid": None, "filled_sz": 0.0,
               "cloid": cloid_str, "dedup": False, "error": None, "raw": raw}
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
        elif self._is_duplicate_cloid_error(st):
            # C-2: Retry traf eine schon akzeptierte Order (Response der 1. ging
            # verloren). Die ERSTE Order lebt → ERFOLG, nicht FAILED. Wir kennen
            # oid/Füllung hier nicht; ok+dedup signalisieren dem Caller, via
            # order_status(cloid=...) aufzulösen statt erneut zu platzieren.
            # # Testnet-Verifikation ausstehend: HL-Cloid-Dedup-Verhalten
            out.update(ok=True, dedup=True, error=None)
            log.warning("place_entry(%s): duplicate-cloid reject → treating as success "
                        "(first order lives), resolve via order_status cloid=%s", coin, cloid_str)
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
        sl_worst = self._round_px(coin, sl_px * (1 + sign * slippage_cap))

        # M-2 (2026-06-12): cloid auch für Schutz-Orders — wie beim Entry (H-4).
        # reduce-only läuft mit max_attempts=5; ein Retry nach verlorener Response
        # (Order serverseitig akzeptiert) konnte SL/TP-Trigger DOPPELT platzieren.
        # Eine pro Order konstante Cloid macht den Retry idempotent (HL dedupt).
        res["sl"] = self._order(coin, is_buy_protection, sz, sl_worst,
                                {"trigger": {"triggerPx": self._round_px(coin, sl_px), "isMarket": True, "tpsl": "sl"}},
                                reduce_only=True,
                                cloid=Cloid.from_str("0x" + os.urandom(16).hex()))
        # C-2: Auf dem reduce-only-SL-Pfad (5 Retries) heißt ein Duplicate-Cloid-
        # Reject, dass die ERSTE SL-Order serverseitig lebt → sl_ok=True. Sonst
        # würde der Caller eine KORREKT geschützte Position notfall-schließen.
        sl_st = self._first_status(res["sl"])
        res["sl_ok"] = self._status_ok(res["sl"]) or self._is_duplicate_cloid_error(sl_st)
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
                tp_worst = self._round_px(coin, px * (1 + sign * slippage_cap))
                # M-2: eigene Cloid pro TP-Order (gleiche Idempotenz-Logik wie SL).
                res["tp"].append(self._order(coin, is_buy_protection, tp_sz, tp_worst,
                                 {"trigger": {"triggerPx": self._round_px(coin, px), "isMarket": True, "tpsl": "tp"}},
                                 reduce_only=True,
                                 cloid=Cloid.from_str("0x" + os.urandom(16).hex())))
        return res

    def cancel_orders(self, coin):
        """Alle offenen Orders (Entry + SL/TP) für einen Coin canceln. -> Anzahl
        TATSÄCHLICH gecancelter Orders (int — Rückgabetyp unverändert, Caller in
        engine/sync prüfen `if n`).

        H-6 (2026-06-13): jeder Cancel parst jetzt statuses. Vorher zählte ein
        "could not cancel … already filled" als Cancel-Erfolg (n++), obwohl die
        Order GEFÜLLT war (Position lebt). Jetzt: nur echte Cancels erhöhen n;
        ein "already filled" wird geloggt und NICHT gezählt. (Der strukturierte
        {ok, already_filled, …}-Rückgabe-Pfad für B/C ist cancel_order_oid.)
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
                        res = hl_retry(lambda: self.exchange.cancel(coin, o["oid"]),
                                       max_attempts=5, label=f"cancel {coin}")
                    except Exception as e:
                        log.warning("cancel %s oid %s final fail: %s", coin, o.get("oid"), e)
                        continue
                    st = self._first_status(res)
                    if st == "success" or self._status_ok(res):
                        n += 1
                    elif isinstance(st, dict) and self._cancel_already_filled(st):
                        # H-6: GEFÜLLT, nicht gecancelt → NICHT zählen (Position lebt).
                        log.warning("cancel %s oid %s: order already filled — NOT counted as cancel",
                                    coin, o.get("oid"))
                    elif isinstance(st, dict) and self._is_already_done_cancel(st):
                        # schon weg/gecancelt — kein neuer Cancel, aber harmlos.
                        log.debug("cancel %s oid %s: already gone", coin, o.get("oid"))
                    else:
                        log.warning("cancel %s oid %s: unexpected response %s", coin, o.get("oid"), res)
        except Exception as e:
            log.warning("open_orders(%s) final fail: %s", coin, e)
        return n

    def _position_size_safe(self, coin):
        """position_size, aber bei Read-Fehler None statt raise (für Re-Read-Bestätigung)."""
        try:
            return self.position_size(coin)
        except Exception as e:
            log.debug("position_size(%s) re-read failed: %s", coin, e)
            return None

    def close_position(self, coin, slippage_cap=None):
        """Offene Position per Market schließen (reduce-only, ohne Builder-Code).
        Phase 6+ (2026-06-03, H-8): explizite Slippage-Cap statt HL-Default (8 %).

        2026-06-12 (Review #4): market_close via hl_retry (5 Versuche, must-succeed).

        C-1 (2026-06-13): SDK market_close ist eine IoC reduce-only LIMIT (mid±slippage).
        Bei No-Match im schnellen/gappenden Markt liefert HL {"status":"ok",
        statuses:[{"error":"…could not immediately match…"}]} — top-level status=="ok"
        ist also KEIN Beweis fürs Schließen. Außerdem: schon flat → SDK returnt None
        (vorher fälschlich ok=False). Jetzt:
          - psz==0 ODER raw is None  → schon flat = ERFOLG.
          - sonst statuses parsen (gefüllte Größe aus statuses[0].filled.totalSz)
            UND mit frischem position_size(coin) RE-READ bestätigen (kurzer Retry).
          - ok = Re-Read zeigt flat (autoritativ; deckt Partial-IoC + No-Match ab).
        Caller MÜSSEN result['ok'] prüfen: ok=False heißt Position evtl. noch OFFEN
        und UNGESCHÜTZT (alter Schutz schon gecancelt).

        Returns: {"ok": bool, "closed": float, "still_open": float, "raw": res}
        """
        coin = coin_of(coin)
        # Positions-Read kann raisen (Review #0). Close ist risk-reduzierend:
        # bei unbekanntem Status trotzdem market_close versuchen (SDK liest Größe selbst).
        psz = None
        try:
            psz = self.position_size(coin)
        except Exception as e:
            log.warning("close_position(%s): position read failed (%s) — versuche market_close trotzdem", coin, e)
        if psz is not None and abs(psz) == 0:
            return {"ok": True, "closed": 0.0, "still_open": 0.0, "raw": None}
        if slippage_cap is None:
            try:
                from app import config as _cfg
                slippage_cap = float(getattr(_cfg, "SL_SLIPPAGE_CAP", 0.02))
            except Exception:
                slippage_cap = 0.02
        try:
            # market_close akzeptiert slippage-Kwarg im HL-SDK (default 0.05).
            # Falls eine SDK-Version den Kwarg nicht hat, fallback.
            def _do():
                try:
                    return self.exchange.market_close(coin, slippage=slippage_cap)
                except TypeError:
                    return self.exchange.market_close(coin)
            raw = hl_retry(_do, max_attempts=5, label=f"market_close {coin}")
        except Exception as e:
            log.warning("market_close(%s) final fail: %s", coin, e)
            return {"ok": False, "closed": 0.0, "still_open": abs(psz) if psz is not None else 0.0,
                    "error": str(e), "raw": None}

        # SDK gibt None zurück, wenn keine Position für den Coin gefunden wurde
        # (for-Loop trifft kein return) → schon flat = Erfolg.
        if raw is None:
            return {"ok": True, "closed": abs(psz) if psz is not None else 0.0,
                    "still_open": 0.0, "raw": None}

        # Gefüllte Größe aus statuses[0].filled.totalSz (best effort).
        filled_sz = 0.0
        st = self._first_status(raw)
        if isinstance(st, dict) and "filled" in st:
            filled_sz = _f(st["filled"].get("totalSz"))

        # AUTORITATIV: frischer Positions-Read bestätigt, ob wirklich flat. Deckt
        # IoC-No-Match (status==ok aber statuses[].error) UND Partial-Fill ab.
        # Kurzer Retry, damit ein einzelner transienter Read den Erfolg nicht maskiert.
        still_open = None
        for _ in range(3):
            pos_after = self._position_size_safe(coin)
            if pos_after is not None:
                still_open = abs(pos_after)
                break
        if still_open is not None:
            ok = (still_open == 0.0)
            closed = (abs(psz) - still_open) if psz is not None else filled_sz
            if closed < 0:
                closed = filled_sz
            return {"ok": ok, "closed": closed, "still_open": still_open, "raw": raw}

        # Re-Read komplett fehlgeschlagen → NICHT blind Erfolg melden. Wir können
        # nur den statuses-Befund nutzen: error-Status = sicher nicht ok.
        st_error = isinstance(st, dict) and "error" in st
        ok = self._status_ok(raw) and not st_error
        return {"ok": ok, "closed": filled_sz,
                "still_open": (abs(psz) - filled_sz) if psz is not None else 0.0, "raw": raw}

    def referral_state(self):
        """Referral-Status der MASTER-Adresse von HL lesen (read-only).

        HL liefert ein dict mit `referredBy`: None, solange kein Referrer
        gesetzt ist; sonst {"referrer": "0x…", "code": "CODE"}. Wir parsen das
        defensiv (jedes Feld kann fehlen) und reduzieren es auf die zwei Werte,
        die der Builder-/Referral-Flow braucht.

        Returns:
            {"referred_by_code": <str|None>, "referrer_addr": <str|None>, "raw": <raw>}
            bei Erfolg, oder {"referred_by_code": None, "referrer_addr": None,
            "error": <str>} bei jeder Exception (nie raisen).
        """
        try:
            raw = self.info.query_referral_state(self.address)
            referred_by = (raw or {}).get("referredBy") if isinstance(raw, dict) else None
            code = None
            addr = None
            if isinstance(referred_by, dict):
                code = referred_by.get("code")
                addr = referred_by.get("referrer")
            return {"referred_by_code": code, "referrer_addr": addr, "raw": raw}
        except Exception as e:
            log.warning("referral_state(%s): %s", self.address, e)
            return {"referred_by_code": None, "referrer_addr": None, "error": str(e)}

    def set_referrer(self, code):
        """Referral-Code für die MASTER-Adresse setzen (signierte Exchange-Action).

        Fail-safe by design: HL lehnt setReferrer ab, wenn (a) schon ein
        Referrer gesetzt ist, (b) Self-Referral, (c) evtl. wenn nur der
        Agent-Key statt des Masters signiert. In ALLEN Fällen geben wir
        {"ok": False, "error": …} zurück und raisen NIE.

        # Testnet-Verifikation ausstehend: ob Agent-Key setReferrer signieren darf

        Returns:
            {"ok": <bool>, "raw": <res>} bei Antwort, sonst {"ok": False, "error": <str>}.
        """
        try:
            res = hl_retry(lambda: self.exchange.set_referrer(code),
                           max_attempts=3, label="set_referrer")
            # setReferrer liefert {"status": "ok", "response": {"type": "default"}}
            # (kein per-Order-`statuses`-Array → _status_ok greift hier NICHT).
            ok = isinstance(res, dict) and res.get("status") == "ok"
            return {"ok": ok, "raw": res}
        except Exception as e:
            log.warning("set_referrer(%s): %s", code, e)
            return {"ok": False, "error": str(e)}
