import os
import telebot
import requests
import time
import sqlite3
import threading
import io
import qrcode
import json
import random
import string
import re
from urllib.parse import quote_plus
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from datetime import datetime, timezone, timedelta
from functools import wraps
from flask import Flask, request, jsonify
from cashfree_pg.api_client import APIClient
from cashfree_pg.api.payments_api import PaymentsApi
from cashfree_pg.models.create_payment_link_request import CreatePaymentLinkRequest
from cashfree_pg.models.customer_details import CustomerDetails

# ================= CONFIG =================
BOT_TOKEN = "7826896426:AAEirZuz8SYakBLKKCUUCNEOZvVX5oaFL4o"
SUPER_ADMIN_ID = 5798029484  # The bot owner who can add/remove other admins
API_ENDPOINT = "https://wadownloader.amitdas.site/api/?url="

DAILY_FREE_CREDITS = 5
REFERRAL_BONUS = 2
COOLDOWN_SECONDS = 20

# Cashfree Payment Config
CASHFREE_APP_ID = "TEST109477892f0e5a9f7bb17d4eea4d98774901"
CASHFREE_SECRET_KEY = "cfsk_ma_test_03fdb41cbaa047ecc8596e135afd82b4_585dd4af"
# This should be a publicly accessible URL. For local testing, you can use ngrok.
WEBHOOK_URL = f"https://{os.getenv('RAILWAY_PUBLIC_DOMAIN', 'localhost')}/cashfree_webhook"
RETURN_URL = "https://t.me/YourBotUsername"  # Replace with your actual bot username
# RETURN_URL = f"https://t.me/{bot.get_me().username}" 

# Credit Plans
CREDIT_PLANS = {
    "plan_1": {"name": "Basic", "credits": 50, "price": 29, "popular": False},
    "plan_2": {"name": "Standard", "credits": 120, "price": 59, "popular": True},
    "plan_3": {"name": "Premium", "credits": 300, "price": 129, "popular": False},
    "plan_4": {"name": "Ultimate", "credits": 700, "price": 249, "popular": False},
}

# Premium Config
PREMIUM_PRICE = 499
PREMIUM_DURATION_DAYS = 30

# Maintenance Mode
MAINTENANCE_MODE = False

# Auto-delete messages after (seconds)
AUTO_DELETE_DELAY = 300  # 5 minutes

# ================= BOT =================
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# Cashfree client
cashfree_client = APIClient(
    app_id=CASHFREE_APP_ID,
    secret_key=CASHFREE_SECRET_KEY,
    environment="sandbox" if "TEST" in CASHFREE_APP_ID else "production"
)
payments_api = PaymentsApi(cashfree_client)

# Try to remove webhook with error handling
try:
    bot.remove_webhook()
    time.sleep(1)
except Exception as e:
    print(f"⚠️  Warning: Could not remove webhook. Network issue? Error: {e}")
    print("🔄 Continuing anyway...")
    time.sleep(1)

# ================= HTTP SESSION (ANTI BLOCK) =================
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/121.0.0.0 Mobile Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://google.com"
})

# ================= DATABASE =================
db = sqlite3.connect("users.db", check_same_thread=False)
lock = threading.Lock()

def cur():
    return db.cursor()

def migrate_db():
    """Migrate database to add new columns if needed"""
    c = cur()
    
    migrations = [
        ("banned", "ALTER TABLE users ADD COLUMN banned INTEGER DEFAULT 0"),
        ("total_downloads", "ALTER TABLE users ADD COLUMN total_downloads INTEGER DEFAULT 0"),
        ("joined_date", "ALTER TABLE users ADD COLUMN joined_date TEXT"),
        ("premium_until", "ALTER TABLE users ADD COLUMN premium_until TEXT"),
        ("warning_count", "ALTER TABLE users ADD COLUMN warning_count INTEGER DEFAULT 0"),
        ("last_claim_streak", "ALTER TABLE users ADD COLUMN last_claim_streak TEXT"),
        ("claim_streak", "ALTER TABLE users ADD COLUMN claim_streak INTEGER DEFAULT 0"),
        ("language", "ALTER TABLE users ADD COLUMN language TEXT DEFAULT 'en'"),
    ]
    
    for column, sql in migrations:
        try:
            c.execute(f"SELECT {column} FROM users LIMIT 1")
        except sqlite3.OperationalError:
            c.execute(sql)
            db.commit()
    
    # Create download_history table
    c.execute("""
        CREATE TABLE IF NOT EXISTS download_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            url TEXT,
            platform TEXT,
            downloaded_at TEXT,
            success INTEGER DEFAULT 1
        )
    """)
    
    # Create support_tickets table
    c.execute("""
        CREATE TABLE IF NOT EXISTS support_tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            subject TEXT,
            message TEXT,
            status TEXT DEFAULT 'open',
            created_at TEXT,
            resolved_at TEXT,
            resolved_by INTEGER
        )
    """)
    
    # Create ticket_messages table for chat conversations
    c.execute("""
        CREATE TABLE IF NOT EXISTS ticket_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id INTEGER,
            sender_id INTEGER,
            sender_type TEXT,
            message TEXT,
            created_at TEXT,
            FOREIGN KEY (ticket_id) REFERENCES support_tickets(id)
        )
    """)
    
    # Create banned_urls table
    c.execute("""
        CREATE TABLE IF NOT EXISTS banned_urls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url_pattern TEXT UNIQUE,
            reason TEXT,
            added_by INTEGER,
            added_at TEXT
        )
    """)
    
    # Create referrals table for better tracking
    c.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER,
            referred_id INTEGER,
            joined_at TEXT,
            credited INTEGER DEFAULT 0
        )
    """)
    
    # Create admins table for multiple admin system
    c.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE,
            added_by INTEGER,
            added_at TEXT,
            is_super_admin INTEGER DEFAULT 0
        )
    """)
    
    # Insert super admin if not exists
    c.execute("SELECT user_id FROM admins WHERE user_id = ?", (SUPER_ADMIN_ID,))
    if not c.fetchone():
        c.execute(
            "INSERT OR IGNORE INTO admins (user_id, added_by, added_at, is_super_admin) VALUES (?, ?, ?, ?)",
            (SUPER_ADMIN_ID, SUPER_ADMIN_ID, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), 1)
        )
        db.commit()

with lock:
    migrate_db()
    c = cur()
    
    # Users table
    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        credits INTEGER DEFAULT 5,
        last_used INTEGER DEFAULT 0,
        last_reset TEXT,
        referred_by INTEGER,
        banned INTEGER DEFAULT 0,
        total_downloads INTEGER DEFAULT 0,
        joined_date TEXT,
        premium_until TEXT,
        warning_count INTEGER DEFAULT 0,
        last_claim_streak TEXT,
        claim_streak INTEGER DEFAULT 0,
        language TEXT DEFAULT 'en'
    )""")
    
    # Stats table
    c.execute("""
    CREATE TABLE IF NOT EXISTS stats (
        id INTEGER PRIMARY KEY,
        downloads INTEGER DEFAULT 0,
        total_credits_purchased INTEGER DEFAULT 0,
        total_referrals INTEGER DEFAULT 0
    )""")
    
    # Payments table
    c.execute("""
    CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        plan_id TEXT,
        amount REAL,
        credits INTEGER,
        status TEXT DEFAULT 'pending',
        utr_number TEXT,
        created_at TEXT,
        verified_at TEXT,
        verified_by INTEGER
    )""")
    
    # Insert default stats if not exists
    c.execute("SELECT id FROM stats WHERE id = 1")
    if not c.fetchone():
        c.execute("INSERT INTO stats VALUES (1, 0, 0, 0)")
    db.commit()

# ================= HELPERS =================
def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def get_user(uid):
    """Get user data, returns None if not found"""
    with lock:
        c = cur()
        c.execute("SELECT * FROM users WHERE user_id=?", (uid,))
        return c.fetchone()

def user_exists(uid):
    """Check if user exists"""
    return get_user(uid) is not None

def add_user(uid, ref=None):
    """Add new user to database"""
    with lock:
        c = cur()
        c.execute(
            """INSERT OR IGNORE INTO users 
               (user_id, credits, last_used, last_reset, referred_by, joined_date) 
               VALUES (?, ?, ?, ?, ?, ?)""",
            (uid, DAILY_FREE_CREDITS, 0, today(), ref, now())
        )
        db.commit()
        
        # Track referral properly
        if ref and ref != uid and user_exists(ref):
            c.execute(
                "INSERT INTO referrals (referrer_id, referred_id, joined_at, credited) VALUES (?, ?, ?, 0)",
                (ref, uid, now())
            )
            db.commit()
            return True
    return False

def process_referral_bonus(uid):
    """Process referral bonus for the referrer"""
    with lock:
        c = cur()
        c.execute(
            "SELECT referrer_id FROM referrals WHERE referred_id = ? AND credited = 0",
            (uid,)
        )
        ref = c.fetchone()
        if ref:
            referrer_id = ref[0]
            c.execute("UPDATE users SET credits = credits + ? WHERE user_id = ?", (REFERRAL_BONUS, referrer_id))
            c.execute("UPDATE referrals SET credited = 1 WHERE referred_id = ?", (uid,))
            c.execute("UPDATE stats SET total_referrals = total_referrals + 1 WHERE id = 1")
            db.commit()
            return referrer_id
    return None

def reset_daily(uid):
    """Reset daily credits with streak system"""
    user = get_user(uid)
    if not user:
        return
    
    last_reset = user[3]
    last_streak = user[10]  # last_claim_streak
    streak = user[11]  # claim_streak
    
    # Check if consecutive day
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    
    with lock:
        c = cur()
        if last_streak == yesterday:
            # Consecutive day - increase streak
            new_streak = streak + 1
            bonus_credits = min(new_streak // 7, 5)  # Extra 1 credit per week, max 5
            total_credits = DAILY_FREE_CREDITS + bonus_credits
        else:
            # Streak broken
            new_streak = 1
            total_credits = DAILY_FREE_CREDITS
        
        c.execute("""
            UPDATE users 
            SET credits=?, last_reset=?, last_claim_streak=?, claim_streak=?
            WHERE user_id=?
        """, (total_credits, today(), today(), new_streak, uid))
        db.commit()
        return new_streak, total_credits

def use_credit(uid):
    """Use one credit"""
    with lock:
        c = cur()
        c.execute("""
            UPDATE users 
            SET credits = credits - 1, 
                last_used = ?,
                total_downloads = total_downloads + 1 
            WHERE user_id = ?
        """, (int(time.time()), uid))
        db.commit()

def add_credit(uid, n):
    """Add credits to user"""
    with lock:
        c = cur()
        c.execute("UPDATE users SET credits = credits + ? WHERE user_id = ?", (n, uid))
        db.commit()

def inc_download():
    """Increment global download counter"""
    with lock:
        c = cur()
        c.execute("UPDATE stats SET downloads = downloads + 1 WHERE id = 1")
        db.commit()

def ban_user_db(uid):
    """Ban user"""
    with lock:
        c = cur()
        c.execute("UPDATE users SET banned = 1 WHERE user_id = ?", (uid,))
        db.commit()

def unban_user_db(uid):
    """Unban user"""
    with lock:
        c = cur()
        c.execute("UPDATE users SET banned = 0, warning_count = 0 WHERE user_id = ?", (uid,))
        db.commit()

def warn_user_db(uid):
    """Add warning to user"""
    with lock:
        c = cur()
        c.execute("UPDATE users SET warning_count = warning_count + 1 WHERE user_id = ?", (uid,))
        c.execute("SELECT warning_count FROM users WHERE user_id = ?", (uid,))
        result = c.fetchone()
        db.commit()
        return result[0] if result else 0

def is_banned(uid):
    """Check if user is banned"""
    user = get_user(uid)
    if not user:
        return False
    return user[5] == 1

def is_premium(uid):
    """Check if user has premium"""
    user = get_user(uid)
    if not user:
        return False
    premium_until = user[8]
    if not premium_until:
        return False
    try:
        expiry = datetime.fromisoformat(premium_until)
        return expiry > datetime.now(timezone.utc)
    except:
        return False

def add_premium(uid, days=PREMIUM_DURATION_DAYS):
    """Add premium status to user"""
    expiry = datetime.now(timezone.utc) + timedelta(days=days)
    with lock:
        c = cur()
        c.execute("UPDATE users SET premium_until = ? WHERE user_id = ?", (expiry.isoformat(), uid))
        db.commit()

def get_stats():
    """Get global stats"""
    with lock:
        c = cur()
        c.execute("SELECT * FROM stats WHERE id = 1")
        return c.fetchone()

def get_all_users_count():
    """Get total users count"""
    with lock:
        c = cur()
        c.execute("SELECT COUNT(*) FROM users")
        return c.fetchone()[0]

def get_active_users_today():
    """Get users active today"""
    with lock:
        c = cur()
        c.execute("SELECT COUNT(*) FROM users WHERE last_reset = ?", (today(),))
        return c.fetchone()[0]

def get_banned_users_count():
    """Get banned users count"""
    with lock:
        c = cur()
        c.execute("SELECT COUNT(*) FROM users WHERE banned = 1")
        return c.fetchone()[0]

def get_premium_users_count():
    """Get premium users count"""
    with lock:
        c = cur()
        current_time = datetime.now(timezone.utc).isoformat()
        c.execute("SELECT COUNT(*) FROM users WHERE premium_until > ?", (current_time,))
        return c.fetchone()[0]

def create_payment(user_id, plan_id, amount, credits):
    """Create a new payment record"""
    with lock:
        c = cur()
        c.execute("""
            INSERT INTO payments (user_id, plan_id, amount, credits, status, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
        """, (user_id, plan_id, amount, credits, now()))
        db.commit()
        return c.lastrowid

def get_payment(payment_id):
    """Get payment by ID"""
    with lock:
        c = cur()
        c.execute("SELECT * FROM payments WHERE id = ?", (payment_id,))
        return c.fetchone()

def verify_payment(payment_id, utr_number, verified_by):
    """Verify a payment and add credits"""
    with lock:
        c = cur()
        c.execute("""
            UPDATE payments 
            SET status = 'verified', utr_number = ?, verified_at = ?, verified_by = ?
            WHERE id = ?
        """, (utr_number, now(), verified_by, payment_id))
        
        c.execute("SELECT user_id, plan_id, amount, credits FROM payments WHERE id = ?", (payment_id,))
        payment = c.fetchone()
        if payment:
            c.execute("UPDATE users SET credits = credits + ? WHERE user_id = ?", (payment[3], payment[0]))
            c.execute("UPDATE stats SET total_credits_purchased = total_credits_purchased + ? WHERE id = 1", (payment[3],))
        
        db.commit()
        return payment

def get_pending_payments():
    """Get all pending payments"""
    with lock:
        c = cur()
        c.execute("SELECT * FROM payments WHERE status = 'pending' ORDER BY created_at DESC")
        return c.fetchall()

def add_download_history(user_id, url, platform, success=True):
    """Add download to history"""
    with lock:
        c = cur()
        c.execute("""
            INSERT INTO download_history (user_id, url, platform, downloaded_at, success)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, url[:500], platform, now(), 1 if success else 0))
        db.commit()

