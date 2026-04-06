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

    # Extract message (depends on Twilio/Meta format)
    message = data.get("Body", "")
    phone = data.get("From", "")

    # AI response
    reply = generate_reply(message)

    # Handle order
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

    return {"reply": reply}