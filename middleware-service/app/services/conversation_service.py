import uuid
import re
import logging

from sqlalchemy.orm import Session

from app.models.whatsapp_session import WhatsappSession
from app.repositories import (
    CommandLogRepository,
    CustomerRepository,
    EventLogRepository,
    InstanceTenantRepository,
    PhoneRegistryRepository,
    ProcessedMessageRepository,
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

# New states for ticket listing and commenting
TICKET_STATUSES = ['Open', 'In Progress', 'Pending', 'Resolved', 'Closed']

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
        '2️⃣ 📋 View Tickets\n'
        '3️⃣ 💬 Add Comment\n'
        '4️⃣ 🗣️ Speak to Agent\n\n'
        'Or just describe your issue and we\'ll help!'
    )


def _build_main_menu() -> dict:
    return _buttons_reply(
        text='Choose an option below to get started:',
        title='👋 Welcome to Support!',
        buttons=[
            {'type': 'reply', 'displayText': '✉️ Create Ticket', 'id': 'create_ticket'},
            {'type': 'reply', 'displayText': '📋 View Tickets', 'id': 'my_tickets_all'},
            {'type': 'reply', 'displayText': '💬 Add Comment', 'id': 'add_comment'},
            {'type': 'reply', 'displayText': '🗣️ Speak to Agent', 'id': 'speak_agent'},
        ],
        footer='Or type 0 to see the menu',
    )


def _build_category_list(names: list[str] | None = None) -> dict:
    if names:
        lines = '\n'.join(f'{i}\ufe0f\u20e3 {n}' if i < 10 else f'{i}. {n}' for i, n in enumerate(names, 1))
        return _text_reply(
            '📁 *Select a Category*\n\n'
            'Please reply with the number of your category:\n\n'
            f'{lines}\n\n'
            'Send *0* at any time to return to the main menu.'
        )
    return _text_reply(
        '📁 *Select a Category*\n\n'
        'Please reply with the number of your category:\n\n'
        '1️⃣ 🌐 *Network* — Internet, connectivity, VPN\n'
        '2️⃣ 💳 *Billing* — Invoices, payments, subscriptions\n'
        '3️⃣ 🔧 *Technical Support* — Software, hardware, errors\n'
        '4️⃣ ❓ *Other* — Anything not listed above\n\n'
        'Send *0* at any time to return to the main menu.'
    )


def _build_cc_prompt() -> dict:
    """Ask for optional CC email addresses before confirming the ticket."""
    return _text_reply(
        '📧 *Optional: CC Email Addresses*\n\n'
        'Would you like to add email addresses to receive a copy of this ticket?\n\n'
        'Enter email addresses separated by commas, e.g.:\n'
        '`manager@example.com, team@example.com`\n\n'
        'Or send *skip* (or *0*) to continue without CC emails.'
    )


def _build_cc_prompt() -> dict:
    """Ask for optional CC email addresses."""
    return _text_reply(
        '📧 *Optional: CC Email Addresses*\n\n'
        'Would you like to add email addresses to be copied on this ticket?\n\n'
        'Enter email addresses separated by commas, e.g.:\n'
        '`manager@example.com, supervisor@example.com`\n\n'
        'Or send *skip* (or *0*) to continue without CC.'
    )


def _build_confirm_buttons(draft: dict) -> dict:
    subject = draft.get('subject', 'N/A')[:100]
    description = draft.get('description', 'N/A')
    category = draft.get('category', 'N/A')
    cc_emails = draft.get('cc_emails', [])

    details = (
        f'\u2022 *Subject:* {subject}\n'
        f'\u2022 *Description:* {description}\n'
        f'\u2022 *Category:* {category}\n'
    )
    if cc_emails:
        details += f'\u2022 *CC:* {", ".join(cc_emails)}\n'

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
    cc_emails = draft.get('cc_emails', [])

    details = (
        f'\u2022 *Subject:* {subject}\n'
        f'\u2022 *Description:* {description}\n'
        f'\u2022 *Category:* {category}\n'
    )
    if cc_emails:
        details += f'\u2022 *CC:* {", ".join(cc_emails)}\n'

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


# ── My Tickets Helper Functions ───────────────────────────


def _build_my_tickets_list(tickets: list[dict], page: int, total: int, per_page: int, status_filter: str | None = None) -> dict:
    """Build a formatted list of tickets for WhatsApp display."""
    if not tickets:
        filter_text = f" (filtered by: {status_filter})" if status_filter else ""
        return _text_reply(
            f'📭 *No Tickets Found*{filter_text}\n\n'
            'You have no tickets matching the current filter.\n\n'
            'Send *0* to return to the main menu.'
        )

    lines = ['📋 *Your Tickets*']
    if status_filter:
        lines.append(f'Filter: *{status_filter}*')
    lines.append('')

    for i, t in enumerate(tickets, 1):
        ticket_num = t.get('ticket_number', 'N/A')
        title = t.get('title', 'No subject')[:50]
        status = t.get('status', 'UNKNOWN')
        priority = t.get('priority')
        category = t.get('category')

        line = f'{i}. `{ticket_num}` — *{title}* [{status}]'
        if priority:
            line += f' (Priority: {priority})'
        if category:
            line += f' ({category})'
        lines.append(line)

    # Pagination info
    total_pages = (total + per_page - 1) // per_page
    lines.append('')
    lines.append(f'Page {page} of {total_pages} (Total: {total})')
    lines.append('')
    lines.append('Options:')
    lines.append('• Reply with a *ticket number* to view details')
    lines.append('• Send *filter* to change status filter')
    lines.append('• Send *next* or *prev* for pagination')
    lines.append('• Send *0* for main menu')

    return _text_reply('\n'.join(lines))


