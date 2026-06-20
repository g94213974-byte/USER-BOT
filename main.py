#!/usr/bin/env python3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from config import BOT_TOKEN, ADMIN_IDS
from database import *
import logging
import datetime
from bson.objectid import ObjectId

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===================== MAIN MENU =====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    users_col.update_one(
        {"user_id": user.id},
        {"$set": {
            "user_id": user.id,
            "first_name": user.first_name,
            "username": user.username,
            "last_interaction": datetime.datetime.now()
        }},
        upsert=True
    )
    
    if blocked_col.find_one({"user_id": user.id}):
        await update.message.reply_text("⛔ You have been blocked.")
        return
    
    keyboard = [
        [InlineKeyboardButton("📱 Buy Number", callback_data="buy_number")],
        [InlineKeyboardButton("🎬 Buy Video", callback_data="buy_video")],
        [InlineKeyboardButton("📖 How To Use", callback_data="how_to_use")]
    ]
    
    # Show admin button for admins
    if user.id in ADMIN_IDS:
        keyboard.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_panel")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "👋 Welcome! Please select an option:",
        reply_markup=reply_markup
    )

# ===================== CALLBACK HANDLER =====================

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    
    if blocked_col.find_one({"user_id": user_id}):
        await query.edit_message_text("⛔ You have been blocked.")
        return
    
    data = query.data
    
    if data == "how_to_use":
        await show_how_to_use(query, context)
    
    elif data == "buy_number":
        await show_number_products(query, context)
    
    elif data == "buy_video":
        await show_video_products(query, context)
    
    elif data.startswith("num_pkg_"):
        pkg_id = data.replace("num_pkg_", "")
        await show_payment(query, context, "number", pkg_id)
    
    elif data.startswith("vid_pkg_"):
        pkg_id = data.replace("vid_pkg_", "")
        await show_payment(query, context, "video", pkg_id)
    
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
        await query.edit_message_text("❌ Payment cancelled.")
    
    elif data.startswith("pay_"):
        parts = data.split("_")
        p_type = parts[1]
        pkg_id = parts[2]
        context.user_data["pending_pkg_id"] = pkg_id
        context.user_data["pending_type"] = p_type
        context.user_data["waiting_for_screenshot"] = True
        
        await query.edit_message_text(
            "📸 Please send your payment screenshot.\n\n"
            "❌ Press below to cancel:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel_payment")]
            ])
        )
    
    # ===== ADMIN CALLBACKS =====
    elif data.startswith("approve_"):
        order_id = data.replace("approve_", "")
        await approve_order(query, context, order_id)
    
    elif data.startswith("reject_"):
        order_id = data.replace("reject_", "")
        await reject_order(query, context, order_id)
    
    elif data.startswith("block_"):
        order_id = data.replace("block_", "")
        await block_user_from_order(query, context, order_id)
    
    elif data.startswith("unblock_"):
        block_id = data.replace("unblock_", "")
        blocked_col.delete_one({"_id": ObjectId(block_id)})
        await query.edit_message_text("✅ User has been unblocked.")
    
    # ===== ADMIN PANEL =====
    elif data == "admin_panel":
        await show_admin_panel(query, context)
    
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

# ===================== HOW TO USE =====================

