"""Liest #signals (von Bot 1) und gibt jedes Signal an die Engine -> alle Nutzer.
Wird nur gestartet, wenn ENABLE_LISTENER=true (discord.py muss installiert sein).

Phase 2 (2026-06-02): Auto-Reconnect mit exponential backoff. Vorher endete
ein Discord-Gateway-Disconnect den Listener-Task → systemd Restart=always
hat den ganzen Service gekickt (~10s Downtime + verlorene Signale). Jetzt
versucht der Listener selbst weiterzulaufen, mit cap-at-5-min backoff.
"""
import asyncio
import logging

from app import config
from app.engine import handle_signal

log = logging.getLogger("goathub.listener")


def _embed_to_dict(embed):
    return {"title": embed.title or "",
            "description": embed.description or "",
            "fields": [{"name": f.name, "value": f.value} for f in embed.fields]}


async def start_listener():
    import discord  # lazy

    backoff = 5  # Sekunden — wächst exponentiell bis max 300
    while True:
        try:
            intents = discord.Intents.default()
            intents.message_content = True
            client = discord.Client(intents=intents)

            @client.event
            async def on_ready():
                log.info("Listener verbunden als %s | channel=%s | net=%s",
                         client.user, config.SIGNALS_CHANNEL_ID,
                         "testnet" if config.HL_TESTNET else "MAINNET")

            @client.event
            async def on_message(message):
                if message.channel.id != config.SIGNALS_CHANNEL_ID or not message.embeds:
                    return
                try:
                    await handle_signal(_embed_to_dict(message.embeds[0]))
                except Exception as e:
                    log.exception("handle_signal: %s", e)

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