def _build_my_tickets_menu() -> dict:
    """Build the View Tickets menu with filter options (text-based)."""
    return _text_reply(
        '📋 *View Tickets*\n\n'
        'Choose an option:\n\n'
        '1️⃣ All Tickets\n'
        '2️⃣ Filter by Status\n'
        '3️⃣ Refresh\n\n'
        'Send *0* to return to the main menu.'
    )


def _build_status_filter_menu() -> dict:
    """Build the status filter selection menu."""
    return _buttons_reply(
        text='🔍 *Filter Tickets by Status*\n\nSelect a status to filter by:',
        title='Status Filter',
        buttons=[
            {'type': 'reply', 'displayText': '📂 All', 'id': 'filter_all'},
            {'type': 'reply', 'displayText': '🟢 Open', 'id': 'filter_Open'},
            {'type': 'reply', 'displayText': '🟡 In Progress', 'id': 'filter_In Progress'},
            {'type': 'reply', 'displayText': '🟠 Pending', 'id': 'filter_Pending'},
            {'type': 'reply', 'displayText': '🔵 Resolved', 'id': 'filter_Resolved'},
            {'type': 'reply', 'displayText': '🔴 Closed', 'id': 'filter_Closed'},
        ],
        footer='Send 0 for main menu',
    )


# ── Add Comment Helper Functions ──────────────────────────


def _build_comment_prompt(ticket_number: str) -> dict:
    """Build the prompt for adding a comment."""
    return _text_reply(
        f'💬 *Add Comment to Ticket {ticket_number}*\n\n'
        'Please enter your comment:\n\n'
        'Send *0* to cancel and return to the main menu.'
    )


def _build_comment_ticket_confirm(ticket_data: dict) -> dict:
    """Build the ticket confirmation prompt showing title before asking for comment."""
    ticket_number = ticket_data.get('ticket_number', 'N/A')
    title = ticket_data.get('title', 'No subject')[:100]
    status = ticket_data.get('status', 'UNKNOWN')
    category = ticket_data.get('category') or 'N/A'
    created_raw = ticket_data.get('created_at', '')
    created_str = created_raw[:10] if created_raw and len(created_raw) >= 10 else ''

    details = (
        f'🔍 *Ticket Found*\n\n'
        f'▸ *Number:* `{ticket_number}`\n'
        f'▸ *Title:* {title}\n'
        f'▸ *Status:* `{status}`\n'
    )
    if category:
        details += f'▸ *Category:* {category}\n'
    if created_str:
        details += f'▸ *Created:* {created_str}\n'

    details += '\nIs this the correct ticket you want to comment on?'

    return _buttons_reply(
        text=details,
        title='Confirm Ticket',
        buttons=[
            {'type': 'reply', 'displayText': '✅ Yes, continue', 'id': 'ticket_confirm_yes'},
            {'type': 'reply', 'displayText': '❌ No, try again', 'id': 'ticket_confirm_no'},
        ],
        footer='Send 0 to cancel',
    )


