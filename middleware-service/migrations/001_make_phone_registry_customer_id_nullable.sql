-- Migration: Make phone_registry.customer_id nullable
-- Reason: Not every user (phone number) belongs to a customer.
-- Registered helpdesk agents may use WhatsApp without being a customer.

ALTER TABLE phone_registry ALTER COLUMN customer_id DROP NOT NULL;
