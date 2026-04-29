from pydantic import BaseModel, Field

class VLANCreate(BaseModel):
    vlan_id: int = Field(..., ge=1, le=4094)
    name: str = Field(..., min_length=1, max_length=32)
    device: str

class VLANUpdate(BaseModel):
    description: str = Field(..., max_length=64)
    device: str
