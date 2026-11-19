-- Phase 4 follow-up: owner email verification.
-- NOT applied automatically. Review, then run once in the Supabase SQL editor (idempotent).
-- Until it is applied the code treats verification as OFF: nothing is gated, no extra emails.
--
-- MIGRATION DECISION FOR EXISTING ACCOUNTS (documented, deliberate):
--   * Existing rows get email_verification_required = FALSE: they are "grandfathered", keep full
--     access and are NOT gated. Nothing is changed for subscribers, trials or customers.
--   * They are NOT marked verified: email_verified_at stays NULL, because we have never proven they
--     own the address. They are invited to verify from the dashboard; verifying records the time.
--   * Accounts created after the code ships are set to required = TRUE by the application (including
--     signups without an email: they stay restricted until they add and confirm one). An existing
--     account that was verified, or is already required, becomes required again if it changes its
--     owner email. A grandfathered account that changes its email is only SENT a verification link.
--   * Enforcing verification on existing accounts is a separate, later, announced decision, in cohorts
--     (active accounts first), after verification emails and a notice period, snapshotting ids first:
--       UPDATE public.businesses SET email_verification_required = TRUE
--        WHERE email_verified_at IS NULL AND owner_email IS NOT NULL AND owner_email <> '' AND id IN (<cohort>);
--     Dormant accounts with stale or mistyped emails can never verify: leave them grandfathered.
--     Other app processes cache business rows briefly: restart or wait a minute after a manual UPDATE.

-- BEFORE RUNNING: confirm the id type matches the integer business_id used below
--   select data_type from information_schema.columns where table_name='businesses' and column_name='id';
--   (if it is bigint or uuid, change business_id in the token table to the same type first)
-- Adding a column with a constant default is metadata-only on PostgreSQL 11+, but it needs a brief
-- exclusive lock; fail fast instead of queueing behind a long transaction (re-run if it times out).
SET lock_timeout = '5s';

ALTER TABLE public.businesses ADD COLUMN IF NOT EXISTS email_verified_at timestamptz;
ALTER TABLE public.businesses ADD COLUMN IF NOT EXISTS email_verification_required boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS public.email_verification_tokens (
    id          bigserial   PRIMARY KEY,
    business_id integer     NOT NULL,
    email       text        NOT NULL,
    token_hash  text        NOT NULL UNIQUE,      -- SHA-256 of the emailed token; the token itself is never stored
    expires_at  timestamptz NOT NULL,
    used_at     timestamptz,
    ip_address  text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS email_verification_tokens_business_idx
    ON public.email_verification_tokens (business_id) WHERE used_at IS NULL;
CREATE INDEX IF NOT EXISTS email_verification_tokens_expires_idx
    ON public.email_verification_tokens (expires_at);

-- Service-role only: RLS on, deliberately NO policies (anon/authenticated get nothing).
ALTER TABLE public.email_verification_tokens ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.email_verification_tokens FROM anon, authenticated;
REVOKE ALL ON SEQUENCE public.email_verification_tokens_id_seq FROM anon, authenticated;

-- RETENTION (proposal, run daily or via pg_cron; a token is live for 24h, rows are tiny):
--   DELETE FROM public.email_verification_tokens
--    WHERE expires_at < now() - interval '7 days' OR (used_at IS NOT NULL AND used_at < now() - interval '7 days');

-- Make PostgREST see the new columns immediately.
NOTIFY pgrst, 'reload schema';

-- ROLLBACK (safe at any time; the application simply stops gating):
--   DROP TABLE IF EXISTS public.email_verification_tokens;
--   ALTER TABLE public.businesses DROP COLUMN IF EXISTS email_verification_required;
--   ALTER TABLE public.businesses DROP COLUMN IF EXISTS email_verified_at;
