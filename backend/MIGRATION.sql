-- ============================================================
-- MIGRATION.sql — WaziBot complete schema upgrade
-- Run in: Supabase Dashboard → SQL Editor → New Query
-- Safe to run multiple times (ADD COLUMN IF NOT EXISTS)
-- ============================================================

-- ╔══════════════════════════════════════════════════════════╗
-- ║  1. ORDERS TABLE — add missing columns                  ║
-- ╚══════════════════════════════════════════════════════════╝

ALTER TABLE orders ADD COLUMN IF NOT EXISTS items             TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_status    TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_reference TEXT;

-- Fix created_at: make sure it has a default and is timestamptz
-- (safe — only changes default, not existing data)
ALTER TABLE orders ALTER COLUMN created_at SET DEFAULT NOW();

-- Backfill payment_status for existing rows
UPDATE orders
  SET payment_status = CASE
    WHEN status = 'paid'      THEN 'paid'
    WHEN status = 'delivered' THEN 'paid'
    ELSE 'pending'
  END
WHERE payment_status IS NULL OR payment_status = '';


-- ╔══════════════════════════════════════════════════════════╗
-- ║  2. BUSINESSES TABLE — payment details                  ║
-- ╚══════════════════════════════════════════════════════════╝

-- EcoCash / mobile money number shown on invoices (e.g. +263771234567)
ALTER TABLE businesses ADD COLUMN IF NOT EXISTS payment_number TEXT;

-- Registered business name shown next to payment number
ALTER TABLE businesses ADD COLUMN IF NOT EXISTS payment_name   TEXT;

-- Make sure created_at has a proper default
ALTER TABLE businesses ALTER COLUMN created_at SET DEFAULT NOW();


-- ╔══════════════════════════════════════════════════════════╗
-- ║  3. MESSAGES TABLE — timestamps fix                     ║
-- ╚══════════════════════════════════════════════════════════╝

-- Ensure created_at is timestamptz with a proper default so times
-- always save correctly (this is why time was showing as "—" in inbox)
ALTER TABLE messages ALTER COLUMN created_at SET DEFAULT NOW();

-- If created_at is plain TIMESTAMP (no timezone), cast it to timestamptz
-- Uncomment the block below ONLY if you see timezone issues:
-- ALTER TABLE messages ALTER COLUMN created_at TYPE TIMESTAMPTZ
--   USING created_at AT TIME ZONE 'UTC';

-- Index for fast message lookups by customer + time
CREATE INDEX IF NOT EXISTS ix_messages_customer_time
  ON messages (customer_id, created_at);

CREATE INDEX IF NOT EXISTS ix_messages_business_time
  ON messages (business_id, created_at);


-- ╔══════════════════════════════════════════════════════════╗
-- ║  4. CHAT_MESSAGES TABLE — timestamp fix                 ║
-- ╚══════════════════════════════════════════════════════════╝

ALTER TABLE chat_messages ALTER COLUMN created_at SET DEFAULT NOW();


-- ╔══════════════════════════════════════════════════════════╗
-- ║  5. CUSTOMERS TABLE — timestamp fix                     ║
-- ╚══════════════════════════════════════════════════════════╝

ALTER TABLE customers ALTER COLUMN created_at SET DEFAULT NOW();
ALTER TABLE customers ALTER COLUMN last_seen  SET DEFAULT NOW();


-- ╔══════════════════════════════════════════════════════════╗
-- ║  6. CARTS TABLE — ensure it exists                      ║
-- ╚══════════════════════════════════════════════════════════╝

CREATE TABLE IF NOT EXISTS carts (
  id          SERIAL PRIMARY KEY,
  phone       TEXT NOT NULL,
  business_id INTEGER NOT NULL,
  items       JSONB NOT NULL DEFAULT '[]',
  updated_at  TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (phone, business_id)
);

CREATE INDEX IF NOT EXISTS ix_carts_phone_biz ON carts (phone, business_id);


-- ╔══════════════════════════════════════════════════════════╗
-- ║  7. USER_MEMORY TABLE — ensure it exists                ║
-- ╚══════════════════════════════════════════════════════════╝

CREATE TABLE IF NOT EXISTS user_memory (
  id              SERIAL PRIMARY KEY,
  phone           TEXT NOT NULL,
  business_id     INTEGER NOT NULL,
  frequent_items  JSONB NOT NULL DEFAULT '{}',
  last_orders     JSONB NOT NULL DEFAULT '[]',
  updated_at      TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (phone, business_id)
);

CREATE INDEX IF NOT EXISTS ix_user_memory_phone_biz ON user_memory (phone, business_id);


