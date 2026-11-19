# PHASE 4B — dependency security, email verification, cookie-auth design, follow-ups (2026-11-19)

Status: **implemented and tested locally; NOT committed, NOT deployed; the database migration is NOT applied.** Until `email_verification.sql` is applied, email verification is OFF and the application behaves exactly as before.

## 1. Dependency security updates
**Method:** built a clean virtual environment from `requirements.txt` the way Render would, ran `pip-audit` (2.10.1) on the resulting set, then on the proposed set, and ran the whole backend suite in it.

Findings that changed the plan:
- A fresh install already resolves to much newer packages than a developer machine has (fastapi 0.165, starlette 1.10, stripe 17.1, pydantic 2.14, bcrypt 5.0, cryptography 50.0, requests 2.34, python-multipart 0.0.34). Nothing is pinned today, so **production likely already runs a set like this**, and the suite passes on it.
- Current advisories name higher fixed versions than the three you listed: python-multipart fixed in **0.0.31** (PYSEC-2026-3036..3040), requests fixed in **2.33.0** (PYSEC-2026-2275). `python-jose` 3.5.0 is the newest release and has **no fixed version** for CVE-2026-85394 (needs an asymmetric key used as an HS256 secret, which WaziBot does not do: symmetric key, `algorithms=["HS256"]` pinned) or the `ecdsa` Minerva note (ECDSA signing, unused).

| File | Change |
|---|---|
| `backend/requirements.txt` | `python-jose[cryptography]>=3.4.0` (was 3.3.0), `python-multipart>=0.0.31` (was 0.0.9), `requests>=2.33.0` (was 2.31.0), with a comment citing the advisories. Nothing else changed |
| `backend/requirements.lock` (new) | Exact pinned set (64 packages) that passed the full suite and the audit. **Not used by the deploy** until the Render build command is changed to `pip install -r requirements.lock` |
| tests | 2 older tests assumed `langdetect` was not installed (it is in requirements, so production has it); they now simulate its absence with `monkeypatch`. No assertions weakened |

Results: `pip-audit` on the pinned set: **only** `ecdsa PYSEC-2026-1325` and `python-jose PYSEC-2026-4298` remain (no fixes exist, analysed above). Test suite in the clean environment: all passed (1670 at that point; Windows needs `tzdata`, Linux uses the system database). No major-version upgrade was introduced by the floors.
Open: confirm Render's Python version before switching to the lockfile; consider replacing `python-jose` with PyJWT (separate approval).

## 2. Email verification
**Behaviour**
- New signup (migration applied): the account is flagged `email_verification_required = true`; a link (48-byte random token, SHA-256 stored, 24 h expiry, single use, bound to the address) is emailed; the **welcome email is sent after verification**; the owner is signed in immediately (no lockout). Signup without an email is flagged too and stays restricted until an address is added (PATCH /me sends the link) and confirmed.
- Link opens `static/verify-email.html`, which POSTs the token only when the person clicks (mail scanners cannot consume it), strips the token from the URL, uses `textContent`.
- `POST /auth/resend-verification` (login required, impersonation refused): sends only to the address already on the account, 3 per hour per account and 10 per hour per address, identical answer for every account (no enumeration). `GET /auth/verification-status` feeds a dashboard banner. `POST /auth/verify-email`: public, 20 per 10 minutes, generic failure text.
- Gating (only when `required = true` and not verified): campaigns, broadcast, win-back, scheduled campaigns (create and run; a scheduled campaign stays pending and goes out after verification), trial warning/expired emails, monthly report emails. **Not gated, on purpose:** the bot, orders, payments, subscriptions, Stripe/billing lifecycle emails, password reset, the trial itself (30 days unchanged).
- Email change (`PATCH /me`): rate limited (3/hour); a verified or already-required account is flagged **before** the new address is saved (if flagging fails the change is refused) and the link goes to the new address; a grandfathered account is only sent a voluntary link; refused for support (impersonation) sessions. Verifying re-checks the address atomically, so a race cannot verify a just-changed address.
- Every check **fails open** (database error or migration absent means no gating).

**Migration decision for existing accounts (documented in the SQL file):** existing rows get `email_verification_required = false` (grandfathered: full access, never gated, subscriptions and customers untouched) and **are not marked verified** (`email_verified_at` stays NULL because ownership was never proven). They see an optional banner and may verify voluntarily. Enforcing on existing accounts is a later, separate, announced decision made in cohorts (snapshot ids first, active accounts first, dormant stale addresses left grandfathered).

