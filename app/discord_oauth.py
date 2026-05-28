"""Discord OAuth2 helpers."""
from __future__ import annotations

import httpx
from app import config

DISCORD_API = "https://discord.com/api/v10"


async def exchange_code(code: str) -> dict:
    """Exchange OAuth code for access token."""
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{DISCORD_API}/oauth2/token", data={
            "client_id": config.DISCORD_CLIENT_ID,
            "client_secret": config.DISCORD_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": config.DISCORD_REDIRECT_URI,
        }, headers={"Content-Type": "application/x-www-form-urlencoded"})
        r.raise_for_status()
        return r.json()


async def get_discord_user(access_token: str) -> dict:
    """Get Discord user info."""
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{DISCORD_API}/users/@me",
            headers={"Authorization": f"Bearer {access_token}"})
        r.raise_for_status()
        return r.json()


async def get_guild_member(discord_user_id: str, bot_token: str, guild_id: str) -> dict | None:
    """Get guild member info using bot token."""
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{DISCORD_API}/guilds/{guild_id}/members/{discord_user_id}",
            headers={"Authorization": f"Bot {bot_token}"}
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


def has_required_role(member: dict | None, required_role_id: str) -> bool:
    """Check if member has the required role."""
    if not member:
        return False
    return required_role_id in member.get("roles", [])
