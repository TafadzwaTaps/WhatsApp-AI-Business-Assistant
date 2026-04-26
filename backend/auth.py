"""
auth.py — JWT authentication helpers.

Changes vs original:
  • create_refresh_token() added — issues 7-day tokens for /auth/refresh.
  • Login/signup callers must return both access_token + refresh_token.
  • Everything else is unchanged.
"""

import hmac
import os
from datetime import datetime, timedelta

from jose import JWTError, jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from dotenv import load_dotenv

load_dotenv()

SECRET_KEY  = os.getenv("SECRET_KEY", "change_this_in_production_use_env_file")
ALGORITHM   = "HS256"

ACCESS_TOKEN_EXPIRE_MINUTES  = 60 * 8       # 8 hours
REFRESH_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days

SUPER_ADMIN_USERNAME = os.getenv("SUPER_ADMIN_USERNAME", "superadmin")
SUPER_ADMIN_PASSWORD = os.getenv("SUPER_ADMIN_PASSWORD", "superadmin123")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


def verify_password(plain: str, stored: str) -> bool:
    """Constant-time comparison — prevents timing attacks."""
    return hmac.compare_digest(plain.encode(), stored.encode())


def create_access_token(data: dict) -> str:
    payload = data.copy()
    payload["exp"]  = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload["type"] = "access"
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def create_refresh_token(data: dict) -> str:
    """Long-lived token used by /auth/refresh to issue a fresh access token."""
    payload = data.copy()
    payload["exp"]  = datetime.utcnow() + timedelta(minutes=REFRESH_TOKEN_EXPIRE_MINUTES)
    payload["type"] = "refresh"
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token invalid or expired. Please log in again.",
        )


def get_current_user(token: str = Depends(oauth2_scheme)) -> dict:
    payload     = decode_token(token)
    username    = payload.get("sub")
    role        = payload.get("role", "business")
    business_id = payload.get("business_id")
    if not username:
        raise HTTPException(status_code=401, detail="Invalid token payload")
    return {"username": username, "role": role, "business_id": business_id}


def require_superadmin(user: dict = Depends(get_current_user)):
    if user["role"] != "superadmin":
        raise HTTPException(status_code=403, detail="Superadmin access required")
    return user


def require_business(user: dict = Depends(get_current_user)):
    if user["role"] not in ("business",):
        raise HTTPException(status_code=403, detail="Business account required")
    return user
