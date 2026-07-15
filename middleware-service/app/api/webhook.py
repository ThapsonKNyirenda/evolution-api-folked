import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.schemas.conversation import ConversationMessage, ConversationStateResponse
from app.services.conversation_service import ConversationService
from app.services.evolution_api import EvolutionAPIService
from app.services.evolution_event_parser import EvolutionEventParser
from app.repositories import EventLogRepository, InstanceTenantRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix='/webhook', tags=['webhook'])

# Events that we want to process (from customers, not bot itself)
PROCESSED_EVENTS = {'messages.upsert'}


@router.post('/evolution')
async def receive_evolution_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Receive webhook events from Evolution API.
    
    Evolution API sends a nested payload:
    {
        "event": "messages.upsert",
        "instance": "Helpdesk-Integration",
        "data": { "key": {...}, "message": {...}, "pushName": "..." },
        ...
    }
    """
    try:
        payload = await request.json()
    except Exception as e:
        logger.error('Failed to parse webhook payload JSON: %s', e)
        raise HTTPException(status_code=400, detail='Invalid JSON payload')

    event = payload.get('event', '')
    instance_name = payload.get('instance', '')

    if not instance_name:
        logger.warning('Webhook received without instance name')
        return {'status': 'ignored', 'reason': 'no_instance'}

    # Check if instance is linked to a tenant
    link = InstanceTenantRepository(db).get_by_instance(instance_name)
    if not link:
        logger.warning('Webhook for unlinked instance: %s', instance_name)
        return {'status': 'ignored', 'reason': 'instance_not_linked'}

    # Only process message events from customers
    if event not in PROCESSED_EVENTS:
        logger.debug('Ignoring event type: %s', event)
        return {'status': 'ignored', 'reason': 'unhandled_event', 'event': event}

    # Parse the message data from the Evolution payload
    parser = EvolutionEventParser()
    parsed = parser.parse_message(payload)

    if not parsed:
        logger.debug('Could not extract message from webhook payload')
        return {'status': 'ignored', 'reason': 'no_message'}

    # Skip messages sent by the bot itself (fromMe)
    if parsed.get('author_type') == 'support':
        logger.debug('Ignoring message sent by bot itself')
        return {'status': 'ignored', 'reason': 'own_message'}

    customer_phone = parsed.get('customer_phone_number')
    text = parsed.get('text')
    message_id = parsed.get('message_id')
    push_name = parsed.get('push_name')

    if not customer_phone or not text:
        logger.debug('Missing phone or text in parsed message')
        return {'status': 'ignored', 'reason': 'missing_fields'}

    try:
        reply = ConversationService(db).process_message(
            instance_name=instance_name,
            phone_number=customer_phone,
            text=text,
            message_id=message_id,
            push_name=push_name,
        )

        # Send the reply back via Evolution API
        evo = EvolutionAPIService()
        try:
            await evo.send_message(instance_name, customer_phone, reply)
        except Exception as e:
            logger.warning('Failed to send reply via Evolution API: %s', e)

        return {
            'status': 'processed',
            'instance': instance_name,
            'phone_number': customer_phone,
            'reply': reply.get('text', ''),
        }
    except Exception as e:
        logger.error('Error processing conversation: %s', e, exc_info=True)
        return {
            'status': 'error',
            'error': str(e),
        }


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
