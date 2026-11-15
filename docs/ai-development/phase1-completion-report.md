# WaziBot Phase 1 Completion & Release-Readiness Report

Date: 2026-11-15 · Branch `phase1-stabilise` · Baseline `aa27072` · Phase 1 = commit `b09ea64` (user-made, also contains agent files/CLAUDE.md/docs) + uncommitted follow-ups.
Method: six read-only specialist reviews (backend, security, QA, database, AI/WhatsApp, frontend) + lead verification. Static/local only: no production access, no migrations, no sends, no deploys, no secrets read, nothing committed or pushed by the lead.

## 1. Executive summary
- **Phase 1 itself is safe to commit.** Backend review found **no unintended or whitespace-only change**; security review found **no weakening of any protection and no Critical issue introduced**; QA found **0 tests that passed at baseline now fail** (baseline 74 failed → now 44 failed; 30 fixed).
- **The branch is NOT release-ready.** 44 tests still fail because commit `506cbaa` (owner-confirmed intentional rollback) removed earlier fixes. Several removed guards are security-relevant. "Intentional" does not make them safe; each needs explicit owner sign-off or restoration (§6).
- Phase 1 introduced **one small self-inflicted defect**: `routes/auth_routes.py:386` (change-password cache pop) uses `except Exception: pass`, which trips the existing `test_no_silent_except_pass_left` scan (that test already failed at baseline for 4 other offenders). Fix is mechanical (`core.safelog.swallow`).

## 2. Test results (actually run)
| Run | Result |
|---|---|
| Full suite, 2 uncollectable modules excluded | **905 passed / 44 failed / 1 skipped** (46 s) |
| The 2 excluded modules (`test_billing_lifecycle.py`, `test_site_i18n_and_billing_messages.py`) | 0 collected, 2 collection errors (`_BILLING_EXT_COLS` removed by 506cbaa) → **billing lifecycle has no executing coverage** |
| Baseline `aa27072` same command | 74 failed |
| Pass-at-baseline / fail-now | **none** |
| `node cloudflare/test_worker.mjs` | 9 passed |
| Targeted Phase 1 tests (hygiene, webhook logging, auth/cron/reset/IDOR) | 43 passed |

## 3. Modified files and exact changes
### Already committed in `b09ea64` (Phase 1a)
| File | Change |
|---|---|
| `core/auth.py` | Auth-failure log no longer includes any part of the Authorization header (`header_present` boolean). |
| `core/crypto.py` | Removed key prefix/length and cipher/plaintext prefixes from logs and from the `TokenDecryptionError` text; fixed a mismatched log call. |
| `routes/auth_routes.py` | **New `POST /auth/change-password`** (JWT business only; superadmin/impersonation 403; bcrypt; `password_changed_at` bump; per-process cache pop; generic 500). |
| `routes/admin_routes.py` | `/debug/env`, `/debug/token`, `/debug/webhook` return set/unset instead of prefixes/tails; platform-stats 500 generic. |
| `routes/webhook_routes.py` | `_mask_phone` helper; STEP logs no longer print phone/text; verify-token via `hmac.compare_digest` (empty `VERIFY_TOKEN` now rejected); comment on inert directory branch. **Pipeline untouched.** |
| `routes_saas/billing_routes.py` | `_cron_secret_ok` (constant-time) on 3 cron routes; 503-unset/403-wrong unchanged. |
| `routes/password_reset_routes.py` | `_get_ip` → `services.security.get_client_ip` (last-hop XFF). |
| `routes/chat_routes.py`, `routes/growth_routes.py` | Generic 500 messages (cause logged). |
| `main.py` | Optional-router import failures logged at ERROR; real router count logged. No routing change. |
| `tests/test_phase1_hygiene.py`, `tests/test_phase1_webhook_logging.py` | New. |
### Uncommitted (Phase 1b), 11 files, +… as listed
| File | +/− | Change |
|---|---|---|
| `routes/auth_routes.py` | 10/6 | change-password: reject empty current/stored hash (400), rate-limit key includes business id, 128-char max, >72-byte new password rejected. |
| `routes/admin_routes.py` | 25/12 | Raw `str(exc)` removed from `/debug/supabase`, `/debug/verify-deploy`, `/debug/token`, `/debug/webhook` fields, notify result, cancel/refund 422s; `/debug/security` text corrected. |
| `routes/business_routes.py` | 10/4 | Generic errors: WhatsApp connection test, create/delete product, low-stock notify. |
| `routes/growth_routes.py` | 1/1 | Scheduled-run error field generic. |
| `routes_saas/billing_routes.py` | 1/1 | Cart-cleanup 500 generic. |
| `tests/test_phase1_hygiene.py` | +80 | New checks incl. `str(exc)` scan with per-file ceiling allow-list. |
| `tests/test_webhook_intent_engine.py`, `test_webhook_media_and_voice.py`, `test_security_audit_production_hardening.py`, `test_system_audit_fixes.py` | small | Stale-test fixes only: `ALLOW_UNSIGNED_WEBHOOKS=1` in fixtures; non-placeholder verify token; `CRON_SECRET` header for scheduled-run tests; renamed fail-open test + new fail-closed test; **one test skipped** (`..._skips_campaign_already_claimed`, claim step removed in 506cbaa). No assertion weakened. |
| `docs/ai-development/change-log.md` | +48 | Phase 1 record. |

