from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    app_name: str = 'evolution-helpdesk-middleware'
    app_env: str = 'development'
    app_host: str = '0.0.0.0'
    app_port: int = 8090

    database_url: str = 'postgresql+psycopg2://middleware_user:middleware_pass@localhost:5432/middleware_db'

    rabbitmq_url: str = 'amqp://guest:guest@localhost:5672/'
    rabbitmq_exchange: str = 'evolution_exchange'
    rabbitmq_queue_in: str = 'middleware.evolution.events'
    rabbitmq_queue_out: str = 'evolution.middleware.out'
    rabbitmq_routing_key_in: str = '#'
    rabbitmq_routing_key_out: str = 'helpdesk.out'

    evolution_api_base_url: str = 'http://localhost:8080'
    evolution_api_key: str = ''
    evolution_api_timeout: int = 20

    support_whatsapp_number: str = ''
    default_tenant_name: str = ''
    default_instance_name: str = ''

    # Helpdesk backend API configuration
    helpdesk_api_base_url: str = 'http://host.containers.internal:8000'
    helpdesk_api_key: str = ''
    helpdesk_api_timeout: int = 30
    helpdesk_default_tenant_id: str = ''

    # Shared authentication with main helpdesk system
    # Uses the same SECRET_KEY and ALGORITHM as the main helpdesk for JWT token generation
    secret_key: str = 'X0FZ0-_XAhFD9zCHZLZk6ePIvAL7WyyojU4L0Xm3-8Y'
    algorithm: str = 'HS256'
    helpdesk_jwt_enabled: bool = True
    helpdesk_service_username: str = 'whatsapp-middleware'

    # Periodic sync configuration
    sync_interval_minutes: int = 15
    """How often to run the background sync (tenants, customers, tickets, registered_users)."""


settings = Settings()