-- ╔══════════════════════════════════════════════════════════╗
-- ║  8. increment_unread RPC — create if missing            ║
-- ╚══════════════════════════════════════════════════════════╝

CREATE OR REPLACE FUNCTION increment_unread(p_customer_id INTEGER)
RETURNS VOID AS $$
BEGIN
  UPDATE customers
    SET unread_count = COALESCE(unread_count, 0) + 1
  WHERE id = p_customer_id;
END;
$$ LANGUAGE plpgsql;


-- ╔══════════════════════════════════════════════════════════╗
-- ║  9. VERIFY — check final schema                         ║
-- ╚══════════════════════════════════════════════════════════╝

SELECT
  table_name,
  column_name,
  data_type,
  column_default,
  is_nullable
FROM information_schema.columns
WHERE table_name IN ('orders', 'businesses', 'messages', 'carts', 'customers')
ORDER BY table_name, ordinal_position;


-- ╔══════════════════════════════════════════════════════════╗
-- ║  10. MULTI-PAYMENT COLUMNS (run after initial migration) ║
-- ╚══════════════════════════════════════════════════════════╝

-- payment_method: which gateway the customer chose
ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_method TEXT DEFAULT 'ecocash';

-- payment_url: Paynow / PayPal redirect link
ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_url TEXT;

-- Extend payment_status to include 'pending_payment' and 'payment_error'
-- (existing 'pending' and 'paid' values remain valid)
-- No schema change needed — it's a free-text field.

-- Index for fast payment status queries
CREATE INDEX IF NOT EXISTS ix_orders_payment_status
  ON orders (payment_status, business_id);

-- Verify payment columns
SELECT column_name, data_type, column_default
FROM information_schema.columns
WHERE table_name = 'orders'
  AND column_name IN ('payment_method','payment_status','payment_reference','payment_url','items')
ORDER BY column_name;


-- ╔══════════════════════════════════════════════════════════╗
-- ║  11. CART STATE COLUMN  (required for stateful checkout) ║
-- ╚══════════════════════════════════════════════════════════╝

-- state_data stores the conversation state for each customer session.
-- This is separate from 'items' so existing cart logic is unaffected.
-- Schema: { state: 'browsing'|'checkout'|'awaiting_payment',
--           session: { cart_snapshot: [...] },
--           pending_payment: { order_id, method, reference } | null }

ALTER TABLE carts ADD COLUMN IF NOT EXISTS state_data JSONB DEFAULT '{"state":"browsing","session":{},"pending_payment":null}'::jsonb;

-- Index for fast state lookups (e.g. finding all users in checkout)
CREATE INDEX IF NOT EXISTS ix_carts_state
  ON carts USING gin(state_data);

-- Verify
SELECT column_name, data_type, column_default
FROM information_schema.columns
WHERE table_name = 'carts'
ORDER BY ordinal_position;


-- ╔══════════════════════════════════════════════════════════╗
-- ║  12. DEDICATED PAYMENT SETTINGS COLUMNS                 ║
-- ╚══════════════════════════════════════════════════════════╝
-- These replace the generic payment_number / payment_name fields
-- with purpose-specific columns. Legacy fields are kept for compat.

-- EcoCash number customers send money to (e.g. +263771234567)
ALTER TABLE businesses ADD COLUMN IF NOT EXISTS ecocash_number TEXT;

-- Registered EcoCash account name (e.g. "Flavoury Foods (Pvt) Ltd")
ALTER TABLE businesses ADD COLUMN IF NOT EXISTS ecocash_name   TEXT;

-- Business's real PayPal email address where money is received
-- (NOT a sandbox/test email — this is the actual PayPal account)
ALTER TABLE businesses ADD COLUMN IF NOT EXISTS paypal_email   TEXT;

-- Backfill: copy legacy payment_number → ecocash_number where not set
UPDATE businesses
  SET ecocash_number = payment_number,
      ecocash_name   = payment_name
WHERE ecocash_number IS NULL
  AND payment_number IS NOT NULL;

-- Indexes for fast lookups
CREATE INDEX IF NOT EXISTS ix_businesses_ecocash  ON businesses (ecocash_number) WHERE ecocash_number IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_businesses_paypal   ON businesses (paypal_email)   WHERE paypal_email   IS NOT NULL;

-- Verify payment columns on businesses table
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'businesses'
  AND column_name IN ('payment_number','payment_name','ecocash_number','ecocash_name','paypal_email')
ORDER BY column_name;


