# Email verification migration — validation runbook (2026-11-19)

Status: **prepared and statically validated. NOT applied anywhere. Awaiting your approval of the final reviewed SQL before any production run.**

## What was validated here (and what was not)
| Check | Result |
|---|---|
| SQL syntax | Parsed with the real PostgreSQL parser (`pglast` 8.9, PostgreSQL 17 grammar): all 10 statements parse |
| Idempotency | Every statement is `IF NOT EXISTS` / `IF EXISTS` or repeatable (`SET`, `REVOKE`, `NOTIFY`) |
| Name conflicts inside the repository | None: no other SQL file or code uses `email_verified_at`, `email_verification_required` or `email_verification_tokens` |
| Type of `businesses.id` | **Cannot be proven from the repository**: there is no base DDL and no foreign key on it. The three other migrations you already ran use `integer` for `business_id`, which is consistent but not proof. `email_verification_validate.sql` part A1 reads the real type |
| Foreign keys | **None added on purpose**: matches the other token tables, avoids a hard dependency on an unverified id type and keeps account deletion simple. If you want one later: `ALTER TABLE ... ADD FOREIGN KEY (business_id) REFERENCES public.businesses(id) ON DELETE CASCADE NOT VALID` then `VALIDATE CONSTRAINT`, using the type A1 reports |
| Running it against a real database | **Not done.** No PostgreSQL is available in this environment and Docker's engine is not running; I did not start it. Run `email_verification_validate.sql` in staging |

## Steps (staging first)
1. **Staging copy** (Supabase branch, or a restored backup in a separate project).
2. Run part A of `backend/migrations/email_verification_validate.sql` (read-only). Stop if A1 is not `integer` (adjust the token table's `business_id`), if A2 returns rows, or if A3 shows long transactions.
3. Run `backend/migrations/email_verification.sql`.
4. Run part B (inside a transaction that rolls back): confirms existing accounts are grandfathered (`required = false`, none verified), counts and billing state unchanged, unique token hash, single-use claim semantics, RLS on and no `anon`/`authenticated` access.
5. Point a staging backend at it and walk through: sign up (link arrives, welcome email arrives only after confirming), campaign refused before and allowed after confirming, existing account sends a campaign, Stripe test subscription unaffected, resend limit, email change.
6. Only then ask for approval of the final SQL for production, and repeat steps 2–4 there at a quiet time.

## Rollback
Application-side: nothing to do; if the columns or table disappear the app stops gating within a minute and keeps working. Database-side (bottom of the SQL file): `DROP TABLE IF EXISTS public.email_verification_tokens; ALTER TABLE public.businesses DROP COLUMN IF EXISTS email_verification_required; ALTER TABLE public.businesses DROP COLUMN IF EXISTS email_verified_at;`. To keep verification times, first run `create table _bak_ev as select id, email_verified_at, email_verification_required from public.businesses where email_verified_at is not null`.
If the `ALTER` is blocked by a lock it fails after 5 seconds (`lock_timeout`) without changing anything: simply run it again.

## Environment variables
| Variable | Needed for | Notes |
|---|---|---|
| `RESEND_API_KEY` | sending the verification email | without it the signup still works but no email is sent (a warning is logged) and the account stays restricted until the owner uses "resend" once it is set |
| `EMAIL_FROM` | sender address | default `WaziBot <noreply@wazibothq.com>`; the domain must be verified in Resend |
| `WAZIBOT_URL` | base of the link in the email | default `https://wazibothq.com`; the link opens `/static/verify-email.html` |
| `EMAIL_MAILING_ADDRESS` | CAN-SPAM footer | already warned about at start-up |
| `SECRET_KEY` | JWTs and unsubscribe links | must stay unchanged during this rollout |
| `CRON_SECRET` | trial-warning and scheduled-campaign cron endpoints | already required |
No new variable is required.

## Deployment order
1. Deploy the application code (verification stays OFF: columns and table are absent).
2. Confirm `/static/verify-email.html` loads in production (through Cloudflare).
3. Apply the migration (staging first). The application detects it within about 60 seconds (no restart).
4. Smoke test with one new test account. 5. Watch the logs for `email verification:` warnings for a day.
Applying the migration before the code is also safe (the old code ignores the columns).

## Behaviour to expect after applying
New signups are restricted from campaigns, trial and report emails until they confirm; **existing accounts are untouched** (and may confirm voluntarily from the banner). Enforcing on existing accounts is a separate, later decision (see the comments in the SQL file).
