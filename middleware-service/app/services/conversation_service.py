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
    """Text-based confirm prompt for ticket creation (used when user sends invalid input)."""
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
        f'1\uFE0F\u20E3 Submit\n'
        f'2\uFE0F\u20E3 Edit Subject\n'
        f'3\uFE0F\u20E3 Cancel\n'
        f'0\uFE0F\u20E3 Main Menu'
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
    """Build the text-based status filter selection menu."""
    return _text_reply(
        '🔍 *Filter Tickets by Status*\n\n'
        'Select a status to filter by:\n\n'
        '1️⃣ All\n'
        '2️⃣ Open\n'
        '3️⃣ In Progress\n'
        '4️⃣ Resolved\n'
        '5️⃣ Closed\n'
        '6️⃣ On Hold\n\n'
        'Send *0* to return to the main menu.'
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
    """Build the ticket confirmation prompt showing title before asking for comment (text-based)."""
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

    details += (
        '\nIs this the correct ticket you want to comment on?\n\n'
        '1️⃣ Yes, continue\n'
        '2️⃣ No, try again\n\n'
        'Send *0* to cancel.'
    )

    return _text_reply(details)


def _build_comment_confirm(ticket_number: str, comment: str) -> dict:
    """Build the text-based confirmation prompt for adding a comment."""
    return _text_reply(
        f'💬 *Confirm Comment*\n\n'
        f'Ticket: `{ticket_number}`\n'
        f'Comment: {comment[:200]}{"..." if len(comment) > 200 else ""}\n\n'
        f'Reply with:\n'
        f'1\uFE0F\u20E3 Submit\n'
        f'2\uFE0F\u20E3 Edit\n'
        f'3\uFE0F\u20E3 Cancel\n\n'
        f'Send *0* to return to the main menu.'
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
        self, phone_number: str, tenant_id: uuid.UUID
    ) -> bool:
        """
        Look up the customer in the main helpdesk system by phone number.
        Tries multiple phone number formats (with/without country code).
        Updates the local customer record with the helpdesk_customer_id if found.

        Does NOT create customers — only reads existing ones. Customers must
        be created through the main helpdesk portal.

        Returns:
            True if a matching helpdesk customer was found (or already linked),
            False if the phone number is not registered as a customer.
        """
        try:
            # Skip API call if local customer is already linked to helpdesk
            existing_local = self.customers.get_by_phone_and_tenant(
                phone_number, tenant_id
            )
            if existing_local and existing_local.helpdesk_customer_id:
                return True

            helpdesk_tenant_id = self._resolve_helpdesk_tenant_id(tenant_id)

            # Try multiple phone number formats to handle both local
            # (0880218905) and international (265880218905) formats.
            phone_variations = self._normalize_phone_variations(phone_number)

            helpdesk_customer = None
            for variant in phone_variations:
                helpdesk_customer = self.helpdesk_api.lookup_customer_by_phone(
                    phone_number=variant,
                    tenant_id=helpdesk_tenant_id,
                )
                if helpdesk_customer:
                    logger.info(
                        "Found helpdesk customer %s via phone variant %s (original: %s)",
                        helpdesk_customer.get("id"), variant, phone_number
                    )
                    break

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
                return True

            logger.info(
                "No helpdesk customer found for phone %s (tried %d variants)",
                phone_number, len(phone_variations)
            )
            return False

        except Exception as e:
            logger.warning(
                "Failed to sync customer with helpdesk: %s (phone=%s, allowing through)",
                e,
                phone_number,
            )
            # If we can't verify (API error etc.), block to be safe
            return False

    @staticmethod
    def _normalize_phone_variations(phone_number: str) -> list[str]:
        """Generate phone number variations for lookup.

        Handles numbers with and without country code prefix.
        Malawi country code is 265. Common formats:
          - 0880218905  (local format, leading 0)
          - 265880218905 (with country code, no leading 0)
          - 880218905    (no prefix at all)
        """
        # Strip any non-digit characters
        digits = re.sub(r'\D', '', phone_number)

        variations = [phone_number, digits]

        # If starts with 0, try with country code 265 (e.g. 0880218905 → 265880218905)
        if digits.startswith('0'):
            variations.append('265' + digits[1:])

        # If starts with 265, try with leading 0 (e.g. 265880218905 → 0880218905)
        if digits.startswith('265') and len(digits) > 3:
            variations.append('0' + digits[3:])
            # Also try just the national number without 0 (e.g. 265880218905 → 880218905)
            variations.append(digits[3:])

        # Deduplicate while preserving order
        seen = set()
        unique = []
        for v in variations:
            if v not in seen:
                seen.add(v)
                unique.append(v)

        return unique

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
            # Not registered as an agent — create a local customer record for
            # conversation tracking only. Then try to link it to an existing
            # customer in the main helpdesk (read-only lookup, never creates).
            customer = self.customers.get_or_create(phone_number, tenant_id)
            customer_id = customer.id
            self.phone_registry.get_or_create(phone_number, tenant_id, customer_id)

            # Update push name if available
            if customer and not customer.name:
                name = push_name or ''
                if name:
                    self.customers.update(customer, name=name)

            # Sync (read-only) with helpdesk to link local customer to any
            # existing helpdesk customer record. Tries multiple phone formats.
            # If no matching customer is found in the main helpdesk, block
            # the conversation — the number must be registered first.
            found = self._sync_customer_with_helpdesk(phone_number, tenant_id)
            if not found:
                logger.info(
                    "Blocked unregistered phone %s for tenant %s",
                    phone_number, tenant_id,
                )
                return _text_reply(
                    '🔒 *Registration Required*\n\n'
                    'Your phone number is not registered in our helpdesk system.\n\n'
                    'To use the WhatsApp chatbot, please contact your administrator '
                    'to have your phone number registered.\n\n'
                    'Once registered, send any message to get started.'
                )

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

        # Look up ticket to confirm with the user
        helpdesk_tenant_id = self._resolve_helpdesk_tenant_id(tenant_id)
        ticket_data = self.helpdesk_api.get_ticket_status(
            ticket_number=ticket_number,
            tenant_id=helpdesk_tenant_id,
        )

        if ticket_data:
            # Ticket found — show title for confirmation
            session.state = 'CONFIRM_COMMENT_TICKET'
            return _build_comment_ticket_confirm(ticket_data)
        else:
            # Try local database fallback
            ticket = self.tickets.get_by_number(ticket_number, tenant_id)
            if ticket:
                ticket_data = {
                    'ticket_number': ticket.ticket_number,
                    'title': ticket.subject or ticket.title or 'No subject',
                    'status': ticket.status.upper() if ticket.status else 'UNKNOWN',
                    'category': ticket.category,
                    'created_at': str(ticket.created_at) if ticket.created_at else None,
                }
                session.state = 'CONFIRM_COMMENT_TICKET'
                return _build_comment_ticket_confirm(ticket_data)

            # Ticket not found
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
        helpdesk_tenant_id = self._resolve_helpdesk_tenant_id(tenant_id)
        ticket_data = self.helpdesk_api.get_ticket_status(
            ticket_number=ticket_number,
            tenant_id=helpdesk_tenant_id,
        )
        if ticket_data:
            return _build_comment_ticket_confirm(ticket_data)
        else:
            ticket = self.tickets.get_by_number(ticket_number, tenant_id)
            if ticket:
                ticket_data = {
                    'ticket_number': ticket.ticket_number,
                    'title': ticket.subject or ticket.title or 'No subject',
                    'status': ticket.status.upper() if ticket.status else 'UNKNOWN',
                    'category': ticket.category,
                    'created_at': str(ticket.created_at) if ticket.created_at else None,
                }
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

        # Determine user_id for the comment
        user_id = None
        if customer_id is not None:
            # Customer - find their user record
            customer = self.customers.get(customer_id)
            if customer:
                # Find user linked to this customer
                from app.models.customer_contact import CustomerContact
                contact = self.db.query(CustomerContact).filter(
                    CustomerContact.customer_id == customer.id
                ).first()
                if contact:
                    user_id = contact.user_id
        else:
            # Registered agent - get their helpdesk user_id
            registration = self._check_user_registration(session.phone_number, tenant_id)
            if registration:
                user_id = registration["user_id"]

        if not user_id:
            session.state = 'MAIN_MENU'
            return _text_reply(
                '❌ *Unable to Add Comment*\n\n'
                'Could not identify your user account. Please contact your administrator.\n\n'
                'Send *0* to return to the main menu.'
            )

        # Call helpdesk API to add comment
        helpdesk_tenant_id = self._resolve_helpdesk_tenant_id(tenant_id)
        result = self.helpdesk_api.add_comment_as_user(
            ticket_number=ticket_number,
            user_id=user_id,
            tenant_id=helpdesk_tenant_id,
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

        # Determine user_id and registration status
        user_id = None
        registration = self._check_user_registration(session.phone_number, tenant_id)
        if registration:
            user_id = registration["user_id"]

        if user_id:
            # Registered user (agent or customer contact) — use helpdesk API
            helpdesk_tenant_id = self._resolve_helpdesk_tenant_id(tenant_id)
            result = self.helpdesk_api.get_my_tickets(
                user_id=user_id,
                tenant_id=helpdesk_tenant_id,
                status_name=status_filter,
                skip=skip,
                limit=per_page,
            )
            tickets = result.get("tickets", [])
            total = result.get("total", 0)
        elif customer_id is not None:
            # Anonymous customer — use local DB as fallback.
            # Query per_page+1 to detect whether a next page exists.
            tickets_raw = self.tickets.list_all(
                tenant_id=tenant_id, customer_id=customer_id, limit=per_page + 1, offset=skip
            )
            has_more = len(tickets_raw) > per_page
            tickets = []
            page_tickets = tickets_raw[:per_page]
            for t in page_tickets:
                t_status = t.status.upper() if t.status else "UNKNOWN"
                # Apply client-side status filter if set
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
            # Estimate total pages: unknown exact total, use approximate
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
            registered_user = self.registered_users.get_by_phone_and_tenant(
                session.phone_number, tenant_id
            )
            if registered_user and registered_user.helpdesk_tenant_id:
                helpdesk_tenant_id = str(registered_user.helpdesk_tenant_id)

        ticket_data = self.helpdesk_api.get_ticket_status(
            ticket_number=ticket_number,
            tenant_id=helpdesk_tenant_id,
        )

        if not ticket_data:
            ticket = self.tickets.get_by_number(ticket_number, tenant_id)
            if not ticket:
                return _text_reply(
                    f'\u274C Ticket `{ticket_number}` was not found.\n\n'
                    'Please check the number and try again, or send *next*/*prev* to browse tickets.\n\n'
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

            # Determine user_id for the comment
            user_id = None
            if customer_id is not None:
                # Customer - find their user record
                customer = self.customers.get(customer_id)
                if customer:
                    # Find user linked to this customer
                    from app.models.customer_contact import CustomerContact
                    contact = self.db.query(CustomerContact).filter(
                        CustomerContact.customer_id == customer.id
                    ).first()
                    if contact:
                        user_id = contact.user_id
            else:
                # Registered agent - get their helpdesk user_id
                registration = self._check_user_registration(session.phone_number, tenant_id)
                if registration:
                    user_id = registration["user_id"]

            if not user_id:
                session.state = 'MAIN_MENU'
                return _text_reply(
                    '❌ *Unable to Add Comment*\n\n'
                    'Could not identify your user account. Please contact your administrator.\n\n'
                    'Send *0* to return to the main menu.'
                )

            # Call helpdesk API to add comment
            helpdesk_tenant_id = self._resolve_helpdesk_tenant_id(tenant_id)
            result = self.helpdesk_api.add_comment_as_user(
                ticket_number=ticket_number,
                user_id=user_id,
                tenant_id=helpdesk_tenant_id,
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