## 4. Unintended or whitespace-only modifications
- **None found.** `git diff aa27072 --stat` and `-w --ignore-cr-at-eol --stat` are identical (17 backend files, +409/−75). Only LF→CRLF *warnings* appear (`core.autocrlf=true`); no EOL-only churn.
- Behaviour changes that are intended but worth stating in release notes: empty `VERIFY_TOKEN` now fails closed; password-reset IP is the last XFF hop (rate-limit keying depends on `TRUSTED_PROXY_DEPTH`); debug endpoint values changed from prefixes to set/unset; cancel/refund 422 text is now generic (specific reason lost); WhatsApp test failure text less detailed; password change invalidates the user's own session.
- Process note: `b09ea64` bundles app code, agent definitions, CLAUDE.md and docs in one commit (user-made). Not rewritten.

## 5. Password-change verification (backend + frontend + DB reviews)
Verified OK: body `{current_password,new_password}` matches `dashboard.js`; business id only from JWT; superadmin/impersonation refused; current password checked first; bcrypt; `owner_password` + `password_changed_at` written in one update and read by login/refresh/auth (column names consistent); rate limit 5/min per IP+business; generic 500; no secrets logged. Not exercised against a real DB (needs staging).
Open integration issues (UI/back-end, none breaks existing features):
1. **Silent logout:** after success the UI says "Password updated" but the next request 401s, refresh is rejected, user is sent to login with no explanation. *Decision:* force-logout deliberately in UI (recommended), or return fresh tokens.
2. **422 shown as `[object Object]`** (pydantic list detail) for >128-char input; `apiFetch` should normalise `detail` arrays.
3. `changePassword()` ignores a `null` return from `apiFetch` (latent false success); no client check for 128 chars/72 bytes.
4. **Stale refresh token gap:** `/auth/refresh` skips the pwd_ts check when either value is missing (`auth_routes.py:~422`), so a user who never changed password before keeps a working old refresh token after first change.
5. Cache invalidation is per-process (≤60 s on other workers); `_lookup_auth_state` fails open on DB error (pre-existing).
6. Direct-reset path `services/password_reset_service.py:~332` does not set `password_changed_at` (does not invalidate sessions) — pre-existing.
7. Dashboard requires an uppercase letter; backend does not (harmless).

