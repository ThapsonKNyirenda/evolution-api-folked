import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class TenantCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    helpdesk_tenant_id: Optional[uuid.UUID] = Field(default=None, description="UUID of the corresponding tenant in the main helpdesk system")


class TenantUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    helpdesk_tenant_id: Optional[uuid.UUID] = Field(default=None, description="UUID of the corresponding tenant in the main helpdesk system")


class TenantResponse(BaseModel):
    id: uuid.UUID
    name: str
    helpdesk_tenant_id: Optional[uuid.UUID] = None
    created_at: datetime
    updated_at: datetime

    model_config = {'from_attributes': True}
