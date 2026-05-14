"""
Channel Subscription Bot — v3
==============================
Improvements over v2:
  - QR sent ONLY as document (no big photo) — cleaner UX
  - Previous bot messages auto-erase as flow progresses (chat stays clean)
  - protect_content=True on all user-facing messages (no forward, no save)
  - Configurable delay before showing "Enter UPI Name" button (default 5s)
  - User's typed UPI name and uploaded photos are also deleted for privacy
"""

import os
import csv
import io
import json
import random
import logging
import sqlite3
from io import BytesIO
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)

# ==================================================================
# CONFIG
# ==================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID  = int(os.getenv("ADMIN_ID", "0"))
# Use local database in repo root (survives all deploys)
# This allows database to be committed to git
DB_PATH   = os.getenv("DB_PATH", "master_database.db")
TZ        = ZoneInfo(os.getenv("TIMEZONE", "Asia/Kolkata"))

QR_FILES = [
    os.getenv("QR1_PATH", "qr1.jpg"),
    os.getenv("QR2_PATH", "qr2.jpg"),
    os.getenv("QR3_PATH", "qr3.jpg"),
]
QR_WEIGHTS = [60, 20, 20]

# Delay before showing the I've Paid button (kept for backward compat; unused now)
QR_DELAY_SECONDS = int(os.getenv("QR_DELAY_SECONDS", "2"))

# Auto-delete unused QR after this many MINUTES (0 = never expire)
# If user gets the QR but doesn't tap "I've Paid" in time, QR is removed.
QR_EXPIRY_MINUTES = int(os.getenv("QR_EXPIRY_MINUTES", "15"))

# Auto-wipe user chat this many MINUTES after approval/rejection (0 = disabled)
# Default 30 mins — gives user time to join channel, then cleans up the chat.
# When user comes back later, chat looks fresh.
AUTO_WIPE_MINUTES = int(os.getenv("AUTO_WIPE_MINUTES", "30"))

def _parse_channel(s: str):
    if not s or "|" not in s:
        return None
    parts = [p.strip() for p in s.split("|")]
    if len(parts) != 3:
        return None
    return {"name": parts[0], "price": int(parts[1]), "link": parts[2]}

CHANNELS = []
for i in range(1, 11):
    c = _parse_channel(os.getenv(f"CHANNEL_{i}", ""))
    if c:
        c["id"] = i
        CHANNELS.append(c)

# BUNDLE CHANNELS — Each bundle has its own invite link
# Format: "Bundle Name|Price|Invite Link"
# Examples:
#   BUNDLE_1="1 Channel|30|https://t.me/+BUNDLE1_HASH"
#   BUNDLE_5="5 Channels|59|https://t.me/+BUNDLE5_HASH"
def _parse_bundle(price: int, s: str):
    if not s or "|" not in s:
        return None
    parts = [p.strip() for p in s.split("|")]
    if len(parts) != 3:
        return None
    return {"name": parts[0], "price": int(parts[1]), "link": parts[2]}

BUNDLES = {}  # {price: bundle_info}
bundle_config = [
    (30, "BUNDLE_1"),
    (59, "BUNDLE_5"),
    (79, "BUNDLE_10"),
    (99, "BUNDLE_15"),
]
for price, env_key in bundle_config:
    b = _parse_bundle(price, os.getenv(env_key, ""))
    if b:
        BUNDLES[price] = b

# Support handle for help button (after approval / on issues)
SUPPORT_HANDLE = os.getenv("SUPPORT_HANDLE", "@LinkbroSupport").lstrip("@")

SUMMARY_HOUR   = int(os.getenv("SUMMARY_HOUR", "9"))
SUMMARY_MINUTE = int(os.getenv("SUMMARY_MINUTE", "0"))
PENDING_REMINDER_HOURS = int(os.getenv("PENDING_REMINDER_HOURS", "24"))

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("subbot")

