"""
Authentication for the middleware's own management API.

The management endpoints (tenants, instance links, Evolution proxy, ...) administer
*every* tenant's WhatsApp wiring, so they are deliberately NOT tied to any helpdesk
tenant user/JWT. They require a dedicated operator secret (``ADMIN_API_KEY``) sent in
the ``X-Admin-Key`` header. This is unrelated to the per-tenant helpdesk API keys the
middleware uses to call the helpdesk (``HELPDESK_TENANT_API_KEYS``).

The Evolution webhook endpoints stay public (Evolution cannot send credentials).
"""

import hmac
import logging

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

from app.core.config import settings

logger = logging.getLogger(__name__)

admin_key_scheme = APIKeyHeader(
    name="X-Admin-Key",
    scheme_name="AdminKey",
    description="Middleware operator key (ADMIN_API_KEY)",
    auto_error=False,
)


def require_admin_role(api_key: str | None = Security(admin_key_scheme)) -> dict:
    expected = settings.admin_api_key
    if not expected:
        # Fail closed: never allow access when no key is configured.
        logger.error("ADMIN_API_KEY is not configured; rejecting management request")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Management API is not configured (set ADMIN_API_KEY)",
        )
    if not api_key or not hmac.compare_digest(api_key.encode(), expected.encode()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Admin-Key",
        )
    return {"role": "operator"}


# Same operator check; kept as a separate name because routes distinguish the two tiers.
require_helpdesk_token = require_admin_role
