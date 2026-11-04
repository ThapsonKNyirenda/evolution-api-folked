-- Schema-per-tenant helpdesk support.
-- The helpdesk now identifies tenants by slug (public.ft_tenants.identifier), not by UUID.
-- Run once against the middleware database (create_all() does not alter existing tables).

BEGIN;

ALTER TABLE tenants ADD COLUMN IF NOT EXISTS helpdesk_tenant_slug VARCHAR(120);
CREATE UNIQUE INDEX IF NOT EXISTS ix_tenants_helpdesk_tenant_slug ON tenants (helpdesk_tenant_slug);

-- Idempotency ledger for inbound WhatsApp messages (webhook retries).
CREATE TABLE IF NOT EXISTS processed_messages (
    id UUID PRIMARY KEY,
    instance_name VARCHAR(255) NOT NULL,
    message_id VARCHAR(255) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_processed_message_instance_msg UNIQUE (instance_name, message_id)
);
CREATE INDEX IF NOT EXISTS ix_processed_messages_created_at ON processed_messages (created_at);

-- After this, set tenants.helpdesk_tenant_slug for every tenant that has a WhatsApp instance, e.g.:
--   UPDATE tenants SET helpdesk_tenant_slug = 'acme' WHERE name = 'Acme Corp';
-- The old helpdesk UUID columns are no longer read; drop them once verified:
--   ALTER TABLE tenants DROP COLUMN helpdesk_tenant_id;

COMMIT;