def get_user_download_history(user_id, limit=10):
    """Get user's download history"""
    with lock:
        c = cur()
        c.execute("""
            SELECT url, platform, downloaded_at, success 
            FROM download_history 
            WHERE user_id = ? 
            ORDER BY downloaded_at DESC 
            LIMIT ?
        """, (user_id, limit))
        return c.fetchall()

def get_top_referrers(limit=10):
    """Get top referrers"""
    with lock:
        c = cur()
        c.execute("""
            SELECT referrer_id, COUNT(*) as count 
            FROM referrals 
            WHERE credited = 1 
            GROUP BY referrer_id 
            ORDER BY count DESC 
            LIMIT ?
        """, (limit,))
        return c.fetchall()

def create_support_ticket(user_id, subject, message):
    """Create support ticket"""
    with lock:
        c = cur()
        c.execute("""
            INSERT INTO support_tickets (user_id, subject, message, created_at)
            VALUES (?, ?, ?, ?)
        """, (user_id, subject[:100], message[:1000], now()))
        db.commit()
        return c.lastrowid

def get_open_tickets():
    """Get all open support tickets"""
    with lock:
        c = cur()
        c.execute("SELECT * FROM support_tickets WHERE status = 'open' ORDER BY created_at DESC")
        return c.fetchall()

def resolve_ticket(ticket_id, resolved_by):
    """Resolve support ticket"""
    with lock:
        c = cur()
        c.execute("""
            UPDATE support_tickets 
            SET status = 'resolved', resolved_at = ?, resolved_by = ?
            WHERE id = ?
        """, (now(), resolved_by, ticket_id))
        db.commit()

def get_ticket(ticket_id):
    """Get ticket by ID"""
    with lock:
        c = cur()
        c.execute("SELECT * FROM support_tickets WHERE id = ?", (ticket_id,))
        return c.fetchone()

def get_user_tickets(user_id):
    """Get all tickets for a user"""
    with lock:
        c = cur()
        c.execute("SELECT * FROM support_tickets WHERE user_id = ? ORDER BY created_at DESC", (user_id,))
        return c.fetchall()

