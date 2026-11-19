# Scaling notes — WaziBot runs as ONE web process (2026-11-19)

Confirmed by the owner: Render currently runs **exactly one web instance with one worker**. The code relies on that in the places below. **Before running more than one worker or instance, every item here needs a database-backed replacement**; otherwise duplicate sends and concurrent state edits become possible. The app logs a warning at startup when `WEB_CONCURRENCY` is greater than 1.

| Area | What relies on a single process | What multi-worker needs |
|---|---|---|
| Per-customer message lock (`routes/webhook_routes.py _customer_lock`) | in-process `asyncio.Lock`; also guards live-vs-recovery replays | a DB lease or advisory lock keyed on (business, phone) |
| Webhook dedupe | the unique `messages.wa_message_id` is the real guard (database-level, already multi-worker safe); the `message_exists` pre-check is only an optimisation | nothing extra |
| Lost-reply recovery (`/webhook/recover-unanswered`) | "still unanswered" re-check happens under the in-process lock | same DB lease as above |
| Scheduled campaigns | claim-before-send is a **database** conditional update, so it is already safe across workers | nothing extra |
| Trial / notification emails (`crud/notification_ledger.py`) | with the `notification_log` table: database-atomic. Without the table (current fallback marker in `features_json`): safe only inside one process | apply `backend/migrations/notification_ledger.sql` |
| Immediate-campaign double-click guard (`routes/business_routes.py _RECENT_CAMPAIGNS`) | in-process, 60 s window | move into the database or a shared store |
| Background schedulers (monthly report, booking reminders) | daemon threads started in `main.py`; every worker would run its own, so reports and reminders would be sent once **per worker** | a single designated scheduler or a database claim per business and period |
| Caches (business lookup 5 s, analytics 60 s, QR, conversation analytics, pause-notice throttle, revoked-token memory) | per process; staleness windows are per worker | acceptable to keep per worker; revocation has a database table already |
| Rate limiters (`services/security.py`) | storage not audited; if in memory they are per instance | shared storage |
| Booking reminders flag, payment reminders | read-then-write flags (not atomic) | conditional update as claim |

Rule of thumb used in the code: a guarantee that must hold across restarts or deploy overlaps (campaigns, notification emails, payment claims, message dedupe) lives in the **database**; a convenience that only needs to hold inside the one process (locks, throttles, caches) lives in memory and is listed above.
