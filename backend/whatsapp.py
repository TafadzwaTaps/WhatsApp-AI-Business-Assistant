# whatsapp.py
"""
WhatsApp webhook router + document sending helper.
All actual HTTP sending goes through main.send_whatsapp() or the helpers below.
"""

import logging
import requests

from fastapi import APIRouter, Request
import crud
from ai import generate_reply

log = logging.getLogger(__name__)
router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────
# WEBHOOK (kept for backwards-compat; primary webhook lives in main.py)
# ─────────────────────────────────────────────────────────────────────────────

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

        if "text" not in msg_obj:
            return {"status": "ignored non-text"}

        message = msg_obj.get("text", {}).get("body", "").strip()
        phone = msg_obj.get("from", "")
        wa_id = msg_obj.get("id")

        if not message:
            return {"status": "empty message"}

        if wa_id and crud.message_exists(wa_id):
            return {"status": "duplicate ignored"}

        business = crud.get_business_by_phone_id(phone_number_id)
        if not business:
            return {"error": "business not found"}

        business_id = business["id"]
        business_name = business["name"]

        try:
            crud.log_message(business_id, phone, "in", message)
        except Exception as e:
            log.warning("log_message failed: %s", e)

        products = crud.get_products(business_id)

        reply = generate_reply(
            message=message,
            phone=phone,
            business_id=business_id,
            business_name=business_name,
            products=products,
        )

        try:
            crud.log_message(business_id, phone, "out", reply)
        except Exception as e:
            log.warning("log_message failed: %s", e)

        token = crud.get_decrypted_token(business)
        if not token:
            return {"error": "missing token"}

        send_whatsapp_message(
            phone_number_id=phone_number_id,
            access_token=token,
            to=phone,
            message=reply,
        )

        return {"status": "sent"}

    except Exception as e:
        log.exception("🔥 Webhook Error: %s", e)
        return {"error": "server error"}


# ─────────────────────────────────────────────────────────────────────────────
# TEXT MESSAGE SENDER
# ─────────────────────────────────────────────────────────────────────────────

def send_whatsapp_message(
    phone_number_id: str,
    access_token: str,
    to: str,
    message: str,
) -> dict:
    """Send a plain text WhatsApp message via Meta Cloud API."""
    to = to.replace("whatsapp:", "").strip()
    url = f"https://graph.facebook.com/v18.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to":   to,
        "type": "text",
        "text": {"body": message},
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        result = resp.json()
        if resp.status_code != 200:
            log.error(
                "send_whatsapp_message failed  status=%s  error=%s",
                resp.status_code, result.get("error", {}),
            )
        return result
    except Exception as exc:
        log.exception("send_whatsapp_message exception: %s", exc)
        return {"error": str(exc)}


# ─────────────────────────────────────────────────────────────────────────────
# DOCUMENT SENDER
# ─────────────────────────────────────────────────────────────────────────────

def send_whatsapp_document(
    phone: str,
    file_path: str,
    access_token: str,
    phone_number_id: str,
    caption: str = "",
) -> dict:
    """
    Upload a file to Meta and send it as a WhatsApp document message.

    Steps:
      1. Upload file to /{phone_number_id}/media → get media_id
      2. Send document message using media_id

    Returns the final send response dict.
    """
    phone = phone.replace("whatsapp:", "").strip()
    headers_auth = {"Authorization": f"Bearer {access_token}"}

    # ── Step 1: Upload media ─────────────────────────────────────────────────
    upload_url = f"https://graph.facebook.com/v18.0/{phone_number_id}/media"
    try:
        with open(file_path, "rb") as f:
            upload_resp = requests.post(
                upload_url,
                headers=headers_auth,
                files={
                    "file": (
                        file_path.split("/")[-1],
                        f,
                        "application/pdf",
                    ),
                    "messaging_product": (None, "whatsapp"),
                },
                timeout=30,
            )
        upload_data = upload_resp.json()
        if upload_resp.status_code != 200 or "id" not in upload_data:
            log.error(
                "send_whatsapp_document: media upload failed  status=%s  resp=%s",
                upload_resp.status_code, upload_data,
            )
            return {"error": "Media upload failed", "details": upload_data}

        media_id = upload_data["id"]
        log.info("send_whatsapp_document: media uploaded  media_id=%s", media_id)

    except FileNotFoundError:
        log.error("send_whatsapp_document: file not found — %s", file_path)
        return {"error": f"File not found: {file_path}"}
    except Exception as exc:
        log.exception("send_whatsapp_document: upload exception — %s", exc)
        return {"error": str(exc)}

    # ── Step 2: Send document message ────────────────────────────────────────
    send_url = f"https://graph.facebook.com/v18.0/{phone_number_id}/messages"
    document_payload: dict = {
        "id":       media_id,
        "filename": file_path.split("/")[-1],
    }
    if caption:
        document_payload["caption"] = caption

    body = {
        "messaging_product": "whatsapp",
        "to":   phone,
        "type": "document",
        "document": document_payload,
    }
    try:
        send_resp = requests.post(
            send_url,
            headers={**headers_auth, "Content-Type": "application/json"},
            json=body,
            timeout=15,
        )
        result = send_resp.json()
        if send_resp.status_code != 200:
            log.error(
                "send_whatsapp_document: send failed  status=%s  error=%s",
                send_resp.status_code, result.get("error", {}),
            )
        else:
            log.info(
                "send_whatsapp_document: ✅ sent  phone=%s  media_id=%s",
                phone, media_id,
            )
        return result
    except Exception as exc:
        log.exception("send_whatsapp_document: send exception — %s", exc)
        return {"error": str(exc)}
