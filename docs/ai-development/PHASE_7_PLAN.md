# Phase 7 plan: reporting, background jobs, maintainability (2026-11-20)

Status: **discovery only, read-only. No application code changed. Awaiting your approval.**
Note: I worked from the Phase 7 title in the master mission and the open items in `PHASE_0_BASELINE_AUDIT.md` (section D) and `SCALING_NOTES.md`. If your brief for Phase 7 has specific items, paste them and I will fold them in.

## 1. Evidence: current state of every background job
| Job | Trigger | Duplicate-send guard today | Gap |
|---|---|---|---|
| Scheduled campaigns | cron `POST /campaigns/scheduled/run` | DB claim (`pending`→`processing`) | None (fixed Phase 1C; Stage 5 added outcome fields) |
| Trial warnings | cron in `billing_routes.py` | DB ledger `notification_log` if migration applied, else `features_json` marker | `notification_ledger.sql` applied? unknown |
| Monthly report | daemon thread, 1st of month 08:00 UTC (`weekly_report_service.py:310`) | **none** (J1) | A restart or second instance inside the window re-sends |
| Booking reminders | daemon thread every 30 min (`booking_service.py:1469`) | `reminder_24h_sent` flag, set **after** sending, read-then-write (J3) | Two runs can double-send; a crash after send and before the flag re-sends |
| Payment reminders | called by cron/endpoint | **in-process dict** `_last_reminder_sent` (`payment_reminder.py:68`) | Lost on every deploy/restart: a deploy sends the reminder tier again to every unpaid order |
| Cart recovery | cron | `last_cart_recovery_at` in `carts.state_data` (read-then-write) + template ledger | Small race between two runs; otherwise per-customer cooldown holds |
| Re-engagement | cron | `last_reengagement_at` in `state_data` (same pattern) | Same small race |

All schedulers are daemon threads started at import; Render runs one worker (SCALING_NOTES), so cross-worker duplication is a risk only if you scale out.

## 2. Proposed work (small, independently testable stages; each with tests; no new infrastructure)
1. **Idempotent job claims (highest value).** Reuse the existing `notification_log` ledger (`crud/notification_ledger.claim/release`, already used for trial emails and templates) for: monthly report (per business and month), booking reminders (per booking and kind), payment reminders (per order and tier, replacing the in-memory dict). Claim before send, release on failure. Falls back to today's behaviour if the ledger table is missing. *Needs `notification_ledger.sql` applied; I will check it exists in production only if you tell me, I cannot see your database.*
2. **Job run visibility.** A small `job_runs` summary (job name, started/finished, counts, error code) written by a thin wrapper around each job, plus an owner-safe admin endpoint to read the last runs. Additive migration, tolerant when absent. Lets you see that reports and reminders actually ran.
3. **Reporting.** Review the existing analytics endpoints (`/analytics/*`, monthly email) for tenant scoping, correctness of totals (cancelled/refunded orders, currency, time zone), and performance on large tenants (the analytics cache is 60 s). Fixes only for confirmed errors; no new report types without your say-so.
4. **Maintainability (low risk only).** Documentation of module map and job map; remove nothing. Optional: extract nothing from `ai.py` (your rule). Candidate small items: a `docs/ai-development/ARCHITECTURE_MAP.md`, test-suite organisation notes, a pre-commit syntax/test script. Dead modules (`ai_new.py`, `crud/crud.py`, root `inventory.py`) stay untouched unless you approve deletion separately.
5. **Verification:** full suite in dev and clean environments, mutation checks for each claim, independent Security and QA reviews, report, stop.

## 3. Risks and rollback
- Claims change when messages go out only by removing duplicates; first run after deploy could skip a send that "should" repeat. Each is a ledger row, removable.
- Any new table is additive and optional; code falls back without it. Rollback = revert the commit; migrations are drop-only.
- No schedulers are moved or removed; thread start-up is unchanged.

## 4. Decisions needed from you
1. Approve stages 1 to 5 (or choose a subset). I recommend 1 first.
2. Is `notification_ledger.sql` applied in production? (If not, stage 1 for emails falls back to the marker.)
3. Is the consent migration (`whatsapp_consent_and_templates.sql`) applied yet? Until it is, marketing messages are not consent-gated.
4. Any specific Phase 7 reporting requirements beyond the above?
