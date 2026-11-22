# WaziBot operations runbook (2026-11-20)

For the person operating the deployment. Names only: **no secret values belong in this file**. Anything about the live Render/Supabase/Meta/Stripe state is **(unverified)**: check it yourself.

## 1. Scheduled work
| Job | How it runs | Once-only guard | Seen in run log (`job_runs`) | Stale after |
|---|---|---|---|---|
| Monthly report email | daemon thread, 1st of month 08:00 UTC | ledger claim per business and month (`notification_log`) | `monthly_report` | 33 days |
| Booking reminders | daemon thread every 30 min | ledger claim per booking and slot | `booking_reminders` | 2 h |
| Scheduled campaigns | cron `POST /campaigns/scheduled/run` (every 15 min recommended) | DB claim `pending`→`processing` | `scheduled_campaigns` | 2 h |
| Trial warnings | cron `POST /billing/trial-warnings` (daily) | ledger claim per business, kind and period | `trial_warnings` | 36 h |
| Account purge | cron `POST /billing/purge-deleted-accounts` (daily) | only processes deletions still marked pending past their purge date (each account independent); not ledger-claimed | `account_purge` | 36 h |
| Cart cleanup | cron `POST /billing/cleanup-carts` (daily) | deletes orphan carts only | not recorded | n/a |
| Lost-reply recovery | cron `POST /webhook/recover-unanswered` (dry run by default) | reply lock + reply-exists check | not recorded | n/a |
| Payment reminders | owner buttons `POST /payments/reminders/send` (once per tier) and `.../{order_id}/nudge` (may re-send a tier) | ledger claim per order and tier; nudge bypasses the refusal | not recorded | n/a |
| Cart recovery / re-engagement | per business, dashboard/growth settings | cooldown in `carts.state_data` + template ledger | not recorded | n/a |

Cron endpoints need header `X-Cron-Secret: <CRON_SECRET>` and refuse to run if `CRON_SECRET` is unset. Check health at `GET /admin/saas/job-runs` (superadmin; needs `job_runs.sql`).

Ledger-backed claims survive restarts and deploys **only if `notification_ledger.sql` is applied** (unverified in production). Without it the guards fall back to in-process behaviour, which resets on every deploy.

## 2. Migrations (all manual, idempotent, review in staging first)
| File | Purpose | Effect when applied | Status |
|---|---|---|---|
| `notification_ledger.sql` | once-only claims table | claims become cross-restart | unverified |
| `email_verification.sql` (+ `_validate.sql`) | email verification | new signups must verify; existing accounts need handling, read `EMAIL_VERIFICATION_MIGRATION_RUNBOOK.md` | unverified |
| `phase3_revoked_tokens.sql`, `billing_lifecycle_hardening.sql` | token revocation, billing fields | additive | unverified |
| `proposed_phase2_stock_restoration.sql` | stock movements ledger | stock restore becomes ledger-guarded | unverified |
| `whatsapp_consent_and_templates.sql` | marketing consent + template registry | **turns marketing-consent enforcement ON** | unverified |
| `scheduled_campaign_failure_info.sql` | campaign outcome columns | additive | unverified |
| `job_runs.sql` | job run log | additive | not applied (new) |
Rollback SQL is in each file's header. Run one at a time, check the app log for errors afterwards.

## 3. Environment variables (names found in code; set in Render, never commit values)
- **Core:** `SECRET_KEY`, `FERNET_KEY`, `DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_KEY`, `SUPABASE_ANON_KEY`, `ALLOWED_ORIGINS`, `TRUSTED_PROXY_DEPTH`, `CRON_SECRET`, `EDGE_SHARED_SECRET`, `SENTRY_DSN`, `WEB_CONCURRENCY` (must stay 1), `RENDER`, `WAZIBOT_URL`, `WAZIBOT_STATIC_DIR`.
- **WhatsApp:** `VERIFY_TOKEN`, `WHATSAPP_APP_SECRET`, `SHARED_WA_TOKEN`, `SHARED_PHONE_NUMBER_ID`, `SHARED_WA_PHONE`, `SHARED_WHATSAPP_NUMBER`, `META_MEDIA_EXTRA_HOSTS`, `ALLOW_UNSIGNED_WEBHOOKS` (never in production), `WA_GRAPH_VERSION` (default `v18.0`), `WA_MARKETING_CONSENT_REQUIRED` (`0` = emergency off), `WA_TEMPLATE_MAX_PER_RUN`, `WA_CATALOG_BATCH_SIZE`.
- **Billing:** `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_*` (starter/growth/business × monthly/annual), `PAYPAL_WEBHOOK_ID`, `PAYMENT_WEBHOOK_SECRET`, `BILLING_PAST_DUE_GRACE_DAYS`, `PRICING_DISPLAY_RATES` (optional JSON for display-only local-currency estimates).
- **Email / AI / other:** `RESEND_API_KEY`, `EMAIL_FROM`, `EMAIL_MAILING_ADDRESS`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `AI_RESPONSE_LLM_*`, `IMAGE_UNDERSTANDING_*`, `MODERATION_VISION_MODEL_*`, `LIBRETRANSLATE_*`, `GOOGLE_CALENDAR_ID`, `GOOGLE_CREDENTIALS_JSON`, `SUPER_ADMIN_USERNAME`, `SUPER_ADMIN_PASSWORD`, `PASSWORD_BREACH_CHECK`, `COOKIE_AUTH_ENABLED` (off), `COOKIE_AUTH_ALLOWED_ORIGINS`, `CSP_REPORT_ONLY`, `CART_RECOVERY_IDLE_MINUTES`, `REENGAGEMENT_*`, `DEFAULT_PAYMENT_NAME`, `DEFAULT_PAYMENT_NUMBER`.

## 4. Safe switches (no deploy needed beyond changing the variable)
- Consent gating off: `WA_MARKETING_CONSENT_REQUIRED=0`.
- Back to the old Graph version: unset `WA_GRAPH_VERSION`.
- Remove local-currency estimates: unset `PRICING_DISPLAY_RATES`.
- Cookie auth stays off unless `COOKIE_AUTH_ENABLED` is set (staging first: `COOKIE_AUTH_STAGE2_STAGING_RUNBOOK.md`).

## 5. After every deploy: quick checks
1. Boot log shows the route-module count and no `Traceback`; the inventory signature check passes.
2. `GET /admin/saas/health` is `ok`; `GET /admin/saas/job-runs` shows recent runs (once the table exists).
3. Send yourself a WhatsApp test message from a test number; confirm one reply.
4. Watch for `wa.failed`, `claim failed`, `notification_log table not found` in the logs for the first hour.

## 6. Rollback
Revert the commit and redeploy; migrations are additive (rollback SQL in each header). Prefer the switches in section 4 for compliance or version issues because they take effect immediately.