# ==================================================================
# DATABASE
# ==================================================================
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                username    TEXT,
                first_name  TEXT,
                last_name   TEXT,
                tracked_msgs TEXT DEFAULT '[]',
                menu_msg_id INTEGER,
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS purchases (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                channel_id  INTEGER NOT NULL,
                channel_name TEXT,
                amount      INTEGER,
                qr_used     INTEGER,
                upi_name    TEXT,
                status      TEXT DEFAULT 'started',
                screenshot_file_id TEXT,
                main_msg_id INTEGER,
                qr_downloaded_at  TEXT,
                upi_submitted_at  TEXT,
                approved_at TEXT,
                rejected_at TEXT,
                reminder_sent INTEGER DEFAULT 0,
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_purch_user   ON purchases(user_id);
            CREATE INDEX IF NOT EXISTS idx_purch_status ON purchases(status);
            CREATE INDEX IF NOT EXISTS idx_purch_date   ON purchases(created_at);
            CREATE TABLE IF NOT EXISTS blocked_users (
                user_id     INTEGER PRIMARY KEY,
                reason      TEXT,
                blocked_at  TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS dnd_users (
                user_id     INTEGER PRIMARY KEY,
                opted_at    TEXT DEFAULT CURRENT_TIMESTAMP,
                reason      TEXT
            );
            CREATE TABLE IF NOT EXISTS user_messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                message_type TEXT,  -- 'text', 'photo', 'upi_name', etc
                content     TEXT,   -- actual text or file_id for photos
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_msg_user ON user_messages(user_id);
            CREATE TABLE IF NOT EXISTS admin_settings (
                key         TEXT PRIMARY KEY,
                value       TEXT,
                updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS channel_visibility (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id  INTEGER NOT NULL,
                segment     TEXT NOT NULL,  -- 'all', 'unpaid', 'T1', 'T1,T2', etc
                is_visible  INTEGER DEFAULT 1,  -- 1=show, 0=hide
                custom_price INTEGER,  -- NULL=use default price, or override with custom
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_ch_vis ON channel_visibility(channel_id, segment);
            CREATE TABLE IF NOT EXISTS active_promotions (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id          INTEGER NOT NULL,
                segment             TEXT NOT NULL,  -- 'all', 'unpaid', 'T1', 'T1,T2', etc
                promotion_price     INTEGER NOT NULL,
                is_active           INTEGER DEFAULT 1,
                started_at          TEXT DEFAULT CURRENT_TIMESTAMP,
                expires_at          TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_promo ON active_promotions(channel_id, segment) WHERE is_active=1;
        """)
        # Migration safety: add columns if upgrading from v2/v3
        try:
            conn.execute("ALTER TABLE users ADD COLUMN tracked_msgs TEXT DEFAULT '[]'")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE users ADD COLUMN menu_msg_id INTEGER")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE purchases ADD COLUMN main_msg_id INTEGER")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE channel_visibility ADD COLUMN custom_price INTEGER")
        except sqlite3.OperationalError:
            pass

def upsert_user(u):
    with db() as conn:
        conn.execute("""
            INSERT INTO users(user_id, username, first_name, last_name)
            VALUES(?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
              username=excluded.username,
              first_name=excluded.first_name,
              last_name=excluded.last_name
        """, (u.id, u.username, u.first_name, u.last_name))

def get_user_row(user_id):
    with db() as conn:
        r = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return dict(r) if r else None

def get_owned_channel_ids(user_id):
    with db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT channel_id FROM purchases "
            "WHERE user_id=? AND status='approved'", (user_id,)
        ).fetchall()
    return {r["channel_id"] for r in rows}

def get_owned_bundle_prices(user_id):
    """Get set of bundle prices user owns (bundles have channel_id=0)."""
    with db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT amount FROM purchases "
            "WHERE user_id=? AND channel_id=0 AND status='approved'", (user_id,)
        ).fetchall()
    return {int(r["amount"]) for r in rows}

def has_paid_tier1(user_id):
    if not CHANNELS:
        return False
    return CHANNELS[0]["id"] in get_owned_channel_ids(user_id)

def get_user_segment(user_id):
    """Return user's segment: 'unpaid', 'T1', 'T1,T2', 'T1,T2,T3', etc."""
    owned = get_owned_channel_ids(user_id)
    if not owned:
        return "unpaid"
    # Build tier string from channel IDs
    tier_str = "T" + ",T".join(str(cid) for cid in sorted(owned))
    return tier_str

def get_visible_channels_for_user(user_id):
    """Get which channels should be visible for this user based on admin configuration."""
    segment = get_user_segment(user_id)
    
    with db() as conn:
        # Get visibility rules for this segment and 'all'
        rows = conn.execute("""
            SELECT DISTINCT channel_id FROM channel_visibility
            WHERE (segment=? OR segment='all') AND is_visible=1
            ORDER BY channel_id
        """, (segment,)).fetchall()
    
    if rows:
        # If explicit rules exist, use them
        return {r["channel_id"] for r in rows}
    
    # No explicit rules — show all channels (backward compat)
    return {c["id"] for c in CHANNELS}

def get_channel_price_for_segment(channel_id, segment):
    """Get the effective price for a channel in a segment.
    Priority: 1. Active promotion 2. Custom segment price 3. Default channel price"""
    return get_effective_price(channel_id, segment)

def set_channel_visibility(channel_id, segment, is_visible):
    """Set visibility of a channel for a user segment."""
    with db() as conn:
        conn.execute("""
            INSERT INTO channel_visibility(channel_id, segment, is_visible)
            VALUES(?, ?, ?)
            ON CONFLICT(channel_id, segment) DO UPDATE SET is_visible=?
        """, (channel_id, segment, is_visible, is_visible))

def set_custom_price(channel_id, segment, price):
    """Set custom price for a channel in a segment. price=None to remove."""
    with db() as conn:
        if price is None:
            conn.execute("""
                UPDATE channel_visibility
                SET custom_price=NULL
                WHERE channel_id=? AND segment=?
            """, (channel_id, segment))
        else:
            conn.execute("""
                INSERT INTO channel_visibility(channel_id, segment, custom_price)
                VALUES(?, ?, ?)
                ON CONFLICT(channel_id, segment) DO UPDATE SET custom_price=?
            """, (channel_id, segment, price, price))

def get_active_promotion_price(channel_id, segment):
    """Get active promotion price if exists, else None."""
    with db() as conn:
        row = conn.execute("""
            SELECT promotion_price FROM active_promotions
            WHERE channel_id=? AND segment=? AND is_active=1
        """, (channel_id, segment)).fetchone()
    return row["promotion_price"] if row else None

def set_active_promotion(channel_id, segment, promotion_price):
    """Set active promotion for a channel+segment."""
    with db() as conn:
        conn.execute("""
            INSERT INTO active_promotions(channel_id, segment, promotion_price)
            VALUES(?, ?, ?)
            ON CONFLICT(channel_id, segment) DO UPDATE SET promotion_price=?, is_active=1
        """, (channel_id, segment, promotion_price, promotion_price))

def clear_active_promotion(channel_id, segment):
    """Deactivate promotion for a channel+segment."""
    with db() as conn:
        conn.execute("""
            UPDATE active_promotions
            SET is_active=0
            WHERE channel_id=? AND segment=?
        """, (channel_id, segment))

def get_effective_price(channel_id, segment):
    """Get effective price: promotion > custom > default.
    Priority: 1. Active promotion 2. Custom segment price 3. Default channel price"""
    # Check promotion first
    promo = get_active_promotion_price(channel_id, segment)
    if promo:
        return promo
    
    # Check custom price
    with db() as conn:
        row = conn.execute("""
            SELECT custom_price FROM channel_visibility
            WHERE channel_id=? AND segment=?
        """, (channel_id, segment)).fetchone()
    if row and row["custom_price"]:
        return row["custom_price"]
    
    # Return default
    channel = next((c for c in CHANNELS if c["id"] == channel_id), None)
    return channel["price"] if channel else 0

def create_purchase(user_id, channel, qr_idx) -> int:
    with db() as conn:
        cur = conn.execute("""
            INSERT INTO purchases(user_id,channel_id,channel_name,amount,qr_used,status)
            VALUES(?,?,?,?,?,?)
        """, (user_id, channel["id"], channel["name"], channel["price"], qr_idx, "qr_sent"))
        return cur.lastrowid

def update_purchase(pid, **fields):
    if not fields: return
    sets = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [pid]
    with db() as conn:
        conn.execute(f"UPDATE purchases SET {sets} WHERE id=?", values)

def get_purchase(pid):
    with db() as conn:
        r = conn.execute("SELECT * FROM purchases WHERE id=?", (pid,)).fetchone()
        return dict(r) if r else None

def get_active_purchase(user_id):
    with db() as conn:
        r = conn.execute("""
            SELECT * FROM purchases
            WHERE user_id=? AND status NOT IN ('approved','rejected','cancelled')
            ORDER BY id DESC LIMIT 1
        """, (user_id,)).fetchone()
        return dict(r) if r else None

# -------- Message tracking (for auto-erase) --------
def get_tracked_msgs(user_id):
    with db() as conn:
        r = conn.execute("SELECT tracked_msgs FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not r:
        return []
    return json.loads(r["tracked_msgs"] or "[]")

def set_tracked_msgs(user_id, ids):
    with db() as conn:
        conn.execute("UPDATE users SET tracked_msgs=? WHERE user_id=?",
                     (json.dumps(ids), user_id))

def track_msg(user_id, msg_id):
    ids = get_tracked_msgs(user_id)
    if msg_id not in ids:
        ids.append(msg_id)
        set_tracked_msgs(user_id, ids)

def is_blocked(user_id: int) -> bool:
    """Check if user is blocked."""
    with db() as conn:
        r = conn.execute("SELECT user_id FROM blocked_users WHERE user_id=?",
                        (user_id,)).fetchone()
    return r is not None

def block_user(user_id: int, reason: str):
    """Block a user and log reason."""
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO blocked_users (user_id, reason) "
            "VALUES (?, ?)", (user_id, reason))
    log.warning(f"User {user_id} blocked. Reason: {reason}")

def unblock_user(user_id: int):
    """Unblock a user."""
    with db() as conn:
        conn.execute("DELETE FROM blocked_users WHERE user_id=?", (user_id,))

def is_dnd(user_id: int) -> bool:
    """Check if user has opted out of promotional messages (DND = Do Not Disturb)."""
    with db() as conn:
        r = conn.execute("SELECT user_id FROM dnd_users WHERE user_id=?",
                        (user_id,)).fetchone()
    return r is not None

def set_dnd(user_id: int):
    """Enable DND (Do Not Disturb) for user - opt out of promos."""
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO dnd_users (user_id, opted_at) "
            "VALUES (?, CURRENT_TIMESTAMP)", (user_id,))
    log.info(f"DND enabled for user {user_id}")

def clear_dnd(user_id: int):
    """Disable DND - user can receive promos again."""
    with db() as conn:
        conn.execute("DELETE FROM dnd_users WHERE user_id=?", (user_id,))
    log.info(f"DND cleared for user {user_id}")

def is_fallback_enabled() -> bool:
    """Check if fallback bundles are enabled by admin."""
    with db() as conn:
        r = conn.execute("SELECT value FROM admin_settings WHERE key='fallback_enabled'",
                        ).fetchone()
    return r and r["value"].lower() == "true" if r else True  # Default: enabled

def is_special_offers_enabled() -> bool:
    """Check if special offers/promotions are enabled by admin."""
    with db() as conn:
        r = conn.execute("SELECT value FROM admin_settings WHERE key='special_offers_enabled'",
                        ).fetchone()
    return r and r["value"].lower() == "true" if r else True  # Default: enabled

def log_user_message(user_id: int, msg_type: str, content: str):
    """Log a message from a user (text, photo, upi_name, etc)."""
    with db() as conn:
        conn.execute(
            "INSERT INTO user_messages (user_id, message_type, content) "
            "VALUES (?, ?, ?)", (user_id, msg_type, content))

def get_bad_submission_count(user_id: int) -> int:
    """Count bad submissions (rejected) in last 24h."""
    with db() as conn:
        r = conn.execute("""
            SELECT COUNT(*) as cnt FROM purchases
            WHERE user_id=? AND status='rejected'
            AND rejected_at > datetime('now', '-1 day')
        """, (user_id,)).fetchone()
    return r["cnt"] if r else 0

def get_admin_setting(key: str, default: str = "") -> str:
    """Get an admin setting."""
    with db() as conn:
        r = conn.execute(
            "SELECT value FROM admin_settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default

def set_admin_setting(key: str, value: str):
    """Set an admin setting."""
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO admin_settings (key, value) "
            "VALUES (?, ?)", (key, value))

def get_user_tier(user_id: int) -> str:
    """Determine user's highest tier: 'none', 'tier1', 'tier2', 'tier3', etc."""
    owned = get_owned_channel_ids(user_id)
    if not owned:
        return "none"
    max_tier = max(owned)
    return f"tier{max_tier}"

def get_users_by_group(group: str) -> list:
    """Get list of (user_id, first_name, last_name, username, tier) for a target group.
    
    Groups:
    - 'all' = all users
    - 'unpaid' = no approved purchases
    - 'tier1' = owns channel 1 only
    - 'tier1+' = owns channel 1 and/or higher
    - 'tier2' = owns channel 2 (may own others)
    - 'tier2+' = owns channel 2 and/or higher
    - etc.
    """
    with db() as conn:
        if group == "all":
            rows = conn.execute("""
                SELECT user_id, first_name, last_name, username
                FROM users ORDER BY user_id DESC
            """).fetchall()
        elif group == "unpaid":
            # Users with no approved purchases
            rows = conn.execute("""
                SELECT u.user_id, u.first_name, u.last_name, u.username
                FROM users u
                WHERE u.user_id NOT IN (
                    SELECT DISTINCT user_id FROM purchases WHERE status='approved'
                )
                ORDER BY u.user_id DESC
            """).fetchall()
        elif group.startswith("tier"):
            # tier1, tier1+, tier2, tier2+, etc.
            if "+" in group:
                # tier1+ = owns >= tier 1
                tier_num = int(group[4])  # extract number from "tier1+"
                rows = conn.execute("""
                    SELECT u.user_id, u.first_name, u.last_name, u.username
                    FROM users u
                    WHERE u.user_id IN (
                        SELECT DISTINCT user_id FROM purchases
                        WHERE status='approved' AND channel_id >= ?
                    )
                    ORDER BY u.user_id DESC
                """, (tier_num,)).fetchall()
            else:
                # tier1 = owns tier 1 only (no tier 2+)
                tier_num = int(group[4])  # extract number from "tier1"
                rows = conn.execute("""
                    SELECT u.user_id, u.first_name, u.last_name, u.username
                    FROM users u
                    WHERE u.user_id IN (
                        SELECT DISTINCT user_id FROM purchases
                        WHERE status='approved' AND channel_id = ?
                    )
                    ORDER BY u.user_id DESC
                """, (tier_num,)).fetchall()
        else:
            return []
    
    # Annotate with tier
    result = []
    for r in rows:
        tier = get_user_tier(r["user_id"])
        result.append({
            "user_id": r["user_id"],
            "first_name": r["first_name"],
            "last_name": r["last_name"],
            "username": r["username"],
            "tier": tier,
        })
    return result

async def clear_tracked(context, user_id):
    """Delete all tracked bot messages in user's chat."""
    ids = get_tracked_msgs(user_id)
    for mid in ids:
        try:
            await context.bot.delete_message(chat_id=user_id, message_id=mid)
        except Exception as e:
            log.debug(f"delete msg {mid} failed: {e}")
    set_tracked_msgs(user_id, [])

async def safe_delete(context, chat_id, msg_id):
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except Exception as e:
        log.debug(f"safe_delete {msg_id} failed: {e}")

async def edit_main(context, chat_id, msg_id, text, reply_markup=None):
    """Edit a tracked main message — works whether it's text or caption.
    Tries text first; falls back to caption (for photo/document messages)."""
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=msg_id,
            text=text, parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )
        return True
    except Exception as e1:
        try:
            await context.bot.edit_message_caption(
                chat_id=chat_id, message_id=msg_id,
                caption=text, parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
            return True
        except Exception as e2:
            log.debug(f"edit_main failed: text={e1} caption={e2}")
            return False

async def auto_delete_message(context):
    """JobQueue callback: delete a single message later."""
    data = context.job.data
    try:
        await context.bot.delete_message(
            chat_id=data["chat_id"], message_id=data["message_id"]
        )
    except Exception as e:
        log.debug(f"auto_delete_message failed: {e}")

def schedule_auto_delete(context, chat_id, message_id, delay_seconds=30):
    """Schedule a message for deletion after `delay_seconds`."""
    context.job_queue.run_once(
        auto_delete_message,
        when=delay_seconds,
        data={"chat_id": chat_id, "message_id": message_id},
        name=f"autodel_{chat_id}_{message_id}",
    )

async def auto_wipe_user_chat(context):
    """JobQueue callback: wipe ALL bot MESSAGES for a user after delay.
    Called automatically X minutes after approval/rejection.

    IMPORTANT: This wipes only messages + menu refs — NOT purchase records.
    Approved purchases stay so the user is still recognized as a paid customer
    when they come back. They'll see ✅ on /start for what they already own."""
    user_id = context.job.data["user_id"]
    log.info(f"Auto-wiping chat (msgs only) for user {user_id}")
    # Delete all tracked bot messages
    ids = get_tracked_msgs(user_id)
    for mid in ids:
        try:
            await context.bot.delete_message(chat_id=user_id, message_id=mid)
        except Exception as e:
            log.debug(f"auto-wipe delete {mid} failed: {e}")
    # Reset menu/main refs so next /start sends fresh, but KEEP purchases
    with db() as conn:
        conn.execute(
            "UPDATE users SET menu_msg_id=NULL, tracked_msgs='[]' "
            "WHERE user_id=?", (user_id,))
        conn.execute(
            "UPDATE purchases SET main_msg_id=NULL WHERE user_id=?",
            (user_id,))


async def expire_qr(context):
    """JobQueue callback: if user got QR but didn't tap 'I've Paid' within
    QR_EXPIRY_MINUTES, delete the QR and mark purchase as cancelled.
    Frees up the chat space and removes stale QRs from inactive users."""
    data = context.job.data
    user_id = data["user_id"]
    pid = data["purchase_id"]
    qr_msg_id = data["qr_msg_id"]

    p = get_purchase(pid)
    if not p:
        return
    # Only expire if still in 'qr_sent' state — user never advanced
    if p["status"] != "qr_sent":
        return

    log.info(f"Expiring stale QR for user {user_id}, purchase {pid}")

    # Delete the QR photo
    try:
        await context.bot.delete_message(chat_id=user_id, message_id=qr_msg_id)
    except Exception as e:
        log.debug(f"expire_qr delete failed: {e}")

    # Mark purchase as cancelled so it doesn't pollute /pending list
    update_purchase(pid, status="cancelled")

    # Send a brief expiry message that auto-deletes in 30s
    try:
        m = await context.bot.send_message(
            chat_id=user_id,
            text="⏰ <b>QR expired</b>\n\nSend /start to try again.",
            parse_mode=ParseMode.HTML,
            disable_notification=True,
        )
        track_msg(user_id, m.message_id)
        # Auto-delete the expiry message itself after 30 seconds
        context.job_queue.run_once(
            auto_delete_message,
            when=30,
            data={"chat_id": user_id, "message_id": m.message_id},
            name=f"expmsg_del_{user_id}_{m.message_id}",
        )
    except Exception as e:
        log.debug(f"expire_qr message send failed: {e}")


def schedule_auto_wipe(context, user_id, minutes):
    """Schedule auto-wipe of user's bot chat after `minutes`."""
    if minutes <= 0:
        return
    context.job_queue.run_once(
        auto_wipe_user_chat,
        when=timedelta(minutes=minutes),
        data={"user_id": user_id},
        name=f"autowipe_{user_id}",
    )

def reset_user_data(user_id: int):
    """Delete all purchases + reset menu/main message refs for a user."""
    with db() as conn:
        conn.execute("DELETE FROM purchases WHERE user_id=?", (user_id,))
        conn.execute(
            "UPDATE users SET tracked_msgs='[]', menu_msg_id=NULL "
            "WHERE user_id=?", (user_id,))

async def full_reset(context, user_id: int):
    """Wipe ALL bot messages from user's chat AND reset DB state.
    User /start as if brand new.

    Fast & safe: only deletes IDs we KNOW are bot messages — no sweep that
    would hammer Telegram's rate limiter and slow everything down."""
    # Gather all known bot message IDs from every source
    all_ids = set()
    all_ids.update(get_tracked_msgs(user_id))
    with db() as conn:
        u = conn.execute(
            "SELECT menu_msg_id FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if u and u["menu_msg_id"]:
            all_ids.add(u["menu_msg_id"])
        purchases = conn.execute(
            "SELECT main_msg_id FROM purchases WHERE user_id=?", (user_id,)
        ).fetchall()
        for p in purchases:
            if p["main_msg_id"]:
                all_ids.add(p["main_msg_id"])

    # Delete each known bot message — fast, no rate-limit risk
    deleted = 0
    for mid in all_ids:
        try:
            await context.bot.delete_message(chat_id=user_id, message_id=mid)
            deleted += 1
        except Exception as e:
            log.debug(f"full_reset delete {mid} failed: {e}")

    log.info(f"full_reset: deleted {deleted} messages for user {user_id}")

    # Wipe all DB state
    reset_user_data(user_id)

# ==================================================================
# HELPERS
# ==================================================================
def pick_qr() -> int:
    return random.choices(range(len(QR_FILES)), weights=QR_WEIGHTS, k=1)[0]

def fmt_user_block(u_row, purchase=None) -> str:
    # Convert sqlite3.Row to dict if needed
    if hasattr(u_row, "keys"):
        u_dict = dict(zip(u_row.keys(), u_row)) if not isinstance(u_row, dict) else u_row
    else:
        u_dict = u_row
    
    first = u_dict.get("first_name") or ""
    last = u_dict.get("last_name") or ""
    name = f"{first} {last}".strip() or "—"
    username = u_dict.get("username")
    un = f"@{username}" if username else "(no username)"
    
    out = (
        f"👤 <b>User</b>\n"
        f"• Name      : {name}\n"
        f"• Username  : {un}\n"
        f"• User ID   : <code>{u_dict['user_id']}</code>\n"
        f"• Open chat : <a href='tg://user?id={u_dict['user_id']}'>tap here</a>"
    )
    if purchase:
        out += (
            f"\n\n🛒 <b>Purchase</b>\n"
            f"• Channel : {purchase['channel_name']}\n"
            f"• Amount  : ₹{purchase['amount']}\n"
            f"• QR sent : QR{(purchase.get('qr_used') or 0)+1}"
        )
        if purchase.get("upi_name"):
            out += f"\n• UPI Name: <b>{purchase['upi_name']}</b>"
    return out

def admin_action_kb(pid):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"adm:approve:{pid}"),
            InlineKeyboardButton("❌ Reject",  callback_data=f"adm:reject:{pid}"),
        ],
        [InlineKeyboardButton("📸 Request Screenshot", callback_data=f"adm:reqss:{pid}")],
    ])

# ==================================================================
# USER: /start
# ==================================================================
async def cmd_start(update, context):
    user = update.effective_user
    
    # Check if user is blocked — if so, ignore silently (don't respond)
    if is_blocked(user.id):
        return
    
    upsert_user(user)

    # NOTE: Do NOT delete the user's /start message — Telegram interprets
    # an empty bot chat as "needs Start" and re-shows the floating "Start bot"
    # button which feels jarring to users. Keep /start message visible.

    # OPTIMIZATION: Consolidated DB queries (max 2 instead of 5+)
    with db() as conn:
        # Query 1: Get all approved purchases (channels + bundles)
        owned_purchases = conn.execute(
            "SELECT DISTINCT channel_id, amount FROM purchases "
            "WHERE user_id=? AND status='approved'",
            (user.id,)).fetchall()
        
        # Query 2: Get user_row with menu_msg_id
        user_row = conn.execute(
            "SELECT menu_msg_id FROM users WHERE user_id=?",
            (user.id,)).fetchone()
    
    owned_channel_ids = {row["channel_id"] for row in owned_purchases if row["channel_id"] != 0}
    # Safe bundle price extraction
    owned_bundle_prices = set()
    for row in owned_purchases:
        if row["channel_id"] == 0 and row["amount"]:
            try:
                owned_bundle_prices.add(int(row["amount"]))
            except (ValueError, TypeError):
                pass
    
    paid_t1 = CHANNELS[0]["id"] in owned_channel_ids if CHANNELS else False

    if not CHANNELS:
        m = await context.bot.send_message(
            user.id, "⚠️ No channels configured. Please contact admin.",
            protect_content=True,
            disable_notification=True,
        )
        track_msg(user.id, m.message_id)
        return

    rows = []
    
    if not paid_t1:
        # ====== UNPAID USER FLOW ======
        # Show: Tier 1 (CORE) + Fallback Bundles (if enabled by admin)
        
        # 1. Show Tier 1 only (mandatory entry) — use base price, consistent with upgrades
        c = CHANNELS[0]
        # Use default channel price (same as upgrade price for consistency)
        price = c["price"]
        rows.append([InlineKeyboardButton(
            f"⭐ {c['name']} — ₹{price}",
            callback_data=f"buy:{c['id']}",
        )])
        
        # 2. Add Fallback Bundle Offers (if admin enabled them)
        if is_fallback_enabled():
            rows.append([InlineKeyboardButton(
                "📦 See Budget Bundles", callback_data="fallback_menu"
            )])
        
        intro = (f"👋 <b>Hi {user.first_name}!</b>\n\n"
                 f"<b>Get started with {c['name']} at ₹{price}</b>")

    else:
        # ====== TIER 1+ OWNER FLOW ======
        # Show: Owned channels + Owned bundles + Available upgrades
        intro = f"👋 <b>Welcome back, {user.first_name}!</b>\n\n"
        intro += f"<b>You have access to your purchased content.</b>"
        
        # Show owned channels first (green ✅)
        for c in CHANNELS:
            if c["id"] in owned_channel_ids:
                rows.append([InlineKeyboardButton(
                    f"✅ {c['name']}", url=c["link"]
                )])
        
        # Show owned bundles (green ✅)
        for price in sorted(owned_bundle_prices):
            if price in BUNDLES:
                bundle = BUNDLES[price]
                rows.append([InlineKeyboardButton(
                    f"✅ {bundle['name']}", url=bundle["link"]
                )])
        
        # Show unowned channels as upgrades (locked 🔒) — use base price for consistency
        for c in CHANNELS:
            if c["id"] not in owned_channel_ids:
                # Use base channel price (same as initial offer for consistency)
                price = c["price"]
                rows.append([InlineKeyboardButton(
                    f"🔒 {c['name']} — ₹{price}",
                    callback_data=f"buy:{c['id']}",
                )])


    # Try to EDIT the existing menu message in-place (keeps bot sticky).
    # If it doesn't exist or can't be edited (e.g. it was a document), send fresh.
    existing_id = None
    if user_row:
        try:
            existing_id = user_row["menu_msg_id"]
        except (KeyError, TypeError):
            existing_id = None
    edited = False
    if existing_id:
        try:
            await context.bot.edit_message_text(
                chat_id=user.id, message_id=existing_id,
                text=intro, parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(rows),
            )
            edited = True
        except Exception as e:
            log.debug(f"menu edit failed, will send fresh: {e}")

    if not edited:
        # Clean up any tracked messages from previous flow first
        await clear_tracked(context, user.id)
        m = await context.bot.send_message(
            chat_id=user.id, text=intro, parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
            protect_content=True,
            disable_notification=True,
        )
        track_msg(user.id, m.message_id)
        # Save as menu message so future /start can edit it
        with db() as conn:
            conn.execute("UPDATE users SET menu_msg_id=? WHERE user_id=?",
                         (m.message_id, user.id))

# ==================================================================
# USER: tap "See Fallback Offers" button
# ==================================================================
# ==================================================================
# USER: tap "See Fallback Offers" button
# ==================================================================
async def cb_fallback_menu(update, context):
    """Show fallback bundle options for unpaid users."""
    q = update.callback_query
    await q.answer()
    user = q.from_user
    
    if is_blocked(user.id):
        return
    
    # Bundle options: (display_name, price_rupees)
    bundles = [
        ("1 Channel", 30),
        ("5 Channels", 59),
        ("10 Channels", 79),
        ("15 Channels", 99),
    ]
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📦 {name} — ₹{price}", callback_data=f"buy_bundle:{price}")]
        for name, price in bundles
    ] + [[InlineKeyboardButton("⬅️ Back", callback_data="back_to_start")]])
    
    text = (f"<b>💰 Budget Bundles</b>\n\n"
            f"Get started with affordable options:\n\n"
            f"• <b>1 Channel</b> — ₹30\n"
            f"• <b>5 Channels</b> — ₹59\n"
            f"• <b>10 Channels</b> — ₹79\n"
            f"• <b>15 Channels</b> — ₹99\n\n"
            f"<i>Tap any bundle to proceed with payment</i>")
    
    try:
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except Exception as e:
        log.debug(f"fallback menu edit failed: {e}")


async def cb_buy_bundle(update, context):
    """User selected a bundle. Show payment QR with "I've Paid" button."""
    q = update.callback_query
    await q.answer()
    user = q.from_user
    
    if is_blocked(user.id):
        return
    
    try:
        bundle_price = int(q.data.split(":")[1])
    except (ValueError, IndexError):
        return
    
    # Pick QR code
    qr_idx = pick_qr()
    qr_path = QR_FILES[qr_idx]
    
    if not os.path.exists(qr_path):
        await context.bot.send_message(
            user.id,
            "⚠️ QR code not available. Please contact admin.",
            disable_notification=True,
        )
        return
    
    # Create purchase record for bundle
    # Bundle: channel_id=0, name includes price, amount=bundle_price
    bundle_channel = {"id": 0, "name": f"Bundle (₹{bundle_price})", "price": bundle_price}
    pid = create_purchase(user.id, bundle_channel, qr_idx)
    
    # Delete the menu message (transitioning to QR flow)
    try:
        await q.message.delete()
    except Exception:
        pass
    with db() as conn:
        conn.execute("UPDATE users SET menu_msg_id=NULL WHERE user_id=?", (user.id,))
    await clear_tracked(context, user.id)
    
    # Send QR with "I've Paid" button
    caption = (
        "Tap image → top-right <b>⋮</b> → <b>Share</b> → choose UPI app"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✍️ I've Paid", callback_data=f"upi:start:{pid}"),
        InlineKeyboardButton("⬅️ Back", callback_data="back_from_qr_bundle")
    ]])
    
    with open(qr_path, "rb") as fh:
        doc = await context.bot.send_photo(
            chat_id=user.id, photo=fh,
            caption=caption, parse_mode=ParseMode.HTML,
            reply_markup=kb,
            disable_notification=True,
        )
    track_msg(user.id, doc.message_id)
    update_purchase(pid, main_msg_id=doc.message_id,
                    qr_downloaded_at=datetime.utcnow().isoformat())
    
    # Schedule QR expiry
    if QR_EXPIRY_MINUTES > 0:
        context.job_queue.run_once(
            expire_qr,
            when=timedelta(minutes=QR_EXPIRY_MINUTES),
            data={"user_id": user.id, "purchase_id": pid,
                  "qr_msg_id": doc.message_id},
            name=f"qr_expire_{user.id}_{pid}",
        )
    
    # Notify admin
    u_row = get_user_row(user.id)
    p_row = get_purchase(pid)
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(f"📥 <b>QR Downloaded (Bundle)</b>\n\n{fmt_user_block(u_row, p_row)}\n\n"
                  f"⏳ Awaiting payment proof…"),
            parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        )
    except Exception as e:
        log.error(f"Admin notify failed: {e}")



async def cb_back_to_start(update, context):
    """Go back to /start menu."""
    q = update.callback_query
    await q.answer()
    user = q.from_user
    
    if is_blocked(user.id):
        return
    
    try:
        await q.delete_message()
    except Exception:
        pass
    
    # Trigger /start flow
    await cmd_start(update, context)


