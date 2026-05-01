from pydantic import BaseModel


class DeviceCreate(BaseModel):
    name: str
    host: str
    vendor: str
    username: str
    password: str  # plain text — encrypted before storing


class DevicePublic(BaseModel):
    id: str
    name: str
    host: str
    vendor: str
    username: str
    # encrypted_password intentionally excluded from responses
