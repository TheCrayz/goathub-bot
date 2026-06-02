"""Auth: Passwort-Hashing (bcrypt) + JWT."""
import datetime

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


class PasswordTooLongError(ValueError):
    """Eingabe > 72 Bytes UTF-8 — bcrypt würde sie sonst still truncieren."""


def _pw_bytes(p: str) -> bytes:
    b = p.encode("utf-8")
    if len(b) > MAX_PW_BYTES:
        raise PasswordTooLongError(f"Password too long ({len(b)} bytes > {MAX_PW_BYTES} max)")
    return b


def hash_pw(p: str) -> str:
    return bcrypt.hashpw(_pw_bytes(p), bcrypt.gensalt()).decode()


def verify_pw(p: str, h: str) -> bool:
    try:
        return bcrypt.checkpw(_pw_bytes(p), h.encode())
    except PasswordTooLongError:
        # Eine Authentifizierung mit zu langem PW ist immer falsch — wir
        # KÖNNEN nicht prüfen ob es passt (truncate wäre Collision).
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
