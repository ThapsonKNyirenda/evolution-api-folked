import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models.registered_user import RegisteredUser


class RegisteredUserRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_by_phone_and_tenant(self, phone_number: str, tenant_id: uuid.UUID) -> Optional[RegisteredUser]:
        return self.db.query(RegisteredUser).filter(
            RegisteredUser.phone_number == phone_number,
            RegisteredUser.tenant_id == tenant_id,
        ).first()

    def get_or_create(self, phone_number: str, tenant_id: uuid.UUID, helpdesk_user_id: uuid.UUID, **kwargs) -> RegisteredUser:
        existing = self.get_by_phone_and_tenant(phone_number, tenant_id)
        if existing:
            # Update with latest info
            existing.helpdesk_user_id = helpdesk_user_id
            for key, value in kwargs.items():
                if hasattr(existing, key) and value is not None:
                    setattr(existing, key, value)
            self.db.commit()
            self.db.refresh(existing)
            return existing

        new_user = RegisteredUser(
            phone_number=phone_number,
            tenant_id=tenant_id,
            helpdesk_user_id=helpdesk_user_id,
            **kwargs,
        )
        self.db.add(new_user)
        self.db.commit()
        self.db.refresh(new_user)
        return new_user

    def update_helpdesk_id(self, registered_user_id: uuid.UUID, helpdesk_user_id: uuid.UUID) -> Optional[RegisteredUser]:
        user = self.db.query(RegisteredUser).filter(RegisteredUser.id == registered_user_id).first()
        if user:
            user.helpdesk_user_id = helpdesk_user_id
            self.db.commit()
            self.db.refresh(user)
        return user

    def deactivate(self, registered_user_id: uuid.UUID) -> Optional[RegisteredUser]:
        user = self.db.query(RegisteredUser).filter(RegisteredUser.id == registered_user_id).first()
        if user:
            user.is_active = False
            self.db.commit()
            self.db.refresh(user)
        return user

    def list_by_tenant(self, tenant_id: uuid.UUID, active_only: bool = True) -> list[RegisteredUser]:
        query = self.db.query(RegisteredUser).filter(RegisteredUser.tenant_id == tenant_id)
        if active_only:
            query = query.filter(RegisteredUser.is_active.is_(True))
        return query.all()