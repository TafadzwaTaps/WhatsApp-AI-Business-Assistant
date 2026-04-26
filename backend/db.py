"""
db.py — Supabase client singleton.

Replaces database.py + models.py entirely.
All queries are done via supabase-py (postgrest under the hood).

Required environment variables:
    SUPABASE_URL        https://xxxx.supabase.co
    SUPABASE_KEY        service_role key  (NOT the anon key — bypasses RLS)

The service_role key is found in:
    Supabase Dashboard → Project Settings → API → service_role (secret)

Never expose the service_role key to the browser.
"""

import os
import logging
import sys

from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)


def _init() -> Client:
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_KEY", "").strip()   # service_role key

    missing = [k for k, v in {"SUPABASE_URL": url, "SUPABASE_KEY": key}.items() if not v]
    if missing:
        log.critical(
            "❌ STARTUP FAILURE — db.py: missing env vars: %s\n"
            "  Set them in Render → Environment (or .env for local dev).",
            ", ".join(missing),
        )
        sys.exit(1)

    client = create_client(url, key)
    log.info("🟢 Supabase connected  url=%s…", url[:40])
    return client


# One client for the whole process — thread-safe for reads, fine for writes
# because supabase-py creates a new HTTP connection per request internally.
supabase: Client = _init()
