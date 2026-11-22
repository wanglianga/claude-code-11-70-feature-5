from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import User
from ..schemas import LoginIn
from ..security import issue_token, require_user, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == body.username).first()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "用户名或密码错误")
    return {"token": issue_token(user.username), "user": {"username": user.username, "role": user.role}}


@router.get("/me")
def me(user: User = Depends(require_user)):
    return {"username": user.username, "role": user.role}
