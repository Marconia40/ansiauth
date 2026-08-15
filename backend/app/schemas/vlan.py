from pydantic import BaseModel, ConfigDict, Field, field_validator


class VLANCreate(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {"vlan_id": 100, "name": "MGMT", "devices": ["switch-01"]}
    })

    vlan_id: int = Field(..., ge=1, le=4094)
    name: str = Field(..., min_length=1, max_length=32)
    devices: list[str] = Field(..., min_length=1)

    @field_validator('name', mode='before')
    @classmethod
    def strip_name(cls, v):
        return v.strip() if isinstance(v, str) else v


class VLANDelete(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {"devices": ["switch-01"]}
    })

    devices: list[str] = Field(..., min_length=1)


class VLANUpdate(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {"description": "Management VLAN", "devices": ["switch-01"]}
    })

    description: str = Field(..., max_length=64)
    devices: list[str] = Field(..., min_length=1)

    @field_validator('description', mode='before')
    @classmethod
    def strip_description(cls, v):
        return v.strip() if isinstance(v, str) else v
