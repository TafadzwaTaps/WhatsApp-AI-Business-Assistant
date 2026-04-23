"""
Token encryption using Fernet symmetric encryption.
Safe for production (Render, Railway, etc.)
"""

import os
import base64
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv

load_dotenv()

def get_fernet():
    raw_key = os.getenv("FERNET_KEY", "").strip()

    if not raw_key:
        raise RuntimeError("❌ FERNET_KEY is missing. Set it in environment variables.")

    try:
        # Ensure it's valid base64
        base64.urlsafe_b64decode(raw_key)

        # Ensure correct length (32 bytes after decoding)
        decoded = base64.urlsafe_b64decode(raw_key)
        if len(decoded) != 32:
            raise ValueError("Invalid key length")

    except Exception as e:
        raise RuntimeError(
            f"❌ Invalid FERNET_KEY format.\n"
            f"Must be 32 url-safe base64 bytes.\n"
            f"Generate one with:\n"
            f"python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"\n"
        ) from e

    return Fernet(raw_key.encode())


# Initialize once
_fernet = get_fernet()


def encrypt_token(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str) -> str:
    if not ciphertext:
        return ""

    try:
        return _fernet.decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        print("⚠️ Invalid token or wrong key used")
        return ""
    except Exception as e:
        print(f"⚠️ Decryption error: {e}")
        return ""


def is_encrypted(value: str) -> bool:
    return value.startswith("gAAA") if value else False