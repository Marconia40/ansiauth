from app.core.security import hash_password, verify_password

_users = {
    "admin": {
        "username": "admin",
        "hashed_password": hash_password("admin123"),
        "role": "admin",
    },
    "operator": {
        "username": "operator",
        "hashed_password": hash_password("operator123"),
        "role": "operator",
    },
    "observer": {
        "username": "observer",
        "hashed_password": hash_password("observer123"),
        "role": "observer",
    },
}


def authenticate_user(username: str, password: str):
    user = _users.get(username)
    if not user:
        return None
    if not verify_password(password, user["hashed_password"]):
        return None
    return user
