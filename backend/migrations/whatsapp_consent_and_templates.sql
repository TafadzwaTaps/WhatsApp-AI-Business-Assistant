-- Phase 6 follow-up: WhatsApp marketing consent (per business, with history) and the approved-template registry.
-- NOT applied automatically. Review, validate in STAGING first (see the runbook), then run once in the
-- Supabase SQL editor. Idempotent. Until it is applied the code treats these features as OFF: nothing is
-- gated by consent, no template can be sent, no error is raised.
--
-- !! APPLYING THIS TURNS MARKETING-CONSENT ENFORCEMENT ON !!
-- Approved decision: unknown marketing consent means NOT opted in, and consent is never inferred for existing
-- customers. After this migration, campaigns, broadcasts, cart recovery and re-engagement send only to
-- customers with a recorded opt-in. Transactional messages (order, booking and payment updates) are not affected.
-- Emergency switch without touching the database: set WA_MARKETING_CONSENT_REQUIRED=0 on the service.
--
-- BEFORE RUNNING: confirm the id type matches the integer business_id used below
--   select data_type from information_schema.columns where table_name='businesses' and column_name='id';
SET lock_timeout = '5s';

-- ── 1. Consent history (append-only: every change is a new row; nothing is updated or deleted) ──────────
CREATE TABLE IF NOT EXISTS public.whatsapp_consent_events (
    id          bigserial   PRIMARY KEY,
    business_id integer     NOT NULL,
    phone       text        NOT NULL,                                  -- digits only, no '+', as WhatsApp sends it
    category    text        NOT NULL DEFAULT 'marketing' CHECK (category IN ('marketing')),
    status      text        NOT NULL CHECK (status IN ('opted_in', 'opted_out')),
    source      text        NOT NULL,                                  -- customer_message | dashboard | website_form | in_store | phone_call | qr | import
    evidence    text,                                                  -- short note (for a keyword: the keyword only, max 200 chars); never a full message
    recorded_by text,                                                  -- 'customer' or the dashboard username
    recorded_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS whatsapp_consent_events_lookup_idx
    ON public.whatsapp_consent_events (business_id, phone, recorded_at DESC, id DESC);

-- Reliable current-state lookup: the latest event per (business, phone). Tenant-specific by construction.
CREATE OR REPLACE VIEW public.whatsapp_marketing_consent_current AS
    SELECT DISTINCT ON (business_id, phone)
           business_id, phone, status, source, recorded_at, id
      FROM public.whatsapp_consent_events
     WHERE category = 'marketing'
  ORDER BY business_id, phone, recorded_at DESC, id DESC;

-- ── 2. Approved WhatsApp message templates (registry of what Meta has approved for each business) ───────
CREATE TABLE IF NOT EXISTS public.whatsapp_templates (
    id          bigserial   PRIMARY KEY,
    business_id integer     NOT NULL,
    purpose     text        NOT NULL CHECK (purpose IN ('order_update', 'booking_reminder', 'payment_reminder',
                                                        'cart_recovery', 'reengagement', 'campaign')),
    name        text        NOT NULL,                                  -- the template name exactly as approved in Meta
    language    text        NOT NULL DEFAULT 'en',                     -- Meta language code, e.g. en, en_US, pl, fr
    category    text        NOT NULL CHECK (category IN ('utility', 'marketing')),
    status      text        NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'paused', 'disabled')),
    param_spec  jsonb       NOT NULL DEFAULT '[]'::jsonb,              -- ordered list of context keys for {{1}}, {{2}}...: customer_name, business_name, order_id, amount, date, time, service, link
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (business_id, name, language)
);
CREATE INDEX IF NOT EXISTS whatsapp_templates_lookup_idx ON public.whatsapp_templates (business_id, purpose, status);

-- ── 3. Service-role only (same pattern as the other internal tables) ────────────────────────────────────
ALTER TABLE public.whatsapp_consent_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.whatsapp_templates      ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.whatsapp_consent_events FROM anon, authenticated;
REVOKE ALL ON public.whatsapp_templates      FROM anon, authenticated;
REVOKE ALL ON public.whatsapp_marketing_consent_current FROM anon, authenticated;
REVOKE ALL ON SEQUENCE public.whatsapp_consent_events_id_seq FROM anon, authenticated;
REVOKE ALL ON SEQUENCE public.whatsapp_templates_id_seq      FROM anon, authenticated;

NOTIFY pgrst, 'reload schema';

-- RETENTION: consent history is a compliance record: do NOT delete it routinely. Keep for the life of the business
-- relationship plus the period your jurisdiction requires.
-- ROLLBACK (application stops gating at once; consent history would be lost, so snapshot first):
--   create table _bak_wa_consent as select * from public.whatsapp_consent_events;
--   DROP VIEW  IF EXISTS public.whatsapp_marketing_consent_current;
--   DROP TABLE IF EXISTS public.whatsapp_templates;
--   DROP TABLE IF EXISTS public.whatsapp_consent_events;
