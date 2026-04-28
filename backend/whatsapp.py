from fastapi import APIRouter, Request
import crud
from ai import generate_reply

router = APIRouter()


@router.post("/webhook")
async def whatsapp_webhook(request: Request):
    data = await request.json()

    try:
        entry = data.get("entry", [None])[0]
        change = entry.get("changes", [None])[0] if entry else None
        value = change.get("value", {}) if change else {}

        phone_number_id = value.get("metadata", {}).get("phone_number_id")
        messages = value.get("messages", [])

        if not messages:
            return {"status": "no message"}

        msg_obj = messages[0]

        # ❌ ignore non-text
        if "text" not in msg_obj:
            return {"status": "ignored non-text"}

        message = msg_obj.get("text", {}).get("body", "").strip()
        phone = msg_obj.get("from", "")
        wa_id = msg_obj.get("id")

        if not message:
            return {"status": "empty message"}

        # 🔥 deduplication (CRITICAL)
        if wa_id and crud.message_exists(wa_id):
            return {"status": "duplicate ignored"}

        business = crud.get_business_by_phone_id(phone_number_id)

        if not business:
            return {"error": "business not found"}

        business_id = business["id"]
        business_name = business["name"]

        # safe logging
        try:
            crud.log_message(business_id, phone, "in", message)
        except Exception as e:
            print("log_message failed:", e)

        products = crud.get_products(business_id)

        reply = generate_reply(
            message=message,
            phone=phone,
            business_id=business_id,
            business_name=business_name,
            products=products
        )

        try:
            crud.log_message(business_id, phone, "out", reply)
        except Exception as e:
            print("log_message failed:", e)

        token = crud.get_decrypted_token(business)

        if not token:
            return {"error": "missing token"}

        from meta_sender import send_whatsapp_message

        send_whatsapp_message(
            phone_number_id=phone_number_id,
            access_token=token,
            to=phone,
            message=reply
        )

        return {"status": "sent"}

    except Exception as e:
        print("🔥 Webhook Error:", str(e))
        return {"error": "server error"}