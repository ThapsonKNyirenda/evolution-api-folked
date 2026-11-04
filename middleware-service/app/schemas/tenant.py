import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class TenantCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    helpdesk_tenant_slug: Optional[str] = Field(default=None, min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9-]*$", description="Tenant slug in the helpdesk (e.g. 'acme')")


class TenantUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    helpdesk_tenant_slug: Optional[str] = Field(default=None, min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9-]*$", description="Tenant slug in the helpdesk (e.g. 'acme')")


class TenantResponse(BaseModel):
    id: uuid.UUID
    name: str
    helpdesk_tenant_slug: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = {'from_attributes': True}
