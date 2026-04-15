"""
Token encryption using Fernet symmetric encryption.
Tokens are encrypted before storing in DB and decrypted on read.

Setup:
1. pip install cryptography
2. Generate a key: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
3. Add FERNET_KEY=<that key> to your .env file
"""
import os
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv

load_dotenv()

_raw_key = os.getenv("FERNET_KEY", "")

# If no key is set, generate one for this session and warn loudly
if not _raw_key:
    _raw_key = Fernet.generate_key().decode()
    print("⚠️  WARNING: FERNET_KEY not set in .env — using a temporary key.")
    print(f"⚠️  Tokens encrypted this session CANNOT be decrypted after restart.")
    print(f"⚠️  Add this to your .env: FERNET_KEY={_raw_key}")

_fernet = Fernet(_raw_key.encode() if isinstance(_raw_key, str) else _raw_key)


def encrypt_token(plaintext: str) -> str:
    """Encrypt a WhatsApp access token before storing in DB."""
    if not plaintext:
        return ""
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str) -> str:
    """Decrypt a WhatsApp access token retrieved from DB."""
    if not ciphertext:
        return ""
    try:
        return _fernet.decrypt(ciphertext.encode()).decode()
    except (InvalidToken, Exception) as e:
        print(f"⚠️  Token decryption failed: {e}")
        return ""


def is_encrypted(value: str) -> bool:
    """Check if a string looks like a Fernet token (starts with gAAA)."""
    return value.startswith("gAAA") if value else False
