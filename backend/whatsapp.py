from fastapi import APIRouter, Request, Depends
from sqlalchemy.orm import Session
from database import SessionLocal
import crud, schemas
from ai import generate_reply

router = APIRouter()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.post("/webhook")
async def whatsapp_webhook(request: Request, db: Session = Depends(get_db)):
    data = await request.json()

    try:
        # =========================================
        # 🟢 META FORMAT (WhatsApp Cloud API)
        # =========================================
        entry = data.get("entry", [None])[0]
        change = entry.get("changes", [None])[0] if entry else None
        value = change.get("value", {}) if change else {}

        phone_number_id = value.get("metadata", {}).get("phone_number_id")
        messages = value.get("messages", [])

        if messages:
            message = messages[0].get("text", {}).get("body", "")
            phone = messages[0].get("from", "")
        else:
            # =========================================
            # 🔵 FALLBACK (Twilio format)
            # =========================================
            message = data.get("Body", "")
            phone = data.get("From", "")
            phone_number_id = None

        print("📩 Message:", message)

        # =========================================
        # 🔎 MULTI-USER: GET CREDENTIALS
        # =========================================
        access_token = None

        if phone_number_id:
            creds = crud.get_credentials_by_phone_id(db, phone_number_id)
            if creds:
                access_token = creds.access_token

        # =========================================
        # 🤖 AI RESPONSE
        # =========================================
        reply = generate_reply(message)

        # =========================================
        # 🛒 ORDER LOGIC (UNCHANGED)
        # =========================================
        if message.lower().startswith("order"):
            try:
                _, product, qty = message.split()
                order = schemas.OrderCreate(
                    customer_phone=phone,
                    product_name=product,
                    quantity=int(qty)
                )
                crud.create_order(db, order)
                reply = f"✅ Order placed: {product} x{qty}"
            except:
                reply = "❌ Invalid format. Use: order <product> <quantity>"

        # =========================================
        # 📤 SEND RESPONSE (META ONLY)
        # =========================================
        if phone_number_id and access_token:
            from meta_sender import send_whatsapp_message

            send_whatsapp_message(
                phone_number_id=phone_number_id,
                access_token=access_token,
                to=phone,
                message=reply
            )

        # Twilio auto-responds differently, so just return
        return {"reply": reply}

    except Exception as e:
        print("🔥 Error:", str(e))
        return {"error": "Something went wrong"}