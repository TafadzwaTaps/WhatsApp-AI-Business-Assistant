# Phase 1 follow-up — baseline and plan (2026-11-17)

Status: **PLAN ONLY — awaiting approval. No application code changed.**
Repo: branch `ui-enhancement`, HEAD `e94f739`, working tree clean.

## 1. Current test baseline (measured today)
`pytest` (backend): **958 passed, 11 failed, 1 skipped**, plus **2 modules that cannot be collected**
(`tests/test_billing_lifecycle.py`, `tests/test_site_i18n_and_billing_messages.py`).
Historical figure for comparison: 905 passed / 44 failed (original audit) -> 950 / 11 after Phase 1/1c.

## 2. Triage of every remaining failure
| Group | Tests | Root cause (verified) | Genuine bug? | Proposed fix |
|---|---|---|---|---|
| Stripe billing lifecycle | 2 uncollectable modules | Commit `506cbaa` removed ~1,066 lines of `billing/stripe_service.py` (cancel/resume/change_plan/portal/invoices, `_BILLING_EXT_COLS`, price verification). Tests import them. | **Yes** (feature regression) | Restore from `d640abc` by hunk, as in Phase 1c. **Needs your payment-logic approval.** |
| Stripe webhook dedupe | 2 | Same revert removed event-id idempotency; webhook returns 503. | **Yes** (duplicate events can be processed twice) | Restore with the above. **Needs payment approval.** |
| PayPal | 3 | Same revert removed webhook-id verification, amount-mismatch rejection and order-lookup hardening. | **Yes** (security) | Restore from history. **Needs payment approval.** |
| Monthly report unsubscribe | 3 | Per-business unsubscribe tokens replaced by another scheme; tests describe the old one. | Unclear | Your decision: restore old scheme, or rewrite tests to the current scheme (I recommend verifying the current one is safe, then rewriting tests). |
| SEO routes | 2 | Tests target unregistered `routes/seo_routes.py`; live `main.py` robots/sitemap are correct. | No (stale tests) | Re-point tests at the live endpoints; keep file for Phase 9 removal proposal. |
| Silent `except: pass` | 1 | 4 pre-existing sites: `routes/expansion_routes.py:265,282`, `services/translation_service.py:269,300`. | Minor | Replace `pass` with `log.debug/warning`. No behaviour change. |

## 3. Security follow-up status
Already done: central handler hides raw 5xx detail (`main.py:182`), password change hardened, 11 guards restored, fail-closed webhook.
**Still leaking** (verified by grep):
- `routes/marketing_routes.py:178,199` — `503` with `str(exc)` (the handler deliberately keeps 503 detail).
- `routes/chat_routes.py:592,678` — returns `{"status":"error","detail": str(exc)}` as a 200 body.
- `routes/ux_routes.py:518-548` — health-check messages include raw exception text.
- Services returning `{"error": str(exc)}` that routes pass on: `payment_service`, `_ai_payments`, `booking_service`, `growth_service`, `marketing_service`, `billing/stripe_service`.
Plan: log server-side, return fixed generic text; add a regression test that scans routes for `str(exc)` in client-visible responses.
Also unverified and to be checked in this phase: session invalidation after password change on WebSocket/refresh paths, and rate-limit scoping on login/change-password.

## 4. Proposed order of work (Phase 1 only)
1. **Safe fixes (no approval beyond this plan):** silent excepts, SEO test re-pointing, remaining raw-exception leaks, regression tests. Files: `routes/expansion_routes.py`, `services/translation_service.py`, `routes/marketing_routes.py`, `routes/chat_routes.py`, `routes/ux_routes.py`, `tests/test_seo_routes.py`, one new test.
2. **Payment restore (needs your explicit go-ahead):** restore Stripe lifecycle + idempotency and PayPal hardening from history, then run the 2 uncollectable modules and the 5 payment tests. Rollback = revert the commit.
3. **Unsubscribe:** your decision (see table).
4. Security + QA agents review; update `change-log.md`; report baseline -> final.

Risks: payment restore is the largest change (~1k lines) and touches live billing; I would restore only hunks the tests and history show were removed, and run Stripe in test mode only. No deploy, no commit, no production data.

## 5. Later phases (unchanged, in your order)
2 Database integrity + WhatsApp reliability -> 3 Security + frontend -> 4 AI employee -> 5 Globalisation + performance.
Phase 2 needs read-only catalog output from you (indexes on `messages.wa_message_id`, RLS state) or a staging database; I cannot run it against production.