## 6. Remaining security findings (verified against current code)
### HIGH (all pre-existing; consequences of the accepted 506cbaa/d17c930 rollback unless noted)
1. `POST /growth/send-weekly-reports` lacks superadmin check (`growth_routes.py:546`) — any tenant can trigger a platform-wide email run. *Failing test #13.*
2. `/chat/send` cross-tenant customer lookup on shared number (`chat_routes.py:~109-132`) — cross-tenant read/write. *4–5 failing tests.*
3. PayPal `/payments/paypal/success` (public) trusts client `reference`, no amount/currency check (`chat_routes.py:516-549`). *2 failing tests.* Exploit path is a hypothesis pending sandbox test.
4. Stripe webhook has no event-id dedupe; handler errors swallowed with 200 (`stripe_service.py:725-760`). *2 failing tests.* The ledger module is now dead code.
5. Store-page output escaping (`_wzEsc`, `_social_url`, buy-now arg escaping removed from `site_generator.py`) — likely stored XSS from product names; template review pending. *~11 failing tests.*
6. CSV import 10 MB cap and `features_json` size cap removed (`business_routes.py:~1536`) — DoS. *1 failing test.* (removed by `d17c930`)
### MEDIUM
7. Global 5xx sanitising exception handler removed (main.py) — any remaining `HTTPException(500, str(exc))` leaks; ~80 remain: tenant routes (`onboarding_routes` ×2 copies, `expansion_routes`, `marketing_routes` 503s, `ux_routes`), service-layer error strings, integrations/whatsapp `{"error": str(exc)}`, PayPal 200-bodies (`chat_routes.py:551,637`), Stripe `{"error": str(exc)}`, superadmin `saas_admin_routes` (low). Phase 1 test records these as a ceiling.
8. Logging still sensitive: `services/ai.py`/`ai_new.py` log message text and full phones (e.g. ai.py:491,494,575,1291,1345); full phones in `_ai_*`, `crud/*`, `growth/*`, `campaign_service`, `tenant_router`, `whatsapp_service`; webhook `_log_event` (wa.sent/wa.failed, step-6 event) writes full phone, undoing masking; webhook exception logs (`STEP n FAIL: %s`) may embed row data; emails logged in email/password-reset services (reset service logs which emails do not exist); PayPal order token/PayerID (`chat_routes.py:522`); `services/events.py` logs full order payload; raw XFF logged (`webhook_routes.py:91,776`).
9. WebSocket no longer rejects refresh tokens (506cbaa removed check).
10. `/healthz`, `maintenance_response`, `X-WZ-App` header, edge-IP trust (`EDGE_SHARED_SECRET`/`CF-Connecting-IP`) removed → maintenance page health check 404s; rate limits key on Cloudflare edge IP unless `TRUSTED_PROXY_DEPTH` is set correctly.
11. Legacy plaintext login branch remains; login no longer upgrades legacy passwords to bcrypt (506cbaa). Count of non-bcrypt rows unknown.
12. SEO: robots no longer disallows dashboard/admin; sitemap lacks `/signup` (undetermined if intended).
13. Rate-limit RPCs (`rl_hit/rl_peek/rl_clear`): SQL file added then deleted; if `RATE_LIMIT_BACKEND=supabase`, failures silently fall back to per-process limits.
### LOW
Per-process auth/pwd caches; `/debug/env`/`supabase` still show 30–50 chars of Supabase URL (superadmin); unpinned dependencies; `python-jose`; CORS defaults; upload MIME trust; `ilike` wildcards (incl. owner_email lookup `crud/businesses.py:69`).

## 7. The 44 remaining failing tests (individually triaged by QA; all also fail at baseline)
| Class | Tests (approx., ±1 per QA count note) | Notes / recommended resolution |
|---|---|---|
| Intentionally reverted feature (506cbaa/d17c930) | ~38 | Booking `_hm` (3, **functional risk: Postgres TIME breaks booking confirmation**); cloudflare edge/X-WZ-App (2); monthly-report unsubscribe (6, **CAN-SPAM/email job probably sends nothing**); `/healthz`+maintenance+global handler (4+1); shared-number lookup (5); Stripe idempotency (2); legacy-password upgrade on login (1); CSV cap (1); site_generator socials/overrides (4); buy-now/cart escaping (5+1, XSS); PayPal success guards (2); `business_id=None` guards (3). Resolution per item: restore code **or** owner-approved removal of the test — never loosen assertions. |
| Genuine regression | 2 | Manual-trigger superadmin gate (security, HIGH); `except: pass` sites — `auth_routes.py:386` (**ours**), `expansion_routes.py:265,282`, `translation_service.py:269,300`. |
| Stale test | 1–2 | PayPal webhook "dev mode unchanged" vs current fail-closed 503 (owner decides). |
| Unknown | 2 | SEO sitemap/robots. |
Missing tests for Phase 1: generic-error bodies for each edited endpoint, caplog check that causes are still logged, WhatsApp-test response shape, CSV cap, PayPal/Stripe guards, billing lifecycle (uncollectable).

