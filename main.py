import sqlite3
import datetime
import logging
import os
import threading
import random
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

# ===================== CONFIG =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(","))) if os.getenv("ADMIN_IDS") else []
PORT = int(os.getenv("PORT", 10000))

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is not set!")

# ===================== LOGGING =====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ===================== DATABASE (Thread-Safe) =====================
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
    
    defaults = {"upi_id": "customupi@bank", "qr_code": "", "how_to_use_video": ""}
    for k, v in defaults.items():
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, str(v)))
    conn.commit()
    conn.close()

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

# ===================== USER HANDLERS =====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    save_user(user.id, user.first_name, user.username)
    if is_blocked(user.id):
        await update.message.reply_text("⛔ You have been blocked.")
        return
    
    # User buttons
    keyboard = [
        [InlineKeyboardButton("📱 Buy Number", callback_data="buy_number")],
        [InlineKeyboardButton("🎬 Buy Video", callback_data="buy_video")],
        [InlineKeyboardButton("📖 How To Use", callback_data="how_to_use")]
    ]
    if user.id in ADMIN_IDS:
        keyboard.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_panel")])
    
    welcome_text = (
        "👋 **Welcome to the Bot!**\n\n"
        "📱 Buy virtual numbers\n"
        "🎬 Buy premium videos\n"
        "📖 Learn how to use\n\n"
        "Select an option below:"
    )
    await update.message.reply_text(welcome_text, reply_markup=InlineKeyboardMarkup(keyboard))

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if is_blocked(user_id):
        await safe_edit(query, "⛔ You have been blocked.")
        return
    data = query.data
    
    # ===== USER SECTION =====
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
        await safe_edit(query, "❌ Payment cancelled.")
    elif data.startswith("pay_"):
        parts = data.split("_")
        context.user_data["pending_pkg_id"] = int(parts[2])
        context.user_data["pending_type"] = parts[1]
        context.user_data["waiting_for_screenshot"] = True
        await safe_edit(query, "📸 Please send your payment screenshot.\n\n❌ Press below to cancel:", 
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_payment")]]))
    # ===== ADMIN ORDERS =====
    elif data.startswith("approve_"):
        await approve_order(query, context, int(data.replace("approve_", "")))
    elif data.startswith("reject_"):
        await reject_order(query, context, int(data.replace("reject_", "")))
    elif data.startswith("block_"):
        await block_user_from_order(query, context, int(data.replace("block_", "")))
    elif data.startswith("unblock_"):
        unblock_user(int(data.replace("unblock_", "")))
        await safe_edit(query, "✅ User has been unblocked.")
    # ===== ADMIN SECTION =====
    elif data == "admin_panel":
        await show_admin_panel(query, context)
    elif data.startswith("del_product_"):
        if query.from_user.id in ADMIN_IDS:
            product_id = int(data.replace("del_product_", ""))
            product = get_product(product_id)
            delete_product(product_id)
            await safe_edit(query, f"✅ Product deleted!")
            if product and product['type'] == 'number':
                await admin_number_products(query, context)
            elif product and product['type'] == 'video':
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
    # ===== ADMIN ACTIONS =====
    elif data == "add_num_start":
        if query.from_user.id in ADMIN_IDS:
            context.user_data["waiting_for_num_name"] = True
            await safe_edit(query, "✏️ Enter number package name:\n\nExample: 5 Random Numbers\n\n❌ /cancel to cancel")
    elif data == "add_vid_start":
        if query.from_user.id in ADMIN_IDS:
            context.user_data["waiting_for_vid_name"] = True
            await safe_edit(query, "✏️ Enter video package name:\n\nExample: Premium Video Pack\n\n❌ /cancel to cancel")
    elif data == "edit_upi":
        if query.from_user.id in ADMIN_IDS:
            context.user_data["waiting_for_upi"] = True
            await safe_edit(query, "✏️ Send your new UPI ID:\n\nExample: yourupi@paytm\n\n❌ /cancel to cancel")
    elif data == "edit_qr":
        if query.from_user.id in ADMIN_IDS:
            context.user_data["waiting_for_qr"] = True
            await safe_edit(query, "📷 Send the QR code photo.\n\n❌ /cancel to cancel")
    elif data == "edit_howto":
        if query.from_user.id in ADMIN_IDS:
            context.user_data["waiting_for_howto"] = True
            await safe_edit(query, "🎬 Send the How-To-Use video.\n\n❌ /cancel to cancel")

async def show_how_to_use(query, context):
    video_id = get_setting("how_to_use_video")
    if video_id:
        try:
            await context.bot.send_video(chat_id=query.message.chat_id, video=video_id, 
                                         caption="📖 Watch this video to learn how to use the bot.")
        except Exception as e:
            logger.error(f"Error sending how-to video: {e}")
    await safe_edit(query, "📖 How To Use\n\nWatch the video above to learn how to use the bot.", 
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_main")]]))

async def show_number_products(query, context):
    products = get_products("number")
    if not products:
        await safe_edit(query, "❌ No number packages available yet.", 
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_main")]]))
        return
    text = "📱 **Number Packages**\n\nSelect a package below:"
    keyboard = []
    row = []
    for p in products:
        btn = InlineKeyboardButton(f"{p['name']} - ₹{p['price']}", callback_data=f"num_pkg_{p['id']}")
        row.append(btn)
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_main")])
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard))

async def show_video_products(query, context):
    products = get_products("video")
    if not products:
        await safe_edit(query, "❌ No video packages available yet.", 
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_main")]]))
        return
    text = "🎬 **Video Packages**\n\nSelect a package below:"
    keyboard = []
    row = []
    for p in products:
        btn = InlineKeyboardButton(f"{p['name']} - ₹{p['price']}", callback_data=f"vid_pkg_{p['id']}")
        row.append(btn)
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_main")])
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard))

