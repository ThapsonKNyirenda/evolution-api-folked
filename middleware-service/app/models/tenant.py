import uuid
from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class Tenant(Base):
    __tablename__ = 'tenants'

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    helpdesk_tenant_slug: Mapped[str | None] = mapped_column(String(120), nullable=True, unique=True, index=True, comment='Tenant slug (ft_tenants.identifier) in the schema-per-tenant helpdesk; the only tenant handle sent to the backend')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    customers = relationship('Customer', back_populates='tenant')
    tickets = relationship('Ticket', back_populates='tenant')
    instance_links = relationship('InstanceTenant', back_populates='tenant')
    whatsapp_sessions = relationship('WhatsappSession', back_populates='tenant')
    phone_registries = relationship('PhoneRegistry', back_populates='tenant', cascade='all, delete-orphan')
    registered_users = relationship('RegisteredUser', back_populates='tenant', cascade='all, delete-orphan')
