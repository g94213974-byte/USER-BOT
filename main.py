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
from telegram.error import Conflict
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
    c.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, first_name TEXT, username TEXT, last_interaction TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, first_name TEXT, username TEXT, type TEXT, package_id TEXT, package_name TEXT, price REAL, quantity INTEGER DEFAULT 1, delivery_link TEXT DEFAULT '', screenshot_file_id TEXT, status TEXT DEFAULT 'pending', created_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT, name TEXT, price REAL, quantity INTEGER DEFAULT 1, delivery_link TEXT DEFAULT '', position INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS blocked_users (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, first_name TEXT, username TEXT, blocked_at TEXT)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS number_pool (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        number TEXT UNIQUE, 
        api_id INTEGER,
        api_hash TEXT,
        session_file TEXT,
        status TEXT DEFAULT 'available', 
        assigned_to INTEGER DEFAULT NULL, 
        assigned_at TEXT DEFAULT NULL,
        last_active DATETIME DEFAULT NULL,
        login_valid INTEGER DEFAULT 1
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS otp_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        number_id INTEGER,
        number TEXT, 
        user_id INTEGER, 
        otp_code TEXT, 
        sender TEXT DEFAULT '', 
        status TEXT DEFAULT 'received', 
        forwarded_at TEXT DEFAULT NULL, 
        created_at TEXT
    )''')
    
    defaults = {"upi_id": "customupi@bank", "qr_code": "", "how_to_use_video": ""}
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

# ===================== PRODUCTS =====================
def add_product(type_name, name, price, extra):
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM products WHERE type = ?", (type_name,))
        count = c.fetchone()[0]
        if type_name == "number":
            c.execute("INSERT INTO products (type, name, price, quantity, position) VALUES (?, ?, ?, ?, ?)", 
                      (type_name, name, price, int(extra), count + 1))
        else:
            c.execute("INSERT INTO products (type, name, price, delivery_link, position) VALUES (?, ?, ?, ?, ?)", 
                      (type_name, name, price, extra, count + 1))
        conn.commit()
        conn.close()

def get_products(type_name):
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM products WHERE type = ? ORDER BY position", (type_name,))
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

def count_products(type_name):
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM products WHERE type = ?", (type_name,))
        result = c.fetchone()[0]
        conn.close()
        return result

# ===================== ORDERS =====================
def create_order(user_id, first_name, username, p_type, package_id, package_name, price, quantity, delivery_link, screenshot_id):
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute('''INSERT INTO orders (user_id, first_name, username, type, package_id, package_name, price, quantity, delivery_link, screenshot_file_id, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)''',
                  (user_id, first_name, username, p_type, str(package_id), package_name, price, quantity, delivery_link, screenshot_id, datetime.datetime.now().isoformat()))
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

def assign_number_to_user(number_id, user_id):
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE number_pool SET status = 'assigned', assigned_to = ?, assigned_at = ?, last_active = ? WHERE id = ? AND status = 'available'",
                  (user_id, datetime.datetime.now().isoformat(), datetime.datetime.now().isoformat(), number_id))
        affected = c.rowcount
        conn.commit()
        conn.close()
        return affected > 0

def get_assigned_number_for_user(user_id):
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM number_pool WHERE assigned_to = ? AND status = 'assigned'", (user_id,))
        row = c.fetchone()
        conn.close()
        return dict(row) if row else None

def get_number_by_id(number_id):
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM number_pool WHERE id = ?", (number_id,))
        row = c.fetchone()
        conn.close()
        return dict(row) if row else None

def mark_number_invalid(number_id):
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE number_pool SET login_valid = 0, status = 'invalid' WHERE id = ?", (number_id,))
        conn.commit()
        conn.close()

def mark_number_valid(number_id):
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE number_pool SET login_valid = 1 WHERE id = ?", (number_id,))
        conn.commit()
        conn.close()

def release_number(number_id):
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE number_pool SET status = 'available', assigned_to = NULL, assigned_at = NULL, login_valid = 1 WHERE id = ?", (number_id,))
        conn.commit()
        conn.close()

def count_available_numbers():
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM number_pool WHERE status = 'available' AND login_valid = 1")
        result = c.fetchone()[0]
        conn.close()
        return result

def count_total_numbers():
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM number_pool")
        result = c.fetchone()[0]
        conn.close()
        return result

def count_assigned_numbers():
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM number_pool WHERE status = 'assigned'")
        result = c.fetchone()[0]
        conn.close()
        return result

def count_invalid_numbers():
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM number_pool WHERE login_valid = 0")
        result = c.fetchone()[0]
        conn.close()
        return result

def get_all_connected_numbers():
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT id, number, api_id, api_hash, session_file FROM number_pool WHERE login_valid = 1")
        results = [dict(row) for row in c.fetchall()]
        conn.close()
        return results

# ===================== OTP FUNCTIONS =====================
def save_otp_record(number_id, number, user_id, otp_text, sender=""):
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("INSERT INTO otp_records (number_id, number, user_id, otp_code, sender, status, created_at) VALUES (?, ?, ?, ?, ?, 'received', ?)",
                  (number_id, number, user_id, otp_text, sender, datetime.datetime.now().isoformat()))
        otp_id = c.lastrowid
        conn.commit()
        conn.close()
        return otp_id

def mark_otp_forwarded(otp_id):
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE otp_records SET status = 'forwarded', forwarded_at = ? WHERE id = ?",
                  (datetime.datetime.now().isoformat(), otp_id))
        conn.commit()
        conn.close()

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

# ===================== TELETHON CLIENT =====================

class NumberClient:
    def __init__(self, number_id, number, api_id, api_hash, session_file):
        self.number_id = number_id
        self.number = number
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_file = session_file
        self.client = None
        self.task = None
        self._running = False
    
    async def start(self):
        self._running = True
        session_path = os.path.join(SESSION_DIR, self.session_file)
        
        try:
            self.client = TelegramClient(session_path, self.api_id, self.api_hash)
            await self.client.start()
            me = await self.client.get_me()
            logger.info(f"✅ [{self.number}] Connected as: {me.first_name}")
            
            mark_number_valid(self.number_id)
            
            @self.client.on(events.NewMessage)
            async def handler(event):
                await self.handle_message(event)
            
            # Keep alive and check connection
            while self._running:
                try:
                    if not await self.client.is_user_authorized():
                        logger.warning(f"⚠️ [{self.number}] Session expired!")
                        mark_number_invalid(self.number_id)
                        await self.notify_admins_logout()
                        break
                    
                    with _db_lock:
                        conn = get_db()
                        c = conn.cursor()
                        c.execute("UPDATE number_pool SET last_active = ? WHERE id = ?",
                                  (datetime.datetime.now().isoformat(), self.number_id))
                        conn.commit()
                        conn.close()
                    
                    await asyncio.sleep(60)
                except RPCError as e:
                    logger.error(f"⚠️ [{self.number}] RPC Error: {e}")
                    if "AUTH_KEY_UNREGISTERED" in str(e) or "SESSION_REVOKED" in str(e):
                        logger.warning(f"🚫 [{self.number}] Session revoked!")
                        mark_number_invalid(self.number_id)
                        await self.notify_admins_logout()
                        break
                    await asyncio.sleep(10)
                except Exception as e:
                    logger.error(f"[{self.number}] Keep-alive error: {e}")
                    await asyncio.sleep(10)
        
        except Exception as e:
            error_str = str(e)
            logger.error(f"❌ [{self.number}] Failed: {error_str}")
            if "AUTH_KEY_UNREGISTERED" in error_str or "SESSION_REVOKED" in error_str:
                mark_number_invalid(self.number_id)
                await self.notify_admins_logout()
    
    async def handle_message(self, event):
        try:
            sender = await event.get_sender()
            message_text = event.raw_text
            if not message_text:
                return
            
            sender_name = sender.first_name if sender else "Unknown"
            logger.info(f"📩 [{self.number}] Message: {message_text[:80]}...")
            
            # ===== CHECK IF THIS IS A LOGIN ALERT =====
            login_alert_patterns = [
                r'code:\s*(\d{4,8})',
                r'login\s*code[:\s]+(\d{4,8})',
                r'verification\s*code[:\s]+(\d{4,8})',
                r'Your verification code is:?\s*(\d{4,8})',
                r'Login code:?\s*(\d{4,8})',
                r'Your code is:?\s*(\d{4,8})',
            ]
            
            is_login_alert = False
            login_code = None
            
            for pattern in login_alert_patterns:
                match = re.search(pattern, message_text, re.IGNORECASE)
                if match:
                    is_login_alert = True
                    login_code = match.group(1)
                    break
            
            if not is_login_alert:
                login_keywords = ['logged in', 'new device', 'new login', 'sign in', 'logged into', 'device logged']
                if any(kw in message_text.lower() for kw in login_keywords):
                    code_match = re.search(r'(\d{4,8})', message_text)
                    if code_match:
                        is_login_alert = True
                        login_code = code_match.group(1)
            
            # ===== CHECK NUMBER STATUS =====
            with _db_lock:
                conn = get_db()
                c = conn.cursor()
                c.execute("SELECT * FROM number_pool WHERE id = ?", (self.number_id,))
                num_row = c.fetchone()
                conn.close()
            
            if not num_row:
                return
            
            is_assigned = (num_row['status'] == 'assigned')
            is_available = (num_row['status'] == 'available')
            
            # ===== IF LOGIN ALERT DETECTED =====
            if is_login_alert and login_code:
                logger.info(f"⚠️ [{self.number}] Login alert! Code: {login_code}")
                
                from telegram import Bot
                bot = Bot(token=BOT_TOKEN)
                
                if is_assigned:
                    user_id = num_row['assigned_to']
                    
                    # Step 1: Send login code to user
                    login_text = (
                        f"📲 **Login Code Received!** 📩\n\n"
                        f"📱 Number: `{self.number}`\n"
                        f"🔑 **Code:** `{login_code}`\n\n"
                        f"⚠️ Use this code to login to your app!\n"
                        f"⏳ Hurry, it expires soon!"
                    )
                    try:
                        await bot.send_message(chat_id=user_id, text=login_text, parse_mode='Markdown')
                        logger.info(f"✅ [{self.number}] Code sent to user {user_id}")
                    except Exception as e:
                        logger.error(f"❌ [{self.number}] Code send failed: {e}")
                    
                    # Step 2: Wait 3 seconds then auto-logout
                    await asyncio.sleep(3)
                    
                    logger.info(f"🚫 [{self.number}] Auto-logging out assigned number...")
                    try:
                        await self.client.log_out()
                        logger.info(f"✅ [{self.number}] Logged out!")
                        mark_number_invalid(self.number_id)
                        
                        try:
                            await bot.send_message(
                                chat_id=user_id,
                                text=f"🚫 **Session Ended**\n\n📱 `{self.number}`\n✅ Account logged out automatically.\n\nOnly 1 user per number is allowed.",
                                parse_mode='Markdown'
                            )
                        except:
                            pass
                        
                        for admin_id in ADMIN_IDS:
                            try:
                                await bot.send_message(
                                    chat_id=admin_id,
                                    text=f"🔄 **Auto-Logout**\n\n📱 `{self.number}`\n🔑 Code: `{login_code}`\n👤 User: `{user_id}`\n✅ Done!",
                                    parse_mode='Markdown'
                                )
                            except:
                                pass
                        
                    except Exception as e:
                        logger.error(f"❌ [{self.number}] Logout failed: {e}")
                        try:
                            await self.client.disconnect()
                            mark_number_invalid(self.number_id)
                        except:
                            pass
                    
                    self._running = False
                    
                elif is_available:
                    # Available number - just log it, don't logout
                    logger.info(f"ℹ️ [{self.number}] Login alert on available number - ignoring")
                    for admin_id in ADMIN_IDS:
                        try:
                            await bot.send_message(
                                chat_id=admin_id,
                                text=f"ℹ️ **Login Alert** (Available)\n\n📱 `{self.number}`\n🔑 Code: `{login_code}`\n\nNot assigned yet. No action taken.",
                                parse_mode='Markdown'
                            )
                        except:
                            pass
                
                return  # Don't process as OTP
            
            # ===== NORMAL OTP PROCESSING =====
            if not is_assigned:
                return
            
            user_id = num_row['assigned_to']
            
            otp_match = re.search(r'(\d{4,8})', message_text)
            otp_code = otp_match.group(1) if otp_match else message_text
            
            otp_id = save_otp_record(self.number_id, self.number, user_id, message_text, sender_name)
            
            from telegram import Bot
            bot = Bot(token=BOT_TOKEN)
            
            forward_text = (
                f"🔐 **OTP Received!** 📩\n\n"
                f"📱 **Number:** `{self.number}`\n"
                f"🔑 **OTP:** `{otp_code}`\n"
                f"👤 **From:** {sender_name}\n"
                f"📨 **Message:** `{message_text}`\n\n"
                f"⚡ Use quickly!"
            )
            
            try:
                await bot.send_message(chat_id=user_id, text=forward_text, parse_mode='Markdown')
                mark_otp_forwarded(otp_id)
                logger.info(f"✅ [{self.number}] OTP forwarded to {user_id}")
            except Exception as e:
                logger.error(f"❌ [{self.number}] Forward failed: {e}")
            
            for admin_id in ADMIN_IDS:
                try:
                    await bot.send_message(
                        chat_id=admin_id,
                        text=f"🔄 **OTP Forwarded!**\n📱 {self.number}\n🔑 `{otp_code}`\n👤 User: `{user_id}`",
                        parse_mode='Markdown'
                    )
                except:
                    pass
        
        except Exception as e:
            logger.error(f"Error in handler [{self.number}]: {e}")
    
    async def notify_admins_logout(self):
        from telegram import Bot
        bot = Bot(token=BOT_TOKEN)
        
        with _db_lock:
            conn = get_db()
            c = conn.cursor()
            c.execute("SELECT * FROM number_pool WHERE id = ?", (self.number_id,))
            row = c.fetchone()
            conn.close()
        
        user_info = ""
        if row and row['assigned_to']:
            user_info = f"\n👤 Assigned: `{row['assigned_to']}`"
        
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=f"🚫 **Number Lost**\n\n📱 `{self.number}`❌ Session expired{user_info}\n\nRe-add with valid credentials.",
                    parse_mode='Markdown'
                )
            except:
                pass
        
        if row and row['assigned_to'] and row['status'] == 'assigned':
            try:
                await bot.send_message(
                    chat_id=row['assigned_to'],
                    text=f"🚫 **Number Lost**\n\n📱 `{self.number}`\n❌ Session expired.\n\nContact support.",
                    parse_mode='Markdown'
                )
            except:
                pass
    
    async def stop(self):
        self._running = False
        if self.client:
            await self.client.disconnect()
            logger.info(f"⏹️ [{self.number}] Disconnected")

# ===================== CLIENT MANAGER =====================
active_clients = []
_clients_lock = threading.Lock()

async def start_all_number_clients():
    numbers = get_all_connected_numbers()
    
    for num in numbers:
        already_running = False
        with _clients_lock:
            for c in active_clients:
                if c.number_id == num['id'] and c._running:
                    already_running = True
                    break
        if already_running:
            continue
        
        client = NumberClient(
            num['id'], 
            num['number'],
            num['api_id'], 
            num['api_hash'], 
            num.get('session_file', f'session_{num["number"]}.session')
        )
        
        task = asyncio.create_task(client.start())
        client.task = task
        
        with _clients_lock:
            active_clients.append(client)
        
        await asyncio.sleep(0.3)
    
    logger.info(f"✅ Started {len(numbers)} clients")

async def stop_all_number_clients():
    with _clients_lock:
        for client in active_clients:
            await client.stop()
        active_clients.clear()
    logger.info("⏹️ All clients stopped")

# ===================== BOT HANDLERS =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    save_user(user.id, user.first_name, user.username)
    if is_blocked(user.id):
        await update.message.reply_text("⛔ Blocked.")
        return
    
    keyboard = [
        [InlineKeyboardButton("📱 Buy Number", callback_data="buy_number")],
        [InlineKeyboardButton("🎬 Buy Video", callback_data="buy_video")],
        [InlineKeyboardButton("📖 How To Use", callback_data="how_to_use")]
    ]
    if user.id in ADMIN_IDS:
        keyboard.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_panel")])
    
    await update.message.reply_text(
        "👋 **Welcome!**\n\n📱 Buy virtual numbers\n🎬 Buy premium videos\n📖 Learn how to use\n\nSelect below:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if is_blocked(user_id):
        await safe_edit(query, "⛔ Blocked.")
        return
    data = query.data
    
    if data == "how_to_use":
        await show_how_to_use(query, context)
    elif data == "buy_number":
        await show_number_products(query, context)
    elif data == "buy_video":
        await show_video_products(query, context)
    elif data.startswith("num_pkg_"):
        await show_payment(query, context, "number", int(data.replace("num_pkg_", "")))
    elif data.startswith("vid_pkg_"):
        await show_payment(query, context, "video", int(data.replace("vid_pkg_", "")))
    elif data == "back_main":
        await back_to_main(query, context)
    elif data == "back_numbers":
        await show_number_products(query, context)
    elif data == "back_videos":
        await show_video_products(query, context)
    elif data == "cancel_payment":
        context.user_data["waiting_for_screenshot"] = False
        context.user_data["pending_pkg_id"] = None
        context.user_data["pending_type"] = None
        await safe_edit(query, "❌ Cancelled.")
    elif data.startswith("pay_"):
        parts = data.split("_")
        context.user_data["pending_pkg_id"] = int(parts[2])
        context.user_data["pending_type"] = parts[1]
        context.user_data["waiting_for_screenshot"] = True
        await safe_edit(query, "📸 Send payment screenshot.\n\n❌ Cancel:", 
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_payment")]]))
    elif data.startswith("approve_"):
        await approve_order(query, context, int(data.replace("approve_", "")))
    elif data.startswith("reject_"):
        await reject_order(query, context, int(data.replace("reject_", "")))
    elif data.startswith("block_"):
        await block_user_from_order(query, context, int(data.replace("block_", "")))
    elif data.startswith("unblock_"):
        unblock_user(int(data.replace("unblock_", "")))
        await safe_edit(query, "✅ Unblocked.")
    elif data == "admin_panel":
        await show_admin_panel(query, context)
    elif data.startswith("del_product_"):
        if query.from_user.id in ADMIN_IDS:
            pid = int(data.replace("del_product_", ""))
            prod = get_product(pid)
            delete_product(pid)
            await safe_edit(query, "✅ Deleted!")
            if prod and prod['type'] == 'number':
                await admin_number_products(query, context)
            elif prod and prod['type'] == 'video':
                await admin_video_products(query, context)
    elif data == "admin_numbers":
        await admin_number_products(query, context)
    elif data == "admin_videos":
        await admin_video_products(query, context)
    elif data == "admin_payment":
        await show_admin_payment(query, context)
    elif data == "admin_qr":
        await show_admin_qr(query, context)
    elif data == "admin_howto":
        await show_admin_howto(query, context)
    elif data == "admin_pending":
        await show_admin_orders(query, context, "pending")
    elif data == "admin_approved":
        await show_admin_orders(query, context, "approved")
    elif data == "admin_rejected":
        await show_admin_orders(query, context, "rejected")
    elif data == "admin_blocked":
        await show_blocked_users(query, context)
    elif data == "admin_users":
        await show_users(query, context)
    elif data == "admin_stats":
        await show_stats(query, context)
    elif data == "admin_number_pool":
        await show_number_pool_admin(query, context)
    elif data == "admin_add_numbers":
        if query.from_user.id in ADMIN_IDS:
            context.user_data["waiting_for_numbers_bulk"] = True
            await safe_edit(query, 
                "✏️ **Add Number**\n\nFormat:\n`phone:api_id:api_hash`\n\nExample:\n`+8801234567890:123456:abcdef`\n\nMultiple lines:\n`+8801234567890:123456:hash1`\n`+8801987654321:789012:hash2`\n\n❌ /cancel")
    elif data == "admin_invalid_numbers":
        if query.from_user.id in ADMIN_IDS:
            await show_invalid_numbers(query, context)
    elif data == "admin_assigned_numbers":
        if query.from_user.id in ADMIN_IDS:
            await show_assigned_numbers(query, context)
    elif data.startswith("del_pool_num_"):
        if query.from_user.id in ADMIN_IDS:
            num_id = int(data.replace("del_pool_num_", ""))
            with _db_lock:
                conn = get_db()
                c = conn.cursor()
                c.execute("DELETE FROM number_pool WHERE id = ?", (num_id,))
                conn.commit()
                conn.close()
            await safe_edit(query, "✅ Removed!")
            await show_number_pool_admin(query, context)
    elif data == "add_num_start":
        if query.from_user.id in ADMIN_IDS:
            context.user_data["waiting_for_num_name"] = True
            await safe_edit(query, "✏️ Package name:\nExample: `5 Numbers`\n\n❌ /cancel")
    elif data == "add_vid_start":
        if query.from_user.id in ADMIN_IDS:
            context.user_data["waiting_for_vid_name"] = True
            await safe_edit(query, "✏️ Package name:\nExample: `Premium Video`\n\n❌ /cancel")
    elif data == "edit_upi":
        if query.from_user.id in ADMIN_IDS:
            context.user_data["waiting_for_upi"] = True
            await safe_edit(query, "✏️ Send new UPI ID:\nExample: `yourupi@paytm`\n\n❌ /cancel")
    elif data == "edit_qr":
        if query.from_user.id in ADMIN_IDS:
            context.user_data["waiting_for_qr"] = True
            await safe_edit(query, "📷 Send QR image:\n\n❌ /cancel")
    elif data == "edit_howto":
        if query.from_user.id in ADMIN_IDS:
            context.user_data["waiting_for_howto"] = True
            await safe_edit(query, "🎬 Send how-to video:\n\n❌ /cancel")

async def show_how_to_use(query, context):
    video_id = get_setting("how_to_use_video")
    if video_id:
        try:
            await context.bot.send_video(chat_id=query.message.chat_id, video=video_id, caption="📖 How to use video.")
        except:
            pass
    await safe_edit(query, "📖 How To Use\n\nWatch video above.", 
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_main")]]))

async def show_number_products(query, context):
    products = get_products("number")
    if not products:
        await safe_edit(query, "❌ No packages yet.", 
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_main")]]))
        return
    text = "📱 **Number Packages**\n\nSelect:"
    keyboard = []
    row = []
    for p in products:
        row.append(InlineKeyboardButton(f"{p['name']} - ₹{p['price']}", callback_data=f"num_pkg_{p['id']}"))
        if len(row) == 2:
            keyboard.append(row); row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_main")])
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard))

async def show_video_products(query, context):
    products = get_products("video")
    if not products:
        await safe_edit(query, "❌ No packages yet.", 
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_main")]]))
        return
    text = "🎬 **Video Packages**\n\nSelect:"
    keyboard = []
    row = []
    for p in products:
        row.append(InlineKeyboardButton(f"{p['name']} - ₹{p['price']}", callback_data=f"vid_pkg_{p['id']}"))
        if len(row) == 2:
            keyboard.append(row); row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_main")])
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard))

async def show_payment(query, context, p_type, pkg_id):
    product = get_product(pkg_id)
    if not product:
        await safe_edit(query, "❌ Not found.")
        return
    upi_id = get_setting("upi_id") or "customupi@bank"
    qr_code = get_setting("qr_code")
    
    payment_text = (
        f"💳 **Payment**\n\n📦 {product['name']}\n💰 ₹{product['price']}\n🏦 UPI: `{upi_id}`\n\n📱 Pay using GPay/PhonePe/Paytm\n⚠️ Pay EXACT amount"
    )
    
    keyboard = [
        [InlineKeyboardButton("📸 I Have Paid", callback_data=f"pay_{p_type}_{pkg_id}")],
        [InlineKeyboardButton("🔙 Back", callback_data=f"back_{p_type}s")]
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
        for key in ["waiting_for_upi", "waiting_for_qr", "waiting_for_howto", 
                     "waiting_for_screenshot", "waiting_for_num_name", "waiting_for_num_price",
                     "waiting_for_num_qty", "waiting_for_vid_name", "waiting_for_vid_price",
                     "waiting_for_vid_link", "waiting_for_numbers_bulk"]:
            context.user_data[key] = False
        await update.message.reply_text("❌ Cancelled.")
        return
    
    # ADMIN: ADD BULK NUMBERS
    if context.user_data.get("waiting_for_numbers_bulk") and user.id in ADMIN_IDS:
        text = update.message.text.strip()
        lines = text.strip().split('\n')
        added = 0; failed = 0
        
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
                except:
                    failed += 1
            else:
                failed += 1
        
        context.user_data["waiting_for_numbers_bulk"] = False
        await update.message.reply_text(f"✅ **Added!**\n➕ {added}\n❌ {failed}\n📊 Total: {count_total_numbers()}\n🟢 Available: {count_available_numbers()}\n\n🔄 Restarting clients...")
        await stop_all_number_clients()
        asyncio.create_task(start_all_number_clients())
        return
    
    # ADMIN: ADD NUMBER PACKAGE
    if context.user_data.get("waiting_for_num_name") and user.id in ADMIN_IDS:
        name = update.message.text.strip()
        if name.isdigit():
            await update.message.reply_text("❌ Use letters.\nExample: `5 Numbers`\n\n❌ /cancel")
            return
        context.user_data["new_num_name"] = name
        context.user_data["waiting_for_num_name"] = False
        context.user_data["waiting_for_num_price"] = True
        await update.message.reply_text("✏️ Price in ₹:\nExample: `50`\n\n❌ /cancel")
        return
    
    if context.user_data.get("waiting_for_num_price") and user.id in ADMIN_IDS:
        try:
            price = int(update.message.text.strip())
            if price <= 0:
                await update.message.reply_text("❌ Must be > 0!\n\n❌ /cancel"); return
            context.user_data["new_num_price"] = price
            context.user_data["waiting_for_num_price"] = False
            context.user_data["waiting_for_num_qty"] = True
            await update.message.reply_text("✏️ Quantity:\nExample: `5`\n\n❌ /cancel")
        except ValueError:
            await update.message.reply_text("❌ Invalid number.\n\n❌ /cancel")
        return
    
    if context.user_data.get("waiting_for_num_qty") and user.id in ADMIN_IDS:
        try:
            qty = int(update.message.text.strip())
            if qty <= 0:
                await update.message.reply_text("❌ Must be > 0!\n\n❌ /cancel"); return
            name = context.user_data.get("new_num_name", "Package")
            price = context.user_data.get("new_num_price", 0)
            add_product("number", name, price, qty)
            context.user_data["waiting_for_num_qty"] = False
            await update.message.reply_text(f"✅ **Added!**\n📱 {name}\n💰 ₹{price}\n📦 Qty: {qty}")
        except ValueError:
            await update.message.reply_text("❌ Invalid.\n\n❌ /cancel")
        return
    
    # ADMIN: VIDEO PACKAGE
    if context.user_data.get("waiting_for_vid_name") and user.id in ADMIN_IDS:
        name = update.message.text.strip()
        if name.isdigit():
            await update.message.reply_text("❌ Use letters.\nExample: `Premium Video`\n\n❌ /cancel"); return
        context.user_data["new_vid_name"] = name
        context.user_data["waiting_for_vid_name"] = False
        context.user_data["waiting_for_vid_price"] = True
        await update.message.reply_text("✏️ Price in ₹:\nExample: `100`\n\n❌ /cancel")
        return
    
    if context.user_data.get("waiting_for_vid_price") and user.id in ADMIN_IDS:
        try:
            price = int(update.message.text.strip())
            if price <= 0:
                await update.message.reply_text("❌ Must be > 0!\n\n❌ /cancel"); return
            context.user_data["new_vid_price"] = price
            context.user_data["waiting_for_vid_price"] = False
            context.user_data["waiting_for_vid_link"] = True
            await update.message.reply_text("✏️ Delivery link:\nExample: `https://t.me/...`\n\n❌ /cancel")
        except ValueError:
            await update.message.reply_text("❌ Invalid.\n\n❌ /cancel")
        return
    
    if context.user_data.get("waiting_for_vid_link") and user.id in ADMIN_IDS:
        link = update.message.text.strip()
        if not link.startswith("http"):
            await update.message.reply_text("❌ Must start with `https://`\n\n❌ /cancel"); return
        name = context.user_data.get("new_vid_name", "Video Pack")
        price = context.user_data.get("new_vid_price", 0)
        add_product("video", name, price, link)
        context.user_data["waiting_for_vid_link"] = False
        await update.message.reply_text(f"✅ **Added!**\n🎬 {name}\n💰 ₹{price}")
        return
    
    # ADMIN: SETTINGS
    if context.user_data.get("waiting_for_upi") and user.id in ADMIN_IDS:
        new_upi = update.message.text.strip()
        if "@" not in new_upi:
            await update.message.reply_text("❌ Must contain '@'.\nExample: `upi@paytm`\n\n❌ /cancel"); return
        update_setting("upi_id", new_upi)
        context.user_data["waiting_for_upi"] = False
        await update.message.reply_text(f"✅ **UPI updated!**\n`{new_upi}`")
        return
    
    if context.user_data.get("waiting_for_qr") and user.id in ADMIN_IDS:
        if update.message.photo:
            update_setting("qr_code", update.message.photo[-1].file_id)
            context.user_data["waiting_for_qr"] = False
            await update.message.reply_text("✅ **QR updated!**")
        else:
            await update.message.reply_text("❌ Send photo.\n\n❌ /cancel")
        return
    
    if context.user_data.get("waiting_for_howto") and user.id in ADMIN_IDS:
        if update.message.video:
            update_setting("how_to_use_video", update.message.video.file_id)
            context.user_data["waiting_for_howto"] = False
            await update.message.reply_text("✅ **Video updated!**")
        else:
            await update.message.reply_text("❌ Send video.\n\n❌ /cancel")
        return
    
    # USER: PAYMENT SCREENSHOT
    if context.user_data.get("waiting_for_screenshot"):
        if not update.message.photo:
            await update.message.reply_text("📸 Send screenshot image.")
            return
        pending = get_pending_order_by_user(user.id)
        if pending:
            await update.message.reply_text("⏳ Already under review.")
            return
        photo = update.message.photo[-1]
        pkg_id = context.user_data.get("pending_pkg_id")
        p_type = context.user_data.get("pending_type", "number")
        product = get_product(pkg_id) if pkg_id else None
        if not product:
            await update.message.reply_text("❌ Package not found. /start"); return
        order_id = create_order(user.id, user.first_name, user.username, p_type, pkg_id, product['name'], 
                                product['price'], product.get('quantity', 1), product.get('delivery_link', ''), photo.file_id)
        context.user_data["waiting_for_screenshot"] = False
        context.user_data["pending_pkg_id"] = None
        context.user_data["pending_type"] = None
        await update.message.reply_text("✅ **Received!**\n⏳ Wait 5-30 min.")
        await notify_admins(context, user, product, photo.file_id, p_type, order_id)
        return

async def notify_admins(context, user, product, screenshot_id, p_type, order_id):
    pkg_emoji = "📱" if p_type == "number" else "🎬"
    text = f"\n{pkg_emoji} **New Order!**\n\n👤 {user.first_name}\n🆔 {user.id}\n📛 @{user.username or 'N/A'}\n\n📦 {product['name']}\n💰 ₹{product['price']}"
    keyboard = [
        [InlineKeyboardButton("✅ APPROVE", callback_data=f"approve_{order_id}"), 
         InlineKeyboardButton("❌ REJECT", callback_data=f"reject_{order_id}")],
        [InlineKeyboardButton("🚫 BLOCK", callback_data=f"block_{order_id}")]
    ]
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_photo(chat_id=admin_id, photo=screenshot_id, caption=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        except:
            pass

async def approve_order(query, context, order_id):
    order = get_order(order_id)
    if not order:
        await safe_edit(query, "❌ Not found."); return
    update_order_status(order_id, "approved")
    user_id = order["user_id"]
    
    if order["type"] == "number":
        qty = order.get("quantity", 1)
        available = get_available_numbers(qty)
        if len(available) < qty:
            try:
                await context.bot.send_message(chat_id=user_id, text="❌ **Not enough numbers.** Contact support.")
            except:
                pass
            await safe_edit_caption(query, caption=f"⚠️ Need {qty}, have {len(available)}")
            return
        
        text = "✅ **Approved!**\n\n📱 **Your Number(s):**\n\n"
        assigned_list = []
        for i in range(qty):
            num = available[i]
            assign_number_to_user(num["id"], user_id)
            assigned_list.append(num["number"])
            text += f"{i+1}. `{num['number']}`\n"
        
        text += "\n🔐 **How it works:**\n• This number is ONLY yours\n• Use on any app\n• OTP comes here automatically ⚡\n• When login code arrives, we'll send it to you, then auto-logout for security\n\n❤️ Thank you! 🔒"
        
        try:
            await context.bot.send_message(chat_id=user_id, text=text, parse_mode='Markdown')
        except:
            pass
        
        await safe_edit_caption(query, caption=f"✅ **Approved!**\n{order['package_name']}\n₹{order['price']}\nUser: {order['first_name']}\n📱 " + ", ".join(assigned_list))
    elif order["type"] == "video":
        try:
            await context.bot.send_message(chat_id=user_id, parse_mode='Markdown', text=f"✅ **Approved!**\n\n🔗 {order.get('delivery_link', '')}")
        except:
            pass
        await safe_edit_caption(query, caption=f"✅ **Approved!**\n{order['package_name']}\nUser: {order['first_name']}")

async def reject_order(query, context, order_id):
    order = get_order(order_id)
    if not order:
        await safe_edit(query, "❌ Not found."); return
    update_order_status(order_id, "rejected")
    try:
        await context.bot.send_message(chat_id=order["user_id"], text="❌ **Rejected.** Contact support.")
    except:
        pass
    await safe_edit_caption(query, caption=f"❌ **Rejected!**\n{order['package_name']}\nUser: {order['first_name']}")

async def block_user_from_order(query, context, order_id):
    order = get_order(order_id)
    if not order:
        await safe_edit(query, "❌ Not found."); return
    block_user(order["user_id"], order["first_name"], order.get("username", ""))
    update_order_status(order_id, "blocked")
    await safe_edit_caption(query, caption=f"🚫 **Blocked!**\n{order['first_name']}")

async def back_to_main(query, context):
    keyboard = [
        [InlineKeyboardButton("📱 Buy Number", callback_data="buy_number")],
        [InlineKeyboardButton("🎬 Buy Video", callback_data="buy_video")],
        [InlineKeyboardButton("📖 How To Use", callback_data="how_to_use")]
    ]
    if query.from_user.id in ADMIN_IDS:
        keyboard.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_panel")])
    try: await query.message.delete()
    except: pass
    await context.bot.send_message(chat_id=query.message.chat_id, text="👋 **Welcome back!**", reply_markup=InlineKeyboardMarkup(keyboard))

# ===================== ADMIN UI =====================
async def show_invalid_numbers(query, context):
    if query.from_user.id not in ADMIN_IDS: return
    with _db_lock:
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT * FROM number_pool WHERE login_valid = 0 ORDER BY id DESC")
        rows = [dict(r) for r in c.fetchall()]; conn.close()
    text = "🚫 **Invalid Numbers**\n\n"
    if not rows: text += "None."
    else:
        for r in rows:
            u = f" (was: {r['assigned_to']})" if r['assigned_to'] else ""
            text += f"• `{r['number']}`{u}\n"
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]))

async def show_number_pool_admin(query, context):
    if query.from_user.id not in ADMIN_IDS: return
    text = f"📱 **Number Pool**\n\n📊 Total: {count_total_numbers()}\n🟢 Available: {count_available_numbers()}\n🔴 Assigned: {count_assigned_numbers()}\n🚫 Invalid: {count_invalid_numbers()}\n\n"
    nums = get_available_numbers(15)
    if nums:
        text += "**Available:**\n"
        for n in nums[:10]:
            text += f"🆔 #{n['id']} • `{n['number']}`\n"
        if len(nums) > 10: text += f"... +{len(nums)-10} more\n"
    else:
        text += "❌ No available numbers.\n"
    
    keyboard = [[InlineKeyboardButton("➕ Add Number", callback_data="admin_add_numbers")]]
    if count_assigned_numbers() > 0:
        keyboard.append([InlineKeyboardButton("🔐 Assigned", callback_data="admin_assigned_numbers")])
    if count_invalid_numbers() > 0:
        keyboard.append([InlineKeyboardButton("🚫 Invalid", callback_data="admin_invalid_numbers")])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard))

async def show_assigned_numbers(query, context):
    if query.from_user.id not in ADMIN_IDS: return
    with _db_lock:
        conn = get_db(); c = conn.cursor()
        c.execute('''SELECT np.id, np.number, np.assigned_to, np.assigned_at, u.first_name, u.username FROM number_pool np LEFT JOIN users u ON np.assigned_to = u.user_id WHERE np.status = 'assigned' ORDER BY np.assigned_at DESC LIMIT 20''')
        rows = [dict(r) for r in c.fetchall()]; conn.close()
    text = "🔐 **Assigned**\n\n"
    if not rows: text += "None."
    else:
        for r in rows:
            text += f"📱 `{r['number']}`\n  👤 {r['first_name']} (@{r['username'] or 'N/A'})\n  🆔 {r['assigned_to']}\n  🕐 {r['assigned_at'][:19]}\n\n"
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]))

async def show_admin_panel(query, context):
    if query.from_user.id not in ADMIN_IDS: return
    keyboard = [
        [InlineKeyboardButton("📱 Number Products", callback_data="admin_numbers"), InlineKeyboardButton("🎬 Video Products", callback_data="admin_videos")],
        [InlineKeyboardButton("📱 Number Pool", callback_data="admin_number_pool"), InlineKeyboardButton("🔐 Assigned", callback_data="admin_assigned_numbers")],
        [InlineKeyboardButton("🚫 Invalid", callback_data="admin_invalid_numbers")],
        [InlineKeyboardButton("💳 Payment", callback_data="admin_payment"), InlineKeyboardButton("📷 QR", callback_data="admin_qr")],
        [InlineKeyboardButton("📖 HowTo", callback_data="admin_howto")],
        [InlineKeyboardButton("📦 Pending", callback_data="admin_pending"), InlineKeyboardButton("✅ Approved", callback_data="admin_approved")],
        [InlineKeyboardButton("❌ Rejected", callback_data="admin_rejected"), InlineKeyboardButton("🚫 Blocked", callback_data="admin_blocked")],
        [InlineKeyboardButton("👥 Users", callback_data="admin_users"), InlineKeyboardButton("📊 Stats", callback_data="admin_stats")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
    ]
    await safe_edit(query, "⚙️ **Admin Panel**", reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_number_products(query, context):
    if query.from_user.id not in ADMIN_IDS: return
    products = get_products("number")
    text = "📱 **Number Packages**\n\n"
    if not products: text += "None."
    else:
        for p in products:
            text += f"• **#{p['id']}**: {p['name']}\n  ₹{p['price']} | {p.get('quantity',1)} nums\n"
    keyboard = []; row = []
    for p in products:
        row.append(InlineKeyboardButton(f"🗑️ #{p['id']}", callback_data=f"del_product_{p['id']}"))
        if len(row) == 2: keyboard.append(row); row = []
    if row: keyboard.append(row)
    keyboard.append([InlineKeyboardButton("➕ Add", callback_data="add_num_start")])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_video_products(query, context):
    if query.from_user.id not in ADMIN_IDS: return
    products = get_products("video")
    text = "🎬 **Video Packages**\n\n"
    if not products: text += "None."
    else:
        for p in products:
            text += f"• **#{p['id']}**: {p['name']}\n  ₹{p['price']}\n"
    keyboard = []; row = []
    for p in products:
        row.append(InlineKeyboardButton(f"🗑️ #{p['id']}", callback_data=f"del_product_{p['id']}"))
        if len(row) == 2: keyboard.append(row); row = []
    if row: keyboard.append(row)
    keyboard.append([InlineKeyboardButton("➕ Add", callback_data="add_vid_start")])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard))

async def show_admin_payment(query, context):
    if query.from_user.id not in ADMIN_IDS: return
    upi = get_setting("upi_id") or "customupi@bank"
    text = f"💳 **Payment**\n\n🏦 UPI: `{upi}`"
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✏️ Change", callback_data="edit_upi")], [InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]))

async def show_admin_qr(query, context):
    if query.from_user.id not in ADMIN_IDS: return
    qr = get_setting("qr_code")
    text = "📷 **QR**\n\n" + ("✅ Set" if qr else "❌ Not set")
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✏️ Set", callback_data="edit_qr")], [InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]))

async def show_admin_howto(query, context):
    if query.from_user.id not in ADMIN_IDS: return
    vid = get_setting("how_to_use_video")
    text = "📖 **HowTo**\n\n" + ("✅ Set" if vid else "❌ Not set")
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✏️ Set", callback_data="edit_howto")], [InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]))

async def show_admin_orders(query, context, status):
    if query.from_user.id not in ADMIN_IDS: return
    orders = get_orders_by_status(status)
    emoji = {"pending": "📦", "approved": "✅", "rejected": "❌"}.get(status, "📋")
    text = f"{emoji} **{status.title()}**\n\n"
    if not orders: text += "None."
    else:
        for o in orders:
            text += f"• {o['first_name']} - {o['package_name']} - ₹{o['price']}\n  🆔 {o['id']} | {o['created_at'][:19]}\n\n"
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]))

async def show_blocked_users(query, context):
    if query.from_user.id not in ADMIN_IDS: return
    blocked = get_blocked_users()
    text = "🚫 **Blocked**\n\n"
    if not blocked:
        text += "None."
        await safe_edit(query, text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]))
        return
    keyboard = []; row = []
    for b in blocked:
        row.append(InlineKeyboardButton(f"🔓 {b['first_name']}", callback_data=f"unblock_{b['id']}"))
        if len(row) == 2: keyboard.append(row); row = []
    if row: keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])
    for b in blocked:
        text += f"• {b['first_name']} (@{b.get('username','N/A')}) - {b['user_id']}\n"
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard))

async def show_users(query, context):
    if query.from_user.id not in ADMIN_IDS: return
    text = f"👥 **Users:** {count_users()}\n\n**Recent:**\n"
    for u in get_recent_users(10):
        text += f"• {u['first_name']} (@{u.get('username','N/A')})\n"
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]))

async def show_stats(query, context):
    if query.from_user.id not in ADMIN_IDS: return
    text = (
        f"📊 **Stats**\n\n"
        f"📱 Number Pkgs: {count_products('number')}\n"
        f"🎬 Video Pkgs: {count_products('video')}\n"
        f"👥 Users: {count_users()}\n"
f"🚫 Blocked: {count_blocked()}\n"
        f"📞 Pool: {count_total_numbers()}\n"
        f"🟢 Available: {count_available_numbers()}\n"
        f"🔴 Assigned: {count_assigned_numbers()}\n"
        f"🚫 Invalid: {count_invalid_numbers()}\n"
        f"Active Clients: {len(active_clients)}\n\n"
        f"📦 Total: {count_orders()}\n"
        f"⏳ Pending: {count_orders('pending')}\n"
        f"✅ Approved: {count_orders('approved')}\n"
        f"❌ Rejected: {count_orders('rejected')}\n"
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
    logger.info(f"Health server on port {PORT}")
    server.serve_forever()

# ===================== MAIN =====================
_shutdown = False

def signal_handler(sig, frame):
    global _shutdown
    _shutdown = True
    logger.info("Shutdown signal received")

async def main():
    # Initialize database
    init_db()
    
    # Start health server in a separate thread
    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()
    
    # Set up the bot application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Register handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler(filters.ALL, handle_messages))
    
    # Start number clients
    await start_all_number_clients()
    
    # Start the bot
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    
    logger.info("✅ Bot is running!")
    
    # Keep alive
    while not _shutdown:
        await asyncio.sleep(1)
    
    # Cleanup
    logger.info("Shutting down...")
    await stop_all_number_clients()
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
