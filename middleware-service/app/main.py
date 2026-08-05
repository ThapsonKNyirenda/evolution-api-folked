import asyncio
import logging
import time

from fastapi import FastAPI

from app.api.routes import get_router
from app.core.config import settings
from app.db.session import Base, engine
from app.models.command_log import CommandLog  # noqa: F401
from app.models.customer import Customer  # noqa: F401
from app.models.event_log import EventLog  # noqa: F401
from app.models.instance_tenant import InstanceTenant  # noqa: F401
from app.models.phone_registry import PhoneRegistry  # noqa: F401
from app.models.tenant import Tenant  # noqa: F401
from app.models.ticket import Ticket  # noqa: F401
from app.models.ticket_comment import TicketComment  # noqa: F401
from app.models.ticket_message import TicketMessage  # noqa: F401
from app.models.whatsapp_session import WhatsappSession  # noqa: F401
from app.db.session import SessionLocal
from app.services.bootstrap import bootstrap_default_instance
from app.services.consumer import start_consumer
from app.services.rabbitmq import RabbitMQService
from app.services.sync_service import SyncService

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s - %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI(title=settings.app_name)
rabbitmq_service = RabbitMQService()


async def run_periodic_sync():
    """
    Background task that periodically syncs data from the helpdesk system
    into the middleware database. Runs every sync_interval_minutes.
    """
    interval = settings.sync_interval_minutes
    if interval <= 0:
        logger.info('Periodic sync is disabled (sync_interval_minutes=%d)', interval)
        return

    logger.info('Periodic sync task started (interval=%d minutes)', interval)

    # Wait a bit for the app to fully initialize
    await asyncio.sleep(10)

    while True:
        try:
            # Use a new DB session for each sync cycle
            db = SessionLocal()
            try:
                syncer = SyncService(db)
                logger.info('Starting periodic sync...')
                start = time.time()
                result = syncer.sync_all()
                elapsed = time.time() - start
                logger.info(
                    'Periodic sync completed in %.2fs: tenants=%d, customers=%d, tickets=%d, registered_users=%d',
                    elapsed,
                    result.get('tenants', {}).get('synced', 0),
                    result.get('customers', {}).get('synced', 0),
                    result.get('tickets', {}).get('synced', 0),
                    result.get('registered_users', {}).get('synced', 0),
                )
                if result.get('tenants', {}).get('errors'):
                    logger.warning('Tenant sync errors: %s', result['tenants']['errors'])
                if result.get('customers', {}).get('errors'):
                    logger.warning('Customer sync errors: %s', result['customers']['errors'])
                if result.get('tickets', {}).get('errors'):
                    logger.warning('Ticket sync errors: %s', result['tickets']['errors'])
                if result.get('registered_users', {}).get('errors'):
                    logger.warning('Registered user sync errors: %s', result['registered_users']['errors'])
            except Exception as e:
                logger.error('Periodic sync failed: %s', e)
            finally:
                db.close()
        except Exception as e:
            logger.error('Periodic sync session error: %s', e)

        await asyncio.sleep(interval * 60)


@app.on_event('startup')
async def on_startup():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        bootstrap_default_instance(db)
    finally:
        db.close()
    await rabbitmq_service.connect()
    await start_consumer(rabbitmq_service)
    # Start background periodic sync
    asyncio.create_task(run_periodic_sync())


@app.on_event('shutdown')
async def on_shutdown():
    await rabbitmq_service.close()


app.include_router(get_router(rabbitmq_service), prefix='/api/v1', tags=['middleware'])