## 8. Remaining message-loss & concurrency risks (AI/WhatsApp review; no webhook processing changed)
Confirmed in code: (1) save-before-reply → crash/restart after save makes Meta's retry a "duplicate", customer never answered; (2) send failure returns 200, outgoing row stored as `"sent"`, Meta never retries; (3) dedupe check-then-insert, safe only if a unique index on `messages.wa_message_id` exists (unverified) and `message_exists` failure disables dedupe; (4) blocking sync calls in `async def receive_message` (one slow LLM/Meta call stalls the worker → retries); (5) only first entry/change/message processed; (6) `_write_state_data` read-modify-write races, and a read error followed by write can wipe state; (7) transient DB errors in STEP 1/2/4 return 200 and permanently lose the message; (8) in-process `_msg_seen` is never called (do not wire it in — it would drop legitimate "yes"/"1"). `506cbaa` did not remove message-handling logic.
Suggested order for a later phase (needs approval): log hygiene → `run_in_threadpool` for blocking calls → iterate all messages → send-failure handling/status → unique index + claim-then-process → atomic state updates.

## 9. Database risks & information the OWNER must supply (read-only, no personal data)
Re-verified: schema not in repo (`migrations/*.sql` 0 bytes, `backend/sql/` empty); Stripe ledger module now only referenced by tests (dead); non-atomic stock + wildcard `ilike` matching; deletion backup gaps and unverified cascades; unbounded queries vs PostgREST `max-rows`; `get_business_by_id(None)` unguarded; `password_changed_at` write verified consistent (existence in live DB unverifiable but already required by auth).
### 9a. Update: owner supplied the table schema (received after the reviews; lead analysis)
The dump shows columns, defaults, primary/foreign keys and column-level UNIQUE/CHECK constraints. It does **not** show indexes (a `CREATE UNIQUE INDEX` would be invisible), RLS, FK `ON DELETE` rules, or functions/RPCs. All 31 tables the code uses via `.table()` exist in the schema (checked by grep); `payment_reminders` and `rate_limit_counters` are never referenced by live code.
**Resolved / downgraded**
- **H2 dedupe race is mitigated at DB level:** `messages.wa_message_id` is `UNIQUE`. The concurrent-duplicate race is safe if the code's unique-violation handling works. Still open: save-before-reply message loss, `chat_messages` write has no dedupe, a failed dedupe SELECT proceeds, and the string-match on the error text.
- `password_changed_at`, `payment_failed_at`, all `stripe_*`/billing columns, `is_service_business`, `sender_type`, `payment_url` etc. exist → change-password column writes are valid. `stripe_webhook_events` table exists (ledger code is just unused). `scheduled_campaigns.status` is `varchar` with no CHECK, so `'processing'` is allowed (claim step is feasible without DB change). No CHECK constraints at all appear.
- The billing columns (`stripe_price_id`, `billing_interval`, `current_period_*`, `cancel_at_period_end`, `stripe_last_invoice_id`, `billing_synced_at`…) exist in the live DB although the code that used them was rolled back → the DB is ahead of the code; restoring Stripe v1/v2 would not need schema changes.
**New or confirmed risks from the schema**
1. **No composite UNIQUE constraint shown on `customers(phone,business_id)`, `carts(phone,business_id)`, `user_memory(phone,business_id)`**, yet ~20 call sites upsert with `on_conflict="phone,business_id"` (customers.py, `_ai_state.py:62`, tenant_router, human_handoff…). Without a unique *constraint or index*, PostgREST upsert fails with 42P10 and the code's fallback (select-then-write) can create duplicate rows under concurrency. A unique *index* would not appear in this dump, so **this must be verified with the index query (item 2/15 below)** before concluding. If absent: HIGH (duplicate customers/carts/state; state races worse).
2. **Many tables have no FK to `businesses`:** `carts`, `user_memory`, `payment_reminders`, `scheduled_campaigns`, `bookings`, `ai_usage_log`, `handoff_log`, `campaign_log`, `security_events`, `account_deletions`, `admin_audit_logs`, notes/risk-flags, `subscription_events`. Deleting a business cannot cascade to them → orphaned tenant data after the 90-day purge unless the purge deletes each table explicitly (backup/purge covers only some tables — see M13). Tenant-data erasure/privacy gap.
3. FKs shown (products, orders, customers, messages, chat_messages, ratings, referrals…) have no visible `ON DELETE`; if `NO ACTION`, `delete_business` fails once a business has rows. Query FK rules (item 5).
4. **Money stored as floating point:** `products.price` and `orders.total_price` are `double precision` (rounding risk; PayPal/Stripe amount comparisons need tolerance). `bookings.price`, commissions, credits are `numeric`.
5. No unique on `orders.payment_reference` / `paypal_order_id` (duplicate attribution possible); `products.name` not unique per business (wildcard `ilike` match can hit the wrong product); no `CHECK (stock >= 0)`.
6. `messages.status` defaults `'sent'` with no CHECK, so a `'failed'` status needs no migration (supports the send-failure fix without schema change).
7. `rate_limit_counters(key PK, window_start, hits)` exists but `rl_hit/rl_peek/rl_clear` functions are not visible; RPC existence still unknown.
8. `businesses.owner_username` is a `USER-DEFINED` type (likely citext) — fine. `owner_password` is plain `text` (consistent with legacy plaintext risk).
**Owner checklist items now answered by the schema:** #1 (schema), #7 (columns), #8 (tables), #9 (campaign status/CHECKs). Still needed: #2–#6, #10–#16 below (indexes, RLS, FK rules, RPC definitions, counts, max-rows, instances, env checks).

