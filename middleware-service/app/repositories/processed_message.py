from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.processed_message import ProcessedMessage


class ProcessedMessageRepository:
    def __init__(self, db: Session):
        self.db = db

    def claim(self, instance_name: str, message_id: str) -> bool:
        """Atomically record a message as handled. False if it was already claimed."""
        self.db.add(ProcessedMessage(instance_name=instance_name, message_id=message_id))
        try:
            self.db.commit()
            return True
        except IntegrityError:
            self.db.rollback()
            return False

    def purge_older_than(self, days: int = 7) -> int:
        cutoff = datetime.utcnow() - timedelta(days=days)
        n = self.db.query(ProcessedMessage).filter(ProcessedMessage.created_at < cutoff).delete()
        self.db.commit()
        return n
