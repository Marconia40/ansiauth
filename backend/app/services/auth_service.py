from typing import Optional

from app.schemas.user import UserRead
from app.services import user_service


def authenticate_user(username: str, password: str) -> Optional[UserRead]:
    return user_service.authenticate(username, password)