Owner checklist (run in Supabase SQL editor; catalog/count queries only):
1. Schema-only export (`supabase db dump --schema-only`) — never data.
2. `select indexname, indexdef from pg_indexes where schemaname='public' and tablename='messages';` — unique index on `wa_message_id`?
3. Duplicate count: `select count(*) from (select wa_message_id from public.messages where wa_message_id is not null group by 1 having count(*)>1) d;`
4. RLS per table: `relrowsecurity`/`relforcerowsecurity`/policy count from `pg_class` + `pg_policies`.
5. FK delete rules from `pg_constraint` (`confdeltype`).
6. `rl_hit/rl_peek/rl_clear` definitions (`pg_proc`) and whether `RATE_LIMIT_BACKEND` is set (value only).
7. Column existence: `businesses(owner_password,password_changed_at,is_active,features_json,subscription_tier,billing_status,trial_ends_at,trial_started_at,payment_failed_at,owner_email,owner_username)`, `messages.wa_message_id`, `products(stock,name,business_id)`; confirm `features_json` is jsonb.
8. Which of `stripe_webhook_events`, `subscription_events`, `rate_limits` tables exist.
9. CHECK constraints mentioning `status` and current campaign status values (is `'processing'` allowed? confirm campaigns table name).
10. Non-bcrypt password counts: `count(*) filter (where owner_password !~ '^\$2[abxy]\$')`, empty count, total.
11. `select count(*) from public.products where stock < 0;`
12. Approximate row counts (`pg_stat_user_tables.n_live_tup`).
13. PostgREST `max-rows` (Supabase dashboard → API settings, or `show pgrst.db_max_rows;`).
14. Render instance/worker count and `TRUSTED_PROXY_DEPTH` value; whether Cloudflare is live in front.
15. Index definitions for tenant tables (`businesses, orders, customers, products, conversations`).
16. Manual check (values not to be shared): `CRON_SECRET`, `VERIFY_TOKEN` (non-placeholder), `WHATSAPP_APP_SECRET`, `PAYMENT_WEBHOOK_SECRET`, `PAYPAL_WEBHOOK_ID`, `STRIPE_WEBHOOK_SECRET` are set in production; installed `stripe` SDK version.

