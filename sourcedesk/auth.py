"""Argon2 password hashes and expiring, algorithm-pinned bearer access tokens."""
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
from fastapi import APIRouter, Depends
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pwdlib import PasswordHash
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sourcedesk.config import get_settings
from sourcedesk.db import get_session
from sourcedesk.errors import AppError
from sourcedesk.models import User

router = APIRouter(prefix="/api/v1", tags=["authentication"])
oauth2 = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)
passwords = PasswordHash.recommended()
_dummy_hash = passwords.hash("dummy-not-a-user-password")


class Registration(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)


class UserView(BaseModel):
    id: UUID
    email: str
    is_active: bool
    created_at: datetime
    model_config = {"from_attributes": True}


class TokenView(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


def current_user(token: str | None = Depends(oauth2), session: Session = Depends(get_session)) -> User:
    if not token or len(token) > 4096:
        raise AppError("invalid_token", "A valid bearer access token is required", 401)
    try:
        payload = jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"],
                             options={"require": ["sub", "exp"]})
        user_id = UUID(payload["sub"])
    except (jwt.InvalidTokenError, ValueError, TypeError, KeyError):
        raise AppError("invalid_token", "Access token is invalid or expired", 401) from None
    user = session.get(User, user_id)
    if not user or not user.is_active:
        raise AppError("invalid_token", "Access token is invalid or user is inactive", 401)
    # Release the read transaction before downstream PDF/provider work.
    session.commit()
    return user


@router.post("/auth/register", response_model=UserView, status_code=201)
def register(body: Registration, session: Session = Depends(get_session)):
    hashed = passwords.hash(body.password)
    user = User(email=str(body.email).strip().lower(), password_hash=hashed)
    session.add(user)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise AppError("email_exists", "An account with this email already exists", 409) from None
    return user


@router.post("/auth/login", response_model=TokenView)
def login(form: OAuth2PasswordRequestForm = Depends(), session: Session = Depends(get_session)):
    if len(form.username) > 320 or len(form.password) > 128:
        raise AppError("invalid_credentials", "Invalid email or password", 401)
    user = session.scalar(select(User).where(User.email == form.username.strip().lower()))
    stored = user.password_hash if user else _dummy_hash
    session.commit()
    valid = passwords.verify(form.password, stored)
    if not valid or not user or not user.is_active:
        raise AppError("invalid_credentials", "Invalid email or password", 401)
    settings = get_settings()
    now = datetime.now(UTC)
    token = jwt.encode({"sub": str(user.id), "iat": now,
                        "exp": now + timedelta(minutes=settings.access_token_minutes)},
                       settings.jwt_secret, algorithm="HS256")
    return TokenView(access_token=token, expires_in=settings.access_token_minutes * 60)


@router.get("/users/me", response_model=UserView)
def me(user: User = Depends(current_user)):
    return user
