"""Verschlüsselung der HL-Agent-Keys (Fernet, symmetrisch)."""
from cryptography.fernet import Fernet

from app import config


def _fernet():
    if not config.ENCRYPTION_KEY:
        raise RuntimeError("ENCRYPTION_KEY nicht gesetzt — kann HL-Keys nicht ver-/entschlüsseln")
    key = config.ENCRYPTION_KEY
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


def new_key() -> str:
    """Hilfsfunktion zum Erzeugen eines ENCRYPTION_KEY."""
    return Fernet.generate_key().decode()
