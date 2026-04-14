from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from typing import List
from pydantic import BaseModel
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
    require_superadmin, SUPER_ADMIN_USERNAME, SUPER_ADMIN_PASSWORD
)

app = FastAPI(title="WaziBot SaaS API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ─── SEND MESSAGE ────────────────────────────────────────
def send_message(business: models.Business, to: str, message: str):
    if not business.whatsapp_token or not business.whatsapp_phone_id:
        print(f"⚠️ Business {business.name} has no WhatsApp credentials")
        return
    url = f"https://graph.facebook.com/v18.0/{business.whatsapp_phone_id}/messages"
    headers = {"Authorization": f"Bearer {business.whatsapp_token}", "Content-Type": "application/json"}
    data = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": message}}
    resp = http_requests.post(url, headers=headers, json=data)
    return resp.json()

# ─── PUBLIC: SELF-SIGNUP ─────────────────────────────────
class SignupRequest(BaseModel):
    business_name: str
    username: str
    password: str
    whatsapp_phone_id: str = ""
    whatsapp_token: str = ""

@app.post("/auth/signup")
def signup(data: SignupRequest, db: Session = Depends(get_db)):
    # Validate inputs
    if len(data.business_name.strip()) < 2:
        raise HTTPException(status_code=400, detail="Business name too short")
    if len(data.username.strip()) < 3:
        raise HTTPException(status_code=400, detail="Username must be at least 3 characters")
    if len(data.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    # Check username not taken
    existing = crud.get_business_by_username(db, data.username.strip())
    if existing:
        raise HTTPException(status_code=400, detail="Username already taken")

    # Create business
    from schemas import BusinessCreate
    business = crud.create_business(db, BusinessCreate(
        name=data.business_name.strip(),
        owner_username=data.username.strip(),
        owner_password=data.password,
        whatsapp_phone_id=data.whatsapp_phone_id.strip() or None,
        whatsapp_token=data.whatsapp_token.strip() or None,
    ))

    # Issue token immediately — no approval needed
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
        "business_id": business.id,
        "message": "Account created successfully"
    }

# ─── AUTH: LOGIN ─────────────────────────────────────────
@app.post("/auth/login")
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    if form_data.username == SUPER_ADMIN_USERNAME:
        if not verify_password(form_data.password, SUPER_ADMIN_PASSWORD):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        token = create_access_token({"sub": form_data.username, "role": "superadmin"})
        return {"access_token": token, "token_type": "bearer", "role": "superadmin"}

    business = crud.get_business_by_username(db, form_data.username)
    if not business or not verify_password(form_data.password, business.owner_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not business.is_active:
        raise HTTPException(status_code=403, detail="Account suspended. Contact support.")
    token = create_access_token({
        "sub": form_data.username,
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
        entry = data["entry"][0]
        changes = entry["changes"][0]["value"]
        phone_number_id = changes["metadata"]["phone_number_id"]
        message_obj = changes["messages"][0]
        phone = message_obj["from"]
        text = message_obj["text"]["body"]

        business = crud.get_business_by_phone_id(db, phone_number_id)
        if not business or not business.is_active:
            print(f"⚠️ No active business for phone_id: {phone_number_id}")
            return {"status": "ok"}

        crud.log_message(db, business.id, phone, "in", text)

        if "menu" in text.lower():
            products = crud.get_products(db, business.id)
            if products:
                lines = "\n".join([f"{i+1}. {p.name} - ${p.price}" for i, p in enumerate(products)])
                reply = f"📋 {business.name} Menu:\n\n{lines}\n\nTo order: order <product> <quantity>"
            else:
                reply = "Our menu is being updated. Check back soon!"
        elif text.lower().startswith("order"):
            try:
                parts = text.strip().split()
                product_name = parts[1]
                qty = int(parts[2])
                crud.create_order(db, business.id, OrderCreate(
                    customer_phone=phone, product_name=product_name, quantity=qty
                ))
                reply = f"✅ Order placed: {product_name} x{qty}\nThank you for ordering from {business.name}!"
            except:
                reply = "Invalid format. Use: order <product> <quantity>"
        else:
            reply = generate_reply(text)

        crud.log_message(db, business.id, phone, "out", reply)
        send_message(business, phone, reply)
        print(f"✅ [{business.name}] {phone}: {text!r} → {reply!r}")

    except Exception as e:
        print(f"⚠️ Webhook error: {e}")

    return {"status": "ok"}

# ─── SUPERADMIN ───────────────────────────────────────────
@app.get("/admin/businesses", response_model=List[BusinessOut])
def list_businesses(db: Session = Depends(get_db), user=Depends(require_superadmin)):
    return crud.get_all_businesses(db)

@app.post("/admin/businesses", response_model=BusinessOut)
def create_business(data: BusinessCreate, db: Session = Depends(get_db), user=Depends(require_superadmin)):
    if crud.get_business_by_username(db, data.owner_username):
        raise HTTPException(status_code=400, detail="Username already taken")
    return crud.create_business(db, data)

@app.patch("/admin/businesses/{business_id}", response_model=BusinessOut)
def update_business(business_id: int, data: BusinessUpdate, db: Session = Depends(get_db), user=Depends(require_superadmin)):
    b = crud.update_business(db, business_id, data)
    if not b:
        raise HTTPException(status_code=404, detail="Not found")
    return b

@app.delete("/admin/businesses/{business_id}")
def delete_business(business_id: int, db: Session = Depends(get_db), user=Depends(require_superadmin)):
    b = crud.delete_business(db, business_id)
    if not b:
        raise HTTPException(status_code=404, detail="Not found")
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

# ─── BUSINESS: PROFILE ───────────────────────────────────
@app.get("/me", response_model=BusinessOut)
def get_me(db: Session = Depends(get_db), user=Depends(get_current_user)):
    if user["role"] == "superadmin":
        raise HTTPException(status_code=400, detail="Use admin routes")
    b = crud.get_business_by_id(db, user["business_id"])
    if not b:
        raise HTTPException(status_code=404, detail="Not found")
    return b

@app.patch("/me")
def update_me(data: BusinessUpdate, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if user["role"] == "superadmin":
        raise HTTPException(status_code=400, detail="Use admin routes")
    # Don't allow business owners to change is_active
    data.is_active = None
    b = crud.update_business(db, user["business_id"], data)
    return b

# ─── BUSINESS: PRODUCTS ──────────────────────────────────
@app.get("/products", response_model=List[ProductOut])
def get_products(db: Session = Depends(get_db), user=Depends(get_current_user)):
    return crud.get_products(db, user["business_id"])

@app.post("/products", response_model=ProductOut)
def create_product(product: ProductCreate, db: Session = Depends(get_db), user=Depends(get_current_user)):
    return crud.create_product(db, user["business_id"], product)

@app.delete("/products/{product_id}")
def delete_product(product_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    p = crud.delete_product(db, product_id, user["business_id"])
    if not p:
        raise HTTPException(status_code=404, detail="Not found")
    return {"deleted": product_id}

# ─── BUSINESS: ORDERS ────────────────────────────────────
@app.get("/orders", response_model=List[OrderOut])
def get_orders(db: Session = Depends(get_db), user=Depends(get_current_user)):
    return crud.get_orders(db, user["business_id"])

# ─── BUSINESS: CHAT ──────────────────────────────────────
@app.get("/conversations", response_model=List[ChatMessageOut])
def get_conversations(db: Session = Depends(get_db), user=Depends(get_current_user)):
    return crud.get_conversations(db, user["business_id"])

@app.get("/conversations/{phone}", response_model=List[ChatMessageOut])
def get_chat(phone: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    return crud.get_messages_for_phone(db, user["business_id"], phone)

# ─── BUSINESS: BROADCAST ─────────────────────────────────
class BroadcastRequest(BaseModel):
    message: str

@app.post("/broadcast")
def broadcast(body: BroadcastRequest, db: Session = Depends(get_db), user=Depends(get_current_user)):
    bid = user["business_id"]
    business = crud.get_business_by_id(db, bid)
    phones = crud.get_all_customer_phones(db, bid)
    if not phones:
        return {"sent": 0, "failed": 0, "message": "No customers found"}
    sent, failed, failed_numbers = 0, 0, []
    for phone in phones:
        try:
            send_message(business, phone, body.message)
            crud.log_message(db, bid, phone, "out", f"[BROADCAST] {body.message}")
            sent += 1
        except Exception as e:
            failed += 1
            failed_numbers.append(phone)
    return {"sent": sent, "failed": failed, "total": len(phones), "failed_numbers": failed_numbers}

@app.get("/customers")
def get_customers(db: Session = Depends(get_db), user=Depends(get_current_user)):
    phones = crud.get_all_customer_phones(db, user["business_id"])
    return {"phones": phones, "total": len(phones)}

# ─── STATIC + ROOT ───────────────────────────────────────
Base.metadata.create_all(bind=engine)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def root():
    return {"message": "WaziBot SaaS 🚀"}