# ==================================================================
# USER: tap channel button
# ==================================================================
async def cb_buy(update, context):
    q = update.callback_query
    await q.answer()
    user = q.from_user
    cid = int(q.data.split(":")[1])

    channel = next((c for c in CHANNELS if c["id"] == cid), None)
    if not channel:
        return

    # Already owned — edit the menu in-place to show join button
    if cid in get_owned_channel_ids(user.id):
        try:
            await q.edit_message_text(
                text=f"✅ <b>{channel['name']}</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔓 Join", url=channel["link"])
                ]]),
            )
        except Exception as e:
            log.debug(f"already-owned edit failed: {e}")
        return

    # TIER 1 MANDATORY CHECK: If trying to buy T2+ without T1, block
    if cid != CHANNELS[0]["id"] and not has_paid_tier1(user.id):
        try:
            await q.edit_message_text(
                text=(f"🔐 <b>Access Restricted</b>\n\n"
                      f"You need to buy <b>{CHANNELS[0]['name']}</b> first.\n\n"
                      f"Tap /start to go back"),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        return

    qr_idx = pick_qr()
    qr_path = QR_FILES[qr_idx]
    pid = create_purchase(user.id, channel, qr_idx)

    if not os.path.exists(qr_path):
        m = await context.bot.send_message(
            user.id, "⚠️ Payment QR not configured. Please contact admin.",
            protect_content=True,
            disable_notification=True,
        )
        track_msg(user.id, m.message_id)
        try:
            await context.bot.send_message(
                ADMIN_ID, f"⚠️ QR missing at {qr_path}. User {user.id} blocked."
            )
        except Exception: pass
        return

    # Delete the menu message (we're transitioning to the QR flow).
    # Clear menu_msg_id since it no longer exists.
    try:
        await q.message.delete()
    except Exception:
        pass
    with db() as conn:
        conn.execute("UPDATE users SET menu_msg_id=NULL WHERE user_id=?",
                     (user.id,))
    await clear_tracked(context, user.id)

    # QR photo: short helpful caption, NO protect_content.
    # Reasoning: the QR is YOUR receiving QR. If a user shares/forwards/saves it,
    # the worst that happens is someone else also pays you. Harmless.
    # Allowing save+share lets users open it in UPI apps via system share sheet.
    # Channel links (the real secret) ARE protected separately on approval.
    caption = (
        "Tap image → top-right <b>⋮</b> → <b>Share</b> → choose UPI app"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✍️ I've Paid", callback_data=f"upi:start:{pid}"),
        InlineKeyboardButton("⬅️ Back", callback_data="back_from_qr_channel")
    ]])

    with open(qr_path, "rb") as fh:
        doc = await context.bot.send_photo(
            chat_id=user.id, photo=fh,
            caption=caption, parse_mode=ParseMode.HTML,
            reply_markup=kb,
            disable_notification=True,
        )
    track_msg(user.id, doc.message_id)
    update_purchase(pid, main_msg_id=doc.message_id,
                    qr_downloaded_at=datetime.utcnow().isoformat())

    # Schedule QR expiry — if user doesn't tap "I've Paid" within
    # QR_EXPIRY_MINUTES, the QR auto-deletes and chat is cleaned up.
    if QR_EXPIRY_MINUTES > 0:
        context.job_queue.run_once(
            expire_qr,
            when=timedelta(minutes=QR_EXPIRY_MINUTES),
            data={"user_id": user.id, "purchase_id": pid,
                  "qr_msg_id": doc.message_id},
            name=f"qr_expire_{user.id}_{pid}",
        )

    # Notify admin
    u_row = get_user_row(user.id)
    p_row = get_purchase(pid)
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(f"📥 <b>QR Downloaded</b>\n\n{fmt_user_block(u_row, p_row)}\n\n"
                  f"⏳ Awaiting payment proof…"),
            parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        )
    except Exception as e:
        log.error(f"Admin notify failed: {e}")

async def send_upi_prompt(context):
    """Job: edit the QR document caption in-place to ADD the 'I've Paid' button.
    No new message sent → bot stays sticky in user's chat list."""
    data = context.job.data
    user_id = data["user_id"]
    pid = data["purchase_id"]
    qr_msg_id = data["qr_msg_id"]
    p = get_purchase(pid)
    if not p or p["status"] in ("approved", "rejected", "cancelled"):
        return

    channel = next((c for c in CHANNELS if c["id"] == p["channel_id"]), None)
    if not channel:
        return

    # Updated caption with the button now visible
    new_caption = (
        f"💳 <b>₹{channel['price']}</b> — {channel['name']}\n\n"
        f"✅ Paid?"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✍️ I've Paid",
                             callback_data=f"upi:start:{pid}")
    ]])
    try:
        await context.bot.edit_message_caption(
            chat_id=user_id, message_id=qr_msg_id,
            caption=new_caption, parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
    except Exception as e:
        log.error(f"send_upi_prompt edit failed: {e}")

# ==================================================================
# USER: enter UPI name
# ==================================================================
AWAITING_UPI = {}

async def cb_upi_start(update, context):
    """User tapped 'I've Paid'. Delete the QR photo entirely (user is done
    with it) and send a fresh text-only message to choose verification."""
    q = update.callback_query
    await q.answer()
    user = q.from_user
    pid = int(q.data.split(":")[2])

    p = get_purchase(pid)
    if not p or p["user_id"] != user.id:
        return
    if p["status"] in ("approved", "rejected", "cancelled"):
        return

    # Delete the QR photo — user has paid, no need to keep it
    try:
        await q.message.delete()
    except Exception as e:
        log.debug(f"QR delete failed: {e}")

    # Send a clean text message offering verification choice
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 Send UPI Name",
                              callback_data=f"proof:name:{pid}")],
        [InlineKeyboardButton("📸 Send Screenshot",
                              callback_data=f"proof:shot:{pid}")],
    ])
    m = await context.bot.send_message(
        chat_id=user.id,
        text="✅ <b>HOW TO VERIFY?</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
        protect_content=True,
        disable_notification=True,
    )
    update_purchase(pid, main_msg_id=m.message_id)
    track_msg(user.id, m.message_id)


async def cb_proof_choice(update, context):
    """User chose UPI Name or Screenshot path."""
    q = update.callback_query
    await q.answer()
    user = q.from_user
    parts = q.data.split(":")  # proof:name:<pid>  or  proof:shot:<pid>
    choice = parts[1]
    pid = int(parts[2])

    p = get_purchase(pid)
    if not p or p["user_id"] != user.id:
        return
    if p["status"] in ("approved", "rejected", "cancelled"):
        return

    if choice == "name":
        AWAITING_UPI[user.id] = pid
        await edit_main(context, user.id, q.message.message_id,
                        "✍️ <b>UPI NAME?</b>\n\n"
                        "Type the name on your UPI account.\n\n"
                        "Example: <b>Sakshi</b>")
    else:  # shot
        update_purchase(pid, status="screenshot_requested")
        await edit_main(context, user.id, q.message.message_id,
                        "📸 <b>SEND SCREENSHOT</b>\n\n"
                        "Send your payment success screenshot here.")


async def on_text_message(update, context):
    user = update.effective_user
    
    # Check if user is blocked
    if is_blocked(user.id):
        return
    
    if user.id not in AWAITING_UPI:
        return  # not in capture state, ignore

    pid = AWAITING_UPI.pop(user.id)
    upi_name = update.message.text.strip()

    # Privacy: delete user's typed UPI-name message
    await safe_delete(context, user.id, update.message.message_id)

    # Log this message for admin review
    log_user_message(user.id, "upi_name", upi_name)

    if len(upi_name) < 2 or len(upi_name) > 80:
        # Bad submission — track it
        bad_count = get_bad_submission_count(user.id) + 1
        # Auto-block after 3 bad submissions in 24h
        if bad_count >= 3:
            block_user(user.id, f"3+ rejected submissions in 24h")
            try:
                await context.bot.send_message(
                    user.id,
                    "🚫 Your account has been blocked due to repeated invalid submissions.",
                    disable_notification=True,
                )
            except Exception:
                pass
            return

        AWAITING_UPI[user.id] = pid
        p = get_purchase(pid)
        if p and p.get("main_msg_id"):
            await edit_main(context, user.id, p["main_msg_id"],
                            "⚠️ <b>INVALID NAME</b>\n\n"
                            "Try again.\n\n"
                            "Example: <b>Sakshi</b>")
        return

    update_purchase(
        pid, upi_name=upi_name, status="verifying",
        upi_submitted_at=datetime.utcnow().isoformat(),
    )

    # Edit the main message to show "Verifying..." (in-place, bot stays sticky)
    p = get_purchase(pid)
    if p and p.get("main_msg_id"):
        await edit_main(context, user.id, p["main_msg_id"],
                        "⏳ <b>VERIFYING…</b>\n\n"
                        f"UPI: <b>{upi_name}</b>\n\n"
                        "<i>Please wait. Stay here.</i>")

        # Schedule animated "Verifying." → "Verifying.." → "Verifying..."
        for i, dots in enumerate(["..", "...", ".", "..", "...", ".", "..", "..."]):
            context.job_queue.run_once(
                animate_verifying,
                when=(i + 1) * 5,   # every 5 seconds
                data={
                    "user_id": user.id,
                    "purchase_id": pid,
                    "msg_id": p["main_msg_id"],
                    "upi_name": upi_name,
                    "dots": dots,
                },
                name=f"anim_{user.id}_{pid}_{i}",
            )

    # Show away message if admin is marked away
    away_msg = get_admin_setting("away_message", "")
    if away_msg:
        try:
            await context.bot.send_message(
                chat_id=user.id,
                text=f"⏰ <b>Admin is currently away</b>\n\n{away_msg}",
                parse_mode=ParseMode.HTML,
                disable_notification=True,
            )
        except Exception:
            pass

    # Notify admin
    u_row = get_user_row(user.id)
    p_row = get_purchase(pid)
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(f"💸 <b>Payment Submitted</b>\n\n{fmt_user_block(u_row, p_row)}\n\n"
                  f"Choose an action 👇"),
            parse_mode=ParseMode.HTML, disable_web_page_preview=True,
            reply_markup=admin_action_kb(pid),
        )
    except Exception as e:
        log.error(f"Admin payment notify failed: {e}")


async def animate_verifying(context):
    """Edit the verifying message with cycling dots — keeps user engaged."""
    data = context.job.data
    p = get_purchase(data["purchase_id"])
    if not p or p["status"] != "verifying":
        return  # admin already acted, stop animating
    await edit_main(context, data["user_id"], data["msg_id"],
                    f"⏳ <b>VERIFYING{data['dots']}</b>\n\n"
                    f"UPI: <b>{data['upi_name']}</b>\n\n"
                    "<i>Please wait. Stay here.</i>")

# ==================================================================
# USER: screenshot upload (only when admin requested)
# ==================================================================
async def on_photo(update, context):
    user = update.effective_user
    
    # Check if user is blocked
    if is_blocked(user.id):
        return
    
    p = get_active_purchase(user.id)
    if not p:
        return
    if p["status"] != "screenshot_requested":
        return

    file_id = update.message.photo[-1].file_id

    # Privacy: delete user's photo upload
    await safe_delete(context, user.id, update.message.message_id)

    # Log that a screenshot was submitted
    log_user_message(user.id, "screenshot", file_id)

    update_purchase(p["id"], screenshot_file_id=file_id, status="verifying")

    # Edit the main message in-place
    if p.get("main_msg_id"):
        await edit_main(context, user.id, p["main_msg_id"],
                        "✅ <b>RECEIVED</b>\n\n"
                        "<i>Verifying… Stay here.</i>")

    u_row = get_user_row(user.id)
    p_row = get_purchase(p["id"])
    try:
        await context.bot.send_photo(
            chat_id=ADMIN_ID, photo=file_id,
            caption=(f"📸 <b>Screenshot Received</b>\n\n{fmt_user_block(u_row, p_row)}"),
            parse_mode=ParseMode.HTML,
            reply_markup=admin_action_kb(p["id"]),
        )
    except Exception as e:
        log.error(f"Admin screenshot send failed: {e}")

