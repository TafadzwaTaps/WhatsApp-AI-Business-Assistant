from database import SessionLocal
import models

db = SessionLocal()

# 🔍 Check businesses (WhatsApp credentials)
businesses = db.query(models.Business).all()

for b in businesses:
    print("----")
    print("ID:", b.id)
    print("Name:", b.name)
    print("Phone ID:", b.whatsapp_phone_id)
    print("Token:", b.whatsapp_token)