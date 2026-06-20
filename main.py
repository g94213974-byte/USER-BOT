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
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(",")))
PORT = int(os.getenv("PORT", 10000))

# ===================== LOGGING =====================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===================== DATABASE =====================
DB_FILE = "bot_data.db"

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, first_name TEXT, username TEXT, last_interaction TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, first_name TEXT, username TEXT, type TEXT, package_id TEXT, package_name TEXT, price REAL, quantity INTEGER DEFAULT 1, delivery_link TEXT DEFAULT '', screenshot_file_id TEXT, status TEXT DEFAULT 'pending', created_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT, name TEXT, price REAL, quantity INTEGER DEFAULT 1, delivery_link TEXT DEFAULT '', position INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS blocked_users (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, first_name TEXT, username TEXT, blocked_at TEXT)''')
    
    defaults = {"upi_id": "customupi@bank", "qr_code": "", "how_to_use_video": "", "number_buttons_position": "horizontal", "video_buttons_position": "horizontal"}
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

def save_user(user_id, first_name, username):
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO users (user_id, first_name, username, last_interaction) VALUES (?, ?, ?, ?)", (user_id, first_name, username, datetime.datetime.now().isoformat()))
    conn.commit()
    conn.close()

def count_users():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    return c.fetchone()[0]

def get_recent_users(limit=10):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users ORDER BY last_interaction DESC LIMIT ?", (limit,))
    return [dict(row) for row in c.fetchall()]

def add_product(type_name, name, price, extra):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM products WHERE type = ?", (type_name,))
    count = c.fetchone()[0]
    if type_name == "number":
        c.execute("INSERT INTO products (type, name, price, quantity, position) VALUES (?, ?, ?, ?, ?)", (type_name, name, price, int(extra), count + 1))
    else:
        c.execute("INSERT INTO products (type, name, price, delivery_link, position) VALUES (?, ?, ?, ?, ?)", (type_name, name, price, extra, count + 1))
    conn.commit()
    conn.close()

def get_products(type_name):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM products WHERE type = ? ORDER BY position", (type_name,))
    return [dict(row) for row in c.fetchall()]

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

def count_products(type_name):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM products WHERE type = ?", (type_name,))
    return c.fetchone()[0]

def create_order(user_id, first_name, username, p_type, package_id, package_name, price, quantity, delivery_link, screenshot_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('''INSERT INTO orders (user_id, first_name, username, type, package_id, package_name, price, quantity, delivery_link, screenshot_file_id, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)''', (user_id, first_name, username, p_type, str(package_id), package_name, price, quantity, delivery_link, screenshot_id, datetime.datetime.now().isoformat()))
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
    return [dict(row) for row in c.fetchall()]

def count_orders(status=None):
    conn = get_db()
    c = conn.cursor()
    if status:
        c.execute("SELECT COUNT(*) FROM orders WHERE status = ?", (status,))
    else:
        c.execute("SELECT COUNT(*) FROM orders")
    return c.fetchone()[0]

def block_user(user_id, first_name, username):
    conn = get_db()
    c = conn.cursor()
    existing = c.execute("SELECT * FROM blocked_users WHERE user_id = ?", (user_id,)).fetchone()
    if not existing:
        c.execute("INSERT INTO blocked_users (user_id, first_name, username, blocked_at) VALUES (?, ?, ?, ?)", (user_id, first_name, username, datetime.datetime.now().isoformat()))
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
    return c.fetchone() is not None

def get_blocked_users():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM blocked_users ORDER BY blocked_at DESC")
    return [dict(row) for row in c.fetchall()]

def count_blocked():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM blocked_users")
    return c.fetchone()[0]

async def safe_edit(query, text, reply_markup=None):
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except Exception as e:
        if "Message is not modified" not in str(e):
            raise e

async def safe_edit_caption(query, caption, reply_markup=None):
    try:
        await query.edit_message_caption(caption=caption, reply_markup=reply_markup)
    except Exception as e:
        if "Message is not modified" not in str(e):
            raise e

# ===================== START =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    save_user(user.id, user.first_name, user.username)
    if is_blocked(user.id):
        await update.message.reply_text("⛔ You have been blocked.")
        return
    keyboard = [
        [InlineKeyboardButton("📱 Buy Number", callback_data="buy_number")],
        [InlineKeyboardButton("🎬 Buy Video", callback_data="buy_video")],
        [InlineKeyboardButton("📖 How To Use", callback_data="how_to_use")]
    ]
    if user.id in ADMIN_IDS:
        keyboard.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_panel")])
    await update.message.reply_text("👋 Welcome! Please select an option:", reply_markup=InlineKeyboardMarkup(keyboard))

# ===================== BUTTON HANDLER =====================
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if is_blocked(user_id):
        await safe_edit(query, "⛔ You have been blocked.")
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
        await safe_edit(query, "❌ Payment cancelled.")
    elif data.startswith("pay_"):
        parts = data.split("_")
        context.user_data["pending_pkg_id"] = int(parts[2])
        context.user_data["pending_type"] = parts[1]
        context.user_data["waiting_for_screenshot"] = True
        await safe_edit(query, "📸 Please send your payment screenshot.\n\n❌ Press below to cancel:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_payment")]]))
    elif data.startswith("approve_"):
        await approve_order(query, context, int(data.replace("approve_", "")))
    elif data.startswith("reject_"):
        await reject_order(query, context, int(data.replace("reject_", "")))
    elif data.startswith("block_"):
        await block_user_from_order(query, context, int(data.replace("block_", "")))
    elif data.startswith("unblock_"):
        unblock_user(int(data.replace("unblock_", "")))
        await safe_edit(query, "✅ User has been unblocked.")
    elif data == "admin_panel":
        await show_admin_panel(query, context)
    elif data.startswith("del_product_"):
        if query.from_user.id in ADMIN_IDS:
            pid = int(data.replace("del_product_", ""))
            p = get_product(pid)
            delete_product(pid)
            msg = f"✅ Product '{p['name'] if p else ''}' (ID: {pid}) deleted!"
            await safe_edit(query, msg)
            if p and p['type'] == 'number':
                await admin_number_products(query, context)
            elif p and p['type'] == 'video':
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
    elif data.startswith("set_upi_"):
        if query.from_user.id in ADMIN_IDS:
            upi = data.replace("set_upi_", "")
            update_setting("upi_id", upi)
            await safe_edit(query, f"✅ UPI ID updated to: {upi}")
    elif data == "admin_back_panel":
        await show_admin_panel(query, context)

# ===================== USER FACING =====================
async def show_how_to_use(query, context):
    video_id = get_setting("how_to_use_video")
    if video_id:
        try:
            await context.bot.send_video(chat_id=query.message.chat_id, video=video_id, caption="📖 Watch this video to learn how to use the bot.")
        except:
            pass
    await safe_edit(query, "📖 How To Use", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_main")]]))

async def show_number_products(query, context):
    products = get_products("number")
    if not products:
        await safe_edit(query, "❌ No number packages available yet.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_main")]]))
        return
    keyboard = []
    row = []
    for p in products:
        row.append(InlineKeyboardButton(f"{p['name']} - ₹{p['price']}", callback_data=f"num_pkg_{p['id']}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_main")])
    await safe_edit(query, "📱 Select a number package:", reply_markup=InlineKeyboardMarkup(keyboard))

async def show_video_products(query, context):
    products = get_products("video")
    if not products:
        await safe_edit(query, "❌ No video packages available yet.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_main")]]))
        return
    keyboard = []
    row = []
    for p in products:
        row.append(InlineKeyboardButton(f"{p['name']} - ₹{p['price']}", callback_data=f"vid_pkg_{p['id']}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_main")])
    await safe_edit(query, "🎬 Select a video package:", reply_markup=InlineKeyboardMarkup(keyboard))

async def show_payment(query, context, p_type, pkg_id):
    product = get_product(pkg_id)
    if not product:
        await safe_edit(query, "❌ Package not found.")
        return
    upi_id = get_setting("upi_id") or "customupi@bank"
    qr_code = get_setting("qr_code")
    payment_text = f"\n━━━━━━━━━━━━━━━━━━━━\n💳 UPI Payment\n━━━━━━━━━━━━━━━━━━━━\n\n💰 Amount: ₹{product['price']}\n🏦 UPI ID: {upi_id}\n\n📱 PhonePe / GPay / Paytm\n\n⚠️ Pay the EXACT amount\n\n━━━━━━━━━━━━━━━━━━━━\n"
    keyboard = [[InlineKeyboardButton("📸 I Have Paid", callback_data=f"pay_{p_type}_{pkg_id}")], [InlineKeyboardButton("🔙 Back", callback_data=f"back_{p_type}s")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if qr_code:
        try:
            await context.bot.send_photo(chat_id=query.message.chat_id, photo=qr_code, caption=payment_text, reply_markup=reply_markup)
            try:
                await query.message.delete()
            except:
                pass
            return
        except:
            pass
    await safe_edit(query, payment_text, reply_markup=reply_markup)

async def handle_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.user_data.get("waiting_for_screenshot"):
        return
    pending = get_pending_order_by_user(user.id)
    if pending:
        await update.message.reply_text("⏳ Your payment is already under review.\n\nPlease wait for verification.")
        return
    if not update.message.photo:
        await update.message.reply_text("📸 Please send a screenshot image.")
        return
    photo = update.message.photo[-1]
    pkg_id = context.user_data.get("pending_pkg_id")
    p_type = context.user_data.get("pending_type", "number")
    product = get_product(pkg_id) if pkg_id else None
    if not product:
        await update.message.reply_text("❌ Package not found. Please try again.")
        return
    order_id = create_order(user.id, user.first_name, user.username, p_type, pkg_id, product['name'], product['price'], product.get('quantity', 1), product.get('delivery_link', ''), photo.file_id)
    context.user_data["waiting_for_screenshot"] = False
    context.user_data["pending_pkg_id"] = None
    context.user_data["pending_type"] = None
    await update.message.reply_text("✅ Payment screenshot received!\n\n⏳ Please wait 5–30 minutes while we verify your payment.")
    await notify_admins(context, user, product, photo.file_id, p_type, order_id)

async def notify_admins(context, user, product, screenshot_id, p_type, order_id):
    pkg_emoji = "📱" if p_type == "number" else "🎬"
    text = f"\n{pkg_emoji} New Order Received!\n\n👤 Name: {user.first_name}\n🆔 User ID: {user.id}\n📛 Username: @{user.username if user.username else 'N/A'}\n\n📦 Package: {product['name']}\n💰 Price: ₹{product['price']}\n"
    keyboard = [[InlineKeyboardButton("✅ APPROVE", callback_data=f"approve_{order_id}"), InlineKeyboardButton("❌ REJECT", callback_data=f"reject_{order_id}")], [InlineKeyboardButton("🚫 BLOCK", callback_data=f"block_{order_id}")]]
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_photo(chat_id=admin_id, photo=screenshot_id, caption=text, reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception as e:
            logger.error(f"Could not notify admin {admin_id}: {e}")

async def approve_order(query, context, order_id):
    order = get_order(order_id)
    if not order:
        await safe_edit(query, "❌ Order not found.")
        return
    update_order_status(order_id, "approved")
    if order["type"] == "number":
        qty = order.get("quantity", 1)
        txt = "✅ Payment Approved!\n\n📱 Your Numbers:\n\n"
        for i in range(qty):
            txt += f"{i+1}. +9112345{random.randint(10000, 99999)}\n"
        txt += "\n❤️ Thank you!\nKeep 2-Step Verification ON. 🔒"
        try:
            await context.bot.send_message(chat_id=order["user_id"], text=txt)
        except:
            pass
    elif order["type"] == "video":
        try:
            await context.bot.send_message(chat_id=order["user_id"], text=f"✅ Payment Approved!\n\n🔗 Access Link:\n\n{order.get('delivery_link', '')}")
        except:
            pass
    await safe_edit_caption(query, caption=f"✅ Approved!\n{order['package_name']}\n₹{order['price']}\nUser: {order['first_name']}")

async def reject_order(query, context, order_id):
    order = get_order(order_id)
    if not order:
        await safe_edit(query, "❌ Order not found.")
        return
    update_order_status(order_id, "rejected")
    try:
        await context.bot.send_message(chat_id=order["user_id"], text="❌ Payment rejected.\nContact support.")
    except:
        pass
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
    await safe_edit(query, "👋 Welcome! Please select an option:", reply_markup=InlineKeyboardMarkup(keyboard))

# ===================== ADMIN PANEL =====================
async def show_admin_panel(query, context):
    if query.from_user.id not in ADMIN_IDS:
        return
    keyboard = [
        [InlineKeyboardButton("📱 Number Products", callback_data="admin_numbers"), InlineKeyboardButton("🎬 Video Products", callback_data="admin_videos")],
        [InlineKeyboardButton("💳 Payment", callback_data="admin_payment"), InlineKeyboardButton("📷 QR Code", callback_data="admin_qr")],
        [InlineKeyboardButton("📖 HowTo Video", callback_data="admin_howto"), InlineKeyboardButton("📦 Pending", callback_data="admin_pending")],
        [InlineKeyboardButton("✅ Approved", callback_data="admin_approved"), InlineKeyboardButton("❌ Rejected", callback_data="admin_rejected")],
        [InlineKeyboardButton("🚫 Blocked", callback_data="admin_blocked"), InlineKeyboardButton("👥 Users", callback_data="admin_users")],
        [InlineKeyboardButton("📊 Stats", callback_data="admin_stats")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
    ]
    await safe_edit(query, "⚙️ Admin Panel\n\nChoose an option:", reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_number_products(query, context):
    if query.from_user.id not in ADMIN_IDS:
        return
    products = get_products("number")
    text = "📱 Number Packages:\n\n"
    if not products:
        text += "No packages yet.\n"
    else:
        for p in products:
            text += f"• {p['name']} - ₹{p['price']} ({p.get('quantity', 1)} nos) — ID: {p['id']}\n"
    text += "\nAdd: /add_number Name Price Qty\n(Example: /add_number 5_Numbers 80 5)"
    keyboard = []
    row = []
    for p in products:
        row.append(InlineKeyboardButton(f"🗑️ Del #{p['id']}", callback_data=f"del_product_{p['id']}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_video_products(query, context):
    if query.from_user.id not in ADMIN_IDS:
        return
    products = get_products("video")
    text = "🎬 Video Packages:\n\n"
    if not products:
        text += "No packages yet.\n"
    else:
        for p in products:
            text += f"• {p['name']} - ₹{p['price']} — ID: {p['id']}\n  Link: {p.get('delivery_link', 'N/A')}\n"
    text += "\nAdd: /add_video Name Price Link\n(Example: /add_video Fast 50 t.me/link)"
    keyboard = []
    row = []
    for p in products:
        row.append(InlineKeyboardButton(f"🗑️ Del #{p['id']}", callback_data=f"del_product_{p['id']}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard))

async def show_admin_payment(query, context):
    if query.from_user.id not in ADMIN_IDS:
        return
    upi = get_setting("upi_id") or "customupi@bank"
    text = f"💳 Payment Settings\n\n🏦 UPI ID: {upi}\n\nTo change - Send: /set_upi NEW_UPI_ID"
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard))

async def show_admin_qr(query, context):
    if query.from_user.id not in ADMIN_IDS:
        return
    qr = get_setting("qr_code")
    text = "📷 QR Code Settings\n\n"
    text += "✅ QR Code is set.\n" if qr else "❌ No QR Code set.\n"
    text += "\nTo set: Reply to a photo with /set_qr"
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard))

async def show_admin_howto(query, context):
    if query.from_user.id not in ADMIN_IDS:
        return
    vid = get_setting("how_to_use_video")
    text = "📖 How To Use Video\n\n"
    text += "✅ Video is set.\n" if vid else "❌ No video set.\n"
    text += "\nTo set: Reply to a video with /set_howto"
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard))

async def show_admin_orders(query, context, status):
    if query.from_user.id not in ADMIN_IDS:
        return
    orders = get_orders_by_status(status)
    emoji = {"pending": "📦", "approved": "✅", "rejected": "❌"}.get(status, "📋")
    text = f"{emoji} {status.title()} Orders:\n\n"
    if not orders:
        text += "No orders found."
    else:
        for o in orders:
            text += f"• {o['first_name']} - {o['package_name']} - ₹{o['price']}\n  ID: {o['id']}\n\n"
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard))

async def show_blocked_users(query, context):
    if query.from_user.id not in ADMIN_IDS:
        return
    blocked = get_blocked_users()
    text = "🚫 Blocked Users:\n\n"
    if not blocked:
        text += "No blocked users."
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]
        await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard))
        return
    keyboard = []
    row = []
    for b in blocked:
        text += f"• {b['first_name']} (@{b.get('username', 'N/A')}) - ID: {b['user_id']}\n"
        row.append(InlineKeyboardButton(f"🔓 {b['first_name']}", callback_data=f"unblock_{b['id']}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard))

async def show_users(query, context):
    if query.from_user.id not in ADMIN_IDS:
        return
    text = f"👥 Total Users: {count_users()}\n\nRecent Users:\n"
    for u in get_recent_users(10):
        text += f"• {u['first_name']} (@{u.get('username', 'N/A')})\n"
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard))

async def show_stats(query, context):
    if query.from_user.id not in ADMIN_IDS:
        return
    text = f"\n📊 Statistics\n\n📱 Number Packages: {count_products('number')}\n🎬 Video Packages: {count_products('video')}\n👥 Total Users: {count_users()}\n🚫 Blocked: {count_blocked()}\n\n📦 Orders: {count_orders()}\n⏳ Pending: {count_orders('pending')}\n✅ Approved: {count_orders('approved')}\n❌ Rejected: {count_orders('rejected')}\n"
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard))

# ===================== ADMIN TEXT COMMANDS (only 5 needed) =====================
async def add_number_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    try:
        args = context.args
        if len(args) < 3:
            await update.message.reply_text("Usage: /add_number Name Price Qty\nExample: /add_number 5_Numbers 80 5")
            return
        name = " ".join(args[:-2]).replace("_", " ")
        price = int(args[-2])
        qty = int(args[-1])
        add_product("number", name, price, qty)
        await update.message.reply_text(f"✅ '{name}' added - ₹{price} ({qty} numbers)")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def add_video_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    try:
        args = context.args
        if len(args) < 3:
            await update.message.reply_text("Usage: /add_video Name Price Link\nExample: /add_video Fast 50 t.me/link")
            return
        name = " ".join(args[:-2]).replace("_", " ")
        price = int(args[-2])
        link = args[-1]
        add_product("video", name, price, link)
        await update.message.reply_text(f"✅ '{name}' added - ₹{price}")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def del_product_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    try:
        pid = int(context.args[0])
        p = get_product(pid)
        delete_product(pid)
        await update.message.reply_text(f"✅ Product '{p['name'] if p else ''}' (ID: {pid}) deleted!")
    except:
        await update.message.reply_text("Usage: /del_product [id]")

async def set_upi_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.args:
        await update.message.reply_text("Usage: /set_upi [upi_id]")
        return
    update_setting("upi_id", " ".join(context.args))
    await update.message.reply_text(f"✅ UPI ID updated!")

async def set_qr_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if update.message.reply_to_message and update.message.reply_to_message.photo:
        update_setting("qr_code", update.message.reply_to_message.photo[-1].file_id)
        await update.message.reply_text("✅ QR Code updated!")
    else:
        await update.message.reply_text("Reply to a photo with /set_qr")

async def set_howto_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if update.message.reply_to_message and update.message.reply_to_message.video:
        update_setting("how_to_use_video", update.message.reply_to_message.video.file_id)
        await update.message.reply_text("✅ How To Use video updated!")
    else:
        await update.message.reply_text("Reply to a video with /set_howto")

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
    HTTPServer(('0.0.0.0', PORT), HealthHandler).serve_forever()

# ===================== MAIN =====================
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    
    # শুধু প্রয়োজনীয় কমান্ড (বাকি সব বাটন-ভিত্তিক)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add_number", add_number_cmd))
    app.add_handler(CommandHandler("add_video", add_video_cmd))
    app.add_handler(CommandHandler("del_product", del_product_cmd))
    app.add_handler(CommandHandler("set_upi", set_upi_cmd))
    app.add_handler(CommandHandler("set_qr", set_qr_cmd))
    app.add_handler(CommandHandler("set_howto", set_howto_cmd))
    
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_screenshot))
    
    logger.info("Bot started successfully!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    threading.Thread(target=run_health_server, daemon=True).start()
    main()
