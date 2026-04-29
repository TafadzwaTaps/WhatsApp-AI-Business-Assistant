from fastapi import APIRouter, Request
import requests
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
    
def send_whatsapp_document(phone, file_path, token, phone_id):

    # Upload file
    upload_url = f"https://graph.facebook.com/v18.0/{phone_id}/media"

    files = {
        'file': open(file_path, 'rb')
    }

    headers = {
        "Authorization": f"Bearer {token}"
    }

    upload_res = requests.post(upload_url, headers=headers, files=files)
    media_id = upload_res.json().get("id")

    # Send doc
    url = f"https://graph.facebook.com/v18.0/{phone_id}/messages"

    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "document",
        "document": {
            "id": media_id,
            "filename": file_path.split("/")[-1]
        }
    }

    requests.post(url, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }, json=payload)    