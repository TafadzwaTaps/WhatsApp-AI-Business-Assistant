# 1. Token encryption — never store plaintext tokens in DB
import os
from cryptography.fernet import Fernet

FERNET_KEY = os.environ["FERNET_KEY"]  # generate once: Fernet.generate_key()
f = Fernet(FERNET_KEY)

def encrypt_token(token: str) -> str:
    return f.encrypt(token.encode()).decode()

def decrypt_token(encrypted: str) -> str:
    return f.decrypt(encrypted.encode()).decode()

