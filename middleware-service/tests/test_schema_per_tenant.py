"""Offline tests for the schema-per-tenant middleware.

No Postgres / RabbitMQ / helpdesk needed: the middleware DB is SQLite and the
helpdesk backend is replaced by an in-process fake that, like the real one,
only answers for the tenant named by X-Tenant-ID *and* whose X-API-Key matches.

Run from middleware-service/:  python -m pytest tests -q
"""

import json
import uuid

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.db.session import Base
import app.models.command_log  # noqa: F401
import app.models.customer  # noqa: F401
import app.models.event_log  # noqa: F401
import app.models.instance_tenant  # noqa: F401
import app.models.phone_registry  # noqa: F401
import app.models.processed_message  # noqa: F401
import app.models.registered_user  # noqa: F401
import app.models.tenant  # noqa: F401
import app.models.ticket  # noqa: F401
import app.models.ticket_comment  # noqa: F401
import app.models.ticket_message  # noqa: F401
import app.models.whatsapp_session  # noqa: F401
from app.repositories import InstanceTenantRepository, TenantRepository
from app.services.conversation_service import ConversationService

KEYS = {'alpha': 'hd_alpha_key', 'beta': 'hd_beta_key'}
PHONE = '265991234567'  # the SAME number is used in both tenants


class FakeHelpdesk:
    """Per-tenant state + request log; mimics tenant resolution and key binding."""

    def __init__(self):
        self.users = {'alpha': {PHONE: str(uuid.uuid4())}, 'beta': {}}
        self.tickets = {'alpha': [], 'beta': []}
        self.calls = []
        self.refuse = False

    def __call__(self, method, url, params=None, json=None, headers=None, timeout=None):
        slug = headers.get('X-Tenant-ID')
        key = headers.get('X-API-Key')
        self.calls.append((method, url, slug, key))
        path = url.split('/integration/whatsapp', 1)[1]
        if KEYS.get(slug) != key:  # key must belong to the resolved tenant
            return httpx.Response(401, json={'detail': 'Invalid API key'})
        if path == '/users/by-phone':
            uid = self.users[slug].get(''.join(c for c in params['phone_number'] if c.isdigit()))
            body = {'is_registered': bool(uid), 'phone_number': params['phone_number']}
            if uid:
                body.update(user_id=uid, full_name='Test User')
            return httpx.Response(200, json=body)
        if path == '/categories':
            cats = {'alpha': ['Incident', 'General Inquiry'], 'beta': ['Billing Issue']}[slug]
            return httpx.Response(200, json={'categories': [{'name': c} for c in cats]})
        if path == '/tickets' and method == 'POST':
            if self.refuse:
                return httpx.Response(400, json={'detail': 'An active subscription is required to create a ticket'})
            n = f'{slug.upper()}-{len(self.tickets[slug]) + 1:05d}'
            t = {'id': str(uuid.uuid4()), 'ticket_number': n, 'title': json['title'], 'status': 'open',
                 'category': json.get('category'), 'priority': 'medium', 'created_at': '2026-11-04T10:00:00',
                 '_user': json['user_id']}
            self.tickets[slug].append(t)
            return httpx.Response(201, json=t)
        if path == '/tickets' and method == 'GET':
            mine = [t for t in self.tickets[slug] if t['_user'] == params['user_id']]
            return httpx.Response(200, json={'tickets': mine, 'total': len(mine), 'skip': 0, 'limit': 10})
        if path.startswith('/tickets/by-number/') and method == 'GET':
            n = path.rsplit('/', 1)[1]
            for t in self.tickets[slug]:
                if t['ticket_number'] == n and t['_user'] == params['user_id']:
                    return httpx.Response(200, json=t)
            return httpx.Response(404, json={'detail': 'Ticket not found'})
        return httpx.Response(500)


@pytest.fixture()
def env(monkeypatch):
    engine = create_engine('sqlite://', poolclass=StaticPool, connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False)
    db = Session()

    monkeypatch.setattr(settings, 'helpdesk_tenant_api_keys', json.dumps(KEYS))
    monkeypatch.setattr(settings, 'helpdesk_tenant_domain_suffix', '')
    fake = FakeHelpdesk()
    monkeypatch.setattr(httpx, 'request', fake)

    tenants = TenantRepository(db)
    links = InstanceTenantRepository(db)
    for name, slug in (('Alpha', 'alpha'), ('Beta', 'beta')):
        t = tenants.create(name, helpdesk_tenant_slug=slug)
        links.create(f'inst-{slug}', t.id)
    unmapped = tenants.create('Unmapped')  # no slug
    links.create('inst-unmapped', unmapped.id)
    yield db, fake
    db.close()


def say(db, instance, text, mid=None, phone=PHONE):
    return ConversationService(db).process_message(instance, phone, text, message_id=mid)


def create_ticket_flow(db, instance):
    say(db, instance, '1')
    say(db, instance, 'Printer is down')
    say(db, instance, 'The office printer shows an error')
    say(db, instance, '1')       # category
    say(db, instance, 'skip')    # cc
    return say(db, instance, '1')  # submit


