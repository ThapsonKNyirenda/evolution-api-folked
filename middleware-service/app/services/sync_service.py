"""Sync service to pull data from the main helpdesk system into the middleware database."""

import uuid
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.repositories import (
    CustomerRepository,
    InstanceTenantRepository,
    RegisteredUserRepository,
    TenantRepository,
    TicketRepository,
)
from app.services.helpdesk_api import HelpdeskAPIClient

logger = logging.getLogger(__name__)


class SyncService:
    """Orchestrates one-way sync from the main helpdesk system into local tables."""

    def __init__(self, db: Session):
        self.db = db
        self.helpdesk_api = HelpdeskAPIClient()
        self.tenants_repo = TenantRepository(db)
        self.customers_repo = CustomerRepository(db)
        self.tickets_repo = TicketRepository(db)
        self.instance_tenants_repo = InstanceTenantRepository(db)
        self.registered_users_repo = RegisteredUserRepository(db)

    def _fetch_all_pages(self, fetch_fn, data_key: str, **kwargs) -> list[dict]:
        """
        Helper to fetch all pages from a paginated helpdesk API endpoint.
        Iterates through all pages until total items are fetched.
        fetch_fn: HelpdeskAPIClient method (e.g., self.helpdesk_api.list_tenants)
        data_key: key in response dict containing items (e.g., 'tenants')
        **kwargs: additional params (page and per_page are managed internally)
        """
        page = 1
        per_page = 200
        all_items = []

        while True:
            data = fetch_fn(page=page, per_page=per_page, **kwargs)
            items = data.get(data_key, [])
            all_items.extend(items)
            total = data.get('total', 0)
            if page * per_page >= total:
                break
            page += 1

        return all_items

    def sync_all(self) -> dict[str, Any]:
        """Run a full sync of tenants, customers, tickets, and registered_users."""
        result = {
            'tenants': self.sync_tenants(),
            'customers': {'synced': 0, 'errors': []},
            'tickets': {'synced': 0, 'errors': []},
            'registered_users': {'synced': 0, 'errors': []},
        }

        # Sync customers for each tenant
        for t in result['tenants'].get('synced_tenants', []):
            try:
                tenant_id = t.get('local_id') or t.get('id')
                if tenant_id:
                    cust_result = self.sync_customers(tenant_id=tenant_id)
                    result['customers']['synced'] += cust_result.get('synced', 0)
                    result['customers']['errors'].extend(cust_result.get('errors', []))
            except Exception as e:
                logger.error('Error syncing customers for tenant %s: %s', t.get('name'), e)
                result['customers']['errors'].append(str(e))

        # Sync tickets for each tenant
        for t in result['tenants'].get('synced_tenants', []):
            try:
                tenant_id = t.get('local_id') or t.get('id')
                if tenant_id:
                    ticket_result = self.sync_tickets(tenant_id=tenant_id)
                    result['tickets']['synced'] += ticket_result.get('synced', 0)
                    result['tickets']['errors'].extend(ticket_result.get('errors', []))
            except Exception as e:
                logger.error('Error syncing tickets for tenant %s: %s', t.get('name'), e)
                result['tickets']['errors'].append(str(e))

        # Sync registered_users for each tenant
        for t in result['tenants'].get('synced_tenants', []):
            try:
                tenant_id = t.get('local_id') or t.get('id')
                if tenant_id:
                    reg_result = self.sync_registered_users(tenant_id=tenant_id)
                    result['registered_users']['synced'] += reg_result.get('synced', 0)
                    result['registered_users']['errors'].extend(reg_result.get('errors', []))
            except Exception as e:
                logger.error('Error syncing registered_users for tenant %s: %s', t.get('name'), e)
                result['registered_users']['errors'].append(str(e))

        return result

    def sync_tenants(self) -> dict[str, Any]:
        """
        Pull all tenants from the helpdesk system and upsert locally.
        Iterates through all pages.
        Returns stats about what was synced.
        """
        synced = []
        errors = []

        try:
            tenants = self._fetch_all_pages(
                self.helpdesk_api.list_tenants,
                data_key='tenants',
            )
            logger.info('Fetched %d tenants from helpdesk API (all pages)', len(tenants))

            for ht in tenants:
                try:
                    helpdesk_id_str = ht.get('id')
                    if not helpdesk_id_str:
                        continue

                    helpdesk_id = uuid.UUID(helpdesk_id_str)
                    name = ht.get('name', 'Unknown')

                    # Check if tenant already exists by helpdesk_id
                    local_tenant = self.tenants_repo.get_by_helpdesk_id(helpdesk_id)

                    if local_tenant:
                        # Update existing tenant
                        self.tenants_repo.update(local_tenant, name=name)
                        logger.debug('Updated tenant %s (%s)', name, helpdesk_id)
                    else:
                        # Check if tenant exists by name (legacy)
                        local_tenant = self.tenants_repo.get_by_name(name)
                        if local_tenant:
                            self.tenants_repo.update(local_tenant, name=name, helpdesk_tenant_id=helpdesk_id)
                            logger.debug('Linked existing tenant %s to helpdesk_id %s', name, helpdesk_id)
                        else:
                            # Create new tenant
                            local_tenant = self.tenants_repo.create(name=name)
                            # Update with helpdesk_id after create
                            self.tenants_repo.update(local_tenant, helpdesk_tenant_id=helpdesk_id, name=name)
                            logger.debug('Created tenant %s from helpdesk (%s)', name, helpdesk_id)

                    synced.append({
                        'id': str(local_tenant.id),
                        'name': name,
                        'helpdesk_id': helpdesk_id_str,
                    })
                except Exception as e:
                    logger.warning('Error syncing tenant %s: %s', ht.get('name', '?'), e)
                    errors.append(f"Tenant {ht.get('name', '?')}: {e}")

        except Exception as e:
            logger.error('Failed to fetch tenants from helpdesk: %s', e)
            errors.append(f"Fetch tenants: {e}")

        return {'synced': len(synced), 'synced_tenants': synced, 'errors': errors}

    def sync_customers(self, tenant_id: uuid.UUID | None = None) -> dict[str, Any]:
        """
        Pull all customers from the helpdesk system for a given tenant.
        If tenant_id is provided, only fetch customers for that local tenant.
        Iterates through all pages.
        Returns stats about what was synced.
        """
        synced = []
        errors = []

        try:
            # Resolve helpdesk tenant_id from local tenant_id
            helpdesk_tenant_id = None
            if tenant_id:
                local_tenant = self.tenants_repo.get(tenant_id)
                if local_tenant and local_tenant.helpdesk_tenant_id:
                    helpdesk_tenant_id = str(local_tenant.helpdesk_tenant_id)

            customers = self._fetch_all_pages(
                self.helpdesk_api.list_customers,
                data_key='customers',
                tenant_id=helpdesk_tenant_id,
            )
            logger.info('Fetched %d customers from helpdesk API (all pages)', len(customers))

            for hc in customers:
                try:
                    helpdesk_customer_id_str = hc.get('id')
                    helpdesk_customer_id = uuid.UUID(helpdesk_customer_id_str) if helpdesk_customer_id_str else None

                    phone = hc.get('phone') or ''
                    name = hc.get('name') or ''
                    email = hc.get('email') or None
                    h_tenant_id_str = hc.get('tenant_id')
                    h_tenant_id = uuid.UUID(h_tenant_id_str) if h_tenant_id_str else None

                    # Find the local tenant by helpdesk_tenant_id
                    local_tenant_id = None
                    if h_tenant_id:
                        local_tenant = self.tenants_repo.get_by_helpdesk_id(h_tenant_id)
                        if local_tenant:
                            local_tenant_id = local_tenant.id

                    if not local_tenant_id:
                        logger.debug('Skipping customer %s - no matching local tenant', name or phone)
                        continue

                    # Try to find by helpdesk_customer_id first, then by phone+tenant
                    local_customer = None
                    if helpdesk_customer_id:
                        local_customer = self.customers_repo.get_by_helpdesk_id(helpdesk_customer_id)

                    if not local_customer and phone and local_tenant_id:
                        local_customer = self.customers_repo.get_by_phone_and_tenant(phone, local_tenant_id)

                    if local_customer:
                        update_kwargs = {}
                        if name:
                            update_kwargs['name'] = name
                        if email:
                            update_kwargs['email'] = email
                        if helpdesk_customer_id and not local_customer.helpdesk_customer_id:
                            update_kwargs['helpdesk_customer_id'] = helpdesk_customer_id
                        if update_kwargs:
                            self.customers_repo.update(local_customer, **update_kwargs)
                    else:
                        # Create new customer
                        from app.models.customer import Customer as CustomerModel
                        new_customer = CustomerModel(
                            phone_number=phone or f"unknown-{uuid.uuid4().hex[:8]}",
                            name=name or None,
                            email=email,
                            tenant_id=local_tenant_id,
                            helpdesk_customer_id=helpdesk_customer_id,
                        )
                        self.db.add(new_customer)
                        self.db.commit()
                        self.db.refresh(new_customer)
                        local_customer = new_customer

                    synced.append({
                        'id': str(local_customer.id),
                        'name': local_customer.name,
                        'phone': local_customer.phone_number,
                        'tenant_id': str(local_customer.tenant_id),
                    })
                except Exception as e:
                    logger.warning('Error syncing customer %s: %s', hc.get('name', '?'), e)
                    errors.append(f"Customer {hc.get('name', '?')}: {e}")

        except Exception as e:
            logger.error('Failed to fetch customers from helpdesk: %s', e)
            errors.append(f"Fetch customers: {e}")

        return {'synced': len(synced), 'errors': errors}

    def sync_tickets(
        self,
        tenant_id: uuid.UUID | None = None,
        customer_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        """
        Pull all tickets from the helpdesk system for a given tenant.
        Iterates through all pages.
        Returns stats about what was synced.
        """
        synced = []
        errors = []

        try:
            # Resolve helpdesk tenant_id from local tenant_id
            helpdesk_tenant_id = None
            if tenant_id:
                local_tenant = self.tenants_repo.get(tenant_id)
                if local_tenant and local_tenant.helpdesk_tenant_id:
                    helpdesk_tenant_id = str(local_tenant.helpdesk_tenant_id)

            tickets = self._fetch_all_pages(
                self.helpdesk_api.list_tickets,
                data_key='tickets',
                tenant_id=helpdesk_tenant_id,
                customer_id=str(customer_id) if customer_id else None,
            )
            logger.info('Fetched %d tickets from helpdesk API (all pages)', len(tickets))

            for ht in tickets:
                try:
                    helpdesk_ticket_id_str = ht.get('id')
                    helpdesk_ticket_id = uuid.UUID(helpdesk_ticket_id_str) if helpdesk_ticket_id_str else None
                    ticket_number = ht.get('ticket_number', '')
                    subject = ht.get('title') or ht.get('subject', '')
                    description = ht.get('description')
                    status = (ht.get('status') or 'Open').lower()
                    category = ht.get('category')
                    h_tenant_id_str = ht.get('tenant_id')
                    h_customer_id_str = ht.get('customer_id')

                    # Find local tenant
                    local_tenant_id = None
                    if h_tenant_id_str:
                        h_tenant_id = uuid.UUID(h_tenant_id_str)
                        local_tenant = self.tenants_repo.get_by_helpdesk_id(h_tenant_id)
                        if local_tenant:
                            local_tenant_id = local_tenant.id

                    if not local_tenant_id:
                        logger.debug('Skipping ticket %s - no matching local tenant', ticket_number)
                        continue

                    # Find local customer
                    local_customer_id = None
                    if h_customer_id_str:
                        h_customer_id = uuid.UUID(h_customer_id_str)
                        local_customer = self.customers_repo.get_by_helpdesk_id(h_customer_id)
                        if local_customer:
                            local_customer_id = local_customer.id

                    if not local_customer_id:
                        logger.debug('Skipping ticket %s - no matching local customer', ticket_number)
                        continue

                    # Try to find existing ticket
                    local_ticket = self.tickets_repo.get_by_helpdesk_id(helpdesk_ticket_id) if helpdesk_ticket_id else None
                    if not local_ticket:
                        local_ticket = self.tickets_repo.get_by_number(ticket_number, local_tenant_id)

                    if local_ticket:
                        # Update existing ticket
                        self.tickets_repo.update(
                            local_ticket,
                            subject=subject,
                            description=description,
                            status=status,
                            category=category,
                        )
                    else:
                        # Create new ticket
                        local_ticket = self.tickets_repo.create(
                            tenant_id=local_tenant_id,
                            customer_id=local_customer_id,
                            subject=subject,
                            description=description,
                            category=category,
                            source='helpdesk-sync',
                            helpdesk_ticket_id=helpdesk_ticket_id,
                        )
                        # Update ticket number if it came from helpdesk
                        if ticket_number and local_ticket.ticket_number != ticket_number:
                            self.tickets_repo.update(local_ticket, ticket_number=ticket_number)

                    synced.append({
                        'id': str(local_ticket.id),
                        'ticket_number': local_ticket.ticket_number,
                        'subject': local_ticket.subject,
                        'status': local_ticket.status,
                    })
                except Exception as e:
                    logger.warning('Error syncing ticket %s: %s', ht.get('ticket_number', '?'), e)
                    errors.append(f"Ticket {ht.get('ticket_number', '?')}: {e}")

        except Exception as e:
            logger.error('Failed to fetch tickets from helpdesk: %s', e)
            errors.append(f"Fetch tickets: {e}")

        return {'synced': len(synced), 'errors': errors}

    def sync_registered_users(self, tenant_id: uuid.UUID) -> dict[str, Any]:
        """
        Pull internal users from the helpdesk system and update matching
        registered_users entries with the latest cached details.
        Only updates users that already exist in registered_users
        (i.e., users that were linked via /phone-user/link).
        Does NOT auto-create new registered_users entries.
        Returns stats about what was synced.
        """
        synced = []
        errors = []

        try:
            # Resolve helpdesk tenant_id from local tenant_id
            local_tenant = self.tenants_repo.get(tenant_id)
            if not local_tenant or not local_tenant.helpdesk_tenant_id:
                logger.warning('No helpdesk tenant mapping for local tenant %s', tenant_id)
                return {'synced': 0, 'errors': ['No helpdesk tenant mapping']}

            helpdesk_tenant_id = str(local_tenant.helpdesk_tenant_id)

            # Fetch all users for this tenant from helpdesk
            users = self._fetch_all_pages(
                self.helpdesk_api.list_users,
                data_key='users',
                tenant_id=helpdesk_tenant_id,
            )
            logger.info('Fetched %d users from helpdesk for tenant %s', len(users), tenant_id)

            # Build lookup by helpdesk_user_id
            helpdesk_users = {}
            for u in users:
                uid = u.get('id')
                if uid:
                    helpdesk_users[uid] = u

            # Get all registered_users for this local tenant
            local_registered = self.registered_users_repo.list_by_tenant(tenant_id, active_only=False)

            for reg in local_registered:
                h_user_id = str(reg.helpdesk_user_id)
                h_user = helpdesk_users.get(h_user_id)
                if not h_user:
                    # User no longer exists in helpdesk or is now a wa_ user — deactivate
                    if reg.is_active:
                        self.registered_users_repo.deactivate(reg.id)
                        logger.info('Deactivated registered_user %s - user not found in helpdesk', reg.phone_number)
                        synced.append({'phone': reg.phone_number, 'action': 'deactivated'})
                    continue

                # Update cached fields
                updates = {}
                for field in ['username', 'email', 'first_name', 'last_name', 'display_name']:
                    h_value = h_user.get(field)
                    current = getattr(reg, field, None)
                    if h_value and h_value != current:
                        updates[field] = h_value

                # Sync is_active status
                h_active = h_user.get('is_active', True)
                if h_active != reg.is_active:
                    updates['is_active'] = h_active

                # Sync helpdesk_tenant_id if missing
                h_tenant = h_user.get('tenant_id')
                if h_tenant and not reg.helpdesk_tenant_id:
                    try:
                        updates['helpdesk_tenant_id'] = uuid.UUID(h_tenant)
                    except (ValueError, TypeError):
                        pass

                if updates:
                    self.registered_users_repo.get_or_create(
                        phone_number=reg.phone_number,
                        tenant_id=tenant_id,
                        helpdesk_user_id=reg.helpdesk_user_id,
                        helpdesk_tenant_id=reg.helpdesk_tenant_id,
                        **updates,
                    )
                    synced.append({'phone': reg.phone_number, 'action': 'updated', 'fields': list(updates.keys())})
                else:
                    synced.append({'phone': reg.phone_number, 'action': 'no_change'})

        except Exception as e:
            logger.error('Failed to sync registered_users: %s', e)
            errors.append(f"Sync registered_users: {e}")

        return {'synced': len(synced), 'errors': errors}
