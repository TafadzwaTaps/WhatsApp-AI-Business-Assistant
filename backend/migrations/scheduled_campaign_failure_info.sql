-- Phase 6 follow-up (Stage 5): structured outcome for scheduled campaigns.
-- NOT applied automatically. Validate in STAGING first, then run once in the Supabase SQL editor. Idempotent.
-- Purely additive: every column is nullable, no existing column, row, index or constraint is changed or removed.
-- Until it is applied the service works exactly as before (the runner falls back to the old status-only update).
--
-- Columns
--   outcome          delivered | partial | failed   ('failed' = nobody was reached; 'partial' = some were reached)
--   failure_code     safe code: window_closed, no_consent, opted_out, rate_limited, template_error,
--                    recipient_invalid, meta_error, internal_error, no_recipients, no_eligible_recipients,
--                    send_cap, partial
--   failure_detail   short fixed sentence for the owner (never a provider message, token or phone number)
--   *_count          how many recipients were sent / failed / skipped, and the audience size
-- The existing `status` column (pending | processing | sent | failed | cancelled) is NOT changed.
SET lock_timeout = '5s';

ALTER TABLE public.scheduled_campaigns ADD COLUMN IF NOT EXISTS outcome          text;
ALTER TABLE public.scheduled_campaigns ADD COLUMN IF NOT EXISTS failure_code     text;
ALTER TABLE public.scheduled_campaigns ADD COLUMN IF NOT EXISTS failure_detail   text;
ALTER TABLE public.scheduled_campaigns ADD COLUMN IF NOT EXISTS sent_count       integer;
ALTER TABLE public.scheduled_campaigns ADD COLUMN IF NOT EXISTS failed_count     integer;
ALTER TABLE public.scheduled_campaigns ADD COLUMN IF NOT EXISTS skipped_count    integer;
ALTER TABLE public.scheduled_campaigns ADD COLUMN IF NOT EXISTS total_recipients integer;

NOTIFY pgrst, 'reload schema';

-- Rollback (only if ever needed; loses the stored outcomes, nothing else):
--   ALTER TABLE public.scheduled_campaigns
--     DROP COLUMN IF EXISTS outcome, DROP COLUMN IF EXISTS failure_code, DROP COLUMN IF EXISTS failure_detail,
--     DROP COLUMN IF EXISTS sent_count, DROP COLUMN IF EXISTS failed_count, DROP COLUMN IF EXISTS skipped_count,
--     DROP COLUMN IF EXISTS total_recipients;