-- ╔══════════════════════════════════════════════════════════╗
-- ║  13. PAYMENT PROOF + RATE LIMITING (no new columns)     ║
-- ╚══════════════════════════════════════════════════════════╝
-- These features store data inside carts.state_data JSONB.
-- No schema change needed — state_data already exists (section 11).
--
-- Fields added to state_data by the application:
--   state_data.state:             'browsing'|'confirm_order'|'checkout'|
--                                 'awaiting_payment'|'awaiting_proof'
--   state_data.pending_proof:     { order_id, method, reference }
--   state_data.checkout_attempts: [timestamp, ...]  (rate limiting)
--
-- Update the state_data default to include new states:
UPDATE carts
  SET state_data = jsonb_set(
    COALESCE(state_data, '{}'),
    '{pending_proof}',
    'null'::jsonb
  )
WHERE state_data IS NOT NULL
  AND NOT (state_data ? 'pending_proof');

-- Also add checkout_attempts array default:
UPDATE carts
  SET state_data = jsonb_set(
    COALESCE(state_data, '{}'),
    '{checkout_attempts}',
    '[]'::jsonb
  )
WHERE state_data IS NOT NULL
  AND NOT (state_data ? 'checkout_attempts');

-- Verify state_data column is JSONB
SELECT column_name, data_type, column_default
FROM information_schema.columns
WHERE table_name = 'carts' AND column_name = 'state_data';


-- ╔══════════════════════════════════════════════════════════╗
-- ║  14. PAYPAL AUTO-VERIFICATION COLUMNS                   ║
-- ╚══════════════════════════════════════════════════════════╝

-- PayPal's order ID returned by the Orders v2 API.
-- Stored so the webhook can look up the internal order when
-- PayPal sends PAYMENT.CAPTURE.COMPLETED.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS paypal_order_id TEXT;

-- Fast lookup by PayPal's order ID (webhook handler uses this)
CREATE UNIQUE INDEX IF NOT EXISTS ix_orders_paypal_order_id
  ON orders (paypal_order_id)
  WHERE paypal_order_id IS NOT NULL;

-- payment_method should allow all current values
-- (no schema change needed — it's a free-text column)

-- Verify
SELECT column_name, data_type, column_default
FROM information_schema.columns
WHERE table_name = 'orders'
  AND column_name IN (
    'payment_method', 'payment_status', 'payment_reference',
    'payment_url', 'paypal_order_id'
  )
ORDER BY column_name;


-- ╔══════════════════════════════════════════════════════════╗
-- ║  15. PRODUCTS TABLE — add optional columns              ║
-- ╚══════════════════════════════════════════════════════════╝
-- Run this to unlock stock tracking and low-stock alerts.
-- The backend will work WITHOUT these columns (they are
-- detected at runtime and omitted if missing).

ALTER TABLE products ADD COLUMN IF NOT EXISTS image_url          TEXT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS stock              INTEGER NOT NULL DEFAULT 0;
ALTER TABLE products ADD COLUMN IF NOT EXISTS low_stock_threshold INTEGER NOT NULL DEFAULT 5;

-- Backfill existing rows with safe defaults
UPDATE products SET stock = 0              WHERE stock IS NULL;
UPDATE products SET low_stock_threshold = 5 WHERE low_stock_threshold IS NULL;

-- Index for fast low-stock queries
CREATE INDEX IF NOT EXISTS ix_products_business
  ON products (business_id);

-- Verify
SELECT column_name, data_type, column_default
FROM information_schema.columns
WHERE table_name = 'products'
ORDER BY ordinal_position;


-- ╔══════════════════════════════════════════════════════════╗
-- ║  16. FULFILLMENT + LIFECYCLE COLUMNS                    ║
-- ╚══════════════════════════════════════════════════════════╝

-- How the order will be fulfilled: 'delivery' or 'pickup'
ALTER TABLE orders ADD COLUMN IF NOT EXISTS fulfillment_method TEXT;

-- Customer's delivery address (set when fulfillment_method = 'delivery')
ALTER TABLE orders ADD COLUMN IF NOT EXISTS delivery_address  TEXT;

-- Optional internal note from staff (e.g. rejection reason)
ALTER TABLE orders ADD COLUMN IF NOT EXISTS fulfillment_notes TEXT;

-- Extend payment_status values (no schema change — free-text column):
--   pending_cash         cash order, confirmed immediately
--   awaiting_payment     waiting for online payment
--   payment_review       proof submitted, team reviewing
--   paid                 payment verified
--   cancelled            order/payment cancelled
--   refunded             refund issued

-- Index for fast fulfillment queries
CREATE INDEX IF NOT EXISTS ix_orders_fulfillment
  ON orders (fulfillment_method, business_id)
  WHERE fulfillment_method IS NOT NULL;

-- Verify
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'orders'
  AND column_name IN (
    'fulfillment_method','delivery_address','fulfillment_notes',
    'payment_method','payment_status','payment_reference','paypal_order_id'
  )
ORDER BY column_name;
