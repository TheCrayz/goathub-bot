"""Rate-Limiter + Client-IP-Key-Funktion.

2026-06-13 Review-Fix (Zirkular-Import): limiter/_client_ip lebten in main.py,
und admin.py holte sie via `from app.main import limiter`. Das funktionierte
NUR, solange app.main vor app.admin importiert wurde (uvicorn-Pfad) — ein
direkter `import app.admin` (Tests, Tools, Shell) crashte mit ImportError aus
dem halb-initialisierten Modul. Jetzt eigenes blattartiges Modul ohne
App-Abhängigkeiten, von beiden Seiten importierbar.
"""
import os

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

# 2026-06-12 #7 (siehe Historie in main.py): Proxy-Header (X-Real-IP/XFF)
# werden NUR vertraut, wenn der direkte TCP-Peer localhost ist (= Caddy auf
# derselben Maschine; uvicorn ist im systemd-Unit auf 127.0.0.1 gebunden).
# Jeder andere Peer wird auf seine eigene IP gekeyt — Spoofing-Schutz.
_TRUSTED_PROXY_PEERS = ("127.0.0.1", "::1")


def _client_ip(request: Request) -> str:
    peer = request.client.host if request.client else None
    if peer in _TRUSTED_PROXY_PEERS:
        # Direkter Peer ist der lokale Reverse-Proxy (Caddy) → dessen Header
        # sind vertrauenswürdig. Primary: X-Real-IP (proxy-kontrolliert).
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip.strip()
        # Fallback: letzter Wert in der XFF-Chain (Proxy appendet als letztes).
        # Achtung: NICHT der erste — der ist attacker-controlled wenn der
        # Proxy kein XFF-Clearing macht.
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[-1].strip()
    # Kein (vertrauenswürdiger) Proxy davor → TCP-Peer zählt. Header von
    # Nicht-localhost-Peers werden bewusst IGNORIERT (Spoofing-Schutz).
    return peer or get_remote_address(request)


limiter = Limiter(key_func=_client_ip)
LOGIN_RATE_LIMIT = os.getenv("LOGIN_RATE_LIMIT", "10/5minute")
REGISTER_RATE_LIMIT = os.getenv("REGISTER_RATE_LIMIT", "5/5minute")
