import os
import requests as http_requests
from typing import List

from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, validator
from sqlalchemy.orm import Session
from dotenv import load_dotenv

from database import Base, engine, SessionLocal
import models
from schemas import (
    ProductCreate, ProductOut,
    OrderOut, OrderCreate,
    ChatMessageOut,
    BusinessCreate, BusinessOut, BusinessUpdate,
)
import crud
from ai import generate_reply
from auth import (
    verify_password, create_access_token, get_current_user,
    require_superadmin, require_business,
    SUPER_ADMIN_USERNAME, SUPER_ADMIN_PASSWORD,
)

load_dotenv()

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "myverifytoken123")

app = FastAPI(title="WaziBot SaaS API", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ─── WHATSAPP SENDER ─────────────────────────────────────
def send_whatsapp(phone_number_id: str, token: str, to: str, message: str) -> dict:
    if not phone_number_id or not token:
        print("⚠️  send_whatsapp: missing phone_number_id or token")
        return {"error": "missing credentials"}

    to = to.replace("whatsapp:", "")

    url = f"https://graph.facebook.com/v18.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": message},
    }

    try:
        resp = http_requests.post(url, headers=headers, json=payload, timeout=10)
        result = resp.json()
        if resp.status_code != 200:
            print(f"⚠️  WhatsApp API error {resp.status_code}: {result}")
        else:
            msg_id = result.get("messages", [{}])[0].get("id", "unknown")
            print(f"📤 Sent to {to} — msg_id: {msg_id}")
        return result
    except Exception as e:
        print(f"⚠️  WhatsApp send exception: {e}")
        return {"error": str(e)}


# ─── SIGNUP ──────────────────────────────────────────────
class SignupRequest(BaseModel):
    business_name: str
    username: str
    password: str
    whatsapp_phone_id: str = ""
    whatsapp_token: str = ""

    @validator("username")
    def username_valid(cls, v):
        v = v.strip().lower()
        if len(v) < 3:
            raise ValueError("Username must be at least 3 characters")
        if " " in v:
            raise ValueError("Username cannot contain spaces")
        return v

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
    if data.username == SUPER_ADMIN_USERNAME.lower():
        raise HTTPException(status_code=400, detail="Username not available")
    if crud.get_business_by_username(db, data.username):
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
        "business_id": business.id,
    })

    print(f"🆕 New signup: {business.name} (@{business.owner_username})")
    return {
        "access_token": token,
        "token_type": "bearer",
        "role": "business",
        "business_name": business.name,
        "business_id": business.id,
    }


# ─── LOGIN ────────────────────────────────────────────────
class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/auth/login")
def login(data: LoginRequest, db: Session = Depends(get_db)):
    username = data.username.strip().lower()

    if username == SUPER_ADMIN_USERNAME.lower():
        if not verify_password(data.password, SUPER_ADMIN_PASSWORD):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        token = create_access_token({"sub": SUPER_ADMIN_USERNAME, "role": "superadmin"})
        return {"access_token": token, "token_type": "bearer", "role": "superadmin"}

    business = crud.get_business_by_username(db, username)
    if not business or not verify_password(data.password, business.owner_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not business.is_active:
        raise HTTPException(status_code=403, detail="Account suspended. Contact support.")

    token = create_access_token({
        "sub": business.owner_username,
        "role": "business",
        "business_id": business.id,
    })
    return {
        "access_token": token,
        "token_type": "bearer",
        "role": "business",
        "business_name": business.name,
        "business_id": business.id,
    }


# ─── WEBHOOK ─────────────────────────────────────────────
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

        # Ignore delivery receipts / status updates
        if "messages" not in changes:
            return {"status": "ok"}

        msg_obj = changes["messages"][0]

        # Only handle text messages
        if msg_obj.get("type") != "text":
            return {"status": "ok"}

        phone_number_id = changes["metadata"]["phone_number_id"]
        customer_phone = msg_obj["from"]
        text = msg_obj["text"]["body"].strip()

        print(f"📥 Incoming | phone_id={phone_number_id} | from={customer_phone} | text={text!r}")

        # Find the business
        business = crud.get_business_by_phone_id(db, phone_number_id)
        if not business:
            print(f"⚠️  No business found for phone_number_id={phone_number_id}")
            return {"status": "ok"}

        if not business.is_active:
            print(f"⚠️  Business '{business.name}' is suspended")
            return {"status": "ok"}

        # Get decrypted token
        token = crud.get_decrypted_token(business)
        if not token:
            print(f"⚠️  Business '{business.name}' has no WhatsApp token")

        # Track customer in CRM
        customer = crud.get_or_create_customer(db, customer_phone, business.id)

        # Log to legacy chat_messages (keeps dashboard working)
        crud.log_message(db, business.id, customer_phone, "in", text)

        # Log to new messages table (CRM inbox)
        crud.create_message(db, customer.id, business.id, text, "incoming")

        # Generate reply
        products = crud.get_products(db, business.id)

        if "menu" in text.lower():
            if products:
                lines = "\n".join(
                    [f"{i+1}. {p.name} - ${p.price}" for i, p in enumerate(products)]
                )
                reply = (
                    f"📋 *{business.name} Menu*\n\n"
                    f"{lines}\n\n"
                    f"To order, type:\n*order <item> <quantity>*\n"
                    f"Example: order sadza 2"
                )
            else:
                reply = f"Hi! {business.name}'s menu is being updated. Check back soon! 🙏"

        elif text.lower().startswith("order "):
            parts = text.strip().split()
            if len(parts) < 3:
                reply = "❌ Please use the format:\norder <item> <quantity>\nExample: order sadza 2"
            else:
                try:
                    product_name = parts[1].lower()
                    qty = int(parts[2])
                    if qty <= 0:
                        raise ValueError("Quantity must be positive")
                    crud.create_order(db, business.id, OrderCreate(
                        customer_phone=customer_phone,
                        product_name=product_name,
                        quantity=qty,
                    ))
                    reply = (
                        f"✅ *Order confirmed!*\n\n"
                        f"{product_name.capitalize()} × {qty}\n\n"
                        f"Thank you for ordering from {business.name}! "
                        f"We'll be in touch shortly. 🙏"
                    )
                except ValueError:
                    reply = "❌ Invalid quantity. Use a whole number.\nExample: order sadza 2"

        else:
            reply = generate_reply(
                message=text,
                business_name=business.name,
                products=products,
            )

        # Log reply
        crud.log_message(db, business.id, customer_phone, "out", reply)
        crud.create_message(db, customer.id, business.id, reply, "outgoing")

        # Send
        if token:
            send_whatsapp(phone_number_id, token, customer_phone, reply)
        else:
            print(f"📵 Reply NOT sent (no token): {reply!r}")

        print(f"✅ [{business.name}] {customer_phone}: {text!r} → replied")

    except KeyError as e:
        print(f"⚠️  Webhook KeyError: {e} | data: {data}")
    except Exception as e:
        print(f"⚠️  Webhook error: {type(e).__name__}: {e}")

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
        "total_revenue": round(sum(o.total_price or 0 for o in orders), 2),
    }