def add_ticket_message(ticket_id, sender_id, sender_type, message):
    """Add message to ticket conversation"""
    with lock:
        c = cur()
        c.execute("""
            INSERT INTO ticket_messages (ticket_id, sender_id, sender_type, message, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (ticket_id, sender_id, sender_type, message[:2000], now()))
        db.commit()
        return c.lastrowid

def get_ticket_messages(ticket_id):
    """Get all messages for a ticket"""
    with lock:
        c = cur()
        c.execute("""
            SELECT sender_id, sender_type, message, created_at 
            FROM ticket_messages 
            WHERE ticket_id = ? 
            ORDER BY created_at ASC
        """, (ticket_id,))
        return c.fetchall()

def get_ticket_stats():
    """Get ticket statistics"""
    with lock:
        c = cur()
        c.execute("SELECT COUNT(*) FROM support_tickets WHERE status = 'open'")
        open_count = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM support_tickets WHERE status = 'resolved'")
        resolved_count = c.fetchone()[0]
        return open_count, resolved_count

def add_banned_url(url_pattern, reason, added_by):
    """Add banned URL pattern"""
    with lock:
        c = cur()
        try:
            c.execute("""
                INSERT INTO banned_urls (url_pattern, reason, added_by, added_at)
                VALUES (?, ?, ?, ?)
            """, (url_pattern, reason[:200], added_by, now()))
            db.commit()
            return True
        except sqlite3.IntegrityError:
            return False

def get_banned_urls():
    """Get all banned URL patterns"""
    with lock:
        c = cur()
        c.execute("SELECT * FROM banned_urls ORDER BY added_at DESC")
        return c.fetchall()

def is_url_banned(url):
    """Check if URL is banned"""
    with lock:
        c = cur()
        c.execute("SELECT url_pattern, reason FROM banned_urls")
        patterns = c.fetchall()
        for pattern, reason in patterns:
            if pattern.lower() in url.lower():
                return True, reason
        return False, None

def delete_banned_url(url_id):
    """Delete banned URL pattern"""
    with lock:
        c = cur()
        c.execute("DELETE FROM banned_urls WHERE id = ?", (url_id,))
        db.commit()



# ================= ADMIN CHECK =================
def is_admin(uid):
    """Check if user is an admin (including super admin)"""
    with lock:
        c = cur()
        c.execute("SELECT user_id FROM admins WHERE user_id = ?", (uid,))
        return c.fetchone() is not None

def is_super_admin(uid):
    """Check if user is the super admin"""
    return uid == SUPER_ADMIN_ID

def add_admin(user_id, added_by):
    """Add a new admin"""
    with lock:
        c = cur()
        try:
            c.execute(
                "INSERT INTO admins (user_id, added_by, added_at, is_super_admin) VALUES (?, ?, ?, 0)",
                (user_id, added_by, now())
            )
            db.commit()
            return True
        except sqlite3.IntegrityError:
            return False

def remove_admin(user_id):
    """Remove an admin (cannot remove super admin)"""
    if user_id == SUPER_ADMIN_ID:
        return False
    with lock:
        c = cur()
        c.execute("DELETE FROM admins WHERE user_id = ? AND is_super_admin = 0", (user_id,))
        db.commit()
        return c.rowcount > 0

def get_all_admins():
    """Get list of all admin user IDs"""
    with lock:
        c = cur()
        c.execute("SELECT user_id FROM admins")
        return [row[0] for row in c.fetchall()]

def get_admin_count():
    """Get total number of admins"""
    with lock:
        c = cur()
        c.execute("SELECT COUNT(*) FROM admins")
        return c.fetchone()[0]

def notify_all_admins(text, reply_markup=None, exclude_admin=None):
    """Send notification to all admins"""
    admins = get_all_admins()
    for admin_id in admins:
        if exclude_admin and admin_id == exclude_admin:
            continue
        try:
            bot.send_message(admin_id, text, reply_markup=reply_markup)
        except:
            pass

# Backward compatibility - ADMIN_ID now refers to all admins
ADMIN_ID = SUPER_ADMIN_ID  # For legacy code that uses ADMIN_ID

# ================= DECORATORS =================
def maintenance_check(func):
    """Decorator to check maintenance mode"""
    @wraps(func)
    def wrapper(message, *args, **kwargs):
        if MAINTENANCE_MODE and not is_admin(message.from_user.id):
            bot.reply_to(message, "🔧 <b>Maintenance Mode</b>\n\nBot is under maintenance. Please try again later.")
            return
        return func(message, *args, **kwargs)
    return wrapper

def auto_delete(seconds=AUTO_DELETE_DELAY):
    """Decorator to auto-delete messages"""
    def decorator(func):
        @wraps(func)
        def wrapper(message, *args, **kwargs):
            result = func(message, *args, **kwargs)
            if result:
                def delete_later():
                    time.sleep(seconds)
                    try:
                        bot.delete_message(message.chat.id, message.message_id)
                        if hasattr(result, 'message_id'):
                            bot.delete_message(message.chat.id, result.message_id)
                    except:
                        pass
                threading.Thread(target=delete_later, daemon=True).start()
            return result
        return wrapper
    return decorator

# ================= KEYBOARDS =================
def main_keyboard(uid):
    """Main menu keyboard"""
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("📥 Download", callback_data="download"),
        InlineKeyboardButton("💳 Credits", callback_data="credits")
    )
    kb.row(
        InlineKeyboardButton("👥 Referral", callback_data="referral"),
        InlineKeyboardButton("📊 Stats", callback_data="stats")
    )
    kb.row(
        InlineKeyboardButton("💎 Premium", callback_data="premium_info"),
        InlineKeyboardButton("📜 History", callback_data="download_history")
    )
    kb.row(
        InlineKeyboardButton("💰 Buy Credits", callback_data="buy_credits"),
        InlineKeyboardButton("❓ Help", callback_data="help")
    )
    kb.row(
        InlineKeyboardButton("🎁 Daily Reward", callback_data="claim_daily")
    )
    if is_admin(uid):
        kb.row(
            InlineKeyboardButton("👑 Admin Panel", callback_data="admin_panel")
        )
    return kb

def admin_keyboard(uid=None):
    """Admin panel keyboard"""
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("📊 Stats", callback_data="admin_stats"),
        InlineKeyboardButton("👥 Users", callback_data="admin_users")
    )
    kb.row(
        InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"),
        InlineKeyboardButton("➕ Add Credits", callback_data="admin_add_credits")
    )
    kb.row(
        InlineKeyboardButton("💳 Payments", callback_data="admin_payments"),
        InlineKeyboardButton("🔍 User Dashboard", callback_data="admin_user_dashboard")
    )
    kb.row(
        InlineKeyboardButton("🏆 Top Referrers", callback_data="admin_top_referrers"),
        InlineKeyboardButton("🎫 Tickets", callback_data="admin_tickets")
    )
    kb.row(
        InlineKeyboardButton("🚫 Banned URLs", callback_data="admin_banned_urls"),
        InlineKeyboardButton("⚠️ Warnings", callback_data="admin_warnings")
    )
    kb.row(
        InlineKeyboardButton("🔧 Maintenance", callback_data="admin_maintenance"),
        InlineKeyboardButton("📤 Export", callback_data="admin_export")
    )
    # Show Manage Admins button only for Super Admin
    if uid and is_super_admin(uid):
        kb.row(InlineKeyboardButton("👑 Manage Admins", callback_data="admin_manage"))
    kb.row(
        InlineKeyboardButton("🔙 Back to Main", callback_data="main_menu")
    )
    return kb

def payment_plans_keyboard():
    """Credit plans keyboard"""
    kb = InlineKeyboardMarkup()
    for plan_id, plan in CREDIT_PLANS.items():
        popular = "⭐ " if plan["popular"] else ""
        kb.add(InlineKeyboardButton(
            f"{popular}{plan['name']} - ₹{plan['price']} ({plan['credits']} Credits)",
            callback_data=f"buy_plan_{plan_id}"
        ))
    kb.add(InlineKeyboardButton("💎 Get Premium", callback_data="buy_premium"))
    kb.add(InlineKeyboardButton("🔙 Back", callback_data="main_menu"))
    return kb

def premium_keyboard():
    """Premium info keyboard"""
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("💎 Buy Premium - ₹" + str(PREMIUM_PRICE), callback_data="buy_premium"))
    kb.add(InlineKeyboardButton("🔙 Back", callback_data="main_menu"))
    return kb

# ================= COMMANDS =================
@bot.message_handler(commands=["start"])
@maintenance_check
def start_cmd(message):
    """Start command handler"""
    uid = message.from_user.id
    uname = message.from_user.first_name
    
    # Handle referral
    ref = None
    args = message.text.split()
    if len(args) > 1:
        try:
            ref_id = int(args[1])
            if ref_id != uid:
                ref = ref_id
        except ValueError:
            pass
    
    # Add user if new
    is_new = False
    if not user_exists(uid):
        add_user(uid, ref)
        is_new = True
        if ref and user_exists(ref):
            bot.send_message(ref, f"🎉 <b>New referral!</b>\n@{message.from_user.username or uid} joined using your link!\n✅ +{REFERRAL_BONUS} credits will be added when they claim their first daily reward.")
    
    # Check if premium
    premium_status = "💎 <b>Premium Active!</b>\n" if is_premium(uid) else ""
    
    welcome_text = (
        f"👋 <b>Welcome, {uname}!</b>\n\n"
        f"{premium_status}"
        "📥 <b>Social Media Downloader Bot</b>\n\n"
        "I can download videos from:\n"
        "📸 Instagram | 🐦 X/Twitter | 📘 Facebook\n"
        "📦 TeraBox | ▶️ YouTube | 🔗 And more!\n\n"
        f"🎁 <b>{DAILY_FREE_CREDITS} free credits daily!</b>\n"
        f"👥 <b>+{REFERRAL_BONUS} credits per referral!</b>\n"
        "💎 <b>Premium users get unlimited downloads!</b>\n\n"
        "👇 Choose an option:"
    )
    
    bot.send_message(message.chat.id, welcome_text, reply_markup=main_keyboard(uid))

@bot.message_handler(commands=["help"])
def help_cmd(message):
    """Help command handler"""
    uid = message.from_user.id
    
    user_text = (
        "<b>📖 How to use the bot:</b>\n\n"
        "1️⃣ Send a video link to download\n"
        "2️⃣ Use /mycredits to check balance\n"
        "3️⃣ Use /dailyreward for free credits\n"
        "4️⃣ Use /referral to invite friends\n"
        "5️⃣ Use /history to see download history\n"
        "6️⃣ Use /premium for unlimited downloads\n\n"
        "<b>📝 Available Commands:</b>\n"
        "/start - Start the bot\n"
        "/mycredits - Check your credits\n"
        "/dailyreward - Claim daily reward\n"
        "/referral - Get referral link\n"
        "/referralstats - Your referral stats\n"
        "/leaderboard - Top referrers\n"
        "/buycredits - Buy more credits\n"
        "/premium - Premium info\n"
        "/history - Download history\n"
        "/stats - Bot statistics\n"
        "/support - Contact support\n"
        "/help - Show this help"
    )
    
    if is_admin(uid):
        admin_text = (
            "\n\n<b>👑 Admin Commands:</b>\n"
            "/admin - Admin panel\n"
            "/broadcast - Broadcast message (reply to msg)\n"
            "/addcredits - Add credits (usage: /addcredits USER_ID AMOUNT)\n"
            "/removecredits - Remove credits\n"
            "/warn - Warn a user\n"
            "/ban - Ban a user\n"
            "/unban - Unban a user\n"
            "/userstats - User details\n"
            "/users - List all users\n"
            "/payments - View pending payments\n"
            "/tickets - View support tickets\n"
            "/closeticket - Close a ticket\n"
            "/banurl - Ban URL pattern\n"
            "/maintenance - Toggle maintenance mode\n"
            "/export - Export user data"
        )
        bot.send_message(message.chat.id, user_text + admin_text, parse_mode="HTML")
    else:
        bot.send_message(message.chat.id, user_text)

@bot.message_handler(commands=["mycredits", "credits"])
def mycredits_cmd(message):
    """Check credits command"""
    uid = message.from_user.id
    user = get_user(uid)
    
    if not user:
        bot.reply_to(message, "❌ <b>User not found.</b> Please use /start first.")
        return
    
    # Check if premium
    premium = is_premium(uid)
    premium_text = "💎 <b>PREMIUM ACTIVE</b> - Unlimited Downloads!\n\n" if premium else ""
    
    # Reset daily credits if needed
    if not premium and user[3] != today():
        streak, credits = reset_daily(uid)
        streak_text = f"🔥 Streak: {streak} days!\n" if streak > 1 else ""
    else:
        streak_text = f"🔥 Streak: {user[11]} days\n" if user[11] > 0 else ""
    
    user = get_user(uid)
    
    text = (
        f"{premium_text}"
        f"💳 <b>Your Credits</b>\n\n"
        f"Available: <b>{user[1]} credits</b>\n"
        f"Total Downloads: <b>{user[6]}</b>\n"
        f"{streak_text}\n"
        "💰 <b>Need more credits?</b>\n"
        "Use /buycredits or invite friends with /referral"
    )
    
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("💰 Buy Credits", callback_data="buy_credits"),
        InlineKeyboardButton("👥 Invite Friends", callback_data="referral")
    )
    
    bot.send_message(message.chat.id, text, reply_markup=kb)

@bot.message_handler(commands=["dailyreward", "daily"])
def dailyreward_cmd(message):
    """Daily reward command with streak"""
    uid = message.from_user.id
    user = get_user(uid)
    
    if not user:
        bot.reply_to(message, "❌ <b>User not found.</b> Please use /start first.")
        return
    
    # Process referral bonus on first claim
    referrer = process_referral_bonus(uid)
    
    if user[3] != today():
        streak, credits = reset_daily(uid)
        
        streak_text = ""
        if streak > 1:
            bonus = min(streak // 7, 5)
            if bonus > 0:
                streak_text = f"\n🔥 <b>Streak Bonus:</b> +{bonus} extra credits!"
        
        bot.reply_to(message, 
            f"🎁 <b>Daily Reward Claimed!</b>\n\n"
            f"You received <b>{credits} credits</b>!{streak_text}\n\n"
            f"🔥 Streak: <b>{streak} days</b>\n\n"
            "Come back tomorrow for more! 🌟")
        
        # Notify referrer
        if referrer:
            try:
                bot.send_message(referrer, 
                    f"🎉 <b>Referral Bonus Credited!</b>\n\n"
                    f"Your referral claimed their daily reward!\n"
                    f"✅ +{REFERRAL_BONUS} credits added to your account!")
            except:
                pass
    else:
        next_claim = datetime.now(timezone.utc) + timedelta(days=1)
        next_claim_str = next_claim.strftime("%H:%M UTC")
        bot.reply_to(message, 
            "⏰ <b>Already Claimed!</b>\n\n"
            f"You've already claimed your daily reward.\n"
            f"Next claim available at: <b>{next_claim_str}</b> 📅")

@bot.message_handler(commands=["referral", "invite"])
def referral_cmd(message):
    """Referral command handler"""
    uid = message.from_user.id
    bot_username = bot.get_me().username
    link = f"https://t.me/{bot_username}?start={uid}"
    
    # Get referral count from referrals table
    with lock:
        c = cur()
        c.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = ? AND credited = 1", (uid,))
        ref_count = c.fetchone()[0]
    
    # Get leaderboard position
    with lock:
        c = cur()
        c.execute("""
            SELECT referrer_id FROM (
                SELECT referrer_id, COUNT(*) as count 
                FROM referrals 
                WHERE credited = 1 
                GROUP BY referrer_id 
                ORDER BY count DESC
            )
        """)
        all_refs = c.fetchall()
        position = next((i+1 for i, r in enumerate(all_refs) if r[0] == uid), None)
    
    position_text = f"🏆 Rank: <b>#{position}</b>\n" if position else ""
    
    text = (
        "👥 <b>Invite & Earn!</b>\n\n"
        f"Share your link and earn <b>{REFERRAL_BONUS} credits</b> for each friend!\n\n"
        f"📊 <b>Your Stats:</b>\n"
        f"Referrals: <b>{ref_count}</b>\n"
        f"Credits earned: <b>{ref_count * REFERRAL_BONUS}</b>\n"
        f"{position_text}\n"
        f"🔗 <b>Your Link:</b>\n<code>{link}</code>"
    )
    
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("📤 Share Link", url=f"https://t.me/share/url?url={quote_plus(link)}&text=Download+videos+from+social+media+for+free!"),
        InlineKeyboardButton("🏆 Leaderboard", callback_data="leaderboard")
    )
    
    bot.send_message(message.chat.id, text, reply_markup=kb)

@bot.message_handler(commands=["leaderboard"])
def leaderboard_cmd(message):
    """Show referral leaderboard"""
    top_refs = get_top_referrers(10)
    
    if not top_refs:
        bot.reply_to(message, "📊 No referrals yet! Be the first! 🏆")
        return
    
    text = "🏆 <b>Referral Leaderboard</b>\n\n"
    medals = ["🥇", "🥈", "🥉"]
    
    for i, (user_id, count) in enumerate(top_refs):
        medal = medals[i] if i < 3 else f"{i+1}."
        text += f"{medal} User <code>{user_id}</code> - <b>{count}</b> referrals\n"
    
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("👥 My Referral Stats", callback_data="referral"))
    
    bot.send_message(message.chat.id, text, reply_markup=kb)

@bot.message_handler(commands=["referralstats"])
def referralstats_cmd(message):
    """Referral stats command"""
    uid = message.from_user.id
    
    with lock:
        c = cur()
        c.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = ? AND credited = 1", (uid,))
        ref_count = c.fetchone()[0]
    
    bot.reply_to(message, f"👥 <b>Referral Stats</b>\n\nTotal Referrals: <b>{ref_count}</b>\nCredits Earned: <b>{ref_count * REFERRAL_BONUS}</b>")

@bot.message_handler(commands=["stats"])
def stats_cmd(message):
    """Stats command"""
    stats = get_stats()
    total_users = get_all_users_count()
    active_today = get_active_users_today()
    premium_users = get_premium_users_count()
    banned = get_banned_users_count()
    
    text = (
        "📊 <b>Bot Statistics</b>\n\n"
        f"👥 Total Users: <b>{total_users}</b>\n"
        f"💎 Premium Users: <b>{premium_users}</b>\n"
        f"📥 Total Downloads: <b>{stats[1]}</b>\n"
        f"🎁 Active Today: <b>{active_today}</b>\n"
        f"💰 Credits Purchased: <b>{stats[2]}</b>\n"
        f"👥 Total Referrals: <b>{stats[3]}</b>\n"
        f"🚫 Banned Users: <b>{banned}</b>"
    )
    
    bot.send_message(message.chat.id, text)

@bot.message_handler(commands=["buycredits", "buy"])
def buycredits_cmd(message):
    """Buy credits command"""
    text = (
        "💰 <b>Buy Credits</b>\n\n"
        "Choose a plan below:\n\n"
        "✅ Instant delivery after payment\n"
        "✅ Secure UPI payment\n"
        "✅ 24/7 support\n\n"
        "💎 Or get <b>Premium</b> for unlimited downloads!"
    )
    bot.send_message(message.chat.id, text, reply_markup=payment_plans_keyboard())

@bot.message_handler(commands=["premium"])
def premium_cmd(message):
    """Premium info command"""
    uid = message.from_user.id
    
    if is_premium(uid):
        user = get_user(uid)
        expiry = user[8]
        expiry_date = datetime.fromisoformat(expiry).strftime("%Y-%m-%d %H:%M")
        
        text = (
            "💎 <b>Premium Status</b>\n\n"
            "✅ <b>Premium Active!</b>\n"
            f"📅 Expires: <b>{expiry_date} UTC</b>\n\n"
            "🌟 Benefits:\n"
            "• Unlimited downloads\n"
            "• No cooldown between downloads\n"
            "• Priority support\n"
            "• Premium badge"
        )
    else:
        text = (
            "💎 <b>Premium Membership</b>\n\n"
            f"Price: <b>₹{PREMIUM_PRICE}</b> for {PREMIUM_DURATION_DAYS} days\n\n"
            "🌟 Benefits:\n"
            "• Unlimited downloads\n"
            "• No cooldown between downloads\n"
            "• Priority support\n"
            "• Premium badge\n\n"
            "Click below to purchase!"
        )
    
    bot.send_message(message.chat.id, text, reply_markup=premium_keyboard())

@bot.message_handler(commands=["history"])
def history_cmd(message):
    """Download history command"""
    uid = message.from_user.id
    history = get_user_download_history(uid, 10)
    
    if not history:
        bot.reply_to(message, "📜 No download history yet!\n\nSend me a video link to start downloading!")
        return
    
    text = "📜 <b>Your Download History</b>\n\n"
    
    for url, platform, downloaded_at, success in history:
        status = "✅" if success else "❌"
        text += f"{status} {platform[:20]} - {downloaded_at[:10]}\n"
    
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🗑️ Clear History", callback_data="clear_history"))
    
    bot.send_message(message.chat.id, text, reply_markup=kb)

@bot.message_handler(commands=["support"])
def support_cmd(message):
    """Support ticket command - shows user's tickets or creates new"""
    uid = message.from_user.id
    
    # Check if user has existing open tickets
    user_tickets = get_user_tickets(uid)
    open_tickets = [t for t in user_tickets if t[4] == 'open']
    
    if open_tickets:
        # Show existing tickets
        text = "🎫 <b>Your Support Tickets</b>\n\n"
        kb = InlineKeyboardMarkup()
        
        for ticket in open_tickets[:5]:
            text += f"🆔 #{ticket[0]} - {ticket[3][:30]}... ({ticket[4]})\n"
            kb.add(InlineKeyboardButton(f"💬 View Ticket #{ticket[0]}", callback_data=f"view_ticket_{ticket[0]}"))
        
        kb.add(InlineKeyboardButton("➕ Create New Ticket", callback_data="create_new_ticket"))
        bot.send_message(message.chat.id, text, reply_markup=kb)
    else:
        # Create new ticket
        msg = bot.send_message(message.chat.id, 
            "🎫 <b>Create Support Ticket</b>\n\n"
            "Please describe your issue in one message.\n"
            "Include any relevant details.\n\n"
            "Type /cancel to cancel.")
        bot.register_next_step_handler(msg, process_support_ticket, uid)

@bot.message_handler(commands=["mytickets"])
def mytickets_cmd(message):
    """View user's support tickets"""
    uid = message.from_user.id
    
    user_tickets = get_user_tickets(uid)
    
    if not user_tickets:
        bot.reply_to(message, "🎫 You don't have any support tickets yet.\n\nUse /support to create one.")
        return
    
    text = "🎫 <b>Your Support Tickets</b>\n\n"
    kb = InlineKeyboardMarkup()
    
    for ticket in user_tickets[:10]:
        status_emoji = "🟢" if ticket[4] == 'open' else "✅"
        text += f"{status_emoji} #{ticket[0]} - {ticket[3][:25]}... ({ticket[4]})\n"
        kb.add(InlineKeyboardButton(f"💬 View Ticket #{ticket[0]}", callback_data=f"view_ticket_{ticket[0]}"))
    
    kb.add(InlineKeyboardButton("➕ Create New Ticket", callback_data="create_new_ticket"))
    bot.send_message(message.chat.id, text, reply_markup=kb)

def process_support_ticket(message, uid):
    """Process support ticket creation"""
    if message.text == "/cancel":
        bot.reply_to(message, "❌ Ticket creation cancelled.")
        return
    
    subject = "General Support"
    ticket_text = message.text
    
    ticket_id = create_support_ticket(uid, subject, ticket_text)
    
    # Add first message to conversation
    add_ticket_message(ticket_id, uid, 'user', ticket_text)
    
    # Notify admin
    try:
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton(f"💬 Reply to Ticket #{ticket_id}", callback_data=f"admin_reply_ticket_{ticket_id}"))
        kb.add(InlineKeyboardButton(f"✅ Close Ticket #{ticket_id}", callback_data=f"admin_close_ticket_{ticket_id}"))
        
        bot.send_message(ADMIN_ID, 
            f"🎫 <b>New Support Ticket!</b>\n\n"
            f"🆔 Ticket ID: <code>{ticket_id}</code>\n"
            f"👤 User: <code>{uid}</code>\n"
            f"📝 Message: {ticket_text[:300]}\n\n"
            f"Use /ticket {ticket_id} to view full conversation", 
            reply_markup=kb)
    except:
        pass
    
    bot.reply_to(message, 
        f"✅ <b>Ticket Created!</b>\n\n"
        f"🆔 Ticket ID: <code>{ticket_id}</code>\n"
        f"Use /mytickets to view your tickets and chat with support.\n\n"
        "Our team will respond shortly. Thank you for your patience! 🙏")

