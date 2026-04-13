from pydantic import BaseModel
from typing import Optional
from datetime import datetime

# ── Business ──────────────────────────────────────────────
class BusinessCreate(BaseModel):
    name: str
    owner_username: str
    owner_password: str
    whatsapp_phone_id: Optional[str] = None
    whatsapp_token: Optional[str] = None

class BusinessOut(BaseModel):
    id: int
    name: str
    owner_username: str
    whatsapp_phone_id: Optional[str] = None
    is_active: bool
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True

class BusinessUpdate(BaseModel):
    name: Optional[str] = None
    whatsapp_phone_id: Optional[str] = None
    whatsapp_token: Optional[str] = None
    is_active: Optional[bool] = None

# ── Product ───────────────────────────────────────────────
class ProductCreate(BaseModel):
    name: str
    price: float

class ProductOut(BaseModel):
    id: int
    name: str
    price: float

    class Config:
        from_attributes = True

# ── Order ─────────────────────────────────────────────────
class OrderCreate(BaseModel):
    customer_phone: str
    product_name: str
    quantity: int

class OrderOut(BaseModel):
    id: int
    customer_phone: str
    product_name: str
    quantity: int
    total_price: float
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True

# ── Chat ──────────────────────────────────────────────────
class ChatMessageOut(BaseModel):
    id: int
    phone: str
    direction: str
    message: str
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True