# ==================================================================
# ADMIN: approve / reject / request screenshot
# ==================================================================
async def cb_admin(update, context):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        await q.answer("Not authorized.", show_alert=True)
        return
    await q.answer()

    parts = q.data.split(":")
    action = parts[1]

    # WIPE: data format is "adm:wipe:<user_id>" (not purchase_id)
    if action == "wipe":
        target_user_id = int(parts[2])
        # Chat-only wipe — preserves user's purchase history.
        # Use this AFTER user joined the channel to clean their DM.
        # If admin wants to full-reset (delete purchases too), use /reset cmd.
        ids = get_tracked_msgs(target_user_id)
        for mid in ids:
            try:
                await context.bot.delete_message(
                    chat_id=target_user_id, message_id=mid)
            except Exception as e:
                log.debug(f"wipe delete {mid} failed: {e}")
        with db() as conn:
            conn.execute(
                "UPDATE users SET menu_msg_id=NULL, tracked_msgs='[]' "
                "WHERE user_id=?", (target_user_id,))
            conn.execute(
                "UPDATE purchases SET main_msg_id=NULL WHERE user_id=?",
                (target_user_id,))
        try:
            await q.answer(
                "✅ Chat wiped. Purchase history kept.",
                show_alert=False)
        except Exception:
            pass
        await _edit_admin_card(q,
            extra=f"\n\n🧹 <b>WIPED</b> {datetime.now(TZ):%d-%b %H:%M}")
        return

    # All other actions: data format is "adm:<action>:<purchase_id>"
    pid = int(parts[2])
    p = get_purchase(pid)
    if not p:
        return

    user_id = p["user_id"]
    channel = next((c for c in CHANNELS if c["id"] == p["channel_id"]), None)

    if action == "approve":
        update_purchase(pid, status="approved",
                        approved_at=datetime.utcnow().isoformat())

        # Build keyboard:
        #  ✅ Just-approved channel → Join URL button (single green tick)
        #  🔒 Unowned channels → buy callback buttons
        #  Previously-owned channels are HIDDEN (user already has access).
        # + Help button at the bottom
        owned_before = get_owned_channel_ids(user_id) - {p["channel_id"]}
        kb_rows = []
        
        # Check if this is a bundle (channel_id == 0)
        if p["channel_id"] == 0:
            # BUNDLE APPROVED
            bundle_price = p["amount"]
            if bundle_price in BUNDLES:
                bundle = BUNDLES[bundle_price]
                kb_rows.append([InlineKeyboardButton(
                    f"✅ {bundle['name']} — Join", url=bundle["link"]
                )])
        else:
            # REGULAR CHANNEL APPROVED
            for c in CHANNELS:
                if c["id"] == p["channel_id"]:
                    # The channel just approved → green tick + Join
                    kb_rows.append([InlineKeyboardButton(
                        f"✅ {c['name']} — Join", url=c["link"]
                    )])
                elif c["id"] in owned_before:
                    # Already owned from a previous purchase → don't show
                    continue
                else:
                    # Not owned → show as locked / buyable
                    kb_rows.append([InlineKeyboardButton(
                        f"🔒 {c['name']} — ₹{c['price']}",
                        callback_data=f"buy:{c['id']}",
                    )])
        
        kb = InlineKeyboardMarkup(kb_rows)

        approval_text = (
            f"🎉 <b>APPROVED</b>\n\n"
        )
        
        # Build approval message based on purchase type
        if p["channel_id"] == 0:
            # BUNDLE
            try:
                bundle_price = int(p["amount"]) if p["amount"] else 0
                if bundle_price > 0 and bundle_price in BUNDLES:
                    bundle_name = BUNDLES[bundle_price].get("name", f"Bundle (₹{bundle_price})")
                    approval_text += f"✅ <b>{bundle_name}</b>"
                else:
                    approval_text += f"✅ <b>Bundle (₹{bundle_price})</b>"
            except (ValueError, TypeError, KeyError):
                approval_text += f"✅ <b>{p.get('channel_name', 'Bundle')}</b>"
        else:
            # CHANNEL
            approval_text += f"✅ <b>{p.get('channel_name', 'Channel')}</b>"

        # Try to edit the existing main message in-place first (clean UX)
        delivered = False
        if p.get("main_msg_id"):
            delivered = await edit_main(
                context, user_id, p["main_msg_id"],
                approval_text, reply_markup=kb,
            )

        # If edit failed, send a fresh message
        if not delivered:
            try:
                m = await context.bot.send_message(
                    chat_id=user_id, text=approval_text,
                    parse_mode=ParseMode.HTML, reply_markup=kb,
                    protect_content=True, disable_notification=True,
                )
                track_msg(user_id, m.message_id)
                update_purchase(pid, main_msg_id=m.message_id)
                delivered = True
            except Exception as e:
                log.error(f"approve delivery failed: {e}")
                try:
                    await context.bot.send_message(
                        ADMIN_ID,
                        f"⚠️ Couldn't deliver approval to user "
                        f"<code>{user_id}</code> ({p['channel_name']}). "
                        f"Open chat manually: tg://user?id={user_id}",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass

        # LIVE STATE REFRESH: Update user's /start menu immediately (if they have one)
        # This ensures the user sees their updated state without needing to tap /start again
        try:
            with db() as conn:
                menu_row = conn.execute(
                    "SELECT menu_msg_id FROM users WHERE user_id=?",
                    (user_id,)
                ).fetchone()
            
            if menu_row and menu_row["menu_msg_id"]:
                # Rebuild the /start menu with new state (now including this purchase)
                with db() as conn:
                    owned_purchases = conn.execute(
                        "SELECT DISTINCT channel_id, amount FROM purchases "
                        "WHERE user_id=? AND status='approved'",
                        (user_id,)).fetchall()
                
                owned_channel_ids = {row["channel_id"] for row in owned_purchases if row["channel_id"] != 0}
                owned_bundle_prices = set()
                for row in owned_purchases:
                    if row["channel_id"] == 0 and row["amount"]:
                        try:
                            owned_bundle_prices.add(int(row["amount"]))
                        except (ValueError, TypeError):
                            pass
                
                if not CHANNELS:
                    return
                
                paid_t1 = CHANNELS[0]["id"] in owned_channel_ids if CHANNELS else False
                rows = []
                
                if not paid_t1:
                    # User still unpaid (shouldn't happen but handle it)
                    c = CHANNELS[0]
                    price = c["price"]
                    rows.append([InlineKeyboardButton(
                        f"⭐ {c['name']} — ₹{price}",
                        callback_data=f"buy:{c['id']}",
                    )])
                    if is_fallback_enabled():
                        rows.append([InlineKeyboardButton(
                            "📦 See Budget Bundles", callback_data="fallback_menu"
                        )])
                    intro = (f"👋 <b>Hi there!</b>\n\n"
                             f"<b>Get started with {c['name']} at ₹{price}</b>")
                else:
                    # User now has access — show updated menu with new purchase
                    intro = f"👋 <b>Welcome back!</b>\n\n<b>You have access to your purchased content.</b>"
                    
                    # Show owned channels
                    for c in CHANNELS:
                        if c["id"] in owned_channel_ids:
                            rows.append([InlineKeyboardButton(
                                f"✅ {c['name']}", url=c["link"]
                            )])
                    
                    # Show owned bundles
                    for price in sorted(owned_bundle_prices):
                        if price in BUNDLES:
                            bundle = BUNDLES[price]
                            rows.append([InlineKeyboardButton(
                                f"✅ {bundle['name']}", url=bundle["link"]
                            )])
                    
                    # Show upgrades
                    for c in CHANNELS:
                        if c["id"] not in owned_channel_ids:
                            price = c["price"]
                            rows.append([InlineKeyboardButton(
                                f"🔒 {c['name']} — ₹{price}",
                                callback_data=f"buy:{c['id']}",
                            )])
                
                # Try to refresh the menu in-place (live update)
                await edit_main(
                    context, user_id, menu_row["menu_msg_id"],
                    intro, reply_markup=InlineKeyboardMarkup(rows)
                )
        except Exception as e:
            log.debug(f"Failed to refresh /start menu: {e}")
            # Silently ignore — if refresh fails, user can still tap /start manually

        # Update admin card with timestamp + Wipe Chat button
        await _edit_admin_card(q,
            extra=f"\n\n✅ <b>APPROVED</b> {datetime.now(TZ):%d-%b %H:%M}"
                  + ("" if delivered else "\n⚠️ <b>USER DELIVERY FAILED</b>"),
            extra_kb=InlineKeyboardMarkup([[
                InlineKeyboardButton("🧹 Wipe User's Chat",
                                     callback_data=f"adm:wipe:{user_id}")
            ]]))

        # Schedule automatic wipe of user's chat after AUTO_WIPE_MINUTES.
        # When user returns later, chat will look fresh.
        schedule_auto_wipe(context, user_id, AUTO_WIPE_MINUTES)

        # Auto-backup DB right after approval — captures purchase records
        # immediately, so a Railway redeploy won't lose this user.
        await event_backup(context)

    elif action == "reject":
        update_purchase(pid, status="rejected",
                        rejected_at=datetime.utcnow().isoformat())

        # Build keyboard for rejection. Logic:
        #   - Owned channels → ✅ Join button
        #   - Tier 1 not owned → only show Tier 1 retry button (others gated)
        #   - Tier 1 owned → show all unowned tiers as 🔒 buttons
        owned_now = get_owned_channel_ids(user_id)
        owns_tier1 = (CHANNELS and CHANNELS[0]["id"] in owned_now)
        kb_rows = []
        for c in CHANNELS:
            if c["id"] in owned_now:
                # Already owned → green tick + Join URL
                kb_rows.append([InlineKeyboardButton(
                    f"✅ {c['name']} — Join", url=c["link"]
                )])
            elif c["id"] == CHANNELS[0]["id"]:
                # Tier 1 — always show the retry button
                kb_rows.append([InlineKeyboardButton(
                    f"🔁 Try Again — {c['name']} ₹{c['price']}",
                    callback_data=f"buy:{c['id']}",
                )])
            elif owns_tier1:
                # Higher tier, user has Tier 1 → show as buyable
                kb_rows.append([InlineKeyboardButton(
                    f"🔒 {c['name']} — ₹{c['price']}",
                    callback_data=f"buy:{c['id']}",
                )])
            # else: higher tier, user lacks Tier 1 → hide it (would just block)
        reject_kb = InlineKeyboardMarkup(kb_rows)

        if p.get("main_msg_id"):
            await edit_main(context, user_id, p["main_msg_id"],
                            "❌ <b>REJECTED</b>\n\n"
                            "<i>Payment not verified.</i>",
                            reply_markup=reject_kb)
        chat_link = f"tg://user?id={user_id}"
        await _edit_admin_card(q,
            extra=f"\n\n❌ <b>REJECTED</b> {datetime.now(TZ):%d-%b %H:%M}",
            extra_kb=InlineKeyboardMarkup([
                [InlineKeyboardButton("🚨 Open User Chat", url=chat_link)],
                [InlineKeyboardButton("🧹 Wipe Now",
                                       callback_data=f"adm:wipe:{user_id}")],
            ]))

        # Auto-wipe user chat after delay so it looks fresh next time
        schedule_auto_wipe(context, user_id, AUTO_WIPE_MINUTES)

        # Auto-backup DB right after rejection — captures the audit trail.
        await event_backup(context)

    elif action == "reqss":
        update_purchase(pid, status="screenshot_requested")

        if p.get("main_msg_id"):
            await edit_main(context, user_id, p["main_msg_id"],
                            "📸 <b>SEND SCREENSHOT</b>\n\n"
                            "<i>UPI name didn't match. Send payment screenshot.</i>")
        await _edit_admin_card(q,
            extra="\n\n📸 <b>Screenshot requested from user</b>")


async def _edit_admin_card(q, extra="", extra_kb=None):
    try:
        if q.message.photo:
            base = q.message.caption_html or ""
            await q.edit_message_caption(
                caption=base + extra, parse_mode=ParseMode.HTML,
                reply_markup=extra_kb,
            )
        else:
            base = q.message.text_html or ""
            await q.edit_message_text(
                text=base + extra, parse_mode=ParseMode.HTML,
                reply_markup=extra_kb, disable_web_page_preview=True,
            )
    except Exception as e:
        log.debug(f"edit admin card failed: {e}")

# ==================================================================
# ADMIN: /stats /pending /summary
# ==================================================================
async def cmd_stats(update, context):
    if update.effective_user.id != ADMIN_ID: return
    with db() as conn:
        total_users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        rows = conn.execute("""
            SELECT status, COUNT(*) c, COALESCE(SUM(amount),0) s
            FROM purchases GROUP BY status
        """).fetchall()
        revenue = conn.execute(
            "SELECT COALESCE(SUM(amount),0) s FROM purchases WHERE status='approved'"
        ).fetchone()["s"]

    text = (f"📊 <b>Bot Stats</b>\n\n"
            f"Total users      : <b>{total_users}</b>\n"
            f"Approved revenue : <b>₹{revenue}</b>\n\n"
            f"<b>Purchases by status:</b>\n")
    for r in rows:
        text += f"• {r['status']}: {r['c']} (₹{r['s']})\n"
    await update.message.reply_html(text)

async def cmd_pending(update, context):
    if update.effective_user.id != ADMIN_ID: return
    with db() as conn:
        rows = conn.execute("""
            SELECT p.*, u.username, u.first_name, u.last_name
            FROM purchases p JOIN users u ON u.user_id=p.user_id
            WHERE p.status IN ('verifying','screenshot_requested')
            ORDER BY p.upi_submitted_at DESC LIMIT 30
        """).fetchall()
    if not rows:
        await update.message.reply_text("✅ No pending verifications.")
        return
    lines = ["⏳ <b>Pending Verifications</b>\n"]
    for r in rows:
        nm = f"{r['first_name'] or ''} {r['last_name'] or ''}".strip()
        un = f"@{r['username']}" if r['username'] else ""
        lines.append(
            f"• #{r['id']} {nm} {un} — {r['channel_name']} ₹{r['amount']}\n"
            f"   UPI: <i>{r['upi_name'] or '—'}</i> "
            f"[<a href='tg://user?id={r['user_id']}'>open</a>]"
        )
    await update.message.reply_html(
        "\n".join(lines), disable_web_page_preview=True
    )

async def cmd_reset(update, context):
    """Admin: /reset <user_id> — full nuclear reset of a user.
    Wipes all bot messages in their chat AND clears all DB state.
    User's chat looks pristine — they /start again like brand new."""
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: <code>/reset &lt;user_id&gt;</code>\n"
            "Or use /resetme to reset your own account.",
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Invalid user_id. Must be a number.")
        return
    await full_reset(context, target_id)
    await update.message.reply_html(
        f"✅ <b>Full reset complete</b> for user <code>{target_id}</code>.\n\n"
        f"All bot messages deleted, all DB state cleared.\n"
        f"<i>(Their own /start text remains — Telegram doesn't allow bots "
        f"to delete user-sent messages.)</i>"
    )

async def cmd_wipeall(update, context):
    """Admin: /wipeall — wipe bot chats for ALL users (preserves purchases).
    Useful for cleaning up after a major change. Confirms before nuking."""
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args or []
    confirm = args and args[0].upper() == "YES"

    with db() as conn:
        rows = conn.execute("SELECT user_id FROM users").fetchall()
    user_count = len(rows)

    if not confirm:
        await update.message.reply_html(
            f"⚠️ <b>Wipe ALL users' chats?</b>\n\n"
            f"This will delete bot messages for <b>{user_count}</b> users.\n"
            f"Purchase history will be preserved.\n\n"
            f"To confirm, send: <code>/wipeall YES</code>"
        )
        return

    await update.message.reply_text(
        f"🧹 Wiping {user_count} users' chats… This may take a minute."
    )

    wiped = 0
    failed = 0
    for r in rows:
        uid = r["user_id"]
        try:
            ids = get_tracked_msgs(uid)
            for mid in ids:
                try:
                    await context.bot.delete_message(chat_id=uid, message_id=mid)
                except Exception:
                    pass
            with db() as conn:
                conn.execute(
                    "UPDATE users SET menu_msg_id=NULL, tracked_msgs='[]' "
                    "WHERE user_id=?", (uid,))
                conn.execute(
                    "UPDATE purchases SET main_msg_id=NULL WHERE user_id=?",
                    (uid,))
            wiped += 1
        except Exception as e:
            log.error(f"wipeall failed for {uid}: {e}")
            failed += 1

    await update.message.reply_html(
        f"✅ <b>Done.</b>\n"
        f"Wiped: {wiped}\nFailed: {failed}"
    )


async def cmd_resetall(update, context):
    """Admin: /resetall — FULL nuclear reset for ALL users.
    Deletes all bot messages AND all purchase records. Use only for testing
    or starting fresh. Requires double confirmation."""
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args or []
    confirm = args and args[0].upper() == "DELETE-EVERYTHING"

    with db() as conn:
        user_count = conn.execute(
            "SELECT COUNT(*) c FROM users").fetchone()["c"]
        purchase_count = conn.execute(
            "SELECT COUNT(*) c FROM purchases").fetchone()["c"]

    if not confirm:
        await update.message.reply_html(
            f"☢️ <b>NUCLEAR RESET — ALL USERS</b>\n\n"
            f"This will permanently delete:\n"
            f"• Bot messages for <b>{user_count}</b> users\n"
            f"• <b>{purchase_count}</b> purchase records (incl. approved)\n\n"
            f"<b>Users will lose their access. They'll have to pay again.</b>\n\n"
            f"To confirm, send: <code>/resetall DELETE-EVERYTHING</code>"
        )
        return

    await update.message.reply_text(
        f"☢️ Resetting everything for {user_count} users…"
    )

    with db() as conn:
        rows = conn.execute("SELECT user_id FROM users").fetchall()

    for r in rows:
        try:
            await full_reset(context, r["user_id"])
        except Exception as e:
            log.error(f"resetall failed for {r['user_id']}: {e}")

    await update.message.reply_html(
        f"☢️ <b>All users reset.</b>\n"
        f"All purchases deleted. DB is now clean."
    )


async def cmd_broadcast(update, context):
    """Admin: /broadcast <message> — send announcement to all users.
    Format: /broadcast Your message text here.
    Supports HTML formatting in the message."""
    if update.effective_user.id != ADMIN_ID:
        return
    msg = update.message.text.split(maxsplit=1)
    if len(msg) < 2:
        await update.message.reply_html(
            "Usage: <code>/broadcast &lt;message&gt;</code>\n\n"
            "Example: <code>/broadcast 🎉 New channel added! Check /start</code>\n\n"
            "Supports HTML: &lt;b&gt;bold&lt;/b&gt;, &lt;i&gt;italic&lt;/i&gt;"
        )
        return

    text = msg[1]
    with db() as conn:
        rows = conn.execute("SELECT user_id FROM users").fetchall()

    await update.message.reply_text(
        f"📢 Broadcasting to {len(rows)} users…"
    )

    sent = 0
    failed = 0
    for r in rows:
        try:
            await context.bot.send_message(
                chat_id=r["user_id"],
                text=f"📢 <b>Announcement</b>\n\n{text}",
                parse_mode=ParseMode.HTML,
                disable_notification=True,
            )
            sent += 1
        except Exception as e:
            log.debug(f"broadcast to {r['user_id']} failed: {e}")
            failed += 1

    await update.message.reply_html(
        f"✅ <b>Broadcast complete.</b>\n"
        f"Sent: {sent}\nFailed: {failed} <i>(blocked / inactive)</i>"
    )


async def cmd_listusers(update, context):
    """Admin: /listusers — show last 30 users with quick stats."""
    if update.effective_user.id != ADMIN_ID:
        return
    with db() as conn:
        rows = conn.execute("""
            SELECT u.user_id, u.first_name, u.last_name, u.username,
                   COUNT(p.id) as total_purchases,
                   SUM(CASE WHEN p.status='approved' THEN 1 ELSE 0 END) as approved,
                   COALESCE(SUM(CASE WHEN p.status='approved' THEN p.amount ELSE 0 END), 0) as revenue
            FROM users u
            LEFT JOIN purchases p ON p.user_id = u.user_id
            GROUP BY u.user_id
            ORDER BY u.created_at DESC
            LIMIT 30
        """).fetchall()

    if not rows:
        await update.message.reply_text("No users yet.")
        return

    text = f"👥 <b>Recent Users ({len(rows)})</b>\n\n"
    for r in rows:
        nm = f"{r['first_name'] or ''} {r['last_name'] or ''}".strip() or "—"
        un = f"@{r['username']}" if r['username'] else ""
        approved = r['approved'] or 0
        text += (
            f"• <a href='tg://user?id={r['user_id']}'>{nm}</a> {un}\n"
            f"  ID: <code>{r['user_id']}</code> · "
            f"✅{approved} · ₹{r['revenue']}\n"
        )

    # Telegram has a 4096 char message limit; truncate if needed
    if len(text) > 4000:
        text = text[:4000] + "\n\n<i>(truncated)</i>"

    await update.message.reply_html(text, disable_web_page_preview=True)


async def cmd_find(update, context):
    """Admin: /find <text> — search users by name or username."""
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args or []
    if not args:
        await update.message.reply_html(
            "Usage: <code>/find &lt;name or username&gt;</code>"
        )
        return

    query = " ".join(args).lower()
    pattern = f"%{query}%"

    with db() as conn:
        rows = conn.execute("""
            SELECT user_id, first_name, last_name, username
            FROM users
            WHERE LOWER(first_name) LIKE ?
               OR LOWER(last_name) LIKE ?
               OR LOWER(username) LIKE ?
            LIMIT 20
        """, (pattern, pattern, pattern)).fetchall()

    if not rows:
        await update.message.reply_html(
            f"🔍 No users found matching <code>{query}</code>"
        )
        return

    text = f"🔍 <b>Found {len(rows)}:</b>\n\n"
    for r in rows:
        nm = f"{r['first_name'] or ''} {r['last_name'] or ''}".strip() or "—"
        un = f"@{r['username']}" if r['username'] else ""
        text += (
            f"• {nm} {un}\n"
            f"  ID: <code>{r['user_id']}</code> · "
            f"<a href='tg://user?id={r['user_id']}'>open chat</a>\n"
        )

    await update.message.reply_html(text, disable_web_page_preview=True)


async def cmd_help(update, context):
    """Admin: /help — show all admin commands."""
    if update.effective_user.id != ADMIN_ID:
        return
    text = (
        "🛠 <b>Admin Commands</b>\n\n"

        "<b>📊 Stats &amp; reports</b>\n"
        "/stats — totals, revenue, status counts\n"
        "/pending — users awaiting approval\n"
        "/summary — yesterday's full summary + CSV\n"
        "/listusers — last 30 users\n"
        "/find &lt;text&gt; — search by name/username\n"
        "/whoami [id] — DB state for one user\n\n"

        "<b>🧹 Cleanup (single user)</b>\n"
        "/wipe &lt;id&gt; — clear chat, keep purchases\n"
        "/reset &lt;id&gt; — full reset (delete purchases too)\n"
        "/resetme — reset yourself (testing)\n\n"

        "<b>☢️ Cleanup (ALL users)</b>\n"
        "/wipeall YES — clear ALL chats, keep purchases\n"
        "/resetall DELETE-EVERYTHING — nuclear reset\n\n"

        "<b>📢 Communication</b>\n"
        "/broadcast &lt;msg&gt; — send to all users\n"
        "/msg &lt;user_id&gt; [message] — send message to one user or open chat\n\n"

        "<b>💾 DB backup</b>\n"
        "/backup — get fresh DB file\n"
        "/restore — reply to a .db file to restore\n"
        "/import_csv — upload master_summary.csv to import purchase records\n\n"

        "<i>Inline buttons on admin notifications:</i>\n"
        "✅ Approve · ❌ Reject · 📸 Request Screenshot\n"
        "🧹 Wipe (after action) · 🚨 Open User Chat (after reject)"
    )
    await update.message.reply_html(text)


async def cmd_logs(update, context):
    """Admin: /logs <user_id> — show all messages from a user."""
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args or []
    if not args:
        await update.message.reply_html(
            "Usage: <code>/logs &lt;user_id&gt;</code>"
        )
        return
    try:
        user_id = int(args[0])
    except ValueError:
        await update.message.reply_text("Invalid user ID")
        return
    
    with db() as conn:
        msgs = conn.execute("""
            SELECT message_type, content, created_at
            FROM user_messages
            WHERE user_id=?
            ORDER BY created_at DESC
            LIMIT 50
        """, (user_id,)).fetchall()
    
    if not msgs:
        await update.message.reply_text(f"No messages logged for user {user_id}")
        return
    
    text = f"📋 <b>Message Log — User {user_id}</b>\n\n"
    for m in msgs:
        msg_type = m["message_type"]
        content = m["content"]
        ts = m["created_at"]
        # Truncate long content
        if len(content) > 50:
            content = content[:50] + "…"
        text += f"• <b>{msg_type}</b> @ {ts}\n  <code>{content}</code>\n"
    
    if len(text) > 4000:
        text = text[:4000] + "\n\n<i>(truncated)</i>"
    
    await update.message.reply_html(text)


async def cmd_msg(update, context):
    """Admin: /msg <user_id> <message> — send message to user or open chat."""
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args or []
    if not args:
        await update.message.reply_html(
            "Usage: <code>/msg &lt;user_id&gt; &lt;message&gt;</code> or <code>/msg &lt;user_id&gt;</code> to open chat"
        )
        return
    
    try:
        user_id = int(args[0])
    except ValueError:
        await update.message.reply_text("Invalid user ID")
        return
    
    # If only user_id provided, open direct chat link
    if len(args) == 1:
        chat_link = f"tg://user?id={user_id}"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("💬 Open User Chat", url=chat_link)
        ]])
        await update.message.reply_html(
            f"<a href='{chat_link}'>Click to open chat with user {user_id}</a>\n\n"
            f"Or use: <code>/msg {user_id} your message here</code>",
            reply_markup=kb
        )
        return
    
    # Send message to user
    message_text = " ".join(args[1:])
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"📩 <b>Message from Admin</b>\n\n{message_text}",
            parse_mode=ParseMode.HTML,
            disable_notification=True,
        )
        await update.message.reply_html(f"✅ Message sent to user {user_id}")
    except Exception as e:
        await update.message.reply_html(f"❌ Failed to send message: {str(e)}")


