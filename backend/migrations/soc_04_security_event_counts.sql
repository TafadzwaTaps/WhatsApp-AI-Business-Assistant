-- Security Operations Center, part 4: let record_security_event() add several repeats at once
-- (p_count), as the Stage 4a recorder sends them. NOT applied automatically: the owner reports
-- running it on 2026-11-27 without errors. Needs part 1 (events and incidents), applied earlier by
-- the owner and not kept in this repository.
--
-- Until this runs, the new code cannot use the function and falls back to a plain insert: events
-- are recorded, but their severity column stays at its default 'info'. Safe to run before or after
-- the code is deployed, and safe to re-run.
--
-- PRE-FLIGHT (read-only), expect one row ending in "p_metadata jsonb":
--   select pg_get_function_identity_arguments(oid) from pg_proc
--    where pronamespace = 'public'::regnamespace and proname = 'record_security_event';

begin;
set local lock_timeout = '5s';

-- The 14-argument version is removed so the API never has two candidates to choose between.
drop function if exists public.record_security_event(
    text, text, text, text, text, text, text, text, text, text, text, text, bigint, jsonb);

create or replace function public.record_security_event(
    p_event_type     text,
    p_dedup_key      text,
    p_severity       text    default 'info',
    p_confidence     text    default null,
    p_outcome        text    default null,
    p_rule           text    default null,
    p_action_taken   text    default null,
    p_endpoint       text    default null,
    p_correlation_id text    default null,
    p_ip             text    default null,
    p_user_agent     text    default null,
    p_username       text    default null,
    p_business_id    bigint  default null,
    p_metadata       jsonb   default '{}'::jsonb,
    p_count          integer default 1
) returns void
language sql
security invoker
set search_path = ''
as $$
    insert into public.security_events as e
        (event_type, dedup_key, severity, confidence, outcome, rule, action_taken, endpoint,
         correlation_id, ip, user_agent, username, business_id, metadata, occurrence_count,
         created_at, last_seen_at)
    values
        (p_event_type, p_dedup_key, p_severity, p_confidence, p_outcome, p_rule, p_action_taken, p_endpoint,
         p_correlation_id, p_ip, p_user_agent, p_username, p_business_id, p_metadata,
         greatest(coalesce(p_count, 1), 1), now(), now())
    on conflict (dedup_key) where dedup_key is not null
    do update set occurrence_count = e.occurrence_count + excluded.occurrence_count, last_seen_at = now();
$$;

revoke all on function public.record_security_event(
    text, text, text, text, text, text, text, text, text, text, text, text, bigint, jsonb, integer)
    from public, anon, authenticated;
grant execute on function public.record_security_event(
    text, text, text, text, text, text, text, text, text, text, text, text, bigint, jsonb, integer)
    to service_role;

commit;

notify pgrst, 'reload schema';

-- VERIFY (writes one test row, then removes it by rolling back). Success means no error is raised:
-- the two calls must add up to occurrence_count = 5.
--   begin;
--   select public.record_security_event('verify_test', 'verify-key', p_count => 2);
--   select public.record_security_event('verify_test', 'verify-key', p_count => 3);
--   do $v$ begin
--     if (select occurrence_count from public.security_events where dedup_key = 'verify-key') is distinct from 5
--     then raise exception 'unexpected count'; end if;
--   end $v$;
--   rollback;
--
-- ROLLBACK (the application falls back to plain inserts when the function is absent):
--   drop function if exists public.record_security_event(
--       text, text, text, text, text, text, text, text, text, text, text, text, bigint, jsonb, integer);
--   notify pgrst, 'reload schema';