@bot.message_handler(commands=["ticket"])
def view_ticket_cmd(message):
    """View specific ticket conversation"""
    uid = message.from_user.id
    args = message.text.split()
    
    if len(args) != 2:
        bot.reply_to(message, "❌ <b>Usage:</b> /ticket <ticket_id>")
        return
    
    try:
        ticket_id = int(args[1])
    except ValueError:
        bot.reply_to(message, "❌ <b>Invalid ticket ID!</b>")
        return
    
    ticket = get_ticket(ticket_id)
    
    if not ticket:
        bot.reply_to(message, "❌ <b>Ticket not found!</b>")
        return
    
    # Check if user owns this ticket or is admin
    if ticket[1] != uid and not is_admin(uid):
        bot.reply_to(message, "❌ <b>Unauthorized!</b>")
        return
    
    # Get conversation
    messages = get_ticket_messages(ticket_id)
    
    # Build conversation text
    text = f"🎫 <b>Ticket #{ticket_id}</b>\n"
    text += f"Status: {'🟢 Open' if ticket[4] == 'open' else '✅ Resolved'}\n"
    text += f"Created: {ticket[5][:16]}\n"
    text += "━" * 20 + "\n\n"
    
    for msg in messages[:20]:  # Show last 20 messages
        sender = "👤 You" if msg[1] == uid and msg[2] == 'user' else ("👑 Admin" if msg[2] == 'admin' else f"👤 User {msg[1]}")
        text += f"{sender} ({msg[3][:16]}):\n{msg[2]}\n\n"
    
    kb = InlineKeyboardMarkup()
    
    if ticket[4] == 'open':
        if is_admin(uid):
            kb.row(
                InlineKeyboardButton("💬 Reply", callback_data=f"admin_reply_ticket_{ticket_id}"),
                InlineKeyboardButton("✅ Close", callback_data=f"admin_close_ticket_{ticket_id}")
            )
        else:
            kb.add(InlineKeyboardButton("💬 Reply", callback_data=f"reply_ticket_{ticket_id}"))
    
    bot.send_message(message.chat.id, text[:4000], reply_markup=kb)  # Limit to 4000 chars

@bot.message_handler(commands=["reply"])
def reply_ticket_cmd(message):
    """Reply to a ticket"""
    uid = message.from_user.id
    args = message.text.split(maxsplit=1)
    
    if len(args) != 2:
        bot.reply_to(message, "❌ <b>Usage:</b> /reply <ticket_id>\nThen send your message.")
        return
    
    try:
        ticket_id = int(args[1])
    except ValueError:
        bot.reply_to(message, "❌ <b>Invalid ticket ID!</b>")
        return
    
    ticket = get_ticket(ticket_id)
    
    if not ticket:
        bot.reply_to(message, "❌ <b>Ticket not found!</b>")
        return
    
    if ticket[4] != 'open':
        bot.reply_to(message, "❌ <b>This ticket is already closed!</b>")
        return
    
    # Check authorization
    if ticket[1] != uid and not is_admin(uid):
        bot.reply_to(message, "❌ <b>Unauthorized!</b>")
        return
    
    msg = bot.send_message(message.chat.id, "💬 <b>Send your reply message:</b>\n\nType /cancel to cancel.")
    bot.register_next_step_handler(msg, process_ticket_reply, ticket_id, uid, is_admin(uid))

def process_ticket_reply(message, ticket_id, uid, is_admin_user):
    """Process ticket reply"""
    if message.text == "/cancel":
        bot.reply_to(message, "❌ Reply cancelled.")
        return
    
    reply_text = message.text
    sender_type = 'admin' if is_admin_user else 'user'
    
    # Add message to conversation
    add_ticket_message(ticket_id, uid, sender_type, reply_text)
    
    # Get ticket info
    ticket = get_ticket(ticket_id)
    
    if is_admin_user:
        # Admin replied - notify user
        try:
            bot.send_message(ticket[1], 
                f"🎫 <b>New Reply on Ticket #{ticket_id}</b>\n\n"
                f"👑 <b>Admin:</b>\n{reply_text[:500]}\n\n"
                f"Use /ticket {ticket_id} to view full conversation or /reply {ticket_id} to respond.")
        except:
            pass
        
        bot.reply_to(message, f"✅ Reply sent to ticket #{ticket_id}")
    else:
        # User replied - notify admin
        try:
            kb = InlineKeyboardMarkup()
            kb.row(
                InlineKeyboardButton(f"💬 Reply", callback_data=f"admin_reply_ticket_{ticket_id}"),
                InlineKeyboardButton(f"✅ Close", callback_data=f"admin_close_ticket_{ticket_id}")
            )
            
            bot.send_message(ADMIN_ID, 
                f"🎫 <b>New Reply on Ticket #{ticket_id}</b>\n\n"
                f"👤 User {uid}:\n{reply_text[:500]}\n\n"
                f"Use /ticket {ticket_id} to view full conversation.",
                reply_markup=kb)
        except:
            pass
        
        bot.reply_to(message, f"✅ Reply sent! We'll get back to you soon. 🙏")

@bot.message_handler(commands=["closeticket"])
def close_ticket_cmd(message):
    """Close a ticket (admin only)"""
    uid = message.from_user.id
    
    if not is_admin(uid):
        bot.reply_to(message, "❌ Unauthorized!")
        return
    
    args = message.text.split()
    if len(args) != 2:
        bot.reply_to(message, "❌ Usage: /closeticket TICKET_ID")
        return
    
    try:
        ticket_id = int(args[1])
    except ValueError:
        bot.reply_to(message, "❌ Invalid ticket ID!")
        return
    
    ticket = get_ticket(ticket_id)
    
    if not ticket:
        bot.reply_to(message, "❌ <b>Ticket not found!</b>")
        return
    
    if ticket[4] == 'resolved':
        bot.reply_to(message, "❌ <b>This ticket is already closed!</b>")
        return
    
    # Close ticket
    resolve_ticket(ticket_id, uid)
    
    # Notify user
    try:
        bot.send_message(ticket[1], 
            f"✅ <b>Ticket #{ticket_id} Closed!</b>\n\n"
            "Your support ticket has been resolved.\n"
            "Thank you for your patience! 🙏\n\n"
            "If you have more questions, feel free to create a new ticket with /support")
    except:
        pass
    
    bot.reply_to(message, f"✅ Ticket #{ticket_id} has been closed.")

@bot.message_handler(commands=["admin"])
def admin_cmd(message):
    """Admin panel command"""
    uid = message.from_user.id
    if not is_admin(uid):
        bot.reply_to(message, "❌ <b>Unauthorized!</b>")
        return
    
    text = (
        "👑 <b>Admin Control Panel</b>\n\n"
        f"🔧 Maintenance Mode: {'✅ ON' if MAINTENANCE_MODE else '❌ OFF'}\n\n"
        "Select an option below to manage the bot."
    )
    bot.send_message(message.chat.id, text, reply_markup=admin_keyboard(uid))

@bot.message_handler(commands=["broadcast"])
def broadcast_cmd(message):
    """Broadcast command"""
    uid = message.from_user.id
    if not is_admin(uid):
        bot.reply_to(message, "❌ <b>Unauthorized!</b>")
        return
    
    if not message.reply_to_message:
        bot.reply_to(message, "❌ <b>Reply to a message</b> to broadcast it.")
        return
    
    # Get all users
    with lock:
        c = cur()
        c.execute("SELECT user_id FROM users WHERE banned = 0")
        users = c.fetchall()
    
    sent = 0
    failed = 0
    
    status_msg = bot.reply_to(message, f"📢 Broadcasting to {len(users)} users...")
    
    for user in users:
        try:
            bot.forward_message(user[0], message.chat.id, message.reply_to_message.message_id)
            sent += 1
            time.sleep(0.05)
        except:
            failed += 1
            continue
        
        if (sent + failed) % 50 == 0:
            try:
                bot.edit_message_text(
                    f"📢 Broadcasting...\n✅ Sent: {sent}\n❌ Failed: {failed}",
                    status_msg.chat.id, status_msg.message_id)
            except:
                pass
    
    bot.edit_message_text(
        f"📢 <b>Broadcast Complete!</b>\n\n✅ Sent: <b>{sent}</b>\n❌ Failed: <b>{failed}</b>",
        status_msg.chat.id, status_msg.message_id)

@bot.message_handler(commands=["addcredits"])
def addcredits_cmd(message):
    """Add credits command"""
    uid = message.from_user.id
    if not is_admin(uid):
        bot.reply_to(message, "❌ Unauthorized!")
        return
    
    args = message.text.split()
    if len(args) != 3:
        bot.reply_to(message, "❌ Usage: /addcredits USER_ID AMOUNT")
        return
    
    try:
        target_uid = int(args[1])
        amount = int(args[2])
    except ValueError:
        bot.reply_to(message, "❌ Invalid numbers!")
        return
    
    if not user_exists(target_uid):
        bot.reply_to(message, f"❌ User <b>{target_uid}</b> not found!")
        return
    
    add_credit(target_uid, amount)
    bot.reply_to(message, f"✅ Added <b>{amount} credits</b> to user <b>{target_uid}</b>")
    
    try:
        bot.send_message(target_uid, f"🎉 <b>Surprise!</b>\n\nAdmin added <b>{amount} credits</b> to your account!")
    except:
        pass

@bot.message_handler(commands=["removecredits"])
def removecredits_cmd(message):
    """Remove credits command"""
    uid = message.from_user.id
    if not is_admin(uid):
        bot.reply_to(message, "❌ Unauthorized!")
        return
    
    args = message.text.split()
    if len(args) != 3:
        bot.reply_to(message, "❌ Usage: /removecredits USER_ID AMOUNT")
        return
    
    try:
        target_uid = int(args[1])
        amount = int(args[2])
    except ValueError:
        bot.reply_to(message, "❌ Invalid numbers!")
        return
    
    if not user_exists(target_uid):
        bot.reply_to(message, f"❌ User <b>{target_uid}</b> not found!")
        return
    
    with lock:
        c = cur()
        c.execute("UPDATE users SET credits = MAX(0, credits - ?) WHERE user_id = ?", (amount, target_uid))
        db.commit()
    
    bot.reply_to(message, f"✅ Removed <b>{amount} credits</b> from user <b>{target_uid}</b>")

@bot.message_handler(commands=["warn"])
def warn_cmd(message):
    """Warn user command"""
    uid = message.from_user.id
    if not is_admin(uid):
        bot.reply_to(message, "❌ Unauthorized!")
        return
    
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "❌ Usage: /warn USER_ID [reason]")
        return
    
    try:
        target_uid = int(args[1])
    except ValueError:
        bot.reply_to(message, "❌ Invalid user ID!")
        return
    
    if not user_exists(target_uid):
        bot.reply_to(message, f"❌ User <b>{target_uid}</b> not found!")
        return
    
    reason = " ".join(args[2:]) if len(args) > 2 else "Violation of terms"
    warning_count = warn_user_db(target_uid)
    
    # Auto-ban after 3 warnings
    if warning_count >= 3:
        ban_user_db(target_uid)
        bot.reply_to(message, f"🚫 User <b>{target_uid}</b> has been <b>banned</b> after {warning_count} warnings!")
        try:
            bot.send_message(target_uid, 
                f"🚫 <b>Your account has been banned!</b>\n\n"
                f"Reason: {warning_count} warnings\n"
                "Contact admin for support.")
        except:
            pass
    else:
        bot.reply_to(message, f"⚠️ User <b>{target_uid}</b> has been warned! ({warning_count}/3)")
        try:
            bot.send_message(target_uid, 
                f"⚠️ <b>Warning!</b>\n\n"
                f"Reason: {reason}\n"
                f"Warning {warning_count}/3\n\n"
                "3 warnings will result in a ban!")
        except:
            pass

@bot.message_handler(commands=["ban"])
def ban_cmd(message):
    """Ban user command"""
    uid = message.from_user.id
    if not is_admin(uid):
        bot.reply_to(message, "❌ Unauthorized!")
        return
    
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "❌ Usage: /ban USER_ID [reason]")
        return
    
    try:
        target_uid = int(args[1])
    except ValueError:
        bot.reply_to(message, "❌ Invalid user ID!")
        return
    
    if not user_exists(target_uid):
        bot.reply_to(message, f"❌ User <b>{target_uid}</b> not found!")
        return
    
    reason = " ".join(args[2:]) if len(args) > 2 else "Banned by admin"
    
    ban_user_db(target_uid)
    bot.reply_to(message, f"🚫 User <b>{target_uid}</b> has been <b>banned</b>!")
    
    try:
        bot.send_message(target_uid, 
            f"🚫 <b>Your account has been banned!</b>\n\n"
            f"Reason: {reason}\n\n"
            "Contact admin for support.")
    except:
        pass

@bot.message_handler(commands=["unban"])
def unban_cmd(message):
    """Unban user command"""
    uid = message.from_user.id
    if not is_admin(uid):
        bot.reply_to(message, "❌ Unauthorized!")
        return
    
    args = message.text.split()
    if len(args) != 2:
        bot.reply_to(message, "❌ Usage: /unban USER_ID")
        return
    
    try:
        target_uid = int(args[1])
    except ValueError:
        bot.reply_to(message, "❌ Invalid user ID!")
        return
    
    if not user_exists(target_uid):
        bot.reply_to(message, f"❌ User <b>{target_uid}</b> not found!")
        return
    
    unban_user_db(target_uid)
    bot.reply_to(message, f"✅ User <b>{target_uid}</b> has been <b>unbanned</b>!")
    
    try:
        bot.send_message(target_uid, "✅ <b>Your account has been unbanned!</b>\n\nYou can now use the bot again.")
    except:
        pass

@bot.message_handler(commands=["userstats"])
def userstats_cmd(message):
    """User stats command"""
    uid = message.from_user.id
    if not is_admin(uid):
        bot.reply_to(message, "❌ Unauthorized!")
        return
    
    args = message.text.split()
    if len(args) != 2:
        bot.reply_to(message, "❌ Usage: /userstats USER_ID")
        return
    
    try:
        target_uid = int(args[1])
    except ValueError:
        bot.reply_to(message, "❌ Invalid user ID!")
        return
    
    user = get_user(target_uid)
    if not user:
        bot.reply_to(message, f"❌ User <b>{target_uid}</b> not found!")
        return
    
    # Get referral count
    with lock:
        c = cur()
        c.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = ? AND credited = 1", (target_uid,))
        ref_count = c.fetchone()[0]
    
    premium_status = "✅ Premium" if is_premium(target_uid) else "❌ No"
    
    last_used = "Never"
    if user[2]:
        last_used = datetime.fromtimestamp(user[2]).strftime('%Y-%m-%d %H:%M:%S')
    
    text = (
        f"👤 <b>User Stats: {target_uid}</b>\n\n"
        f"💳 Credits: <b>{user[1]}</b>\n"
        f"📥 Downloads: <b>{user[6]}</b>\n"
        f"👥 Referrals: <b>{ref_count}</b>\n"
        f"💎 Premium: <b>{premium_status}</b>\n"
        f"⚠️ Warnings: <b>{user[9]}/3</b>\n"
        f"🚫 Banned: <b>{'Yes' if user[5] else 'No'}</b>\n"
        f"📅 Joined: <b>{user[7] or 'Unknown'}</b>\n"
        f"🕐 Last Used: <b>{last_used}</b>\n"
        f"🔥 Streak: <b>{user[11]} days</b>"
    )
    
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("➕ Add Credits", callback_data=f"admin_addcredit_{target_uid}"),
        InlineKeyboardButton("⚠️ Warn", callback_data=f"admin_warn_{target_uid}")
    )
    kb.row(
        InlineKeyboardButton("💎 Add Premium", callback_data=f"admin_premium_{target_uid}"),
        InlineKeyboardButton("🚫 Ban" if not user[5] else "✅ Unban", callback_data=f"admin_ban_{target_uid}")
    )
    
    bot.send_message(message.chat.id, text, reply_markup=kb)

