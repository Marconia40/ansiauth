import logging

from cryptography.fernet import Fernet

from app.core.config import FERNET_KEY

logger = logging.getLogger(__name__)

if not FERNET_KEY:
    raise RuntimeError("FERNET_KEY must be set in .env")


class SecretVault:
    """Encrypts/decrypts device passwords with a Fernet key held as real
    instance state — see docs/DEVICE_IMPLEMENTATION_PLAN.md D6. Replaces the
    module-level ``_fernet`` singleton that ``encrypt_password``/
    ``decrypt_password`` used to close over directly.
    """

    def __init__(self, key: str | bytes | None = None):
        key = key if key is not None else FERNET_KEY
        self._fernet = Fernet(key.encode() if isinstance(key, str) else key)

    def encrypt(self, plain: str) -> str:
        return self._fernet.encrypt(plain.encode()).decode()

    def decrypt(self, encrypted: str) -> str:
        return self._fernet.decrypt(encrypted.encode()).decode()


# Module-level singleton — every existing caller keeps importing these two
# functions unchanged; they now delegate to the class instead of holding
# their own module-global Fernet instance directly.
vault = SecretVault()


def encrypt_password(plain: str) -> str:
    return vault.encrypt(plain)


def decrypt_password(encrypted: str) -> str:
    return vault.decrypt(encrypted)
