import sqlite3
import datetime

DB_FILE = "bot_data.db"

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_settings():
    conn = get_db()
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        first_name TEXT,
        username TEXT,
        last_interaction TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        first_name TEXT,
        username TEXT,
        type TEXT,
        package_id TEXT,
        package_name TEXT,
        price REAL,
        quantity INTEGER DEFAULT 1,
        delivery_link TEXT DEFAULT '',
        screenshot_file_id TEXT,
        status TEXT DEFAULT 'pending',
        created_at TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type TEXT,
        name TEXT,
        price REAL,
        quantity INTEGER DEFAULT 1,
        delivery_link TEXT DEFAULT '',
        position INTEGER
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS blocked_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        first_name TEXT,
        username TEXT,
        blocked_at TEXT
    )''')
    
    # Default settings
    defaults = {
        "upi_id": "customupi@bank",
        "qr_code": "",
        "how_to_use_video": "",
        "number_buttons_position": "vertical",
        "video_buttons_position": "vertical"
    }
    
    for k, v in defaults.items():
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, str(v)))
    
    conn.commit()
    conn.close()

def get_setting(key):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def update_setting(key, value):
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()

# Users
def save_user(user_id, first_name, username):
    conn = get_db()
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO users (user_id, first_name, username, last_interaction)
                 VALUES (?, ?, ?, ?)''',
              (user_id, first_name, username, datetime.datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_all_users():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users ORDER BY last_interaction DESC")
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def count_users():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    count = c.fetchone()[0]
    conn.close()
    return count

# Products
def add_product(type_name, name, price, quantity_or_link):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM products WHERE type = ?", (type_name,))
    count = c.fetchone()[0]
    
    if type_name == "number":
        c.execute("INSERT INTO products (type, name, price, quantity, position) VALUES (?, ?, ?, ?, ?)",
                  (type_name, name, price, int(quantity_or_link), count + 1))
    else:
        c.execute("INSERT INTO products (type, name, price, delivery_link, position) VALUES (?, ?, ?, ?, ?)",
                  (type_name, name, price, quantity_or_link, count + 1))
    
    conn.commit()
    conn.close()

def get_products(type_name):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM products WHERE type = ? ORDER BY position", (type_name,))
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def get_product(product_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM products WHERE id = ?", (product_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def delete_product(product_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM products WHERE id = ?", (product_id,))
    conn.commit()
    conn.close()

# Orders
def create_order(user_id, first_name, username, p_type, package_id, package_name, price, quantity, delivery_link, screenshot_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('''INSERT INTO orders (user_id, first_name, username, type, package_id, package_name, 
                 price, quantity, delivery_link, screenshot_file_id, status, created_at)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)''',
              (user_id, first_name, username, p_type, package_id, package_name, price, 
               quantity, delivery_link, screenshot_id, datetime.datetime.now().isoformat()))
    order_id = c.lastrowid
    conn.commit()
    conn.close()
    return order_id

def get_pending_order_by_user(user_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM orders WHERE user_id = ? AND status = 'pending'", (user_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def get_order(order_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def update_order_status(order_id, status):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
    conn.commit()
    conn.close()

def get_orders_by_status(status, limit=20):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM orders WHERE status = ? ORDER BY created_at DESC LIMIT ?", (status, limit))
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def count_orders(status=None):
    conn = get_db()
    c = conn.cursor()
    if status:
        c.execute("SELECT COUNT(*) FROM orders WHERE status = ?", (status,))
    else:
        c.execute("SELECT COUNT(*) FROM orders")
    count = c.fetchone()[0]
    conn.close()
    return count

# Blocked users
def block_user(user_id, first_name, username):
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO blocked_users (user_id, first_name, username, blocked_at) VALUES (?, ?, ?, ?)",
              (user_id, first_name, username, datetime.datetime.now().isoformat()))
    conn.commit()
    conn.close()

def unblock_user(block_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM blocked_users WHERE id = ?", (block_id,))
    conn.commit()
    conn.close()

def is_blocked(user_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM blocked_users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row is not None

def get_blocked_users():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM blocked_users ORDER BY blocked_at DESC")
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def count_blocked():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM blocked_users")
    count = c.fetchone()[0]
    conn.close()
    return count

def count_products(type_name):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM products WHERE type = ?", (type_name,))
    count = c.fetchone()[0]
    conn.close()
    return count
