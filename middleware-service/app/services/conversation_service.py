import uuid
import logging

from sqlalchemy.orm import Session

from app.models.whatsapp_session import WhatsappSession
from app.repositories import (
    CommandLogRepository,
    CustomerRepository,
    EventLogRepository,
    InstanceTenantRepository,
    PhoneRegistryRepository,
    RegisteredUserRepository,
    TenantRepository,
    TicketCommentRepository,
    TicketMessageRepository,
    TicketRepository,
    WhatsappSessionRepository,
)
from app.services.helpdesk_api import HelpdeskAPIClient

logger = logging.getLogger(__name__)


CANCEL_KEYWORDS = {'0', 'cancel', 'menu', 'start over', 'restart', 'exit'}
ESCALATE_KEYWORDS = {'3', 'agent', 'human', 'support'}
CATEGORIES = {
    '1': 'Network',
    '2': 'Billing',
    '3': 'Technical Support',
    '4': 'Other',
}
CATEGORY_OPTIONS_TEXT = '\n'.join([f'{k}. {v}' for k, v in CATEGORIES.items()])

SESSION_TIMEOUT_MINUTES = 10


def _is_cancel(text: str) -> bool:
    return text.strip().lower() in CANCEL_KEYWORDS


def _is_escalate(text: str) -> bool:
    return text.strip().lower() in ESCALATE_KEYWORDS


def _text_reply(text: str) -> dict:
    return {'type': 'text', 'text': text}


def _buttons_reply(text: str, title: str, buttons: list[dict], footer: str | None = None) -> dict:
    reply: dict = {'type': 'buttons', 'text': text, 'title': title, 'buttons': buttons}
    if footer:
        reply['footer'] = footer
    return reply


def _list_reply(text: str, title: str, button_text: str, sections: list[dict], footer: str | None = None) -> dict:
    reply: dict = {
        'type': 'list',
        'text': text,
        'title': title,
        'button_text': button_text,
        'sections': sections,
    }
    if footer:
        reply['footer'] = footer
    return reply


def _cancel_reply() -> dict:
    """Return a text-based main menu when user cancels or requests menu."""
    return _text_reply(
        '📋 *Main Menu*\n\n'
        'Reply with a number:\n'
        '1️⃣ ✉️ Create Ticket\n'
        '2️⃣ 🔍 Check Ticket\n'
        '3️⃣ 💬 Speak to Agent\n\n'
        'Or just describe your issue and we\'ll help!'
    )


def _build_main_menu() -> dict:
    return _buttons_reply(
        text='Choose an option below to get started:',
        title='👋 Welcome to Support!',
        buttons=[
            {'type': 'reply', 'displayText': '✉️ Create Ticket', 'id': 'create_ticket'},
            {'type': 'reply', 'displayText': '🔍 Check Ticket', 'id': 'check_ticket'},
            {'type': 'reply', 'displayText': '💬 Speak to Agent', 'id': 'speak_agent'},
        ],
        footer='Or type 0 to see the menu',
    )


def _build_category_list() -> dict:
    return _text_reply(
        '📁 *Select a Category*\n\n'
        'Please reply with the number of your category:\n\n'
        '1️⃣ 🌐 *Network* — Internet, connectivity, VPN\n'
        '2️⃣ 💳 *Billing* — Invoices, payments, subscriptions\n'
        '3️⃣ 🔧 *Technical Support* — Software, hardware, errors\n'
        '4️⃣ ❓ *Other* — Anything not listed above\n\n'
        'Send *0* at any time to return to the main menu.'
    )


