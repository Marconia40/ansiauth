import logging
import os

from cryptography.fernet import Fernet
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_raw_key = os.getenv("SECRET_KEY")
if not _raw_key:
    _raw_key = Fernet.generate_key().decode()
    logger.warning("SECRET_KEY not set — using ephemeral key (credentials will not survive restart)")

_fernet = Fernet(_raw_key.encode() if isinstance(_raw_key, str) else _raw_key)


def encrypt_password(plain: str) -> str:
    return _fernet.encrypt(plain.encode()).decode()


def decrypt_password(encrypted: str) -> str:
    return _fernet.decrypt(encrypted.encode()).decode()
