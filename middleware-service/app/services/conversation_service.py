import uuid
import logging

from sqlalchemy.orm import Session

from app.models.ticket import Ticket
from app.models.whatsapp_session import WhatsappSession
from app.repositories import (
    CustomerRepository,
    InstanceTenantRepository,
    TicketMessageRepository,
    TicketRepository,
    WhatsappSessionRepository,
)

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
    return _build_main_menu()


def _build_main_menu() -> dict:
    return _list_reply(
        text='Choose an option below to get started:',
        title='\uD83D\uDC4B Welcome to Support!',
        button_text='View Options',
        sections=[
            {
                'title': 'Support Options',
                'rows': [
                    {'title': '\u2709\uFE0F Create Ticket', 'description': 'Report a new issue or request help', 'rowId': 'create_ticket'},
                    {'title': '\uD83D\uDD0D Check Ticket', 'description': 'Check the status of an existing ticket', 'rowId': 'check_ticket'},
                    {'title': '\uD83D\uDCAC Speak to Agent', 'description': 'Talk to a human support agent', 'rowId': 'speak_agent'},
                ],
            },
        ],
        footer='Select an option to continue',
    )


def _build_category_list() -> dict:
    return _list_reply(
        text='Which category best describes your issue?',
        title='\uD83D\uDCC1 Issue Category',
        button_text='Select Category',
        sections=[
            {
                'title': 'Categories',
                'rows': [
                    {'title': '\uD83C\uDF10 Network', 'description': 'Internet, connectivity, VPN issues', 'rowId': 'cat_network'},
                    {'title': '\uD83D\uDCB3 Billing', 'description': 'Invoices, payments, subscriptions', 'rowId': 'cat_billing'},
                    {'title': '\uD83D\uDD27 Technical Support', 'description': 'Software, hardware, system errors', 'rowId': 'cat_tech'},
                    {'title': '\u2753 Other', 'description': 'Anything else not listed above', 'rowId': 'cat_other'},
                ],
            },
        ],
        footer='Pick the closest category',
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
        footer='Choose an action',
    )


def _build_ticket_created(ticket: Ticket) -> dict:
    return _text_reply(
        f'\u2705 *Ticket Created Successfully!*\n\n'
        f'\u2022 *Ticket Number:* `{ticket.ticket_number}`\n'
        f'\u2022 *Status:* Open\n'
        f'\u2022 *Category:* {ticket.category or "N/A"}\n\n'
        f'Our team will review your request and get back to you as soon as possible.\n\n'
        f'To return to the main menu at any time, send *0*.'
    )


def _build_ticket_status(ticket: Ticket) -> dict:
    return _text_reply(
        f'\uD83D\uDD0D *Ticket Details*\n\n'
        f'\u2022 *Number:* `{ticket.ticket_number}`\n'
        f'\u2022 *Status:* `{ticket.status.upper()}`\n'
        f'\u2022 *Subject:* {ticket.subject}\n'
        f'\u2022 *Category:* {ticket.category or "N/A"}\n'
        f'\u2022 *Created:* {ticket.created_at.strftime("%Y-%m-%d %H:%M")}\n\n'
        f'Send *0* to return to the main menu.'
    )


def _build_escalate_reply() -> dict:
    return _text_reply(
        '\uD83D\uDCAC *Speak to Agent*\n\n'
        'An agent will be with you shortly. In the meantime, please describe your issue '
        'and we will make sure the right team handles it.\n\n'
        'Send *0* to return to the main menu.'
    )


def _build_subject_prompt() -> dict:
    return _text_reply(
        '\u2709\uFE0F *Step 1: Subject*\n\n'
        'Please enter a *short subject* for your ticket (e.g., "Internet not working", '
        '"Payment issue", "Account access problem").\n\n'
        'Keep it brief — one line is enough.\n\n'
        'Send *0* at any time to cancel and return to the main menu.'
    )


