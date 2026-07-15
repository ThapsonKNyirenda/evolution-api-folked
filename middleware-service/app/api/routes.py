import uuid
import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.comments import router as comments_router
from app.api.customers import router as customers_router
from app.api.instances import router as instances_router
from app.api.tenants import router as tenants_router
from app.api.ticket_messages import router as ticket_messages_router
from app.api.tickets import router as tickets_router
from app.api.webhook import router as webhook_router
from app.core.config import settings
from app.db.session import get_db
from app.schemas.events import IncomingEvent, PublishMessage
from app.schemas.evolution import RabbitMQInstanceConfig
from app.services.evolution_api import EvolutionAPIService
from app.services.helpdesk_api import HelpdeskAPIClient
from app.services.rabbitmq import RabbitMQService
from app.services.sync_service import SyncService
from app.repositories import EventLogRepository, PhoneRegistryRepository
from app.services.ticket_event_publisher import TicketEventPublisher
from app.services.ticket_pipeline import publish_ticket_result, summarize_ticket_result
from app.services.ticket_service import TicketService

logger = logging.getLogger(__name__)


def get_router(rabbitmq: RabbitMQService) -> APIRouter:
    router = APIRouter()
    evolution_api = EvolutionAPIService()
    helpdesk_api = HelpdeskAPIClient()

    router.include_router(tenants_router)
    router.include_router(customers_router)
    router.include_router(tickets_router)
    router.include_router(ticket_messages_router)
    router.include_router(comments_router)
    router.include_router(instances_router)
    router.include_router(webhook_router)

    @router.get('/health')
    async def health_check():
        return {'status': 'ok', 'service': settings.app_name}

    # ── Evolution API Info ─────────────────────────────────

    @router.get('/evolution/info')
    async def evolution_info():
        try:
            return await evolution_api.get_information()
        except Exception as error:
            raise HTTPException(status_code=502, detail=f'Failed to fetch Evolution info: {error}') from error

    @router.post('/evolution/rabbitmq/set/{instance_name}')
    async def configure_evolution_rabbitmq(instance_name: str, config: RabbitMQInstanceConfig):
        try:
            response = await evolution_api.set_instance_rabbitmq(instance_name, config.model_dump())
            return {'message': 'RabbitMQ configured on Evolution instance', 'instance': instance_name, 'response': response}
        except Exception as error:
            raise HTTPException(status_code=502, detail=f'Failed to configure Evolution RabbitMQ: {error}') from error

    # ── Evolution API Instance Management ──────────────────

    @router.get('/evolution/instances')
    async def list_evolution_instances():
        """List all instances from the Evolution API."""
        try:
            instances = await evolution_api.get_instances()
            return {'instances': instances, 'total': len(instances)}
        except Exception as error:
            raise HTTPException(status_code=502, detail=f'Failed to fetch Evolution instances: {error}') from error

    @router.get('/evolution/instances/{instance_name}')
    async def get_evolution_instance(instance_name: str):
        """Get connection state for a specific Evolution API instance."""
        try:
            instance = await evolution_api.get_instance(instance_name)
            if instance is None:
                raise HTTPException(status_code=404, detail=f'Instance {instance_name} not found in Evolution API')
            return instance
        except HTTPException:
            raise
        except Exception as error:
            raise HTTPException(status_code=502, detail=f'Failed to fetch instance: {error}') from error

    @router.post('/evolution/instances', status_code=201)
    async def create_evolution_instance(instance_name: str = Query(..., description='Name for the new instance')):
        """Create a new instance in the Evolution API."""
        try:
            result = await evolution_api.create_instance(instance_name)
            if result is None:
                raise HTTPException(status_code=500, detail=f'Failed to create instance {instance_name}')
            return {'message': 'Instance created', 'instance': instance_name, 'response': result}
        except HTTPException:
            raise
        except Exception as error:
            raise HTTPException(status_code=502, detail=f'Failed to create instance: {error}') from error

    @router.delete('/evolution/instances/{instance_name}')
    async def delete_evolution_instance(instance_name: str):
        """Delete an instance from the Evolution API."""
        success = await evolution_api.delete_instance(instance_name)
        if not success:
            raise HTTPException(status_code=500, detail=f'Failed to delete instance {instance_name}')
        return {'message': 'Instance deleted', 'instance': instance_name}

    @router.post('/evolution/instances/{instance_name}/restart')
    async def restart_evolution_instance(instance_name: str):
        """Restart an instance in the Evolution API."""
        success = await evolution_api.restart_instance(instance_name)
        if not success:
            raise HTTPException(status_code=500, detail=f'Failed to restart instance {instance_name}')
        return {'message': 'Instance restarted', 'instance': instance_name}

    # ── Helpdesk Data Sync / Query Endpoints ───────────────

    @router.get('/sync/tenants')
    async def sync_tenants(
        page: int = Query(default=1, ge=1),
        per_page: int = Query(default=50, ge=1, le=200),
        include_deleted: bool = Query(default=False),
    ):
        """
        Pull tenants from the main helpdesk system.
        Proxies the helpdesk /api/v1/whatsapp/tenants endpoint.
        """
        data = helpdesk_api.list_tenants(
            page=page,
            per_page=per_page,
            include_deleted=include_deleted,
        )
        return {
            'source': 'helpdesk',
            'tenants': data.get('tenants', []),
            'total': data.get('total', 0),
            'page': page,
            'per_page': per_page,
        }

    @router.get('/sync/customers')
    async def sync_customers(
        tenant_id: Optional[str] = Query(None, description='Filter by tenant UUID'),
        page: int = Query(default=1, ge=1),
        per_page: int = Query(default=50, ge=1, le=200),
        include_deleted: bool = Query(default=False),
    ):
        """
        Pull customers from the main helpdesk system.
        Proxies the helpdesk /api/v1/whatsapp/customers/list endpoint.
        """
        data = helpdesk_api.list_customers(
            tenant_id=tenant_id,
            page=page,
            per_page=per_page,
            include_deleted=include_deleted,
        )
        return {
            'source': 'helpdesk',
            'customers': data.get('customers', []),
            'total': data.get('total', 0),
            'page': page,
            'per_page': per_page,
        }

    @router.get('/sync/tickets')
    async def sync_tickets(
        tenant_id: Optional[str] = Query(None, description='Filter by tenant UUID'),
        customer_id: Optional[str] = Query(None, description='Filter by customer UUID'),
        page: int = Query(default=1, ge=1),
        per_page: int = Query(default=50, ge=1, le=200),
    ):
        """
        Pull tickets from the main helpdesk system.
        Proxies the helpdesk /api/v1/whatsapp/tickets/list endpoint.
        """
        data = helpdesk_api.list_tickets(
            tenant_id=tenant_id,
            customer_id=customer_id,
            page=page,
            per_page=per_page,
        )
        return {
            'source': 'helpdesk',
            'tickets': data.get('tickets', []),
            'total': data.get('total', 0),
            'page': page,
            'per_page': per_page,
        }

    @router.get('/sync/health')
    async def sync_health():
        """
        Check connectivity to both the helpdesk backend and Evolution API.
        """
        helpdesk_ok = helpdesk_api.health_check()
        evolution_ok = False
        try:
            await evolution_api.get_information()
            evolution_ok = True
        except Exception:
            pass

        return {
            'helpdesk_api': {'reachable': helpdesk_ok, 'url': settings.helpdesk_api_base_url},
            'evolution_api': {'reachable': evolution_ok, 'url': settings.evolution_api_base_url},
            'overall': helpdesk_ok and evolution_ok,
        }

    # ── Sync Operations (pull data from helpdesk into local DB) ──

    @router.post('/sync/run')
    async def run_full_sync(db: Session = Depends(get_db)):
        """
        Run a full sync: pull tenants, customers, and tickets from
        the main helpdesk system into the local middleware database.
        """
        syncer = SyncService(db)
        result = syncer.sync_all()
        return {
            'message': 'Full sync completed',
            'result': result,
        }

    @router.post('/sync/tenants')
    async def sync_tenants_to_db(db: Session = Depends(get_db)):
        """
        Pull tenants from the helpdesk system and upsert them into
        the local middleware database.
        """
        syncer = SyncService(db)
        result = syncer.sync_tenants()
        return {
            'message': 'Tenants sync completed',
            'result': result,
        }

    @router.post('/sync/customers')
    async def sync_customers_to_db(
        tenant_id: Optional[str] = Query(None, description='Local tenant UUID to sync customers for'),
        db: Session = Depends(get_db),
    ):
        """
        Pull customers from the helpdesk system and upsert them into
        the local middleware database.
        """
        tenant_uuid = uuid.UUID(tenant_id) if tenant_id else None
        syncer = SyncService(db)
        result = syncer.sync_customers(tenant_id=tenant_uuid)
        return {
            'message': 'Customers sync completed',
            'result': result,
        }

    @router.post('/sync/tickets')
    async def sync_tickets_to_db(
        tenant_id: Optional[str] = Query(None, description='Local tenant UUID to sync tickets for'),
        db: Session = Depends(get_db),
    ):
        """
        Pull tickets from the helpdesk system and upsert them into
        the local middleware database.
        """
        tenant_uuid = uuid.UUID(tenant_id) if tenant_id else None
        syncer = SyncService(db)
        result = syncer.sync_tickets(tenant_id=tenant_uuid)
        return {
            'message': 'Tickets sync completed',
            'result': result,
        }

    # ── Phone Registry (phone → customer mapping) ─────────

    @router.get('/phone-registry')
    async def list_phone_registry(
        tenant_id: Optional[str] = Query(None, description='Filter by tenant UUID'),
        phone_number: Optional[str] = Query(None, description='Filter by phone number'),
        db: Session = Depends(get_db),
    ):
        """
        List phone-to-customer mappings from the phone registry.
        This table maps WhatsApp phone numbers to local customer records
        and their corresponding helpdesk customer IDs.
        """
        repo = PhoneRegistryRepository(db)
        query = repo.list_by_tenant

        if tenant_id:
            try:
                tenant_uuid = uuid.UUID(tenant_id)
            except ValueError:
                raise HTTPException(status_code=400, detail='Invalid tenant_id')
            entries = repo.list_by_tenant(tenant_uuid)
        else:
            # If no tenant filter, get all (iterate all tenants)
            from app.repositories import TenantRepository
            tenant_repo = TenantRepository(db)
            all_entries = []
            for t in tenant_repo.list_all():
                all_entries.extend(repo.list_by_tenant(t.id))
            entries = all_entries

        if phone_number:
            entries = [e for e in entries if e.phone_number == phone_number]

        return {
            'total': len(entries),
            'entries': [
                {
                    'id': str(e.id),
                    'phone_number': e.phone_number,
                    'customer_id': str(e.customer_id),
                    'tenant_id': str(e.tenant_id),
                    'helpdesk_customer_id': str(e.helpdesk_customer_id) if e.helpdesk_customer_id else None,
                    'is_active': e.is_active,
                    'first_seen_at': e.first_seen_at.isoformat() if e.first_seen_at else None,
                    'last_seen_at': e.last_seen_at.isoformat() if e.last_seen_at else None,
                }
                for e in entries
            ],
        }

    # ── Events ─────────────────────────────────────────────

    @router.post('/events/evolution')
    async def receive_evolution_event(data: IncomingEvent, db: Session = Depends(get_db)):
        repo = EventLogRepository(db)
        event = repo.create(source='evolution', event_type=data.event_type, payload=data.payload)

        ticket_result = TicketService(db).process_evolution_payload(data.payload)
        if ticket_result:
            await publish_ticket_result(TicketEventPublisher(rabbitmq), ticket_result)

        await rabbitmq.publish(
            payload={'source': 'evolution', 'event_type': data.event_type, 'payload': data.payload, 'event_id': event.id},
            routing_key=settings.rabbitmq_routing_key_out,
        )

        return {
            'message': 'Event received and processed',
            'event_id': event.id,
            'ticket_result': summarize_ticket_result(ticket_result),
        }

    @router.post('/events/helpdesk')
    async def receive_helpdesk_event(data: IncomingEvent, db: Session = Depends(get_db)):
        repo = EventLogRepository(db)
        event = repo.create(source='helpdesk', event_type=data.event_type, payload=data.payload)

        await rabbitmq.publish(
            payload={'source': 'helpdesk', 'event_type': data.event_type, 'payload': data.payload, 'event_id': event.id},
            routing_key=settings.rabbitmq_routing_key_in,
        )

        return {'message': 'Helpdesk event received and sent to queue', 'event_id': event.id}

    @router.post('/publish')
    async def publish_message(data: PublishMessage):
        routing_key = data.routing_key or settings.rabbitmq_routing_key_out
        await rabbitmq.publish(payload=data.message, routing_key=routing_key)
        return {'message': 'Message published', 'routing_key': routing_key}

    return router
