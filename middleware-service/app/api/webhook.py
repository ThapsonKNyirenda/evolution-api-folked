import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.schemas.conversation import ConversationMessage, ConversationStateResponse
from app.services.conversation_service import ConversationService
from app.services.evolution_api import EvolutionAPIService
from app.repositories import EventLogRepository, InstanceTenantRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix='/webhook', tags=['webhook'])


@router.post('/evolution')
async def receive_evolution_webhook(data: ConversationMessage, db: Session = Depends(get_db)):
    link = InstanceTenantRepository(db).get_by_instance(data.instance_name)
    if not link:
        raise HTTPException(status_code=400, detail='Instance not linked to any tenant')

    try:
        reply = ConversationService(db).process_message(
            instance_name=data.instance_name,
            phone_number=data.phone_number,
            text=data.text,
            message_id=data.message_id,
            push_name=data.push_name,
        )

        session = ConversationService(db).sessions.get_or_create(data.phone_number, link.tenant_id)

        evo = EvolutionAPIService()
        try:
            await evo.send_message(data.instance_name, data.phone_number, reply)
        except Exception as e:
            logger.warning('Failed to send reply via Evolution API: %s', e)

        return ConversationStateResponse(
            session_id=session.id,
            phone_number=data.phone_number,
            tenant_id=link.tenant_id,
            customer_id=session.customer_id,
            state=session.state,
            ticket_draft=session.ticket_draft,
            reply=reply.get('text', ''),
        )
    except Exception as e:
        logger.error('Error processing conversation: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail=f'Error processing message: {e}')


@router.post('/evolution/raw')
async def receive_raw_evolution_event(
    instance_name: str,
    event_type: str,
    phone_number: str,
    text: str,
    message_id: str | None = None,
    db: Session = Depends(get_db),
):
    link = InstanceTenantRepository(db).get_by_instance(instance_name)
    if not link:
        raise HTTPException(status_code=400, detail='Instance not linked to any tenant')

    reply = ConversationService(db).process_message(
        instance_name=instance_name,
        phone_number=phone_number,
        text=text,
        message_id=message_id,
        push_name=None,
    )

    evo = EvolutionAPIService()
    try:
        await evo.send_message(instance_name, phone_number, reply)
    except Exception as e:
        logger.warning('Failed to send reply: %s', e)

    return {
        'status': 'processed',
        'instance': instance_name,
        'reply': reply.get('text', ''),
    }
