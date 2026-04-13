import sqlite3

conn = sqlite3.connect("app.db")
c = conn.cursor()

print("🔄 Starting migration...")

# 1. Create businesses table
c.execute("""
CREATE TABLE IF NOT EXISTS businesses (
    id INTEGER PRIMARY KEY,
    name VARCHAR NOT NULL,
    owner_username VARCHAR UNIQUE NOT NULL,
    owner_password VARCHAR NOT NULL,
    whatsapp_phone_id VARCHAR UNIQUE,
    whatsapp_token VARCHAR,
    is_active BOOLEAN DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
""")
print("✅ businesses table ready")

# 2. Insert default business for your existing data
# ⚠️  CHANGE these values to your real WhatsApp credentials!
c.execute("""
INSERT OR IGNORE INTO businesses (id, name, owner_username, owner_password, whatsapp_phone_id, whatsapp_token)
VALUES (1, 'My Business', 'mybusiness', 'mypassword123', '1088821460973757', '"EAAnCPODMFHgBRFKYjzy3TAqpZABcM3ZAmZBmRKgqYfmjE7wS9DYH93mdZB7HRZCZBpV7HETpTG4TXzTW6LFcBrAAonq86cp4lV4e3ZCkkWhZAO3jXfwUx2EwahwLSPZBGsSm5ALsUEnTFf72JIic5ZBI2ZAIFh4lb2GH6koS5LUPbxEScfqqpA4WgJOP3dEZAbZB6HWsr9ZAVxwcIJP48pCvIITsGhBIzcbHJinoZCaS74p134aDqqwn89k2SsRcD8RdMWyPlYBAUJtNZA7SWMbRrbZBWwxBHYwZDZD"')
""")
print("✅ Default business created (id=1)")
print("   Login: mybusiness / mypassword123")

# 3. Add business_id to products
try:
    c.execute("ALTER TABLE products ADD COLUMN business_id INTEGER DEFAULT 1")
    print("✅ products.business_id added")
except:
    print("ℹ️  products.business_id already exists")

# 4. Add business_id to orders
try:
    c.execute("ALTER TABLE orders ADD COLUMN business_id INTEGER DEFAULT 1")
    print("✅ orders.business_id added")
except:
    print("ℹ️  orders.business_id already exists")

# 5. Add business_id to chat_messages
try:
    c.execute("ALTER TABLE chat_messages ADD COLUMN business_id INTEGER DEFAULT 1")
    print("✅ chat_messages.business_id added")
except:
    print("ℹ️  chat_messages.business_id already exists")

# 6. Set all existing rows to business_id=1
c.execute("UPDATE products SET business_id=1 WHERE business_id IS NULL")
c.execute("UPDATE orders SET business_id=1 WHERE business_id IS NULL")
c.execute("UPDATE chat_messages SET business_id=1 WHERE business_id IS NULL")
print("✅ All existing data assigned to business id=1")

conn.commit()
conn.close()

print("\n🎉 Migration complete!")
print("\nNext steps:")
print("1. Open migrate.py and update the token for business id=1")
print("2. Run: uvicorn main:app --reload")
print("3. Login as superadmin / superadmin123 to manage businesses")
print("4. Login as mybusiness / mypassword123 to see your existing data")
