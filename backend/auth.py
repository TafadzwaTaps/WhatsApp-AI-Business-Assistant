import hmac
import secrets
from datetime import datetime, timedelta
from jose import JWTError, jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

# ── Security config ───────────────────────────────────────
# IMPORTANT: Change SECRET_KEY to a long random string in production
# Generate one with: python -c "import secrets; print(secrets.token_hex(32))"
SECRET_KEY = "wazibotSECRET2024changeThisInProduction_REPLACE_ME"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 8  # 8 hours

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

# ── Super admin credentials ───────────────────────────────
# IMPORTANT: Change these before going live!
SUPER_ADMIN_USERNAME = "superadmin"
SUPER_ADMIN_PASSWORD = "superadmin123"

def verify_password(plain: str, stored: str) -> bool:
    """Constant-time comparison to prevent timing attacks."""
    return hmac.compare_digest(plain.encode(), stored.encode())

def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire, "iat": datetime.utcnow()})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def decode_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token invalid or expired. Please log in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )

def get_current_user(token: str = Depends(oauth2_scheme)) -> dict:
    payload = decode_token(token)
    username = payload.get("sub")
    role = payload.get("role", "business")
    business_id = payload.get("business_id")
    if not username:
        raise HTTPException(status_code=401, detail="Invalid token payload")
    return {"username": username, "role": role, "business_id": business_id}

def require_superadmin(user: dict = Depends(get_current_user)):
    if user["role"] != "superadmin":
        raise HTTPException(status_code=403, detail="Superadmin access required")
    return user

def require_business(user: dict = Depends(get_current_user)):
    if user["role"] != "business":
        raise HTTPException(status_code=403, detail="Business account required")
    return user
