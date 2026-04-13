import hmac
from datetime import datetime, timedelta
from jose import JWTError, jwt
from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer

SECRET_KEY = "wazibotSECRET2024changeThisInProduction"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 8

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

# ── Super admin (you — manages all businesses) ────────────
SUPER_ADMIN_USERNAME = "superadmin"
SUPER_ADMIN_PASSWORD = "superadmin123"   # ← change this!

def verify_password(plain: str, stored: str) -> bool:
    return hmac.compare_digest(plain, stored)

def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

def get_current_user(token: str = Depends(oauth2_scheme)) -> dict:
    """Returns {"username": ..., "role": "superadmin"|"business", "business_id": ...}"""
    payload = decode_token(token)
    username = payload.get("sub")
    role = payload.get("role", "business")
    business_id = payload.get("business_id")
    if not username:
        raise HTTPException(status_code=401, detail="Invalid token")
    return {"username": username, "role": role, "business_id": business_id}

def require_superadmin(user: dict = Depends(get_current_user)):
    if user["role"] != "superadmin":
        raise HTTPException(status_code=403, detail="Superadmin only")
    return user

def get_business_id_from_token(user: dict = Depends(get_current_user)) -> int:
    if user["role"] == "superadmin":
        raise HTTPException(status_code=403, detail="Use a business account")
    return user["business_id"]
