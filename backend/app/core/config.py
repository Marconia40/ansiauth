import os

from dotenv import load_dotenv

load_dotenv()

FERNET_KEY = os.getenv("FERNET_KEY")
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY")

_raw_expiry = os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "15")
try:
    ACCESS_TOKEN_EXPIRE_MINUTES = int(_raw_expiry)
except ValueError:
    raise RuntimeError(
        f"ACCESS_TOKEN_EXPIRE_MINUTES must be an integer, got: {_raw_expiry!r}"
    )

EXECUTION_MODE = os.getenv("EXECUTION_MODE", "mock")  # mock | real

_DB_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "db"))
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{_DB_DIR}/app.db")

_ANSIBLE_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "ansible")
)
ANSIBLE_BASE_PATH = _ANSIBLE_DIR
INVENTORY_PATH = os.path.join(_ANSIBLE_DIR, "inventory", "inventory.ini")
PLAYBOOKS_PATH = os.path.join(_ANSIBLE_DIR, "project")

BOOTSTRAP_ADMIN_USER = os.getenv("BOOTSTRAP_ADMIN_USER", "admin")
BOOTSTRAP_ADMIN_PASSWORD = os.getenv("BOOTSTRAP_ADMIN_PASSWORD")

_raw_retention = os.getenv("AUDIT_RETENTION_DAYS", "90")
try:
    AUDIT_RETENTION_DAYS = int(_raw_retention)
    if AUDIT_RETENTION_DAYS < 1:
        raise ValueError
except ValueError:
    raise RuntimeError(
        f"AUDIT_RETENTION_DAYS must be a positive integer, got: {_raw_retention!r}"
    )