async def cmd_block(update, context):
    """Admin: /block <user_id> [reason] — block a user."""
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args or []
    if not args:
        await update.message.reply_html(
            "Usage: <code>/block &lt;user_id&gt; [reason]</code>"
        )
        return
    try:
        user_id = int(args[0])
    except ValueError:
        await update.message.reply_text("Invalid user ID")
        return
    
    reason = " ".join(args[1:]) if len(args) > 1 else "unspecified"
    block_user(user_id, reason)
    
    await update.message.reply_html(
        f"🚫 User <code>{user_id}</code> blocked.\n"
        f"Reason: <code>{reason}</code>"
    )


async def cmd_unblock(update, context):
    """Admin: /unblock <user_id> — unblock a user."""
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args or []
    if not args:
        await update.message.reply_html(
            "Usage: <code>/unblock &lt;user_id&gt;</code>"
        )
        return
    try:
        user_id = int(args[0])
    except ValueError:
        await update.message.reply_text("Invalid user ID")
        return
    
    unblock_user(user_id)
    
    await update.message.reply_html(
        f"✅ User <code>{user_id}</code> unblocked."
    )


async def cmd_away(update, context):
    """Admin: /away <message> — set away message. /away off to disable."""
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args or []
    
    if not args or (len(args) == 1 and args[0].lower() == "off"):
        # Turn away mode off
        set_admin_setting("away_message", "")
        await update.message.reply_text("✅ Away mode disabled.")
        return
    
    msg = " ".join(args)
    set_admin_setting("away_message", msg)
    
    await update.message.reply_html(
        f"⏰ <b>Away message set:</b>\n\n{msg}\n\n"
        f"<i>Users will see this after submitting payment proof.</i>\n\n"
        f"To disable: <code>/away off</code>"
    )


async def cmd_fallback_toggle(update, context):
    """Admin: /fallback_toggle <on|off> — enable/disable fallback bundle offers
    
    When enabled: Unpaid users see "📦 See Fallback Offers" button
    When disabled: Unpaid users see only primary offer (no fallback bundles)
    
    Examples:
    /fallback_toggle on       → Enable fallback bundles
    /fallback_toggle off      → Disable fallback bundles
    /fallback_toggle status   → Check current status
    
    Use case: Run campaigns with fallback enabled, then disable temporarily
    """
    if update.effective_user.id != ADMIN_ID:
        return
    
    args = context.args or []
    if not args:
        await update.message.reply_html(
            "Usage: <code>/fallback_toggle &lt;on|off|status&gt;</code>\n\n"
            "<code>/fallback_toggle on</code> — Enable fallback bundles\n"
            "<code>/fallback_toggle off</code> — Disable fallback bundles\n"
            "<code>/fallback_toggle status</code> — Check status"
        )
        return
    
    action = args[0].lower()
    
    if action == "status":
        enabled = is_fallback_enabled()
        status = "✅ ENABLED" if enabled else "🚫 DISABLED"
        await update.message.reply_html(
            f"<b>Fallback Bundles:</b> {status}\n\n"
            f"<i>Unpaid users can see budget-friendly options: ₹30, ₹59, ₹79, ₹99</i>"
        )
        return
    
    if action not in ["on", "off"]:
        await update.message.reply_text("Use: on, off, or status")
        return
    
    enabled = action == "on"
    set_admin_setting("fallback_enabled", "true" if enabled else "false")
    
    status = "✅ ENABLED" if enabled else "🚫 DISABLED"
    msg = (
        f"<b>Fallback Bundles:</b> {status}\n\n"
        f"<i>Next /start by unpaid users will reflect this change.</i>"
    )
    await update.message.reply_html(msg)


async def cmd_special_offers_toggle(update, context):
    """Admin: /special_offers_toggle <on|off> — enable/disable special offers
    
    When enabled: You can send targeted offers to specific users
    When disabled: Offer commands are disabled
    
    Examples:
    /special_offers_toggle on       → Enable special offers
    /special_offers_toggle off      → Disable special offers
    /special_offers_toggle status   → Check current status
    
    Related commands when enabled:
    /promo_set <ch> <seg> <price>  — Set promotion for segment
    /promo_personal <id> <ch> <pr> — Send special offer to user
    /offer_tier <seg> <ch> <pr> CONFIRM — Bulk offer to segment
    """
    if update.effective_user.id != ADMIN_ID:
        return
    
    args = context.args or []
    if not args:
        await update.message.reply_html(
            "Usage: <code>/special_offers_toggle &lt;on|off|status&gt;</code>\n\n"
            "<code>/special_offers_toggle on</code> — Enable special offers\n"
            "<code>/special_offers_toggle off</code> — Disable special offers\n"
            "<code>/special_offers_toggle status</code> — Check status"
        )
        return
    
    action = args[0].lower()
    
    if action == "status":
        enabled = is_special_offers_enabled()
        status = "✅ ENABLED" if enabled else "🚫 DISABLED"
        await update.message.reply_html(
            f"<b>Special Offers:</b> {status}\n\n"
            f"<i>Use promotion commands to send targeted offers to users.</i>"
        )
        return
    
    if action not in ["on", "off"]:
        await update.message.reply_text("Use: on, off, or status")
        return
    
    enabled = action == "on"
    set_admin_setting("special_offers_enabled", "true" if enabled else "false")
    
    status = "✅ ENABLED" if enabled else "🚫 DISABLED"
    msg = (
        f"<b>Special Offers:</b> {status}\n\n"
        f"<i>Promotion commands are now {('available' if enabled else 'disabled')}.</i>"
    )
    await update.message.reply_html(msg)


async def cmd_show_channels(update, context):
    """Admin: /show_channels <channel_id> <segment> <visible>
    
    Control which channels are visible for which user segments.
    
    Examples:
    /show_channels 1 all 1           → Show Channel 1 to all users
    /show_channels 2 unpaid 1        → Show Channel 2 to unpaid users only
    /show_channels 3 unpaid 0        → Hide Channel 3 from unpaid users
    /show_channels 2 T1 1            → Show Channel 2 to Tier 1 owners
    
    Segments: all, unpaid, T1, T1,T2, T1,T2,T3, etc.
    Visible: 1 = show, 0 = hide
    
    Use /show_channels_status to see current configuration.
    """
    if update.effective_user.id != ADMIN_ID:
        return
    
    args = context.args or []
    if len(args) < 3:
        await update.message.reply_html(
            "Usage: <code>/show_channels &lt;channel_id&gt; &lt;segment&gt; &lt;visible&gt;</code>\n\n"
            "Example: <code>/show_channels 1 all 1</code>\n"
            "Example: <code>/show_channels 2 unpaid 0</code>\n\n"
            "Segments: all, unpaid, T1, T1,T2, etc.\n"
            "Visible: 1 = show, 0 = hide"
        )
        return
    
    try:
        channel_id = int(args[0])
        segment = args[1].lower()
        is_visible = int(args[2])
    except ValueError:
        await update.message.reply_text("Invalid format")
        return
    
    # Validate channel exists
    if not any(c["id"] == channel_id for c in CHANNELS):
        await update.message.reply_text(f"Channel {channel_id} not found")
        return
    
    set_channel_visibility(channel_id, segment, is_visible)
    
    ch_name = next(c["name"] for c in CHANNELS if c["id"] == channel_id)
    status = "✅ SHOW" if is_visible else "🚫 HIDE"
    
    await update.message.reply_html(
        f"{status}\n\n"
        f"<b>Channel:</b> {ch_name} (ID {channel_id})\n"
        f"<b>Segment:</b> {segment}\n\n"
        f"Tip: Use <code>/show_channels_status</code> to see all rules."
    )


async def cmd_show_channels_status(update, context):
    """Admin: /show_channels_status — show all channel visibility rules"""
    if update.effective_user.id != ADMIN_ID:
        return
    
    with db() as conn:
        rules = conn.execute("""
            SELECT channel_id, segment, is_visible FROM channel_visibility
            ORDER BY channel_id, segment
        """).fetchall()
    
    if not rules:
        await update.message.reply_text("No visibility rules configured (all channels show by default)")
        return
    
    text = f"📋 <b>Channel Visibility Rules</b>\n\n"
    
    by_ch = {}
    for r in rules:
        ch_id = r["channel_id"]
        if ch_id not in by_ch:
            by_ch[ch_id] = []
        by_ch[ch_id].append(r)
    
    for ch_id in sorted(by_ch.keys()):
        ch_name = next((c["name"] for c in CHANNELS if c["id"] == ch_id), f"Channel {ch_id}")
        text += f"<b>Channel {ch_id}: {ch_name}</b>\n"
        for r in by_ch[ch_id]:
            status = "✅" if r["is_visible"] else "🚫"
            text += f"  {status} {r['segment']}\n"
        text += "\n"
    
    if len(text) > 4000:
        text = text[:4000]
    
    await update.message.reply_html(text)


async def cmd_channel_price(update, context):
    """Admin: /channel_price <channel_id> <segment> <price>
    
    Set custom price for a channel in a specific segment.
    
    Examples:
    /channel_price 1 unpaid 150     → Show Channel 1 at ₹150 to unpaid users
    /channel_price 2 T1 299         → Show Channel 2 at ₹299 to Tier 1 owners
    /channel_price 3 all 499        → Show Channel 3 at ₹499 to everyone
    
    Use /channel_price_status to see all custom prices.
    """
    if update.effective_user.id != ADMIN_ID:
        return
    
    args = context.args or []
    if len(args) < 3:
        await update.message.reply_html(
            "Usage: <code>/channel_price &lt;channel_id&gt; &lt;segment&gt; &lt;price&gt;</code>\n\n"
            "Examples:\n"
            "<code>/channel_price 1 unpaid 150</code>\n"
            "<code>/channel_price 2 T1 299</code>\n"
            "<code>/channel_price 3 all 499</code>"
        )
        return
    
    try:
        channel_id = int(args[0])
        segment = args[1].lower()
        price = int(args[2])
    except ValueError:
        await update.message.reply_text("Invalid format. Channel ID and price must be numbers.")
        return
    
    if price < 1:
        await update.message.reply_text("Price must be positive")
        return
    
    # Validate channel exists
    channel = next((c for c in CHANNELS if c["id"] == channel_id), None)
    if not channel:
        await update.message.reply_text(f"Channel {channel_id} not found")
        return
    
    set_custom_price(channel_id, segment, price)
    default_price = channel["price"]
    
    await update.message.reply_html(
        f"💰 <b>Custom Price Set</b>\n\n"
        f"<b>Channel:</b> {channel['name']} (ID {channel_id})\n"
        f"<b>Segment:</b> {segment}\n"
        f"<b>Default price:</b> ₹{default_price}\n"
        f"<b>Custom price:</b> ₹{price}\n\n"
        f"Tip: Use <code>/channel_price_status</code> to see all custom prices."
    )


async def cmd_channel_price_status(update, context):
    """Admin: /channel_price_status — show all custom prices per segment"""
    if update.effective_user.id != ADMIN_ID:
        return
    
    with db() as conn:
        rules = conn.execute("""
            SELECT channel_id, segment, custom_price FROM channel_visibility
            WHERE custom_price IS NOT NULL
            ORDER BY channel_id, segment
        """).fetchall()
    
    if not rules:
        await update.message.reply_text("No custom prices configured (using default channel prices)")
        return
    
    text = f"💰 <b>Custom Prices Configuration</b>\n\n"
    
    by_ch = {}
    for r in rules:
        ch_id = r["channel_id"]
        if ch_id not in by_ch:
            by_ch[ch_id] = []
        by_ch[ch_id].append(r)
    
    for ch_id in sorted(by_ch.keys()):
        ch = next((c for c in CHANNELS if c["id"] == ch_id), None)
        if not ch:
            continue
        ch_name = ch["name"]
        default_price = ch["price"]
        text += f"<b>Channel {ch_id}: {ch_name}</b> (default: ₹{default_price})\n"
        for r in by_ch[ch_id]:
            custom = r["custom_price"]
            diff = custom - default_price
            diff_str = f"(+₹{diff})" if diff > 0 else f"(-₹{abs(diff)})"
            text += f"  • {r['segment']}: <b>₹{custom}</b> {diff_str}\n"
        text += "\n"
    
    if len(text) > 4000:
        text = text[:4000]
    
    await update.message.reply_html(text)


async def cmd_promo_set(update, context):
    """Admin: /promo_set <channel_id> <segment> <price>
    
    Set active promotion for a channel+segment. Overrides custom & default prices.
    Shows ONLY final price in /start menu and offer messages.
    
    Examples:
    /promo_set 1 unpaid 99      → Unpaid users see ₹99 (ONLY ₹99, no original price)
    /promo_set 2 T1 199         → T1 owners see ₹199
    /promo_set 3 all 299        → Everyone sees ₹299
    
    Use /promo_status to see all active promotions.
    """
    if update.effective_user.id != ADMIN_ID:
        return
    
    # Check if special offers are enabled
    if not is_special_offers_enabled():
        await update.message.reply_html(
            "❌ <b>Special Offers Disabled</b>\n\n"
            "Enable with: <code>/special_offers_toggle on</code>"
        )
        return
    
    args = context.args or []
    if len(args) < 3:
        await update.message.reply_html(
            "Usage: <code>/promo_set &lt;channel_id&gt; &lt;segment&gt; &lt;price&gt;</code>\n\n"
            "Examples:\n"
            "<code>/promo_set 1 unpaid 99</code>\n"
            "<code>/promo_set 2 T1 199</code>\n"
            "<code>/promo_set 3 all 299</code>"
        )
        return
    
    try:
        channel_id = int(args[0])
        segment = args[1].lower()
        promotion_price = int(args[2])
    except ValueError:
        await update.message.reply_text("Invalid format. Channel ID and price must be numbers.")
        return
    
    if promotion_price < 1:
        await update.message.reply_text("Price must be positive")
        return
    
    # Validate channel exists
    channel = next((c for c in CHANNELS if c["id"] == channel_id), None)
    if not channel:
        await update.message.reply_text(f"Channel {channel_id} not found")
        return
    
    set_active_promotion(channel_id, segment, promotion_price)
    default_price = channel["price"]
    
    await update.message.reply_html(
        f"🎉 <b>PROMOTION ACTIVE</b>\n\n"
        f"<b>Channel:</b> {channel['name']} (ID {channel_id})\n"
        f"<b>Segment:</b> {segment}\n"
        f"<b>Promotion Price:</b> ₹{promotion_price}\n"
        f"<b>Default Price:</b> ₹{default_price}\n\n"
        f"✅ Users will see ONLY <b>₹{promotion_price}</b> in menu and offers.\n\n"
        f"Tip: Use <code>/promo_status</code> to see all active promotions."
    )


async def cmd_promo_clear(update, context):
    """Admin: /promo_clear <channel_id> <segment>
    
    Deactivate promotion for a channel+segment.
    Prices revert to: custom (if set) or default.
    """
    if update.effective_user.id != ADMIN_ID:
        return
    
    # Check if special offers are enabled
    if not is_special_offers_enabled():
        await update.message.reply_html(
            "❌ <b>Special Offers Disabled</b>\n\n"
            "Enable with: <code>/special_offers_toggle on</code>"
        )
        return
    
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_html(
            "Usage: <code>/promo_clear &lt;channel_id&gt; &lt;segment&gt;</code>\n\n"
            "Example: <code>/promo_clear 1 unpaid</code>"
        )
        return
    
    try:
        channel_id = int(args[0])
        segment = args[1].lower()
    except ValueError:
        await update.message.reply_text("Invalid format")
        return
    
    channel = next((c for c in CHANNELS if c["id"] == channel_id), None)
    if not channel:
        await update.message.reply_text(f"Channel {channel_id} not found")
        return
    
    clear_active_promotion(channel_id, segment)
    
    await update.message.reply_html(
        f"❌ <b>PROMOTION CLEARED</b>\n\n"
        f"<b>Channel:</b> {channel['name']} (ID {channel_id})\n"
        f"<b>Segment:</b> {segment}\n\n"
        f"Price reverted to custom/default."
    )


async def cmd_promo_status(update, context):
    """Admin: /promo_status — show all active promotions"""
    if update.effective_user.id != ADMIN_ID:
        return
    
    with db() as conn:
        promos = conn.execute("""
            SELECT channel_id, segment, promotion_price FROM active_promotions
            WHERE is_active=1
            ORDER BY channel_id, segment
        """).fetchall()
    
    if not promos:
        await update.message.reply_text("No active promotions (using custom/default prices)")
        return
    
    text = f"🎉 <b>ACTIVE PROMOTIONS</b>\n\n"
    
    by_ch = {}
    for p in promos:
        ch_id = p["channel_id"]
        if ch_id not in by_ch:
            by_ch[ch_id] = []
        by_ch[ch_id].append(p)
    
    for ch_id in sorted(by_ch.keys()):
        ch = next((c for c in CHANNELS if c["id"] == ch_id), None)
        if not ch:
            continue
        ch_name = ch["name"]
        text += f"<b>Channel {ch_id}: {ch_name}</b>\n"
        for p in by_ch[ch_id]:
            text += f"  • {p['segment']}: <b>₹{p['promotion_price']}</b>\n"
        text += "\n"
    
    if len(text) > 4000:
        text = text[:4000]
    
    await update.message.reply_html(text)


