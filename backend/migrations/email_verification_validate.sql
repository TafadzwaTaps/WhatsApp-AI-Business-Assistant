-- Validation script for email_verification.sql. RUN IN STAGING OR AN ISOLATED COPY FIRST, never first in production.
-- Part A is read-only (safe anywhere). Part B runs inside a transaction that is ROLLED BACK at the end.
-- Nothing here changes data permanently.

-- ── PART A: pre-flight (read-only) ──────────────────────────────────────────────────────────────
-- A1. The id type of businesses. The token table uses `integer`; if this shows bigint or uuid, change
--     email_verification_tokens.business_id to the same type BEFORE running the migration.
SELECT data_type FROM information_schema.columns
 WHERE table_schema = 'public' AND table_name = 'businesses' AND column_name = 'id';

-- A2. Name conflicts: expect ZERO rows from both queries.
SELECT column_name FROM information_schema.columns
 WHERE table_schema = 'public' AND table_name = 'businesses'
   AND column_name IN ('email_verified_at', 'email_verification_required');
SELECT table_name FROM information_schema.tables
 WHERE table_schema = 'public' AND table_name = 'email_verification_tokens';

-- A3. PostgreSQL version (>= 11 makes ADD COLUMN ... DEFAULT metadata-only) and long-running transactions
--     that could make the ALTER wait for its lock (the migration sets lock_timeout = 5s and fails fast).
SHOW server_version;
SELECT pid, now() - xact_start AS running_for, left(query, 80) AS query
  FROM pg_stat_activity WHERE xact_start IS NOT NULL AND now() - xact_start > interval '30 seconds' AND pid <> pg_backend_pid();

-- A4. Baseline for the "existing users unchanged" check afterwards.
SELECT count(*) AS businesses_before,
       count(*) FILTER (WHERE is_active) AS active_before,
       count(*) FILTER (WHERE billing_status IN ('active', 'trialing', 'past_due')) AS billing_live_before
  FROM public.businesses;

-- ── PART B: functional validation, after email_verification.sql has been applied to STAGING ────
BEGIN;

-- B1. Existing accounts are grandfathered: every row has required = false and verified_at NULL.
SELECT count(*) FILTER (WHERE email_verification_required IS NOT FALSE) AS must_be_zero_required_not_false,
       count(*) FILTER (WHERE email_verified_at IS NOT NULL)            AS must_be_zero_already_verified
  FROM public.businesses;

-- B2. Row counts and billing state are unchanged (compare with A4).
SELECT count(*) AS businesses_after,
       count(*) FILTER (WHERE is_active) AS active_after,
       count(*) FILTER (WHERE billing_status IN ('active', 'trialing', 'past_due')) AS billing_live_after
  FROM public.businesses;

-- B3. Token table: unique hash, defaults, partial index in place.
INSERT INTO public.email_verification_tokens (business_id, email, token_hash, expires_at)
     SELECT id, 'validate@example.invalid', 'validate-hash-1', now() + interval '1 day' FROM public.businesses LIMIT 1;
-- must fail with a unique violation (run alone to see the error):
--   INSERT INTO public.email_verification_tokens (business_id, email, token_hash, expires_at)
--        SELECT id, 'validate@example.invalid', 'validate-hash-1', now() + interval '1 day' FROM public.businesses LIMIT 1;
SELECT id, used_at, created_at FROM public.email_verification_tokens WHERE token_hash = 'validate-hash-1';

-- B4. Single-use claim semantics the application relies on: the second update must affect 0 rows.
UPDATE public.email_verification_tokens SET used_at = now() WHERE token_hash = 'validate-hash-1' AND used_at IS NULL;   -- 1 row
UPDATE public.email_verification_tokens SET used_at = now() WHERE token_hash = 'validate-hash-1' AND used_at IS NULL;   -- 0 rows

-- B5. Privileges: anon and authenticated must have NO access (expect zero rows).
SELECT grantee, privilege_type FROM information_schema.role_table_grants
 WHERE table_schema = 'public' AND table_name = 'email_verification_tokens' AND grantee IN ('anon', 'authenticated');
SELECT relrowsecurity AS rls_enabled FROM pg_class WHERE oid = 'public.email_verification_tokens'::regclass;   -- must be true

ROLLBACK;   -- nothing above is kept

-- ── PART C: after applying in production (read-only) ───────────────────────────────────────────
--   Repeat B1 and B2 and compare with A4. Then sign up a test account and confirm one row appears in
--   email_verification_tokens and the account has email_verification_required = true.
