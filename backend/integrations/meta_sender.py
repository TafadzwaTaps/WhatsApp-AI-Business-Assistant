# meta_sender.py
"""
STUB FILE — all WhatsApp sending is handled elsewhere.

Sending hierarchy (in order of preference):
  1. services.WhatsAppService.send()       ← new service layer (preferred for new code)
  2. main.send_whatsapp()                  ← existing direct caller (still works)
  3. whatsapp.send_whatsapp_message()      ← low-level HTTP call

DO NOT add tokens or credentials here.
DO NOT import this file — it is documentation only.

Quick send test (dev only):
  python -c "
  from services.whatsapp_service import WhatsAppService
  WhatsAppService.send('YOUR_PHONE_NUMBER_ID', 'YOUR_TOKEN', '263771234567', 'Test')
  "
"""