def _build_description_prompt() -> dict:
    return _text_reply(
        '\uD83D\uDCDD *Step 2: Description*\n\n'
        'Now please describe your issue in *detail*:\n'
        '\u2022 What happened?\n'
        '\u2022 When did it start?\n'
        '\u2022 Any error messages?\n\n'
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

    def _is_session_stale(self, session: WhatsappSession) -> bool:
        from datetime import datetime, timedelta
        cutoff = datetime.utcnow() - timedelta(minutes=SESSION_TIMEOUT_MINUTES)
        return session.last_activity < cutoff and session.state != 'MAIN_MENU'

    def process_message(
        self, instance_name: str, phone_number: str, text: str, message_id: str | None = None, push_name: str | None = None
    ) -> dict:
        link = self.instance_tenants.get_by_instance(instance_name)
        if not link:
            logger.warning('No tenant linked to instance %s', instance_name)
            return _text_reply('This support line is not configured. Please contact the administrator.')

        tenant_id = link.tenant_id
        customer = self.customers.get_or_create(phone_number, tenant_id)
        session = self.sessions.get_or_create(phone_number, tenant_id)
        self.sessions.set_customer(session, customer.id)

        if customer and not customer.name:
            name = push_name or ''
            if name:
                self.customers.update(customer, name=name)

        if self._is_session_stale(session):
            session.ticket_draft = None
            session.state = 'MAIN_MENU'

        reply = self._handle_state(session, text.strip(), customer.id, tenant_id)
        self.sessions.update_state(session, session.state)
        return reply

    def _handle_state(
        self, session: WhatsappSession, text: str, customer_id: uuid.UUID, tenant_id: uuid.UUID
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
            return self._handle_check_ticket(session, text, tenant_id)

        session.state = 'MAIN_MENU'
        return _build_main_menu()

    def _handle_main_menu(
        self, session: WhatsappSession, text: str, customer_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> dict:
        choice = text.strip().lower()

        if choice in ('1', 'create_ticket', 'create ticket'):
            session.ticket_draft = {}
            session.state = 'WAITING_SUBJECT'
            return _build_subject_prompt()

        if choice in ('2', 'check_ticket', 'check ticket'):
            session.state = 'CHECKING_TICKET'
            return _text_reply(
                '\uD83D\uDD0D *Check Ticket Status*\n\n'
                'Please enter your ticket number (e.g., `TKT-2026-00001`).\n\n'
                'Send *0* to return to the main menu.'
            )

        if choice in ('3', 'speak_agent', 'speak to agent', 'agent'):
            session.state = 'MAIN_MENU'
            return _build_escalate_reply()

        draft = session.ticket_draft
        if not draft or not draft.get('subject'):
            session.ticket_draft = {'subject': text[:200], 'description': text}
            session.state = 'WAITING_CATEGORY'
            return _build_category_list()

        return _build_main_menu()

    def _handle_subject(self, session: WhatsappSession, text: str) -> dict:
        if _is_cancel(text):
            session.ticket_draft = None
            session.state = 'MAIN_MENU'
            return _cancel_reply()

        draft = session.ticket_draft or {}
        draft['subject'] = text[:200]
        session.ticket_draft = draft
        session.state = 'WAITING_DESCRIPTION'
        return _build_description_prompt()

    def _handle_description(self, session: WhatsappSession, text: str) -> dict:
        if _is_cancel(text):
            session.ticket_draft = None
            session.state = 'MAIN_MENU'
            return _cancel_reply()

        draft = session.ticket_draft or {}
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

        draft = session.ticket_draft or {}
        draft['category'] = category
        session.ticket_draft = draft
        session.state = 'CONFIRM_TICKET'
        return _build_confirm_buttons(draft)

    def _handle_confirm(
        self, session: WhatsappSession, text: str, customer_id: uuid.UUID, tenant_id: uuid.UUID
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
            return _build_ticket_created(ticket)

        if normalized in ('confirm_edit_subject', '2', 'edit subject', 'edit'):
            session.state = 'WAITING_SUBJECT'
            return _build_subject_prompt()

        if normalized in ('confirm_cancel', '3', 'cancel'):
            session.ticket_draft = None
            session.state = 'MAIN_MENU'
            return _cancel_reply()

        draft = session.ticket_draft or {}
        return _build_confirm_buttons(draft)

    def _handle_check_ticket(self, session: WhatsappSession, text: str, tenant_id: uuid.UUID) -> dict:
        if _is_cancel(text):
            session.state = 'MAIN_MENU'
            return _cancel_reply()

        ticket_number = text.strip().upper()
        ticket = self.tickets.get_by_number(ticket_number, tenant_id)

        if not ticket:
            return _text_reply(
                f'\u274C Ticket `{ticket_number}` was not found.\n\n'
                'Please check the number and try again, or send *0* to return to the main menu.'
            )

        session.state = 'MAIN_MENU'
        return _build_ticket_status(ticket)
