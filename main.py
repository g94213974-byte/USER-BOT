import sqlite3
import datetime
import logging
import os
import threading
import re
import asyncio
import signal
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telethon import TelegramClient, events
from telethon.errors import RPCError

# ===================== CONFIG =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(","))) if os.getenv("ADMIN_IDS") else []
PORT = int(os.getenv("PORT", 10000))

SESSION_DIR = "sessions"
os.makedirs(SESSION_DIR, exist_ok=True)

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is not set!")

# ===================== LOGGING =====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ===================== DATABASE =====================
DB_FILE = "bot_data.db"
_db_lock = threading.Lock()

def get_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, first_name TEXT, username TEXT, last_interaction TEXT, total_spent REAL DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, first_name TEXT, username TEXT, package_id TEXT, package_name TEXT, price REAL, quantity INTEGER DEFAULT 1, screenshot_file_id TEXT, status TEXT DEFAULT 'pending', created_at TEXT, delivered_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, price REAL, quantity INTEGER DEFAULT 1, position INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS blocked_users (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, first_name TEXT, username TEXT, blocked_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS number_pool (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        number TEXT UNIQUE, 
        api_id INTEGER,
        api_hash TEXT,
        session_file TEXT,
        status TEXT DEFAULT 'available',
        sold_to INTEGER DEFAULT NULL,
        sold_at TEXT DEFAULT NULL,
        last_active DATETIME DEFAULT NULL,
        login_valid INTEGER DEFAULT 1
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS active_logins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        number_id INTEGER,
        number TEXT,
        api_id INTEGER,
        api_hash TEXT,
        session_file TEXT,
        status TEXT DEFAULT 'awaiting_otp',
        login_phase TEXT DEFAULT 'first_otp',
        created_at TEXT
    )''')
    
    defaults = {"upi_id": "customupi@bank", "qr_code": ""}
    for k, v in defaults.items():
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, str(v)))
    conn.commit()
    conn.close()

# ===================== DB FUNCTIONS =====================
def get_setting(key):
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = c.fetchone()
        conn.close()
        return row[0] if row else None

def update_setting(key, value):
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
        conn.commit()
        conn.close()

def save_user(user_id, first_name, username):
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO users (user_id, first_name, username, last_interaction) VALUES (?, ?, ?, ?)", 
                  (user_id, first_name, username, datetime.datetime.now().isoformat()))
        conn.commit()
        conn.close()

def count_users():
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM users")
        result = c.fetchone()[0]
        conn.close()
        return result

def get_recent_users(limit=10):
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM users ORDER BY last_interaction DESC LIMIT ?", (limit,))
        results = [dict(row) for row in c.fetchall()]
        conn.close()
        return results

def add_user_spent(user_id, amount):
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE users SET total_spent = COALESCE(total_spent, 0) + ? WHERE user_id = ?", (amount, user_id))
        conn.commit()
        conn.close()

# ===================== PRODUCTS =====================
def add_product(name, price, quantity):
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM products", ())
        count = c.fetchone()[0]
        c.execute("INSERT INTO products (name, price, quantity, position) VALUES (?, ?, ?, ?)", 
                  (name, price, quantity, count + 1))
        conn.commit()
        conn.close()

def get_products():
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM products ORDER BY position")
        results = [dict(row) for row in c.fetchall()]
        conn.close()
        return results

def get_product(product_id):
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM products WHERE id = ?", (product_id,))
        row = c.fetchone()
        conn.close()
        return dict(row) if row else None

def delete_product(product_id):
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("DELETE FROM products WHERE id = ?", (product_id,))
        conn.commit()
        conn.close()

def count_products():
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM products")
        result = c.fetchone()[0]
        conn.close()
        return result

# ===================== NUMBER POOL =====================
def add_number_to_pool(number, api_id, api_hash, session_file):
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        try:
            c.execute(
                "INSERT INTO number_pool (number, api_id, api_hash, session_file, status, login_valid) VALUES (?, ?, ?, ?, 'available', 1)", 
                (number.strip(), api_id, api_hash, session_file)
            )
            conn.commit()
            conn.close()
            return True
        except sqlite3.IntegrityError:
            conn.close()
            return False

def get_available_numbers(limit=100):
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM number_pool WHERE status = 'available' AND login_valid = 1 LIMIT ?", (limit,))
        results = [dict(row) for row in c.fetchall()]
        conn.close()
        return results

def get_available_number_count():
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM number_pool WHERE status = 'available' AND login_valid = 1")
        result = c.fetchone()[0]
        conn.close()
        return result

def get_total_number_count():
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM number_pool")
        result = c.fetchone()[0]
        conn.close()
        return result

def get_sold_number_count():
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM number_pool WHERE status = 'sold'")
        result = c.fetchone()[0]
        conn.close()
        return result

def get_invalid_number_count():
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM number_pool WHERE login_valid = 0")
        result = c.fetchone()[0]
        conn.close()
        return result

def assign_number_for_login(user_id, number_ids, package_name, price):
    """Assign numbers to user, setup for login verification, DON'T mark sold yet"""
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        now = datetime.datetime.now().isoformat()
        assigned_numbers = []
        
        for num_id in number_ids:
            c.execute("SELECT * FROM number_pool WHERE id = ? AND status = 'available' AND login_valid = 1", (num_id,))
            row = c.fetchone()
            if row:
                session_file = f"login_{num_id}_{user_id}.session"
                c.execute("UPDATE number_pool SET status = 'assigned', sold_to = ?, sold_at = ?, last_active = ? WHERE id = ?",
                          (user_id, now, now, num_id))
                assigned_numbers.append({
                    'id': row['id'],
                    'number': row['number'],
                    'api_id': row['api_id'],
                    'api_hash': row['api_hash'],
                    'session_file': session_file
                })
                
                c.execute("INSERT INTO active_logins (user_id, number_id, number, api_id, api_hash, session_file, status, login_phase, created_at) VALUES (?, ?, ?, ?, ?, ?, 'awaiting_otp', 'first_otp', ?)",
                          (user_id, row['id'], row['number'], row['api_id'], row['api_hash'], session_file, now))
        
        conn.commit()
        conn.close()
        return assigned_numbers

def get_active_login_for_user(user_id):
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM active_logins WHERE user_id = ? AND status = 'awaiting_otp' ORDER BY id DESC LIMIT 1", (user_id,))
        row = c.fetchone()
        conn.close()
        return dict(row) if row else None

def get_active_login_by_number(number):
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM active_logins WHERE number = ? AND status = 'awaiting_otp' ORDER BY id DESC LIMIT 1", (number,))
        row = c.fetchone()
        conn.close()
        return dict(row) if row else None

def update_active_login_phase(login_id, phase):
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE active_logins SET login_phase = ? WHERE id = ?", (phase, login_id))
        conn.commit()
        conn.close()

def complete_login_success(login_id, number_id):
    """Mark number as SOLD after successful login"""
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE active_logins SET status = 'success' WHERE id = ?", (login_id,))
        c.execute("UPDATE number_pool SET status = 'sold' WHERE id = ?", (number_id,))
        conn.commit()
        conn.close()

def complete_login_failed(login_id, number_id):
    """Mark number back to AVAILABLE after failed login"""
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE active_logins SET status = 'failed' WHERE id = ?", (login_id,))
        c.execute("UPDATE number_pool SET status = 'available', sold_to = NULL, sold_at = NULL WHERE id = ?", (number_id,))
        conn.commit()
        conn.close()

def mark_number_invalid(number_id):
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE number_pool SET login_valid = 0 WHERE id = ?", (number_id,))
        conn.commit()
        conn.close()

def mark_number_valid(number_id):
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE number_pool SET login_valid = 1 WHERE id = ?", (number_id,))
        conn.commit()
        conn.close()

def get_numbers_by_status(status, limit=30):
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM number_pool WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit))
        results = [dict(row) for row in c.fetchall()]
        conn.close()
        return results

def delete_number_stock(num_id):
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("DELETE FROM number_pool WHERE id = ?", (num_id,))
        conn.commit()
        conn.close()

# ===================== ORDERS =====================
def create_order(user_id, first_name, username, package_id, package_name, price, quantity, screenshot_id):
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute('''INSERT INTO orders (user_id, first_name, username, package_id, package_name, price, quantity, screenshot_file_id, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)''',
                  (user_id, first_name, username, str(package_id), package_name, price, quantity, screenshot_id, datetime.datetime.now().isoformat()))
        order_id = c.lastrowid
        conn.commit()
        conn.close()
        return order_id

def get_pending_order_by_user(user_id):
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM orders WHERE user_id = ? AND status = 'pending'", (user_id,))
        row = c.fetchone()
        conn.close()
        return dict(row) if row else None

def get_order(order_id):
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
        row = c.fetchone()
        conn.close()
        return dict(row) if row else None

def update_order_status(order_id, status):
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        if status == 'delivered':
            c.execute("UPDATE orders SET status = ?, delivered_at = ? WHERE id = ?", (status, datetime.datetime.now().isoformat(), order_id))
        else:
            c.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
        conn.commit()
        conn.close()

def get_orders_by_status(status, limit=20):
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM orders WHERE status = ? ORDER BY created_at DESC LIMIT ?", (status, limit))
        results = [dict(row) for row in c.fetchall()]
        conn.close()
        return results

def count_orders(status=None):
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        if status:
            c.execute("SELECT COUNT(*) FROM orders WHERE status = ?", (status,))
        else:
            c.execute("SELECT COUNT(*) FROM orders")
        result = c.fetchone()[0]
        conn.close()
        return result

def get_total_revenue():
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COALESCE(SUM(price), 0) FROM orders WHERE status = 'delivered'")
        result = c.fetchone()[0]
        conn.close()
        return result

# ===================== BLOCK =====================
def block_user(user_id, first_name, username):
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        existing = c.execute("SELECT * FROM blocked_users WHERE user_id = ?", (user_id,)).fetchone()
        if not existing:
            c.execute("INSERT INTO blocked_users (user_id, first_name, username, blocked_at) VALUES (?, ?, ?, ?)",
                      (user_id, first_name, username, datetime.datetime.now().isoformat()))
            conn.commit()
        conn.close()

def unblock_user(block_id):
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("DELETE FROM blocked_users WHERE id = ?", (block_id,))
        conn.commit()
        conn.close()

def is_blocked(user_id):
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM blocked_users WHERE user_id = ?", (user_id,))
        result = c.fetchone() is not None
        conn.close()
        return result

def get_blocked_users():
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM blocked_users ORDER BY blocked_at DESC")
        results = [dict(row) for row in c.fetchall()]
        conn.close()
        return results

def count_blocked():
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM blocked_users")
        result = c.fetchone()[0]
        conn.close()
        return result

# ===================== SAFE EDIT =====================
async def safe_edit(query, text, reply_markup=None):
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except Exception as e:
        if "Message is not modified" not in str(e):
            logger.error(f"safe_edit error: {e}")

async def safe_edit_caption(query, caption, reply_markup=None):
    try:
        await query.edit_message_caption(caption=caption, reply_markup=reply_markup)
    except Exception as e:
        if "Message is not modified" not in str(e):
            logger.error(f"safe_edit_caption error: {e}")

# ===================== TELETHON OTP MONITOR =====================
async def start_otp_listener_and_wait(number_id, number, api_id, api_hash, session_file, timeout=120):
    """
    Start Telethon client, listen for OTP, wait and return:
    - "OTP:CODE" if OTP received
    - "TIMEOUT" if no OTP in time
    - "LOGIN_FAILED" if login failure message detected
    - "ERROR:msg" if error
    """
    session_path = os.path.join(SESSION_DIR, session_file)
    otp_event = asyncio.Event()
    otp_result = {"value": None}
    
    try:
        client = TelegramClient(session_path, api_id, api_hash)
        await client.connect()
        
        # Check if already authorized (shouldn't be)
        if await client.is_user_authorized():
            logger.warning(f"[{number}] Already authorized unexpectedly")
            await client.disconnect()
            return "ALREADY_AUTHORIZED"
        
        @client.on(events.NewMessage)
        async def handler(event):
            nonlocal otp_result
            try:
                text = event.raw_text
                if not text:
                    return
                
                logger.info(f"📩 [{number}] SMS: {text[:150]}")
                
                # Check for OTP codes
                code_match = re.search(r'(\d{4,8})', text)
                if code_match:
                    otp_result["value"] = f"OTP:{code_match.group(1)}"
                    otp_event.set()
                    return
                
                # Check for login failure messages
                fail_patterns = [
                    r'incomplete login',
                    r'failed login attempt',
                    r'nobody gained access',
                    r'incorrect password',
                    r'password was not given',
                    r'terminate the incomplete',
                ]
                for p in fail_patterns:
                    if re.search(p, text, re.IGNORECASE):
                        otp_result["value"] = "LOGIN_FAILED"
                        otp_event.set()
                        return
                
                # Also check for login success
                success_patterns = [
                    r'logged in',
                    r'login successful',
                    r'successfully logged',
                ]
                for p in success_patterns:
                    if re.search(p, text, re.IGNORECASE):
                        otp_result["value"] = "LOGIN_SUCCESS"
                        otp_event.set()
                        return
                        
            except Exception as e:
                logger.error(f"[{number}] Handler error: {e}")
        
        # Wait for OTP
        try:
            await asyncio.wait_for(otp_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            otp_result["value"] = "TIMEOUT"
        
        await client.disconnect()
        return otp_result["value"]
        
    except Exception as e:
        logger.error(f"[{number}] Listener error: {e}")
        mark_number_invalid(number_id)
        return f"ERROR:{str(e)[:50]}"

# ===================== BOT HANDLERS =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    save_user(user.id, user.first_name, user.username)
    if is_blocked(user.id):
        await update.message.reply_text("⛔ আপনি ব্লক করা হয়েছেন।")
        return
    
    keyboard = [
        [InlineKeyboardButton("📱 Buy Number", callback_data="buy_number")],
        [InlineKeyboardButton("📖 How To Use", callback_data="how_to_use")]
    ]
    if user.id in ADMIN_IDS:
        keyboard.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_panel")])
    
    await update.message.reply_text(
        "👋 **স্বাগতম নম্বর বটে!**\n\n"
        "📱 ভার্চুয়াল নম্বর কিনুন\n"
        "✅ OTP রিসিভ করুন বটের মাধ্যমে\n"
        "🔐 সম্পূর্ণ অটোমেটিক\n\n"
        "নিচ থেকে সিলেক্ট করুন:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if is_blocked(user_id):
        await safe_edit(query, "⛔ আপনি ব্লক করা হয়েছেন।")
        return
    data = query.data
    
    if data == "how_to_use":
        await show_how_to_use(query, context)
    elif data == "buy_number":
        await show_products(query, context)
    elif data.startswith("pkg_"):
        pkg_id = int(data.replace("pkg_", ""))
        await show_payment(query, context, pkg_id)
    elif data == "back_main":
        await back_to_main(query, context)
    elif data == "back_products":
        await show_products(query, context)
    elif data == "cancel_payment":
        context.user_data["waiting_for_screenshot"] = False
        context.user_data["pending_pkg_id"] = None
        await safe_edit(query, "❌ বাতিল করা হয়েছে।")
    elif data.startswith("pay_"):
        pkg_id = int(data.replace("pay_", ""))
        context.user_data["pending_pkg_id"] = pkg_id
        context.user_data["waiting_for_screenshot"] = True
        await safe_edit(query, "📸 পেমেন্টের স্ক্রিনশট পাঠান।\n\n❌ Cancel:", 
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_payment")]]))
    elif data.startswith("approve_"):
        await approve_order(query, context, int(data.replace("approve_", "")))
    elif data.startswith("reject_"):
        await reject_order(query, context, int(data.replace("reject_", "")))
    elif data.startswith("block_"):
        await block_user_from_order(query, context, int(data.replace("block_", "")))
    elif data.startswith("unblock_"):
        unblock_user(int(data.replace("unblock_", "")))
        await safe_edit(query, "✅ আনব্লক করা হয়েছে!")
    elif data == "start_login":
        # User requests OTP - start listener
        await handle_start_login(query, context)
    elif data == "gate_otp":
        # User clicked "Gate OTP" - needs new OTP
        await handle_gate_otp(query, context)
    elif data == "check_login":
        # Check if login was successful
        await handle_check_login(query, context)
    elif data == "admin_panel":
        if user_id in ADMIN_IDS:
            await show_admin_panel(query, context)
    elif data.startswith("del_product_"):
        if user_id in ADMIN_IDS:
            pid = int(data.replace("del_product_", ""))
            delete_product(pid)
            await safe_edit(query, "✅ ডিলিট করা হয়েছে!")
            await admin_products(query, context)
    elif data == "admin_products":
        if user_id in ADMIN_IDS:
            await admin_products(query, context)
    elif data == "admin_stock":
        if user_id in ADMIN_IDS:
            await admin_stock(query, context)
    elif data == "admin_add_numbers":
        if user_id in ADMIN_IDS:
            context.user_data["waiting_for_numbers_bulk"] = True
            await safe_edit(query, 
                "✏️ **নম্বর যোগ করুন**\n\n"
                "ফরম্যাট:\n"
                "`ফোন:এপিআই_আইডি:এপিআই_হ্যাশ`\n\n"
                "উদাহরণ:\n"
                "`+8801234567890:123456:abcdef`\n\n"
                "❌ /cancel")
    elif data == "admin_sold":
        if user_id in ADMIN_IDS:
            await admin_sold(query, context)
    elif data == "admin_payment":
        if user_id in ADMIN_IDS:
            await show_admin_payment(query, context)
    elif data == "admin_qr":
        if user_id in ADMIN_IDS:
            await show_admin_qr(query, context)
    elif data == "admin_pending":
        if user_id in ADMIN_IDS:
            await show_admin_orders(query, context, "pending")
    elif data == "admin_delivered":
        if user_id in ADMIN_IDS:
            await show_admin_orders(query, context, "delivered")
    elif data == "admin_rejected":
        if user_id in ADMIN_IDS:
            await show_admin_orders(query, context, "rejected")
    elif data == "admin_blocked":
        if user_id in ADMIN_IDS:
            await show_blocked_users(query, context)
    elif data == "admin_users":
        if user_id in ADMIN_IDS:
            await show_users(query, context)
    elif data == "admin_stats":
        if user_id in ADMIN_IDS:
            await show_stats(query, context)
    elif data == "edit_upi":
        if user_id in ADMIN_IDS:
            context.user_data["waiting_for_upi"] = True
            await safe_edit(query, "✏️ নতুন UPI ID পাঠান:\nউদাহরণ: `yourupi@paytm`\n\n❌ /cancel")
    elif data == "edit_qr":
        if user_id in ADMIN_IDS:
            context.user_data["waiting_for_qr"] = True
            await safe_edit(query, "📷 QR ইমেজ পাঠান:\n\n❌ /cancel")
    elif data == "add_product":
        if user_id in ADMIN_IDS:
            context.user_data["waiting_for_product_name"] = True
            await safe_edit(query, "✏️ প্যাকেজের নাম:\nউদাহরণ: `5 Numbers`\n\n❌ /cancel")

async def show_how_to_use(query, context):
    await safe_edit(query, 
        "📖 **কিভাবে কাজ করে**\n\n"
        "🔹 নম্বর কিনুন\n"
        "🔹 **Start Login** বাটনে ক্লিক করুন\n"
        "🔹 আপনার নম্বরে Telegram থেকে **OTP পাঠাবে**\n"
        "🔹 বট সেই OTP **ডিটেক্ট করে আপনাকে দেবে**\n"
        "🔹 আপনি OTP বসিয়ে লগইন করুন\n"
        "🔹 লগইন সম্পন্ন হলে **নম্বরটি আপনার হয়ে যাবে**\n\n"
        "⚠️ **নোট:**\n"
        "• OTP না পেলে **Gate OTP** বাটনে ক্লিক করুন\n"
        "• ২ মিনিটের মধ্যে OTP দিন\n"
        "• সফল লগইনের পর নম্বর **আপনার**, আর কাউকে দেওয়া হবে না",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_main")]])
    )

async def show_products(query, context):
    products = get_products()
    available = get_available_number_count()
    
    text = f"📱 **নম্বর প্যাকেজ**\n\n🟢 স্টকে: {available} টি\n\n"
    
    if not products:
        text += "❌ কোনো প্যাকেজ নেই।"
        await safe_edit(query, text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_main")]]))
        return
    
    keyboard = []
    row = []
    for p in products:
        row.append(InlineKeyboardButton(f"{p['name']} - ₹{p['price']}", callback_data=f"pkg_{p['id']}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_main")])
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard))

async def show_payment(query, context, pkg_id):
    product = get_product(pkg_id)
    if not product:
        await safe_edit(query, "❌ প্যাকেজ পাওয়া যায়নি।")
        return
    
    available = get_available_number_count()
    if available < product['quantity']:
        await safe_edit(query, f"❌ **স্টক শেষ!**\nপ্রয়োজন: {product['quantity']} টি\nস্টকে: {available} টি")
        return
    
    upi_id = get_setting("upi_id") or "customupi@bank"
    qr_code = get_setting("qr_code")
    
    payment_text = (
        f"💳 **পেমেন্ট**\n\n"
        f"📦 {product['name']}\n"
        f"💰 ₹{product['price']}\n"
        f"📦 {product['quantity']} টি নম্বর\n"
        f"🏦 UPI: `{upi_id}`\n\n"
        f"পেমেন্ট করে নিচের বাটনে ক্লিক করুন:"
    )
    
    keyboard = [
        [InlineKeyboardButton("📸 I Have Paid", callback_data=f"pay_{pkg_id}")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_products")]
    ]
    
    if qr_code:
        try:
            try: await query.message.delete()
            except: pass
            await context.bot.send_photo(chat_id=query.message.chat_id, photo=qr_code, caption=payment_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
            return
        except:
            pass
    
    try: await query.message.delete()
    except: pass
    await context.bot.send_message(chat_id=query.message.chat_id, text=payment_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

# ===================== MESSAGE HANDLER =====================
async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    if update.message.text and update.message.text.strip() == "/cancel":
        for key in list(context.user_data.keys()):
            if key.startswith("waiting_for_"):
                context.user_data[key] = False
        await update.message.reply_text("❌ বাতিল করা হয়েছে।")
        return
    
    # ADMIN: ADD BULK NUMBERS
    if context.user_data.get("waiting_for_numbers_bulk") and user.id in ADMIN_IDS:
        text = update.message.text.strip()
        lines = text.strip().split('\n')
        added = 0
        failed = 0
        
        for line in lines:
            line = line.strip()
            if not line: continue
            parts = line.split(':')
            if len(parts) >= 3:
                phone = parts[0].strip()
                try:
                    api_id = int(parts[1].strip())
                    api_hash = parts[2].strip()
                    session_file = f"session_{phone.replace('+', '')}.session"
                    if add_number_to_pool(phone, api_id, api_hash, session_file):
                        added += 1
                    else:
                        failed += 1
                except ValueError:
                    failed += 1
            else:
                failed += 1
        
        context.user_data["waiting_for_numbers_bulk"] = False
        await update.message.reply_text(
            f"✅ **নম্বর যোগ!**\n"
            f"➕ {added}\n❌ {failed}\n"
            f"📊 মোট: {get_total_number_count()}\n🟢 উপলব্ধ: {get_available_number_count()}"
        )
        return
    
    # ADMIN: ADD PRODUCT
    if context.user_data.get("waiting_for_product_name") and user.id in ADMIN_IDS:
        name = update.message.text.strip()
        if name.isdigit():
            await update.message.reply_text("❌ নাম লিখুন। উদাহরণ: `5 Numbers`\n\n❌ /cancel")
            return
        context.user_data["new_product_name"] = name
        context.user_data["waiting_for_product_name"] = False
        context.user_data["waiting_for_product_price"] = True
        await update.message.reply_text("✏️ দাম (₹):\nউদাহরণ: `50`\n\n❌ /cancel")
        return
    
    if context.user_data.get("waiting_for_product_price") and user.id in ADMIN_IDS:
        try:
            price = float(update.message.text.strip())
            if price <= 0:
                await update.message.reply_text("❌ দাম ০ এর বেশি হতে হবে!\n\n❌ /cancel")
                return
            context.user_data["new_product_price"] = price
            context.user_data["waiting_for_product_price"] = False
            context.user_data["waiting_for_product_qty"] = True
            await update.message.reply_text("✏️ কয়টি নম্বর:\nউদাহরণ: `5`\n\n❌ /cancel")
        except ValueError:
            await update.message.reply_text("❌ দাম একটি সংখ্যা হতে হবে।\n\n❌ /cancel")
        return
    
    if context.user_data.get("waiting_for_product_qty") and user.id in ADMIN_IDS:
        try:
            qty = int(update.message.text.strip())
            if qty <= 0:
                await update.message.reply_text("❌ সংখ্যা ০ এর বেশি হতে হবে!\n\n❌ /cancel")
                return
            name = context.user_data.get("new_product_name", "Package")
            price = context.user_data.get("new_product_price", 0)
            add_product(name, price, qty)
            context.user_data["waiting_for_product_qty"] = False
            await update.message.reply_text(f"✅ **প্যাকেজ যোগ!**\n📱 {name}\n💰 ₹{price}\n📦 {qty} টি")
        except ValueError:
            await update.message.reply_text("❌ সংখ্যা ইনপুট সঠিক নয়।\n\n❌ /cancel")
        return
    
    # ADMIN: SETTINGS
    if context.user_data.get("waiting_for_upi") and user.id in ADMIN_IDS:
        new_upi = update.message.text.strip()
        if "@" not in new_upi:
            await update.message.reply_text("❌ UPI ID তে '@' থাকতে হবে।\n`upi@paytm`\n\n❌ /cancel")
            return
        update_setting("upi_id", new_upi)
        context.user_data["waiting_for_upi"] = False
        await update.message.reply_text(f"✅ **UPI আপডেট!**\n`{new_upi}`")
        return
    
    if context.user_data.get("waiting_for_qr") and user.id in ADMIN_IDS:
        if update.message.photo:
            update_setting("qr_code", update.message.photo[-1].file_id)
            context.user_data["waiting_for_qr"] = False
            await update.message.reply_text("✅ **QR আপডেট!**")
        else:
            await update.message.reply_text("❌ ছবি পাঠান।\n\n❌ /cancel")
        return
    
    # USER: PAYMENT SCREENSHOT
    if context.user_data.get("waiting_for_screenshot"):
        if not update.message.photo:
            await update.message.reply_text("📸 পেমেন্টের স্ক্রিনশট পাঠান (ছবি)।")
            return
        
        pending = get_pending_order_by_user(user.id)
        if pending:
            await update.message.reply_text("⏳ আপনার ইতিমধ্যে পেন্ডিং অর্ডার আছে।")
            return
        
        photo = update.message.photo[-1]
        pkg_id = context.user_data.get("pending_pkg_id")
        product = get_product(pkg_id) if pkg_id else None
        
        if not product:
            await update.message.reply_text("❌ প্যাকেজ পাওয়া যায়নি। /start")
            return
        
        order_id = create_order(
            user.id, user.first_name, user.username, 
            pkg_id, product['name'], 
            product['price'], product.get('quantity', 1), 
            photo.file_id
        )
        
        context.user_data["waiting_for_screenshot"] = False
        context.user_data["pending_pkg_id"] = None
        
        await update.message.reply_text(
            "✅ **পেমেন্ট রিসিভ!**\n\n⏳ **৫-৩০ মিনিট** অপেক্ষা করুন।\nঅ্যাডমিন ভেরিফাই করে নম্বর দেবেন।"
        )
        
        await notify_admins(context, user, product, photo.file_id, order_id)
        return

async def notify_admins(context, user, product, screenshot_id, order_id):
    text = (
        f"\n📱 **নতুন অর্ডার!**\n\n"
        f"👤 {user.first_name}\n"
        f"🆔 `{user.id}`\n"
        f"📛 @{user.username or 'N/A'}\n"
        f"📦 {product['name']}\n"
        f"💰 ₹{product['price']}\n"
        f"📦 {product.get('quantity', 1)} টি"
    )
    
    keyboard = [
        [InlineKeyboardButton("✅ APPROVE", callback_data=f"approve_{order_id}"), 
         InlineKeyboardButton("❌ REJECT", callback_data=f"reject_{order_id}")],
        [InlineKeyboardButton("🚫 BLOCK", callback_data=f"block_{order_id}")]
    ]
    
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_photo(
                chat_id=admin_id, 
                photo=screenshot_id, 
                caption=text, 
                reply_markup=InlineKeyboardMarkup(keyboard), 
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Admin notify: {e}")

async def approve_order(query, context, order_id):
    order = get_order(order_id)
    if not order:
        await safe_edit(query, "❌ অর্ডার পাওয়া যায়নি।")
        return
    
    user_id = order["user_id"]
    qty = order.get("quantity", 1)
    
    available = get_available_numbers(qty)
    if len(available) < qty:
        await safe_edit_caption(query, caption=f"⚠️ স্টক শেষ! প্রয়োজন {qty}, আছে {len(available)}")
        try:
            await context.bot.send_message(chat_id=user_id, text="❌ পর্যাপ্ত নম্বর নেই। পরে আসবেন।")
        except:
            pass
        return
    
    # Assign numbers for login (NOT sold yet!)
    num_ids = [n['id'] for n in available[:qty]]
    assigned = assign_number_for_login(user_id, num_ids, order['package_name'], order['price'])
    
    if not assigned:
        await safe_edit_caption(query, caption="❌ অ্যাসাইন ব্যর্থ।")
        return
    
    update_order_status(order_id, "delivered")
    add_user_spent(user_id, order['price'])
    
    # Send number to user with Start Login button
    text = "✅ **অর্ডার অ্যাপ্রুভড!** ✅\n\n"
    text += "📋 **আপনার নম্বর:**\n\n"
    
    for i, num in enumerate(assigned, 1):
        text += f"━━━━━━━━━━━━━\n📱 নম্বর {i}: `{num['number']}`\n"
    
    text += (
        "━━━━━━━━━━━━━\n\n"
        "🔰 **এখন লগইন করতে নিচের বাটনে ক্লিক করুন**\n\n"
        "📌 ১. Telegram অ্যাপ খুলুন\n"
        "📌 ২. এই নম্বর দিয়ে লগইন করুন\n"
        "📌 ৩. Telegram OTP পাঠাবে - বট সেটা ডিটেক্ট করবে\n"
        "📌 ৪. বট থেকে OTP নিয়ে বসান\n"
        "📌 ৫. লগইন সম্পন্ন করুন!\n\n"
        "👇 **Start Login** বাটনে ক্লিক করুন"
    )
    
    keyboard = [[InlineKeyboardButton("📲 Start Login", callback_data="start_login")]]
    
    try:
        await context.bot.send_message(chat_id=user_id, text=text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        logger.error(f"Send to user failed: {e}")
    
    num_list = ", ".join([f"`{n['number']}`" for n in assigned])
    await safe_edit_caption(query, caption=f"✅ **অ্যাপ্রুভড!**\n{order['package_name']}\n👤 {order['first_name']}\n📱 {num_list}")

async def handle_start_login(query, context):
    """Start listening for OTP"""
    user_id = query.from_user.id
    active = get_active_login_for_user(user_id)
    
    if not active:
        await safe_edit(query, 
            "❌ **কোনো সক্রিয় লগইন নেই!**\n\nআপনি এখনো নম্বর কেনেননি। /start",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_main")]])
        )
        return
    
    number = active['number']
    number_id = active['number_id']
    
    await safe_edit(query, 
        "⏳ **OTP এর জন্য অপেক্ষা করুন...**\n\n"
        f"📱 নম্বর: `{number}`\n\n"
        "🔴 **Telegram অ্যাপে গিয়ে লগইন করুন**\n"
        "Telegram আপনার নম্বরে **OTP পাঠাবে**\n"
        "বট সেটা ডিটেক্ট করে আপনাকে জানাবে।\n\n"
        "⏱️ সর্বোচ্চ **২ মিনিট** সময়",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Check Status", callback_data="check_login")]])
    )
    
    # Run OTP listener in background
    asyncio.create_task(
        run_otp_listener(context, active['id'], user_id, number_id, number, 
                        active['api_id'], active['api_hash'], active['session_file'],
                        query.message.chat_id, query.message.message_id, 'first_otp')
    )

async def handle_gate_otp(query, context):
    """User clicked Gate OTP - they need a new OTP code"""
    user_id = query.from_user.id
    active = get_active_login_for_user(user_id)
    
    if not active:
        await safe_edit(query, 
            "❌ **কোনো সক্রিয় লগইন নেই!**",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_main")]])
        )
        return
    
    # Update phase
    update_active_login_phase(active['id'], 'gate_otp')
    
    number = active['number']
    
    await safe_edit(query, 
        "🔑 **নতুন OTP এর জন্য অপেক্ষা করুন...**\n\n"
        f"📱 নম্বর: `{number}`\n\n"
        "Telegram অ্যাপে **পুনরায় OTP রিকোয়েস্ট করুন**\n"
        "(Gate OTP বাটনে ক্লিক করুন)\n\n"
        "বট নতুন OTP ডিটেক্ট করবে...\n\n"
        "⏱️ সর্বোচ্চ **২ মিনিট**",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Check Status", callback_data="check_login")]])
    )
    
    # Run OTP listener again
    asyncio.create_task(
        run_otp_listener(context, active['id'], user_id, active['number_id'], number,
                        active['api_id'], active['api_hash'], active['session_file'],
                        query.message.chat_id, query.message.message_id, 'gate_otp')
    )

async def run_otp_listener(context, login_id, user_id, number_id, number, api_id, api_hash, session_file, chat_id, message_id, phase):
    """Background task to listen for OTP"""
    try:
        result = await start_otp_listener_and_wait(number_id, number, api_id, api_hash, session_file, timeout=120)
        
        if result and result.startswith("OTP:"):
            otp_code = result.replace("OTP:", "")
            logger.info(f"✅ [{number}] OTP received: {otp_code}")
            
            update_active_login_phase(login_id, f"{phase}_done")
            
            # Send OTP to user with Gate OTP option
            text = (
                f"✅ **OTP রিসিভ!** ✅\n\n"
                f"📱 নম্বর: `{number}`\n"
                f"🔑 **OTP কোড: `{otp_code}`**\n\n"
                f"⚡ **এখনই ব্যবহার করুন!**\n\n"
                f"Telegram অ্যাপে এই OTP বসান।\n\n"
                f"⚠️ OTP **২ মিনিট** এর মধ্যে এক্সপায়ার হবে!\n\n"
                f"✅ লগইন সম্পন্ন হলে নিচের বাটনে ক্লিক করুন\n"
                f"অথবা নতুন OTP চাইলে Gate OTP ক্লিক করুন"
            )
            
            keyboard = [
                [InlineKeyboardButton("✅ Login Done - Check", callback_data="check_login")],
                [InlineKeyboardButton("🔄 Gate OTP (New Code)", callback_data="gate_otp")]
            ]
            
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    parse_mode='Markdown',
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            except:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode='Markdown',
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
        
        elif result == "LOGIN_FAILED":
            logger.info(f"⚠️ [{number}] Login failed message detected")
            
            # Force logout and release number
            complete_login_failed(login_id, number_id)
            
            session_path = os.path.join(SESSION_DIR, session_file)
            try:
                client = TelegramClient(session_path, api_id, api_hash)
                await client.connect()
                await client.log_out()
                await client.disconnect()
            except:
                pass
            
            fail_text = (
                f"❌ **লগইন ব্যর্থ!** ❌\n\n"
                f"📱 নম্বর: `{number}`\n\n"
                f"⚠️ **কারণ:** Telegram থেকে রিপোর্ট এসেছে যে\n"
                f"লগইন সম্পন্ন হয়নি (পাসওয়ার্ড প্রয়োজন/ভুল)\n\n"
                f"❌ অ্যাকাউন্ট **লগআউট** করা হয়েছে!\n"
                f"নম্বরটি আবার **স্টকে** ফেরত দেওয়া হয়েছে।\n\n"
                f"📞 নতুন করে কিনতে **/start** দিন"
            )
            
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=fail_text,
                    parse_mode='Markdown'
                )
            except:
                await context.bot.send_message(chat_id=chat_id, text=fail_text, parse_mode='Markdown')
            
            # Notify admins
            for admin_id in ADMIN_IDS:
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=f"⚠️ লগইন ব্যর্থ! নম্বর স্টকে ফেরত\n📱 `{number}`\n👤 `{user_id}`",
                        parse_mode='Markdown'
                    )
                except:
                    pass
        
        elif result == "LOGIN_SUCCESS":
            logger.info(f"✅ [{number}] Login success message detected!")
            
            complete_login_success(login_id, number_id)
            
            success_text = (
                f"✅ **লগইন সফল!** ✅\n\n"
                f"📱 নম্বর: `{number}`\n\n"
                f"🎉 **অভিনন্দন!**\n\n"
                f"এই নম্বরটি এখন **আপনার**!\n"
                f"✅ এটি আর কাউকে দেওয়া হবে না।\n"
                f"🔒 সম্পূর্ণ নিরাপদ।\n\n"
                f"📌 এখন আপনি Telegram ইউজ করতে পারবেন।\n\n"
                f"❤️ **ধন্যবাদ!**"
            )
            
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=success_text,
                    parse_mode='Markdown'
                )
            except:
                await context.bot.send_message(chat_id=chat_id, text=success_text, parse_mode='Markdown')
            
            # Notify admins
            for admin_id in ADMIN_IDS:
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=f"✅ লগইন সফল! নম্বর সোল্ড!\n📱 `{number}`\n👤 `{user_id}`\n💰 নম্বরটি বিক্রি হয়ে গেছে!",
                        parse_mode='Markdown'
                    )
                except:
                    pass
        
        elif result == "TIMEOUT":
            timeout_text = (
                f"⏰ **টাইমআউট!** ⏰\n\n"
                f"📱 নম্বর: `{number}`\n\n"
                f"❌ ২ মিনিটের মধ্যে কোনো OTP আসেনি!\n\n"
                f"কারণ:\n"
                f"• Telegram অ্যাপে লগইন করেননি\n"
                f"• অথবা নম্বরটি ব্লকেড\n\n"
                f"🔄 আবার চেষ্টা করুন:"
            )
            
            keyboard = [[InlineKeyboardButton("🔄 Start Login Again", callback_data="start_login")]]
            
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=timeout_text,
                    parse_mode='Markdown',
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            except:
                pass
        
        else:
            # ERROR
            err_text = (
                f"❌ **এরর!**\n\n"
                f"📱 নম্বর: `{number}`\n\n"
                f"টেকনিক্যাল সমস্যা হয়েছে।\n"
                f"সাপোর্টে জানান।\n\n"
                f"Error: {result}"
            )
            
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=err_text,
                    parse_mode='Markdown'
                )
            except:
                pass
    
    except Exception as e:
        logger.error(f"OTP listener error: {e}")
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"❌ এরর!\n\n{str(e)[:100]}",
                parse_mode='Markdown'
            )
        except:
            pass

async def handle_check_login(query, context):
    """User says login is done - check if authorized"""
    user_id = query.from_user.id
    active = get_active_login_for_user(user_id)
    
    if not active:
        # Maybe it's already marked success/failed
        await safe_edit(query, 
            "⏳ লগইন চেক করা হচ্ছে...\n\nআপনি সম্ভবত ইতিমধ্যে লগইন করেছেন।\nনতুন করে /start দিন।",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_main")]])
        )
        return
    
    number = active['number']
    session_path = os.path.join(SESSION_DIR, active['session_file'])
    
    await safe_edit(query, 
        "🔍 **চেক করা হচ্ছে...**\n\nঅনুগ্রহপূর্বক অপেক্ষা করুন...",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Check Again", callback_data="check_login")]])
    )
    
    try:
        client = TelegramClient(session_path, active['api_id'], active['api_hash'])
        await client.connect()
        
        if await client.is_user_authorized():
            # Login successful!
            me = await client.get_me()
            complete_login_success(active['id'], active['number_id'])
            await client.disconnect()
            
            success_text = (
                f"✅ **লগইন সফল!** ✅\n\n"
                f"📱 নম্বর: `{number}`\n"
                f"👤 নাম: {me.first_name}\n\n"
                f"🎉 **অভিনন্দন!**\n"
                f"এই নম্বরটি এখন **আপনার**!\n"
                f"✅ আর কাউকে দেওয়া হবে না।\n\n"
                f"❤️ ধন্যবাদ!"
            )
            
            try:
                await context.bot.edit_message_text(
                    chat_id=query.message.chat_id,
                    message_id=query.message.message_id,
                    text=success_text,
                    parse_mode='Markdown'
                )
            except:
                await context.bot.send_message(chat_id=query.message.chat_id, text=success_text, parse_mode='Markdown')
            
            for admin_id in ADMIN_IDS:
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=f"✅ লগইন সফল! নম্বর সোল্ড!\n📱 `{number}`\n👤 `{user_id}`",
                        parse_mode='Markdown'
                    )
                except:
                    pass
        else:
            # Not authorized yet
            not_ready_text = (
                f"❌ **লগইন সম্পন্ন হয়নি!**\n\n"
                f"📱 নম্বর: `{number}`\n\n"
                f"আপনি এখনো OTP বসিয়ে লগইন করেননি\nঅথবা লগইন ব্যর্থ হয়েছে।\n\n"
                f"🔄 আবার চেষ্টা করুন বা Gate OTP নিন:"
            )
            
            keyboard = [
                [InlineKeyboardButton("🔄 Gate OTP", callback_data="gate_otp")],
                [InlineKeyboardButton("📲 Start Over", callback_data="start_login")]
            ]
            
            try:
                await context.bot.edit_message_text(
                    chat_id=query.message.chat_id,
                    message_id=query.message.message_id,
                    text=not_ready_text,
                    parse_mode='Markdown',
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            except:
                pass
            
            await client.disconnect()
    
    except Exception as e:
        logger.error(f"Check login error: {e}")
        await safe_edit(query, 
            f"❌ চেক করতে সমস্যা!\n\n{str(e)[:50]}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Try Again", callback_data="check_login")]])
        )

async def reject_order(query, context, order_id):
    order = get_order(order_id)
    if not order:
        await safe_edit(query, "❌ অর্ডার পাওয়া যায়নি।")
        return
    
    update_order_status(order_id, "rejected")
    
    try:
        await context.bot.send_message(chat_id=order["user_id"], text="❌ **পেমেন্ট রিজেক্ট!** সাপোর্টে যোগাযোগ করুন।")
    except:
        pass
    
    await safe_edit_caption(query, caption=f"❌ **রিজেক্টেড!**\n{order['package_name']}\n{order['first_name']}")

async def block_user_from_order(query, context, order_id):
    order = get_order(order_id)
    if not order:
        await safe_edit(query, "❌ অর্ডার পাওয়া যায়নি।")
        return
    
    block_user(order["user_id"], order["first_name"], order.get("username", ""))
    update_order_status(order_id, "blocked")
    await safe_edit_caption(query, caption=f"🚫 **ব্লক!**\n{order['first_name']}")

async def back_to_main(query, context):
    keyboard = [
        [InlineKeyboardButton("📱 Buy Number", callback_data="buy_number")],
        [InlineKeyboardButton("📖 How To Use", callback_data="how_to_use")]
    ]
    if query.from_user.id in ADMIN_IDS:
        keyboard.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_panel")])
    
    try: await query.message.delete()
    except: pass
    await context.bot.send_message(chat_id=query.message.chat_id, text="👋 **স্বাগতম!**", reply_markup=InlineKeyboardMarkup(keyboard))

# ===================== ADMIN UI =====================
async def show_admin_panel(query, context):
    if query.from_user.id not in ADMIN_IDS:
        return
    
    keyboard = [
        [InlineKeyboardButton("📦 Products", callback_data="admin_products"), InlineKeyboardButton("📱 Stock", callback_data="admin_stock")],
        [InlineKeyboardButton("💳 Payment", callback_data="admin_payment"), InlineKeyboardButton("📷 QR", callback_data="admin_qr")],
        [InlineKeyboardButton("📦 Pending", callback_data="admin_pending"), InlineKeyboardButton("✅ Delivered", callback_data="admin_delivered")],
        [InlineKeyboardButton("❌ Rejected", callback_data="admin_rejected"), InlineKeyboardButton("🚫 Blocked", callback_data="admin_blocked")],
        [InlineKeyboardButton("👥 Users", callback_data="admin_users"), InlineKeyboardButton("📊 Stats", callback_data="admin_stats")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
    ]
    
    await safe_edit(query, "⚙️ **Admin Panel**", reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_products(query, context):
    if query.from_user.id not in ADMIN_IDS:
        return
    
    products = get_products()
    text = "📦 **প্যাকেজ সমূহ**\n\n"
    
    if not products:
        text += "কোনো প্যাকেজ নেই।"
    else:
        for p in products:
            text += f"• **#{p['id']}**: {p['name']}\n  💰 ₹{p['price']} | 📦 {p.get('quantity', 1)} টি\n"
    
    keyboard = []
    row = []
    for p in products:
        row.append(InlineKeyboardButton(f"🗑️ #{p['id']}", callback_data=f"del_product_{p['id']}"))
        if len(row) >= 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    
    keyboard.append([InlineKeyboardButton("➕ Add Package", callback_data="add_product")])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])
    
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_stock(query, context):
    if query.from_user.id not in ADMIN_IDS:
        return
    
    text = (
        f"📱 **নম্বর স্টক**\n\n"
        f"📊 মোট: {get_total_number_count()}\n"
        f"🟢 উপলব্ধ: {get_available_number_count()}\n"
        f"🔴 সোল্ড: {get_sold_number_count()}\n"
        f"🚫 ইনভ্যালিড: {get_invalid_number_count()}\n\n"
    )
    
    nums = get_numbers_by_status('available', 10)
    if nums:
        text += "**উপলব্ধ:**\n"
        for n in nums:
            text += f"🆔 #{n['id']} • `{n['number']}`\n"
    
    keyboard = [
        [InlineKeyboardButton("➕ Add Numbers", callback_data="admin_add_numbers")],
    ]
    if get_sold_number_count() > 0:
        keyboard.append([InlineKeyboardButton("🔴 Sold", callback_data="admin_sold")])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])
    
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_sold(query, context):
    if query.from_user.id not in ADMIN_IDS:
        return
    
    nums = get_numbers_by_status('sold', 30)
    text = "🔴 **সোল্ড নম্বর**\n\n"
    
    if not nums:
        text += "কিছু নেই।"
    else:
        for n in nums:
            text += f"📱 `{n['number']}`\n  👤 `{n['sold_to']}`\n  🕐 {n['sold_at'][:19]}\n\n"
    
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_stock")]]))

async def show_admin_payment(query, context):
    if query.from_user.id not in ADMIN_IDS:
        return
    
    upi = get_setting("upi_id") or "customupi@bank"
    text = f"💳 **পেমেন্ট**\n\n🏦 UPI: `{upi}`"
    
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Change", callback_data="edit_upi")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]
    ]))

async def show_admin_qr(query, context):
    if query.from_user.id not in ADMIN_IDS:
        return
    
    qr = get_setting("qr_code")
    text = "📷 **QR**\n\n" + ("✅ সেট করা আছে।" if qr else "❌ সেট করা নেই।")
    
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Set", callback_data="edit_qr")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]
    ]))

async def show_admin_orders(query, context, status):
    if query.from_user.id not in ADMIN_IDS:
        return
    
    orders = get_orders_by_status(status)
    emoji = {"pending": "📦", "delivered": "✅", "rejected": "❌"}.get(status, "📋")
    status_bn = {"pending": "পেন্ডিং", "delivered": "ডেলিভারি", "rejected": "রিজেক্টেড"}.get(status, status)
    
    text = f"{emoji} **{status_bn} অর্ডার**\n\n"
    
    if not orders:
        text += "কিছু নেই।"
    else:
        for o in orders:
            text += f"• {o['first_name']} - {o['package_name']} - ₹{o['price']}\n  🆔 {o['id']} | {o['created_at'][:19]}\n\n"
    
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]))

async def show_blocked_users(query, context):
    if query.from_user.id not in ADMIN_IDS:
        return
    
    blocked = get_blocked_users()
    text = "🚫 **ব্লক করা ইউজার**\n\n"
    
    if not blocked:
        text += "কেউ নেই।"
        await safe_edit(query, text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]))
        return
    
    keyboard = []
    row = []
    for b in blocked:
        row.append(InlineKeyboardButton(f"🔓 {b['first_name']}", callback_data=f"unblock_{b['id']}"))
        if len(row) >= 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    
    for b in blocked:
        text += f"• {b['first_name']} (@{b.get('username','N/A')}) - `{b['user_id']}`\n"
    
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard))

async def show_users(query, context):
    if query.from_user.id not in ADMIN_IDS:
        return
    
    text = f"👥 **মোট ইউজার:** {count_users()}\n\n**সাম্প্রতিক:**\n"
    for u in get_recent_users(10):
        spent = u.get('total_spent', 0) or 0
        text += f"• {u['first_name']} (@{u.get('username','N/A')}) - ₹{spent}\n"
    
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]))

async def show_stats(query, context):
    if query.from_user.id not in ADMIN_IDS:
        return
    
    text = (
        f"📊 **স্ট্যাটিস্টিকস**\n\n"
        f"📦 প্যাকেজ: {count_products()}\n"
        f"👥 ইউজার: {count_users()}\n"
        f"🚫 ব্লকড: {count_blocked()}\n"
        f"📞 মোট স্টক: {get_total_number_count()}\n"
        f"🟢 উপলব্ধ: {get_available_number_count()}\n"
        f"🔴 সোল্ড: {get_sold_number_count()}\n"
        f"🚫 ইনভ্যালিড: {get_invalid_number_count()}\n"
        f"📦 মোট অর্ডার: {count_orders()}\n"
        f"⏳ পেন্ডিং: {count_orders('pending')}\n"
        f"✅ ডেলিভারি: {count_orders('delivered')}\n"
        f"❌ রিজেক্টেড: {count_orders('rejected')}\n"
        f"💰 **মোট আয়: ₹{get_total_revenue()}**"
    )
    
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]))

# ===================== HEALTH SERVER =====================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args): pass

def run_health_server():
    server = HTTPServer(('0.0.0.0', PORT), HealthHandler)
    logger.info(f"✅ Health server on port {PORT}")
    server.serve_forever()

# ===================== MAIN =====================
_shutdown = False

def signal_handler(sig, frame):
    global _shutdown
    _shutdown = True
    logger.info("Shutdown signal received")

async def main():
    init_db()
    
    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()
    
    application = Application.builder().token(BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler(filters.ALL, handle_messages))
    
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    
    logger.info("✅ Bot is running!")
    
    while not _shutdown:
        await asyncio.sleep(1)
    
    logger.info("Shutting down...")
    await application.updater.stop()
    await application.stop()
    await application.shutdown()

if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("Bot stopped.")
