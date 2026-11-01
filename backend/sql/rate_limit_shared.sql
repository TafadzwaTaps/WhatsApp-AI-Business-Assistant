-- Shared rate-limit / lockout counters (optional). Run once in Supabase SQL editor,
-- then set RATE_LIMIT_BACKEND=supabase in Render. Safe to re-run.
create table if not exists rate_limit_counters (
  key          text primary key,
  window_start timestamptz not null default now(),
  hits         integer     not null default 0
);
alter table rate_limit_counters enable row level security;   -- service role bypasses RLS

create or replace function rl_hit(p_key text, p_window integer) returns integer
language plpgsql as $$
declare v integer;
begin
  insert into rate_limit_counters(key, window_start, hits) values (p_key, now(), 1)
  on conflict (key) do update set
    hits = case when rate_limit_counters.window_start < now() - make_interval(secs => p_window)
                then 1 else rate_limit_counters.hits + 1 end,
    window_start = case when rate_limit_counters.window_start < now() - make_interval(secs => p_window)
                then now() else rate_limit_counters.window_start end
  returning hits into v;
  return v;
end $$;

create or replace function rl_peek(p_key text, p_window integer) returns integer
language sql stable as $$
  select coalesce((select hits from rate_limit_counters
                   where key = p_key and window_start >= now() - make_interval(secs => p_window)), 0)
$$;

create or replace function rl_clear(p_key text) returns void
language sql as $$ delete from rate_limit_counters where key = p_key $$;

-- Optional housekeeping (run occasionally or schedule with pg_cron):
-- delete from rate_limit_counters where window_start < now() - interval '1 day';