@bot.message_handler(commands=["users"])
def users_cmd(message):
    """List users command"""
    uid = message.from_user.id
    if not is_admin(uid):
        bot.reply_to(message, "❌ <b>Unauthorized!</b>")
        return
    
    total = get_all_users_count()
    banned = get_banned_users_count()
    premium = get_premium_users_count()
    active = get_active_users_today()
    
    text = (
        f"👥 <b>User Statistics</b>\n\n"
        f"📊 Total Users: <b>{total}</b>\n"
        f"💎 Premium: <b>{premium}</b>\n"
        f"✅ Active Today: <b>{active}</b>\n"
        f"🚫 Banned: <b>{banned}</b>"
    )
    
    bot.send_message(message.chat.id, text)

@bot.message_handler(commands=["payments"])
def payments_cmd(message):
    """View pending payments command"""
    uid = message.from_user.id
    if not is_admin(uid):
        bot.reply_to(message, "❌ <b>Unauthorized!</b>")
        return
    
    payments = get_pending_payments()
    
    if not payments:
        bot.reply_to(message, "✅ <b>No pending payments!</b>")
        return
    
    text = f"💳 <b>Pending Payments ({len(payments)})</b>\n\n"
    
    for p in payments[:5]:
        text += f"🆔 #{p[0]} | 👤 {p[1]} | ₹{p[3]} | {p[4]} credits\n"
    
    if len(payments) > 5:
        text += f"\n...and {len(payments) - 5} more"
    
    kb = InlineKeyboardMarkup()
    for p in payments[:5]:
        kb.add(InlineKeyboardButton(
            f"Verify Payment #{p[0]} - ₹{p[3]}",
            callback_data=f"admin_verify_{p[0]}"
        ))
    
    bot.send_message(message.chat.id, text, reply_markup=kb)

@bot.message_handler(commands=["tickets"])
def tickets_cmd(message):
    """View support tickets command"""
    uid = message.from_user.id
    if not is_admin(uid):
        bot.reply_to(message, "❌ <b>Unauthorized!</b>")
        return
    
    tickets = get_open_tickets()
    
    if not tickets:
        bot.reply_to(message, "✅ <b>No open tickets!</b>")
        return
    
    text = f"🎫 <b>Open Support Tickets ({len(tickets)})</b>\n\n"
    
    for t in tickets[:5]:
        text += f"🆔 #{t[0]} | 👤 {t[1]} | {t[3][:30]}...\n"
    
    kb = InlineKeyboardMarkup()
    for t in tickets[:5]:
        kb.add(InlineKeyboardButton(
            f"View Ticket #{t[0]}",
            callback_data=f"admin_ticket_{t[0]}"
        ))
    
    bot.send_message(message.chat.id, text, reply_markup=kb)

@bot.message_handler(commands=["banurl"])
def banurl_cmd(message):
    """Ban URL pattern command"""
    uid = message.from_user.id
    if not is_admin(uid):
        bot.reply_to(message, "❌ Unauthorized!")
        return
    
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        bot.reply_to(message, "❌ Usage: /banurl URL_PATTERN [reason]")
        return
    
    parts = args[1].split(maxsplit=1)
    pattern = parts[0]
    reason = parts[1] if len(parts) > 1 else "Banned by admin"
    
    if add_banned_url(pattern, reason, uid):
        bot.reply_to(message, f"✅ URL pattern <code>{pattern}</code> has been banned!")
    else:
        bot.reply_to(message, f"⚠️ URL pattern <code>{pattern}</code> is already banned!")

@bot.message_handler(commands=["maintenance"])
def maintenance_cmd(message):
    """Toggle maintenance mode"""
    global MAINTENANCE_MODE
    uid = message.from_user.id
    if not is_admin(uid):
        bot.reply_to(message, "❌ <b>Unauthorized!</b>")
        return
    
    MAINTENANCE_MODE = not MAINTENANCE_MODE
    status = "✅ ON" if MAINTENANCE_MODE else "❌ OFF"
    bot.reply_to(message, f"🔧 Maintenance Mode: <b>{status}</b>")

@bot.message_handler(commands=["export"])
def export_cmd(message):
    """Export user data"""
    uid = message.from_user.id
    if not is_admin(uid):
        bot.reply_to(message, "❌ <b>Unauthorized!</b>")
        return
    
    with lock:
        c = cur()
        c.execute("SELECT user_id, credits, total_downloads, premium_until, warning_count, joined_date FROM users ORDER BY user_id")
        users = c.fetchall()
    
    # Create CSV content
    csv_content = "user_id,credits,total_downloads,premium_until,warning_count,joined_date\n"
    for u in users:
        csv_content += f"{u[0]},{u[1]},{u[2]},{u[3] or ''},{u[4]},{u[5] or ''}\n"
    
    # Send as file
    file_io = io.BytesIO(csv_content.encode())
    file_io.name = "users_export.csv"
    bot.send_document(message.chat.id, file_io, caption=f"📊 User Export\n\nTotal users: {len(users)}")

@bot.message_handler(commands=["addadmin"])
def addadmin_cmd(message):
    """Add admin command - Super Admin only"""
    uid = message.from_user.id
    if not is_super_admin(uid):
        bot.reply_to(message, "❌ <b>Only Super Admin can add admins!</b>")
        return
    
    args = message.text.split()
    if len(args) != 2:
        bot.reply_to(message, "❌ Usage: /addadmin USER_ID")
        return
    
    try:
        target_uid = int(args[1])
    except ValueError:
        bot.reply_to(message, "❌ Invalid user ID!")
        return
    
    if not user_exists(target_uid):
        bot.reply_to(message, f"❌ User <b>{target_uid}</b> not found!")
        return
    
    if is_admin(target_uid):
        bot.reply_to(message, f"⚠️ User <b>{target_uid}</b> is already an admin!")
        return
    
    if add_admin(target_uid, uid):
        bot.reply_to(message, f"✅ User <b>{target_uid}</b> has been added as admin!")
        try:
            bot.send_message(target_uid, "🎉 <b>You have been promoted to Admin!</b>\n\nUse /admin to access the admin panel.")
        except:
            pass
    else:
        bot.reply_to(message, "❌ Failed to add admin!")

@bot.message_handler(commands=["removeadmin"])
def removeadmin_cmd(message):
    """Remove admin command - Super Admin only"""
    uid = message.from_user.id
    if not is_super_admin(uid):
        bot.reply_to(message, "❌ <b>Only Super Admin can remove admins!</b>")
        return
    
    args = message.text.split()
    if len(args) != 2:
        bot.reply_to(message, "❌ Usage: /removeadmin USER_ID")
        return
    
    try:
        target_uid = int(args[1])
    except ValueError:
        bot.reply_to(message, "❌ Invalid user ID!")
        return
    
    if target_uid == SUPER_ADMIN_ID:
        bot.reply_to(message, "❌ Cannot remove the Super Admin!")
        return
    
    if not is_admin(target_uid):
        bot.reply_to(message, f"⚠️ User <b>{target_uid}</b> is not an admin!")
        return
    
    if remove_admin(target_uid):
        bot.reply_to(message, f"✅ User <b>{target_uid}</b> has been removed from admin!")
        try:
            bot.send_message(target_uid, "⚠️ <b>Your admin privileges have been revoked.</b>")
        except:
            pass
    else:
        bot.reply_to(message, "❌ Failed to remove admin!")

@bot.message_handler(commands=["admins"])
def admins_cmd(message):
    """List all admins - Super Admin only"""
    uid = message.from_user.id
    if not is_super_admin(uid):
        bot.reply_to(message, "❌ <b>Only Super Admin can view admin list!</b>")
        return
    
    admins = get_all_admins()
    
    if not admins:
        bot.reply_to(message, "📋 No admins found!")
        return
    
    text = "👑 <b>Admin List</b>\n\n"
    for admin_id in admins:
        super_admin_mark = " (Super Admin)" if admin_id == SUPER_ADMIN_ID else ""
        text += f"• <code>{admin_id}</code>{super_admin_mark}\n"
    
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("➕ Add Admin", callback_data="admin_manage_add"))
    kb.add(InlineKeyboardButton("➖ Remove Admin", callback_data="admin_manage_remove"))
    
    bot.send_message(message.chat.id, text, reply_markup=kb)

@bot.message_handler(commands=["addadmin"])
def addadmin_cmd(message):
    """Add admin command - Super Admin only"""
    uid = message.from_user.id
    if not is_super_admin(uid):
        bot.reply_to(message, "❌ <b>Only Super Admin can add admins!</b>")
        return
    
    args = message.text.split()
    if len(args) != 2:
        bot.reply_to(message, "❌ Usage: /addadmin USER_ID")
        return
    
    try:
        target_uid = int(args[1])
    except ValueError:
        bot.reply_to(message, "❌ Invalid user ID!")
        return
    
    if not user_exists(target_uid):
        bot.reply_to(message, f"❌ User <b>{target_uid}</b> not found!")
        return
    
    if is_admin(target_uid):
        bot.reply_to(message, f"⚠️ User <b>{target_uid}</b> is already an admin!")
        return
    
    if add_admin(target_uid, uid):
        bot.reply_to(message, f"✅ User <b>{target_uid}</b> has been added as admin!")
        try:
            bot.send_message(target_uid, "🎉 <b>You have been promoted to Admin!</b>\n\nUse /admin to access the admin panel.")
        except:
            pass
    else:
        bot.reply_to(message, "❌ Failed to add admin!")

@bot.message_handler(commands=["removeadmin"])
def removeadmin_cmd(message):
    """Remove admin command - Super Admin only"""
    uid = message.from_user.id
    if not is_super_admin(uid):
        bot.reply_to(message, "❌ <b>Only Super Admin can remove admins!</b>")
        return
    
    args = message.text.split()
    if len(args) != 2:
        bot.reply_to(message, "❌ Usage: /removeadmin USER_ID")
        return
    
    try:
        target_uid = int(args[1])
    except ValueError:
        bot.reply_to(message, "❌ Invalid user ID!")
        return
    
    if target_uid == SUPER_ADMIN_ID:
        bot.reply_to(message, "❌ Cannot remove the Super Admin!")
        return
    
    if not is_admin(target_uid):
        bot.reply_to(message, f"⚠️ User <b>{target_uid}</b> is not an admin!")
        return
    
    if remove_admin(target_uid):
        bot.reply_to(message, f"✅ User <b>{target_uid}</b> has been removed from admin!")
        try:
            bot.send_message(target_uid, "⚠️ <b>Your admin privileges have been revoked.</b>")
        except:
            pass
    else:
        bot.reply_to(message, "❌ Failed to remove admin!")

@bot.message_handler(commands=["admins"])
def admins_cmd(message):
    """List all admins - Super Admin only"""
    uid = message.from_user.id
    if not is_super_admin(uid):
        bot.reply_to(message, "❌ <b>Only Super Admin can view admin list!</b>")
        return
    
    admins = get_all_admins()
    
    if not admins:
        bot.reply_to(message, "📋 No admins found!")
        return
    
    text = "👑 <b>Admin List</b>\n\n"
    for admin_id in admins:
        super_admin_mark = " (Super Admin)" if admin_id == SUPER_ADMIN_ID else ""
        text += f"• <code>{admin_id}</code>{super_admin_mark}\n"
    
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("➕ Add Admin", callback_data="admin_manage_add"))
    kb.add(InlineKeyboardButton("➖ Remove Admin", callback_data="admin_manage_remove"))
    
    bot.send_message(message.chat.id, text, reply_markup=kb)


