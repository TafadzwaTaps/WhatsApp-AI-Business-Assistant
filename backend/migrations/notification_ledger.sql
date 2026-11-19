-- Proposed migration: notification ledger ("send at most once").
-- NOT applied automatically. Review, then run once in the Supabase SQL editor.
-- The code works without it (it falls back to a marker in businesses.features_json),
-- but the table makes the guarantee atomic across processes.
--
-- Pre-check (read-only): select to_regclass('public.notification_log');   -- NULL = not created yet

CREATE TABLE IF NOT EXISTS public.notification_log (
    id          bigserial   PRIMARY KEY,
    business_id integer     NOT NULL,
    kind        text        NOT NULL,      -- e.g. trial_warn_7, trial_warn_3, trial_warn_1, trial_expired
    period_key  text        NOT NULL,      -- e.g. the trial_ends_at value, so a new trial starts fresh
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT notification_log_once UNIQUE (business_id, kind, period_key)
);

CREATE INDEX IF NOT EXISTS notification_log_business_idx ON public.notification_log (business_id);

-- Service-role only: RLS on, deliberately NO policies (anon/authenticated get nothing),
-- consistent with the existing lockdown.
ALTER TABLE public.notification_log ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.notification_log FROM anon, authenticated;
REVOKE ALL ON SEQUENCE public.notification_log_id_seq FROM anon, authenticated;
GRANT ALL ON public.notification_log TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.notification_log_id_seq TO service_role;
NOTIFY pgrst, 'reload schema';

-- ROLLBACK:
--   DROP TABLE IF EXISTS public.notification_log;