def _build_confirm_buttons(draft: dict) -> dict:
    subject = draft.get('subject', 'N/A')[:100]
    description = draft.get('description', 'N/A')
    category = draft.get('category', 'N/A')

    details = (
        f'\u2022 *Subject:* {subject}\n'
        f'\u2022 *Description:* {description}\n'
        f'\u2022 *Category:* {category}\n'
    )

    return _buttons_reply(
        text=f'Please review and confirm your ticket details:\n\n{details}',
        title='\u2705 Confirm Ticket',
        buttons=[
            {'type': 'reply', 'displayText': '\u2705 Submit', 'id': 'confirm_submit'},
            {'type': 'reply', 'displayText': '\u270F\uFE0F Edit Subject', 'id': 'confirm_edit_subject'},
            {'type': 'reply', 'displayText': '\u274C Cancel', 'id': 'confirm_cancel'},
        ],
        footer='Or send 0 to cancel',
    )


def _build_confirm_text(draft: dict) -> dict:
    """Text-based confirm prompt for when user sends invalid input at confirm step."""
    subject = draft.get('subject', 'N/A')[:100]
    description = draft.get('description', 'N/A')
    category = draft.get('category', 'N/A')

    details = (
        f'\u2022 *Subject:* {subject}\n'
        f'\u2022 *Description:* {description}\n'
        f'\u2022 *Category:* {category}\n'
    )

    return _text_reply(
        f'\u2705 *Confirm Ticket*\n\n'
        f'{details}\n'
        f'Reply with:\n'
        f'1️⃣ Submit\n'
        f'2️⃣ Edit Subject\n'
        f'3️⃣ Cancel\n'
        f'0️⃣ Main Menu'
    )


def _build_ticket_created(ticket_data: dict) -> dict:
    ticket_number = ticket_data.get('ticket_number', 'N/A')
    category = ticket_data.get('category') or 'N/A'
    return _text_reply(
        f'\u2705 *Ticket Created Successfully!*\n\n'
        f'\u2022 *Ticket Number:* `{ticket_number}`\n'
        f'\u2022 *Status:* Open\n'
        f'\u2022 *Category:* {category}\n\n'
        f'Our team will review your request and get back to you as soon as possible.\n\n'
        f'To return to the main menu at any time, send *0*.'
    )


def _build_ticket_status(ticket_data: dict) -> dict:
    ticket_number = ticket_data.get('ticket_number', 'N/A')
    status = ticket_data.get('status', 'UNKNOWN')
    title = ticket_data.get('title', 'N/A')
    category = ticket_data.get('category') or 'N/A'
    created_at_raw = ticket_data.get('created_at', '')
    created_str = created_at_raw[:10] if created_at_raw and len(created_at_raw) >= 10 else 'N/A'

    return _text_reply(
        f'🔍 *Ticket Details*\n\n'
        f'\u2022 *Number:* `{ticket_number}`\n'
        f'\u2022 *Status:* `{status}`\n'
        f'\u2022 *Subject:* {title}\n'
        f'\u2022 *Category:* {category}\n'
        f'\u2022 *Created:* {created_str}\n\n'
        f'Send *0* to return to the main menu.'
    )


def _build_escalate_reply() -> dict:
    return _text_reply(
        '💬 *Speak to Agent*\n\n'
        'An agent will be with you shortly. In the meantime, please describe your issue '
        'and we will make sure the right team handles it.\n\n'
        'Send *0* to return to the main menu.'
    )


def _build_subject_prompt() -> dict:
    return _text_reply(
        '**Step 1: Subject*\n\n'
        'Please enter a *short subject* for your ticket (e.g., "Internet not working", '
        '"Payment issue", "Account access problem").\n\n'
        'Keep it brief  one line is enough.\n\n'
        'Send *0* at any time to cancel and return to the main menu.'
    )


def _build_description_prompt() -> dict:
    return _text_reply(
        '📝 *Step 2: Description*\n\n'
        'Now please describe your issue in *detail*:\n'
        ' What happened?\n'
        ' When did it start?\n'
        ' Any error messages?\n\n'
        'The more detail you provide, the faster we can help you.\n\n'
        'Send *0* at any time to cancel and return to the main menu.'
    )