# ================= CALLBACK HANDLERS =================
@bot.callback_query_handler(func=lambda c: True)
def callback_handler(call):
    """Handle all callbacks"""
    uid = call.from_user.id
    data = call.data
    
    try:
        bot.answer_callback_query(call.id)
        
        if not user_exists(uid) and not data.startswith("admin_"):
            bot.send_message(call.message.chat.id, "❌ Please use /start first!")
            return
        
        user = get_user(uid)
        if user and user[3] != today() and not is_premium(uid):
            reset_daily(uid)
            user = get_user(uid)
        
        # Main menu callbacks
        if data == "main_menu":
            bot.edit_message_text(
                "👋 <b>Welcome back!</b>\n\nChoose an option:",
                call.message.chat.id, call.message.message_id,
                reply_markup=main_keyboard(uid))
        
        elif data == "download":
            bot.send_message(call.message.chat.id, 
                "🔗 <b>Send me the video link to download!</b>\n\n"
                "Supported: Instagram, X, Facebook, YouTube, TeraBox")
        
        elif data == "credits":
            if user:
                premium = is_premium(uid)
                premium_text = "💎 PREMIUM - Unlimited!\n\n" if premium else ""
                text = f"{premium_text}💳 <b>Your Credits</b>\n\nAvailable: <b>{user[1]} credits</b>\nTotal Downloads: <b>{user[6]}</b>"
                kb = InlineKeyboardMarkup()
                kb.row(
                    InlineKeyboardButton("💰 Buy More", callback_data="buy_credits"),
                    InlineKeyboardButton("🎁 Daily Reward", callback_data="claim_daily")
                )
                kb.add(InlineKeyboardButton("🔙 Back", callback_data="main_menu"))
                bot.send_message(call.message.chat.id, text, reply_markup=kb)
        
        elif data == "referral":
            bot_username = bot.get_me().username
            link = f"https://t.me/{bot_username}?start={uid}"
            
            with lock:
                c = cur()
                c.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = ? AND credited = 1", (uid,))
                ref_count = c.fetchone()[0]
            
            text = (
                "👥 <b>Invite & Earn!</b>\n\n"
                f"Share your link and earn <b>{REFERRAL_BONUS} credits</b> for each friend!\n\n"
                f"📊 Referrals: <b>{ref_count}</b>\n"
                f"💰 Credits earned: <b>{ref_count * REFERRAL_BONUS}</b>\n\n"
                f"🔗 <code>{link}</code>"
            )
            
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("📤 Share Link", url=f"https://t.me/share/url?url={quote_plus(link)}"))
            kb.add(InlineKeyboardButton("🔙 Back", callback_data="main_menu"))
            
            bot.send_message(call.message.chat.id, text, reply_markup=kb)
        
        elif data == "leaderboard":
            top_refs = get_top_referrers(10)
            
            if not top_refs:
                bot.send_message(call.message.chat.id, "📊 No referrals yet! Be the first! 🏆")
                return
            
            text = "🏆 <b>Referral Leaderboard</b>\n\n"
            medals = ["🥇", "🥈", "🥉"]
            
            for i, (user_id, count) in enumerate(top_refs):
                medal = medals[i] if i < 3 else f"{i+1}."
                text += f"{medal} User <code>{user_id}</code> - <b>{count}</b> referrals\n"
            
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("👥 My Referral Stats", callback_data="referral"))
            
            bot.send_message(call.message.chat.id, text, reply_markup=kb)
        
        elif data == "stats":
            stats = get_stats()
            total_users = get_all_users_count()
            active_today = get_active_users_today()
            premium_users = get_premium_users_count()
            
            text = (
                "📊 <b>Bot Statistics</b>\n\n"
                f"👥 Total Users: <b>{total_users}</b>\n"
                f"💎 Premium: <b>{premium_users}</b>\n"
                f"📥 Total Downloads: <b>{stats[1]}</b>\n"
                f"🎁 Active Today: <b>{active_today}</b>\n"
                f"💰 Credits Purchased: <b>{stats[2]}</b>\n"
                f"👥 Total Referrals: <b>{stats[3]}</b>"
            )
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("🔄 Refresh", callback_data="stats"))
            kb.add(InlineKeyboardButton("🔙 Back", callback_data="main_menu"))
            bot.send_message(call.message.chat.id, text, reply_markup=kb)
        
        elif data == "help":
            text = (
                "<b>📖 How to use:</b>\n\n"
                "1️⃣ Send a video link to download\n"
                "2️⃣ Credits are deducted per download\n"
                "3️⃣ Get free daily credits\n"
                "4️⃣ Invite friends for bonus credits\n\n"
                "<b>Commands:</b> /help"
            )
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("🔙 Back", callback_data="main_menu"))
            bot.send_message(call.message.chat.id, text, reply_markup=kb)
        
        elif data == "claim_daily":
            if user:
                if user[3] != today():
                    streak, credits = reset_daily(uid)
                    bot.send_message(call.message.chat.id, 
                        f"🎁 <b>Daily Reward Claimed!</b>\n\n"
                        f"You received <b>{credits} credits</b>!\n"
                        f"🔥 Streak: <b>{streak} days</b>")
                else:
                    bot.send_message(call.message.chat.id, "⏰ <b>Already claimed!</b> Come back tomorrow!")
        
        elif data == "buy_credits":
            text = (
                "💰 <b>Buy Credits</b>\n\n"
                "Select a plan:\n\n"
                "✅ Instant delivery\n"
                "✅ Secure UPI payment\n"
                f"✅ UPI ID: <code>{UPI_ID}</code>"
            )
            bot.send_message(call.message.chat.id, text, reply_markup=payment_plans_keyboard())
        
        elif data.startswith("buy_plan_"):
            plan_id = data.replace("buy_plan_", "")
            plan = CREDIT_PLANS.get(plan_id)
            
            if plan:
                payment_id = create_payment(uid, plan_id, plan["price"], plan["credits"])
                
                try:
                    link_request = CreatePaymentLinkRequest(
                        link_id=str(payment_id),
                        link_amount=plan["price"],
                        link_currency="INR",
                        link_purpose=f"{plan['credits']} Credits Purchase",
                        customer_details=CustomerDetails(customer_id=str(uid)),
                        link_meta={"payment_id": payment_id},
                        link_notify={"send_sms": False, "send_email": False},
                        return_url=RETURN_URL
                    )
                    
                    response = payments_api.create_payment_link(x_api_version="2022-09-01", create_payment_link_request=link_request)
                    
                    text = (
                        f"💰 <b>{plan['name']} Plan</b>\n\n"
                        f"📊 Credits: <b>{plan['credits']}</b>\n"
                        f"💵 Price: <b>₹{plan['price']}</b>\n\n"
                        f"Click the button below to complete the payment."
                    )
                    
                    kb = InlineKeyboardMarkup()
                    kb.add(InlineKeyboardButton("Click here to Pay", url=response.link_url))
                    kb.add(InlineKeyboardButton("🔙 Back", callback_data="buy_credits"))
                    
                    bot.send_message(call.message.chat.id, text, reply_markup=kb)
                
                except Exception as e:
                    print(f"Cashfree error: {e}")
                    bot.send_message(call.message.chat.id, "❌ Could not create payment link. Please try again later.")

        elif data == "buy_premium":
            payment_id = create_payment(uid, "premium", PREMIUM_PRICE, 0)
            
            try:
                link_request = CreatePaymentLinkRequest(
                    link_id=str(payment_id),
                    link_amount=PREMIUM_PRICE,
                    link_currency="INR",
                    link_purpose="Premium Membership",
                    customer_details=CustomerDetails(customer_id=str(uid)),
                    link_meta={"payment_id": payment_id},
                    link_notify={"send_sms": False, "send_email": False},
                    return_url=RETURN_URL
                )
                
                response = payments_api.create_payment_link(x_api_version="2022-09-01", create_payment_link_request=link_request)
                
                text = (
                    f"💎 <b>Premium Membership</b>\n\n"
                    f"📅 Duration: <b>{PREMIUM_DURATION_DAYS} days</b>\n"
                    f"💵 Price: <b>₹{PREMIUM_PRICE}</b>\n\n"
                    f"Click the button below to complete the payment."
                )
                
                kb = InlineKeyboardMarkup()
                kb.add(InlineKeyboardButton("Click here to Pay", url=response.link_url))
                kb.add(InlineKeyboardButton("🔙 Back", callback_data="main_menu"))
                
                bot.send_message(call.message.chat.id, text, reply_markup=kb)
            
            except Exception as e:
                print(f"Cashfree error: {e}")
                bot.send_message(call.message.chat.id, "❌ Could not create payment link. Please try again later.")
            
            bot.send_photo(call.message.chat.id, qr, caption=text, reply_markup=kb)
        
        elif data == "premium_info":
            if is_premium(uid):
                user = get_user(uid)
                expiry = user[8]
                expiry_date = datetime.fromisoformat(expiry).strftime("%Y-%m-%d %H:%M")
                
                text = (
                    "💎 <b>Premium Status</b>\n\n"
                    "✅ <b>Premium Active!</b>\n"
                    f"📅 Expires: <b>{expiry_date} UTC</b>\n\n"
                    "🌟 Benefits:\n"
                    "• Unlimited downloads\n"
                    "• No cooldown between downloads\n"
                    "• Priority support\n"
                    "• Premium badge"
                )
                bot.send_message(call.message.chat.id, text, reply_markup=main_keyboard(uid))
            else:
                text = (
                    "💎 <b>Premium Membership</b>\n\n"
                    f"Price: <b>₹{PREMIUM_PRICE}</b> for {PREMIUM_DURATION_DAYS} days\n\n"
                    "🌟 Benefits:\n"
                    "• Unlimited downloads\n"
                    "• No cooldown between downloads\n"
                    "• Priority support\n"
                    "• Premium badge\n\n"
                    "Click below to purchase!"
                )
                bot.send_message(call.message.chat.id, text, reply_markup=premium_keyboard())
        
        elif data == "download_history":
            history = get_user_download_history(uid, 10)
            
            if not history:
                bot.send_message(call.message.chat.id, 
                    "📜 No download history yet!\n\nSend me a video link to start downloading!")
                return
            
            text = "📜 <b>Your Download History</b>\n\n"
            
            for url, platform, downloaded_at, success in history:
                status = "✅" if success else "❌"
                text += f"{status} {platform[:20]} - {downloaded_at[:10]}\n"
            
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("🔙 Back", callback_data="main_menu"))
            
            bot.send_message(call.message.chat.id, text, reply_markup=kb)
        
        elif data == "clear_history":
            with lock:
                c = cur()
                c.execute("DELETE FROM download_history WHERE user_id = ?", (uid,))
                db.commit()
            bot.send_message(call.message.chat.id, "🗑️ <b>Download history cleared!</b>")
        
        elif data.startswith("paid_"):
            if data.startswith("paid_premium_"):
                payment_id = int(data.replace("paid_premium_", ""))
                msg = bot.send_message(
                    call.message.chat.id,
                    f"🆔 Premium Payment #{payment_id}\n\n"
                    "Please enter your <b>UTR/UPI Reference number</b> (12 digits):\n\n"
                    "Type /cancel to cancel.")
                bot.register_next_step_handler(msg, process_premium_utr, payment_id)
            else:
                payment_id = int(data.replace("paid_", ""))
                payment = get_payment(payment_id)
                
                if payment and payment[5] == 'pending':
                    msg = bot.send_message(
                        call.message.chat.id,
                        f"🆔 Payment #{payment_id}\n\n"
                        "Please enter your <b>UTR/UPI Reference number</b> (12 digits):\n\n"
                        "Type /cancel to cancel.")
                    bot.register_next_step_handler(msg, process_utr, payment_id)
                else:
                    bot.send_message(call.message.chat.id, "❌ Payment not found or already processed!")
        
        # Admin callbacks
        elif data == "admin_manage":
            if not is_super_admin(uid):
                return
            admins_cmd(message=type('obj', (object,), {'from_user': type('obj', (object,), {'id': uid}), 'chat': call.message.chat, 'text': '/admins'}))
        
        elif data == "admin_panel":
            if not is_admin(uid):
                return
            bot.edit_message_text(
                "👑 <b>Admin Control Panel</b>",
                call.message.chat.id, call.message.message_id,
                reply_markup=admin_keyboard())
        
        elif data == "admin_stats":
            if not is_admin(uid):
                return
            stats = get_stats()
            total_users = get_all_users_count()
            active_today = get_active_users_today()
            banned = get_banned_users_count()
            premium = get_premium_users_count()
            
            text = (
                "📊 <b>Admin Stats</b>\n\n"
                f"👥 Total Users: <b>{total_users}</b>\n"
                f"💎 Premium: <b>{premium}</b>\n"
                f"🎁 Active Today: <b>{active_today}</b>\n"
                f"🚫 Banned Users: <b>{banned}</b>\n"
                f"📥 Total Downloads: <b>{stats[1]}</b>\n"
                f"💰 Credits Purchased: <b>{stats[2]}</b>\n"
                f"👥 Total Referrals: <b>{stats[3]}</b>"
            )
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("🔄 Refresh", callback_data="admin_stats"))
            kb.add(InlineKeyboardButton("🔙 Back", callback_data="admin_panel"))
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb)
        
        elif data == "admin_users":
            if not is_admin(uid):
                return
            kb = InlineKeyboardMarkup()
            kb.row(
                InlineKeyboardButton("🔍 Search User", callback_data="admin_search_user"),
                InlineKeyboardButton("📜 List Users", callback_data="admin_list_users_1")
            )
            kb.add(InlineKeyboardButton("🔙 Back", callback_data="admin_panel"))
            bot.edit_message_text("👥 <b>User Management</b>", call.message.chat.id, call.message.message_id, reply_markup=kb)
        
        elif data == "admin_search_user":
            if not is_admin(uid):
                return
            msg = bot.send_message(call.message.chat.id, "🔍 <b>Enter User ID to search:</b>")
            bot.register_next_step_handler(msg, admin_search_user_handler)
        
        elif data.startswith("admin_list_users_"):
            if not is_admin(uid):
                return
            page = int(data.split("_")[-1])
            
            with lock:
                c = cur()
                c.execute("SELECT user_id, credits, banned, premium_until FROM users ORDER BY user_id DESC LIMIT 10 OFFSET ?", ((page - 1) * 10,))
                users = c.fetchall()
                c.execute("SELECT COUNT(*) FROM users")
                total = c.fetchone()[0]
            
            text = f"📜 <b>Users (Page {page})</b>\n\n"
            for u in users:
                status = "🚫" if u[2] else ("💎" if u[3] else "✅")
                text += f"{status} <code>{u[0]}</code> | Credits: {u[1]}\n"
            
            kb = InlineKeyboardMarkup()
            nav_buttons = []
            if page > 1:
                nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"admin_list_users_{page-1}"))
            if page * 10 < total:
                nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"admin_list_users_{page+1}"))
            if nav_buttons:
                kb.row(*nav_buttons)
            kb.add(InlineKeyboardButton("🔙 Back", callback_data="admin_users"))
            
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb)
        
        elif data == "admin_payments":
            if not is_admin(uid):
                return
            payments = get_pending_payments()
            
            if not payments:
                kb = InlineKeyboardMarkup()
                kb.add(InlineKeyboardButton("🔙 Back", callback_data="admin_panel"))
                bot.edit_message_text("✅ <b>No pending payments!</b>", call.message.chat.id, call.message.message_id, reply_markup=kb)
                return
            
            text = f"💳 <b>Pending Payments ({len(payments)})</b>\n\n"
            kb = InlineKeyboardMarkup()
            for p in payments[:10]:
                text += f"🆔 #{p[0]} | 👤 {p[1]} | ₹{p[3]} | {p[4]}cr\n"
                kb.add(InlineKeyboardButton(f"✅ Verify #{p[0]}", callback_data=f"admin_verify_{p[0]}"))
            kb.add(InlineKeyboardButton("🔙 Back", callback_data="admin_panel"))
            
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb)
        
        elif data.startswith("admin_verify_"):
            if not is_admin(uid):
                return
            payment_id = int(data.split("_")[-1])
            msg = bot.send_message(call.message.chat.id, 
                f"🆔 Payment #{payment_id}\n\nEnter the <b>UTR number</b> to verify:")
            bot.register_next_step_handler(msg, admin_verify_payment, payment_id)
        
        elif data == "admin_user_dashboard":
            if not is_admin(uid):
                return
            msg = bot.send_message(call.message.chat.id, 
                "🔍 <b>Enter User ID to view dashboard:</b>")
            bot.register_next_step_handler(msg, admin_view_user_dashboard)
        
        elif data == "admin_top_referrers":
            if not is_admin(uid):
                return
            top_refs = get_top_referrers(20)
            
            if not top_refs:
                text = "📊 No referrals yet!"
            else:
                text = "🏆 <b>Top 20 Referrers</b>\n\n"
                for i, (user_id, count) in enumerate(top_refs):
                    text += f"{i+1}. User <code>{user_id}</code> - <b>{count}</b> referrals\n"
            
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("🔙 Back", callback_data="admin_panel"))
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb)
        
        elif data == "admin_tickets":
            if not is_admin(uid):
                return
            tickets = get_open_tickets()
            
            if not tickets:
                kb = InlineKeyboardMarkup()
                kb.add(InlineKeyboardButton("🔙 Back", callback_data="admin_panel"))
                bot.edit_message_text("✅ <b>No open tickets!</b>", call.message.chat.id, call.message.message_id, reply_markup=kb)
                return
            
            text = f"🎫 <b>Open Tickets ({len(tickets)})</b>\n\n"
            kb = InlineKeyboardMarkup()
            for t in tickets[:10]:
                text += f"🆔 #{t[0]} | 👤 {t[1]} | {t[3][:30]}...\n"
                kb.add(InlineKeyboardButton(f"✅ Resolve #{t[0]}", callback_data=f"admin_resolve_ticket_{t[0]}"))
            kb.add(InlineKeyboardButton("🔙 Back", callback_data="admin_panel"))
            
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb)
        
        elif data.startswith("admin_ticket_"):
            if not is_admin(uid):
                return
            ticket_id = int(data.split("_")[-1])
            
            # Get ticket info
            ticket = get_ticket(ticket_id)
            if not ticket:
                bot.answer_callback_query(call.id, "Ticket not found!")
                return
            
            # Get conversation
            messages = get_ticket_messages(ticket_id)
            
            # Build conversation text
            text = f"🎫 <b>Ticket #{ticket_id}</b>\n"
            text += f"👤 User: <code>{ticket[1]}</code>\n"
            text += f"Status: {'🟢 Open' if ticket[4] == 'open' else '✅ Resolved'}\n"
            text += f"Created: {ticket[5][:16]}\n"
            text += "━" * 20 + "\n\n"
            
            # Show original message
            text += f"📋 <b>Original Message:</b>\n{ticket[3][:200]}\n\n"
            
            # Show conversation
            if messages:
                text += "💬 <b>Conversation:</b>\n\n"
                for msg in messages[:15]:  # Show last 15 messages
                    sender = "👑 Admin" if msg[2] == 'admin' else f"👤 User {msg[1]}"
                    text += f"{sender} ({msg[3][11:16]}):\n{msg[2][:200]}\n\n"
            
            kb = InlineKeyboardMarkup()
            if ticket[4] == 'open':
                kb.row(
                    InlineKeyboardButton("💬 Reply", callback_data=f"admin_reply_ticket_{ticket_id}"),
                    InlineKeyboardButton("✅ Close Ticket", callback_data=f"admin_close_ticket_{ticket_id}")
                )
            kb.add(InlineKeyboardButton("🔙 Back to Tickets", callback_data="admin_tickets"))
            
            bot.send_message(call.message.chat.id, text[:4000], reply_markup=kb)
        
        elif data.startswith("admin_reply_ticket_"):
            if not is_admin(uid):
                return
            ticket_id = int(data.split("_")[-1])
            msg = bot.send_message(call.message.chat.id, 
                f"🎫 <b>Reply to Ticket #{ticket_id}</b>\n\n"
                "Enter your reply message:")
            bot.register_next_step_handler(msg, admin_reply_to_ticket, ticket_id)
        
        elif data.startswith("admin_close_ticket_"):
            if not is_admin(uid):
                return
            ticket_id = int(data.split("_")[-1])
            
            ticket = get_ticket(ticket_id)
            if not ticket:
                bot.answer_callback_query(call.id, "Ticket not found!")
                return
            
            if ticket[4] == 'resolved':
                bot.answer_callback_query(call.id, "Ticket already closed!")
                return
            
            # Close ticket
            resolve_ticket(ticket_id, uid)
            bot.answer_callback_query(call.id, "Ticket closed!")
            
            # Notify user
            try:
                bot.send_message(ticket[1], 
                    f"✅ <b>Ticket #{ticket_id} Closed!</b>\n\n"
                    "Your support ticket has been resolved.\n"
                    "Thank you for your patience! 🙏\n\n"
                    "If you have more questions, feel free to create a new ticket with /support")
            except:
                pass
            
            # Refresh tickets list
            bot.edit_message_text(
                "👑 <b>Admin Control Panel</b>",
                call.message.chat.id, call.message.message_id,
                reply_markup=admin_keyboard())
        
        elif data.startswith("admin_resolve_ticket_"):
            if not is_admin(uid):
                return
            ticket_id = int(data.split("_")[-1])
            resolve_ticket(ticket_id, uid)
            bot.answer_callback_query(call.id, "Ticket resolved!")
            
            # Notify user
            with lock:
                c = cur()
                c.execute("SELECT user_id FROM support_tickets WHERE id = ?", (ticket_id,))
                ticket_user = c.fetchone()
            if ticket_user:
                try:
                    bot.send_message(ticket_user[0], 
                        f"✅ <b>Ticket #{ticket_id} Resolved!</b>\n\n"
                        "Your support ticket has been resolved.\n"
                        "Thank you for your patience!")
                except:
                    pass
            
            # Refresh
            callback_handler(call)
        
        elif data == "admin_banned_urls":
            if not is_admin(uid):
                return
            banned_urls = get_banned_urls()
            
            if not banned_urls:
                text = "✅ No banned URLs"
            else:
                text = "🚫 <b>Banned URL Patterns</b>\n\n"
                for u in banned_urls[:10]:
                    text += f"🆔 {u[0]}: <code>{u[1]}</code>\n"
            
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("➕ Add Pattern", callback_data="admin_add_banned_url"))
            kb.add(InlineKeyboardButton("🔙 Back", callback_data="admin_panel"))
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb)
        
        elif data == "admin_add_banned_url":
            if not is_admin(uid):
                return
            msg = bot.send_message(call.message.chat.id, 
                "🚫 <b>Ban URL Pattern</b>\n\n"
                "Enter the URL pattern to ban (e.g., 'spam-site.com'):\n"
                "Type /cancel to cancel.")
            bot.register_next_step_handler(msg, admin_add_banned_url_handler)
        
        elif data.startswith("admin_addcredit_"):
            if not is_admin(uid):
                return
            target_uid = int(data.split("_")[-1])
            msg = bot.send_message(call.message.chat.id, 
                f"➕ <b>Add credits to {target_uid}</b>\n\nEnter amount:")
            bot.register_next_step_handler(msg, admin_addcredit_handler, target_uid)
        
        elif data.startswith("admin_warn_"):
            if not is_admin(uid):
                return
            target_uid = int(data.split("_")[-1])
            msg = bot.send_message(call.message.chat.id, 
                f"⚠️ <b>Warn user {target_uid}</b>\n\nEnter reason:")
            bot.register_next_step_handler(msg, admin_warn_handler, target_uid)
        
        elif data.startswith("admin_premium_"):
            if not is_admin(uid):
                return
            target_uid = int(data.split("_")[-1])
            msg = bot.send_message(call.message.chat.id, 
                f"💎 <b>Add premium to {target_uid}</b>\n\nEnter days:")
            bot.register_next_step_handler(msg, admin_premium_handler, target_uid)
        
        elif data.startswith("admin_ban_"):
            if not is_admin(uid):
                return
            target_uid = int(data.split("_")[-1])
            user = get_user(target_uid)
            if user:
                if user[5]:
                    unban_user_db(target_uid)
                    bot.answer_callback_query(call.id, "User unbanned!")
                else:
                    ban_user_db(target_uid)
                    bot.answer_callback_query(call.id, "User banned!")
                
                # Refresh
                userstats_cmd_type = type('obj', (object,), {
                    'chat': call.message.chat, 
                    'from_user': type('obj', (object,), {'id': uid}), 
                    'text': f"/userstats {target_uid}"
                })
                userstats_cmd(userstats_cmd_type)
        
        elif data == "admin_maintenance":
            if not is_admin(uid):
                return
            global MAINTENANCE_MODE
            MAINTENANCE_MODE = not MAINTENANCE_MODE
            status = "✅ ON" if MAINTENANCE_MODE else "❌ OFF"
            bot.answer_callback_query(call.id, f"Maintenance: {status}")
            callback_handler(call)
        
        elif data == "admin_export":
            if not is_admin(uid):
                return
            export_cmd(type('obj', (object,), {
                'chat': call.message.chat,
                'from_user': type('obj', (object,), {'id': uid})
            }))
    
    except Exception as e:
        print(f"Callback error: {e}")
        send_error_to_admin(e, uid, f"Callback: {data}")

