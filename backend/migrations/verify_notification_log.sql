-- READ-ONLY verification for the customer notification ledger. Safe to run in the Supabase SQL editor
-- (SELECTs only; changes nothing). Run against STAGING first, then production.

-- 1. The table exists (NULL = missing -> apply notification_ledger.sql after review)
select to_regclass('public.notification_log') as notification_log_table;

-- 2. The uniqueness constraint exists: expect one row with UNIQUE (business_id, kind, period_key)
select conname, pg_get_constraintdef(oid) as definition
  from pg_constraint
 where conrelid = to_regclass('public.notification_log') and contype = 'u';

-- 3. Row level security is on and no anon/authenticated access: expect rls = true and no rows for the grants query
select relrowsecurity as rls from pg_class where oid = to_regclass('public.notification_log');
select grantee, privilege_type from information_schema.role_table_grants
 where table_schema = 'public' and table_name = 'notification_log' and grantee in ('anon', 'authenticated');

-- 4. No existing duplicates (expect zero rows)
select business_id, kind, period_key, count(*) from public.notification_log
 group by 1, 2, 3 having count(*) > 1;

-- 5. Optional second once-only layer for stock restores (NULL is acceptable; restores stay guarded by the
--    atomic status change)
select to_regclass('public.stock_movements') as stock_movements_table;
