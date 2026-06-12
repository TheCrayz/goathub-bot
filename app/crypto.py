"""Verschlüsselung der HL-Agent-Keys (Fernet, symmetrisch)."""
from cryptography.fernet import Fernet, MultiFernet

from app import config


def _fernet():
    # 2026-06-12 #43/M-15: MultiFernet statt Single-Fernet → Key-Rotation.
    # encrypt() nutzt IMMER den ersten (= neuesten) Key, decrypt() probiert
    # alle Keys der Reihe nach. Quelle ist config.ENCRYPTION_KEYS (newest
    # first) — gespeist aus ENCRYPTION_KEYS (kommasepariert) oder fallback
    # ENCRYPTION_KEY/+_OLD. Bestehende hl_api_secret_enc bleiben lesbar,
    # neue Saves laufen schon über den neuen Key. Validierung der Keys passiert
    # beim Import in config.py (Service startet ohne gültigen Key gar nicht).
    keys = getattr(config, "ENCRYPTION_KEYS", None) or \
        [k for k in (config.ENCRYPTION_KEY, getattr(config, "ENCRYPTION_KEY_OLD", "")) if k]
    if not keys:
        raise RuntimeError("ENCRYPTION_KEY nicht gesetzt — kann HL-Keys nicht ver-/entschlüsseln")

    def _k(key):
        return Fernet(key.encode() if isinstance(key, str) else key)

    return MultiFernet([_k(k) for k in keys])


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


def new_key() -> str:
    """Hilfsfunktion zum Erzeugen eines ENCRYPTION_KEY."""
    return Fernet.generate_key().decode()
