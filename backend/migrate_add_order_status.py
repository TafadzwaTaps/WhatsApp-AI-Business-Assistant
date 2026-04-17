"""
Migration: add `status` column to the orders table.

Run ONCE on any existing database before restarting the server:

    python migrate_add_order_status.py

Safe to re-run — skips if column already exists.
"""
import sqlite3
import os

DB_PATH = os.getenv("DB_PATH", "app.db")

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

# Check existing columns
cur.execute("PRAGMA table_info(orders)")
cols = [row[1] for row in cur.fetchall()]

if "status" in cols:
    print("✅ 'status' column already exists in orders — nothing to do.")
else:
    cur.execute("ALTER TABLE orders ADD COLUMN status TEXT DEFAULT 'pending'")
    conn.commit()
    print("✅ Added 'status' column to orders table with default 'pending'.")
    # Back-fill existing rows
    cur.execute("UPDATE orders SET status = 'pending' WHERE status IS NULL")
    conn.commit()
    updated = cur.rowcount
    print(f"   Back-filled {updated} existing order(s) with status='pending'.")

conn.close()
print("Done.")