async def show_how_to_use(query, context):
    video_id = get_setting("how_to_use_video")
    
    if video_id:
        await context.bot.send_video(
            chat_id=query.message.chat_id,
            video=video_id,
            caption="📖 Watch this video to learn how to use the bot."
        )
    
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_main")]]
    await query.edit_message_text(
        "📖 How To Use",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ===================== NUMBER PRODUCTS =====================

async def show_number_products(query, context):
    products = list(products_col.find({"type": "number"}).sort("position", 1))
    
    if not products:
        await query.edit_message_text("❌ No number packages available yet.")
        return
    
    keyboard = []
    position = get_setting("number_buttons_position") or "vertical"
    
    row = []
    for p in products:
        btn = InlineKeyboardButton(f"{p['name']} - ₹{p['price']}", callback_data=f"num_pkg_{p['_id']}")
        if position == "horizontal":
            row.append(btn)
            if len(row) == 2:
                keyboard.append(row)
                row = []
        else:
            keyboard.append([btn])
    
    if row:
        keyboard.append(row)
    
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_main")])
    await query.edit_message_text("📱 Select a number package:", reply_markup=InlineKeyboardMarkup(keyboard))

# ===================== VIDEO PRODUCTS =====================

async def show_video_products(query, context):
    products = list(products_col.find({"type": "video"}).sort("position", 1))
    
    if not products:
        await query.edit_message_text("❌ No video packages available yet.")
        return
    
    keyboard = []
    position = get_setting("video_buttons_position") or "vertical"
    
    row = []
    for p in products:
        btn = InlineKeyboardButton(f"{p['name']} - ₹{p['price']}", callback_data=f"vid_pkg_{p['_id']}")
        if position == "horizontal":
            row.append(btn)
            if len(row) == 2:
                keyboard.append(row)
                row = []
        else:
            keyboard.append([btn])
    
    if row:
        keyboard.append(row)
    
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_main")])
    await query.edit_message_text("🎬 Select a video package:", reply_markup=InlineKeyboardMarkup(keyboard))

# ===================== PAYMENT PAGE =====================

async def show_payment(query, context, p_type, pkg_id):
    product = products_col.find_one({"_id": ObjectId(pkg_id)})
    if not product:
        await query.edit_message_text("❌ Package not found.")
        return
    
    upi_id = get_setting("upi_id") or "customupi@bank"
    qr_code = get_setting("qr_code")
    
    payment_text = f"""
━━━━━━━━━━━━━━━━━━━━
💳 UPI Payment
━━━━━━━━━━━━━━━━━━━━

💰 Amount: ₹{product['price']}
🏦 UPI ID: {upi_id}

📱 PhonePe / GPay / Paytm

⚠️ Pay the EXACT amount

━━━━━━━━━━━━━━━━━━━━
"""
    
    keyboard = [
        [InlineKeyboardButton("📸 I Have Paid", callback_data=f"pay_{p_type}_{pkg_id}")],
        [InlineKeyboardButton("🔙 Back", callback_data=f"back_{p_type}s")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if qr_code:
        try:
            await context.bot.send_photo(
                chat_id=query.message.chat_id,
                photo=qr_code,
                caption=payment_text,
                reply_markup=reply_markup
            )
            try:
                await query.message.delete()
            except:
                pass
            return
        except:
            pass
    
    await query.edit_message_text(payment_text, reply_markup=reply_markup)

# ===================== SCREENSHOT HANDLING =====================

async def handle_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    if not context.user_data.get("waiting_for_screenshot"):
        return
    
    # Check if user already has a pending order
    pending = orders_col.find_one({
        "user_id": user.id,
        "status": "pending"
    })
    
    if pending:
        await update.message.reply_text(
            "⏳ Your payment is already under review.\n\n"
            "Please wait for verification. Sending multiple screenshots will not speed up the process."
        )
        return
    
    if not update.message.photo:
        await update.message.reply_text("📸 Please send a screenshot image.")
        return
    
    photo = update.message.photo[-1]
    pkg_id = context.user_data.get("pending_pkg_id")
    p_type = context.user_data.get("pending_type", "number")
    
    product = products_col.find_one({"_id": ObjectId(pkg_id)}) if pkg_id else None
    
    if not product:
        await update.message.reply_text("❌ Package not found. Please try again.")
        return
    
    order = {
        "user_id": user.id,
        "first_name": user.first_name,
        "username": user.username,
        "type": p_type,
        "package_id": pkg_id,
        "package_name": product["name"],
        "price": product["price"],
        "quantity": product.get("quantity", 1),
        "delivery_link": product.get("delivery_link", ""),
        "screenshot_file_id": photo.file_id,
        "status": "pending",
        "created_at": datetime.datetime.now()
    }
    
    result = orders_col.insert_one(order)
    order_id = str(result.inserted_id)
    
    context.user_data["waiting_for_screenshot"] = False
    context.user_data["pending_pkg_id"] = None
    context.user_data["pending_type"] = None
    
    await update.message.reply_text(
        "✅ Payment screenshot received!\n\n"
        "⏳ Please wait 5–30 minutes while we verify your payment. 💳\n\n"
        "🔔 You will be notified automatically once your payment is approved.\n\n"
        "⚠️ Please do not send multiple screenshots or make repeated payments while your order is being reviewed."
    )
    
    await notify_admins(context, order, order_id)

# ===================== NOTIFY ADMINS =====================

async def notify_admins(context, order, order_id):
    pkg_emoji = "📱" if order["type"] == "number" else "🎬"
    
    text = f"""
{pkg_emoji} New Order Received!

👤 Name: {order['first_name']}
🆔 User ID: {order['user_id']}
📛 Username: @{order['username'] if order['username'] else 'N/A'}

📦 Package: {order['package_name']}
💰 Price: ₹{order['price']}
    """
    
    keyboard = [
        [
            InlineKeyboardButton("✅ APPROVED", callback_data=f"approve_{order_id}"),
            InlineKeyboardButton("❌ REJECT", callback_data=f"reject_{order_id}"),
        ],
        [InlineKeyboardButton("🚫 BLOCK", callback_data=f"block_{order_id}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_photo(
                chat_id=admin_id,
                photo=order["screenshot_file_id"],
                caption=text,
                reply_markup=reply_markup
            )
        except Exception as e:
            logger.error(f"Could not notify admin {admin_id}: {e}")

# ===================== ADMIN ACTIONS =====================

async def approve_order(query, context, order_id):
    order = orders_col.find_one({"_id": ObjectId(order_id)})
    if not order:
        await query.edit_message_text("❌ Order not found.")
        return
    
    orders_col.update_one(
        {"_id": ObjectId(order_id)},
        {"$set": {"status": "approved", "approved_at": datetime.datetime.now()}}
    )
    
    user_id = order["user_id"]
    
    if order["type"] == "number":
        quantity = order.get("quantity", 1)
        
        numbers_text = "✅ Payment Approved!\n\n📱 Your Numbers:\n\n"
        
        for i in range(quantity):
            numbers_text += f"{i+1}. +9112345678{i+1:03d}\n"
        
        numbers_text += """
❤️ Thank you for buying from us!

This is a virtual number.
Keep 2-Step Verification ON to protect your account.
Otherwise, someone else may get access to it. 🔒
        """
        
        try:
            await context.bot.send_message(chat_id=user_id, text=numbers_text)
        except Exception as e:
            logger.error(f"Could not send to user {user_id}: {e}")
    
    elif order["type"] == "video":
        link = order.get("delivery_link", "")
        
        text = f"✅ Payment Approved!\n\n🔗 Access Link:\n\n{link}"
        
        try:
            await context.bot.send_message(chat_id=user_id, text=text)
        except Exception as e:
            logger.error(f"Could not send to user {user_id}: {e}")
    
    await query.edit_message_caption(
        caption=f"✅ Order Approved!\n\nPackage: {order['package_name']}\nPrice: ₹{order['price']}\nUser: {order['first_name']}"
    )

async def reject_order(query, context, order_id):
    order = orders_col.find_one({"_id": ObjectId(order_id)})
    if not order:
        await query.edit_message_text("❌ Order not found.")
        return
    
    orders_col.update_one(
        {"_id": ObjectId(order_id)},
        {"$set": {"status": "rejected", "rejected_at": datetime.datetime.now()}}
    )
    
    user_id = order["user_id"]
    
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text="❌ Payment rejected.\n\nPlease contact support if needed."
        )
    except Exception as e:
        logger.error(f"Could not send to user {user_id}: {e}")
    
    await query.edit_message_caption(
        caption=f"❌ Order Rejected!\n\nPackage: {order['package_name']}\nPrice: ₹{order['price']}\nUser: {order['first_name']}"
    )

async def block_user_from_order(query, context, order_id):
    order = orders_col.find_one({"_id": ObjectId(order_id)})
    if not order:
        await query.edit_message_text("❌ Order not found.")
        return
    
    user_id = order["user_id"]
    
    # Check if already blocked
    if not blocked_col.find_one({"user_id": user_id}):
        blocked_col.insert_one({
            "user_id": user_id,
            "first_name": order["first_name"],
            "username": order["username"],
            "blocked_at": datetime.datetime.now()
        })
    
    orders_col.update_one(
        {"_id": ObjectId(order_id)},
        {"$set": {"status": "blocked"}}
    )
    
    await query.edit_message_caption(
        caption=f"🚫 User Blocked!\n\nName: {order['first_name']}\nUser ID: {user_id}"
    )

# ===================== BACK TO MAIN =====================

async def back_to_main(query, context):
    keyboard = [
        [InlineKeyboardButton("📱 Buy Number", callback_data="buy_number")],
        [InlineKeyboardButton("🎬 Buy Video", callback_data="buy_video")],
        [InlineKeyboardButton("📖 How To Use", callback_data="how_to_use")]
    ]
    
    if query.from_user.id in ADMIN_IDS:
        keyboard.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_panel")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "👋 Welcome! Please select an option:",
        reply_markup=reply_markup
    )

# ===================== ADMIN PANEL =====================

async def show_admin_panel(query, context):
    if query.from_user.id not in ADMIN_IDS:
        return
    
    keyboard = [
        [InlineKeyboardButton("📱 Number Products", callback_data="admin_numbers")],
        [InlineKeyboardButton("🎬 Video Products", callback_data="admin_videos")],
        [InlineKeyboardButton("💳 Payment Settings", callback_data="admin_payment")],
        [InlineKeyboardButton("📷 QR Code Settings", callback_data="admin_qr")],
        [InlineKeyboardButton("📖 How To Use Video", callback_data="admin_howto")],
        [InlineKeyboardButton("📦 Pending Orders", callback_data="admin_pending")],
        [InlineKeyboardButton("✅ Approved Orders", callback_data="admin_approved")],
        [InlineKeyboardButton("❌ Rejected Orders", callback_data="admin_rejected")],
        [InlineKeyboardButton("🚫 Blocked Users", callback_data="admin_blocked")],
        [InlineKeyboardButton("👥 Users", callback_data="admin_users")],
        [InlineKeyboardButton("📊 Statistics", callback_data="admin_stats")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
    ]
    
    await query.edit_message_text(
        "⚙️ Admin Panel\n\nChoose an option:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def admin_number_products(query, context):
    if query.from_user.id not in ADMIN_IDS:
        return
    
    products = list(products_col.find({"type": "number"}).sort("position", 1))
    
    text = "📱 Number Packages:\n\n"
    if not products:
        text += "No packages yet.\n"
    else:
        for p in products:
            text += f"• {p['name']} - ₹{p['price']} ({p.get('quantity', 1)} numbers)\n"
            text += f"  ID: {str(p['_id'])[:12]}...\n\n"
    
    text += "\nCommands:\n"
    text += "/add_number [name] [price] [quantity]\n"
    text += "/del_product [id]\n"
    text += "/pos_number [vertical/horizontal]"
    
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_video_products(query, context):
    if query.from_user.id not in ADMIN_IDS:
        return
    
    products = list(products_col.find({"type": "video"}).sort("position", 1))
    
    text = "🎬 Video Packages:\n\n"
    if not products:
        text += "No packages yet.\n"
    else:
        for p in products:
            text += f"• {p['name']} - ₹{p['price']}\n"
            text += f"  Link: {p.get('delivery_link', 'N/A')}\n"
            text += f"  ID: {str(p['_id'])[:12]}...\n\n"
    
    text += "\nCommands:\n"
    text += "/add_video [name] [price] [link]\n"
    text += "/del_product [id]\n"
    text += "/pos_video [vertical/horizontal]"
    
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def show_admin_payment(query, context):
    if query.from_user.id not in ADMIN_IDS:
        return
    
    upi_id = get_setting("upi_id") or "customupi@bank"
    
    text = f"""
💳 Payment Settings

🏦 Current UPI ID: {upi_id}

To change UPI ID:
/set_upi [new_upi_id]
"""
    
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def show_admin_qr(query, context):
    if query.from_user.id not in ADMIN_IDS:
        return
    
    qr_code = get_setting("qr_code")
    
    text = "📷 QR Code Settings\n\n"
    
    if qr_code:
        text += "✅ QR Code is set.\n"
    else:
        text += "❌ No QR Code set.\n"
    
    text += "\nTo set QR Code:\nReply to a photo with /set_qr"
    
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def show_admin_howto(query, context):
    if query.from_user.id not in ADMIN_IDS:
        return
    
    video_id = get_setting("how_to_use_video")
    
    text = "📖 How To Use Video\n\n"
    
    if video_id:
        text += "✅ Video is set.\n"
    else:
        text += "❌ No video set.\n"
    
    text += "\nTo set video:\nReply to a video with /set_howto"
    
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def show_admin_orders(query, context, status):
    if query.from_user.id not in ADMIN_IDS:
        return
    
    orders = list(orders_col.find({"status": status}).sort("created_at", -1).limit(20))
    
    status_emoji = {"pending": "📦", "approved": "✅", "rejected": "❌"}
    emoji = status_emoji.get(status, "📋")
    
    text = f"{emoji} {status.title()} Orders:\n\n"
    
    if not orders:
        text += "No orders found."
    else:
        for o in orders:
            text += f"• {o['first_name']} - {o['package_name']} - ₹{o['price']}\n"
            text += f"  ID: {str(o['_id'])[:12]}...\n\n"
    
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def show_blocked_users(query, context):
    if query.from_user.id not in ADMIN_IDS:
        return
    
    blocked = list(blocked_col.find().sort("blocked_at", -1))
    
    text = "🚫 Blocked Users:\n\n"
    
    if not blocked:
        text += "No blocked users."
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return
    
    keyboard = []
    for b in blocked:
        text += f"• {b['first_name']} (@{b.get('username', 'N/A')}) - ID: {b['user_id']}\n"
        keyboard.append([InlineKeyboardButton(
            f"🔓 Unblock {b['first_name']}",
            callback_data=f"unblock_{str(b['_id'])}"
        )])
    
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def show_users(query, context):
    if query.from_user.id not in ADMIN_IDS:
        return
    
    total = users_col.count_documents({})
    recent = list(users_col.find().sort("last_interaction", -1).limit(10))
    
    text = f"👥 Total Users: {total}\n\nRecent Users:\n"
    
    for u in recent:
        text += f"• {u['first_name']} (@{u.get('username', 'N/A')})\n"
    
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def show_stats(query, context):
    if query.from_user.id not in ADMIN_IDS:
        return
    
    total_orders = orders_col.count_documents({})
    pending = orders_col.count_documents({"status": "pending"})
    approved = orders_col.count_documents({"status": "approved"})
    rejected = orders_col.count_documents({"status": "rejected"})
    total_users = users_col.count_documents({})
    blocked = blocked_col.count_documents({})
    num_products = products_col.count_documents({"type": "number"})
    vid_products = products_col.count_documents({"type": "video"})
    
    text = f"""
📊 Statistics

📱 Number Packages: {num_products}
🎬 Video Packages: {vid_products}
👥 Total Users: {total_users}
🚫 Blocked Users: {blocked}

📦 Total Orders: {total_orders}
⏳ Pending: {pending}
✅ Approved: {approved}
❌ Rejected: {rejected}
"""
    
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

# ===================== ADMIN COMMANDS =====================

async def add_number_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    
    try:
        args = context.args
        if len(args) < 3:
            await update.message.reply_text(
                "Usage: /add_number [name] [price] [quantity]\n"
                "Example: /add_number 5_Numbers 80 5"
            )
            return
        
        name = " ".join(args[:-2]).replace("_", " ")
        price = int(args[-2])
        quantity = int(args[-1])
        count = products_col.count_documents({"type": "number"})
        
        products_col.insert_one({
            "type": "number",
            "name": name,
            "price": price,
            "quantity": quantity,
            "position": count + 1
        })
        
        await update.message.reply_text(f"✅ '{name}' added - ₹{price} ({quantity} numbers)")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}\nUsage: /add_number [name] [price] [quantity]")

async def add_video_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    
    try:
        args = context.args
        if len(args) < 3:
            await update.message.reply_text(
                "Usage: /add_video [name] [price] [link]\n"
                "Example: /add_video Fast 50 t.me/demo"
            )
            return
        
        name = " ".join(args[:-2]).replace("_", " ")
        price = int(args[-2])
        link = args[-1]
        count = products_col.count_documents({"type": "video"})
        
        products_col.insert_one({
            "type": "video",
            "name": name,
            "price": price,
            "delivery_link": link,
            "position": count + 1
        })
        
        await update.message.reply_text(f"✅ '{name}' added - ₹{price}")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}\nUsage: /add_video [name] [price] [link]")

async def delete_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    
    try:
        pkg_id = context.args[0]
        result = products_col.delete_one({"_id": ObjectId(pkg_id)})
        
        if result.deleted_count > 0:
            await update.message.reply_text("✅ Package deleted.")
        else:
            await update.message.reply_text("❌ Package not found.")
    except:
        await update.message.reply_text("Usage: /del_product [package_id]")

async def set_upi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /set_upi [upi_id]")
        return
    
    upi_id = " ".join(context.args)
    update_setting("upi_id", upi_id)
    await update.message.reply_text(f"✅ UPI ID updated: {upi_id}")

async def set_qr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    
    if update.message.reply_to_message and update.message.reply_to_message.photo:
        photo = update.message.reply_to_message.photo[-1]
        update_setting("qr_code", photo.file_id)
        await update.message.reply_text("✅ QR Code updated!")
    else:
        await update.message.reply_text("Reply to a photo with /set_qr")

async def set_how_to_use(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    
    if update.message.reply_to_message and update.message.reply_to_message.video:
        video = update.message.reply_to_message.video
        update_setting("how_to_use_video", video.file_id)
        await update.message.reply_text("✅ How To Use video updated!")
    else:
        await update.message.reply_text("Reply to a video with /set_howto")

async def set_position(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /pos_number [vertical/horizontal] or /pos_video [vertical/horizontal]")
        return
    
    ptype = context.args[0]
    pos = context.args[1].lower()
    
    if pos not in ["vertical", "horizontal"]:
        await update.message.reply_text("Use 'vertical' or 'horizontal'.")
        return
    
    if ptype == "number":
        update_setting("number_buttons_position", pos)
        await update.message.reply_text(f"✅ Number buttons position: {pos}")
    elif ptype == "video":
        update_setting("video_buttons_position", pos)
        await update.message.reply_text(f"✅ Video buttons position: {pos}")
    else:
        await update.message.reply_text("Usage: /pos_number [vertical/horizontal] or /pos_video [vertical/horizontal]")

# ===================== MAIN =====================

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    
    init_settings()
    
    # User commands
    app.add_handler(CommandHandler("start", start))
    
    # Admin commands
    app.add_handler(CommandHandler("add_number", add_number_product))
    app.add_handler(CommandHandler("add_video", add_video_product))
    app.add_handler(CommandHandler("del_product", delete_product))
    app.add_handler(CommandHandler("set_upi", set_upi))
    app.add_handler(CommandHandler("set_qr", set_qr))
    app.add_handler(CommandHandler("set_howto", set_how_to_use))
    app.add_handler(CommandHandler("pos_number", set_position))
    app.add_handler(CommandHandler("pos_video", set_position))
    
    # Callback handler
    app.add_handler(CallbackQueryHandler(button_callback))
    
    # Screenshot handler
    app.add_handler(MessageHandler(filters.PHOTO, handle_screenshot))
    
    logger.info("Bot started successfully!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
