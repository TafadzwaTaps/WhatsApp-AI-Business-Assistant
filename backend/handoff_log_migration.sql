-- Phase 14: Conversation Analytics — durable handoff-event log.
--
-- Run this once in the Supabase SQL editor. Optional in the sense that
-- every read/write against it (crud/handoff_log.py) fails soft if it's
-- missing — WaziBot works exactly the same without it; the human-handoff
-- feature itself (Phase 9) does not depend on this table at all, it just
-- won't have an accurate handoff-rate-over-time analytics figure yet
-- (existing endpoints fall back to their prior emoji-text-prefix proxy).

create table if not exists handoff_log (
    id             bigserial primary key,
    business_id    bigint not null,
    phone          text,
    reason         text,       -- e.g. "Complex request", "Refund request", ...
    created_at     timestamptz default now()
);

create index if not exists handoff_log_business_created_idx
    on handoff_log (business_id, created_at);
