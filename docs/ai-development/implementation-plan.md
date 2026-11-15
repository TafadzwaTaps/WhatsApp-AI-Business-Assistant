# WaziBot Implementation Plan (Roadmap derived from the Phase 1 audit)

Date: 2026-11-15 · Source: `architecture-review.md` (finding IDs H#/M# refer to it).

## APPROVAL STATUS: **NOT APPROVED — nothing in this plan is authorised.**
No application code, tests, migrations, dependencies or configuration may be changed until the user approves a specific phase/item. Each item below must still get its own mini-plan (problem, files, ownership, compatibility, security, tests, rollback) before implementation, per CLAUDE.md Phase 2/3.

Standing constraints (apply to every phase): extend don't rewrite; no file deletes/renames/moves; do not split `services/ai.py`; no dependency upgrades or new infrastructure; no production DB access, deploys, real WhatsApp sends or Meta/Render/Supabase setting changes without explicit approval; no secrets in docs or commits; keep `marketing_router` before `business_router`; preserve API response shapes.

Complexity key: **S** ≤ 1 day · **M** 2–4 days · **L** 1–2 weeks · **XL** > 2 weeks (single engineer, incl. tests).

---

## Phase 0 — Owner confirmations & safety net (no code changes)
Purpose: remove the unknowns that change how later phases are done.

| # | Item | Owner | Size |
|---|---|---|---|
| 0.1 | Owner supplies redacted answers/exports for architecture-review §9 (schema-only dump, unique index on `messages.wa_message_id`, RLS status, FK cascades, `max-rows`, instance count, `TRUSTED_PROXY_DEPTH`, Stripe SDK version, non-bcrypt password row **count**) | User + database-engineer (review) | S |
| 0.2 | Decide product intent: `payment_status="paid"` on lifecycle push (H15); `/auth/change-password` (H14); keep or retire dead duplicates | User | S |
| 0.3 | Take a Supabase backup/point-in-time marker before any DB-side work | User | S |

Gate: Phase 1 DB items cannot start until 0.1 answers exist.

## Phase 1 — Stabilise & stop the bleeding (low-risk, high-value)
Goal: reliable tests, no silent data/message loss, close easy security wins.

| # | Item (finding) | Owner agent(s) | Files touched (exclusive) | Size | Risk |
|---|---|---|---|---|---|
| 1.1 | **Triage the 71 failing tests + 2 uncollectable billing modules** one by one: classify stale-test vs real regression (esp. `None business_id` DB query, `_BILLING_EXT_COLS`, `run_scheduled_campaigns(request)`, `maintenance_response`, `_report_unsub_token`, `_social_url`). Fix stale tests (sign webhook payloads with a test secret); raise real regressions as separate bug items. (H13, M19) | qa-engineer (triage, read-only) → backend-engineer (edits tests/code) | `backend/tests/*` first; app code only for confirmed bugs | M | Low; must not weaken assertions to go green |
| 1.2 | **Wire Stripe idempotency + proper failure status**: call `claim_event/mark_processed`, return 5xx on handler failure (M2) | backend-engineer | `billing/stripe_service.py`, `crud/stripe_webhook_events.py` | S–M | Medium (billing path) — needs billing tests from 1.1 first |
| 1.3 | **Add missing `/auth/change-password`** (bcrypt, require current password, bump `pwd_ts`) or hide button — per 0.2 (H14) | backend-engineer (+ frontend if hiding) | `routes/auth_routes.py` (+ tests) | S | Low |
| 1.4 | **Log hygiene**: stop logging key prefixes, Authorization header, customer phone/message text (mask) (M6, M7) | backend-engineer / ai-whatsapp-engineer | `core/crypto.py`, `core/auth.py`, `webhook_routes.py`, `admin_routes.py` | S | Low |
| 1.5 | **PayPal return-URL hardening**: ignore client `reference`; verify amount/currency/business before marking paid; generic errors (M1) | backend-engineer | `routes/chat_routes.py` | S | Medium (payments) |
| 1.6 | **Password hardening, no behaviour break**: rehash-on-login for non-bcrypt; confirm reset path hashes; count-only audit (H9) | backend-engineer + security-engineer review | `core/auth.py`, `services/password_reset_service.py` | S–M | Medium (login path) |
| 1.7 | **Small safe fixes**: constant-time CRON/verify-token compare; `get_client_ip` everywhere; remove `str(exc)` in 500s; fix dead `/businesses` directory import or neutralise the command safely (H17); correct hard-coded "8 modules" log; surface router import failures loudly at startup (M10) | backend-engineer / ai-whatsapp-engineer | specific lines only | S | Low |
| 1.8 | **Route-auth inventory test + twin-guard test** (the missing `test_module_twins.py`): fail on any new unauthenticated route not on an allow-list; fail if dead duplicates are imported | qa-engineer (spec) → backend-engineer | `backend/tests/` (new files) | S–M | Low |

Exit criteria: suite fully green (or each remaining failure documented), billing suites collect and pass, security items 1.2–1.7 reviewed by security-engineer.

## Phase 2 — Data integrity & tenant safety (needs Phase 0 answers + approval for each DB change)
All SQL is **proposed text only**; applied by the owner in a controlled environment (staging first), with rollback written in advance. Application code is written to work both before and after the DB change.

| # | Item | Owner | Size | Depends on |
|---|---|---|---|---|
| 2.1 | **Commit the real schema to the repo** (schema-only export → `backend/migrations/`; restore `billing_lifecycle_hardening.sql` content) (H1) | database-engineer (review) + user (export) | S–M | 0.1 |
| 2.2 | **Unique index `messages(wa_message_id)`** (pre-check duplicates, `CONCURRENTLY`, rollback = drop) (H2) | database-engineer proposes; user applies | S | 0.1, 0.3 |
| 2.3 | **Atomic stock**: `reduce_stock_atomic` RPC + switch `inventory.py`; later `create_order_atomic` RPC for whole cart; optional `CHECK (stock>=0) NOT VALID` (H6) | database-engineer (SQL) → backend-engineer (callers) | M–L | 2.1, tests from 3.x P0 |
| 2.4 | **Payment confirmation guard** (`WHERE payment_status <> 'paid'`, check affected rows), idempotent memory sync (DB M5/M3) | backend-engineer | S | 1.5 |
| 2.5 | **Defence-in-depth scoping**: add `.eq("business_id", …)` to id-only updates/reads (`order_lifecycle`, `crud/messages.py`, `reduce_stock`); audit every `get_order` caller; escape `ilike` wildcards or switch to exact/ID match (M3, M14) | backend-engineer; security-engineer reviews | M | 1.8 |
| 2.6 | **Indexes** (introspect first; add only missing): orders(business_id, created_at), messages(customer_id, created_at), customers(business_id, last_seen), partial scheduled_campaigns pending, products(business_id, lower(name)) | database-engineer proposes; user applies | S–M | 0.1 |
| 2.7 | **Pagination + SQL-side aggregation** for orders/customers/conversations/dashboard stats; keep default response shape, add `limit/offset` params with safe defaults (H12) | backend-engineer; frontend adapts | M–L | none (frontend change coordinated, 4.x) |
| 2.8 | **Account deletion/export completeness**: back up `messages`, `carts`, `user_memory`, `bookings`, `ratings`, logs; fail on truncation/read errors; verify FK cascade first (M13) | backend-engineer + database-engineer | M | 0.1 |
| 2.9 | **RLS default-deny** on tenant tables (service_role unaffected) once confirmed browser never uses anon key | database-engineer proposes; user applies | S | 0.1 |

Exit criteria: duplicates impossible at DB level; no oversell under concurrency test; schema reproducible from repo.

## Phase 3 — WhatsApp/AI reliability (core of the "AI employee")
Order matters; 3.1–3.3 change the webhook contract and need an explicit design review and owner decision (see risks).

| # | Item | Owner | Size | Depends on |
|---|---|---|---|---|
| 3.1 | **Iterate all `entries/changes/messages`** in a payload (H3) | ai-whatsapp-engineer | S | 2.2 recommended |
| 3.2 | **Insert-first atomic dedupe**; save incoming message with a processing marker so a crash doesn't lose the reply on retry (H2) | ai-whatsapp-engineer + database-engineer | M | 2.2 |
| 3.3 | **Take blocking work off the event loop** (`run_in_threadpool` first — smallest change; BackgroundTask only if owner accepts "ack first, process later") with **per-phone serialisation** to fix state races (H4, H5) | ai-whatsapp-engineer | M–L | 3.2 |
| 3.4 | **Atomic/versioned state writes** for `carts.state_data`, quota counters (H5) | ai-whatsapp-engineer + database-engineer (RPC) | M | 2.1 |
| 3.5 | **Delivery visibility**: handle inbound `statuses` events; mark failed sends in inbox; add retry outbox table (needs DB approval) (H7) | ai-whatsapp-engineer + backend + frontend | L | 2.1 |
| 3.6 | **Never silently drop**: tenant-resolution errors and empty-token cases log structured errors and notify platform admin; do not reply "sent" on failure (H7) | ai-whatsapp-engineer | S–M | none |
| 3.7 | **Centralise Graph API version constant** (default stays v18.0; no bump) and route all senders through one wrapper honouring pause/retry/fallback (M8, H8) | ai-whatsapp-engineer | M | none; **version bump is a separate approval** |
| 3.8 | **24h-window check + template sender** for cart recovery, reminders, re-engagement, campaigns. Requires owner to create/approve templates in Meta (not done by us) (H8) | ai-whatsapp-engineer + backend | L | 3.7 |
| 3.9 | Per-phone inbound rate limit; shared-number selection TTL; fix `is_from_agent`; keep user text when picking via START (M9, M15, M16) | ai-whatsapp-engineer | M | 3.3 |
| 3.10 | Reduce per-message DB calls: per-request product/config cache, dedupe `_get_state/_get_session` reads (perf) | ai-whatsapp-engineer | M | 3.4 |

Exit criteria: duplicate/batch/retry/concurrency tests pass with mocks; no code path returns 200 without either replying or recording why.

## Phase 4 — Frontend security, globalisation, performance
Frontend-engineer owns `static/*`; backend contract changes go through the lead.

| # | Item | Size | Depends on |
|---|---|---|---|
| 4.1 | **Single shared escape helper (incl. `'`)**; replace inline-handler args with `data-*` + delegated listeners at known-unsafe sites (`dashboard.js:2056,1329,6640`, `inbox.js:1636-1645`); full sweep of the 174+23 `innerHTML` sinks for customer text (H10, H11) | M–L | none |
| 4.2 | Globalise defaults: neutral phone/address placeholders, country-driven timezone/currency, payment-readiness check counts all methods, region-aware payment tabs, neutral testimonials (config-driven, no redesign) (H16) | M | `/config/public` (existing) |
| 4.3 | Use `location.origin` for API/WS; move WS token off query string (needs backend support) (M18) | S–M | backend support for WS auth change |
| 4.4 | Pause 15 s polling when tab hidden; fix duplicate heartbeat timers; consume pagination from 2.7 | S–M | 2.7 |
| 4.5 | Fix/remove sitemap entries for missing `/solutions*` pages (or add pages — product decision) | S | 0.2 |
| 4.6 | Accessibility basics: keyboard-operable rows, focus-visible, aria labels, reduced-motion; mobile breakpoints audit | M | none |
| 4.7 | Longer-term: refresh token to HttpOnly cookie; remove CSP `unsafe-inline` (needs handler refactor, backend contract change, **separate impact analysis & approval**) | XL | 4.1 |
| 4.8 | Minimal JS test harness for `escHtml`, payment readiness (no new deps beyond existing node usage) | S | 4.1 |

## Phase 5 — AI employee experience upgrades (after Phases 1–3 are stable)

| # | Item | Size | Depends on |
|---|---|---|---|
| 5.1 | Customer profile memory (language, preferred items, address) + "same as last time" reorder | M | 3.4 |
| 5.2 | Abandoned-state expiry (checkout/booking) with polite follow-up | M | 3.8 |
| 5.3 | WhatsApp location messages → delivery address capture | S–M | 3.1 |
| 5.4 | Handoff quality: merge duplicate handoff routes, summary + SLA timers, AI draft suggestion, "return to AI" undo | M–L | 4.1 |
| 5.5 | Owner "AI activity feed" + AI-resolution/handoff-reason analytics + daily digest (reuse weekly report service) | M–L | 2.7 |
| 5.6 | Per-tenant AI persona/tone/hours/escalation policy via `tenant_features` | M | none |
| 5.7 | Onboarding "test your WhatsApp connection" checklist (uses `/me/test-whatsapp`) | S | none |
| 5.8 | Review/tune intent-engine low-confidence intercept using real `intent.classified` logs; prompt-injection guard false-positive review | S–M | log hygiene 1.4 |

## Phase 6 — Maintainability & hardening (only with explicit approvals)
- Dependency pinning + lockfile + `pip-audit` in CI; `python-jose`→PyJWT evaluation; confirm Stripe SDK ≥ required (**dependency changes need approval**).
- Retire dead duplicates (`crud/crud.py`, `routes_saas` duplicates, `routes/tenant_router.py`, `services/ai_new.py`, `tests/seo_routes.py`, tracked empty `app.log`) — **only if the user approves deletions**; otherwise add banner comments / guard tests.
- Scheduler single-runner safeguard (advisory lock or leader flag) before scaling beyond one instance.
- Cache `/api/directory` and heavy analytics; serial campaign sends → background job with progress (no new infra such as Celery/Redis unless approved; use DB-backed queue/cron).
- JWT hardening (`iss/aud`, `datetime.now(UTC)`), upload magic-byte check, field whitelist for admin business update, impersonation guard rails, Pydantic v2 migration plan.
- Frontend asset versioning / cache headers.

---

## Dependency graph (critical path)

```
0.1 owner facts ─┬─> 2.1 schema in repo ─┬─> 2.3 atomic stock ──┐
                 ├─> 2.2 unique wa_id ───┼─> 3.2 atomic dedupe ─> 3.3 off-loop + per-phone lock ─> 3.9, 3.10
                 ├─> 2.6 indexes         └─> 3.4 atomic state ──> 5.1 memory
                 └─> 2.9 RLS
1.1 test triage ─> 1.2 Stripe ─> (billing safe to touch)
1.1 ─> 1.8 route/twin guards ─> 2.5 scoping sweep
1.5 PayPal ─> 2.4 payment guard
3.7 sender wrapper ─> 3.8 templates ─> 5.2 follow-ups
2.7 pagination ─> 4.4 polling / 5.5 activity feed
4.1 escaping ─> 4.7 CSP/cookie ; 4.1 ─> 5.4 handoff UI
```
Parallelisable (disjoint files): Phase 1 backend items vs Phase 4.1–4.2 frontend items; DB review (database-engineer, read-only) alongside either.
Must be sequential: 2.2 → 3.2 → 3.3; 3.7 → 3.8; 1.1 → 1.2.

## Agent ownership rules for implementation
- backend-engineer: `routes/*`, `crud/*`, `core/*`, `billing/*`, backend `tests/`.
- ai-whatsapp-engineer: `routes/webhook_routes.py`, `services/ai*.py`, `_ai_*`, conversation/intent/translation services, `integrations/`, `growth/` senders.
- frontend-engineer: `static/*`.
- database-engineer: proposes SQL/RPC text only; owner applies.
- security-engineer & qa-engineer: independent reviewers of every phase; QA runs only local mocked tests.
- Shared files (e.g. `main.py`, `webhook_routes.py` when both backend and AI items touch it): lead sequences; no concurrent edits.

## Key risks of the roadmap
1. **Webhook ack semantics (3.3):** acknowledging first then processing in background means a process crash loses work unless 3.2/3.5 (marker/outbox) land first. Recommended: threadpool + per-phone lock first; background-ack only after outbox exists.
2. **DB changes without a staging copy (Phase 2):** unique index creation fails if duplicates exist; check first; apply on staging.
3. **Test-fixing could hide regressions (1.1):** every red test is triaged individually; the `None business_id` failure is treated as a suspected real bug.
4. **Fake DB ignores filters:** isolation fixes (2.5) need a recording fake or targeted assertions (QA P2 idea) to be provable.
5. **Template messages (3.8)** require Meta-side template approval — outside our control, may delay.
6. **Pagination (2.7)** changes payload size/shape expectations of the dashboard; add params with backwards-compatible defaults and update frontend in the same release.
7. **Auth changes (1.6, 4.7)** can lock users out; ship with rollback flag and test accounts only.

## Rollback approach (generic)
Per-item small commits on a feature branch; no deploy without approval; DB changes shipped as forward + rollback SQL pairs; app code written to tolerate both pre- and post-migration schema; feature flags/env toggles for behaviour changes (e.g. async webhook path), reverting = revert commit/flag.

## Suggested approval order
Approve **Phase 0** (no code) now → then **Phase 1** (items 1.1, 1.3, 1.4, 1.7 are the safest starting set) → Phase 2/3 after owner answers → Phase 4 in parallel with Phase 1/2 → Phase 5 → Phase 6 selectively.