# ================= MESSAGE HANDLERS =================
def process_utr(message, payment_id):
    """Process UTR number from user"""
    if message.text == "/cancel":
        bot.reply_to(message, "❌ Payment cancelled.")
        return
    
    utr = message.text.strip()
    if not utr.isdigit() or len(utr) < 8:
        msg = bot.reply_to(message, "❌ <b>Invalid UTR!</b> Please enter a valid 12-digit UTR number:")
        bot.register_next_step_handler(msg, process_utr, payment_id)
        return
    
    payment = get_payment(payment_id)
    if not payment:
        bot.reply_to(message, "❌ Payment not found!")
        return
    
    with lock:
        c = cur()
        c.execute("UPDATE payments SET utr_number = ? WHERE id = ?", (utr, payment_id))
        db.commit()
    
    plan = CREDIT_PLANS.get(payment[2], {})
    text = (
        f"💳 <b>New Payment!</b>\n\n"
        f"🆔 Payment ID: <code>{payment_id}</code>\n"
        f"👤 User: <code>{payment[1]}</code>\n"
        f"📦 Plan: {plan.get('name', 'Unknown')}\n"
        f"💵 Amount: ₹{payment[3]}\n"
        f"📊 Credits: {payment[4]}\n"
        f"🧾 UTR: <code>{utr}</code>\n\n"
        f"Verify with: /payments"
    )
    
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("✅ Verify Payment", callback_data=f"admin_verify_{payment_id}"))
    
    try:
        bot.send_message(ADMIN_ID, text, reply_markup=kb)
    except:
        pass
    
    bot.reply_to(message, 
        "✅ <b>Payment submitted!</b>\n\n"
        "Your credits will be added after verification.\n"
        "This usually takes a few minutes.")

def process_premium_utr(message, payment_id):
    """Process premium payment UTR"""
    if message.text == "/cancel":
        bot.reply_to(message, "❌ Payment cancelled.")
        return
    
    utr = message.text.strip()
    if not utr.isdigit() or len(utr) < 8:
        msg = bot.reply_to(message, "❌ <b>Invalid UTR!</b> Please enter a valid 12-digit UTR number:")
        bot.register_next_step_handler(msg, process_premium_utr, payment_id)
        return
    
    with lock:
        c = cur()
        c.execute("UPDATE payments SET utr_number = ? WHERE id = ?", (utr, payment_id))
        db.commit()
    
    text = (
        f"💎 <b>New Premium Payment!</b>\n\n"
        f"🆔 Payment ID: <code>{payment_id}</code>\n"
        f"💵 Amount: ₹{PREMIUM_PRICE}\n"
        f"🧾 UTR: <code>{utr}</code>\n\n"
        f"Verify with: /payments"
    )
    
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("✅ Verify Premium", callback_data=f"admin_verify_{payment_id}"))
    
    try:
        bot.send_message(ADMIN_ID, text, reply_markup=kb)
    except:
        pass
    
    bot.reply_to(message, 
        "✅ <b>Premium payment submitted!</b>\n\n"
        "Your premium will be activated after verification.\n"
        "This usually takes a few minutes.")

def admin_search_user_handler(message):
    """Admin search user handler"""
    try:
        target_uid = int(message.text.strip())
        userstats_cmd(type('obj', (object,), {
            'chat': message.chat, 
            'from_user': message.from_user, 
            'text': f"/userstats {target_uid}"
        }))
    except ValueError:
        bot.reply_to(message, "❌ <b>Invalid User ID!</b>")

def admin_verify_payment(message, payment_id):
    """Admin verify payment handler"""
    if not is_admin(message.from_user.id):
        return
    
    utr = message.text.strip()
    payment = verify_payment(payment_id, utr, message.from_user.id)
    
    if payment:
        # Check if it's premium (plan_id is 'premium')
        if payment[1] == 'premium':
            add_premium(payment[0])
            bot.reply_to(message, f"✅ <b>Premium Payment #{payment_id} verified!</b>\n\nPremium activated for user {payment[0]}")
            try:
                bot.send_message(payment[0], 
                    "🎉 <b>Premium Activated!</b>\n\n"
                    f"Your premium membership is now active for {PREMIUM_DURATION_DAYS} days!\n"
                    "Enjoy unlimited downloads! 💎")
            except:
                pass
        else:
            bot.reply_to(message, f"✅ <b>Payment #{payment_id} verified!</b>\n\n{payment[3]} credits added to user {payment[0]}")
            try:
                bot.send_message(payment[0], 
                    f"🎉 <b>Payment Verified!</b>\n\n"
                    f"Your payment has been verified.\n"
                    f"✅ <b>{payment[3]} credits</b> added to your account!")
            except:
                pass
    else:
        bot.reply_to(message, "❌ Payment not found!")

def admin_view_user_dashboard(message):
    """Admin view user dashboard"""
    if not is_admin(message.from_user.id):
        return
    
    try:
        target_uid = int(message.text.strip())
        show_user_dashboard_to_admin(message.chat.id, target_uid)
    except ValueError:
        bot.reply_to(message, "❌ <b>Invalid User ID!</b>")