# ─── BUSINESS: PROFILE ───────────────────────────────────
@app.get("/me", response_model=BusinessOut)
def get_me(db: Session = Depends(get_db), user=Depends(require_business)):
    b = crud.get_business_by_id(db, user["business_id"])
    if not b:
        raise HTTPException(status_code=404, detail="Not found")
    return b


@app.patch("/me", response_model=BusinessOut)
def update_me(data: BusinessUpdate, db: Session = Depends(get_db), user=Depends(require_business)):
    data_dict = data.dict(exclude_none=True)
    data_dict.pop("is_active", None)
    b = crud.update_business(db, user["business_id"], BusinessUpdate(**data_dict))
    return b


@app.get("/me/test-whatsapp")
def test_whatsapp_connection(db: Session = Depends(get_db), user=Depends(require_business)):
    b = crud.get_business_by_id(db, user["business_id"])
    if not b or not b.whatsapp_phone_id:
        return {"ok": False, "reason": "No Phone Number ID saved"}
    token = crud.get_decrypted_token(b)
    if not token:
        return {"ok": False, "reason": "No access token saved"}
    try:
        resp = http_requests.get(
            f"https://graph.facebook.com/v18.0/{b.whatsapp_phone_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        if resp.status_code == 200:
            return {"ok": True, "reason": "Connected"}
        err = resp.json().get("error", {}).get("message", "Unknown error")
        return {"ok": False, "reason": err}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


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


# ─── CONVERSATIONS (legacy dashboard) ────────────────────
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
    token = crud.get_decrypted_token(business)
    phones = crud.get_all_customer_phones(db, bid)

    if not phones:
        return {"sent": 0, "failed": 0, "total": 0, "message": "No customers found"}

    sent, failed, failed_numbers = 0, 0, []
    for phone in phones:
        try:
            send_whatsapp(business.whatsapp_phone_id, token, phone, body.message)
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


# ─── CHAT INBOX (CRM) ────────────────────────────────────
@app.get("/chat/customers")
def chat_customers(db: Session = Depends(get_db), user=Depends(get_current_user)):
    """List all tracked customers for this business."""
    customers = crud.get_customers_for_business(db, user["business_id"])
    return [
        {"id": c.id, "phone": c.phone, "customer_since": c.created_at}
        for c in customers
    ]


@app.get("/chat/conversations")
def chat_conversations(db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Inbox view: one row per customer with their last message preview."""
    return crud.get_chat_conversations(db, user["business_id"])


@app.get("/chat/messages/{customer_id}")
def chat_messages(customer_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Full message history for one customer (oldest first)."""
    customer = db.query(models.Customer).filter(
        models.Customer.id == customer_id,
        models.Customer.business_id == user["business_id"],
    ).first()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    msgs = crud.get_messages_by_customer(db, customer_id)
    return [
        {"id": m.id, "text": m.text, "direction": m.direction, "created_at": m.created_at}
        for m in msgs
    ]


# ─── STATIC + ROOT ───────────────────────────────────────
Base.metadata.create_all(bind=engine)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def root():
    return {"message": "WaziBot SaaS 🚀"}
