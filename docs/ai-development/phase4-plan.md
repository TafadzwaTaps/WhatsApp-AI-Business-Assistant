# Phase 4 plan — AI employee improvements (2026-11-17)

Status: **PLAN ONLY — awaiting approval. No code changed.**
Basis: read-only audits by the AI/WhatsApp engineer (code) and QA engineer (test map). Items marked (V) I re-read in the code myself; the rest are agent findings I have not yet re-verified. The audit did not open `intent_engine.py`, `nl_commerce.py`, `order_parser_service.py` or `language_commands.py` in detail.

## Headline
The engine is deliberately deterministic: prices, stock, cart, order and payment text come from database rows, the only LLM text call is one optional add-to-cart rephrase behind an allow-list, payment claims never mark an order paid, tenant scoping on state/cart/memory is by `(phone, business_id)`, and `services/ai.py` stays one module. The real risks are **the bot speaking over a human, losing conversation state on a transient DB error, and a few global/cost gaps** — not hallucination.

## Findings (ranked)
| ID | Pri | Finding | Minimal fix | Risk |
|---|---|---|---|---|
| H1 (V) | P1 | `workflows/human_handoff.py:181-199`: after a complaint/safety handoff (not agent-initiated) the customer typing "hi"/"menu" resumes the bot mid-incident; the 45-minute timeout ignores `last_agent_reply_at` although the docstring says it must not. | No restart-word resume for safety/complaint handoffs; timeout requires no recent agent reply. | Low-med (resume UX changes) |
| S1 (V) | P1 | `services/_ai_state.py:25-42,70-91`: `_read_state_data` swallows errors and returns `{}`; `_write_state_data` then upserts `{}`+patch over the whole row, wiping `human_handoff`, `pending_payment` and the cart snapshot after a transient read error. | The write path uses a strict read and aborts (logs) on error instead of overwriting. | Low |
| H2 (V) | P1 | `services/ai.py:3303-3306` and `routes/webhook_routes.py` ~474/801-830: the platform "replies paused" text and the step-6 crash fallback are sent even when the conversation is in human handoff, and the pause text repeats on every message. | Stay silent when `_in_handoff`; throttle the pause notice. | Low |
| O1 | P2 | `ai.py:1584,3186`: two recursive `_generate_reply_core` calls drop `business_config` → non-USD/service businesses get "$" and default rules on that path. | Pass `business_config` (and `image_analysis`) through. | Low |
| O2 | P2 | `order_lifecycle.py:172-205`, `ai.py:2780`: cart prices are snapshots; no max quantity ("add 100000" on unlimited stock/service items). | Cap quantity (default 99, per-business setting later); re-read price by product id at order creation and warn on mismatch. | **Medium: changes order totals** |
| T1 | P2 | `webhook_routes.py:682-697`: `biz_config` lacks `escalation_keywords`, so business escalation rules never fire. | Add the field to the config. | Low |
| G1 | P2 | `business_knowledge.py:219-228`: delivery fee 0/unset answers "We offer free delivery!" | Claim free delivery only when explicitly configured (None vs 0). | Low |
| N1 | P2 | `webhook_routes.py` (several lines), `translation_layer.py:443-465`: sync Supabase/`requests` calls inside the async handler block the event loop for all tenants; `get_products` called twice per message. | `run_in_threadpool` + reuse `products`. | Low |
| C1 | P2 | Voice notes: no `should_call_llm` gate or usage record for Whisper; no per-sender inbound rate cap; quota fails open. A voice flood costs money. | Gate+record transcription like image analysis; per-sender message cap. | Low |
| P1x | P2 | `ai.py` logs raw phone numbers and first 80-120 chars of customer text (19 sites). | Mask phones, drop message text from logs. | Low |
| H3 | P2 | Handoffs write a dashboard marker only; owners are not pinged on WhatsApp (new orders are). | Same throttled owner notification. | Low (**sends WhatsApp to owner: needs your OK**) |
| L1 | P3 | `llm_response.py` module-global usage metadata shared across threads → usage can be attributed to the wrong tenant. | Return metadata with the result. | Low |
| L2 | P3 | Money check only sees `$£€R`; delivery fee not in the allowed facts; merchant product names enter the prompt unfiltered. | Check any number vs facts; business currency symbol. | Low (feature off by default) |
| G2 | P3 | "Dial *151# (Zimbabwe)" shown to any business with EcoCash; PayPal confirmation hardcodes `$`/USD. | Business currency; conditional EcoCash hint. | Low |
| I1 | P3 | Injection guard is English-regex only and refuses benign "what are your rules/guidelines". | Narrow patterns, add languages. | Low |
| W1 | P3 | Per-customer lock is in-process (single Render instance assumed, OK); crash after message store + Meta retry is deduplicated so the customer gets only the generic fallback; unreadable state → no reply. | Send a short "please try again" when state unreadable; DB lock only if you ever run >1 worker. | Med |

## Test gaps to close (from QA)
Each fix above adds an offline test; additionally, new tests for: `_handle_one_message` end to end (real body, stubbed AI/send); handoff silence for normal text/order/payment-claim messages; same phone in two tenants keeps separate state/cart/memory (needs a stateful filter-aware fake like `test_phase2_tenant_isolation.py`); retried wamid does not create two orders or double-decrement stock; repeated "I paid", no pending order, other business's order; LLM timeout/empty/malformed output reaching the customer; injection through a product name and in other languages.

## Proposed stages (each: tests, Security + QA review, change-log; no commit/deploy)
1. **Conversation safety (H1, S1, H2, T1)** — bot never speaks over a human; state never wiped by a read error; business escalation rules work. Files: `human_handoff.py`, `_ai_state.py`, `ai.py`, `webhook_routes.py` (lead sequences; one owner at a time).
2. **Correctness (O1, G1, G2, L1)** — config propagation, honest delivery/payment text, no cross-tenant usage attribution.
3. **Cost and load (C1, N1, P1x)** — gate voice transcription, per-sender cap, threadpool for blocking calls, log hygiene.
4. **Order guardrails (O2)** — quantity cap and price re-check; **needs your decision on the cap and on mismatch behaviour** (warn vs. use current price).
5. **Hardening/tests (I1, L2, W1, new test suite above).**
6. **H3 owner WhatsApp notification** — only if you approve sending real WhatsApp messages to owners (CLAUDE.md rule); I would build it behind the existing owner-notification path and test with mocks only.

## Decisions needed
1. Approve stages 1-3 now?
2. O2: maximum quantity per line (suggest 99) and on a price change between add-to-cart and order — charge the current price and tell the customer, or block until they re-confirm?
3. H3: approve owner WhatsApp pings for handoffs (throttled, e.g. one per conversation per 30 minutes)?
4. Inbox WebSocket switch (Phase 3 step 2): is the Phase 3 backend deployed now? If yes I do it first (small).
