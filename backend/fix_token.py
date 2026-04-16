"""
Run this ONCE to fix your WhatsApp token in the database.

Your token was previously set to "UPDATE_YOUR_TOKEN_HERE" which is why
replies weren't being sent to phones. This script replaces it with
the real token and encrypts it properly.

Usage:
    python fix_token.py

Make sure your .env file has FERNET_KEY set before running.
"""
import sqlite3
import os
import sys
from dotenv import load_dotenv

load_dotenv()

# ── PASTE YOUR REAL TOKEN HERE ────────────────────────────
REAL_TOKEN = "EAAnCPODMFHgBRDJb1EdZA2oej987ZAZBzBRXgS95VWRS3CeMBDdFljn01t84NZCG2JfkoWOKSSm8LagnqZClZCNCReDVZALvYdbDLi5JN01P29iiOY5i7iVzsixD1TlsGMrcmocZAUXdPzNjZAfvLBSAYFJQhctK9XxpdLr2bRYCwZAObfFj6qPXn82AwDOscIfAZDZD"
PHONE_NUMBER_ID = "1088821460973757"  # your phone number ID — verify this is correct
# ─────────────────────────────────────────────────────────

if REAL_TOKEN == "PASTE_YOUR_META_ACCESS_TOKEN_HERE":
    print("❌ Edit fix_token.py and paste your real Meta access token first!")
    sys.exit(1)

# Encrypt the token
try:
    from crypto import encrypt_token
    encrypted = encrypt_token(REAL_TOKEN)
    print(f"✅ Token encrypted successfully")
except Exception as e:
    print(f"⚠️  Could not encrypt (cryptography not installed?): {e}")
    print("   Storing plaintext token — install cryptography for encryption")
    encrypted = REAL_TOKEN

# Update in database
conn = sqlite3.connect("app.db")
c = conn.cursor()

# Check current state
c.execute("SELECT id, name, owner_username, whatsapp_phone_id, whatsapp_token FROM businesses")
rows = c.fetchall()
print(f"\nCurrent businesses in DB:")
for row in rows:
    token_preview = row[4][:20] + "..." if row[4] else "None"
    print(f"  id={row[0]} | {row[2]} | phone_id={row[3]} | token={token_preview}")

# Update business id=1 (the default one)
c.execute(
    "UPDATE businesses SET whatsapp_token=?, whatsapp_phone_id=? WHERE id=1",
    (encrypted, PHONE_NUMBER_ID)
)
conn.commit()
print(f"\n✅ Business id=1 updated with real token and phone_id={PHONE_NUMBER_ID}")

# Verify
c.execute("SELECT whatsapp_token FROM businesses WHERE id=1")
stored = c.fetchone()[0]
print(f"   Stored token preview: {stored[:30]}...")

conn.close()
print("\n🎉 Done! Restart uvicorn and test WhatsApp — replies should now reach your phone.")
