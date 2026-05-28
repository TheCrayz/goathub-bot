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


def make_token(uid: int) -> str:
    exp = datetime.datetime.utcnow() + datetime.timedelta(hours=config.JWT_EXPIRE_HOURS)
    return jwt.encode({"sub": str(uid), "exp": exp}, config.JWT_SECRET, algorithm="HS256")


def current_user(token: str = Depends(_oauth2), db: Session = Depends(get_db)) -> User:
    if not token:
        raise HTTPException(401, "Nicht angemeldet")
    try:
        uid = int(jwt.decode(token, config.JWT_SECRET, algorithms=["HS256"])["sub"])
    except (JWTError, KeyError, ValueError):
        raise HTTPException(401, "Ungültiger Token")
    u = db.get(User, uid)
    if not u:
        raise HTTPException(401, "Nutzer nicht gefunden")
    return u
