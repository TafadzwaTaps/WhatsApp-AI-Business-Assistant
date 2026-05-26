from pydantic import BaseModel
from typing import Optional, List
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
    image_url: Optional[str] = None
    stock: int = 0
    low_stock_threshold: int = 5

class ProductOut(BaseModel):
    id: int
    name: str
    price: float
    image_url: Optional[str] = None
    stock: Optional[int] = 0
    low_stock_threshold: Optional[int] = 5

    class Config:
        from_attributes = True

# ── Order ─────────────────────────────────────────────────
class CartItem(BaseModel):
    name: str
    qty: int
    price: float

class OrderCreate(BaseModel):
    customer_phone: str
    items: List[CartItem]

class OrderOut(BaseModel):
    id: int
    customer_phone: str
    product_name: str
    quantity: int
    total_price: float
    status: Optional[str] = "pending"
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True

class OrderStatusUpdate(BaseModel):
    status: str  # pending | confirmed | paid | delivered

# ── Chat ──────────────────────────────────────────────────
class ChatMessageOut(BaseModel):
    id: int
    phone: str
    direction: str
    message: str
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True

class WhatsAppCredentialCreate(BaseModel):
    user_id: int
    phone_number_id: str
    access_token: str
