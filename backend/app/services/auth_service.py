from typing import Optional

from app.models.user import User


def authenticate_user(username: str, password: str) -> Optional[User]:
    from app.composition import user_repository

    user = user_repository.obtener_por_username(username)
    if user is None or not user.verificar_password(password):
        return None
    return user