async def cmd_promo_send(update, context):
    """Admin: /promo_send <channel_id> <segment> CONFIRM
    
    Send active promotion offer to all users in segment.
    Uses the promotion price set with /promo_set.
    
    Example:
    /promo_send 1 unpaid CONFIRM
    → Sends: "🎉 Master Pack now ₹99" to all unpaid users
    """
    if update.effective_user.id != ADMIN_ID:
        return
    
    # Check if special offers are enabled
    if not is_special_offers_enabled():
        await update.message.reply_html(
            "❌ <b>Special Offers Disabled</b>\n\n"
            "Enable with: <code>/special_offers_toggle on</code>"
        )
        return
    
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_html(
            "Usage: <code>/promo_send &lt;channel_id&gt; &lt;segment&gt; [CONFIRM]</code>\n\n"
            "Example: <code>/promo_send 1 unpaid CONFIRM</code>"
        )
        return
    
    try:
        channel_id = int(args[0])
        segment = args[1].lower()
    except ValueError:
        await update.message.reply_text("Invalid format")
        return
    
    channel = next((c for c in CHANNELS if c["id"] == channel_id), None)
    if not channel:
        await update.message.reply_text(f"Channel {channel_id} not found")
        return
    
    # Get active promotion price
    promo_price = get_active_promotion_price(channel_id, segment)
    if not promo_price:
        await update.message.reply_text(
            f"No active promotion for Channel {channel_id}, {segment}.\n"
            f"Set one first: /promo_set {channel_id} {segment} <price>"
        )
        return
    
    # Get target users
    with db() as conn:
        all_users = conn.execute("SELECT user_id FROM users").fetchall()
        purchases = conn.execute(
            "SELECT user_id, channel_id FROM purchases WHERE status='approved'"
        ).fetchall()
    
    # Build tier map
    tier_map = {}
    for p in purchases:
        uid = p['user_id']
        if uid not in tier_map:
            tier_map[uid] = set()
        tier_map[uid].add(p['channel_id'])
    
    # Select users based on segment
    target_users = []
    if segment == "unpaid":
        target_users = [u['user_id'] for u in all_users if u['user_id'] not in tier_map]
    elif segment == "all":
        target_users = [u['user_id'] for u in all_users]
    else:
        # Parse T1,T2,T3 format
        try:
            required_tiers = {int(t.replace("T", "")) for t in segment.split(",")}
            for u in all_users:
                uid = u['user_id']
                owned = tier_map.get(uid, set())
                if any(t in owned for t in required_tiers):
                    target_users.append(uid)
        except ValueError:
            await update.message.reply_text("Invalid segment format")
            return
    
    if not target_users:
        await update.message.reply_text(f"No users found in segment '{segment}'")
        return
    
    # Confirmation
    if len(args) < 3 or args[2].upper() != "CONFIRM":
        await update.message.reply_html(
            f"⚠️ About to send promotion to <b>{len(target_users)}</b> users.\n\n"
            f"<b>Channel:</b> {channel['name']}\n"
            f"<b>Segment:</b> {segment}\n"
            f"<b>Price:</b> ₹{promo_price}\n\n"
            f"To confirm: <code>/promo_send {channel_id} {segment} CONFIRM</code>"
        )
        return
    
    await update.message.reply_text(f"📢 Sending promotion to {len(target_users)} users…")
    
    # Send offer (only final price, no original)
    offer_msg = (
        f"🎉 <b>SPECIAL PROMOTION</b>\n\n"
        f"<b>{channel['name']}</b>\n"
        f"<b>₹{promo_price}</b>\n\n"
        f"Limited time! Send /start to claim."
    )
    
    sent = 0
    failed = 0
    for uid in target_users:
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=offer_msg,
                parse_mode=ParseMode.HTML,
                disable_notification=True,
            )
            log_user_message(uid, "promo_sent", f"Ch{channel_id} ₹{promo_price}")
            sent += 1
        except Exception as e:
            log.debug(f"promo to {uid} failed: {e}")
            failed += 1
    
    await update.message.reply_html(
        f"✅ <b>Promotion sent.</b>\n"
        f"Sent: {sent}\nFailed/blocked: {failed}"
    )


async def cmd_promo_personal(update, context):
    """Admin: /promo_personal <user_id> <channel_id> <price>
    
    Send individual promotional offer to one user.
    Shows ONLY final price (no original → discount message).
    """
    if update.effective_user.id != ADMIN_ID:
        return
    
    # Check if special offers are enabled
    if not is_special_offers_enabled():
        await update.message.reply_html(
            "❌ <b>Special Offers Disabled</b>\n\n"
            "Enable with: <code>/special_offers_toggle on</code>"
        )
        return
    
    args = context.args or []
    if len(args) < 3:
        await update.message.reply_html(
            "Usage: <code>/promo_personal &lt;user_id&gt; &lt;channel_id&gt; &lt;price&gt;</code>\n\n"
            "Example: <code>/promo_personal 123456789 1 99</code>"
        )
        return
    
    try:
        user_id = int(args[0])
        channel_id = int(args[1])
        promo_price = int(args[2])
    except ValueError:
        await update.message.reply_text("Invalid format")
        return
    
    if promo_price < 1:
        await update.message.reply_text("Price must be positive")
        return
    
    channel = next((c for c in CHANNELS if c["id"] == channel_id), None)
    if not channel:
        await update.message.reply_text(f"Channel {channel_id} not found")
        return
    
    # Get user info
    with db() as conn:
        u = conn.execute("SELECT * FROM users WHERE user_id=?",
                        (user_id,)).fetchone()
    
    if not u:
        await update.message.reply_text(f"User {user_id} not found")
        return
    
    nm = f"{u['first_name'] or ''} {u['last_name'] or ''}".strip() or f"ID {user_id}"
    
    # Send offer (only final price)
    offer_msg = (
        f"🎉 <b>EXCLUSIVE OFFER FOR YOU</b>\n\n"
        f"<b>{channel['name']}</b>\n"
        f"<b>₹{promo_price}</b>\n\n"
        f"This special price is just for you! Send /start to claim."
    )
    
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=offer_msg,
            parse_mode=ParseMode.HTML,
            disable_notification=True,
        )
        log_user_message(user_id, "promo_personal_sent", f"Ch{channel_id} ₹{promo_price}")
        
        await update.message.reply_html(
            f"✅ Promotion sent to {nm}.\n"
            f"Channel: {channel['name']}\n"
            f"Price: ₹{promo_price}"
        )
    except Exception as e:
        await update.message.reply_html(f"❌ Failed to send: {e}")


async def cmd_bulk_ids(update, context):
    """Admin: /bulk_ids <segment>
    
    Get copy-ready User ID list for any segment.
    Perfect for /bulk_promo_users command.
    
    Examples:
    /bulk_ids unpaid      → All unpaid user IDs (comma-separated)
    /bulk_ids T1          → All Tier 1 user IDs
    /bulk_ids T1,T2       → All T1 or T2 user IDs
    /bulk_ids all         → All user IDs
    
    Output: 123456,789123,456789 (ready to paste)
    """
    if update.effective_user.id != ADMIN_ID:
        return
    
    args = context.args or []
    if not args:
        await update.message.reply_html(
            "Usage: <code>/bulk_ids &lt;segment&gt;</code>\n\n"
            "Segments:\n"
            "  unpaid - all users with no purchases\n"
            "  T1 - Tier 1 only\n"
            "  T1,T2 - Tier 1 or 2\n"
            "  all - everyone\n\n"
            "Output: Comma-separated IDs ready for /bulk_promo_users"
        )
        return
    
    segment = args[0].lower()
    
    with db() as conn:
        all_users = conn.execute("SELECT user_id FROM users ORDER BY user_id").fetchall()
        purchases = conn.execute(
            "SELECT user_id, channel_id FROM purchases WHERE status='approved'"
        ).fetchall()
    
    # Build tier map
    tier_map = {}
    for p in purchases:
        uid = p['user_id']
        if uid not in tier_map:
            tier_map[uid] = set()
        tier_map[uid].add(p['channel_id'])
    
    # Select users
    target_ids = []
    if segment == "unpaid":
        target_ids = [u['user_id'] for u in all_users if u['user_id'] not in tier_map]
    elif segment == "all":
        target_ids = [u['user_id'] for u in all_users]
    else:
        # Parse T1,T2,T3 format
        try:
            required_tiers = {int(t.replace("T", "")) for t in segment.split(",")}
            for u in all_users:
                uid = u['user_id']
                owned = tier_map.get(uid, set())
                if any(t in owned for t in required_tiers):
                    target_ids.append(uid)
        except ValueError:
            await update.message.reply_text("Invalid segment format")
            return
    
    if not target_ids:
        await update.message.reply_text(f"No users found in segment '{segment}'")
        return
    
    # Format as comma-separated IDs
    ids_str = ",".join(str(uid) for uid in target_ids)
    
    await update.message.reply_html(
        f"<b>User IDs for segment: {segment}</b>\n"
        f"Total: {len(target_ids)} users\n\n"
        f"<code>{ids_str}</code>\n\n"
        f"Ready for: <code>/bulk_promo_users {ids_str} &lt;channel&gt; &lt;price&gt; CONFIRM</code>"
    )


async def cmd_bulk_promo_users(update, context):
    """Admin: /bulk_promo_users <user_ids> <channel_id> <price>
    
    Send promotion to MULTIPLE specific users by their User IDs.
    Comma-separated list of IDs. Works even if users are inactive.
    
    Examples:
    /bulk_promo_users 123456,789123,456789 1 99
    /bulk_promo_users 111222,333444 2 249
    
    Get User IDs from: /unpaid (shows all users with their IDs)
    """
    if update.effective_user.id != ADMIN_ID:
        return
    
    args = context.args or []
    if len(args) < 3:
        await update.message.reply_html(
            "Usage: <code>/bulk_promo_users &lt;user_ids&gt; &lt;channel_id&gt; &lt;price&gt;</code>\n\n"
            "Examples:\n"
            "<code>/bulk_promo_users 123456,789123,456789 1 99</code>\n"
            "<code>/bulk_promo_users 111222,333444 2 249</code>\n\n"
            "User IDs: Get from <code>/unpaid</code> (shows all IDs)"
        )
        return
    
    user_ids_str = args[0]
    try:
        channel_id = int(args[1])
        promo_price = int(args[2])
    except ValueError:
        await update.message.reply_text("Invalid format. Channel ID and price must be numbers.")
        return
    
    if promo_price < 1:
        await update.message.reply_text("Price must be positive")
        return
    
    # Parse user IDs
    try:
        user_ids = [int(uid.strip()) for uid in user_ids_str.split(",")]
    except ValueError:
        await update.message.reply_text("Invalid User IDs. Use comma-separated numbers: 123456,789123,456789")
        return
    
    # Validate channel
    channel = next((c for c in CHANNELS if c["id"] == channel_id), None)
    if not channel:
        await update.message.reply_text(f"Channel {channel_id} not found")
        return
    
    # Confirmation
    if len(args) < 4 or args[3].upper() != "CONFIRM":
        await update.message.reply_html(
            f"⚠️ About to send promotion to <b>{len(user_ids)}</b> users.\n\n"
            f"<b>User IDs:</b> {user_ids_str[:100]}{'...' if len(user_ids_str) > 100 else ''}\n"
            f"<b>Channel:</b> {channel['name']}\n"
            f"<b>Price:</b> ₹{promo_price}\n\n"
            f"To confirm: <code>/bulk_promo_users {user_ids_str} {channel_id} {promo_price} CONFIRM</code>"
        )
        return
    
    await update.message.reply_text(f"📢 Sending promotion to {len(user_ids)} users…")
    
    # Send offer (only final price)
    offer_msg = (
        f"🎉 <b>SPECIAL PROMOTION</b>\n\n"
        f"<b>{channel['name']}</b>\n"
        f"<b>₹{promo_price}</b>\n\n"
        f"Limited time! Send /start to claim."
    )
    
    sent = 0
    failed = 0
    not_found = 0
    
    for user_id in user_ids:
        # Check if user exists in DB
        with db() as conn:
            u = conn.execute("SELECT user_id FROM users WHERE user_id=?",
                           (user_id,)).fetchone()
        
        if not u:
            not_found += 1
            continue
        
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=offer_msg,
                parse_mode=ParseMode.HTML,
                disable_notification=True,
            )
            log_user_message(user_id, "bulk_promo_sent", f"Ch{channel_id} ₹{promo_price}")
            sent += 1
        except Exception as e:
            log.debug(f"bulk promo to {user_id} failed: {e}")
            failed += 1
    
    await update.message.reply_html(
        f"✅ <b>Bulk promotion sent.</b>\n"
        f"Sent: {sent}\n"
        f"Failed/blocked: {failed}\n"
        f"Not in DB: {not_found}"
    )


async def cmd_offer_tier(update, context):
    """Admin: /offer_tier <tier> <channel_id> <price>
    
    Send offer to users at specific tier(s).
    
    Examples:
    /offer_tier unpaid 1 150          → all unpaid: Channel 1 at ₹150
    /offer_tier T1 2 250              → all Tier 1 owners: Channel 2 at ₹250
    /offer_tier T1,T2 3 400           → all Tier 1+2 owners: Channel 3 at ₹400
    /offer_tier all 1 99              → everyone: Channel 1 at ₹99
    """
    if update.effective_user.id != ADMIN_ID:
        return
    
    # Check if special offers are enabled
    if not is_special_offers_enabled():
        await update.message.reply_html(
            "❌ <b>Special Offers Disabled</b>\n\n"
            "Enable with: <code>/special_offers_toggle on</code>"
        )
        return
    
    args = context.args or []
    if len(args) < 3:
        await update.message.reply_html(
            "Usage: <code>/offer_tier &lt;tier&gt; &lt;channel_id&gt; &lt;price&gt;</code>\n\n"
            "Tier options:\n"
            "  unpaid - users with no purchases\n"
            "  T1 - Tier 1 only\n"
            "  T1,T2 - Tier 1 or Tier 2\n"
            "  T1,T2,T3 - Tier 1, 2, or 3\n"
            "  all - everyone\n\n"
            "Examples:\n"
            "<code>/offer_tier unpaid 1 150</code>\n"
            "<code>/offer_tier T1 2 250</code>\n"
            "<code>/offer_tier all 1 99</code>"
        )
        return
    
    tier_spec = args[0].lower()
    try:
        channel_id = int(args[1])
        price = int(args[2])
    except ValueError:
        await update.message.reply_text("Invalid channel_id or price")
        return
    
    # Find the channel
    channel = next((c for c in CHANNELS if c["id"] == channel_id), None)
    if not channel:
        await update.message.reply_text(f"Channel {channel_id} not found")
        return
    
    # Get all users matching the tier spec
    with db() as conn:
        all_users = conn.execute("SELECT user_id FROM users").fetchall()
        purchases = conn.execute(
            "SELECT user_id, channel_id FROM purchases WHERE status='approved'"
        ).fetchall()
    
    # Build tier map
    tier_map = {}
    for p in purchases:
        uid = p['user_id']
        if uid not in tier_map:
            tier_map[uid] = set()
        tier_map[uid].add(p['channel_id'])
    
    # Select users based on tier_spec
    target_users = []
    if tier_spec == "unpaid":
        target_users = [u['user_id'] for u in all_users if u['user_id'] not in tier_map]
    elif tier_spec == "all":
        target_users = [u['user_id'] for u in all_users]
    else:
        # Parse T1,T2,T3 format
        try:
            required_tiers = {int(t.replace("T", "")) for t in tier_spec.split(",")}
            for u in all_users:
                uid = u['user_id']
                owned = tier_map.get(uid, set())
                if any(t in owned for t in required_tiers):
                    target_users.append(uid)
        except ValueError:
            await update.message.reply_text("Invalid tier format. Use: unpaid, all, or T1,T2,T3")
            return
    
    if not target_users:
        await update.message.reply_text(f"No users found matching '{tier_spec}'")
        return
    
    # Confirmation
    if len(context.args) < 4 or context.args[3].upper() != "CONFIRM":
        await update.message.reply_html(
            f"⚠️ About to send offer to <b>{len(target_users)}</b> users.\n\n"
            f"<b>Tier:</b> {tier_spec}\n"
            f"<b>Channel:</b> {channel['name']}\n"
            f"<b>Price:</b> ₹{price}\n\n"
            f"To confirm, send: <code>/offer_tier {tier_spec} {channel_id} {price} CONFIRM</code>"
        )
        return
    
    await update.message.reply_text(f"📢 Sending to {len(target_users)} users…")
    
    # Send offer
    offer_msg = (
        f"🎉 <b>SPECIAL OFFER</b>\n\n"
        f"<b>{channel['name']}</b>\n"
        f"<b>₹{price}</b>\n\n"
        f"Limited time! Send /start to claim."
    )
    
    sent = 0
    failed = 0
    for uid in target_users:
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=offer_msg,
                parse_mode=ParseMode.HTML,
                disable_notification=True,
            )
            log_user_message(uid, "offer_sent", f"Ch{channel_id} ₹{price}")
            sent += 1
        except Exception as e:
            log.debug(f"offer to {uid} failed: {e}")
            failed += 1
    
    await update.message.reply_html(
        f"✅ <b>Offers sent.</b>\n"
        f"Sent: {sent}\nFailed/blocked: {failed}"
    )


async def cmd_offer_user(update, context):
    """Admin: /offer_user <user_id> <channel_id> <price>
    
    Send specific offer to ONE user.
    
    Example: /offer_user 123456789 1 150
    """
    if update.effective_user.id != ADMIN_ID:
        return
    
    # Check if special offers are enabled
    if not is_special_offers_enabled():
        await update.message.reply_html(
            "❌ <b>Special Offers Disabled</b>\n\n"
            "Enable with: <code>/special_offers_toggle on</code>"
        )
        return
    
    args = context.args or []
    if len(args) < 3:
        await update.message.reply_html(
            "Usage: <code>/offer_user &lt;user_id&gt; &lt;channel_id&gt; &lt;price&gt;</code>"
        )
        return
    
    try:
        user_id = int(args[0])
        channel_id = int(args[1])
        price = int(args[2])
    except ValueError:
        await update.message.reply_text("Invalid user_id, channel_id, or price")
        return
    
    # Find channel
    channel = next((c for c in CHANNELS if c["id"] == channel_id), None)
    if not channel:
        await update.message.reply_text(f"Channel {channel_id} not found")
        return
    
    # Get user info
    with db() as conn:
        u = conn.execute("SELECT * FROM users WHERE user_id=?",
                        (user_id,)).fetchone()
    
    if not u:
        await update.message.reply_text(f"User {user_id} not found")
        return
    
    nm = f"{u['first_name'] or ''} {u['last_name'] or ''}".strip() or f"ID {user_id}"
    
    # Send offer
    offer_msg = (
        f"🎉 <b>SPECIAL OFFER FOR YOU</b>\n\n"
        f"<b>{channel['name']}</b>\n"
        f"<b>₹{price}</b>\n\n"
        f"Limited time! Send /start to claim."
    )
    
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=offer_msg,
            parse_mode=ParseMode.HTML,
            disable_notification=True,
        )
        log_user_message(user_id, "offer_sent", f"Ch{channel_id} ₹{price}")
        
        await update.message.reply_html(
            f"✅ Offer sent to {nm}.\n"
            f"Channel: {channel['name']}\n"
            f"Price: ₹{price}"
        )
    except Exception as e:
        await update.message.reply_html(
            f"❌ Failed to send: {e}"
        )


