# Meta Graph API: centralisation and upgrade plan (2026-11-20)

Status: **code centralised and tested with mocks; the DEPLOYED VERSION IS UNCHANGED (default `v18.0`). Nothing was upgraded. No live Meta call was made.**

## 1. What Meta's official documentation says (read 2026-11-20)
Sources: `developers.facebook.com/docs/graph-api/changelog/`, `.../guides/versioning`, `.../whatsapp/cloud-api/changelog` (read in full for the recent entries; older entries only summarised, so not relied on), `.../whatsapp/cloud-api/reference/messages`, `.../whatsapp/pricing`, `.../whatsapp/cloud-api/guides/send-messages`, `.../business-messaging/whatsapp/templates/overview`, `.../getting-opt-in`. Two template guides returned "This page has moved" stubs.
- **Latest Graph API version: v27.0**, introduced 4 November 2026 (Meta's table says "Available until: TBD"; the word "stable" is not used).
- **v18.0 is expired**: introduced 12 Sept 2023, available until **26 Jan 2026**. Meta: "any calls made to [an unusable version] will be defaulted to the next oldest, usable version". Every version older than v21.0 is expired in the table, so what a v18.0 call returns today is **not stated** in the docs: it works today by Meta's automatic redirection but is not guaranteed.
- **Versions listed as available (until):** v21.0 (21 Jan 2027), v22.0 (20 May 2027), v23.0 (8 Oct 2027), v24.0 (18 Feb 2028), v25.0 (29 Jul 2028), v26.0 (introduced 29 Jul 2026, TBD), v27.0 (TBD). v20.0 expired 24 Sept 2026. Each version is usable for two years after the *next* version is released.
- **Version-gated WhatsApp changes** (the changelog does not label breaking changes): v21.0+ autofill/zero-tap template fields and `data_localization_region` removed from `/register`; v23.0+ error 132018 for invalid template-send parameters; v24.0+ status webhooks drop the `conversation` object unless the message was sent in a free-entry-point window, `messaging_limit_tier` now reflects the business portfolio, error 131059 for an invalid template pagination cursor.
- **Other dated entries relevant to us:** `allow_category_change` no longer supported (Apr 2025); inbound media webhook IDs downloadable for 30 days then 7 days after 9 Oct 2025, and media webhooks now include a `url` (Oct 2025); authentication templates cannot go to a BSUID; **from 1 Oct 2026 utility messages sent inside an open customer-service window are charged** and service messages are free only up to 1,000 per phone number per month (pricing page, partly summarised).
- **Template send payload (confirmed):** `{"messaging_product":"whatsapp","recipient_type":"individual","to":"...","type":"template","template":{"name":"...","language":{"code":"en_US"},"components":[{"type":"body","parameters":[{"type":"text","text":"..."}]}]}}`. The category (utility/marketing/authentication) is set when the template is created in Meta; the send payload has no category field. Templates must be APPROVED before sending.
- **Opt-in (Nov 2024 policy):** "Businesses are required to obtain opt-in permission before messaging people on WhatsApp"; the opt-in must say the person is opting in to messages, name the business, comply with law, and businesses must honour opt-out requests. **24-hour window:** free-form messages only within 24 h of the customer's last message; afterwards only approved templates.

## 2. Recommendation
Target **v25.0** (supported until 29 Jul 2028, about 20 months of runway, released about 4 months ago); **v24.0** is the conservative fallback (until 18 Feb 2028). Do **not** pick v27.0 yet (two weeks old). The minimum acceptable is v21.0, but it expires 21 Jan 2027. This is a recommendation for your decision; the default in code stays `v18.0`.

## 3. What was done
- `core/wa_version.py`: `WA_GRAPH_VERSION` (environment), default `v18.0`, validated (`vNN.N` only; anything else falls back to the default), read on every call so rollback needs no deploy. `graph_url(path)` builds every Graph URL.
- **All 12 hard-coded references replaced** (`main.py` send, `growth/reengagement.py`, `growth/cart_recovery.py`, `integrations/whatsapp.py` x3, `routes/admin_routes.py`, `routes/business_routes.py`, `services/whatsapp_catalog.py` x2, `services/whatsapp_service.py` x2) plus the new template sender. A test fails the build if a literal version reappears anywhere outside the module.
- `tests/test_graph_version_central.py` (37 tests, mocked HTTP) verifies, for the default, v24.0 and v27.0: text sends (URL, payload, headers unchanged), the legacy integration sender, document upload and send, catalogue text/image sends, the direct-post marketing paths, media lookup and voice transcription, template sends, and the diagnostic routes.

## 4. Compatibility review (code reading, not a live test)
| Area | Used by WaziBot | Version risk |
|---|---|---|
| Text messages (`POST /{phone-number-id}/messages`) | all replies, reminders, campaigns | none documented between v18 and v27 |
| Media upload / send (`/media`, document, image) | invoices, product images | none documented |
| Media download (`GET /{media-id}`) | voice notes, images | webhook media IDs now expire (30 days, then 7): fine, they are downloaded on receipt |
| Template messages | new (Stage 1) | v23.0+ error 132018 for bad parameters: classified as a template error |
| Delivery-status webhooks | `statuses[].id/status/errors` only | v24.0 drops the `conversation` object: **the code does not read it** |
| Inbound message webhooks | `messages[]` text/media/location | no documented change; media webhooks gain a `url` (unused) |
| Phone-number lookup (`GET /{phone-number-id}`, diagnostics) | admin/business diagnostics | none documented |
| Catalogue / interactive | image and text cards only | none documented |
| Business numbers vs shared number | same endpoints | none |
| Permissions / tokens | per-business tokens, shared token | tokens are version-independent; confirm permissions in staging |
**What only a live test can confirm:** response structures for each call on the chosen version, token permissions, template send behaviour, webhook payloads. Use the staging checklist below.

## 5. Rollout (needs your explicit approval; nothing here has been done)
1. Create a **staging** service pointing at a **test WhatsApp number** (Meta's free test number is enough) and a staging database; no production tokens.
2. Set `WA_GRAPH_VERSION=v25.0` on staging only. Run: send a text; send an image and a PDF; receive text, image and voice note (confirm download and transcription); receive status webhooks (sent, delivered, read, failed); send an approved test template; check the diagnostics routes; check the catalogue sends; shared-number path and invalid-phone-id fallback.
3. Compare responses with `v18.0` on the same staging number; note any difference in fields the code reads (`messages[0].id`, `error.code`, `error.error_subcode`, `statuses`).
4. Check Meta's app dashboard "Upgrade API Version" and the app's default version (unversioned calls use it).
5. Only then set `WA_GRAPH_VERSION=v25.0` on production in a quiet period, watch the logs for `wa.failed` and error codes for an hour, and the delivery-status updates.
## 6. Rollback
Unset `WA_GRAPH_VERSION` (or set `v18.0`) in the Render environment: effective immediately, no deploy (the value is read per call). If Meta's automatic redirection of the expired v18.0 ever stops working, set any version from section 1 that has passed staging (v21.0 and later).