def test_registered_user_creates_ticket_in_own_tenant_only(env):
    db, fake = env
    reply = create_ticket_flow(db, 'inst-alpha')
    assert 'Ticket Created Successfully' in reply['text']
    assert len(fake.tickets['alpha']) == 1 and fake.tickets['beta'] == []
    posts = [c for c in fake.calls if c[0] == 'POST']
    assert posts and all(c[2] == 'alpha' and c[3] == KEYS['alpha'] for c in posts)


def test_same_phone_in_other_tenant_is_not_registered(env):
    db, fake = env
    reply = say(db, 'inst-beta', '1')
    assert 'Registration Required' in reply['text']
    assert fake.tickets['beta'] == [] and fake.tickets['alpha'] == []
    assert all(c[2] == 'beta' and c[3] == KEYS['beta'] for c in fake.calls)


def test_sessions_are_isolated_per_tenant(env):
    db, fake = env
    say(db, 'inst-alpha', '1')              # alpha session -> WAITING_SUBJECT
    reply = say(db, 'inst-beta', 'hello')   # beta session untouched -> menu
    assert 'Main Menu' in reply['text']
    from app.models.whatsapp_session import WhatsappSession
    states = {s.tenant_id: s.state for s in db.query(WhatsappSession).all()}
    assert sorted(states.values()) == ['MAIN_MENU', 'WAITING_SUBJECT']


def test_tickets_listing_and_lookup_are_tenant_scoped(env):
    db, fake = env
    create_ticket_flow(db, 'inst-alpha')
    listing = say(db, 'inst-alpha', '2'), say(db, 'inst-alpha', '1')
    assert 'ALPHA-00001' in listing[1]['text']
    # tenant beta cannot see alpha's ticket number
    fake.users['beta'][PHONE] = str(uuid.uuid4())
    say(db, 'inst-beta', 'check')                       # "Check Ticket Status" prompt
    reply = say(db, 'inst-beta', 'ALPHA-00001')
    assert 'not found' in reply['text'].lower()
    say(db, 'inst-alpha', '0')
    say(db, 'inst-alpha', 'check')
    assert 'ALPHA-00001' in say(db, 'inst-alpha', 'ALPHA-00001')['text']


def test_duplicate_delivery_does_not_duplicate_ticket(env):
    db, fake = env
    say(db, 'inst-alpha', '1', mid='m1')
    say(db, 'inst-alpha', 'Printer is down', mid='m2')
    say(db, 'inst-alpha', 'details', mid='m3')
    say(db, 'inst-alpha', '1', mid='m4')
    say(db, 'inst-alpha', 'skip', mid='m5')
    assert say(db, 'inst-alpha', '1', mid='m6')['type'] == 'text'
    assert say(db, 'inst-alpha', '1', mid='m6') is None   # Evolution retry
    assert len(fake.tickets['alpha']) == 1


def test_unmapped_tenant_fails_closed_without_calling_backend(env):
    db, fake = env
    reply = say(db, 'inst-unmapped', '1')
    assert 'not configured' in reply['text']
    assert fake.calls == []


def test_unlinked_instance_fails_closed(env):
    db, fake = env
    assert 'not configured' in say(db, 'inst-unknown', '1')['text']
    assert fake.calls == []


def test_missing_api_key_never_falls_back_to_another_tenants_key(env, monkeypatch):
    db, fake = env
    monkeypatch.setattr(settings, 'helpdesk_tenant_api_keys', json.dumps({'beta': KEYS['beta']}))
    reply = say(db, 'inst-alpha', '1')   # alpha key removed
    assert 'Registration Required' in reply['text']
    assert fake.calls == []


def test_domain_suffix_adds_forwarded_host(env, monkeypatch):
    db, fake = env
    seen = {}
    orig = fake.__call__

    def spy(method, url, params=None, json=None, headers=None, timeout=None):
        seen.update(headers)
        return orig(method, url, params, json, headers, timeout)

    monkeypatch.setattr(settings, 'helpdesk_tenant_domain_suffix', '.mitra.mw')
    monkeypatch.setattr(httpx, 'request', spy)
    say(db, 'inst-alpha', '1')
    assert seen['X-Forwarded-Host'] == 'alpha.mitra.mw'


def test_category_menu_uses_tenant_categories_and_ticket_carries_name(env):
    db, fake = env
    say(db, 'inst-alpha', '1')
    say(db, 'inst-alpha', 'Printer is down')
    menu = say(db, 'inst-alpha', 'details')
    assert 'Incident' in menu['text'] and 'General Inquiry' in menu['text'] and 'Billing' not in menu['text']
    say(db, 'inst-alpha', '2')   # General Inquiry
    say(db, 'inst-alpha', 'skip')
    say(db, 'inst-alpha', '1')
    assert fake.tickets['alpha'][0]['category'] == 'General Inquiry'


def test_helpdesk_refusal_reason_is_shown_not_called_unavailable(env):
    db, fake = env
    fake.refuse = True
    reply = create_ticket_flow(db, 'inst-alpha')
    assert 'subscription is required' in reply['text']
    assert 'unavailable' not in reply['text'].lower()