async def cmd_offer_users(update, context):
    """Admin: /offer_users <channel_id> <price> <user_id1> [user_id2] [user_id3] ...
    
    Send same offer to multiple specific users.
    
    Example: /offer_users 1 150 123456789 987654321 555666777
    Sends Channel 1 at ₹150 to three users.
    """
    if update.effective_user.id != ADMIN_ID:
        return
    
    # Check if special offers are enabled
    if not is_special_offers_enabled():
        await update.message.reply_html(
            "❌ <b>Special Offers Disabled</b>\n\n"
            "Enable with: <code>/special_offers_toggle on</code>"
        )
        return
    
    args = context.args or []
    if len(args) < 3:
        await update.message.reply_html(
            "Usage: <code>/offer_users &lt;channel_id&gt; &lt;price&gt; &lt;user_id1&gt; [user_id2] ...</code>\n\n"
            "Example: <code>/offer_users 1 150 123456789 987654321 555666777</code>"
        )
        return
    
    try:
        channel_id = int(args[0])
        price = int(args[1])
        user_ids = [int(uid) for uid in args[2:]]
    except ValueError:
        await update.message.reply_text("Invalid format. All IDs and prices must be numbers.")
        return
    
    # Find channel
    channel = next((c for c in CHANNELS if c["id"] == channel_id), None)
    if not channel:
        await update.message.reply_text(f"Channel {channel_id} not found")
        return
    
    # Confirmation
    await update.message.reply_html(
        f"⚠️ About to send offer to <b>{len(user_ids)}</b> users.\n\n"
        f"<b>Channel:</b> {channel['name']}\n"
        f"<b>Price:</b> ₹{price}\n"
        f"<b>Users:</b> {', '.join(str(u) for u in user_ids[:5])}"
        f"{'...' if len(user_ids) > 5 else ''}\n\n"
        f"To confirm, send: <code>/offer_users {channel_id} {price} {' '.join(str(u) for u in user_ids)} CONFIRM</code>"
    )
    
    # Check for CONFIRM
    if not any("CONFIRM" in str(arg).upper() for arg in args):
        return
    
    await update.message.reply_text(f"📢 Sending to {len(user_ids)} users…")
    
    offer_msg = (
        f"🎉 <b>SPECIAL OFFER FOR YOU</b>\n\n"
        f"<b>{channel['name']}</b>\n"
        f"<b>₹{price}</b>\n\n"
        f"Limited time! Send /start to claim."
    )
    
    sent = 0
    failed = 0
    for uid in user_ids:
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=offer_msg,
                parse_mode=ParseMode.HTML,
                disable_notification=True,
            )
            log_user_message(uid, "offer_sent", f"Ch{channel_id} ₹{price}")
            sent += 1
        except Exception as e:
            log.debug(f"offer to {uid} failed: {e}")
            failed += 1
    
    await update.message.reply_html(
        f"✅ <b>Offers sent.</b>\n"
        f"Sent: {sent}\nFailed/blocked: {failed}"
    )


async def cmd_unpaid(update, context):
    """Admin: /unpaid — show ALL users segmented by tier with prominent IDs"""
    if update.effective_user.id != ADMIN_ID:
        return
    
    with db() as conn:
        users = conn.execute("""
            SELECT u.user_id, u.first_name, u.last_name, u.username
            FROM users u
            ORDER BY u.created_at DESC
        """).fetchall()
        
        purchases = conn.execute("""
            SELECT user_id, channel_id, channel_name
            FROM purchases
            WHERE status='approved'
        """).fetchall()
    
    # Build tier map
    tier_map = {}
    for p in purchases:
        uid = p['user_id']
        if uid not in tier_map:
            tier_map[uid] = set()
        tier_map[uid].add(p['channel_id'])
    
    # Segment users
    unpaid = []
    by_tier = {}
    
    for u in users:
        uid = u['user_id']
        owned = tier_map.get(uid, set())
        
        if not owned:
            unpaid.append(u)
        else:
            tier_str = "T" + ",T".join(str(cid) for cid in sorted(owned))
            if tier_str not in by_tier:
                by_tier[tier_str] = []
            by_tier[tier_str].append(u)
    
    # Build report with prominent User IDs
    text = f"📊 <b>User Segmentation Report</b>\n\n"
    
    # UNPAID section — IDs first
    text += f"🔴 <b>UNPAID ({len(unpaid)} users)</b>\n"
    if unpaid:
        for u in unpaid:
            nm = f"{u['first_name'] or ''} {u['last_name'] or ''}".strip() or "—"
            un = f"@{u['username']}" if u['username'] else "(no username)"
            text += f"<code>{u['user_id']}</code> — {nm} {un}\n"
    else:
        text += "✅ No unpaid users!\n"
    text += "\n"
    
    # By tier section
    for tier_str in sorted(by_tier.keys()):
        users_list = by_tier[tier_str]
        text += f"🟢 <b>{tier_str} ({len(users_list)} users)</b>\n"
        for u in users_list:
            nm = f"{u['first_name'] or ''} {u['last_name'] or ''}".strip() or "—"
            un = f"@{u['username']}" if u['username'] else "(no username)"
            text += f"<code>{u['user_id']}</code> — {nm} {un}\n"
        text += "\n"
    
    # Commands reference
    text += f"<b>💼 Run Offers:</b>\n"
    text += f"<code>/offer_tier unpaid 1 150 CONFIRM</code>\n"
    text += f"<code>/offer_tier T1 2 250 CONFIRM</code>\n"
    text += f"<code>/offer_user 123456789 1 120</code>\n"
    text += f"<code>/bulk_promo_users 123456,789123,456789 1 99 CONFIRM</code>\n"
    text += f"<code>/show_channels 1 all 1</code> (show Ch 1 for all)\n"
    
    if len(text) > 4000:
        text = text[:4000] + "\n\n<i>(output truncated — use /bulk_ids command)</i>"
    
    await update.message.reply_html(text, disable_web_page_preview=True)
    
    # Also send CSV for easy copy-paste/import
    csv_buffer = io.StringIO()
    csv_writer = csv.writer(csv_buffer)
    csv_writer.writerow(["User ID", "Name", "Username", "Tier"])
    
    for u in unpaid:
        nm = f"{u['first_name'] or ''} {u['last_name'] or ''}".strip()
        csv_writer.writerow([u['user_id'], nm, u['username'] or "", "unpaid"])
    
    for tier_str in sorted(by_tier.keys()):
        for u in by_tier[tier_str]:
            nm = f"{u['first_name'] or ''} {u['last_name'] or ''}".strip()
            csv_writer.writerow([u['user_id'], nm, u['username'] or "", tier_str])
    
    csv_file = InputFile(
        BytesIO(csv_buffer.getvalue().encode()),
        filename="user_segments.csv"
    )
    try:
        await context.bot.send_document(
            chat_id=ADMIN_ID,
            document=csv_file,
            caption="User segmentation CSV for import/analysis"
        )
    except Exception as e:
        log.debug(f"CSV send failed: {e}")


async def cmd_whoami(update, context):
    """Admin: /whoami [user_id] — show DB state for a user (or yourself).
    Useful for debugging 'bot forgot me' issues."""
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args or []
    target_id = int(args[0]) if args else update.effective_user.id

    with db() as conn:
        u = conn.execute("SELECT * FROM users WHERE user_id=?",
                         (target_id,)).fetchone()
        purchases = conn.execute(
            "SELECT id, channel_id, channel_name, amount, status, "
            "created_at, approved_at "
            "FROM purchases WHERE user_id=? ORDER BY id DESC", (target_id,)
        ).fetchall()

    if not u:
        await update.message.reply_html(
            f"❌ User <code>{target_id}</code> not in DB."
        )
        return

    name = f"{u['first_name'] or ''} {u['last_name'] or ''}".strip() or "—"
    text = (
        f"🔍 <b>User <code>{target_id}</code></b>\n"
        f"Name: {name}\n"
        f"Created: {u['created_at']}\n\n"
        f"<b>Purchases ({len(purchases)}):</b>\n"
    )
    if not purchases:
        text += "<i>None</i>\n"
    else:
        for p in purchases:
            mark = "✅" if p["status"] == "approved" else "•"
            text += (
                f"{mark} #{p['id']} {p['channel_name']} ₹{p['amount']} "
                f"<b>{p['status']}</b>\n"
                f"   created: {p['created_at']}\n"
            )
            if p["approved_at"]:
                text += f"   approved: {p['approved_at']}\n"

    text += f"\n<b>has_paid_tier1:</b> {has_paid_tier1(target_id)}"
    text += f"\n<b>owned_channel_ids:</b> {sorted(get_owned_channel_ids(target_id))}"

    await update.message.reply_html(text)

async def cmd_resetme(update, context):
    """Admin convenience: full reset of MY OWN account for testing."""
    if update.effective_user.id != ADMIN_ID:
        return
    me = update.effective_user.id
    await full_reset(context, me)
    await update.message.reply_text(
        "✅ Full reset complete. Send /start to begin again."
    )

async def cmd_wipe(update, context):
    """Admin: /wipe <user_id> — alias for /reset, same full nuclear reset."""
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: <code>/wipe &lt;user_id&gt;</code> "
            "(same as /reset — full clean)",
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Invalid user_id. Must be a number.")
        return
    await full_reset(context, target_id)
    await update.message.reply_html(
        f"🧹 Wiped user <code>{target_id}</code> completely."
    )

# ==================================================================
# DB BACKUP / RESTORE — survives Railway redeploys without a volume.
# ==================================================================
async def cmd_backup(update, context):
    """Admin: /backup — sends current bot.db to your DM."""
    if update.effective_user.id != ADMIN_ID:
        return
    await send_db_backup(context, manual=True)

async def send_db_backup(context, manual=False):
    """Send bot.db file to admin chat. Runs daily + on /backup + on key events."""
    if not os.path.exists(DB_PATH):
        if manual:
            try:
                await context.bot.send_message(
                    ADMIN_ID, "⚠️ No DB file found at " + DB_PATH
                )
            except Exception: pass
        return
    try:
        with open(DB_PATH, "rb") as fh:
            stamp = datetime.now(TZ).strftime("%Y%m%d_%H%M")
            await context.bot.send_document(
                chat_id=ADMIN_ID,
                document=InputFile(fh, filename=f"bot_backup_{stamp}.db"),
                caption=(f"💾 DB backup — {stamp}"
                         + ("\n<i>(manual)</i>" if manual else "\n<i>(auto)</i>")),
                parse_mode=ParseMode.HTML,
                disable_notification=True,
            )
    except Exception as e:
        log.error(f"DB backup send failed: {e}")

async def event_backup(context):
    """Silent backup triggered after key events (approve/reject).
    Sends DB file to admin without notification or fanfare."""
    if not os.path.exists(DB_PATH):
        return
    try:
        with open(DB_PATH, "rb") as fh:
            stamp = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")
            await context.bot.send_document(
                chat_id=ADMIN_ID,
                document=InputFile(fh, filename=f"bot_backup_{stamp}.db"),
                caption=f"💾 Auto-backup ({stamp})",
                disable_notification=True,
            )
    except Exception as e:
        log.error(f"event_backup failed: {e}")

async def cmd_import_csv(update, context):
    """Admin: Upload CSV file to import purchase records into database."""
    if update.effective_user.id != ADMIN_ID:
        return
    
    await update.message.reply_html(
        "📤 <b>CSV Import Ready</b>\n\n"
        "Reply with <code>master_summary.csv</code> file\n\n"
        "Expected columns:\n"
        "PurchaseID, CreatedAt, UserID, Name, Username, Channel, Amount, QR, UPIName, Status, ApprovedAt, RejectedAt\n\n"
        "<i>File will be parsed and imported into database</i>"
    )


async def on_csv_import_file(update, context):
    """Handle CSV file upload for import."""
    user = update.effective_user
    
    if user.id != ADMIN_ID:
        return
    
    doc = update.message.document
    if not doc or not doc.file_name.endswith('.csv'):
        await update.message.reply_text("❌ Please upload a CSV file (.csv)")
        return
    
    try:
        # Download file
        file = await context.bot.get_file(doc.file_id)
        file_bytes = await file.download_as_bytearray()
        file_content = file_bytes.decode('utf-8-sig')
        
        # Parse CSV
        csv_reader = csv.DictReader(io.StringIO(file_content))
        rows = list(csv_reader)
        
        if not rows:
            await update.message.reply_text("❌ CSV file is empty!")
            return
        
        # Import records
        imported = 0
        skipped = 0
        errors = []
        
        with db() as conn:
            for row in rows:
                try:
                    # Extract fields
                    purchase_id = int(row.get('PurchaseID', 0))
                    created_at = row.get('CreatedAt', '')
                    user_id = int(row.get('UserID', 0))
                    name = row.get('Name', '')
                    username = row.get('Username', '')
                    channel_name = row.get('Channel', '')
                    amount = int(row.get('Amount', 0))
                    qr = row.get('QR', '')
                    upi_name = row.get('UPIName', '')
                    status = row.get('Status', 'pending')
                    approved_at = row.get('ApprovedAt', None)
                    rejected_at = row.get('RejectedAt', None)
                    
                    if not user_id or not channel_name or not amount:
                        skipped += 1
                        continue
                    
                    # Check if record already exists
                    existing = conn.execute(
                        "SELECT id FROM purchases WHERE id=?",
                        (purchase_id,)
                    ).fetchone()
                    
                    if existing:
                        skipped += 1
                        continue
                    
                    # Determine channel_id from channel name
                    channel_id = None
                    for c in CHANNELS:
                        if c['name'] == channel_name:
                            channel_id = c['id']
                            break
                    
                    # Insert record
                    conn.execute("""
                        INSERT INTO purchases 
                        (id, user_id, channel_id, channel_name, amount, qr_used, upi_name, 
                         status, created_at, approved_at, rejected_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (purchase_id, user_id, channel_id or 0, channel_name, amount, 
                          qr, upi_name, status, created_at, approved_at, rejected_at))
                    
                    # Also ensure user exists
                    conn.execute("""
                        INSERT OR IGNORE INTO users (user_id, first_name, username)
                        VALUES (?, ?, ?)
                    """, (user_id, name.split()[0] if name else "User", username))
                    
                    imported += 1
                    
                except Exception as e:
                    errors.append(f"Row {purchase_id}: {str(e)}")
                    continue
            
            conn.commit()
        
        # Send summary
        summary = (
            f"✅ <b>CSV Import Complete</b>\n\n"
            f"📊 <b>Results:</b>\n"
            f"✅ Imported: {imported} records\n"
            f"⏭️ Skipped: {skipped} records (duplicates/invalid)\n"
        )
        
        if errors:
            summary += f"\n❌ Errors: {len(errors)}\n"
            for err in errors[:5]:
                summary += f"  • {err}\n"
            if len(errors) > 5:
                summary += f"  ... and {len(errors)-5} more"
        
        await update.message.reply_html(summary)
        
        # Notify in logs
        log.info(f"CSV Import: {imported} imported, {skipped} skipped, {len(errors)} errors")
        
    except Exception as e:
        await update.message.reply_html(f"❌ Error processing CSV:\n<code>{str(e)}</code>")
        log.error(f"CSV import error: {e}")


async def cmd_restore(update, context):
    """Admin: reply to a backup .db file with /restore — replaces current DB."""
    if update.effective_user.id != ADMIN_ID:
        return
    msg = update.message
    if not msg.reply_to_message or not msg.reply_to_message.document:
        await msg.reply_text(
            "Reply to a backup .db file with /restore to restore it."
        )
        return
    doc = msg.reply_to_message.document
    if not doc.file_name.endswith(".db"):
        await msg.reply_text("⚠️ That's not a .db file.")
        return
    try:
        f = await context.bot.get_file(doc.file_id)
        await f.download_to_drive(DB_PATH)
        await msg.reply_text(
            f"✅ Restored DB from <b>{doc.file_name}</b>.\n"
            f"Restart the bot for it to take full effect (Railway will redeploy).",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        log.error(f"Restore failed: {e}")
        await msg.reply_text(f"⚠️ Restore failed: {e}")

async def cmd_summary(update, context):
    if update.effective_user.id != ADMIN_ID: return
    await send_daily_summary(context, manual=True)

# ==================================================================
# DAILY SUMMARY
# ==================================================================
async def send_daily_summary(context, manual=False):
    now_local = datetime.now(TZ)
    yest_local_start = (now_local - timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    yest_local_end   = yest_local_start + timedelta(days=1)
    start_utc = yest_local_start.astimezone(ZoneInfo("UTC")).isoformat()
    end_utc   = yest_local_end.astimezone(ZoneInfo("UTC")).isoformat()
    label     = yest_local_start.strftime("%d-%b-%Y")

    with db() as conn:
        agg = conn.execute("""
            SELECT
              SUM(CASE WHEN status='approved'  THEN 1 ELSE 0 END) approved,
              SUM(CASE WHEN status='rejected'  THEN 1 ELSE 0 END) rejected,
              SUM(CASE WHEN status='verifying' THEN 1 ELSE 0 END) pending,
              COUNT(*) total,
              COALESCE(SUM(CASE WHEN status='approved' THEN amount ELSE 0 END),0) revenue
            FROM purchases
            WHERE created_at >= ? AND created_at < ?
        """, (start_utc, end_utc)).fetchone()

        new_users = conn.execute("""
            SELECT COUNT(*) c FROM users
            WHERE created_at >= ? AND created_at < ?
        """, (start_utc, end_utc)).fetchone()["c"]

        rows = conn.execute("""
            SELECT p.id, p.created_at, p.user_id,
                   u.first_name, u.last_name, u.username,
                   p.channel_name, p.amount, p.qr_used, p.upi_name,
                   p.status, p.approved_at, p.rejected_at
            FROM purchases p JOIN users u ON u.user_id=p.user_id
            WHERE p.created_at >= ? AND p.created_at < ?
            ORDER BY p.id
        """, (start_utc, end_utc)).fetchall()

    text = (
        f"📅 <b>Daily Summary — {label}</b>\n\n"
        f"👥 New users          : <b>{new_users}</b>\n"
        f"🛒 Purchase attempts  : <b>{agg['total'] or 0}</b>\n"
        f"✅ Approved           : <b>{agg['approved'] or 0}</b>\n"
        f"❌ Rejected           : <b>{agg['rejected'] or 0}</b>\n"
        f"⏳ Still pending      : <b>{agg['pending'] or 0}</b>\n"
        f"💰 Revenue (approved) : <b>₹{agg['revenue'] or 0}</b>\n"
    )
    if manual:
        text += "\n<i>(manually triggered)</i>"

    sio = io.StringIO()
    sio.write("\ufeff")
    writer = csv.writer(sio)
    writer.writerow([
        "PurchaseID","CreatedAt","UserID","Name","Username",
        "Channel","Amount","QR","UPIName","Status","ApprovedAt","RejectedAt",
    ])
    for r in rows:
        nm = f"{r['first_name'] or ''} {r['last_name'] or ''}".strip()
        writer.writerow([
            r["id"], r["created_at"], r["user_id"], nm,
            r["username"] or "", r["channel_name"], r["amount"],
            f"QR{(r['qr_used'] or 0)+1}", r["upi_name"] or "",
            r["status"], r["approved_at"] or "", r["rejected_at"] or "",
        ])
    csv_bytes = sio.getvalue().encode("utf-8")

    try:
        await context.bot.send_message(ADMIN_ID, text, parse_mode=ParseMode.HTML)
        if rows:
            await context.bot.send_document(
                ADMIN_ID,
                document=InputFile(BytesIO(csv_bytes), filename=f"summary_{label}.csv"),
                caption=f"📊 Full activity for {label}",
            )
    except Exception as e:
        log.error(f"Daily summary send failed: {e}")

# ==================================================================
# Stale-pending reminder
# ==================================================================
async def check_pending_reminders(context):
    threshold = (datetime.utcnow() - timedelta(hours=PENDING_REMINDER_HOURS)).isoformat()
    with db() as conn:
        rows = conn.execute("""
            SELECT * FROM purchases
            WHERE status='verifying' AND reminder_sent=0
              AND upi_submitted_at IS NOT NULL
              AND upi_submitted_at < ?
        """, (threshold,)).fetchall()
    for r in rows:
        try:
            await context.bot.send_message(
                ADMIN_ID,
                text=(f"⏰ <b>Stale pending #{r['id']}</b>\n"
                      f"User <code>{r['user_id']}</code> — "
                      f"{r['channel_name']} ₹{r['amount']} — "
                      f"submitted {r['upi_submitted_at']} UTC.\n"
                      f"Use /pending to review."),
                parse_mode=ParseMode.HTML,
            )
            update_purchase(r["id"], reminder_sent=1)
        except Exception as e:
            log.error(f"Reminder send failed: {e}")

# ==================================================================
# MAIN
# ==================================================================
async def on_error(update, context):
    log.error("Handler exception:", exc_info=context.error)

async def cmd_update_user(update, context):
    """Admin: Manually mark user as paid/approved - FIX for data restoration"""
    if update.effective_user.id != ADMIN_ID:
        return
    
    args = context.args
    if len(args) < 3:
        await update.message.reply_text(
            "Usage: /update_user <user_id> <channel_id|0> <price> [status]\n\n"
            "channel_id=0 for bundles, 1-10 for channels\n"
            "Status: approved (default) | pending | rejected\n\n"
            "Examples:\n"
            "/update_user 123456789 0 99 approved   (Bundle ₹99)\n"
            "/update_user 123456789 1 99 approved   (Channel 1)"
        )
        return
    
    try:
        user_id = int(args[0])
        channel_id = int(args[1])
        price = int(args[2])
        status = args[3].lower() if len(args) > 3 else "approved"
    except ValueError:
        await update.message.reply_text("❌ Invalid arguments (must be integers)")
        return
    
    if status not in ["approved", "pending", "rejected"]:
        await update.message.reply_text("❌ Status must be: approved, pending, or rejected")
        return
    
    now = datetime.now(TZ).isoformat()
    
    try:
        with db() as conn:
            # Ensure user exists
            upsert_user({"user_id": user_id})
            
            # Insert purchase
            conn.execute(
                """
                INSERT INTO purchases 
                (user_id, channel_id, channel_name, amount, status, created_at, approved_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, channel_id, 
                 f"Channel {channel_id}" if channel_id > 0 else f"Bundle ₹{price}",
                 price, status, now, now if status == "approved" else None)
            )
        
        # Notify user
        if status == "approved":
            try:
                msg_text = "✅ <b>Admin has approved your access!</b>\n\nTap /start to see your channels."
                await context.bot.send_message(user_id, msg_text, parse_mode=ParseMode.HTML, disable_notification=True)
            except:
                pass
        
        await update.message.reply_html(f"✅ Updated: User {user_id} → {status}")
        log_user_message(user_id, "admin_update", f"Marked as {status}")
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")