class ConversationService:
    def __init__(self, db: Session):
        self.db = db
        self.customers = CustomerRepository(db)
        self.tickets = TicketRepository(db)
        self.ticket_messages = TicketMessageRepository(db)
        self.sessions = WhatsappSessionRepository(db)
        self.instance_tenants = InstanceTenantRepository(db)
        self.tenants = TenantRepository(db)
        self.phone_registry = PhoneRegistryRepository(db)
        self.registered_users = RegisteredUserRepository(db)
        self.helpdesk_api = HelpdeskAPIClient()

    def _resolve_helpdesk_tenant_id(self, local_tenant_id: uuid.UUID) -> str:
        """
        Translate a local middleware tenant ID to the corresponding
        helpdesk tenant ID by looking up the helpdesk_tenant_id mapping.
        Falls back to the local ID if no mapping exists.
        """
        tenant = self.tenants.get(local_tenant_id)
        if tenant and tenant.helpdesk_tenant_id:
            return str(tenant.helpdesk_tenant_id)
        return str(local_tenant_id)

    def _is_session_stale(self, session: WhatsappSession) -> bool:
        from datetime import datetime, timedelta
        cutoff = datetime.utcnow() - timedelta(minutes=SESSION_TIMEOUT_MINUTES)
        return session.last_activity < cutoff and session.state != 'MAIN_MENU'

    def _check_user_registration(self, phone_number: str, tenant_id: uuid.UUID) -> dict | None:
        """
        Check if the phone number is registered to a user account.
        
        First checks the local registered_users table (populated by sync from helpdesk).
        If not found locally, falls back to querying the helpdesk API directly.
        This ensures that recently assigned phone numbers work immediately
        without waiting for the next sync cycle.
        
        Returns user info dict if registered, None if not registered.
        """
        try:
            registered_user = self.registered_users.get_by_phone_and_tenant(phone_number, tenant_id)
            if registered_user and registered_user.is_active:
                return {
                    "is_registered": True,
                    "user_id": str(registered_user.helpdesk_user_id),
                    "phone_number": registered_user.phone_number,
                    "username": registered_user.username,
                    "email": registered_user.email,
                    "first_name": registered_user.first_name,
                    "last_name": registered_user.last_name,
                    "display_name": registered_user.display_name,
                    "is_active": registered_user.is_active,
                    "tenant_id": str(registered_user.helpdesk_tenant_id or registered_user.tenant_id),
                }

            # Fallback: check helpdesk API directly (in case sync hasn't run yet)
            helpdesk_tenant_id = self._resolve_helpdesk_tenant_id(tenant_id)
            helpdesk_result = self.helpdesk_api.check_user_registration(
                phone_number=phone_number,
                tenant_id=helpdesk_tenant_id,
            )

            if helpdesk_result and helpdesk_result.get("is_registered"):
                # Found in helpdesk — create local registered_user entry for future lookups
                try:
                    self.registered_users.get_or_create(
                        phone_number=phone_number,
                        tenant_id=tenant_id,
                        helpdesk_user_id=uuid.UUID(helpdesk_result["user_id"]),
                        helpdesk_tenant_id=uuid.UUID(helpdesk_result.get("tenant_id", str(tenant_id))),
                        username=helpdesk_result.get("username"),
                        email=helpdesk_result.get("email"),
                        first_name=helpdesk_result.get("first_name"),
                        last_name=helpdesk_result.get("last_name"),
                        display_name=helpdesk_result.get("display_name"),
                        is_active=helpdesk_result.get("is_active", True),
                    )
                    logger.info(
                        "Auto-created registered_user from helpdesk fallback: phone=%s",
                        phone_number,
                    )
                except Exception as e:
                    logger.warning(
                        "Failed to create local registered_user from helpdesk fallback: %s", e
                    )

                return helpdesk_result

            return None
        except Exception as e:
            logger.warning(
                "Failed to check user registration locally: %s (phone=%s)",
                e,
                phone_number,
            )
            return None

    def _sync_customer_with_helpdesk(
        self, phone_number: str, tenant_id: uuid.UUID, push_name: str | None = None
    ) -> None:
        """
        Ensure the customer exists in the main helpdesk system and link them.
        Updates the local customer record with the helpdesk_customer_id.
        Translates local tenant_id to the helpdesk tenant_id.
        """
        try:
            helpdesk_tenant_id = self._resolve_helpdesk_tenant_id(tenant_id)
            helpdesk_customer = self.helpdesk_api.get_or_create_customer(
                tenant_id=helpdesk_tenant_id,
                phone_number=phone_number,
                customer_name=push_name,
            )
            if helpdesk_customer and helpdesk_customer.get("id"):
                helpdesk_id = helpdesk_customer["id"]
                # Update local customer record with helpdesk reference
                local_customer = self.customers.get_by_phone_and_tenant(
                    phone_number, tenant_id
                )
                if local_customer and not local_customer.helpdesk_customer_id:
                    self.customers.update(
                        local_customer,
                        helpdesk_customer_id=uuid.UUID(helpdesk_id)
                        if isinstance(helpdesk_id, str)
                        else helpdesk_id,
                    )
                    # Also update the phone registry
                    try:
                        registry_entry = self.phone_registry.get_by_phone_and_tenant(
                            phone_number, tenant_id
                        )
                        if registry_entry:
                            self.phone_registry.update_helpdesk_id(
                                registry_entry.id,
                                uuid.UUID(helpdesk_id) if isinstance(helpdesk_id, str) else helpdesk_id,
                            )
                    except Exception as reg_err:
                        logger.warning(
                            "Failed to update phone registry helpdesk ID: %s",
                            reg_err,
                        )
        except Exception as e:
            logger.warning(
                "Failed to sync customer with helpdesk: %s (phone=%s, continuing with local)",
                e,
                phone_number,
            )

    def process_message(
        self, instance_name: str, phone_number: str, text: str, message_id: str | None = None, push_name: str | None = None
    ) -> dict:
        link = self.instance_tenants.get_by_instance(instance_name)
        if not link:
            logger.warning('No tenant linked to instance %s', instance_name)
            return _text_reply('This support line is not configured. Please contact the administrator.')

        tenant_id = link.tenant_id

        # Check if user is registered in helpdesk FIRST — this determines
        # whether we treat them as a customer or a registered user (agent).
        registration = self._check_user_registration(phone_number, tenant_id)

        customer = None
        customer_id = None

        if registration:
            # Registered user (helpdesk agent) — do NOT force-create a Customer record.
            # Just register the phone in the registry without a customer link.
            # Do NOT call _sync_customer_with_helpdesk — that would auto-create
            # a customer in the helpdesk system, which we don't want for agents.
            self.phone_registry.get_or_create(phone_number, tenant_id, customer_id=None)
        else:
            # Not registered — treat as a regular customer
            customer = self.customers.get_or_create(phone_number, tenant_id)
            customer_id = customer.id
            self.phone_registry.get_or_create(phone_number, tenant_id, customer_id)

            # Update push name if available
            if customer and not customer.name:
                name = push_name or ''
                if name:
                    self.customers.update(customer, name=name)

            # Sync customer with helpdesk (best-effort)
            self._sync_customer_with_helpdesk(phone_number, tenant_id, push_name)

            # Update phone registry with latest helpdesk customer ID
            helpdesk_id = customer.helpdesk_customer_id
            if helpdesk_id:
                try:
                    registry_entry = self.phone_registry.get_by_phone_and_tenant(
                        phone_number, tenant_id
                    )
                    if registry_entry and not registry_entry.helpdesk_customer_id:
                        self.phone_registry.update_helpdesk_id(
                            registry_entry.id, helpdesk_id
                        )
                except Exception as e:
                    logger.warning("Failed to update phone registry helpdesk ID: %s", e)

        session = self.sessions.get_or_create(phone_number, tenant_id)
        if customer_id:
            self.sessions.set_customer(session, customer_id)

        if self._is_session_stale(session):
            session.ticket_draft = None
            session.state = 'MAIN_MENU'

        reply = self._handle_state(session, text.strip(), customer_id, tenant_id)
        self.sessions.update_state(session, session.state)
        return reply

    def _handle_state(
        self, session: WhatsappSession, text: str, customer_id: uuid.UUID | None, tenant_id: uuid.UUID
    ) -> dict:
        state = session.state

        if state == 'MAIN_MENU':
            return self._handle_main_menu(session, text, customer_id, tenant_id)

        if state == 'WAITING_SUBJECT':
            return self._handle_subject(session, text)

        if state == 'WAITING_DESCRIPTION':
            return self._handle_description(session, text)

        if state == 'WAITING_CATEGORY':
            return self._handle_category(session, text)

        if state == 'CONFIRM_TICKET':
            return self._handle_confirm(session, text, customer_id, tenant_id)

        if state in ('CHECKING_TICKET',):
            return self._handle_check_ticket(session, text, tenant_id, customer_id)

        session.state = 'MAIN_MENU'
        return _build_main_menu()

    def _handle_main_menu(
        self, session: WhatsappSession, text: str, customer_id: uuid.UUID | None, tenant_id: uuid.UUID
    ) -> dict:
        choice = text.strip().lower()

        if choice in ('0', 'menu', 'cancel'):
            # Explicit menu request — show text menu
            return _cancel_reply()

        if choice in ('1', 'create_ticket', 'create ticket'):
            # Check if user is registered in helpdesk before allowing ticket creation
            if customer_id is not None:
                customer = self.customers.get(customer_id)
                if customer:
                    registration = self._check_user_registration(customer.phone_number, tenant_id)
                    if not registration:
                        return _text_reply(
                            '🔒 *Registration Required*\n\n'
                            'Your phone number is not registered to a user account in the helpdesk system.\n\n'
                            'Please contact your administrator to register your phone number before using the WhatsApp chatbot.\n\n'
                            'Send *0* to return to the main menu.'
                        )
                session.ticket_draft = {}
                session.state = 'WAITING_SUBJECT'
                return _build_subject_prompt()
            else:
                # Registered agent without a customer record — use the session's
                # phone number to check registration
                registration = self._check_user_registration(session.phone_number, tenant_id)
                if registration:
                    session.ticket_draft = {}
                    session.state = 'WAITING_SUBJECT'
                    return _build_subject_prompt()
                return _text_reply(
                    '🔒 *Registration Required*\n\n'
                    'Your phone number is not registered to a user account.\n\n'
                    'Send *0* to return to the main menu.'
                )

        if choice in ('2', 'check_ticket', 'check ticket', 'my tickets', 'my_tickets'):
            tickets = self.tickets.list_all(tenant_id=tenant_id, customer_id=customer_id, limit=10)
            if not tickets:
                session.state = 'MAIN_MENU'
                return _text_reply(
                    ' *No Tickets Found*\n\n'
                    'You have no tickets yet. Send *1* to create a new ticket.\n\n'
                    '*0* to return to the main menu.'
                )

            lines = [' *Your Tickets*\n']
            for t in tickets:
                lines.append(f' `{t.ticket_number}`  *{t.subject[:50]}* ({t.status})')
            lines.append('\nReply with a ticket number to see details, or *0* for menu.')

            session.state = 'CHECKING_TICKET'
            return _text_reply(
                '🔍 *Check Ticket Status*\n\n'
                'Please enter your ticket number (e.g., `TKT-2026-00001`).\n\n'
                'Send *0* to return to the main menu.'
            )

        if choice in ('3', 'speak_agent', 'speak to agent', 'agent'):
            session.state = 'MAIN_MENU'
            return _build_escalate_reply()

        # For unrecognized text, show the text menu instead of buttons
        # (buttons may not display properly if previous ones are still visible)
        return _cancel_reply()

    def _handle_subject(self, session: WhatsappSession, text: str) -> dict:
        if _is_cancel(text):
            session.ticket_draft = None
            session.state = 'MAIN_MENU'
            return _cancel_reply()

        draft = dict(session.ticket_draft or {})
        draft['subject'] = text[:200]
        session.ticket_draft = draft
        session.state = 'WAITING_DESCRIPTION'
        return _build_description_prompt()

    def _handle_description(self, session: WhatsappSession, text: str) -> dict:
        if _is_cancel(text):
            session.ticket_draft = None
            session.state = 'MAIN_MENU'
            return _cancel_reply()

        draft = dict(session.ticket_draft or {})
        draft['description'] = text
        session.ticket_draft = draft
        session.state = 'WAITING_CATEGORY'
        return _build_category_list()

    def _handle_category(self, session: WhatsappSession, text: str) -> dict:
        if _is_cancel(text):
            session.ticket_draft = None
            session.state = 'MAIN_MENU'
            return _cancel_reply()

        normalized = text.strip().lower()

        category_map = {
            '1': 'Network', 'cat_network': 'Network', 'network': 'Network',
            '2': 'Billing', 'cat_billing': 'Billing', 'billing': 'Billing',
            '3': 'Technical Support', 'cat_tech': 'Technical Support',
            'technical support': 'Technical Support', 'tech': 'Technical Support',
            '4': 'Other', 'cat_other': 'Other', 'other': 'Other',
        }

        category = category_map.get(normalized)
        if not category:
            return _build_category_list()

        draft = dict(session.ticket_draft or {})
        draft['category'] = category
        session.ticket_draft = draft
        session.state = 'CONFIRM_TICKET'
        # Use text-based confirmation instead of interactive buttons for
        # better compatibility across WhatsApp clients
        return _build_confirm_text(draft)

    def _handle_confirm(
        self, session: WhatsappSession, text: str, customer_id: uuid.UUID | None, tenant_id: uuid.UUID
    ) -> dict:
        normalized = text.strip().lower()

        if normalized in ('confirm_submit', '1', 'submit', 'yes', 'confirm'):
            draft = session.ticket_draft or {}
            subject = draft.get('subject', 'No subject')
            description = draft.get('description')
            category = draft.get('category')

            if not subject or subject == 'No subject':
                session.state = 'WAITING_SUBJECT'
                return _build_subject_prompt()

            phone_number = session.phone_number

            # Get the registered user's helpdesk info for creator_id and tenant
            creator_id = None
            registered_user = self.registered_users.get_by_phone_and_tenant(
                phone_number, tenant_id
            )
            if registered_user:
                creator_id = registered_user.helpdesk_user_id

            # Use the registered user's helpdesk tenant ID if available,
            # otherwise fall back to resolving from local tenant mapping.
            if registered_user and registered_user.helpdesk_tenant_id:
                helpdesk_tenant_id = str(registered_user.helpdesk_tenant_id)
            else:
                helpdesk_tenant_id = self._resolve_helpdesk_tenant_id(tenant_id)

            # Resolve customer info for the ticket
            customer = self.customers.get(customer_id) if customer_id else None
            ticket_customer_id = customer.helpdesk_customer_id if customer and customer.helpdesk_customer_id else None

            # Create ticket via the real helpdesk backend with full details
            ticket_data = self.helpdesk_api.create_ticket(
                tenant_id=helpdesk_tenant_id,
                title=subject,
                description=description,
                creator_id=creator_id,
                customer_id=ticket_customer_id,
                category=category,
                priority=None,  # Will use default
                channel='WhatsApp',
                phone_number=phone_number,
            )

            if ticket_data:
                # Store the helpdesk ticket reference locally (only if we have a customer)
                helpdesk_ticket_id = ticket_data.get("id")
                if helpdesk_ticket_id and customer_id is not None:
                    try:
                        self.tickets.create(
                            tenant_id=tenant_id,
                            customer_id=customer_id,
                            subject=subject,
                            description=description,
                            category=category,
                            source='whatsapp',
                            helpdesk_ticket_id=uuid.UUID(helpdesk_ticket_id)
                            if isinstance(helpdesk_ticket_id, str)
                            else helpdesk_ticket_id,
                        )
                    except Exception as e:
                        logger.warning(
                            "Failed to create local ticket reference: %s", e
                        )

                session.ticket_draft = None
                session.state = 'MAIN_MENU'
                return _build_ticket_created(ticket_data)

            # Fallback: if helpdesk API fails, use local database
            if customer_id is None:
                # Cannot create local ticket without a customer reference
                logger.warning("Cannot fall back to local ticket: no customer_id for agent %s", session.phone_number)
                session.ticket_draft = None
                session.state = 'MAIN_MENU'
                return _text_reply(
                    '❌ *Ticket Creation Failed*\n\n'
                    'Unable to create ticket. The helpdesk system is currently unavailable.\n\n'
                    'Please try again later or contact your administrator.\n\n'
                    'Send *0* to return to the main menu.'
                )

            logger.warning(
                "Helpdesk API unavailable, falling back to local ticket creation"
            )
            ticket = self.tickets.create(
                tenant_id=tenant_id,
                customer_id=customer_id,
                subject=subject,
                description=description,
                category=category,
                source='whatsapp',
            )

            self.ticket_messages.create(
                ticket_id=ticket.id,
                content=description or subject,
                from_whatsapp=True,
            )

            session.ticket_draft = None
            session.state = 'MAIN_MENU'
            return _build_ticket_created({
                'ticket_number': ticket.ticket_number,
                'category': ticket.category,
            })

        if normalized in ('confirm_edit_subject', '2', 'edit subject', 'edit'):
            session.state = 'WAITING_SUBJECT'
            return _build_subject_prompt()

        if normalized in ('confirm_cancel', '3', 'cancel', '0', 'menu'):
            session.ticket_draft = None
            session.state = 'MAIN_MENU'
            return _cancel_reply()

        draft = session.ticket_draft or {}
        return _build_confirm_text(draft)

    def _handle_check_ticket(self, session: WhatsappSession, text: str, tenant_id: uuid.UUID, customer_id: uuid.UUID | None) -> dict:
        if _is_cancel(text):
            session.state = 'MAIN_MENU'
            return _cancel_reply()

        ticket_number = text.strip().upper()

        # Use the registered user's helpdesk tenant ID if available
        helpdesk_tenant_id = self._resolve_helpdesk_tenant_id(tenant_id)
        if customer_id is not None:
            customer = self.customers.get(customer_id)
            if customer:
                registered_user = self.registered_users.get_by_phone_and_tenant(
                    customer.phone_number, tenant_id
                )
                if registered_user and registered_user.helpdesk_tenant_id:
                    helpdesk_tenant_id = str(registered_user.helpdesk_tenant_id)
        else:
            # For registered agents without customer record, look up by session phone
            registered_user = self.registered_users.get_by_phone_and_tenant(
                session.phone_number, tenant_id
            )
            if registered_user and registered_user.helpdesk_tenant_id:
                helpdesk_tenant_id = str(registered_user.helpdesk_tenant_id)

        # Look up ticket via the real helpdesk backend
        ticket_data = self.helpdesk_api.get_ticket_status(
            ticket_number=ticket_number,
            tenant_id=helpdesk_tenant_id,
        )

        if not ticket_data:
            # Fallback: try local database
            ticket = self.tickets.get_by_number(ticket_number, tenant_id)
            if not ticket:
                # Re-show the check ticket prompt with the error
                return _text_reply(
                    f'\u274C Ticket `{ticket_number}` was not found.\n\n'
                    'Please check the number and try again.\n\n'
                    '🔍 *Check Ticket Status*\n'
                    'Enter your ticket number (e.g., TKT-2026-00001).\n\n'
                    'Send *0* to return to the main menu.'
                )
            session.state = 'MAIN_MENU'
            return _build_ticket_status({
                'ticket_number': ticket.ticket_number,
                'status': ticket.status.upper(),
                'title': ticket.subject,
                'category': ticket.category,
                'created_at': str(ticket.created_at),
            })

        session.state = 'MAIN_MENU'
        return _build_ticket_status(ticket_data)
