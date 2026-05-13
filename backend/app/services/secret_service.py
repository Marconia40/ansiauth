import logging

from cryptography.fernet import Fernet

from app.core.config import SECRET_KEY

logger = logging.getLogger(__name__)

if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY must be set in .env")

_fernet = Fernet(SECRET_KEY.encode() if isinstance(SECRET_KEY, str) else SECRET_KEY)


def encrypt_password(plain: str) -> str:
    return _fernet.encrypt(plain.encode()).decode()


def decrypt_password(encrypted: str) -> str:
    return _fernet.decrypt(encrypted.encode()).decode()