def _build_comment_confirm(ticket_number: str, comment: str) -> dict:
    """Build the confirmation prompt for adding a comment."""
    return _buttons_reply(
        text=f'💬 *Confirm Comment*\n\n'
             f'Ticket: `{ticket_number}`\n'
             f'Comment: {comment[:200]}{"..." if len(comment) > 200 else ""}\n\n'
             'Submit this comment?',
        title='Confirm Comment',
        buttons=[
            {'type': 'reply', 'displayText': '✅ Submit', 'id': 'comment_submit'},
            {'type': 'reply', 'displayText': '✏️ Edit', 'id': 'comment_edit'},
            {'type': 'reply', 'displayText': '❌ Cancel', 'id': 'comment_cancel'},
        ],
        footer='Send 0 to cancel',
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
        self.processed_messages = ProcessedMessageRepository(db)
        self.helpdesk_api = HelpdeskAPIClient()
        # Per-message cache (a ConversationService lives for one inbound message only).
        self._registration_cache: dict[tuple[str, uuid.UUID], dict | None] = {}

    def _tenant_slug(self, local_tenant_id: uuid.UUID) -> str | None:
        """
        Helpdesk tenant slug for a middleware tenant, or None if unmapped.
        The slug is the only tenant handle sent to the backend; the backend
        resolves the schema itself. Nothing is guessed or defaulted.
        """
        tenant = self.tenants.get(local_tenant_id)
        slug = (tenant.helpdesk_tenant_slug or '').strip().lower() if tenant else ''
        return slug or None

    def _is_session_stale(self, session: WhatsappSession) -> bool:
        from datetime import datetime, timedelta
        cutoff = datetime.utcnow() - timedelta(minutes=SESSION_TIMEOUT_MINUTES)
        return session.last_activity < cutoff and session.state != 'MAIN_MENU'

    def _check_user_registration(self, phone_number: str, tenant_id: uuid.UUID) -> dict | None:
        """
        Ask the helpdesk (authoritative) whether this phone belongs to an active
        user of the tenant. Returns the user info dict (with ``user_id``) when
        registered, else None. The result is cached for the current message only.
        """
        key = (phone_number, tenant_id)
        if key in self._registration_cache:
            return self._registration_cache[key]

        result = None
        slug = self._tenant_slug(tenant_id)
        if slug:
            try:
                data = self.helpdesk_api.check_user_registration(phone_number, slug)
                if data and data.get('is_registered') and data.get('user_id'):
                    result = data
            except Exception as e:
                logger.warning('Failed to check user registration: %s (phone=%s)', e, phone_number)
        self._registration_cache[key] = result
        return result

    def _tenant_categories(self, session: WhatsappSession) -> list[str] | None:
        slug = self._tenant_slug(session.tenant_id)
        if not slug:
            return None
        try:
            return self.helpdesk_api.get_categories(slug)
        except Exception as e:
            logger.warning('Failed to load tenant categories: %s', e)
            return None

    def _registered_user_id(self, phone_number: str, tenant_id: uuid.UUID) -> str | None:
        registration = self._check_user_registration(phone_number, tenant_id)
        return str(registration['user_id']) if registration else None

    def process_message(
        self, instance_name: str, phone_number: str, text: str, message_id: str | None = None, push_name: str | None = None
    ) -> dict | None:
        """Returns the reply, or None when the message is a duplicate delivery (webhook retry)."""
        link = self.instance_tenants.get_by_instance(instance_name)
        if not link:
            logger.warning('No tenant linked to instance %s', instance_name)
            return _text_reply('This support line is not configured. Please contact the administrator.')

        tenant_id = link.tenant_id
        if not self._tenant_slug(tenant_id):
            logger.error('Instance %s is linked to tenant %s which has no helpdesk_tenant_slug', instance_name, tenant_id)
            return _text_reply('This support line is not configured. Please contact the administrator.')

        # Idempotency: a retried delivery of the same WhatsApp message must not run
        # the state machine (and e.g. submit a ticket) twice.
        if message_id and not self.processed_messages.claim(instance_name, message_id):
            logger.info('Duplicate message %s on instance %s ignored', message_id, instance_name)
            return None

        # Is this phone a helpdesk user of THIS tenant? (checked against the backend)
        registration = self._check_user_registration(phone_number, tenant_id)

        customer = None
        customer_id = None

        if registration:
            # Registered helpdesk user — no local Customer record is created.
            self.phone_registry.get_or_create(phone_number, tenant_id, customer_id=None)
        else:
            # Not registered — local conversation identity only, scoped to this tenant.
            customer = self.customers.get_or_create(phone_number, tenant_id)
            customer_id = customer.id
            self.phone_registry.get_or_create(phone_number, tenant_id, customer_id)

            if customer and not customer.name:
                name = push_name or ''
                if name:
                    self.customers.update(customer, name=name)

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

        if state == 'WAITING_CC_EMAILS':
            return self._handle_cc_emails(session, text, customer_id, tenant_id)

        if state == 'CONFIRM_TICKET':
            return self._handle_confirm(session, text, customer_id, tenant_id)

        if state in ('CHECKING_TICKET',):
            return self._handle_check_ticket(session, text, tenant_id, customer_id)

        # New states for My Tickets
        if state == 'MY_TICKETS_MENU':
            return self._handle_my_tickets_menu(session, text, tenant_id, customer_id)

        if state == 'MY_TICKETS_LIST':
            return self._handle_my_tickets_list(session, text, tenant_id, customer_id)

        if state == 'MY_TICKETS_FILTER':
            return self._handle_my_tickets_filter(session, text, tenant_id, customer_id)

        # New states for Add Comment
        if state == 'ADD_COMMENT_TICKET':
            return self._handle_add_comment_ticket(session, text, tenant_id, customer_id)

        if state == 'CONFIRM_COMMENT_TICKET':
            return self._handle_confirm_comment_ticket(session, text, tenant_id, customer_id)

        if state == 'ADD_COMMENT_MESSAGE':
            return self._handle_add_comment_message(session, text, tenant_id, customer_id)

        if state == 'WAITING_COMMENT':
            return self._handle_waiting_comment(session, text, tenant_id, customer_id)

        if state == 'CONFIRM_COMMENT':
            return self._handle_confirm_comment(session, text, tenant_id, customer_id)

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
            # Only users registered in this tenant's helpdesk (phone on their profile) may create tickets
            if self._check_user_registration(session.phone_number, tenant_id):
                session.ticket_draft = {}
                session.state = 'WAITING_SUBJECT'
                return _build_subject_prompt()
            return _text_reply(
                '🔒 *Registration Required*\n\n'
                'Your phone number is not registered to a user account in the helpdesk system.\n\n'
                'Please contact your administrator to register your phone number before using the WhatsApp chatbot.\n\n'
                'Send *0* to return to the main menu.'
            )

        if choice in ('2', 'my tickets', 'my_tickets', 'my_tickets_all', 'view tickets', 'view_tickets', 'list tickets', 'check ticket'):
            # Show the My Tickets menu with sub-options (All, Filter by Status, Refresh)
            session.state = 'MY_TICKETS_MENU'
            return _build_my_tickets_menu()

        if choice in ('3', 'add_comment', 'comment', 'add comment'):
            # Start comment flow
            return self._handle_add_comment_start(session, tenant_id, customer_id)

        if choice in ('4', 'speak_agent', 'speak to agent', 'agent', '5'):
            session.state = 'MAIN_MENU'
            return _build_escalate_reply()

        if choice in ('check_ticket', 'check'):
            # Direct ticket number lookup
            session.state = 'CHECKING_TICKET'
            return _text_reply(
                '🔍 *Check Ticket Status*\n\n'
                'Please enter your ticket number (e.g., `TKT-2026-00001`).\n\n'
                'Send *0* to return to the main menu.'
            )

        if choice in ('filter', 'filter tickets'):
            # Show filtered tickets with status selection
            return self._handle_my_tickets_filtered(session, tenant_id, customer_id)

        # For unrecognized text, show the text menu instead of buttons
        # (buttons may not display properly if previous ones are still visible)
        return _cancel_reply()

    def _handle_my_tickets_filtered(
        self, session: WhatsappSession, tenant_id: uuid.UUID, customer_id: uuid.UUID | None
    ) -> dict:
        """Show status filter options for my tickets."""
        session.state = 'MY_TICKETS_FILTER'
        return _text_reply(
            '🔍 *Filter My Tickets by Status*\n\n'
            'Reply with the number of the status you want to filter by:\n\n'
            '1️⃣ All\n'
            '2️⃣ Open\n'
            '3️⃣ In Progress\n'
            '4️⃣ Resolved\n'
            '5️⃣ Closed\n'
            '6️⃣ On Hold\n\n'
            'Send *0* to return to the main menu.'
        )

    def _handle_my_tickets_filter(
        self, session: WhatsappSession, text: str, tenant_id: uuid.UUID, customer_id: uuid.UUID | None
    ) -> dict:
        """Handle status filter selection for my tickets.
        
        Saves the chosen filter in the session and redirects to the
        paginated ticket list handler.
        """
        if _is_cancel(text):
            session.state = 'MAIN_MENU'
            return _cancel_reply()

        normalized = text.strip().lower()

        status_map = {
            '1': None,  # All
            'all': None,
            '2': 'Open',
            'open': 'Open',
            '3': 'In Progress',
            'in progress': 'In Progress',
            'in_progress': 'In Progress',
            '4': 'Resolved',
            'resolved': 'Resolved',
            '5': 'Closed',
            'closed': 'Closed',
            '6': 'On Hold',
            'on hold': 'On Hold',
            'on_hold': 'On Hold',
        }

        status_filter = status_map.get(normalized)
        if status_filter is None and normalized not in ('1', 'all'):
            return _text_reply(
                '❌ Invalid option. Please reply with a number (1-6) or the status name.\n\n'
                'Send *0* to return to the main menu.'
            )

        # Store filter in session and redirect to paginated list
        draft = dict(session.ticket_draft or {})
        draft['_ticket_status_filter'] = status_filter
        draft['_ticket_page'] = 1
        session.ticket_draft = draft

        session.state = 'MY_TICKETS_LIST'
        return self._handle_my_tickets_list(session, 'all', tenant_id, customer_id)

    # ── Add Comment Flow ──────────────────────────────────

    def _handle_add_comment_start(
        self, session: WhatsappSession, tenant_id: uuid.UUID, customer_id: uuid.UUID | None
    ) -> dict:
        """Start the add comment flow by asking for ticket number."""
        session.state = 'ADD_COMMENT_TICKET'
        return _text_reply(
            '💬 *Add Comment to Ticket*\n\n'
            'Please enter the ticket number you want to comment on (e.g., `TKT-2026-00001`).\n\n'
            'Send *0* to return to the main menu.'
        )

    def _handle_add_comment_ticket(
        self, session: WhatsappSession, text: str, tenant_id: uuid.UUID, customer_id: uuid.UUID | None
    ) -> dict:
        """Handle ticket number input for adding comment.
        
        Looks up the ticket title and asks the user to confirm
        they are commenting on the correct ticket.
        """
        if _is_cancel(text):
            session.state = 'MAIN_MENU'
            return _cancel_reply()

        ticket_number = text.strip().upper()
        session.ticket_draft = {'ticket_number': ticket_number}

        # Look up ticket (visible to this user only) to confirm with the user
        ticket_data = self._lookup_ticket(session, ticket_number, tenant_id, customer_id)

        if ticket_data:
            session.state = 'CONFIRM_COMMENT_TICKET'
            return _build_comment_ticket_confirm(ticket_data)

        return _text_reply(
            f'❌ Ticket `{ticket_number}` was not found.\n\n'
            'Please check the ticket number and try again, or send *0* to return to the main menu.'
        )

    def _handle_confirm_comment_ticket(
        self, session: WhatsappSession, text: str, tenant_id: uuid.UUID, customer_id: uuid.UUID | None
    ) -> dict:
        """Handle ticket confirmation — whether to proceed with adding a comment."""
        if _is_cancel(text):
            session.ticket_draft = None
            session.state = 'MAIN_MENU'
            return _cancel_reply()

        normalized = text.strip().lower()

        if normalized in ('ticket_confirm_yes', '1', 'yes', 'confirm', 'continue', 'y'):
            # Proceed to comment input
            ticket_number = session.ticket_draft.get('ticket_number', '')
            session.state = 'ADD_COMMENT_MESSAGE'
            return _text_reply(
                f'💬 *Add Comment to {ticket_number}*\n\n'
                'Please enter your comment:\n\n'
                'Send *0* to cancel and return to the main menu.'
            )

        if normalized in ('ticket_confirm_no', '2', 'no', 'try again', 'n'):
            # Go back to ticket number input
            session.state = 'ADD_COMMENT_TICKET'
            return _text_reply(
                '💬 *Add Comment to Ticket*\n\n'
                'Please enter the correct ticket number (e.g., `TKT-2026-00001`).\n\n'
                'Send *0* to return to the main menu.'
            )

        # Invalid input — re-show the confirmation with current data
        ticket_number = session.ticket_draft.get('ticket_number', '')
        ticket_data = self._lookup_ticket(session, ticket_number, tenant_id, customer_id)
        if ticket_data:
            return _build_comment_ticket_confirm(ticket_data)
        return _text_reply(
            f'❌ Ticket `{ticket_number}` was not found.\n\n'
            'Please send *1* to try again or *0* to return to the main menu.'
        )

    def _handle_add_comment_message(
        self, session: WhatsappSession, text: str, tenant_id: uuid.UUID, customer_id: uuid.UUID | None
    ) -> dict:
        """Handle comment message input and submit to helpdesk."""
        if _is_cancel(text):
            session.ticket_draft = None
            session.state = 'MAIN_MENU'
            return _cancel_reply()

        ticket_number = session.ticket_draft.get('ticket_number')
        message = text.strip()

        if not message:
            return _text_reply(
                '❌ Comment cannot be empty. Please enter your comment.\n\n'
                'Send *0* to cancel.'
            )

        # The comment is always posted as the helpdesk user who owns this phone number
        user_id = self._registered_user_id(session.phone_number, tenant_id)

        if not user_id:
            session.state = 'MAIN_MENU'
            return _text_reply(
                '❌ *Unable to Add Comment*\n\n'
                'Could not identify your user account. Please contact your administrator.\n\n'
                'Send *0* to return to the main menu.'
            )

        # Call helpdesk API to add comment (tenant resolved from the instance link)
        result = self.helpdesk_api.add_comment_as_user(
            ticket_number=ticket_number,
            user_id=user_id,
            tenant_slug=self._tenant_slug(tenant_id),
            message=message,
        )

        session.ticket_draft = None
        session.state = 'MAIN_MENU'

        if result and result.get("success"):
            return _text_reply(
                f'✅ *Comment Added Successfully*\n\n'
                f'Ticket: `{ticket_number}`\n'
                f'Your comment has been added and notifications sent to relevant parties.\n\n'
                f'Send *0* to return to the main menu.'
            )
        else:
            return _text_reply(
                f'❌ *Failed to Add Comment*\n\n'
                f'Ticket: `{ticket_number}`\n'
                f'Error: {result.get("message", "Unknown error") if result else "Ticket not found or API error"}\n\n'
                f'Send *0* to return to the main menu.'
            )

    def _handle_my_tickets_menu(
        self, session: WhatsappSession, text: str, tenant_id: uuid.UUID, customer_id: uuid.UUID | None
    ) -> dict:
        """Handle the My Tickets menu option."""
        if _is_cancel(text):
            session.state = 'MAIN_MENU'
            return _cancel_reply()

        normalized = text.strip().lower()

        # Handle both button IDs and text/number commands
        if normalized in ('1', 'all', 'my_tickets_all', 'my tickets', 'view tickets', 'view_tickets'):
            # Show all tickets
            session.state = 'MY_TICKETS_LIST'
            return self._handle_my_tickets_list(session, 'all', tenant_id, customer_id)

        if normalized in ('2', 'filter', 'filter tickets', 'my_tickets_filter'):
            # Show filter options
            session.state = 'MY_TICKETS_FILTER'
            return _text_reply(
                '🔍 *Filter My Tickets by Status*\n\n'
                'Reply with the number of the status you want to filter by:\n\n'
                '1️⃣ All\n'
                '2️⃣ Open\n'
                '3️⃣ In Progress\n'
                '4️⃣ Resolved\n'
                '5️⃣ Closed\n'
                '6️⃣ On Hold\n\n'
                'Send *0* to return to the main menu.'
            )

        if normalized in ('3', 'refresh', 'my_tickets_refresh'):
            # Refresh the list (clear filter, go to first page)
            session.state = 'MY_TICKETS_LIST'
            draft = dict(session.ticket_draft or {})
            draft.pop('_ticket_page', None)
            draft.pop('_ticket_status_filter', None)
            session.ticket_draft = draft
            return self._handle_my_tickets_list(session, 'refresh', tenant_id, customer_id)

        if normalized in ('next', 'n', 'prev', 'p', 'back', '<', '>') or normalized.startswith('page '):
            # Pagination commands from list view — route directly to list handler
            session.state = 'MY_TICKETS_LIST'
            return self._handle_my_tickets_list(session, text, tenant_id, customer_id)

        # Show the menu
        return _build_my_tickets_menu()

    def _handle_my_tickets_list(
        self, session: WhatsappSession, text: str, tenant_id: uuid.UUID, customer_id: uuid.UUID | None
    ) -> dict:
        """Handle displaying the list of tickets with pagination.
        
        Supports pagination via "next", "prev", "page N" commands.
        Users can also type a ticket number to view details, or
        "filter" to change the status filter.
        
        The session stays in MY_TICKETS_LIST state so that subsequent
        pagination commands continue to be routed here.
        """
        if _is_cancel(text):
            session.state = 'MAIN_MENU'
            return _cancel_reply()

        normalized = text.strip().lower()

        # ── Filter command from list view ──
        if normalized in ('filter', 'filter tickets', 'my_tickets_filter', '2'):
            session.state = 'MY_TICKETS_FILTER'
            return _text_reply(
                '🔍 *Filter Tickets by Status*\n\n'
                'Reply with the number of the status you want to filter by:\n\n'
                '1️⃣ All\n'
                '2️⃣ Open\n'
                '3️⃣ In Progress\n'
                '4️⃣ Resolved\n'
                '5️⃣ Closed\n'
                '6️⃣ On Hold\n\n'
                'Send *0* to return to the main menu.'
            )

        # ── Ticket number lookup ──
        # If the text looks like a ticket number (e.g. TKT-2026-00001, INC-...),
        # route directly to ticket detail lookup.
        if self._looks_like_ticket_number(normalized):
            return self._handle_ticket_lookup(session, text, tenant_id, customer_id)

        # ── Pagination ──
        per_page = 10
        draft = dict(session.ticket_draft or {})
        current_page = draft.get('_ticket_page', 1)
        status_filter = draft.get('_ticket_status_filter')  # stored by _handle_my_tickets_filter

        if normalized in ('refresh', 'my_tickets_refresh'):
            # Clear filter and reset to page 1
            draft.pop('_ticket_page', None)
            draft.pop('_ticket_status_filter', None)
            session.ticket_draft = draft
            current_page = 1
            status_filter = None
        elif normalized in ('next', 'n', '>'):
            current_page = current_page + 1
        elif normalized in ('prev', 'p', '<', 'back'):
            current_page = max(1, current_page - 1)
        elif normalized.startswith('page '):
            try:
                current_page = max(1, int(normalized.replace('page ', '').strip()))
            except (ValueError, IndexError):
                current_page = 1
        # else: same page (unknown command — just re-display)

        skip = (current_page - 1) * per_page

        # Tickets come from the helpdesk, scoped to the user that owns this phone
        user_id = self._registered_user_id(session.phone_number, tenant_id)

        if user_id:
            result = self.helpdesk_api.get_my_tickets(
                user_id=user_id,
                tenant_slug=self._tenant_slug(tenant_id),
                status_name=status_filter,
                skip=skip,
                limit=per_page,
            )
            tickets = result.get("tickets", [])
            total = result.get("total", 0)
        elif customer_id is not None:
            # Unregistered customer — local conversation records for this tenant/customer only.
            # Query per_page+1 to detect whether a next page exists.
            tickets_raw = self.tickets.list_all(
                tenant_id=tenant_id, customer_id=customer_id, limit=per_page + 1, offset=skip
            )
            has_more = len(tickets_raw) > per_page
            tickets = []
            for t in tickets_raw[:per_page]:
                t_status = t.status.upper() if t.status else "UNKNOWN"
                if status_filter and t_status.lower() != status_filter.lower():
                    continue
                tickets.append({
                    "ticket_number": t.ticket_number,
                    "title": t.subject or t.title,
                    "status": t_status,
                    "priority": None,
                    "category": t.category,
                    "created_at": str(t.created_at) if t.created_at else None,
                    "updated_at": str(t.updated_at) if t.updated_at else None,
                })
            total = (current_page - 1) * per_page + len(tickets) + (per_page if has_more else 0)
        else:
            tickets = []
            total = 0

        if not tickets:
            session.state = 'MAIN_MENU'
            return _text_reply(
                '📭 *No Tickets Found*\n\n'
                'You have no tickets yet. Send *1* to create a new ticket.\n\n'
                '*0* to return to the main menu.'
            )

        # Save current page in session for pagination
        draft = dict(session.ticket_draft or {})
        draft['_ticket_page'] = current_page
        session.ticket_draft = draft

        # Keep state as MY_TICKETS_LIST so pagination routing continues to work
        session.state = 'MY_TICKETS_LIST'

        return _build_my_tickets_list(tickets, current_page, total, per_page, status_filter)

    def _looks_like_ticket_number(self, text: str) -> bool:
        """Heuristic: does the text look like a ticket number?
        
        Matches patterns like:
          - TKT-2026-00001
          - INC-2026-00001
          - Any alphanumeric with hyphens (e.g. ABC-123)
        """
        # Known ticket prefixes (case-insensitive already)
        if re.match(r'^(tkt|inc|sla|prb|chg|rfc)-\d{4}-\d+', text):
            return True
        # Generic: starts with 2-4 uppercase letters, hyphen, digits
        if re.match(r'^[a-z]{2,4}-\d+', text):
            return True
        return False

    def _handle_ticket_lookup(
        self, session: WhatsappSession, text: str, tenant_id: uuid.UUID, customer_id: uuid.UUID | None
    ) -> dict:
        """Look up a single ticket by number and show its details."""
        ticket_number = text.strip().upper()
        ticket_data = self._lookup_ticket(session, ticket_number, tenant_id, customer_id)

        if not ticket_data:
            return _text_reply(
                f'\u274C Ticket `{ticket_number}` was not found.\n\n'
                'Please check the number and try again, or send *next*/*prev* to browse tickets.\n\n'
                'Send *0* to return to the main menu.'
            )

        session.state = 'MAIN_MENU'
        return _build_ticket_status(ticket_data)

    def _lookup_ticket(
        self, session: WhatsappSession, ticket_number: str, tenant_id: uuid.UUID, customer_id: uuid.UUID | None
    ) -> dict | None:
        """
        Find a ticket by number, restricted to what the sender may see.
        Registered users: the helpdesk applies that user's visibility rules in the
        tenant schema. Unregistered customers: only local records of that customer.
        """
        user_id = self._registered_user_id(session.phone_number, tenant_id)
        if user_id:
            return self.helpdesk_api.get_ticket_status(
                ticket_number=ticket_number,
                tenant_slug=self._tenant_slug(tenant_id),
                user_id=user_id,
            )
        if customer_id is not None:
            ticket = self.tickets.get_by_number(ticket_number, tenant_id)
            if ticket and ticket.customer_id == customer_id:
                return {
                    'ticket_number': ticket.ticket_number,
                    'title': ticket.subject or 'No subject',
                    'status': ticket.status.upper() if ticket.status else 'UNKNOWN',
                    'category': ticket.category,
                    'created_at': str(ticket.created_at) if ticket.created_at else None,
                }
        return None

    def _handle_waiting_comment(
        self, session: WhatsappSession, text: str, tenant_id: uuid.UUID, customer_id: uuid.UUID | None
    ) -> dict:
        """Handle the comment input state."""
        if _is_cancel(text):
            session.state = 'MAIN_MENU'
            return _cancel_reply()

        ticket_number = session.ticket_draft.get('ticket_number')
        message = text.strip()

        if not message:
            return _text_reply(
                '❌ Comment cannot be empty. Please enter your comment.\n\n'
                'Send *0* to cancel.'
            )

        # Show confirmation
        session.state = 'CONFIRM_COMMENT'
        return _build_comment_confirm(ticket_number, message)

    def _handle_confirm_comment(
        self, session: WhatsappSession, text: str, tenant_id: uuid.UUID, customer_id: uuid.UUID | None
    ) -> dict:
        """Handle comment confirmation."""
        if _is_cancel(text):
            session.state = 'MAIN_MENU'
            return _cancel_reply()

        normalized = text.strip().lower()

        if normalized in ('confirm_submit', '1', 'submit', 'yes', 'confirm'):
            # Submit the comment
            ticket_number = session.ticket_draft.get('ticket_number')
            message = session.ticket_draft.get('message')

            user_id = self._registered_user_id(session.phone_number, tenant_id)

            if not user_id:
                session.state = 'MAIN_MENU'
                return _text_reply(
                    '❌ *Unable to Add Comment*\n\n'
                    'Could not identify your user account. Please contact your administrator.\n\n'
                    'Send *0* to return to the main menu.'
                )

            # Call helpdesk API to add comment (tenant resolved from the instance link)
            result = self.helpdesk_api.add_comment_as_user(
                ticket_number=ticket_number,
                user_id=user_id,
                tenant_slug=self._tenant_slug(tenant_id),
                message=message,
            )

            session.ticket_draft = None
            session.state = 'MAIN_MENU'

            if result and result.get("success"):
                return _text_reply(
                    f'✅ *Comment Added Successfully*\n\n'
                    f'Ticket: `{ticket_number}`\n'
                    f'Your comment has been added and notifications sent to relevant parties.\n\n'
                    f'Send *0* to return to the main menu.'
                )
            else:
                return _text_reply(
                    f'❌ *Failed to Add Comment*\n\n'
                    f'Ticket: `{ticket_number}`\n'
                    f'Error: {result.get("message", "Unknown error") if result else "Ticket not found or API error"}\n\n'
                    f'Send *0* to return to the main menu.'
                )

        if normalized in ('confirm_edit', '2', 'edit'):
            # Go back to comment input
            session.state = 'ADD_COMMENT_MESSAGE'
            return _text_reply(
                f'💬 *Add Comment to {session.ticket_draft.get("ticket_number")}*\n\n'
                'Please enter your comment:\n\n'
                'Send *0* to cancel and return to the main menu.'
            )

        if normalized in ('confirm_cancel', '3', 'cancel', '0', 'menu'):
            session.ticket_draft = None
            session.state = 'MAIN_MENU'
            return _cancel_reply()

        # Invalid input - show confirmation again
        ticket_number = session.ticket_draft.get('ticket_number')
        message = session.ticket_draft.get('message')
        return _build_comment_confirm(ticket_number, message)

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
        # Offer this tenant's own categories (fixed list only if the helpdesk can't be asked).
        names = self._tenant_categories(session)
        draft['_categories'] = names or []
        session.ticket_draft = draft
        session.state = 'WAITING_CATEGORY'
        return _build_category_list(names)

    def _handle_category(self, session: WhatsappSession, text: str) -> dict:
        if _is_cancel(text):
            session.ticket_draft = None
            session.state = 'MAIN_MENU'
            return _cancel_reply()

        normalized = text.strip().lower()
        draft = dict(session.ticket_draft or {})
        names = draft.get('_categories') or []

        if names:
            by_name = {n.lower(): n for n in names}
            if normalized.isdigit() and 1 <= int(normalized) <= len(names):
                category = names[int(normalized) - 1]
            else:
                category = by_name.get(normalized)
        else:
            category_map = {
                '1': 'Network', 'cat_network': 'Network', 'network': 'Network',
                '2': 'Billing', 'cat_billing': 'Billing', 'billing': 'Billing',
                '3': 'Technical Support', 'cat_tech': 'Technical Support',
                'technical support': 'Technical Support', 'tech': 'Technical Support',
                '4': 'Other', 'cat_other': 'Other', 'other': 'Other',
            }
            category = category_map.get(normalized)
        if not category:
            return _build_category_list(names or None)

        draft['category'] = category
        session.ticket_draft = draft
        session.state = 'WAITING_CC_EMAILS'
        return _build_cc_prompt()

    def _handle_cc_emails(
        self, session: WhatsappSession, text: str, customer_id: uuid.UUID | None, tenant_id: uuid.UUID
    ) -> dict:
        """Handle optional CC email input after category selection."""
        if _is_cancel(text):
            session.ticket_draft = None
            session.state = 'MAIN_MENU'
            return _cancel_reply()

        normalized = text.strip().lower()

        # Skip CC emails and go to confirmation
        if normalized in ('skip', 'none', 'no', '0'):
            draft = dict(session.ticket_draft or {})
            session.ticket_draft = draft
            session.state = 'CONFIRM_TICKET'
            return _build_confirm_text(draft)

        # Parse comma-separated email addresses
        raw_emails = [e.strip() for e in text.split(',') if e.strip()]
        valid_emails = []
        invalid_emails = []

        import re
        email_pattern = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

        for email in raw_emails:
            clean = email.strip().lower()
            if email_pattern.match(clean):
                valid_emails.append(clean)
            else:
                invalid_emails.append(email)

        if not valid_emails:
            return _text_reply(
                '❌ No valid email addresses found.\n\n'
                'Please enter valid email addresses separated by commas, e.g.:\n'
                '`manager@example.com, team@example.com`\n\n'
                'Or send *skip* to continue without CC emails.'
            )

        if invalid_emails:
            return _text_reply(
                f'⚠️ The following addresses are not valid: {", ".join(invalid_emails)}\n\n'
                f'Valid addresses accepted: {", ".join(valid_emails)}\n\n'
                'Please correct the invalid addresses or send *skip* to continue.'
            )

        # Store valid CC emails in draft and show confirmation
        draft = dict(session.ticket_draft or {})
        draft['cc_emails'] = valid_emails
        session.ticket_draft = draft
        session.state = 'CONFIRM_TICKET'
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

            # The ticket is created in the sender's tenant, as the helpdesk user who owns this phone.
            user_id = self._registered_user_id(session.phone_number, tenant_id)
            if not user_id:
                session.ticket_draft = None
                session.state = 'MAIN_MENU'
                return _text_reply(
                    '🔒 *Registration Required*\n\n'
                    'Your phone number is not registered to a user account in the helpdesk system.\n\n'
                    'Send *0* to return to the main menu.'
                )

            ticket_data = self.helpdesk_api.create_ticket(
                tenant_slug=self._tenant_slug(tenant_id),
                title=subject,
                user_id=user_id,
                description=description,
                category=category,
                cc_emails=draft.get('cc_emails'),
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

            # The helpdesk answered but refused the ticket (e.g. subscription required):
            # tell the user why instead of calling the system unavailable.
            if self.helpdesk_api.last_error:
                session.ticket_draft = None
                session.state = 'MAIN_MENU'
                return _text_reply(
                    '❌ *Ticket Could Not Be Created*\n\n'
                    f'{self.helpdesk_api.last_error}\n\n'
                    'Please contact your administrator if you think this is a mistake.\n\n'
                    'Send *0* to return to the main menu.'
                )

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

        normalized = text.strip().lower()

        # Redirect pagination commands back to the ticket list handler
        if normalized in ('next', 'n', 'prev', 'p', 'back', '<', '>', 'filter') or normalized.startswith('page '):
            session.state = 'MY_TICKETS_LIST'
            return self._handle_my_tickets_list(session, text, tenant_id, customer_id)

        ticket_number = text.strip().upper()

        ticket_data = self._lookup_ticket(session, ticket_number, tenant_id, customer_id)

        if not ticket_data:
            # Re-show the check ticket prompt with the error
            return _text_reply(
                f'\u274C Ticket `{ticket_number}` was not found.\n\n'
                'Please check the number and try again.\n\n'
                '🔍 *Check Ticket Status*\n'
                'Enter your ticket number (e.g., TKT-2026-00001).\n\n'
                'Send *0* to return to the main menu.'
            )

        session.state = 'MAIN_MENU'
        return _build_ticket_status(ticket_data)
