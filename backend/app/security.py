import base64
import hashlib
import hmac
import os
import time

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from . import config
from .db import get_db
from .models import User

bearer = HTTPBearer(auto_error=False)


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or os.urandom(8).hex()
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 20000).hex()
    return f"{salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, _ = stored.split("$", 1)
    except ValueError:
        return False
    return hmac.compare_digest(hash_password(password, salt), stored)


def issue_token(username: str) -> str:
    expiry = int(time.time()) + config.TOKEN_TTL_HOURS * 3600
    body = base64.urlsafe_b64encode(f"{username}:{expiry}".encode()).decode()
    sig = hmac.new(config.SECRET_KEY.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def parse_token(token: str) -> str | None:
    try:
        body, sig = token.rsplit(".", 1)
    except ValueError:
        return None
    expect = hmac.new(config.SECRET_KEY.encode(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expect, sig):
        return None
    try:
        username, expiry = base64.urlsafe_b64decode(body.encode()).decode().rsplit(":", 1)
    except Exception:
        return None
    if int(expiry) < int(time.time()):
        return None
    return username


def require_user(
    cred: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    if cred is None:
        raise HTTPException(401, "未登录")
    username = parse_token(cred.credentials)
    if not username:
        raise HTTPException(401, "令牌无效或已过期")
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(401, "用户不存在")
    return user
