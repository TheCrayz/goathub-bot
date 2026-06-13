"""Liest #signals (von Bot 1) und gibt jedes Signal an die Engine -> alle Nutzer.
Wird nur gestartet, wenn ENABLE_LISTENER=true (discord.py muss installiert sein).

Phase 2 (2026-06-02): Auto-Reconnect mit exponential backoff. Vorher endete
ein Discord-Gateway-Disconnect den Listener-Task → systemd Restart=always
hat den ganzen Service gekickt (~10s Downtime + verlorene Signale). Jetzt
versucht der Listener selbst weiterzulaufen, mit cap-at-5-min backoff.

2026-06-12 Audit M-6/M-7/LOW-4:
  M-6   — backoff wird bei erfolgreichem Connect (on_ready) auf den Startwert
          zurückgesetzt. Vorher wuchs er über die gesamte Prozess-Lifetime
          (5→300s) — nach ein paar Disconnects über Wochen kostete JEDER
          spätere Ausfall bis zu 5 Minuten verpasste Signale.
  M-7   — Signale, die WÄHREND eines Disconnects gepostet wurden, werden nach
          dem Reconnect via channel.history(after=last_seen) nachgeholt
          (Backfill, max. _BACKFILL_LIMIT Messages, gleicher Pfad wie live).
          Replay-sicher: ProcessedSignal-Dedup (NEW_TRADE) + SL-Ratchet
          (UPDATE) in der Engine. Toggle: SIGNAL_BACKFILL=false (default an).
  LOW-4 — ALLE Embeds einer Message werden in Reihenfolge verarbeitet, nicht
          nur embeds[0] (Bot 1 kann mehrere Signale in eine Message packen).
"""
import asyncio
import logging
import os

from app import config
from app.engine import handle_signal

log = logging.getLogger("goathub.listener")

_INITIAL_BACKOFF_S = 5     # M-6: Startwert, auf den on_ready zurücksetzt
_BACKFILL_LIMIT = 50       # M-7: max. nachzuholende Messages pro Reconnect


def _backfill_enabled():
    """M-7: SIGNAL_BACKFILL env-Toggle (default an). Direkt aus env gelesen
    statt über config.py — config gehört gerade einem anderen Edit-Stream;
    der Integrator kann das später dorthin ziehen."""
    return str(os.getenv("SIGNAL_BACKFILL", "true")).strip().lower() in ("1", "true", "yes", "on")


def _alert(text):
    """H-14 (2026-06-13): Discord-Alert über engine._post_alert (Webhook).
    Lazy-Import, damit das Listener-Modul ohne die volle Engine/HL-Kette
    importierbar bleibt (gleiche Praxis wie sync.py). Best-effort: jeder Fehler
    im Alert-Pfad darf den Listener NIE umbringen."""
    try:
        from app.engine import _post_alert
        _post_alert(text)
    except Exception as e:
        log.warning("alert dispatch failed: %s", e)


async def _resolve_signals_channel(client):
    """H-14: Den #signals-Channel auflösen — erst get_channel (Cache), dann
    fetch_channel (REST). Returnt das Channel-Objekt oder None, wenn BEIDE
    fehlschlagen. Fehler werden geschluckt; None ist das Signal für 'tot'."""
    try:
        ch = client.get_channel(config.SIGNALS_CHANNEL_ID)
        if ch is not None:
            return ch
    except Exception as e:
        log.warning("get_channel(%s) fehlgeschlagen: %s", config.SIGNALS_CHANNEL_ID, e)
    try:
        return await client.fetch_channel(config.SIGNALS_CHANNEL_ID)
    except Exception as e:
        log.warning("fetch_channel(%s) fehlgeschlagen: %s", config.SIGNALS_CHANNEL_ID, e)
        return None


def _embed_to_dict(embed):
    return {"title": embed.title or "",
            "description": embed.description or "",
            "fields": [{"name": f.name, "value": f.value} for f in embed.fields]}


async def _handle_embeds(embeds):
    """LOW-4: ALLE Embeds einer Message in Reihenfolge an die Engine geben.
    Vorher wurde nur embeds[0] verarbeitet — weitere Signale in derselben
    Message gingen still verloren. Fehler pro Embed gefangen, damit ein
    kaputtes Embed die restlichen nicht blockiert."""
    for embed in embeds:
        try:
            await handle_signal(_embed_to_dict(embed))
        except Exception as e:
            log.exception("handle_signal: %s", e)


