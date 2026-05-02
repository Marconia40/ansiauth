import os

from dotenv import load_dotenv

load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY")
EXECUTION_MODE = os.getenv("EXECUTION_MODE", "mock")  # mock | real

_ANSIBLE_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "ansible")
)
ANSIBLE_BASE_PATH = _ANSIBLE_DIR
INVENTORY_PATH = os.path.join(_ANSIBLE_DIR, "inventory", "inventory.ini")
PLAYBOOKS_PATH = os.path.join(_ANSIBLE_DIR, "project")
