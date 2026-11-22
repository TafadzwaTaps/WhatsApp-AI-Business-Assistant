-- PLACEHOLDER: this file contains NO real SQL. It was committed empty; code comments refer to it (stripe_service.py, plan_guard.py, stripe_webhook_events.py) but nothing executes it.
-- The billing/lifecycle columns on public.businesses currently exist in production only because they were applied
-- by hand or by the inline statements in backend/billing/stripe_service.py (about lines 1973-1990) and the
-- admin-route SQL dump in backend/routes/saas_admin_routes.py. Those two sources differ.
-- TODO (owner): take a redacted `pg_dump --schema-only` of production, then write the real, idempotent migration
-- here (ADD COLUMN IF NOT EXISTS ...) and review it in staging. Do not rely on this file as it stands.
SELECT 1;  -- no-op so the file is harmless if run
