-- Phase 7, stage 2: background-job run log. NOT applied automatically.
-- Validate in STAGING first, then run once in the Supabase SQL editor. Idempotent and purely additive:
-- a new table, nothing existing is changed. Until it is applied, the code records nothing and nothing breaks.
-- Stores counts and an error TYPE name only (no messages, phone numbers, emails or tokens). Rows older than
-- 60 days are pruned by the application.
SET lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS public.job_runs (
    id          bigserial   PRIMARY KEY,
    job         text        NOT NULL,                                   -- monthly_report | booking_reminders | trial_warnings | ...
    status      text        NOT NULL CHECK (status IN ('ok', 'partial', 'error')),
    started_at  timestamptz NOT NULL,
    finished_at timestamptz,
    duration_ms integer,
    summary     jsonb       NOT NULL DEFAULT '{}'::jsonb,               -- integer counts, e.g. {"sent": 3, "skipped": 1}
    error_code  text                                                    -- exception type name only
);
CREATE INDEX IF NOT EXISTS job_runs_job_started_idx ON public.job_runs (job, started_at DESC);
CREATE INDEX IF NOT EXISTS job_runs_started_idx     ON public.job_runs (started_at DESC);

-- The service-role key bypasses RLS; nobody else (anon / signed-in users) may read or write this table.
ALTER TABLE public.job_runs ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.job_runs FROM anon, authenticated;

NOTIFY pgrst, 'reload schema';

-- Rollback (loses the run history only):
--   DROP TABLE IF EXISTS public.job_runs;