async def _backfill_missed(channel, anchor, limit=_BACKFILL_LIMIT):
    """M-7: Messages nach `anchor` (discord.Object mit Message-Id) aus der
    Channel-History ziehen und durch denselben Pfad wie Live-Messages schicken.
    Returns (anzahl, letzte_message_id_oder_None). Fehler raisen — der Caller
    (on_ready) fängt sie; ein Backfill-Fehler darf den Live-Listener NIE
    verhindern. Einzelne Messages können doppelt laufen (live + History-Race)
    — die Engine dedupt NEW_TRADEs persistent (ProcessedSignal)."""
    n = 0
    last_id = None
    async for msg in channel.history(after=anchor, limit=limit, oldest_first=True):
        if msg.embeds:
            await _handle_embeds(msg.embeds)
        last_id = msg.id
        n += 1
    return n, last_id


async def start_listener():
    import discord  # lazy

    backoff = _INITIAL_BACKOFF_S  # Sekunden — wächst exponentiell bis max 300
    # M-7: Backfill-Anker = letzte im Channel gesehene Message-Id (rückt auch
    # bei Embed-losen Messages vor). had_session unterscheidet Erst-Connect
    # (kein Backfill nötig) von Reconnect.
    last_seen_id = None
    had_session = False

    while True:
        try:
            intents = discord.Intents.default()
            intents.message_content = True
            client = discord.Client(intents=intents)

            @client.event
            async def on_ready():
                nonlocal backoff, had_session, last_seen_id
                log.info("Listener verbunden als %s | channel=%s | net=%s",
                         client.user, config.SIGNALS_CHANNEL_ID,
                         "testnet" if config.HL_TESTNET else "MAINNET")
                # M-6: Connect geschafft → backoff zurück auf den Startwert.
                backoff = _INITIAL_BACKOFF_S
                is_reconnect = had_session
                had_session = True
                # H-14 (2026-06-13): den #signals-Channel SCHON beim ERSTEN Connect
                # auflösen. Ein toter/falscher SIGNALS_CHANNEL_ID lässt get_channel
                # (und fetch_channel) None liefern: on_message matcht dann NIE
                # (filtert nach channel.id) → der Listener hängt "grün" am Gateway
                # und empfängt SCHWEIGEND null Signale = stiller Totalausfall. Hier
                # einmal pro Connect prüfen und bei beidseitigem Fehlschlag laut
                # alerten (vorher wurde der Channel nur im Reconnect-Backfill-Zweig
                # angefasst — ein Erst-Connect auf einen toten Channel blieb stumm).
                channel = await _resolve_signals_channel(client)
                if channel is None:
                    msg = (f"Discord signals channel {config.SIGNALS_CHANNEL_ID} could not be "
                           f"resolved (get_channel and fetch_channel both failed): bot is "
                           f"connected but will receive NO signals — silent trading outage. "
                           f"Check SIGNALS_CHANNEL_ID, bot guild membership and channel "
                           f"permissions.")
                    log.error(msg)
                    _alert(f"🚨 {msg}")
                    return
                # M-7: ohne gesehene Message die letzte Channel-Message als
                # Anker nehmen (cached im READY-Payload, kein API-Call) — sonst
                # hätte ein Reconnect nach stiller Erst-Session keinen Anker.
                # H-14: Channel ist oben schon aufgelöst — wiederverwenden.
                if last_seen_id is None:
                    try:
                        if getattr(channel, "last_message_id", None):
                            last_seen_id = channel.last_message_id
                    except Exception as e:
                        log.warning("Backfill-Anker-Init fehlgeschlagen: %s", e)
                # M-7: Reconnect (inkl. discord.py-internem Re-Identify) →
                # zwischenzeitlich gepostete Signale nachholen. Best-effort.
                if not (is_reconnect and _backfill_enabled() and last_seen_id is not None):
                    return
                try:
                    n, newest = await _backfill_missed(channel, discord.Object(id=last_seen_id))
                    if newest is not None:
                        last_seen_id = max(last_seen_id, newest)
                    if n:
                        log.info("Signal-Backfill: %d Message(s) nach Reconnect nachgeholt", n)
                except Exception as e:
                    log.error("Signal-Backfill fehlgeschlagen (Live-Listener läuft weiter): %s", e)

            @client.event
            async def on_message(message):
                nonlocal last_seen_id
                if message.channel.id != config.SIGNALS_CHANNEL_ID:
                    return
                # M-7: Anker auch für Embed-lose Messages vorrücken, damit der
                # Backfill nach Reconnect nicht längst Gesehenes neu zieht.
                if last_seen_id is None or message.id > last_seen_id:
                    last_seen_id = message.id
                if not message.embeds:
                    return
                await _handle_embeds(message.embeds)

            await client.start(config.DISCORD_BOT_TOKEN)
            # Sauberes start()-Ende = beabsichtigtes Stop. Loop verlassen.
            log.info("Discord-Listener sauber beendet.")
            return
        except asyncio.CancelledError:
            # Lifespan-shutdown — sauber raus.
            raise
        except Exception as e:
            log.error("Discord-Listener Absturz: %s — Reconnect in %ds", e, backoff)
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                raise
            backoff = min(backoff * 2, 300)  # cap bei 5 Min
