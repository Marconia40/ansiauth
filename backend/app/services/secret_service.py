import logging

from cryptography.fernet import Fernet

from app.core.config import FERNET_KEY

logger = logging.getLogger(__name__)

if not FERNET_KEY:
    raise RuntimeError("FERNET_KEY must be set in .env")

_fernet = Fernet(FERNET_KEY.encode() if isinstance(FERNET_KEY, str) else FERNET_KEY)


def encrypt_password(plain: str) -> str:
    return _fernet.encrypt(plain.encode()).decode()


def decrypt_password(encrypted: str) -> str:
    return _fernet.decrypt(encrypted.encode()).decode()
