"""Auth: Passwort-Hashing (bcrypt) + JWT."""
import datetime

from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
import bcrypt
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app import config
from app.db import get_db
from app.models import User

_oauth2 = OAuth2PasswordBearer(tokenUrl="/api/login", auto_error=False)


def hash_pw(p: str) -> str:
    return bcrypt.hashpw(p.encode()[:72], bcrypt.gensalt()).decode()


def verify_pw(p: str, h: str) -> bool:
    try:
        return bcrypt.checkpw(p.encode()[:72], h.encode())
    except Exception:
        return False


def make_token(uid: int, token_version: int = 0) -> str:
    """JWT minten. `token_version` mit-einbacken, damit Logout/Pw-Change alle
    alten Tokens unbrauchbar machen kann (Phase 1, 2026-06-02)."""
    exp = datetime.datetime.utcnow() + datetime.timedelta(hours=config.JWT_EXPIRE_HOURS)
    return jwt.encode(
        {"sub": str(uid), "exp": exp, "tv": int(token_version)},
        config.JWT_SECRET,
        algorithm="HS256",
    )


def current_user(token: str = Depends(_oauth2), db: Session = Depends(get_db)) -> User:
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
