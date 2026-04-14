from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from typing import List
from pydantic import BaseModel, validator
from database import Base, engine, SessionLocal
import models
from schemas import (
    ProductCreate, ProductOut, OrderOut, OrderCreate,
    ChatMessageOut, BusinessCreate, BusinessOut, BusinessUpdate
)
import crud
from ai import generate_reply
import requests as http_requests
from auth import (
    verify_password, create_access_token, get_current_user,
    require_superadmin, require_business,
    SUPER_ADMIN_USERNAME, SUPER_ADMIN_PASSWORD
)

app = FastAPI(title="WaziBot SaaS API", docs_url=None, redoc_url=None)  # Disable docs in production

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict to your domain in production
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ─── SEND WHATSAPP MESSAGE ───────────────────────────────
def send_message(business: models.Business, to: str, message: str):
    if not business.whatsapp_token or not business.whatsapp_phone_id:
        print(f"⚠️  [{business.name}] No WhatsApp credentials set")
        return None
    url = f"https://graph.facebook.com/v18.0/{business.whatsapp_phone_id}/messages"
    headers = {
        "Authorization": f"Bearer {business.whatsapp_token}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": message}
    }
    try:
        resp = http_requests.post(url, headers=headers, json=payload, timeout=10)
        return resp.json()
    except Exception as e:
        print(f"⚠️  WhatsApp send error: {e}")
        return None

# ─── PUBLIC: SELF-SIGNUP ─────────────────────────────────
class SignupRequest(BaseModel):
    business_name: str
    username: str
    password: str
    whatsapp_phone_id: str = ""
    whatsapp_token: str = ""

    @validator("username")
    def username_valid(cls, v):
        v = v.strip()
        if len(v) < 3:
            raise ValueError("Username must be at least 3 characters")
        if " " in v:
            raise ValueError("Username cannot contain spaces")
        if not v.replace("_","").replace("-","").isalnum():
            raise ValueError("Username can only contain letters, numbers, - and _")
        return v.lower()

    @validator("password")
    def password_valid(cls, v):
        if len(v) < 6:
            raise ValueError("Password must be at least 6 characters")
        return v

    @validator("business_name")
    def bizname_valid(cls, v):
        v = v.strip()
        if len(v) < 2:
            raise ValueError("Business name too short")
        return v

@app.post("/auth/signup")
def signup(data: SignupRequest, db: Session = Depends(get_db)):
    # Block superadmin username
    if data.username.lower() == SUPER_ADMIN_USERNAME.lower():
        raise HTTPException(status_code=400, detail="Username not available")

    existing = crud.get_business_by_username(db, data.username)
    if existing:
        raise HTTPException(status_code=400, detail="Username already taken")

    business = crud.create_business(db, BusinessCreate(
        name=data.business_name,
        owner_username=data.username,
        owner_password=data.password,
        whatsapp_phone_id=data.whatsapp_phone_id.strip() or None,
        whatsapp_token=data.whatsapp_token.strip() or None,
    ))

    token = create_access_token({
        "sub": business.owner_username,
        "role": "business",
        "business_id": business.id
    })

    print(f"🆕 New signup: {business.name} (@{business.owner_username})")

    return {
        "access_token": token,
        "token_type": "bearer",
        "role": "business",
        "business_name": business.name,
        "business_id": business.id
    }

# ─── LOGIN ────────────────────────────────────────────────
class LoginRequest(BaseModel):
    username: str
    password: str

