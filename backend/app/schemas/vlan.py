from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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


class VLANBatchChangeItem(BaseModel):
    """1 entry of ``POST /vlans/batch`` — a single VLAN operation. Either
    a create/update (``name`` set) or a delete (``eliminar=True``). Same
    shape used to be split across three endpoints (create/update/delete);
    keeping the union here lets one batch request mix all three on the
    same devices and land as 1 SSH session per device."""

    model_config = ConfigDict(json_schema_extra={
        "example": {"vlan_id": 100, "name": "MGMT"}
    })

    vlan_id: int = Field(..., ge=1, le=4094)
    name: Optional[str] = Field(default=None, min_length=1, max_length=32)
    eliminar: bool = False

    @field_validator('name', mode='before')
    @classmethod
    def strip_name(cls, v):
        return v.strip() if isinstance(v, str) else v

    @model_validator(mode='after')
    def _validate_shape(self):
        if self.eliminar:
            if self.name is not None:
                raise ValueError("'name' must not be provided when 'eliminar' is true")
        else:
            if self.name is None:
                raise ValueError("'name' is required for create/update (omit or set 'eliminar=true' to delete)")
        return self


class VLANBatchRequest(BaseModel):
    """Request body of ``POST /vlans/batch`` — N VLAN operations applied
    to N devices. For each device the backend enqueues **one** job
    carrying all N operations, so each device gets a **single** SSH
    session no matter how many VLANs are being touched. Same-shape
    endpoint as ``POST /devices/{name}/ports/batch``; the deletion path
    still requires admin on every target device, while a batch that only
    creates/updates falls back to operator."""

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "changes": [
                {"vlan_id": 10, "name": "MGMT"},
                {"vlan_id": 20, "name": "GUESTS"},
                {"vlan_id": 30, "eliminar": True},
            ],
            "devices": ["switch-01", "switch-02"],
        }
    })

    changes: list[VLANBatchChangeItem] = Field(..., min_length=1)
    devices: list[str] = Field(..., min_length=1)

    @model_validator(mode='after')
    def _unique_vlan_ids(self):
        ids = [c.vlan_id for c in self.changes]
        if len(ids) != len(set(ids)):
            raise ValueError("'changes' must not contain duplicate vlan_id entries")
        return self
