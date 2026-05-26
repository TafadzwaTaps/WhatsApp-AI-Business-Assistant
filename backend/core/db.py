"""
db.py — Supabase client singleton.

All queries are done via supabase-py (postgrest under the hood).

Required environment variables (Render → Environment, or .env locally):
    SUPABASE_URL   https://<project-ref>.supabase.co
    SUPABASE_KEY   service_role key  (NOT the anon key — bypasses RLS)

Find them in:
    Supabase Dashboard → Project Settings → API
"""

import os
import re
import logging
import sys

from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)


# ── Startup helpers ───────────────────────────────────────────────────────────

def _fatal(msg: str) -> None:
    log.critical("❌  STARTUP FAILURE — db.py\n%s", msg)
    sys.exit(1)


def _validate_supabase_url(url: str) -> None:
    """
    Validate SUPABASE_URL before attempting a connection.
    Catches the most common Render misconfiguration mistakes.
    Calls sys.exit(1) with a clear human-readable message on any problem.
    """
    if not url:
        _fatal(
            "SUPABASE_URL is not set.\n"
            "\n"
            "  ➜  Add it in Render → Your Service → Environment → Add Variable\n"
            "       Key  : SUPABASE_URL\n"
            "       Value: https://<your-project-ref>.supabase.co\n"
            "\n"
            "  ➜  Find your URL in:\n"
            "       Supabase Dashboard → Project Settings → API → Project URL"
        )

    if "xxxx" in url.lower():
        _fatal(
            f"SUPABASE_URL still contains the placeholder value: {url!r}\n"
            "\n"
            "  ➜  Replace it with your real Supabase project URL.\n"
            "  ➜  Example: https://abcdefghijklmnop.supabase.co\n"
            "  ➜  Find it in Supabase Dashboard → Project Settings → API → Project URL"
        )

    if not url.startswith("https://"):
        _fatal(
            f"SUPABASE_URL must start with 'https://'. Got: {url!r}\n"
            "\n"
            "  ➜  Example: https://abcdefghijklmnop.supabase.co\n"
            "  ➜  Make sure you copied the full URL including https://"
        )

    if not re.search(r"https://[a-z0-9]+\.supabase\.co", url.rstrip("/")):
        _fatal(
            f"SUPABASE_URL doesn't look like a valid Supabase URL: {url!r}\n"
            "\n"
            "  ➜  Expected format: https://<project-ref>.supabase.co\n"
            "       where <project-ref> is 20 lowercase alphanumeric characters\n"
            "  ➜  Find it in Supabase Dashboard → Project Settings → API → Project URL\n"
            "  ➜  Make sure there are no extra characters, spaces, or trailing slashes"
        )


def _validate_supabase_key(key: str) -> None:
    """
    Validate SUPABASE_KEY format.
    Supabase JWTs (both anon and service_role) always start with 'eyJ'.
    """
    if not key:
        _fatal(
            "SUPABASE_KEY is not set.\n"
            "\n"
            "  ➜  Add it in Render → Your Service → Environment → Add Variable\n"
            "       Key  : SUPABASE_KEY\n"
            "       Value: <your service_role JWT>\n"
            "\n"
            "  ➜  Find it in:\n"
            "       Supabase Dashboard → Project Settings → API → service_role (secret)\n"
            "\n"
            "  ⚠   Use the service_role key, NOT the anon key.\n"
            "      The service_role key bypasses Row Level Security."
        )

    if not key.startswith("eyJ"):
        _fatal(
            f"SUPABASE_KEY does not look like a valid JWT.\n"
            f"  Got prefix: {key[:12]!r}  (length: {len(key)})\n"
            "\n"
            "  ➜  A valid Supabase key always starts with 'eyJ'\n"
            "  ➜  Find it in Supabase Dashboard → Project Settings → API → service_role (secret)\n"
            "  ➜  Make sure you copied the complete key — it should be ~200+ characters\n"
            "  ⚠   No extra spaces, newlines, or quotes around the value"
        )


# ── Initialisation ────────────────────────────────────────────────────────────

def _init() -> Client:
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_KEY", "").strip()

    # Validate before attempting connection — gives a clear, actionable error
    # message instead of the cryptic "[Errno -2] Name or service not known"
    # DNS failure that happens at the first query if the URL is wrong.
    _validate_supabase_url(url)
    _validate_supabase_key(key)

    try:
        client = create_client(url, key)
    except Exception as exc:
        _fatal(
            f"Supabase client creation failed: {exc}\n"
            f"\n"
            f"  URL : {url[:60]}\n"
            f"  Key : {key[:12]}… (length {len(key)})\n"
            "\n"
            "  ➜  Verify both values in Render → Environment are correct and saved."
        )

    log.info("🟢 Supabase client initialised  url=%s…", url[:50])
    return client


# One client for the whole process — thread-safe for reads.
supabase: Client = _init()
