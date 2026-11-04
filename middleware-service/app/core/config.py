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
    default_tenant_slug: str = ''
    default_instance_name: str = ''

    # Helpdesk backend API configuration (schema-per-tenant, /api/v2).
    # Base URL must include the version prefix, e.g. http://helpdesk:8000/api/v2
    helpdesk_api_base_url: str = 'http://host.containers.internal:8000/api/v2'
    helpdesk_api_timeout: int = 30
    # JSON object mapping helpdesk tenant slug -> that tenant's integration API key
    # (generated in the helpdesk: POST /api/v2/integration/api-key, scopes read,write).
    # Keys are bound to a single tenant by the backend, so there is no shared key.
    helpdesk_tenant_api_keys: str = '{}'
    # Production resolves tenants by Host subdomain. When set (e.g. ".mitra.mw"),
    # requests carry X-Forwarded-Host: <slug><suffix>. The X-Tenant-ID header is
    # always sent too (honoured by the backend only if header fallback is enabled).
    helpdesk_tenant_domain_suffix: str = ''

    # Operator key for this middleware's own management API (X-Admin-Key header).
    # Required; management endpoints return 503 while it is empty.
    admin_api_key: str = ''

    def tenant_api_keys(self) -> dict[str, str]:
        import json
        try:
            data = json.loads(self.helpdesk_tenant_api_keys or '{}')
        except ValueError:
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k).strip().lower(): str(v) for k, v in data.items() if k and v}


settings = Settings()
