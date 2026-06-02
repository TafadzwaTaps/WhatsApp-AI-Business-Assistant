"""
routes/_deps.py — Shared dependencies for all route modules.

Every router imports from here rather than duplicating imports.
main.py injects `app`, `manager`, `send_whatsapp`, `_send_direct`,
`_log_event`, `SHARED_*`, `INVOICES_DIR`, `STATIC_DIR` at startup via
routes._deps after creating the FastAPI app.  Routers access them as:

    from routes._deps import manager, send_whatsapp, log, ...
"""

import logging

# These are populated by main.py after app creation
app          = None        # FastAPI app — set by main.py
manager      = None        # ConnectionManager — set by main.py
send_whatsapp  = None      # WhatsApp sender — set by main.py
_send_direct   = None      # Direct sender — set by main.py
_log_event     = None      # Event logger — set by main.py
_token_pair    = None      # JWT pair builder — set by main.py
STATIC_DIR     = None      # Static files directory — set by main.py
INVOICES_DIR   = None      # Invoice output directory — set by main.py
WHATSAPP_APP_SECRET = ""   # Webhook signature secret — set by main.py
SHARED_PHONE_NUMBER_ID = ""
SHARED_WA_TOKEN        = ""
SHARED_WA_PHONE        = ""

log = logging.getLogger("wazibot")
