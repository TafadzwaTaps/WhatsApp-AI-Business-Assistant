-- Phase 3b: server-side token revocation (logout).
-- NOT applied automatically. Review, then run once in the Supabase SQL editor.
-- Code treats a missing table as "nothing revoked" (logs a warning, fails open).

CREATE TABLE IF NOT EXISTS public.revoked_tokens (
    jti         text        PRIMARY KEY,
    business_id integer,
    expires_at  timestamptz,
    revoked_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS revoked_tokens_expires_at_idx ON public.revoked_tokens (expires_at);

-- Service-role only: RLS on, deliberately NO policies (anon/authenticated get nothing).
ALTER TABLE public.revoked_tokens ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.revoked_tokens FROM anon, authenticated;

-- ROLLBACK:
--   DROP TABLE IF EXISTS public.revoked_tokens;
