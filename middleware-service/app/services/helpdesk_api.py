"""
Helpdesk Backend API Client (schema-per-tenant, ``/api/v2``)

The helpdesk backend is the single authority for tenant resolution, schema
selection and data isolation. This client therefore never touches database
schemas: for every call it names the tenant by its *slug* and authenticates with
that tenant's own integration API key (``X-API-Key``). The backend rejects a key
that does not belong to the resolved tenant, so a request can never reach
another tenant's data even if the wrong slug were supplied.

Endpoints used (all under ``/integration/whatsapp``):
  GET  /users/by-phone                       resolve a WhatsApp number to a tenant user
  GET  /tickets                              tickets visible to a user
  GET  /tickets/by-number/{n}                one ticket, if visible to the user
  POST /tickets                              create a ticket as the user
  POST /tickets/by-number/{n}/comments       comment as the user

Every method returns ``None`` / an empty result on failure (logged without
secrets) so the conversation flow can degrade gracefully.
"""

import logging
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_PREFIX = '/integration/whatsapp'


class TenantNotConfiguredError(Exception):
    """Raised when a tenant has no slug or no API key configured."""


def _display_status(value: str | None) -> str:
    return (value or 'unknown').replace('_', ' ').upper()


def _normalize_ticket(t: dict[str, Any]) -> dict[str, Any]:
    out = dict(t)
    out['status'] = _display_status(t.get('status'))
    if t.get('priority'):
        out['priority'] = str(t['priority']).replace('_', ' ').title()
    return out


class HelpdeskAPIClient:
    """Client for the helpdesk backend WhatsApp integration API."""

    def __init__(self):
        self.base_url = settings.helpdesk_api_base_url.rstrip('/')
        self.timeout = settings.helpdesk_api_timeout

    # ── Request plumbing ───────────────────────────────────

    def _headers(self, tenant_slug: str) -> dict[str, str]:
        slug = (tenant_slug or '').strip().lower()
        if not slug:
            raise TenantNotConfiguredError('tenant slug is empty')
        api_key = settings.tenant_api_keys().get(slug)
        if not api_key:
            raise TenantNotConfiguredError(f'no helpdesk API key configured for tenant "{slug}"')
        headers = {
            'Content-Type': 'application/json',
            'X-API-Key': api_key,
            'X-Tenant-ID': slug,
        }
        suffix = settings.helpdesk_tenant_domain_suffix.strip()
        if suffix:
            headers['X-Forwarded-Host'] = f'{slug}{suffix}'
        return headers

    def _request(
        self,
        method: str,
        path: str,
        tenant_slug: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response | None:
        """Perform a tenant-scoped request. Returns None on transport/config errors."""
        try:
            headers = self._headers(tenant_slug)
            return httpx.request(
                method,
                f'{self.base_url}{_PREFIX}{path}',
                params=params,
                json=json,
                headers=headers,
                timeout=self.timeout,
            )
        except TenantNotConfiguredError as e:
            logger.error('Helpdesk call blocked: %s', e)
        except httpx.RequestError as e:
            logger.error('Helpdesk API request error (%s %s): %s', method, path, e)
        return None

    @staticmethod
    def _log_http_error(action: str, response: httpx.Response) -> None:
        # Body is truncated and never includes request headers/keys.
        logger.error(
            'Helpdesk API error %s (status %s): %s',
            action, response.status_code, response.text[:300],
        )

    # ── User registration ──────────────────────────────────

    def check_user_registration(self, phone_number: str, tenant_slug: str) -> dict[str, Any] | None:
        """
        Resolve a phone number to an active user of the tenant.
        Returns ``{'is_registered': bool, 'user_id': ..., ...}`` or ``None`` when the
        backend could not be asked (callers treat that as "unknown", not "registered").
        """
        response = self._request(
            'GET', '/users/by-phone', tenant_slug, params={'phone_number': phone_number}
        )
        if response is None:
            return None
        if response.status_code != 200:
            self._log_http_error('checking user registration', response)
            return None
        data = response.json()
        if data.get('is_registered') and data.get('user_id'):
            data['display_name'] = data.get('full_name')
        return data

    # ── Tickets ────────────────────────────────────────────

    def create_ticket(
        self,
        tenant_slug: str,
        title: str,
        user_id: str | UUID,
        description: str | None = None,
        category: str | None = None,
        cc_emails: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Create a ticket as the registered user. Returns the ticket or None."""
        payload: dict[str, Any] = {
            'user_id': str(user_id),
            'title': title,
            'description': description,
            'category': category,
            'cc_emails': cc_emails or None,
        }
        payload = {k: v for k, v in payload.items() if v is not None}
        response = self._request('POST', '/tickets', tenant_slug, json=payload)
        if response is None:
            return None
        if response.status_code not in (200, 201):
            self._log_http_error('creating ticket', response)
            return None
        result = _normalize_ticket(response.json())
        logger.info('Helpdesk ticket created: #%s (tenant=%s)', result.get('ticket_number', '?'), tenant_slug)
        return result

    def get_ticket_status(
        self, ticket_number: str, tenant_slug: str, user_id: str | UUID
    ) -> dict[str, Any] | None:
        """Get one ticket by number, only if it is visible to ``user_id``."""
        response = self._request(
            'GET',
            f'/tickets/by-number/{quote(ticket_number, safe="")}',
            tenant_slug,
            params={'user_id': str(user_id)},
        )
        if response is None:
            return None
        if response.status_code == 404:
            logger.info('Ticket %s not found/visible in tenant %s', ticket_number, tenant_slug)
            return None
        if response.status_code != 200:
            self._log_http_error('fetching ticket', response)
            return None
        return _normalize_ticket(response.json())

    def get_my_tickets(
        self,
        user_id: str | UUID,
        tenant_slug: str,
        status_name: str | None = None,
        skip: int = 0,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Tickets visible to a registered user (same visibility as the portal)."""
        params: dict[str, Any] = {'user_id': str(user_id), 'skip': skip, 'limit': limit}
        if status_name:
            params['status_name'] = status_name
        empty = {'tickets': [], 'total': 0, 'page': 1, 'per_page': limit}
        response = self._request('GET', '/tickets', tenant_slug, params=params)
        if response is None:
            return empty
        if response.status_code != 200:
            self._log_http_error('fetching my tickets', response)
            return empty
        data = response.json()
        data['tickets'] = [_normalize_ticket(t) for t in data.get('tickets', [])]
        return data

    def add_comment_as_user(
        self, ticket_number: str, user_id: str | UUID, tenant_slug: str, message: str
    ) -> dict[str, Any] | None:
        """Add a comment to a ticket as a registered user."""
        response = self._request(
            'POST',
            f'/tickets/by-number/{quote(ticket_number, safe="")}/comments',
            tenant_slug,
            json={'user_id': str(user_id), 'message': message},
        )
        if response is None:
            return None
        if response.status_code == 404:
            logger.info('Ticket %s not found/visible for comment in tenant %s', ticket_number, tenant_slug)
            return {'success': False, 'message': 'Ticket not found'}
        if response.status_code == 400:
            try:
                detail = response.json().get('detail')
            except ValueError:
                detail = None
            return {'success': False, 'message': detail or 'Comment rejected'}
        if response.status_code not in (200, 201):
            self._log_http_error('adding comment', response)
            return None
        return response.json()

    # ── Health ─────────────────────────────────────────────

    def health_check(self) -> bool:
        """Check that the helpdesk API host is reachable."""
        root = self.base_url.rsplit('/api/', 1)[0]
        try:
            return httpx.get(f'{root}/api/health', timeout=10).status_code < 500
        except Exception:
            return False
