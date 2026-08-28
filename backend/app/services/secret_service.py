import logging

from cryptography.fernet import Fernet

from app.core.config import FERNET_KEY

logger = logging.getLogger(__name__)

if not FERNET_KEY:
    raise RuntimeError("FERNET_KEY must be set in .env")


class SecretVault:
    """Encrypts/decrypts device passwords with a Fernet key held as real
    instance state — see docs/DEVICE_IMPLEMENTATION_PLAN.md D6.
    """

    def __init__(self, key: str | bytes | None = None):
        key = key if key is not None else FERNET_KEY
        self._fernet = Fernet(key.encode() if isinstance(key, str) else key)

    def encrypt(self, plain: str) -> str:
        return self._fernet.encrypt(plain.encode()).decode()

    def decrypt(self, encrypted: str) -> str:
        return self._fernet.decrypt(encrypted.encode()).decode()


vault = SecretVault()
