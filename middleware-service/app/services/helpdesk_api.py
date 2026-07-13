"""
Helpdesk Backend API Client

HTTP client that connects the middleware service to the real helpdesk backend.
Replaces local database operations with API calls to the helpdesk system's
WhatsApp integration endpoints.
"""

import logging
from typing import Any
from uuid import UUID

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class HelpdeskAPIClient:
    """Client for the helpdesk backend WhatsApp integration API."""

    def __init__(self):
        self.base_url = settings.helpdesk_api_base_url.rstrip("/")
        self.api_key = settings.helpdesk_api_key
        self.timeout = settings.helpdesk_api_timeout
        self.default_tenant_id = settings.helpdesk_default_tenant_id

        if not self.api_key:
            logger.warning(
                "HELPDESK_API_KEY is not set! Middleware will not be able to "
                "authenticate with the helpdesk backend."
            )

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-API-Key": self.api_key,
        }

    def _tenant_id(self, tenant_id: str | UUID | None = None) -> str:
        """Resolve tenant ID from argument or default."""
        if tenant_id:
            return str(tenant_id)
        if self.default_tenant_id:
            return self.default_tenant_id
        raise ValueError(
            "No tenant_id provided and HELPDESK_DEFAULT_TENANT_ID is not configured"
        )

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
        Returns the created ticket data, or None on failure.
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
                "Helpdesk ticket created: #%s for %s",
                result.get("ticket_number", "?"),
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

    # ── Health check ───────────────────────────────────────

    def health_check(self) -> bool:
        """Check if the helpdesk API is reachable."""
        url = f"{self.base_url}/api/v1/whatsapp/health"
        try:
            response = httpx.get(url, timeout=10)
            return response.status_code == 200
        except Exception:
            return False
