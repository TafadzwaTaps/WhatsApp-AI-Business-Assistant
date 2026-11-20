# WaziBot architecture map (2026-11-20, after Phase 7 stage 3)

Written from the repository as it stands at commit `fddcbf7`. Every path below exists; behaviour claims were read from the code. Items marked **(unverified)** were not checked against the live deployment (Render, Supabase, Meta, Stripe), which this workspace cannot see.

## 1. Shape of the system
- **Backend:** FastAPI app in `backend/main.py` (about 900 lines of wiring: CORS, routers, security headers, two scheduler threads, startup checks). One web process, one worker on Render (`SCALING_NOTES.md`).
- **Database:** Supabase/PostgreSQL through the service-role client in `backend/core/db.py`. Tenant scoping is done **in application code** (`business_id` on every query); row-level security does not protect the app's own queries.
- **WhatsApp:** Meta Cloud API. Inbound webhook `POST /webhook` (`routes/webhook_routes.py`); outbound via `main.send_whatsapp` and template sends (`services/wa_templates.py`). Graph version comes from `core/wa_version.py` (`WA_GRAPH_VERSION`, default `v18.0`).
- **Frontend:** static HTML/CSS/vanilla JS in `static/` (dashboard, inbox, storefront, marketing pages), served by the same app; some pages sit behind a Cloudflare worker (`cloudflare/`).
- **AI:** a deterministic, priority-ordered engine in `services/ai.py` plus helper modules (below), with optional LLM phrasing (`services/llm_response.py`). `ai.py` is deliberately one large module and must not be split without approval.

## 2. Request flow for a customer message
1. `POST /webhook` verifies the Meta signature (`WHATSAPP_APP_SECRET`) and splits the payload (`_split_webhook_payload`).
2. `_handle_one_message` resolves the business and customer, de-duplicates on the unique `messages.wa_message_id`, takes the per-customer in-process lock, stores the incoming row.
3. Consent keywords (STOP/START, only when the consent migration is applied) are handled by `services/wa_consent.py` before the AI.
4. `services/ai.py: generate_reply` → expiry check (`_ai_lifecycle.expire_stale`) → `_generate_reply_core` (priority list below) → handoff checks.
5. The reply is sent (free text inside the 24-hour window, approved template outside it where one is registered) and stored.

### AI priority order (`services/ai.py`, header comment is authoritative)
Agent-echo suppression → human handoff → prompt-injection defence → handoff triggers → survey → **P0 global cancel** (with confirmation state `awaiting_cancel_confirm`) → **P0.5 refund/dispute** → **P0.55 order-status inquiry** → abuse/completion/urgency → order preview, booking and fulfilment states → payment states (proof, payment, **price_changed_confirm**, confirm_order, checkout) → intro, booking, reorder, cart edits, sales, add-to-cart, menu, status, business Q&A, fallback.

### AI support modules
`_ai_state` (state in `carts.state_data`), `_ai_memory`, `_ai_intent`, `_ai_products`, `_ai_payments`, `_ai_lifecycle` (expiry, repricing, cancellation, order-status reply), `conversation_service` (state constants and transitions: new states must be added to `STATE.ALL`), `intent_engine`, `model_routing`, `handoff_triggers`, `prompt_injection_guard`. `services/ai_new.py` is not imported (kept, referenced by a test).

## 3. Where things live
| Area | Files |
|---|---|
| Auth, sessions, roles | `core/auth.py`, `core/cookie_auth.py` (flag off by default), `routes/auth_routes.py`, `routes/password_reset_routes.py`, `services/password_policy.py`, `services/email_verification_service.py` |
| Tenant, plans, restrictions | `core/plan_guard.py`, `routes_saas/tenant_features.py`, `core/platform_controls.py` |
| Orders, stock | `workflows/order_lifecycle.py` (status transitions, atomic stock), `services/inventory.py`, `services/stock_restore.py` |
| Payments | `services/payment_service.py`, `services/_ai_payments.py`, `routes/chat_routes.py` (PayPal), `billing/stripe_service.py` + `routes_saas/billing_routes.py` (subscriptions) |
| WhatsApp compliance | `services/wa_window.py` (24 h window), `wa_consent.py`, `wa_templates.py`, `wa_outbound.py`, `routes/wa_compliance_routes.py` |
| Campaigns and growth | `services/campaign_service.py`, `routes/growth_routes.py` (scheduled campaigns), `growth/cart_recovery.py`, `growth/reengagement.py` |
| Bookings | `services/booking_service.py`, `routes/booking_routes.py`, `routes/expansion_routes.py` |
| Reporting | `crud/analytics.py`, `crud/orders.py` (dashboard stats), `crud/_helpers.py` (`counts_as_revenue`, `fetch_all`), `services/weekly_report_service.py` (monthly email) |
| Jobs and run log | `services/job_runs.py`, `crud/notification_ledger.py` (once-only claims) |
| Regional | `services/phone_utils.py`, `address_utils.py`, `pricing_display.py` (display only) |
| Admin (superadmin) | `routes/saas_admin_routes.py`, `routes/admin_routes.py` |
| Duplicate/unused | `crud/crud.py` (not the live package export; `crud/__init__.py` exports the split modules), root `inventory.py`, `services/ai_new.py`. Nothing deleted. Note `routes_saas/*` copies are **live** where mounted. |

## 4. Background work
Two daemon threads start at import (`main.py`): monthly report (1st of month 08:00 UTC; the nearby comment in `main.py` still says Monday and is stale) and booking reminders (every 30 minutes). Everything else is triggered by cron secret endpoints. Each job's once-only guarantee is described in `OPERATIONS_RUNBOOK.md`.

## 5. Data model notes
- Order status flow and allowed transitions: `workflows/order_lifecycle.py` (`VALID_TRANSITIONS`); cancelling restores stock once.
- Revenue everywhere = paid and not cancelled/refunded (`counts_as_revenue`).
- Conversation state: `carts.state_data` JSON (`state`, `session`, `pending_payment`, `pending_proof`, `last_activity`).
- Settings that avoid schema changes live in `businesses.features_json` (country, timezone, `default_dialing_code`, profile fields, notification markers).
- Migrations are plain SQL in `backend/migrations/`, run manually in Supabase (see runbook).

## 6. Tests
`backend/tests/` (about 130 files), run from the repo root with `python -m pytest backend/tests -p no:cacheprovider` (config in `backend/pytest.ini`). Tests use in-memory fake databases and mocked HTTP; nothing contacts Meta, Stripe, Supabase or a real WhatsApp number. Conventions: sibling-import helpers (`_routes_util.py`), monkeypatch only, source-inspection tests for wiring that cannot be exercised cheaply, a scratch mutation check before accepting a safety test. `node` is used for two JS harness tests.

## 7. Known limits (see also `SCALING_NOTES.md`, `PHASE_6_REPORT.md`)
Single worker assumed; in-process locks, caches and rate limiters; cart items carry no product id or currency (repricing matches by name); dashboard day buckets use UTC; platform totals add across currencies; template approval status is owner-attested, not read from Meta.
