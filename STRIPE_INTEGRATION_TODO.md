# Stripe Integration TODO

Existing Checkout Session calls were found and updated in place (no new files, routes or refactors):

- `create_checkout_session()` – subscription upgrades (`mode="subscription"`)
- `create_product_checkout_session()` – storefront purchases (`mode="payment"`)

## Values to Replace

All `sample_only` parameters (`mode`, `success_url`, `cancel_url`, `line_items`) already have real values in your code and were preserved. Nothing is still a placeholder, except:

**Files containing placeholders:**
- [backend/billing/stripe_service.py](backend/billing/stripe_service.py)

| Field | Current Value | What to Set |
|-------|--------------|-------------|
| `WAZIBOT_URL` env var (base of default success/cancel URLs) | falls back to `https://wazibothq.com` | Set to your real domain in production. |
| `STRIPE_PRICE_<TIER>_<PERIOD>` env vars (subscription `line_items`) | per-tier price IDs from env | Set to your real Stripe Price IDs (https://dashboard.stripe.com/prices). |

## Configured Parameters

**Files containing these parameters:**
- [backend/billing/stripe_service.py](backend/billing/stripe_service.py)

| Parameter | Value | Applied to |
|-----------|-------|-----------|
| billing_address_collection | `auto` | both |
| phone_number_collection | `{enabled: true}` | both |
| automatic_tax | `{enabled: false}` | both |
| allow_promotion_codes | `true` | both |
| payment_method_collection | `always` | subscription only (per rule 8) |
| submit_type | `auto` | payment only (Stripe only accepts it in `payment` mode) |

### Parameters NOT sent (correction)
`ui_mode`, `branding_settings`, `origin_context` and `integration_identifier` from Checkout Studio are **not** sent. `backend/billing/stripe_service.py` pins `api_version = "2024-06-20"`, which does not accept them (`ui_mode` only allows `hosted`/`embedded` there), so sending them would make every checkout fail. Re-add them only after deliberately upgrading the pinned API version and testing in test mode.

### SDK version note (historical)
`backend/requirements.txt` pins `stripe>=7.0.0`, so the installed version couldn't be determined. `ui_mode="hosted_page"` is used (SDK >= 21.0.0). If your installed SDK is older than 21.0.0, change it to `"hosted"` or upgrade (`pip install -U stripe`). Also confirm your SDK/API version supports `branding_settings`, `origin_context` and `integration_identifier`; Stripe rejects unknown params.

## Setup and next steps

- Env vars (already used by the code): `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `WAZIBOT_URL`, per-tier `STRIPE_PRICE_*`. See `backend/.env.example`.
- Flow: client calls the billing/storefront route → server creates a Checkout Session → user is redirected to the Stripe-hosted page → Stripe sends `checkout.session.completed` to your existing webhook handler, which activates the subscription or records the order.
- Testing: use test keys and card `4242 4242 4242 4242` (any future expiry, any CVC). Declines: `4000 0000 0000 0002`. See https://docs.stripe.com/testing.
- Next steps: verify the branding logo file ID exists in your Stripe account, confirm Stripe Tax is not needed (automatic_tax is off), and test both flows end to end.
- Resources: https://support.stripe.com and https://docs.stripe.com/mcp
