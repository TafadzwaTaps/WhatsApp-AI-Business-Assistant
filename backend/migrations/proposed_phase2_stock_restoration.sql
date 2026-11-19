-- PROPOSED migration (Phase 2): idempotent stock restoration on cancel / refund.
-- *** NOT APPLIED. NOT TESTED against a live database. Run in a STAGING copy first. ***
-- Design and rules: docs/ai-development/phase2-inventory-restoration-plan.md
--
-- PRE-CHECKS (read-only, send me the output before running anything):
--   select to_regclass('public.stock_movements');                         -- NULL = not created yet
--   select count(*) from public.products where stock < 0;                 -- must be 0 before the CHECK
--   select proname from pg_proc where pronamespace='public'::regnamespace and proname='adjust_stock_atomic';

-- 1) Ledger: one row per (order, product, kind). The UNIQUE constraint is the idempotency
--    guarantee: inserting a second 'restore' for the same order+product fails, so a stock
--    restore can never be applied twice.
CREATE TABLE IF NOT EXISTS public.stock_movements (
    id          bigserial   PRIMARY KEY,
    business_id integer     NOT NULL,
    order_id    integer     NOT NULL,
    product_id  integer     NOT NULL,
    kind        text        NOT NULL CHECK (kind IN ('deduct', 'restore')),
    quantity    integer     NOT NULL CHECK (quantity > 0),
    reason      text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT stock_movements_once UNIQUE (order_id, product_id, kind)
);
CREATE INDEX IF NOT EXISTS stock_movements_business_idx ON public.stock_movements (business_id, order_id);

ALTER TABLE public.stock_movements ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.stock_movements FROM anon, authenticated;
REVOKE ALL ON SEQUENCE public.stock_movements_id_seq FROM anon, authenticated;
GRANT ALL ON public.stock_movements TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.stock_movements_id_seq TO service_role;

-- 2) Atomic, tenant-scoped stock adjustment. A single UPDATE (no read-modify-write); products
--    with NULL stock (unlimited) are never touched; returns the new stock or no row.
CREATE OR REPLACE FUNCTION public.adjust_stock_atomic(p_business_id integer, p_product_id integer, p_delta integer)
RETURNS integer
LANGUAGE sql
SET search_path = public
AS $$
    UPDATE public.products
       SET stock = stock + p_delta
     WHERE id = p_product_id
       AND business_id = p_business_id
       AND stock IS NOT NULL
       AND stock + p_delta >= 0
    RETURNING stock;
$$;
-- PostgreSQL grants EXECUTE to PUBLIC by default; revoke it there too, then allow only the backend role.
REVOKE ALL ON FUNCTION public.adjust_stock_atomic(integer, integer, integer) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.adjust_stock_atomic(integer, integer, integer) TO service_role;

-- Make PostgREST notice the new table and function without a restart.
NOTIFY pgrst, 'reload schema';

-- 3) Optional backstop, to be done LATER AND SEPARATELY (not part of the steps above):
--    stock can never go negative. NOT VALID still enforces the rule on every new write at once, and
--    ADD CONSTRAINT briefly takes an exclusive lock on products, so: check `select count(*) from
--    public.products where stock < 0` is 0, run `SET lock_timeout = '3s';` first, and retry if it times out.
-- Optional foreign keys (staging first; check orders.id / businesses.id types match):
--    ALTER TABLE public.stock_movements ADD CONSTRAINT stock_movements_order_fk
--        FOREIGN KEY (order_id) REFERENCES public.orders(id) ON DELETE CASCADE;
--    (do NOT add a foreign key on product_id: products can be deleted and the ledger is history)
-- ALTER TABLE public.products ADD CONSTRAINT products_stock_nonneg CHECK (stock IS NULL OR stock >= 0) NOT VALID;
-- ALTER TABLE public.products VALIDATE CONSTRAINT products_stock_nonneg;

-- ROLLBACK:
--   DROP FUNCTION IF EXISTS public.adjust_stock_atomic(integer, integer, integer);
--   DROP TABLE IF EXISTS public.stock_movements;
--   ALTER TABLE public.products DROP CONSTRAINT IF EXISTS products_stock_nonneg;