**Migration requirements (not applied):** `backend/migrations/email_verification.sql`, reviewed by the database engineer: idempotent; `lock_timeout` added; sequence privileges revoked; RLS on with no policies; retention statement proposed. Before running: check `businesses.id` is an integer (`select data_type from information_schema.columns where table_name='businesses' and column_name='id'`). The application re-probes every minute, so no restart is needed after applying.
**Rollback:** the three `DROP` statements at the bottom of the SQL file; the application simply stops gating. Snapshot first if you want to keep recorded verification times (`create table _bak_ev as select id, email_verified_at, email_verification_required from businesses where email_verified_at is not null`).

**Files:** `services/email_verification_service.py`, `routes/auth_routes.py`, `routes/business_routes.py`, `routes/growth_routes.py`, `routes_saas/billing_routes.py`, `services/weekly_report_service.py`, `core/plan_guard.py` (`require_email_verified`), `static/verify-email.html|js`, `static/dashboard.html|js`, `migrations/email_verification.sql`, tests `test_email_verification.py` (54).

**Reviews:** security (no critical/high). Fixed from it: no-email signup bypass, unlimited email-change mail (rate limit), grandfathered accounts being restricted by an email change, "verified" surviving a failed flag (now flag-first and abort), verify race (conditional update). Database review: lock timeout, sequence revoke, retention, cohort enforcement guidance.
**Residual:** the pre-existing signup message that an email is already registered (accepted trade-off); an attacker can still register a victim's address first, but that account is restricted from sending anything and cannot email the victim except the one verification link (rate limited); `growth/cart_recovery` and `growth/reengagement` have no callers today and are not guarded (listed in the follow-up plan).

## 3. Cookie-authentication design
`docs/ai-development/COOKIE_AUTH_DESIGN.md`: current state, `__Host-`/`__Secure-` HttpOnly Secure SameSite=Strict cookies, refresh cookie scoped to `/auth/refresh`, double-submit CSRF plus Origin/Sec-Fetch-Site checks for cookie sessions only, Bearer path untouched (Flutter and API clients unaffected), CORS with explicit origins if ever needed, feature flag `COOKIE_AUTH_ENABLED` default off, staged rollout (0 to 4) with a gate each, rollback by flag, test list. **Nothing implemented; production authentication unchanged.** Four decisions needed from you (listed at the end of that document).

## 4. Remaining security issues, in risk order
`docs/ai-development/SECURITY_FOLLOWUP_PLAN.md` holds the prioritised plan for all items you listed. Implemented now (small, backward-compatible):
- **Impersonation tokens refused on financial and irreversible actions**: new `require_not_impersonated` on order approve/cancel/refund/status, every payment-settings route, delete-account and referral withdrawal; owner-email change refused. Reading and non-financial profile edits still work for support.
- **Unsubscribe HMAC** no longer falls back to a public constant (uses the effective signing key; identical when `SECRET_KEY` is set).
- **`SECRET_KEY` shorter than 32 characters** logs a warning (key kept, so no session is invalidated).
- **Password reset** has the same 128 character and 72 byte limits as change-password.
Planned (not implemented): refresh-token family rotation, WebSocket `?token=` removal, product/admin input validation, image URL validation, production CORS, single password policy.

## 5. Tests
`python -m pytest tests -q` (from `backend/`): **1752 passed, 1 skipped** (was 1670). New: `test_email_verification.py` (54), `test_phase4_followups.py` (28). 25 protections mutation-verified (single-use claim, address binding, expiry, strict `True`, welcome suppression, resend limits, impersonation checks, trial/report/scheduled/campaign gates, email-change flag-first/abort/limit/grandfather exemption, race condition, no-email signup flag, every guard on money routes, unsubscribe secret, 72 byte check, key warning). `node --check` and `compileall` clean.
Not tested: the migration against a real database, a real email provider, a browser, multi-worker behaviour, Render's Python version.

## 6. Manual steps before release
1. Review and (when you decide) run `email_verification.sql` in Supabase; confirm the id type first. 2. Confirm `RESEND_API_KEY` and `WAZIBOT_URL` are set so verification emails can send (the link is built from the email service base URL). 3. Check `/static/verify-email.html` loads in production through Cloudflare. 4. Decide on the lockfile switch. 5. Smoke test: sign up, receive the link, confirm, send a campaign; sign up without confirming and see the campaign refused; existing account unaffected.

## 7. Compatibility
Additive APIs only (`/auth/verify-email`, `/auth/resend-verification`, `/auth/verification-status`; new keys `email_verification_required` on signup, `unverified_skipped` on trial-warnings, `skipped_unverified_email` on the scheduled runner). New 403 `email_unverified` on campaign routes only for flagged accounts. Money routes answer 403 to support sessions. `PATCH /me` is limited to 3 email changes per hour. Mobile and API clients: no change.