@app.post("/auth/login")
def login(data: LoginRequest, db: Session = Depends(get_db)):
    # Sanitize
    username = data.username.strip().lower()
    password = data.password

    # Superadmin check
    if username == SUPER_ADMIN_USERNAME.lower():
        if not verify_password(password, SUPER_ADMIN_PASSWORD):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        token = create_access_token({"sub": SUPER_ADMIN_USERNAME, "role": "superadmin"})
        return {"access_token": token, "token_type": "bearer", "role": "superadmin"}

    # Business account check
    business = crud.get_business_by_username(db, username)
    if not business:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not verify_password(password, business.owner_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not business.is_active:
        raise HTTPException(status_code=403, detail="Account suspended. Contact support.")

    token = create_access_token({
        "sub": business.owner_username,
        "role": "business",
        "business_id": business.id
    })
    return {
        "access_token": token,
        "token_type": "bearer",
        "role": "business",
        "business_name": business.name,
        "business_id": business.id
    }

# ─── WEBHOOK ─────────────────────────────────────────────
VERIFY_TOKEN = "myverifytoken123"

@app.get("/webhook")
async def verify_webhook(request: Request):
    params = request.query_params
    if params.get("hub.verify_token") == VERIFY_TOKEN:
        return int(params.get("hub.challenge"))
    return {"error": "Verification failed"}

@app.post("/webhook")
async def receive_message(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    try:
        changes = data["entry"][0]["changes"][0]["value"]
        phone_number_id = changes["metadata"]["phone_number_id"]

        # Ignore status updates (delivery receipts etc)
        if "messages" not in changes:
            return {"status": "ok"}

        message_obj = changes["messages"][0]

        # Only handle text messages
        if message_obj.get("type") != "text":
            return {"status": "ok"}

        phone = message_obj["from"]
        text = message_obj["text"]["body"].strip()

        business = crud.get_business_by_phone_id(db, phone_number_id)
        if not business or not business.is_active:
            print(f"⚠️  No active business for phone_id: {phone_number_id}")
            return {"status": "ok"}

        crud.log_message(db, business.id, phone, "in", text)

        # Build reply
        if "menu" in text.lower():
            products = crud.get_products(db, business.id)
            if products:
                lines = "\n".join([f"{i+1}. {p.name} - ${p.price}" for i, p in enumerate(products)])
                reply = f"📋 {business.name} Menu:\n\n{lines}\n\nTo order, type:\norder <product> <quantity>\n\nExample: order sadza 2"
            else:
                reply = f"Hi! Welcome to {business.name}. Our menu is being updated. Check back soon! 🙏"
        elif text.lower().startswith("order "):
            try:
                parts = text.strip().split()
                if len(parts) < 3:
                    raise ValueError("Too few parts")
                product_name = parts[1].lower()
                qty = int(parts[2])
                if qty <= 0:
                    raise ValueError("Invalid quantity")
                crud.create_order(db, business.id, OrderCreate(
                    customer_phone=phone,
                    product_name=product_name,
                    quantity=qty
                ))
                reply = f"✅ Order confirmed!\n\n{product_name.capitalize()} x{qty}\n\nThank you for ordering from {business.name}! We'll be in touch shortly. 🙏"
            except ValueError:
                reply = "❌ Invalid format.\n\nUse: order <product> <quantity>\nExample: order sadza 2"
        else:
            reply = generate_reply(text)

        crud.log_message(db, business.id, phone, "out", reply)
        send_message(business, phone, reply)
        print(f"✅ [{business.name}] {phone}: {text!r}")

    except Exception as e:
        print(f"⚠️  Webhook error: {e}")

    return {"status": "ok"}

# ─── SUPERADMIN ───────────────────────────────────────────
@app.get("/admin/businesses", response_model=List[BusinessOut])
def list_businesses(db: Session = Depends(get_db), user=Depends(require_superadmin)):
    return crud.get_all_businesses(db)

@app.post("/admin/businesses", response_model=BusinessOut)
def admin_create_business(data: BusinessCreate, db: Session = Depends(get_db), user=Depends(require_superadmin)):
    if crud.get_business_by_username(db, data.owner_username):
        raise HTTPException(status_code=400, detail="Username already taken")
    return crud.create_business(db, data)

@app.patch("/admin/businesses/{business_id}", response_model=BusinessOut)
def admin_update_business(business_id: int, data: BusinessUpdate, db: Session = Depends(get_db), user=Depends(require_superadmin)):
    b = crud.update_business(db, business_id, data)
    if not b:
        raise HTTPException(status_code=404, detail="Business not found")
    return b

@app.delete("/admin/businesses/{business_id}")
def admin_delete_business(business_id: int, db: Session = Depends(get_db), user=Depends(require_superadmin)):
    b = crud.delete_business(db, business_id)
    if not b:
        raise HTTPException(status_code=404, detail="Business not found")
    return {"deleted": business_id}

@app.get("/admin/stats")
def admin_stats(db: Session = Depends(get_db), user=Depends(require_superadmin)):
    businesses = crud.get_all_businesses(db)
    orders = db.query(models.Order).all()
    return {
        "businesses": len(businesses),
        "active_businesses": sum(1 for b in businesses if b.is_active),
        "total_orders": len(orders),
        "total_revenue": round(sum(o.total_price or 0 for o in orders), 2)
    }

# ─── BUSINESS: ME ────────────────────────────────────────
@app.get("/me", response_model=BusinessOut)
def get_me(db: Session = Depends(get_db), user=Depends(require_business)):
    b = crud.get_business_by_id(db, user["business_id"])
    if not b:
        raise HTTPException(status_code=404, detail="Not found")
    return b

@app.patch("/me", response_model=BusinessOut)
def update_me(data: BusinessUpdate, db: Session = Depends(get_db), user=Depends(require_business)):
    # Business owners cannot change their own active status
    data_dict = data.dict(exclude_none=True)
    data_dict.pop("is_active", None)
    safe_data = BusinessUpdate(**data_dict)
    b = crud.update_business(db, user["business_id"], safe_data)
    return b

# ─── PRODUCTS ────────────────────────────────────────────
@app.get("/products", response_model=List[ProductOut])
def get_products(db: Session = Depends(get_db), user=Depends(get_current_user)):
    return crud.get_products(db, user["business_id"])

@app.post("/products", response_model=ProductOut)
def create_product(product: ProductCreate, db: Session = Depends(get_db), user=Depends(require_business)):
    return crud.create_product(db, user["business_id"], product)

@app.delete("/products/{product_id}")
def delete_product(product_id: int, db: Session = Depends(get_db), user=Depends(require_business)):
    p = crud.delete_product(db, product_id, user["business_id"])
    if not p:
        raise HTTPException(status_code=404, detail="Product not found")
    return {"deleted": product_id}

# ─── ORDERS ──────────────────────────────────────────────
@app.get("/orders", response_model=List[OrderOut])
def get_orders(db: Session = Depends(get_db), user=Depends(require_business)):
    return crud.get_orders(db, user["business_id"])

# ─── CONVERSATIONS ────────────────────────────────────────
@app.get("/conversations", response_model=List[ChatMessageOut])
def get_conversations(db: Session = Depends(get_db), user=Depends(require_business)):
    return crud.get_conversations(db, user["business_id"])

@app.get("/conversations/{phone}", response_model=List[ChatMessageOut])
def get_chat(phone: str, db: Session = Depends(get_db), user=Depends(require_business)):
    return crud.get_messages_for_phone(db, user["business_id"], phone)

# ─── BROADCAST ───────────────────────────────────────────
class BroadcastRequest(BaseModel):
    message: str

    @validator("message")
    def message_valid(cls, v):
        v = v.strip()
        if len(v) < 3:
            raise ValueError("Message too short")
        if len(v) > 1024:
            raise ValueError("Message too long (max 1024 characters)")
        return v

@app.post("/broadcast")
def broadcast(body: BroadcastRequest, db: Session = Depends(get_db), user=Depends(require_business)):
    bid = user["business_id"]
    business = crud.get_business_by_id(db, bid)
    phones = crud.get_all_customer_phones(db, bid)
    if not phones:
        return {"sent": 0, "failed": 0, "total": 0, "message": "No customers found"}

    sent, failed, failed_numbers = 0, 0, []
    for phone in phones:
        try:
            send_message(business, phone, body.message)
            crud.log_message(db, bid, phone, "out", f"[BROADCAST] {body.message}")
            sent += 1
        except Exception as e:
            failed += 1
            failed_numbers.append(phone)
            print(f"⚠️  Broadcast failed for {phone}: {e}")

    return {"sent": sent, "failed": failed, "total": len(phones), "failed_numbers": failed_numbers}

@app.get("/customers")
def get_customers(db: Session = Depends(get_db), user=Depends(require_business)):
    phones = crud.get_all_customer_phones(db, user["business_id"])
    return {"phones": phones, "total": len(phones)}

# ─── STATIC + ROOT ───────────────────────────────────────
Base.metadata.create_all(bind=engine)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def root():
    return {"message": "WaziBot SaaS 🚀"}
