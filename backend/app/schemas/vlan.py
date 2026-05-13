from pydantic import BaseModel, Field


class VLANCreate(BaseModel):
    vlan_id: int = Field(..., ge=1, le=4094)
    name: str = Field(..., min_length=1, max_length=32)
    devices: list[str] = Field(..., min_length=1)


class VLANDelete(BaseModel):
    devices: list[str] = Field(..., min_length=1)


class VLANUpdate(BaseModel):
    description: str = Field(..., max_length=64)
    devices: list[str] = Field(..., min_length=1)