async def show_payment(query, context, p_type, pkg_id):
    product = get_product(pkg_id)
    if not product:
        await safe_edit(query, "❌ Package not found.")
        return
    upi_id = get_setting("upi_id") or "customupi@bank"
    qr_code = get_setting("qr_code")
    payment_text = (
        f"💳 **Payment Details**\n\n"
        f"📦 Package: {product['name']}\n"
        f"💰 Amount: ₹{product['price']}\n"
        f"🏦 UPI ID: `{upi_id}`\n\n"
        f"📱 Pay using PhonePe / GPay / Paytm\n\n"
        f"⚠️ Pay the EXACT amount shown above"
    )
    keyboard = [
        [InlineKeyboardButton("📸 I Have Paid", callback_data=f"pay_{p_type}_{pkg_id}")],
        [InlineKeyboardButton("🔙 Back", callback_data=f"back_{p_type}s")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if qr_code:
        try:
            await context.bot.send_photo(chat_id=query.message.chat_id, photo=qr_code, 
                                         caption=payment_text, reply_markup=reply_markup, parse_mode='Markdown')
            try:
                await query.message.delete()
            except:
                pass
            return
        except Exception as e:
            logger.error(f"Error sending QR: {e}")
    await safe_edit(query, payment_text, reply_markup=reply_markup)

# ===================== MESSAGE HANDLER =====
async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles ALL user inputs: text, photos, videos"""
    user = update.effective_user
    
    # ===== CANCEL =====
    if update.message.text == "/cancel":
        for key in ["waiting_for_upi", "waiting_for_qr", "waiting_for_howto", 
                     "waiting_for_screenshot", "waiting_for_num_name", "waiting_for_num_price",
                     "waiting_for_num_qty", "waiting_for_vid_name", "waiting_for_vid_price",
                     "waiting_for_vid_link"]:
            context.user_data[key] = False
        await update.message.reply_text("❌ Cancelled.")
        return
    
    # ===== ADMIN: ADD NUMBER PACKAGE =====
    if context.user_data.get("waiting_for_num_name") and user.id in ADMIN_IDS:
        context.user_data["new_num_name"] = update.message.text.strip()
        context.user_data["waiting_for_num_name"] = False
        context.user_data["waiting_for_num_price"] = True
        await update.message.reply_text("✏️ Enter price (in ₹):\n\nExample: 50\n\n❌ /cancel to cancel")
        return
    
    if context.user_data.get("waiting_for_num_price") and user.id in ADMIN_IDS:
        try:
            price = int(update.message.text.strip())
            context.user_data["new_num_price"] = price
            context.user_data["waiting_for_num_price"] = False
            context.user_data["waiting_for_num_qty"] = True
            await update.message.reply_text("✏️ Enter quantity (how many numbers?):\n\nExample: 5\n\n❌ /cancel to cancel")
        except:
            await update.message.reply_text("❌ Invalid price! Send a number.\n\n❌ /cancel to cancel")
        return
    
    if context.user_data.get("waiting_for_num_qty") and user.id in ADMIN_IDS:
        try:
            qty = int(update.message.text.strip())
            name = context.user_data.get("new_num_name", "Package")
            price = context.user_data.get("new_num_price", 0)
            add_product("number", name, price, qty)
            context.user_data["waiting_for_num_qty"] = False
            await update.message.reply_text(f"✅ Number package added!\n\n📱 {name}\n💰 ₹{price}\n📦 Qty: {qty}")
        except:
            await update.message.reply_text("❌ Invalid quantity! Send a number.\n\n❌ /cancel to cancel")
        return
    
    # ===== ADMIN: ADD VIDEO PACKAGE =====
    if context.user_data.get("waiting_for_vid_name") and user.id in ADMIN_IDS:
        context.user_data["new_vid_name"] = update.message.text.strip()
        context.user_data["waiting_for_vid_name"] = False
        context.user_data["waiting_for_vid_price"] = True
        await update.message.reply_text("✏️ Enter price (in ₹):\n\nExample: 100\n\n❌ /cancel to cancel")
        return
    
    if context.user_data.get("waiting_for_vid_price") and user.id in ADMIN_IDS:
        try:
            price = int(update.message.text.strip())
            context.user_data["new_vid_price"] = price
            context.user_data["waiting_for_vid_price"] = False
            context.user_data["waiting_for_vid_link"] = True
            await update.message.reply_text("✏️ Enter delivery link:\n\nExample: https://t.me/yourchannel/123\n\n❌ /cancel to cancel")
        except:
            await update.message.reply_text("❌ Invalid price! Send a number.\n\n❌ /cancel to cancel")
        return
    
    if context.user_data.get("waiting_for_vid_link") and user.id in ADMIN_IDS:
        link = update.message.text.strip()
        name = context.user_data.get("new_vid_name", "Video Pack")
        price = context.user_data.get("new_vid_price", 0)
        add_product("video", name, price, link)
        context.user_data["waiting_for_vid_link"] = False
        await update.message.reply_text(f"✅ Video package added!\n\n🎬 {name}\n💰 ₹{price}\n🔗 {link}")
        return
    
    # ===== ADMIN: SET UPI ID =====
    if context.user_data.get("waiting_for_upi") and user.id in ADMIN_IDS:
        new_upi = update.message.text.strip()
        if "@" not in new_upi:
            await update.message.reply_text("❌ Invalid UPI ID. Must contain '@'.\n\n❌ /cancel to cancel")
            return
        update_setting("upi_id", new_upi)
        context.user_data["waiting_for_upi"] = False
        await update.message.reply_text(f"✅ UPI ID updated to: {new_upi}")
        return
    
    # ===== ADMIN: SET QR CODE =====
    if context.user_data.get("waiting_for_qr") and user.id in ADMIN_IDS:
        if update.message.photo:
            update_setting("qr_code", update.message.photo[-1].file_id)
            context.user_data["waiting_for_qr"] = False
            await update.message.reply_text("✅ QR Code updated!")
        else:
            await update.message.reply_text("❌ Please send a photo.\n\n❌ /cancel to cancel")
        return
    
    # ===== ADMIN: SET HOW TO VIDEO =====
    if context.user_data.get("waiting_for_howto") and user.id in ADMIN_IDS:
        if update.message.video:
            update_setting("how_to_use_video", update.message.video.file_id)
            context.user_data["waiting_for_howto"] = False
            await update.message.reply_text("✅ How-To video updated!")
        else:
            await update.message.reply_text("❌ Please send a video.\n\n❌ /cancel to cancel")
        return
    
    # ===== USER: PAYMENT SCREENSHOT =====
    if context.user_data.get("waiting_for_screenshot"):
        if not update.message.photo:
            await update.message.reply_text("📸 Please send a screenshot image.")
            return
        pending = get_pending_order_by_user(user.id)
        if pending:
            await update.message.reply_text("⏳ Your payment is already under review.")
            return
        photo = update.message.photo[-1]
        pkg_id = context.user_data.get("pending_pkg_id")
        p_type = context.user_data.get("pending_type", "number")
        product = get_product(pkg_id) if pkg_id else None
        if not product:
            await update.message.reply_text("❌ Package not found.")
            return
        order_id = create_order(user.id, user.first_name, user.username, p_type, pkg_id, product['name'], 
                                product['price'], product.get('quantity', 1), product.get('delivery_link', ''), photo.file_id)
        context.user_data["waiting_for_screenshot"] = False
        context.user_data["pending_pkg_id"] = None
        context.user_data["pending_type"] = None
        await update.message.reply_text("✅ Payment screenshot received!\n\n⏳ Please wait 5–30 minutes while we verify your payment.")
        await notify_admins(context, user, product, photo.file_id, p_type, order_id)
        return

async def notify_admins(context, user, product, screenshot_id, p_type, order_id):
    pkg_emoji = "📱" if p_type == "number" else "🎬"
    text = (
        f"\n{pkg_emoji} **New Order Received!**\n\n"
        f"👤 Name: {user.first_name}\n"
        f"🆔 User ID: {user.id}\n"
        f"📛 Username: @{user.username if user.username else 'N/A'}\n\n"
        f"📦 Package: {product['name']}\n"
        f"💰 Price: ₹{product['price']}\n"
    )
    keyboard = [
        [InlineKeyboardButton("✅ APPROVE", callback_data=f"approve_{order_id}"), 
         InlineKeyboardButton("❌ REJECT", callback_data=f"reject_{order_id}")],
        [InlineKeyboardButton("🚫 BLOCK", callback_data=f"block_{order_id}")]
    ]
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_photo(chat_id=admin_id, photo=screenshot_id, caption=text, 
                                         reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Could not notify admin {admin_id}: {e}")

async def approve_order(query, context, order_id):
    order = get_order(order_id)
    if not order:
        await safe_edit(query, "❌ Order not found.")
        return
    update_order_status(order_id, "approved")
    user_id = order["user_id"]
    if order["type"] == "number":
        quantity = order.get("quantity", 1)
        text = "✅ **Payment Approved!**\n\n📱 Your Numbers:\n\n"
        for i in range(quantity):
            text += f"{i+1}. +9112345{random.randint(10000, 99999)}\n"
        text += "\n❤️ Thank you!\nKeep 2-Step Verification ON. 🔒"
        try:
            await context.bot.send_message(chat_id=user_id, text=text, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Could not send approval to user {user_id}: {e}")
    elif order["type"] == "video":
        try:
            await context.bot.send_message(chat_id=user_id, parse_mode='Markdown',
                                           text=f"✅ **Payment Approved!**\n\n🔗 Access Link:\n\n{order.get('delivery_link', '')}")
        except Exception as e:
            logger.error(f"Could not send approval to user {user_id}: {e}")
    await safe_edit_caption(query, caption=f"✅ Order Approved!\n\n{order['package_name']}\n₹{order['price']}\nUser: {order['first_name']}")

async def reject_order(query, context, order_id):
    order = get_order(order_id)
    if not order:
        await safe_edit(query, "❌ Order not found.")
        return
    update_order_status(order_id, "rejected")
    try:
        await context.bot.send_message(chat_id=order["user_id"], 
                                       text="❌ **Payment Rejected.**\nContact support for details.")
    except Exception as e:
        logger.error(f"Could not notify user: {e}")
    await safe_edit_caption(query, caption=f"❌ Rejected!\n{order['package_name']}\nUser: {order['first_name']}")

async def block_user_from_order(query, context, order_id):
    order = get_order(order_id)
    if not order:
        await safe_edit(query, "❌ Order not found.")
        return
    block_user(order["user_id"], order["first_name"], order.get("username", ""))
    update_order_status(order_id, "blocked")
    await safe_edit_caption(query, caption=f"🚫 Blocked!\n{order['first_name']} (ID: {order['user_id']})")

async def back_to_main(query, context):
    keyboard = [
        [InlineKeyboardButton("📱 Buy Number", callback_data="buy_number")],
        [InlineKeyboardButton("🎬 Buy Video", callback_data="buy_video")],
        [InlineKeyboardButton("📖 How To Use", callback_data="how_to_use")]
    ]
    if query.from_user.id in ADMIN_IDS:
        keyboard.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_panel")])
    
    welcome_text = (
        "👋 **Welcome back!**\n\n"
        "📱 Buy virtual numbers\n"
        "🎬 Buy premium videos\n"
        "📖 Learn how to use\n\n"
        "Select an option below:"
    )
    await safe_edit(query, welcome_text, reply_markup=InlineKeyboardMarkup(keyboard))

# ===================== ADMIN PANEL =====================

async def show_admin_panel(query, context):
    if query.from_user.id not in ADMIN_IDS:
        return
    keyboard = [
        [InlineKeyboardButton("📱 Number Products", callback_data="admin_numbers"), 
         InlineKeyboardButton("🎬 Video Products", callback_data="admin_videos")],
        [InlineKeyboardButton("💳 Payment Settings", callback_data="admin_payment"), 
         InlineKeyboardButton("📷 QR Code", callback_data="admin_qr")],
        [InlineKeyboardButton("📖 HowTo Video", callback_data="admin_howto")],
        [InlineKeyboardButton("📦 Pending Orders", callback_data="admin_pending"), 
         InlineKeyboardButton("✅ Approved", callback_data="admin_approved")],
        [InlineKeyboardButton("❌ Rejected", callback_data="admin_rejected"), 
         InlineKeyboardButton("🚫 Blocked Users", callback_data="admin_blocked")],
        [InlineKeyboardButton("👥 Users", callback_data="admin_users"), 
         InlineKeyboardButton("📊 Statistics", callback_data="admin_stats")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
    ]
    await safe_edit(query, "⚙️ **Admin Panel**\n\nAll controls are here — no commands needed!", reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_number_products(query, context):
    if query.from_user.id not in ADMIN_IDS:
        return
    products = get_products("number")
    text = "📱 **Number Packages**\n\n"
    if not products:
        text += "No packages yet.\n"
    else:
        for p in products:
            text += f"• **ID {p['id']}**: {p['name']}\n  💰 ₹{p['price']} | 📦 {p.get('quantity', 1)} numbers\n"
    
    keyboard = []
    row = []
    for p in products:
        row.append(InlineKeyboardButton(f"🗑️ #{p['id']}", callback_data=f"del_product_{p['id']}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("➕ Add Number Package", callback_data="add_num_start")])
    keyboard.append([InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_panel")])
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_video_products(query, context):
    if query.from_user.id not in ADMIN_IDS:
        return
    products = get_products("video")
    text = "🎬 **Video Packages**\n\n"
    if not products:
        text += "No packages yet.\n"
    else:
        for p in products:
            text += f"• **ID {p['id']}**: {p['name']}\n  💰 ₹{p['price']} | 🔗 {p.get('delivery_link', 'N/A')}\n"
    
    keyboard = []
    row = []
    for p in products:
        row.append(InlineKeyboardButton(f"🗑️ #{p['id']}", callback_data=f"del_product_{p['id']}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("➕ Add Video Package", callback_data="add_vid_start")])
    keyboard.append([InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_panel")])
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard))

async def show_admin_payment(query, context):
    if query.from_user.id not in ADMIN_IDS:
        return
    current_upi = get_setting("upi_id") or "customupi@bank"
    text = (
        f"💳 **Payment Settings**\n\n"
        f"🏦 Current UPI ID: `{current_upi}`\n\n"
        f"Tap the button below to change your UPI ID."
    )
    keyboard = [
        [InlineKeyboardButton("✏️ Change UPI ID", callback_data="edit_upi")],
        [InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_panel")]
    ]
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard))

async def show_admin_qr(query, context):
    if query.from_user.id not in ADMIN_IDS:
        return
    qr = get_setting("qr_code")
    text = "📷 **QR Code Settings**\n\n"
    text += "✅ QR Code is set.\n" if qr else "❌ No QR Code set.\n"
    text += "\nTap the button to set a new QR code."
    keyboard = [
        [InlineKeyboardButton("✏️ Set QR Code", callback_data="edit_qr")],
        [InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_panel")]
    ]
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard))

async def show_admin_howto(query, context):
    if query.from_user.id not in ADMIN_IDS:
        return
    vid = get_setting("how_to_use_video")
    text = "📖 **How To Use Video**\n\n"
    text += "✅ Video is set.\n" if vid else "❌ No video set.\n"
    text += "\nTap the button to set a new how-to video."
    keyboard = [
        [InlineKeyboardButton("✏️ Set Video", callback_data="edit_howto")],
        [InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_panel")]
    ]
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard))

async def show_admin_orders(query, context, status):
    if query.from_user.id not in ADMIN_IDS:
        return
    orders = get_orders_by_status(status)
    emoji = {"pending": "📦", "approved": "✅", "rejected": "❌"}.get(status, "📋")
    text = f"{emoji} **{status.title()} Orders**\n\n"
    if not orders:
        text += "No orders found."
    else:
        for o in orders:
            text += f"• {o['first_name']} - {o['package_name']} - ₹{o['price']}\n  🆔 ID: {o['id']} | 🕐 {o['created_at'][:19]}\n\n"
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_panel")]]))

async def show_blocked_users(query, context):
    if query.from_user.id not in ADMIN_IDS:
        return
    blocked = get_blocked_users()
    text = "🚫 **Blocked Users**\n\n"
    if not blocked:
        text += "No blocked users."
        await safe_edit(query, text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_panel")]]))
        return
    keyboard = []
    row = []
    for b in blocked:
        row.append(InlineKeyboardButton(f"🔓 {b['first_name']}", callback_data=f"unblock_{b['id']}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_panel")])
    for b in blocked:
        text += f"• {b['first_name']} (@{b.get('username', 'N/A')}) - ID: {b['user_id']}\n"
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard))

async def show_users(query, context):
    if query.from_user.id not in ADMIN_IDS:
        return
    text = f"👥 **Total Users:** {count_users()}\n\n**Recent Users:**\n"
    for u in get_recent_users(10):
        text += f"• {u['first_name']} (@{u.get('username', 'N/A')})\n"
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_panel")]]))

async def show_stats(query, context):
    if query.from_user.id not in ADMIN_IDS:
        return
    text = (
        f"📊 **Bot Statistics**\n\n"
        f"📱 Number Packages: {count_products('number')}\n"
        f"🎬 Video Packages: {count_products('video')}\n"
        f"👥 Total Users: {count_users()}\n"
        f"🚫 Blocked: {count_blocked()}\n\n"
        f"📦 **Orders:**\n"
        f"• Total: {count_orders()}\n"
        f"• ⏳ Pending: {count_orders('pending')}\n"
        f"• ✅ Approved: {count_orders('approved')}\n"
        f"• ❌ Rejected: {count_orders('rejected')}\n"
    )
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_panel")]]))

# ===================== HEALTH SERVER =====================

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass

def run_health_server():
    server = HTTPServer(('0.0.0.0', PORT), HealthHandler)
    logger.info(f"Health server running on port {PORT}")
    server.serve_forever()

# ===================== MAIN =====================

def main():
    init_db()
    logger.info("Database initialized")
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    # ONLY THREE HANDLERS:
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.ALL, handle_messages))
    
    logger.info("Bot started successfully!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()
    main()
