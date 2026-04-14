# meta_sender.py
# NOTE: This file is kept for backwards compatibility only.
# In the multi-tenant system, WhatsApp messages are sent directly
# via send_message() in main.py using per-business credentials.
# Do NOT put credentials here anymore.

import requests

def send_whatsapp_message(phone_number_id: str, token: str, to: str, message: str):
    """Send a WhatsApp message using explicit credentials (no hardcoded tokens)."""
    url = f"https://graph.facebook.com/v18.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    data = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": message}
    }
    response = requests.post(url, headers=headers, json=data, timeout=10)
    return response.json()