## 10. Frontend impact
No existing dashboard functionality is broken by Phase 1 backend changes: no static code calls a removed/renamed route; generic-error endpoints are consumed only by `/me/test-whatsapp` (still renders `reason`) and generic `e.detail` display; `/debug/*` are not used by the UI. Corrections to earlier notes: `/healthz` (used by `maintenance.html`) is **absent** from app code (removed by 506cbaa), so that health check 404s; `/solutions*` pages still missing but listed in sitemap. Password-change UI issues are in §5.

## 11. Recommended Git commit grouping (nothing committed by the lead)
Already committed: `b09ea64` (mixed). Do not amend/rewrite without approval. For the uncommitted work:
1. `fix(auth): harden change-password and stop leaking raw errors` — `routes/auth_routes.py`, `routes/admin_routes.py`, `routes/business_routes.py`, `routes/growth_routes.py`, `routes_saas/billing_routes.py`, `tests/test_phase1_hygiene.py`.
2. `test: fix stale webhook/cron tests for fail-closed behaviour` — the 4 modified test files (note the single documented skip).
3. `docs: phase 1 change-log, completion report` — `docs/ai-development/*`.
Suggested pre-commit micro-fix (needs your OK): replace `except Exception: pass` at `auth_routes.py:386` with `core.safelog.swallow` (our one regression).
Do not commit: scratch files (none in repo), `__pycache__`.

## 12. Deployment readiness assessment
| Question | Verdict |
|---|---|
| Is Phase 1 safe to commit? | **Yes** (after the one-line `swallow` fix, ideally). |
| Is Phase 1 safe to deploy on its own? | **Yes, low risk** — logging/error-text/hardening plus one additive route; no schema/contract break. Needs a staging smoke test of `/auth/change-password` (never run against a real DB) and confirmation `TRUSTED_PROXY_DEPTH` suits Render+Cloudflare (password-reset IP keying changed). |
| Is the branch release-ready as a whole? | **No.** Open High risks from the rollback: weekly-report trigger gate, shared-number cross-tenant lookup, PayPal success guards, Stripe dedupe, store-page escaping, CSV cap; booking confirmation for Postgres TIME values; report emails and unsubscribe link; no `/healthz`/maintenance page; billing has no executing test coverage. |
| Blockers that need an owner decision | (a) Was each security guard's removal acceptable? Recommend restoring at least items 1–6 in §6, via small per-item changes with tests (Stripe/PayPal changes need separate approval per CLAUDE.md). (b) Keep skip on claimed-campaign test or restore claim step (double-send risk). (c) Forced-logout vs fresh tokens after password change. (d) Direct-reset path `password_changed_at`. (e) Cancel/refund 422 detail. (f) Is `_get_ip` last-hop wanted. (g) SEO robots/sitemap intent. (h) PayPal dev-mode 503. |
| Phase 0 | Owner facts in §9 still outstanding; database work (unique index, atomic stock, RLS) cannot start without them. |

## 13. Suggested next steps (none started; awaiting approval)
1. Approve the `swallow` micro-fix and the three-commit grouping.
2. Decide the owner questions in §12.
3. Approve a "Phase 1c — restore security guards" item list (superadmin gate; shared-number lookup; global 5xx handler; CSV caps; `_wzEsc`/site_generator escaping; booking `_hm`; report unsubscribe + import fix; `None` guards; WebSocket refresh rejection), each with its test; Stripe/PayPal items separately approved.
4. Supply §9 facts so Phase 2 (database) and Phase 3 (webhook reliability) can be planned safely.
