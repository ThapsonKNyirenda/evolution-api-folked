"""
Phone Registry Repository

Provides CRUD operations for the phone_registry table,
which maps phone numbers to customer records for quick lookup
during ticket creation and message processing.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.phone_registry import PhoneRegistry


class PhoneRegistryRepository:
    """Repository for managing phone-to-customer mappings."""

    def __init__(self, db: Session):
        self.db = db

    def get_by_phone_and_tenant(
        self, phone_number: str, tenant_id: uuid.UUID
    ) -> PhoneRegistry | None:
        """Look up a phone registry entry by phone number and tenant."""
        return self.db.query(PhoneRegistry).filter(
            PhoneRegistry.phone_number == phone_number,
            PhoneRegistry.tenant_id == tenant_id,
            PhoneRegistry.is_active == True,  # noqa: E712
        ).first()

    def get_or_create(
        self,
        phone_number: str,
        tenant_id: uuid.UUID,
        customer_id: uuid.UUID,
        helpdesk_customer_id: uuid.UUID | None = None,
    ) -> PhoneRegistry:
        """
        Find an existing phone registry entry for the given phone + tenant,
        or create a new one. Updates last_seen_at on each access.
        """
        entry = self.get_by_phone_and_tenant(phone_number, tenant_id)
        now = datetime.utcnow()

        if entry:
            entry.last_seen_at = now
            # Update helpdesk_customer_id if it was missing
            if helpdesk_customer_id and not entry.helpdesk_customer_id:
                entry.helpdesk_customer_id = helpdesk_customer_id
            self.db.flush()
            return entry

        entry = PhoneRegistry(
            phone_number=phone_number,
            tenant_id=tenant_id,
            customer_id=customer_id,
            helpdesk_customer_id=helpdesk_customer_id,
            is_active=True,
            first_seen_at=now,
            last_seen_at=now,
        )
        self.db.add(entry)
        self.db.flush()
        self.db.refresh(entry)
        return entry

    def update_helpdesk_id(
        self, entry_id: uuid.UUID, helpdesk_customer_id: uuid.UUID
    ) -> PhoneRegistry | None:
        """Update the helpdesk customer ID for a registry entry."""
        entry = self.db.query(PhoneRegistry).filter(PhoneRegistry.id == entry_id).first()
        if entry:
            entry.helpdesk_customer_id = helpdesk_customer_id
            self.db.flush()
        return entry

    def deactivate(self, phone_number: str, tenant_id: uuid.UUID) -> None:
        """Mark all registry entries for a phone + tenant as inactive."""
        self.db.query(PhoneRegistry).filter(
            PhoneRegistry.phone_number == phone_number,
            PhoneRegistry.tenant_id == tenant_id,
        ).update({PhoneRegistry.is_active: False})
        self.db.flush()

    def list_by_tenant(self, tenant_id: uuid.UUID) -> list[PhoneRegistry]:
        """List all active registry entries for a tenant."""
        return self.db.query(PhoneRegistry).filter(
            PhoneRegistry.tenant_id == tenant_id,
            PhoneRegistry.is_active == True,  # noqa: E712
        ).all()
