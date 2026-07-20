"""
Helpdesk Backend API Client

HTTP client that connects the middleware service to the real helpdesk backend.
Supports both API-key and JWT authentication (JWT uses the shared SECRET_KEY).
Provides methods for customer sync, ticket operations, and health checks.
"""

import logging
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import jwt

from app.core.config import settings

logger = logging.getLogger(__name__)


class HelpdeskAPIClient:
    """Client for the helpdesk backend WhatsApp integration API."""

    def __init__(self):
        self.base_url = settings.helpdesk_api_base_url.rstrip("/")
        self.api_key = settings.helpdesk_api_key
        self.timeout = settings.helpdesk_api_timeout
        self.default_tenant_id = settings.helpdesk_default_tenant_id
        self._jwt_token: str | None = None
        self._jwt_expiry: datetime | None = None

        if not self.api_key:
            logger.warning(
                "HELPDESK_API_KEY is not set! Middleware will not be able to "
                "authenticate with the helpdesk backend."
            )

    # ── Authentication ─────────────────────────────────────

    def _generate_jwt_token(self) -> str:
        """Generate a JWT token using the shared SECRET_KEY for helpdesk API auth."""
        now = datetime.utcnow()
        payload = {
            "sub": settings.helpdesk_service_username,
            "user": {
                "id": str(settings.helpdesk_service_username),
                "email": f"{settings.helpdesk_service_username}@middleware.local",
                "username": settings.helpdesk_service_username,
                "tenant_id": self.default_tenant_id,
                "type": "service",
            },
            "iat": now,
            "exp": now + timedelta(hours=1),
        }
        return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)

    def _get_jwt_token(self) -> str:
        """Get a valid JWT token, generating a new one if expired."""
        if not self._jwt_token or not self._jwt_expiry or datetime.utcnow() >= self._jwt_expiry:
            self._jwt_token = self._generate_jwt_token()
            self._jwt_expiry = datetime.utcnow() + timedelta(minutes=55)
        return self._jwt_token

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}

        # Prefer JWT auth if enabled, fall back to API key
        if settings.helpdesk_jwt_enabled:
            headers["Authorization"] = f"Bearer {self._get_jwt_token()}"
        elif self.api_key:
            headers["X-API-Key"] = self.api_key

        return headers

    def _tenant_id(self, tenant_id: str | UUID | None = None) -> str:
        """Resolve tenant ID from argument or default."""
        if tenant_id:
            return str(tenant_id)
        if self.default_tenant_id:
            return self.default_tenant_id
        raise ValueError(
            "No tenant_id provided and HELPDESK_DEFAULT_TENANT_ID is not configured"
        )

    # ── Customer Operations ────────────────────────────────

    def lookup_customer_by_phone(
        self,
        phone_number: str,
        tenant_id: str | UUID | None = None,
    ) -> dict[str, Any] | None:
        """
        Look up a customer in the main helpdesk system by phone number.
        Returns the customer data including id, tenant_id, name, etc., or None.
        """
        url = f"{self.base_url}/api/v1/whatsapp/customers/lookup/phone"
        params = {
            "phone_number": phone_number,
            "tenant_id": self._tenant_id(tenant_id),
        }

        try:
            response = httpx.get(
                url,
                params=params,
                headers=self._headers(),
                timeout=self.timeout,
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            logger.error(
                "Helpdesk API error looking up customer (status %s): %s",
                e.response.status_code,
                e.response.text,
            )
            return None
        except httpx.RequestError as e:
            logger.error(
                "Helpdesk API request error looking up customer: %s", e
            )
            return None

    def create_customer(
        self,
        tenant_id: str | UUID,
        phone_number: str,
        customer_name: str | None = None,
    ) -> dict[str, Any] | None:
        """
        Create a customer record in the main helpdesk system for a WhatsApp user.
        Returns the created customer data, or None on failure.
        """
        url = f"{self.base_url}/api/v1/whatsapp/customers"
        payload = {
            "tenant_id": self._tenant_id(tenant_id),
            "phone_number": phone_number,
            "customer_name": customer_name,
        }

        try:
            response = httpx.post(
                url,
                json=payload,
                headers=self._headers(),
                timeout=self.timeout,
            )
            response.raise_for_status()
            result = response.json()
            logger.info(
                "Helpdesk customer created/retrieved: id=%s phone=%s",
                result.get("id", "?"),
                phone_number,
            )
            return result
        except httpx.HTTPStatusError as e:
            logger.error(
                "Helpdesk API error creating customer (status %s): %s",
                e.response.status_code,
                e.response.text,
            )
            return None
        except httpx.RequestError as e:
            logger.error(
                "Helpdesk API request error creating customer: %s", e
            )
            return None

    def get_or_create_customer(
        self,
        tenant_id: str | UUID,
        phone_number: str,
        customer_name: str | None = None,
    ) -> dict[str, Any] | None:
        """
        Look up a customer by phone number, creating one if not found.
        This is the primary method to sync customers between middleware and helpdesk.
        """
        customer = self.lookup_customer_by_phone(phone_number, tenant_id)
        if customer:
            return customer
        return self.create_customer(tenant_id, phone_number, customer_name)

    # ── Ticket Operations ──────────────────────────────────

    def create_ticket(
        self,
        tenant_id: str | UUID | None,
        phone_number: str,
        subject: str,
        description: str | None = None,
        category: str | None = None,
        customer_name: str | None = None,
    ) -> dict[str, Any] | None:
        """
        Create a ticket in the helpdesk backend.
        Returns the created ticket data (including id and ticket_number), or None on failure.
        """
        url = f"{self.base_url}/api/v1/whatsapp/tickets"
        payload = {
            "tenant_id": self._tenant_id(tenant_id),
            "phone_number": phone_number,
            "subject": subject,
            "description": description,
            "category": category,
            "customer_name": customer_name,
        }

        try:
            response = httpx.post(
                url,
                json=payload,
                headers=self._headers(),
                timeout=self.timeout,
            )
            response.raise_for_status()
            result = response.json()
            logger.info(
                "Helpdesk ticket created: #%s (id=%s) for %s",
                result.get("ticket_number", "?"),
                result.get("id", "?"),
                phone_number,
            )
            return result
        except httpx.HTTPStatusError as e:
            logger.error(
                "Helpdesk API error creating ticket (status %s): %s",
                e.response.status_code,
                e.response.text,
            )
            return None
        except httpx.RequestError as e:
            logger.error(
                "Helpdesk API request error creating ticket: %s", e
            )
            return None
        except Exception as e:
            logger.error(
                "Unexpected error creating helpdesk ticket: %s", e, exc_info=True
            )
            return None

    def get_ticket_status(
        self,
        ticket_number: str,
        tenant_id: str | UUID | None = None,
    ) -> dict[str, Any] | None:
        """
        Get ticket status and details from the helpdesk backend.
        """
        url = f"{self.base_url}/api/v1/whatsapp/tickets/{ticket_number}"
        params = {"tenant_id": self._tenant_id(tenant_id)}

        try:
            response = httpx.get(
                url,
                params=params,
                headers=self._headers(),
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.info("Ticket %s not found in helpdesk", ticket_number)
                return None
            logger.error(
                "Helpdesk API error fetching ticket (status %s): %s",
                e.response.status_code,
                e.response.text,
            )
            return None
        except httpx.RequestError as e:
            logger.error(
                "Helpdesk API request error fetching ticket: %s", e
            )
            return None

    def lookup_tickets_by_phone(
        self,
        phone_number: str,
        tenant_id: str | UUID | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """
        Look up tickets by customer phone number.
        """
        url = f"{self.base_url}/api/v1/whatsapp/tickets/lookup/phone"
        params = {
            "phone_number": phone_number,
            "tenant_id": self._tenant_id(tenant_id),
            "limit": limit,
        }

        try:
            response = httpx.get(
                url,
                params=params,
                headers=self._headers(),
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
            return data.get("tickets", [])
        except httpx.HTTPStatusError as e:
            logger.error(
                "Helpdesk API error looking up tickets (status %s): %s",
                e.response.status_code,
                e.response.text,
            )
            return []
        except httpx.RequestError as e:
            logger.error(
                "Helpdesk API request error looking up tickets: %s", e
            )
            return []

    def add_comment(
        self,
        ticket_number: str,
        tenant_id: str | UUID | None,
        phone_number: str,
        message: str,
        author_type: str = "customer",
    ) -> dict[str, Any] | None:
        """
        Add a comment to a ticket.
        """
        url = f"{self.base_url}/api/v1/whatsapp/tickets/{ticket_number}/comments"
        payload = {
            "tenant_id": self._tenant_id(tenant_id),
            "phone_number": phone_number,
            "message": message,
            "author_type": author_type,
        }

        try:
            response = httpx.post(
                url,
                json=payload,
                headers=self._headers(),
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.info(
                    "Ticket %s not found for adding comment", ticket_number
                )
                return None
            logger.error(
                "Helpdesk API error adding comment (status %s): %s",
                e.response.status_code,
                e.response.text,
            )
            return None
        except httpx.RequestError as e:
            logger.error(
                "Helpdesk API request error adding comment: %s", e
            )
            return None

    # ── Data Query Operations (pull data from helpdesk) ────

    def list_tenants(
        self,
        page: int = 1,
        per_page: int = 50,
        include_deleted: bool = False,
    ) -> dict[str, Any]:
        """
        Fetch tenants from the main helpdesk system.
        Returns paginated tenant data.
        """
        url = f"{self.base_url}/api/v1/whatsapp/tenants"
        params = {
            "page": page,
            "per_page": per_page,
            "include_deleted": str(include_deleted).lower(),
        }
        try:
            response = httpx.get(
                url,
                params=params,
                headers=self._headers(),
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            logger.error(
                "Helpdesk API error listing tenants (status %s): %s",
                e.response.status_code,
                e.response.text,
            )
            return {"tenants": [], "total": 0, "page": page, "per_page": per_page}
        except httpx.RequestError as e:
            logger.error("Helpdesk API request error listing tenants: %s", e)
            return {"tenants": [], "total": 0, "page": page, "per_page": per_page}

    def list_customers(
        self,
        tenant_id: str | UUID | None = None,
        page: int = 1,
        per_page: int = 50,
        include_deleted: bool = False,
    ) -> dict[str, Any]:
        """
        Fetch customers from the main helpdesk system.
        Can be filtered by tenant_id.
        Returns paginated customer data.
        """
        url = f"{self.base_url}/api/v1/whatsapp/customers/list"
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "include_deleted": str(include_deleted).lower(),
        }
        if tenant_id:
            params["tenant_id"] = self._tenant_id(tenant_id)

        try:
            response = httpx.get(
                url,
                params=params,
                headers=self._headers(),
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            logger.error(
                "Helpdesk API error listing customers (status %s): %s",
                e.response.status_code,
                e.response.text,
            )
            return {"customers": [], "total": 0, "page": page, "per_page": per_page}
        except httpx.RequestError as e:
            logger.error("Helpdesk API request error listing customers: %s", e)
            return {"customers": [], "total": 0, "page": page, "per_page": per_page}

    def list_tickets(
        self,
        tenant_id: str | UUID | None = None,
        customer_id: str | UUID | None = None,
        page: int = 1,
        per_page: int = 50,
    ) -> dict[str, Any]:
        """
        Fetch tickets from the main helpdesk system.
        Can be filtered by tenant_id and/or customer_id.
        Returns paginated ticket data.
        """
        url = f"{self.base_url}/api/v1/whatsapp/tickets/list-all"
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
        }
        if tenant_id:
            params["tenant_id"] = self._tenant_id(tenant_id)
        if customer_id:
            params["customer_id"] = str(customer_id)

        try:
            response = httpx.get(
                url,
                params=params,
                headers=self._headers(),
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            logger.error(
                "Helpdesk API error listing tickets (status %s): %s",
                e.response.status_code,
                e.response.text,
            )
            return {"tickets": [], "total": 0, "page": page, "per_page": per_page}
        except httpx.RequestError as e:
            logger.error("Helpdesk API request error listing tickets: %s", e)
            return {"tickets": [], "total": 0, "page": page, "per_page": per_page}

    # ── Health check ───────────────────────────────────────

    def health_check(self) -> bool:
        """Check if the helpdesk API is reachable."""
        url = f"{self.base_url}/api/v1/whatsapp/health"
        try:
            response = httpx.get(url, timeout=10)
            return response.status_code == 200
        except Exception:
            return False

    # ── User Registration Check ──────────────────────────

    def check_user_registration(
        self,
        phone_number: str,
        tenant_id: str | UUID | None = None,
    ) -> dict[str, Any] | None:
        """
        Check if a phone number is registered to a user account in the helpdesk.
        Returns user details if registered, or None if not registered or on error.
        """
        url = f"{self.base_url}/api/v1/whatsapp/users/check-registration"
        params = {
            "phone_number": phone_number,
            "tenant_id": self._tenant_id(tenant_id),
        }

        try:
            response = httpx.get(
                url,
                params=params,
                headers=self._headers(),
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.info("No user registered for phone %s in helpdesk", phone_number)
                return {"is_registered": False, "phone_number": phone_number}
            logger.error(
                "Helpdesk API error checking user registration (status %s): %s",
                e.response.status_code,
                e.response.text,
            )
            return None
        except httpx.RequestError as e:
            logger.error(
                "Helpdesk API request error checking user registration: %s", e
            )
            return None

    # ── Phone-User Linking ────────────────────────────────

    def link_phone_to_user(
        self,
        phone_number: str,
        user_id: str | UUID,
        tenant_id: str | UUID | None = None,
    ) -> dict[str, Any] | None:
        """
        Link a phone number to a user ID in the helpdesk system.
        This allows the middleware to register a WhatsApp phone number
        to an existing helpdesk user account.
        """
        url = f"{self.base_url}/api/v1/whatsapp/users/link-phone"
        payload = {
            "phone_number": phone_number,
            "user_id": str(user_id),
            "tenant_id": self._tenant_id(tenant_id),
        }

        try:
            response = httpx.post(
                url,
                json=payload,
                headers=self._headers(),
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            logger.error(
                "Helpdesk API error linking phone to user (status %s): %s",
                e.response.status_code,
                e.response.text,
            )
            return None
        except httpx.RequestError as e:
            logger.error(
                "Helpdesk API request error linking phone to user: %s", e
            )
            return None

    # ── Ticket Creation ──────────────────────────────────

    def create_ticket(
        self,
        tenant_id: str | UUID,
        title: str,
        description: str | None = None,
        creator_id: str | UUID | None = None,
        customer_id: str | UUID | None = None,
        category: str | None = None,
        priority: str | None = None,
        channel: str | None = None,
        phone_number: str | None = None,
    ) -> dict[str, Any] | None:
        """
        Create a ticket in the helpdesk backend with full details.
        This is the main endpoint for creating tickets from the middleware.
        
        Args:
            tenant_id: The tenant UUID
            title: Ticket title/subject
            description: Ticket description
            creator_id: The user ID who is creating the ticket (optional)
            customer_id: The customer ID (optional)
            category: Category name (optional)
            priority: Priority name (optional)
            channel: Channel name (optional)
            phone_number: WhatsApp phone number (optional, for WhatsApp-originated tickets)
        """
        url = f"{self.base_url}/api/v1/whatsapp/tickets/create"
        payload = {
            "tenant_id": self._tenant_id(tenant_id),
            "title": title,
            "description": description,
            "creator_id": str(creator_id) if creator_id else None,
            "customer_id": str(customer_id) if customer_id else None,
            "category": category,
            "priority": priority,
            "channel": channel,
            "phone_number": phone_number,
        }
        # Remove None values
        payload = {k: v for k, v in payload.items() if v is not None}

        try:
            response = httpx.post(
                url,
                json=payload,
                headers=self._headers(),
                timeout=self.timeout,
            )
            response.raise_for_status()
            result = response.json()
            logger.info(
                "Ticket created in helpdesk backend: #%s (category=%s)",
                result.get("ticket_number", "?"),
                result.get("category", "?"),
            )
            return result
        except httpx.HTTPStatusError as e:
            logger.error(
                "Helpdesk API error creating ticket (status %s): %s",
                e.response.status_code,
                e.response.text,
            )
            return None
        except httpx.RequestError as e:
            logger.error(
                "Helpdesk API request error creating ticket: %s", e
            )
            return None
