-- ═══════════════════════════════════════════════════════════════════════════
-- Migration: add wa_message_id deduplication column to messages table
-- ═══════════════════════════════════════════════════════════════════════════
--
-- Run in: Supabase Dashboard → SQL Editor → New query → Run
--
-- SAFE TO RUN MULTIPLE TIMES — uses IF NOT EXISTS / DO $$ guards.
-- All existing rows get NULL for wa_message_id which is valid (nullable).
-- Only NEW incoming messages from WhatsApp will have a value.
-- ═══════════════════════════════════════════════════════════════════════════

BEGIN;

-- ── 1. Add the column (nullable — existing rows stay valid) ───────────────
ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS wa_message_id TEXT DEFAULT NULL;

-- ── 2. Unique constraint — one row per WhatsApp message ID ────────────────
--    NULL values are excluded from UNIQUE constraints in PostgreSQL,
--    so outgoing messages and legacy rows (NULL) never conflict.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE  conname = 'uq_messages_wa_message_id'
    ) THEN
        ALTER TABLE messages
            ADD CONSTRAINT uq_messages_wa_message_id
            UNIQUE (wa_message_id);
    END IF;
END
$$;

-- ── 3. Index for fast dedup lookups (message_exists() is called on         ──
--    every inbound webhook hit — must be sub-millisecond)                   ──
CREATE INDEX IF NOT EXISTS ix_messages_wa_message_id
    ON messages (wa_message_id)
    WHERE wa_message_id IS NOT NULL;

COMMIT;

-- ── Verify ────────────────────────────────────────────────────────────────
SELECT
    column_name,
    data_type,
    is_nullable,
    column_default
FROM information_schema.columns
WHERE table_name  = 'messages'
  AND column_name = 'wa_message_id';

-- Expected: 1 row  |  wa_message_id  |  text  |  YES  |  NULL
