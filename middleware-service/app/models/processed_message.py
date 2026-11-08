import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class ProcessedMessage(Base):
    """Idempotency ledger: one row per inbound WhatsApp message already handled.

    Scoped by Evolution instance (which maps to exactly one tenant), so message
    ids from different tenants can never collide.
    """

    __tablename__ = 'processed_messages'
    __table_args__ = (
        UniqueConstraint('instance_name', 'message_id', name='uq_processed_message_instance_msg'),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    instance_name: Mapped[str] = mapped_column(String(255), nullable=False)
    message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)