async def cb_back_from_qr_bundle(update, context):
    """User tapped back from bundle QR — return to fallback menu WITHOUT deleting."""
    q = update.callback_query
    await q.answer()
    user = q.from_user
    
    if is_blocked(user.id):
        return
    
    # Edit the current message to show fallback menu (preserve chat flow)
    bundles = [
        ("1 Channel", 30),
        ("5 Channels", 59),
        ("10 Channels", 79),
        ("15 Channels", 99),
    ]
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📦 {name} — ₹{price}", callback_data=f"buy_bundle:{price}")]
        for name, price in bundles
    ] + [[InlineKeyboardButton("⬅️ Back", callback_data="back_to_start")]])
    
    text = (f"<b>💰 Budget Bundles</b>\n\n"
            f"Get started with affordable options:\n\n"
            f"• <b>1 Channel</b> — ₹30\n"
            f"• <b>5 Channels</b> — ₹59\n"
            f"• <b>10 Channels</b> — ₹79\n"
            f"• <b>15 Channels</b> — ₹99\n\n"
            f"<i>Tap any bundle to proceed with payment</i>")
    
    try:
        # Edit the current message (was QR, now shows bundle menu)
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except Exception as e:
        log.debug(f"back_from_qr_bundle edit failed: {e}")
        # Fallback: send new message if edit fails
        try:
            await context.bot.send_message(
                user.id, text, parse_mode=ParseMode.HTML, 
                reply_markup=kb, disable_notification=True
            )
        except:
            pass

async def cb_back_from_qr_channel(update, context):
    """User tapped back from channel QR — return to main menu (SAME LOGIC AS BUNDLE)."""
    q = update.callback_query
    await q.answer()
    user = q.from_user
    
    if is_blocked(user.id):
        return
    
    # Get the main menu text and keyboard based on user state
    if is_dnd(user.id):
        text = "⏳ Admin is currently away. Message will be reviewed soon."
        kb = None
    else:
        paid = has_paid_tier1(user.id)
        owned_cids = set(get_owned_channel_ids(user.id))
        owned_prices = set(get_owned_bundle_prices(user.id))
        
        if not paid:
            # Unpaid user - show Tier 1 and bundles
            text = (f"👋 <b>Welcome!</b>\n\n"
                    f"Get started with <b>{CHANNELS[0]['name']}</b> for ₹{CHANNELS[0]['price']}\n\n"
                    f"Or choose from budget bundles below.")
            
            rows = [[InlineKeyboardButton(f"⭐ {CHANNELS[0]['name']} — ₹{CHANNELS[0]['price']}", 
                                         callback_data=f"buy:{CHANNELS[0]['id']}")]]
            if is_fallback_enabled():
                rows.append([InlineKeyboardButton("📦 Budget Bundles", callback_data="fallback_menu")])
            kb = InlineKeyboardMarkup(rows)
        else:
            # Paid user - show channels and upgrades
            text = "<b>✅ Your Channels</b>\n\n"
            rows = []
            
            # Show owned channels with join links
            for ch in CHANNELS:
                if ch["id"] in owned_cids:
                    rows.append([InlineKeyboardButton(f"✅ {ch['name']}", url=ch["link"])])
            
            # Show owned bundles with join links
            for price in sorted(owned_prices):
                if price in BUNDLES:
                    rows.append([InlineKeyboardButton(f"✅ {BUNDLES[price]['name']}", 
                                                     url=BUNDLES[price]["link"])])
            
            # Show locked channels as upgrades
            for ch in CHANNELS:
                if ch["id"] not in owned_cids:
                    rows.append([InlineKeyboardButton(f"🔒 {ch['name']} — ₹{ch['price']}", 
                                                     callback_data=f"buy:{ch['id']}")])
            
            # Show bundle upgrades
            if owned_prices:
                max_price = max(owned_prices)
                for price in sorted(BUNDLES.keys()):
                    if price > max_price:
                        rows.append([InlineKeyboardButton(
                            f"🔒 {BUNDLES[price]['name']} — ₹{price}",
                            callback_data=f"buy_bundle:{price}"
                        )])
            
            kb = InlineKeyboardMarkup(rows) if rows else None
    
    try:
        # Edit the current message (was QR, now shows menu) — SAME AS BUNDLE BACK BUTTON
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except Exception as e:
        log.debug(f"back_from_qr_channel edit failed: {e}")
        # Fallback: send new message if edit fails — SAME AS BUNDLE BACK BUTTON
        try:
            await context.bot.send_message(
                user.id, text, parse_mode=ParseMode.HTML, 
                reply_markup=kb, disable_notification=True
            )
        except:
            pass



async def cb_back_from_proof(update, context):
    """User tapped back from proof screen — return to main menu (SAME LOGIC AS BUNDLE)."""
    q = update.callback_query
    await q.answer()
    user = q.from_user
    
    if is_blocked(user.id):
        return
    
    # Get the main menu text and keyboard (same as cb_back_from_qr_channel)
    if is_dnd(user.id):
        text = "⏳ Admin is currently away. Message will be reviewed soon."
        kb = None
    else:
        paid = has_paid_tier1(user.id)
        owned_cids = set(get_owned_channel_ids(user.id))
        owned_prices = set(get_owned_bundle_prices(user.id))
        
        if not paid:
            text = (f"👋 <b>Welcome!</b>\n\n"
                    f"Get started with <b>{CHANNELS[0]['name']}</b> for ₹{CHANNELS[0]['price']}\n\n"
                    f"Or choose from budget bundles below.")
            
            rows = [[InlineKeyboardButton(f"⭐ {CHANNELS[0]['name']} — ₹{CHANNELS[0]['price']}", 
                                         callback_data=f"buy:{CHANNELS[0]['id']}")]]
            if is_fallback_enabled():
                rows.append([InlineKeyboardButton("📦 Budget Bundles", callback_data="fallback_menu")])
            kb = InlineKeyboardMarkup(rows)
        else:
            text = "<b>✅ Your Channels</b>\n\n"
            rows = []
            
            # Show owned channels
            for ch in CHANNELS:
                if ch["id"] in owned_cids:
                    rows.append([InlineKeyboardButton(f"✅ {ch['name']}", url=ch["link"])])
            
            # Show owned bundles
            for price in sorted(owned_prices):
                if price in BUNDLES:
                    rows.append([InlineKeyboardButton(f"✅ {BUNDLES[price]['name']}", 
                                                     url=BUNDLES[price]["link"])])
            
            # Show upgrades
            for ch in CHANNELS:
                if ch["id"] not in owned_cids:
                    rows.append([InlineKeyboardButton(f"🔒 {ch['name']} — ₹{ch['price']}", 
                                                     callback_data=f"buy:{ch['id']}")])
            
            # Show bundle upgrades
            if owned_prices:
                max_price = max(owned_prices)
                for price in sorted(BUNDLES.keys()):
                    if price > max_price:
                        rows.append([InlineKeyboardButton(
                            f"🔒 {BUNDLES[price]['name']} — ₹{price}",
                            callback_data=f"buy_bundle:{price}"
                        )])
            
            kb = InlineKeyboardMarkup(rows) if rows else None
    
    try:
        # Edit the current message (was proof prompt, now shows main menu)
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except Exception as e:
        log.debug(f"back_from_proof edit failed: {e}")
        # Fallback: send new message if edit fails
        try:
            await context.bot.send_message(
                user.id, text, parse_mode=ParseMode.HTML, 
                reply_markup=kb, disable_notification=True
            )
        except:
            pass

def main():
    if not BOT_TOKEN or not ADMIN_ID:
        raise RuntimeError("Set BOT_TOKEN and ADMIN_ID env vars.")
    if not CHANNELS:
        log.warning("No CHANNEL_n env vars set!")
    
    # Check database path
    db_size = 0
    if os.path.exists(DB_PATH):
        db_size = os.path.getsize(DB_PATH) / (1024 * 1024)  # Convert to MB
        log.info(f"✅ Using local database: {DB_PATH} ({db_size:.2f} MB)")
    else:
        log.info(f"📝 Database will be created at: {DB_PATH}")
    
    log.info(f"✅ Database is committed to git - all data persists across deploys!")

    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    # User
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(cb_fallback_menu,  pattern=r"^fallback_menu$"))
    app.add_handler(CallbackQueryHandler(cb_buy_bundle,     pattern=r"^buy_bundle:"))
    app.add_handler(CallbackQueryHandler(cb_back_to_start,  pattern=r"^back_to_start$"))
    app.add_handler(CallbackQueryHandler(cb_buy,          pattern=r"^buy:"))
    app.add_handler(CallbackQueryHandler(cb_upi_start,    pattern=r"^upi:start:"))
    app.add_handler(CallbackQueryHandler(cb_back_from_qr_bundle, pattern=r"^back_from_qr_bundle$"))
    app.add_handler(CallbackQueryHandler(cb_back_from_qr_channel, pattern=r"^back_from_qr_channel$"))
    app.add_handler(CallbackQueryHandler(cb_back_from_proof, pattern=r"^back_from_proof$"))
    app.add_handler(CallbackQueryHandler(cb_proof_choice, pattern=r"^proof:"))
    app.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND,
        on_text_message,
    ))
    app.add_handler(MessageHandler(
        filters.PHOTO & filters.ChatType.PRIVATE, on_photo
    ))

    # Admin
    app.add_handler(CommandHandler("stats",   cmd_stats))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(CommandHandler("reset",   cmd_reset))
    app.add_handler(CommandHandler("update_user",   cmd_update_user))
    app.add_handler(CommandHandler("resetme", cmd_resetme))
    app.add_handler(CommandHandler("wipe",    cmd_wipe))
    app.add_handler(CommandHandler("whoami",    cmd_whoami))
    app.add_handler(CommandHandler("wipeall",   cmd_wipeall))
    app.add_handler(CommandHandler("resetall",  cmd_resetall))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("listusers", cmd_listusers))
    app.add_handler(CommandHandler("find",      cmd_find))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("logs",      cmd_logs))
    app.add_handler(CommandHandler("msg",       cmd_msg))
    app.add_handler(CommandHandler("block",     cmd_block))
    app.add_handler(CommandHandler("unblock",     cmd_unblock))
    app.add_handler(CommandHandler("away",              cmd_away))
    app.add_handler(CommandHandler("fallback_toggle",   cmd_fallback_toggle))
    app.add_handler(CommandHandler("special_offers_toggle", cmd_special_offers_toggle))
    app.add_handler(CommandHandler("show_channels",     cmd_show_channels))
    app.add_handler(CommandHandler("show_channels_status", cmd_show_channels_status))
    app.add_handler(CommandHandler("channel_price",     cmd_channel_price))
    app.add_handler(CommandHandler("channel_price_status", cmd_channel_price_status))
    app.add_handler(CommandHandler("promo_set",      cmd_promo_set))
    app.add_handler(CommandHandler("promo_clear",    cmd_promo_clear))
    app.add_handler(CommandHandler("promo_status",   cmd_promo_status))
    app.add_handler(CommandHandler("promo_send",     cmd_promo_send))
    app.add_handler(CommandHandler("promo_personal", cmd_promo_personal))
    app.add_handler(CommandHandler("bulk_ids",       cmd_bulk_ids))
    app.add_handler(CommandHandler("bulk_promo_users", cmd_bulk_promo_users))
    app.add_handler(CommandHandler("offer_tier",        cmd_offer_tier))
    app.add_handler(CommandHandler("offer_user",        cmd_offer_user))
    app.add_handler(CommandHandler("unpaid",      cmd_unpaid))
    app.add_handler(CommandHandler("backup",      cmd_backup))
    app.add_handler(CommandHandler("import_csv",  cmd_import_csv))
    app.add_handler(MessageHandler(
        filters.Document.ALL & filters.ChatType.PRIVATE,
        on_csv_import_file
    ))
    app.add_handler(CommandHandler("restore",     cmd_restore))
    app.add_handler(CallbackQueryHandler(cb_admin, pattern=r"^adm:"))

    jq = app.job_queue
    jq.run_daily(
        send_daily_summary,
        time=dtime(SUMMARY_HOUR, SUMMARY_MINUTE, tzinfo=TZ),
        name="daily_summary",
    )
    jq.run_repeating(
        check_pending_reminders,
        interval=timedelta(hours=1),
        first=timedelta(minutes=5),
        name="pending_reminders",
    )
    # Auto-backup DB to admin's Telegram once per day at midnight IST.
    # Manual /backup command available anytime for instant snapshots.
    jq.run_daily(
        send_db_backup,
        time=dtime(0, 0, tzinfo=TZ),
        name="daily_db_backup",
    )

    app.add_error_handler(on_error)
    log.info("Bot v3 starting…")
    # Tuned polling: timeout=10 keeps long-poll connection open for 10s,
    # giving Telegram instant push to deliver updates with minimal lag.
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        poll_interval=0.0,   # no artificial sleep between polls
        timeout=10,          # long-poll: server holds connection until update arrives
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
