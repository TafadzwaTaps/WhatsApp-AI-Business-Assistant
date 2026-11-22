# Phase 7 report (2026-11-22)

Scope: reporting correctness, background-job safety, maintainability. Commits `c84552d` (stage 1), `d445c08` (stage 2), `fddcbf7` (stage 3), `3ee9918` (stage 4 docs), plus the stage 5 fixes below (uncommitted until approved).

## What changed
1. **Once-only job claims** (stage 1): payment reminders (per order and tier), booking reminders (per booking and slot), monthly report (per business and month) claim a `notification_log` row before sending; failure releases the claim; ledger errors fail closed.
2. **Job run log** (stage 2): `services/job_runs.py`, `GET /admin/saas/job-runs` (superadmin), `migrations/job_runs.sql` (not applied). Stores integer counts and an exception type name only.
3. **Reporting** (stage 3): one revenue rule (`counts_as_revenue`: paid or confirmed, not cancelled/refunded) on the dashboard, `/analytics/stats`, admin stats and the monthly email; paged reads; exact counts; `gross_order_value` keeps the old figure; top-customers limit clamped 1-100.
4. **Docs** (stage 4): `ARCHITECTURE_MAP.md`, `OPERATIONS_RUNBOOK.md`.

## Verification (stage 5)
- Dev environment: 2679 passed, 3 skipped (after fixes). Clean venv before the fixes: 2679 passed, 2 skipped.
- Mutation checks: stage 3 reporting file, 10 mutations, all caught (one earlier "survivor" was a malformed mutation; the real paging mutation is caught). Stage 1 and 2 mutation runs were 8/8 and 9/9 earlier.
- Independent Security and QA reviews: no Critical or High findings.

## Review findings and disposition
| Finding | Disposition |
|---|---|
| QA M2: a raising `send_whatsapp` left the booking claim held, so the reminder was lost | **Fixed** with test |
| Security L3: job-runs read used `select("*")` | **Fixed** (explicit columns) |
| Security L2: `job_runs.sql` lacked service_role grants and sequence revoke | **Fixed** (mirrors `notification_ledger.sql`) |
| QA M1: owner "nudge" re-send is blocked after the first send of a tier (returns HTTP 200 with `ok:false`) | **Decision needed** |
| Security M1: ledger "table missing" state is sticky until restart; a transient schema-cache error is read as missing | Open: proposed re-probe every 60 s; until then restart after applying `notification_ledger.sql` |
| Security M2: a send that reached Meta but raised afterwards releases the claim, so a duplicate is possible | Accepted trade-off (at-least-once); documented here |
| QA M3: dashboard `total_revenue` is now lower (paid only); frontend does not show `gross_order_value`; `payment_status="confirmed"` accepted as paid | Owner-visible change: tell customers; `confirmed` as a payment_status is not written by current code |
| QA L1/L2/L5: some wiring tests are source-text checks; no test for a missing ledger table on the booking and monthly paths | Open, low |
| QA L3/L4: only completed runs are recorded; recorder makes small synchronous DB calls | Accepted, low |
| QA L6: a failed stats read sends a zeroed monthly report and claims the month | Open, low (pre-existing behaviour, now permanent for the month) |
| Security L1/L4, I1, I2 | Low or informational, not changed |

## Remaining risks and manual steps
- Apply `notification_ledger.sql` (if not yet) and `job_runs.sql` in staging first; restart afterwards.
- Migrations still unverified in production: see runbook section 2.
- Nothing deployed, pushed or applied by me.