def show_user_dashboard_to_admin(chat_id, target_uid):
    """Show user dashboard to admin"""
    user = get_user(target_uid)
    if not user:
        bot.send_message(chat_id, f"❌ User <b>{target_uid}</b> not found!")
        return
    
    try:
        chat_info = bot.get_chat(target_uid)
        username = chat_info.username or "N/A"
        first_name = chat_info.first_name or "N/A"
    except:
        username = "N/A"
        first_name = "Unknown"
    
    with lock:
        c = cur()
        c.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = ? AND credited = 1", (target_uid,))
        ref_count = c.fetchone()[0]
    
    premium_status = "✅ Premium" if is_premium(target_uid) else "❌ No"
    
    last_used = "Never"
    if user[2]:
        last_used = datetime.fromtimestamp(user[2]).strftime('%Y-%m-%d %H:%M:%S')
    
    text = (
        f"👤 <b>User Dashboard: {target_uid}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📛 Name: <b>{first_name}</b>\n"
        f"🔤 Username: <b>@{username}</b>\n"
        f"🆔 ID: <code>{target_uid}</code>\n\n"
        f"💳 <b>Credit Info:</b>\n"
        f"  • Balance: <b>{user[1]} credits</b>\n"
        f"  • Downloads: <b>{user[6]}</b>\n"
        f"  • Referrals: <b>{ref_count}</b>\n"
        f"  • Premium: <b>{premium_status}</b>\n\n"
        f"📊 <b>Account Status:</b>\n"
        f"  • Banned: <b>{'🚫 YES' if user[5] else '✅ No'}</b>\n"
        f"  • Warnings: <b>{user[9]}/3</b>\n"
        f"  • Joined: <b>{user[7] or 'Unknown'}</b>\n"
        f"  • Last Active: <b>{last_used}</b>\n"
        f"  • Streak: <b>{user[11]} days</b>"
    )
    
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("➕ Add Credits", callback_data=f"admin_addcredit_{target_uid}"),
        InlineKeyboardButton("⚠️ Warn", callback_data=f"admin_warn_{target_uid}")
    )
    kb.row(
        InlineKeyboardButton("💎 Premium", callback_data=f"admin_premium_{target_uid}"),
        InlineKeyboardButton("🚫 Ban" if not user[5] else "✅ Unban", callback_data=f"admin_ban_{target_uid}")
    )
    kb.row(InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_panel"))
    
    bot.send_message(chat_id, text, reply_markup=kb)

def admin_addcredit_handler(message, target_uid):
    """Admin add credit handler"""
    if not is_admin(message.from_user.id):
        return
    
    try:
        amount = int(message.text.strip())
        add_credit(target_uid, amount)
        bot.reply_to(message, f"✅ Added <b>{amount} credits</b> to user <b>{target_uid}</b>")
        try:
            bot.send_message(target_uid, f"🎉 Admin added <b>{amount} credits</b> to your account!")
        except:
            pass
    except ValueError:
        bot.reply_to(message, "❌ <b>Invalid amount!</b>")

def admin_warn_handler(message, target_uid):
    """Admin warn handler"""
    if not is_admin(message.from_user.id):
        return
    
    reason = message.text.strip()
    warning_count = warn_user_db(target_uid)
    
    if warning_count >= 3:
        ban_user_db(target_uid)
        bot.reply_to(message, f"🚫 User <b>{target_uid}</b> banned after {warning_count} warnings!")
        try:
            bot.send_message(target_uid, f"🚫 <b>Banned!</b>\nReason: {warning_count} warnings")
        except:
            pass
    else:
        bot.reply_to(message, f"⚠️ User <b>{target_uid}</b> warned! ({warning_count}/3)")
        try:
            bot.send_message(target_uid, f"⚠️ <b>Warning {warning_count}/3</b>\nReason: {reason}")
        except:
            pass

def admin_premium_handler(message, target_uid):
    """Admin add premium handler"""
    if not is_admin(message.from_user.id):
        return
    
    try:
        days = int(message.text.strip())
        add_premium(target_uid, days)
        bot.reply_to(message, f"💎 Added <b>{days} days</b> premium to user <b>{target_uid}</b>")
        try:
            bot.send_message(target_uid, 
                f"🎉 <b>Premium Activated!</b>\n\n"
                f"You received <b>{days} days</b> of premium membership!\n"
                "Enjoy unlimited downloads! 💎")
        except:
            pass
    except ValueError:
        bot.reply_to(message, "❌ <b>Invalid days!</b>")

def admin_add_banned_url_handler(message):
    """Admin add banned URL handler"""
    if not is_admin(message.from_user.id):
        return
    
    if message.text == "/cancel":
        bot.reply_to(message, "❌ Cancelled.")
        return
    
    pattern = message.text.strip()
    if add_banned_url(pattern, "Banned by admin", message.from_user.id):
        bot.reply_to(message, f"✅ URL pattern <code>{pattern}</code> banned!")
    else:
        bot.reply_to(message, f"⚠️ Pattern <code>{pattern}</code> already exists!")

def admin_reply_to_ticket(message, ticket_id):
    """Admin reply to ticket handler"""
    if not is_admin(message.from_user.id):
        return
    
    if message.text == "/cancel":
        bot.reply_to(message, "❌ Reply cancelled.")
        return
    
    reply_text = message.text.strip()
    
    # Add message to conversation
    add_ticket_message(ticket_id, message.from_user.id, 'admin', reply_text)
    
    # Get ticket info to notify user
    ticket = get_ticket(ticket_id)
    if ticket:
        try:
            bot.send_message(ticket[1], 
                f"🎫 <b>New Reply on Ticket #{ticket_id}</b>\n\n"
                f"👑 <b>Admin:</b>\n{reply_text[:500]}\n\n"
                f"Use /ticket {ticket_id} to view full conversation or /reply {ticket_id} to respond.")
        except:
            pass
    
    bot.reply_to(message, f"✅ Reply sent to ticket #{ticket_id}")

# ================= DOWNLOADER =================
@bot.message_handler(func=lambda m: m.text and m.text.startswith("http"))
@maintenance_check
def downloader(message):
    """Video downloader handler - supports ALL URLs"""
    uid = message.from_user.id
    url = message.text.strip()
    
    if not user_exists(uid):
        bot.reply_to(message, "❌ <b>Please use /start first!</b>")
        return
    
    if is_banned(uid):
        bot.reply_to(message, "🚫 <b>Your account is banned!</b>\n\nContact admin for support.")
        return
    
    # Check if URL is banned
    is_banned_url, ban_reason = is_url_banned(url)
    if is_banned_url:
        bot.reply_to(message, f"🚫 <b>URL Blocked!</b>\n\nReason: {ban_reason}")
        return
    
    user = get_user(uid)
    is_premium_user = is_premium(uid)
    
    if not is_premium_user and user[3] != today():
        reset_daily(uid)
        user = get_user(uid)
    
    if not is_premium_user and user[1] <= 0:
        kb = InlineKeyboardMarkup()
        kb.row(
            InlineKeyboardButton("💰 Buy Credits", callback_data="buy_credits"),
            InlineKeyboardButton("💎 Get Premium", callback_data="premium_info")
        )
        bot.reply_to(message, "❌ <b>No credits left!</b>\n\nBuy credits or get premium for unlimited downloads!", reply_markup=kb)
        return
    
    if not is_premium_user and time.time() - user[2] < COOLDOWN_SECONDS:
        wait = COOLDOWN_SECONDS - int(time.time() - user[2])
        bot.reply_to(message, f"⏳ <b>Please wait!</b>\n\nCooldown: <b>{wait}s</b> remaining.\n\n💎 Get premium for no cooldown!")
        return
    
    # Try to detect platform for display purposes
    platforms = {
        "instagram.com": "Instagram",
        "twitter.com": "X",
        "x.com": "X",
        "facebook.com": "Facebook",
        "fb.watch": "Facebook",
        "youtube.com": "YouTube",
        "youtu.be": "YouTube",
        "terabox.com": "TeraBox",
        "teraboxlink.com": "TeraBox",
        "tiktok.com": "TikTok",
        "pinterest.com": "Pinterest",
        "reddit.com": "Reddit",
        "linkedin.com": "LinkedIn",
        "snapchat.com": "Snapchat",
        "vimeo.com": "Vimeo",
        "dailymotion.com": "Dailymotion",
        "twitch.tv": "Twitch",
        "soundcloud.com": "SoundCloud",
        "spotify.com": "Spotify"
    }
    
    platform = "Link"
    for domain, name in platforms.items():
        if domain in url.lower():
            platform = name
            break
    
    process_msg = bot.reply_to(message, f"⏳ <b>Processing your {platform}...</b>")
    
    try:
        api_url = API_ENDPOINT + quote_plus(url)
        r = session.get(api_url, timeout=60)
        data = r.json()
        
        # Check if API returned success with media
        if data.get("status") == "success" and data.get("media_url"):
            media_url = data.get("media_url")
            title = data.get("title", "Video")
            
            if not is_premium_user:
                use_credit(uid)
            inc_download()
            add_download_history(uid, url, platform, True)
            
            bot.delete_message(message.chat.id, process_msg.message_id)
            
            kb = InlineKeyboardMarkup()
            kb.row(
                InlineKeyboardButton("📥 Download Again", callback_data="download"),
                InlineKeyboardButton("💳 My Credits", callback_data="credits")
            )
            
            credits_text = f"💳 Credits left: <b>{user[1] - (0 if is_premium_user else 1)}</b>" if not is_premium_user else "💎 <b>Premium - Unlimited!</b>"
            
            bot.send_message(
                message.chat.id,
                f"🎬 <b>Download Ready!</b>\n\n"
                f"📹 {title[:100]}{'...' if len(title) > 100 else ''}\n\n"
                f"📥 <a href='{media_url}'>Click here to download</a>\n\n"
                f"{credits_text}",
                reply_markup=kb,
                disable_web_page_preview=True)
        else:
            # API returned error or no media - URL not supported
            bot.delete_message(message.chat.id, process_msg.message_id)
            add_download_history(uid, url, platform, False)
            
            kb = InlineKeyboardMarkup()
            kb.row(
                InlineKeyboardButton("🎫 Contact Support", callback_data="main_menu"),
                InlineKeyboardButton("❓ Help", callback_data="help")
            )
            
            bot.send_message(
                message.chat.id,
                "😔 <b>Sorry, this link is not supported!</b>\n\n"
                "The platform you're trying to download from may not be supported by our service.\n\n"
                "💡 <b>What you can do:</b>\n"
                "• Try a different link\n"
                "• Contact admin for help\n"
                "• Request support for this platform\n\n"
                "We're constantly adding new platforms! 🚀",
                reply_markup=kb)
        
    except Exception as e:
        bot.delete_message(message.chat.id, process_msg.message_id)
        print(f"Download error: {e}")
        add_download_history(uid, url, platform, False)
        send_error_to_admin(e, uid, url)
        
        kb = InlineKeyboardMarkup()
        kb.row(
            InlineKeyboardButton("🎫 Contact Support", callback_data="main_menu"),
            InlineKeyboardButton("❓ Help", callback_data="help")
        )
        
        bot.send_message(
            message.chat.id,
            "😔 <b>Sorry, this link is not supported!</b>\n\n"
            "The platform you're trying to download from may not be supported by our service.\n\n"
            "💡 <b>What you can do:</b>\n"
            "• Try a different link\n"
            "• Contact admin for help\n"
            "• Request support for this platform\n\n"
            "We're constantly adding new platforms! 🚀",
            reply_markup=kb)

# ================= WEBHOOK =================
app = Flask(__name__)

@app.route('/cashfree_webhook', methods=['POST'])
def cashfree_webhook():
    """Webhook for Cashfree to send payment status"""
    try:
        data = request.json
        print("Received Cashfree webhook:")
        print(json.dumps(data, indent=4))
        
        # TODO: Add signature verification
        # TODO: Process the payment confirmation
        
        return jsonify({'status': 'success'}), 200
    except Exception as e:
        print(f"Error in webhook: {e}")
        return jsonify({'status': 'error'}), 400

def run_webhook_server():
    """Run the Flask app in a separate thread"""
    # Note: For production, use a proper WSGI server like Gunicorn or uWSGI
    # app.run(host='0.0.0.0', port=8080)
    # NEW:
port = int(os.getenv('PORT', 8080))
app.run(host='0.0.0.0', port=port)

# ================= RUN =================
if __name__ == "__main__":
    # Start the webhook server in a background thread
    webhook_thread = threading.Thread(target=run_webhook_server)
    webhook_thread.daemon = True
    webhook_thread.start()
    print("🚀 Bot started successfully!")
    print(f"📊 Admin ID: {ADMIN_ID}")
    print(f"🎁 Daily Credits: {DAILY_FREE_CREDITS}")
    print(f"👥 Referral Bonus: {REFERRAL_BONUS}")
    print(f"💎 Premium: ₹{PREMIUM_PRICE} for {PREMIUM_DURATION_DAYS} days")
    print("-" * 40)
    
    print("💰 Credit Plans:")
    for plan_id, plan in CREDIT_PLANS.items():
        popular = " ⭐ POPULAR" if plan["popular"] else ""
        print(f"  • {plan['name']}: ₹{plan['price']} = {plan['credits']} credits{popular}")
    print("-" * 40)
    
    retry_count = 0
    max_retries = 5
    
    while retry_count < max_retries:
        try:
            print("🔄 Starting bot polling...")
            bot.infinity_polling(skip_pending=True, timeout=30)
            break
        except Exception as e:
            retry_count += 1
            print(f"❌ Connection error (attempt {retry_count}/{max_retries}): {e}")
            
            if retry_count < max_retries:
                wait_time = min(retry_count * 5, 30)
                print(f"⏳ Retrying in {wait_time} seconds...")
                time.sleep(wait_time)
            else:
                print("❌ Max retries reached. Please check your internet connection.")
                print("\n💡 Troubleshooting tips:")
                print("   1. Check if you have an active internet connection")
                print("   2. Verify that api.telegram.org is not blocked by your firewall")
                print("   3. Try using a VPN if you're in a region with Telegram restrictions")
                print("   4. Check your DNS settings")
                break
