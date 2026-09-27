-- Phase 12: AI Cost Optimization — usage/cost tracking table.
--
-- Run this once in the Supabase SQL editor. Optional in the sense that
-- every read/write against it (crud/ai_usage.py) fails soft if it's
-- missing — WaziBot works exactly the same without it, it just won't
-- have usage history or an enforced per-business AI request cap yet.

create table if not exists ai_usage_log (
    id                 bigserial primary key,
    business_id        bigint not null,
    phone              text,
    conversation_id    text,
    intent             text,
    tier               text,       -- "none" | "cheap" | "full"
    provider           text,       -- "openai" | "anthropic"
    model              text,
    prompt_tokens      integer default 0,
    completion_tokens  integer default 0,
    total_tokens       integer default 0,
    estimated_cost     numeric(10, 6) default 0,
    latency_ms         integer,
    status             text,       -- "ok" | "disabled" | "no_key" | "rejected" | "error" | ...
    created_at         timestamptz default now()
);

create index if not exists ai_usage_log_business_created_idx
    on ai_usage_log (business_id, created_at);
