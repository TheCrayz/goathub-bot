"""Auth: Passwort-Hashing (bcrypt mit SHA256-pre-hash) + JWT."""
import base64
import datetime
import hashlib

from fastapi import Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordBearer
import bcrypt
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app import config
from app.db import get_db
from app.models import User

_oauth2 = OAuth2PasswordBearer(tokenUrl="/api/login", auto_error=False)

# Phase 2 #18 (2026-06-02): hybrid auth — Cookie hat Vorrang vor Bearer.
# Beide funktionieren weiter, neue Sessions kommen über Cookie.
SESSION_COOKIE_NAME = "ght_session"

# Phase 4 (2026-06-02): bcrypt schneidet stillschweigend bei 72 Bytes ab.
# Zwei Passwörter mit gleichem 72-Byte-Präfix hashen identisch (Collision-
# Class). Wir lehnen >72-Byte-Eingaben sauber ab und werfen aus dem .encode()
# selbst — UTF-8-Multibyte-Zeichen verbrauchen mehr als 1 Byte.
MAX_PW_BYTES = 72

# 2026-06-04 Restposten #3: BCrypt pre-hash mit Version-Prefix für saubere
# Migration. Der 72-Byte-Limit von bcrypt wird durch SHA256-pre-hash umgangen
# (SHA256-Output ist immer 32 Bytes → safe für bcrypt). Bei Bedarf später auf
# argon2 etc. erweiterbar via neuem Prefix.
#
# Format der gespeicherten Hashes:
#   "v2:<base64-44-char>$<bcrypt-60-char>" → SHA256(pw) → base64 → bcrypt
#   "<bcrypt-60-char>"                     → legacy (direkt-bcrypt, ≤72 Bytes)
#
# Beim verify_pw transparent beide Wege probieren. Bei erfolgreichem Login
# eines legacy-Hashes kann der Caller via `_needs_rehash()` prüfen und einen
# v2-Hash zurückschreiben (siehe main.py:login).
_PREHASH_PREFIX = "v2:"


class PasswordTooLongError(ValueError):
    """Eingabe > 72 Bytes UTF-8 — bcrypt würde sie sonst still truncieren.

    Bleibt erhalten für Backward-Compat, wird aber mit der v2-pre-hash-Pfad
    NICHT mehr ausgelöst (sha256 → 32 byte, immer unter 72). Nur noch bei
    expliziten legacy-Calls oder wenn jemand _pw_bytes() direkt nutzt.
    """


def _pw_bytes(p: str) -> bytes:
    b = p.encode("utf-8")
    if len(b) > MAX_PW_BYTES:
        raise PasswordTooLongError(f"Password too long ({len(b)} bytes > {MAX_PW_BYTES} max)")
    return b


def _prehash(p: str) -> bytes:
    """SHA256(password) base64-encoded — 44 ASCII bytes, sicher unter 72-Limit."""
    digest = hashlib.sha256(p.encode("utf-8")).digest()
    return base64.b64encode(digest)


def hash_pw(p: str) -> str:
    """v2-Hash: SHA256-pre-hash → bcrypt. Keine 72-Byte-Grenze für den User."""
    return _PREHASH_PREFIX + bcrypt.hashpw(_prehash(p), bcrypt.gensalt()).decode()


def needs_rehash(stored: str) -> bool:
    """True wenn der gespeicherte Hash legacy (kein v2-Prefix) ist → beim
    nächsten erfolgreichen Login transparent re-hashen."""
    return not (stored or "").startswith(_PREHASH_PREFIX)


def verify_pw(p: str, h: str) -> bool:
    """v2-Hashes via SHA256→bcrypt; legacy-Hashes direkt-bcrypt (≤72 bytes).

    User mit altem Hash UND langem Passwort (>72 bytes) waren schon vorher kaputt
    (PasswordTooLongError) — bleibt false. Nach erfolgreichem Login auf v2
    migrieren um diese Klasse zu beseitigen.
    """
    try:
        if (h or "").startswith(_PREHASH_PREFIX):
            return bcrypt.checkpw(_prehash(p), h[len(_PREHASH_PREFIX):].encode())
        # legacy direkt-bcrypt path
        return bcrypt.checkpw(_pw_bytes(p), h.encode())
    except PasswordTooLongError:
        # Eine Authentifizierung mit zu langem PW gegen legacy-Hash ist
        # immer falsch — wir können nicht prüfen ob es passt (truncate wäre
        # Collision-Class). Nur via v2-Pfad lösbar.
        return False
    except Exception:
        return False


def make_token(uid: int, token_version: int = 0) -> str:
    """JWT minten. `token_version` mit-einbacken, damit Logout/Pw-Change alle
    alten Tokens unbrauchbar machen kann (Phase 1, 2026-06-02).
    Phase 6 (2026-06-02): datetime.now(timezone.utc) statt deprecated utcnow().
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    exp = now + datetime.timedelta(hours=config.JWT_EXPIRE_HOURS)
    return jwt.encode(
        {
            "sub": str(uid),
            "exp": exp,
            "iat": now,                                   # Phase 6: issued-at
            "jti": __import__("secrets").token_hex(8),    # Phase 6: unique token id
            "tv": int(token_version),
        },
        config.JWT_SECRET,
        algorithm="HS256",
    )


def current_user(
    request: Request,
    bearer: str = Depends(_oauth2),
    db: Session = Depends(get_db),
) -> User:
    """Auth-Resolver. Versucht zuerst das httpOnly-Session-Cookie (Phase 2 #18,
    XSS-sicher), fällt sonst auf das Bearer-Token zurück (Backward-Compat für
    alte Browser-Sessions, curl/dev usage). Beide Wege validieren denselben JWT.
    """
    token = request.cookies.get(SESSION_COOKIE_NAME) or bearer
    if not token:
        raise HTTPException(401, "Nicht angemeldet")
    try:
        payload = jwt.decode(token, config.JWT_SECRET, algorithms=["HS256"])
        uid = int(payload["sub"])
        tv = int(payload.get("tv", 0))
    except (JWTError, KeyError, ValueError):
        raise HTTPException(401, "Ungültiger Token")
    u = db.get(User, uid)
    if not u:
        raise HTTPException(401, "Nutzer nicht gefunden")
    # Phase 1: stale tokens nach Logout/Pw-Change abweisen.
    if int(getattr(u, "token_version", 0) or 0) != tv:
        raise HTTPException(401, "Session abgelaufen — bitte neu einloggen")
    return u
