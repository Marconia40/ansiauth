from pydantic import BaseModel


class Device(BaseModel):
    id: str
    ip: str
    type: str
    username: str
    password: str
