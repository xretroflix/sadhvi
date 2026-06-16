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
QR_EXPIRY_MINUTES = int(os.getenv("QR_EXPIRY_MINUTES", "5"))

# Auto-wipe user chat this many MINUTES after approval/rejection (0 = disabled)
# Default 30 mins — gives user time to join channel, then cleans up the chat.
# When user comes back later, chat looks fresh.
AUTO_WIPE_MINUTES = int(os.getenv("AUTO_WIPE_MINUTES", "5"))

# Auto-wipe idle user chat after this many MINUTES of no interaction (0 = disabled)
INACTIVITY_MINUTES = int(os.getenv("INACTIVITY_MINUTES", "5"))

def _parse_channel(s: str):
    if not s or "|" not in s:
        return None
    parts = [p.strip() for p in s.split("|")]
    if len(parts) != 3:
        return None
    return {"name": parts[0], "price": int(parts[1]), "link": parts[2]}

# Seed channels from env vars only — DB is the source of truth at runtime.
# Env vars are only used once on first boot to pre-populate the DB.
_ENV_CHANNELS = []
for i in range(1, 11):
    c = _parse_channel(os.getenv(f"CHANNEL_{i}", ""))
    if c:
        c["id"] = i
        _ENV_CHANNELS.append(c)

def get_channels() -> list:
    """Live channel list from DB. Falls back to env vars if DB is empty.
    Queries base columns first, then optional columns separately so a
    missing migration column never breaks the whole channel list."""
    try:
        with db() as conn:
            # Step 1 — safe base columns always present
            rows = conn.execute("""
                SELECT id, name, price, link, position
                FROM channels
                WHERE is_active=1
                ORDER BY position ASC, id ASC
            """).fetchall()

        if not rows:
            log.warning("No channels in DB — falling back to env var channels.")
            return _ENV_CHANNELS

        result = [dict(r) for r in rows]

        # Step 2 — optional columns added by migration; safe to fail
        try:
            with db() as conn:
                extra_rows = conn.execute("""
                    SELECT id, group_label, separator_after
                    FROM channels
                    WHERE is_active=1
                    ORDER BY position ASC, id ASC
                """).fetchall()
            extra_map = {r["id"]: dict(r) for r in extra_rows}
            for c in result:
                ex = extra_map.get(c["id"], {})
                c["group_label"] = ex.get("group_label") or ""
                c["separator_after"] = int(ex.get("separator_after") or 0)
        except sqlite3.OperationalError:
            # Columns not yet migrated — set safe defaults
            for c in result:
                c["group_label"] = ""
                c["separator_after"] = 0

        return result

    except sqlite3.OperationalError:
        log.warning("Channels table missing — falling back to env var channels.")
        return _ENV_CHANNELS

# Module-level alias so all existing code using CHANNELS still works.
# Every access re-queries DB so changes are instant.
class _ChannelProxy:
    """Proxy that makes CHANNELS behave like a list but always reads from DB."""
    def __iter__(self):
        return iter(get_channels())
    def __len__(self):
        return len(get_channels())
    def __getitem__(self, idx):
        return get_channels()[idx]
    def __bool__(self):
        return bool(get_channels())

CHANNELS = _ChannelProxy()

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
    (299, "BUNDLE_1"),
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
            CREATE TABLE IF NOT EXISTS sleep_visitors (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                first_name  TEXT,
                last_name   TEXT,
                username    TEXT,
                visited_at  TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_sleep_vis
                ON sleep_visitors(visited_at);
            CREATE TABLE IF NOT EXISTS channels (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                price       INTEGER NOT NULL,
                link        TEXT NOT NULL,
                is_active   INTEGER DEFAULT 1,
                position    INTEGER DEFAULT 0,
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS qr_codes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                file_id     TEXT NOT NULL,
                upi_label   TEXT,
                is_active   INTEGER DEFAULT 1,
                priority    INTEGER DEFAULT 1,
                position    INTEGER DEFAULT 0,
                use_count   INTEGER DEFAULT 0,
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS qr_settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS channels (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT NOT NULL,
                price           INTEGER NOT NULL,
                link            TEXT NOT NULL,
                is_active       INTEGER DEFAULT 1,
                position        INTEGER DEFAULT 0,
                group_label     TEXT DEFAULT NULL,
                separator_after INTEGER DEFAULT 0,
                created_at      TEXT DEFAULT CURRENT_TIMESTAMP
            );
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
        try:
            conn.execute("ALTER TABLE channels ADD COLUMN group_label TEXT DEFAULT NULL")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE channels ADD COLUMN separator_after INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE purchases ADD COLUMN qr_code_id INTEGER")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE purchases ADD COLUMN qr_code_id INTEGER")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE users ADD COLUMN session_gen INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        

    seed_channels_from_env()

def seed_channels_from_env():
    """Seed env var channels into DB if no active channels exist.
    Safe to call multiple times — only inserts if DB has no active channels."""
    if not _ENV_CHANNELS:
        log.warning("No CHANNEL_n env vars set and no channels in DB.")
        return

    with db() as conn:
        existing = conn.execute(
            "SELECT COUNT(*) c FROM channels WHERE is_active=1"
        ).fetchone()["c"]
        if existing > 0:
            return  # Active channels exist in DB — don't overwrite

        # Check if rows exist but all deactivated — don't re-insert
        total = conn.execute(
            "SELECT COUNT(*) c FROM channels"
        ).fetchone()["c"]
        if total > 0:
            log.warning(
                f"All {total} channels are deactivated. "
                f"Use /channel_restore to reactivate."
            )
            return

        # Insert env var channels
        for i, c in enumerate(_ENV_CHANNELS):
            conn.execute("""
                INSERT INTO channels (name, price, link, position)
                VALUES (?, ?, ?, ?)
            """, (c["name"], c["price"], c["link"], i))
        log.info(f"Seeded {len(_ENV_CHANNELS)} channels from env vars into DB")

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
            WHERE user_id=? AND status NOT IN ('approved','rejected')
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

def get_users_by_purchase_status(status: str) -> list:
    """Get users who have purchases with given status (rejected/cancelled)
    but do NOT have any approved purchases."""
    with db() as conn:
        rows = conn.execute("""
            SELECT DISTINCT u.user_id, u.first_name, u.last_name, u.username
            FROM users u
            JOIN purchases p ON p.user_id = u.user_id
            WHERE p.status = ?
            AND u.user_id NOT IN (
                SELECT DISTINCT user_id FROM purchases WHERE status='approved'
            )
            ORDER BY u.user_id DESC
        """, (status,)).fetchall()
    return [dict(r) for r in rows]

def is_tier_gate_enabled() -> bool:
    """Check if Tier 1 mandatory gate is enabled (default: True)."""
    with db() as conn:
        r = conn.execute(
            "SELECT value FROM admin_settings WHERE key='tier_gate_enabled'"
        ).fetchone()
    return r["value"].lower() == "true" if r else True

async def cmd_tier_gate(update, context):
    """Admin: /tier_gate <on|off|status>

    on  → Tier 1 mandatory (default). Users must buy T1 before T2+.
    off → All tiers visible directly. No mandatory gate.

    Examples:
    /tier_gate on
    /tier_gate off
    /tier_gate status
    """
    if update.effective_user.id != ADMIN_ID:
        return

    args = context.args or []
    if not args:
        await update.message.reply_html(
            "Usage: <code>/tier_gate &lt;on|off|status&gt;</code>\n\n"
            "<code>/tier_gate on</code> — Tier 1 mandatory before T2+\n"
            "<code>/tier_gate off</code> — All tiers visible directly\n"
            "<code>/tier_gate status</code> — Check current setting"
        )
        return

    action = args[0].lower()

    if action == "status":
        enabled = is_tier_gate_enabled()
        status = "⭐ ON (Tier 1 mandatory)" if enabled else "🔓 OFF (all tiers visible)"
        await update.message.reply_html(
            f"<b>Tier Gate:</b> {status}\n\n"
            f"<i>{'Users must buy Tier 1 before accessing higher tiers.' if enabled else 'All tiers are directly purchasable without Tier 1.'}</i>"
        )
        return

    if action not in ["on", "off"]:
        await update.message.reply_text("Use: on, off, or status")
        return

    enabled = action == "on"
    set_admin_setting("tier_gate_enabled", "true" if enabled else "false")

    status = "⭐ ON (Tier 1 mandatory)" if enabled else "🔓 OFF (all tiers visible)"
    await update.message.reply_html(
        f"<b>Tier Gate:</b> {status}\n\n"
        f"<i>Next /start will reflect this change for all users.</i>"
    )

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
    expected_gen = context.job.data.get("session_gen")
    # If a newer /start has run since this job was scheduled, abort.
    if expected_gen is not None and get_session_gen(user_id) != expected_gen:
        log.info(f"auto_wipe_user_chat: stale job for user {user_id}, skipping.")
        return
    log.info(f"Auto-wiping chat (msgs only) for user {user_id}")
    # Gather ALL known bot message IDs (tracked + menu_msg_id + main_msg_id)
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
    for mid in all_ids:
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
    gen = get_session_gen(user_id)
    context.job_queue.run_once(
        auto_wipe_user_chat,
        when=timedelta(minutes=minutes),
        data={"user_id": user_id, "session_gen": gen},
        name=f"autowipe_{user_id}",
    )

async def inactivity_wipe(context):
    """JobQueue callback: user went idle mid-flow — wipe their chat entirely."""
    user_id = context.job.data["user_id"]
    expected_gen = context.job.data.get("session_gen")
    # If a newer /start has run since this job was scheduled, abort.
    if expected_gen is not None and get_session_gen(user_id) != expected_gen:
        log.info(f"inactivity_wipe: stale job for user {user_id}, skipping.")
        return
    p = get_active_purchase(user_id)

    # If user has a purchase stuck in mid-flow, cancel it cleanly
    if p and p["status"] not in ("approved", "rejected", "cancelled"):
        update_purchase(p["id"], status="cancelled")
        log.info(f"Inactivity: cancelled purchase #{p['id']} for user {user_id}")

    log.info(f"Inactivity wipe triggered for user {user_id}")

    # Gather ALL known bot message IDs
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

    for mid in all_ids:
        try:
            await context.bot.delete_message(chat_id=user_id, message_id=mid)
        except Exception as e:
            log.debug(f"inactivity_wipe delete {mid} failed: {e}")

    # Reset message refs — keep purchase records intact
    with db() as conn:
        conn.execute(
            "UPDATE users SET menu_msg_id=NULL, tracked_msgs='[]' "
            "WHERE user_id=?", (user_id,))
        conn.execute(
            "UPDATE purchases SET main_msg_id=NULL WHERE user_id=?",
            (user_id,))

    # Remove AWAITING_UPI state if stuck there
    AWAITING_UPI.pop(user_id, None)


def reset_inactivity_timer(context, user_id):
    if INACTIVITY_MINUTES <= 0:
        return

    job_name = f"inactivity_{user_id}"

    # Cancel existing timer
    for job in context.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()

    gen = get_session_gen(user_id)
    # Schedule fresh timer carrying current generation
    context.job_queue.run_once(
        inactivity_wipe,
        when=timedelta(minutes=INACTIVITY_MINUTES),
        data={"user_id": user_id, "session_gen": gen},
        name=job_name,
    )


def cancel_inactivity_timer(context, user_id):
    """Cancel inactivity timer — call when flow completes (approved/rejected)."""
    for job in context.job_queue.get_jobs_by_name(f"inactivity_{user_id}"):
        job.schedule_removal()

def get_session_gen(user_id: int) -> int:
    """Get current session generation counter for a user."""
    with db() as conn:
        r = conn.execute(
            "SELECT session_gen FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
    return int(r["session_gen"] or 0) if r else 0

def bump_session_gen(user_id: int) -> int:
    """Increment session generation counter. Returns new value.
    Called at the top of cmd_start to invalidate all prior scheduled jobs."""
    with db() as conn:
        conn.execute(
            "UPDATE users SET session_gen = COALESCE(session_gen, 0) + 1 WHERE user_id=?",
            (user_id,)
        )
        r = conn.execute(
            "SELECT session_gen FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
    return int(r["session_gen"] or 0) if r else 0

def cancel_all_user_jobs(context, user_id: int):
    """Cancel every scheduled job for a user — inactivity, auto-wipe,
    animations, QR expiry, reconfirm jobs, maintenance msgs.
    Call at top of cmd_start."""
    prefixes = [
        f"inactivity_{user_id}",
        f"autowipe_{user_id}",
        f"maint_del_{user_id}",
    ]
    for job in context.job_queue.jobs():
        name = job.name or ""
        if any(name.startswith(p) for p in prefixes):
            job.schedule_removal()
        if (f"anim_{user_id}_" in name
                or f"reconfirm_approve_" in name
                or f"reconfirm_reject_" in name
                or f"qr_expire_{user_id}_" in name
                or f"expmsg_del_{user_id}_" in name):
            job.schedule_removal()


def reset_user_data(user_id: int):
    """Delete all purchases + reset all message refs for a user."""
    with db() as conn:
        conn.execute("DELETE FROM purchases WHERE user_id=?", (user_id,))
        conn.execute(
            "UPDATE users SET tracked_msgs='[]', menu_msg_id=NULL "
            "WHERE user_id=?", (user_id,))
    # Clear in-memory UPI capture state
    AWAITING_UPI.pop(user_id, None)

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

async def cb_noop(update, context):
    """No-op callback for label/separator buttons — just dismiss the alert."""
    await update.callback_query.answer()

async def cmd_proof_mode(update, context):
    """Admin: /proof_mode <upi|screenshot|both|none>

    upi        → only UPI name collection
    screenshot → only screenshot collection
    both       → both options shown (default)
    none       → skip proof entirely, queues for manual admin approval

    Examples:
    /proof_mode upi
    /proof_mode screenshot
    /proof_mode both
    /proof_mode none
    /proof_mode status
    """
    if update.effective_user.id != ADMIN_ID:
        return

    args = context.args or []
    if not args:
        await update.message.reply_html(
            "Usage: <code>/proof_mode &lt;upi|screenshot|both|none|status&gt;</code>\n\n"
            "<code>both</code>       — UPI name + Screenshot (default)\n"
            "<code>upi</code>        — UPI name only\n"
            "<code>screenshot</code> — Screenshot only\n"
            "<code>none</code>       — Skip proof, queue for manual approval\n\n"
            "Example: <code>/proof_mode upi</code>"
        )
        return

    action = args[0].lower()

    if action == "status":
        mode = get_proof_mode()
        labels = {
            "both":       "👤 UPI Name + 📸 Screenshot",
            "upi":        "👤 UPI Name only",
            "screenshot": "📸 Screenshot only",
            "none":       "⏭️ No proof — manual approval queue",
        }
        await update.message.reply_html(
            f"<b>Proof Mode:</b> {labels.get(mode, mode)}\n\n"
            f"<i>Change with /proof_mode &lt;upi|screenshot|both|none&gt;</i>"
        )
        return

    if action not in ("upi", "screenshot", "both", "none"):
        await update.message.reply_text(
            "Valid options: upi, screenshot, both, none, status"
        )
        return

    set_admin_setting("proof_mode", action)

    labels = {
        "both":       "👤 UPI Name + 📸 Screenshot",
        "upi":        "👤 UPI Name only",
        "screenshot": "📸 Screenshot only",
        "none":       "⏭️ No proof — manual approval queue",
    }
    await update.message.reply_html(
        f"✅ <b>Proof Mode set:</b> {labels[action]}\n\n"
        f"<i>Takes effect immediately on next payment.</i>"
    )

def cancel_all_user_jobs(context, user_id: int):
    """Cancel every scheduled job tied to a user.
    Called at the very top of cmd_start so no prior-session job
    can fire and corrupt the new session."""
    uid = str(user_id)
    for job in context.job_queue.jobs():
        name = job.name or ""
        if (
            # Exact-prefix jobs
            name.startswith(f"inactivity_{uid}")
            or name.startswith(f"autowipe_{uid}")
            or name.startswith(f"maint_del_{uid}")
            or name.startswith(f"pinmsg_{uid}")
            or name.startswith(f"expmsg_del_{uid}")
            or name.startswith(f"qr_expire_{uid}")
            # Pattern jobs that embed user_id anywhere
            or f"_anim_{uid}_"   in f"_{name}"
            or f"_anim_{uid}_"   in f"_{name}_"
            or name.startswith(f"anim_{uid}_")
            or f"reconfirm_approve_" in name
            or f"reconfirm_reject_"  in name
        ):
            job.schedule_removal()
            log.debug(f"cancel_all_user_jobs: removed job '{name}' for user {uid}")

async def send_and_autodelete(context, chat_id, text,
                               delay=300, **kwargs):
    """Send a message and schedule it for auto-deletion after `delay` seconds.
    Default delay is 300 seconds (5 minutes).
    Returns the sent message object."""
    m = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        **kwargs
    )
    track_msg(chat_id, m.message_id)
    context.job_queue.run_once(
        _maintenance_msg_expire,
        when=delay,
        data={"chat_id": chat_id, "message_id": m.message_id},
        name=f"maint_del_{chat_id}_{m.message_id}",
    )
    return m

async def _maintenance_msg_expire(context):
    """JobQueue callback: delete maintenance message after 5 minutes.
    Always deletes unconditionally — if /start already wiped it,
    the delete will just silently fail which is fine."""
    data = context.job.data
    user_id    = data["chat_id"]
    message_id = data["message_id"]
    try:
        await context.bot.delete_message(
            chat_id=user_id, message_id=message_id
        )
        log.debug(f"maintenance msg expired for user {user_id}")
    except Exception as e:
        log.debug(f"_maintenance_msg_expire delete failed (already gone): {e}")

async def cmd_sleep(update, context):
    """Admin: /sleep <on|off|status> [return message]

    on  → Sleep mode ON. Paid users see only their channels.
          New users see maintenance message.
    off → Bot wakes up normally.

    Optional return-time message appended after 'on':
    /sleep on Back in 3 hours
    /sleep on Returns at 9:00 PM IST
    /sleep on Maintenance until Sunday 9 AM IST
    """
    if update.effective_user.id != ADMIN_ID:
        return

    args = context.args or []
    if not args:
        await update.message.reply_html(
            "Usage: <code>/sleep &lt;on|off|status&gt; [return message]</code>\n\n"
            "<code>/sleep on</code> — Enable maintenance mode\n"
            "<code>/sleep on Back in 3 hours</code> — With return time\n"
            "<code>/sleep on Returns at 9:00 PM IST</code> — Exact time\n"
            "<code>/sleep off</code> — Wake bot up\n"
            "<code>/sleep status</code> — Current state\n\n"
            "Use <code>/sleep_visitors</code> to see who visited while asleep."
        )
        return

    action = args[0].lower()

    if action == "status":
        enabled = is_sleep_mode()
        maint_msg = get_maintenance_message()
        with db() as conn:
            count = conn.execute(
                "SELECT COUNT(*) c FROM sleep_visitors"
            ).fetchone()["c"]
        status = "😴 SLEEPING (maintenance)" if enabled else "✅ AWAKE"
        await update.message.reply_html(
            f"<b>Bot Status:</b> {status}\n"
            f"<b>Return message:</b> {maint_msg or '(none set)'}\n"
            f"<b>Visitors recorded:</b> {count}\n\n"
            f"<i>Use /sleep_visitors to see the full list.</i>"
        )
        return

    if action not in ("on", "off"):
        await update.message.reply_text("Use: on, off, or status")
        return

    enabled = action == "on"
    set_admin_setting("sleep_mode", "true" if enabled else "false")

    if enabled:
        # Optional return-time message: everything after "on"
        return_msg = " ".join(args[1:]).strip() if len(args) > 1 else ""
        set_maintenance_message(return_msg)

        preview = return_msg or "(no return time set — users will just see maintenance notice)"
        await update.message.reply_html(
            "😴 <b>Maintenance mode ON</b>\n\n"
            f"<b>Return message:</b> {preview}\n\n"
            "• Paid users → see only their purchased channels\n"
            "• New/unpaid users → see maintenance message\n"
            "• No QR codes or purchase buttons shown\n\n"
            "Use <code>/sleep off</code> to wake up.\n"
            "Use <code>/sleep_visitors</code> to see who visited."
        )
    else:
        set_maintenance_message("")
        with db() as conn:
            count = conn.execute(
                "SELECT COUNT(*) c FROM sleep_visitors"
            ).fetchone()["c"]
        await update.message.reply_html(
            f"✅ <b>Bot is now AWAKE</b>\n\n"
            f"Recorded <b>{count}</b> visitors while in maintenance.\n"
            f"Use <code>/sleep_visitors</code> to see them."
        )


async def cmd_sleep_visitors(update, context):
    """Admin: /sleep_visitors [clear] — list or clear sleep mode visitors."""
    if update.effective_user.id != ADMIN_ID:
        return

    args = context.args or []

    if args and args[0].lower() == "clear":
        with db() as conn:
            count = conn.execute(
                "SELECT COUNT(*) c FROM sleep_visitors"
            ).fetchone()["c"]
            conn.execute("DELETE FROM sleep_visitors")
        await update.message.reply_html(
            f"🗑 Cleared <b>{count}</b> sleep visitors."
        )
        return

    with db() as conn:
        rows = conn.execute("""
            SELECT user_id, first_name, last_name, username, visited_at
            FROM sleep_visitors
            ORDER BY visited_at DESC
            LIMIT 100
        """).fetchall()

    if not rows:
        await update.message.reply_text(
            "No visitors recorded yet.\n"
            "Enable sleep mode with /sleep on"
        )
        return

    text = f"😴 <b>Sleep Visitors ({len(rows)})</b>\n\n"
    for r in rows:
        nm = f"{r['first_name'] or ''} {r['last_name'] or ''}".strip() or "—"
        un = f"@{r['username']}" if r['username'] else "(no username)"
        ts = r['visited_at'][:16].replace("T", " ")
        text += (
            f"<code>{r['user_id']}</code> — {nm} {un}\n"
            f"  <i>{ts}</i>\n"
        )

    if len(text) > 4000:
        text = text[:4000] + "\n\n<i>(truncated — use /sleep_visitors clear to reset)</i>"

    csv_buf = io.StringIO()
    csv_writer = csv.writer(csv_buf)
    csv_writer.writerow(["UserID", "Name", "Username", "VisitedAt"])
    for r in rows:
        nm = f"{r['first_name'] or ''} {r['last_name'] or ''}".strip()
        csv_writer.writerow([
            r['user_id'], nm,
            r['username'] or "", r['visited_at']
        ])

    await update.message.reply_html(
        text, disable_web_page_preview=True
    )
    try:
        await context.bot.send_document(
            chat_id=ADMIN_ID,
            document=InputFile(
                BytesIO(csv_buf.getvalue().encode()),
                filename="sleep_visitors.csv"
            ),
            caption="Sleep mode visitor list — ready for /bulk_promo_users",
        )
    except Exception as e:
        log.debug(f"sleep visitors CSV send failed: {e}")

async def cmd_channel_add(update, context):
    """Admin: /channel_add <name> | <price> | <invite_link>

    Add a new channel. Takes effect immediately — no restart needed.

    Example:
    /channel_add Premium Pack | 199 | https://t.me/+HASH
    """
    if update.effective_user.id != ADMIN_ID:
        return

    args_raw = update.message.text.split(maxsplit=1)
    if len(args_raw) < 2 or "|" not in args_raw[1]:
        await update.message.reply_html(
            "Usage: <code>/channel_add &lt;name&gt; | &lt;price&gt; | &lt;invite_link&gt;</code>\n\n"
            "Example:\n"
            "<code>/channel_add Premium Pack | 199 | https://t.me/+HASH</code>"
        )
        return

    parts = [p.strip() for p in args_raw[1].split("|")]
    if len(parts) != 3:
        await update.message.reply_html(
            "⚠️ Need exactly 3 parts separated by <code>|</code>\n"
            "Format: <code>Name | Price | Link</code>"
        )
        return

    name, price_str, link = parts
    try:
        price = int(price_str)
    except ValueError:
        await update.message.reply_text("⚠️ Price must be a number.")
        return

    if price < 1:
        await update.message.reply_text("⚠️ Price must be positive.")
        return

    if not link.startswith("https://t.me/"):
        await update.message.reply_text(
            "⚠️ Link must start with https://t.me/"
        )
        return

    # Get next position
    with db() as conn:
        max_pos = conn.execute(
            "SELECT COALESCE(MAX(position), 0) m FROM channels"
        ).fetchone()["m"]
        cur = conn.execute("""
            INSERT INTO channels (name, price, link, position)
            VALUES (?, ?, ?, ?)
        """, (name, price, link, max_pos + 1))
        new_id = cur.lastrowid

    await update.message.reply_html(
        f"✅ <b>Channel Added</b>\n\n"
        f"ID       : <code>{new_id}</code>\n"
        f"Name     : {name}\n"
        f"Price    : ₹{price}\n"
        f"Link     : <code>{link}</code>\n"
        f"Position : {max_pos + 1}\n\n"
        f"<i>Live immediately — no restart needed.</i>"
    )


async def cmd_channel_edit(update, context):
    """Admin: /channel_edit <id> <field> <value>

    Edit a channel's name, price, or link.

    Fields: name, price, link

    Examples:
    /channel_edit 1 price 299
    /channel_edit 1 name VIP Pack
    /channel_edit 1 link https://t.me/+NEWHASH
    """
    if update.effective_user.id != ADMIN_ID:
        return

    args = context.args or []
    if len(args) < 3:
        await update.message.reply_html(
            "Usage: <code>/channel_edit &lt;id&gt; &lt;field&gt; &lt;value&gt;</code>\n\n"
            "Fields: <code>name</code>, <code>price</code>, <code>link</code>, "
            "<code>position</code>\n\n"
            "Examples:\n"
            "<code>/channel_edit 1 price 299</code>\n"
            "<code>/channel_edit 1 name VIP Pack</code>\n"
            "<code>/channel_edit 1 link https://t.me/+NEWHASH</code>\n"
            "<code>/channel_edit 1 position 2</code>"
        )
        return

    try:
        channel_id = int(args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Invalid channel ID.")
        return

    field = args[1].lower()
    value_raw = " ".join(args[2:])

    allowed = ("name", "price", "link", "position", "group_label", "separator_after")
    if field not in allowed:
        await update.message.reply_text(
            f"⚠️ Field must be one of: {', '.join(allowed)}"
        )
        return

    # Type-check value
    if field in ("price", "position"):
        try:
            value = int(value_raw)
        except ValueError:
            await update.message.reply_text(f"⚠️ {field} must be a number.")
            return
    else:
        value = value_raw

    with db() as conn:
        existing = conn.execute(
            "SELECT * FROM channels WHERE id=?", (channel_id,)
        ).fetchone()
        if not existing:
            await update.message.reply_text(
                f"⚠️ Channel ID {channel_id} not found."
            )
            return
        conn.execute(
            f"UPDATE channels SET {field}=? WHERE id=?",
            (value, channel_id)
        )

    await update.message.reply_html(
        f"✅ <b>Channel Updated</b>\n\n"
        f"ID    : <code>{channel_id}</code>\n"
        f"Field : {field}\n"
        f"Value : {value}\n\n"
        f"<i>Live immediately — no restart needed.</i>"
    )


async def cmd_channel_remove(update, context):
    """Admin: /channel_remove <id>

    Deactivate a channel (soft delete — data kept, just hidden from users).
    Use /channel_restore <id> to bring it back.

    Example:
    /channel_remove 3
    """
    if update.effective_user.id != ADMIN_ID:
        return

    args = context.args or []
    if not args:
        await update.message.reply_html(
            "Usage: <code>/channel_remove &lt;id&gt;</code>\n\n"
            "Soft-deletes the channel (hidden from users, data kept).\n"
            "Example: <code>/channel_remove 3</code>"
        )
        return

    try:
        channel_id = int(args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Invalid channel ID.")
        return

    with db() as conn:
        existing = conn.execute(
            "SELECT * FROM channels WHERE id=?", (channel_id,)
        ).fetchone()
        if not existing:
            await update.message.reply_text(
                f"⚠️ Channel ID {channel_id} not found."
            )
            return
        conn.execute(
            "UPDATE channels SET is_active=0 WHERE id=?", (channel_id,)
        )

    await update.message.reply_html(
        f"🚫 <b>Channel Removed</b>\n\n"
        f"ID   : <code>{channel_id}</code>\n"
        f"Name : {existing['name']}\n\n"
        f"<i>Hidden from users immediately. "
        f"Use /channel_restore {channel_id} to bring it back.</i>"
    )


async def cmd_channel_restore(update, context):
    """Admin: /channel_restore <id> — restore a removed channel."""
    if update.effective_user.id != ADMIN_ID:
        return

    args = context.args or []
    if not args:
        await update.message.reply_html(
            "Usage: <code>/channel_restore &lt;id&gt;</code>"
        )
        return

    try:
        channel_id = int(args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Invalid channel ID.")
        return

    with db() as conn:
        existing = conn.execute(
            "SELECT * FROM channels WHERE id=?", (channel_id,)
        ).fetchone()
        if not existing:
            await update.message.reply_text(
                f"⚠️ Channel ID {channel_id} not found."
            )
            return
        conn.execute(
            "UPDATE channels SET is_active=1 WHERE id=?", (channel_id,)
        )

    await update.message.reply_html(
        f"✅ <b>Channel Restored</b>\n\n"
        f"ID   : <code>{channel_id}</code>\n"
        f"Name : {existing['name']}\n\n"
        f"<i>Visible to users immediately.</i>"
    )


async def cmd_channel_list(update, context):
    """Admin: /channel_list — show all channels (active and inactive)."""
    if update.effective_user.id != ADMIN_ID:
        return

    with db() as conn:
        rows = conn.execute("""
            SELECT id, name, price, link, is_active, position
            FROM channels
            ORDER BY position ASC, id ASC
        """).fetchall()

    if not rows:
        await update.message.reply_html(
            "No channels in DB yet.\n\n"
            "Add one: <code>/channel_add Name | Price | Link</code>"
        )
        return

    # Fetch optional columns separately — safe if migration hasn't run
    extra_map = {}
    try:
        with db() as conn:
            extras = conn.execute(
                "SELECT id, group_label, separator_after FROM channels"
            ).fetchall()
        extra_map = {r["id"]: dict(r) for r in extras}
    except sqlite3.OperationalError:
        pass

    text = f"📺 <b>All Channels ({len(rows)})</b>\n\n"
    for r in rows:
        status = "✅" if r["is_active"] else "🚫"
        ex = extra_map.get(r["id"], {})
        group = ex.get("group_label") or "—"
        sep = "on" if ex.get("separator_after") else "off"
        text += (
            f"{status} <b>ID {r['id']}</b> — {r['name']}\n"
            f"   💰 ₹{r['price']} | 📌 pos {r['position']}\n"
            f"   🏷 Group: {group} | ┄ Sep: {sep}\n"
            f"   🔗 <code>{r['link']}</code>\n\n"
        )

    text += (
        f"<b>Commands:</b>\n"
        f"<code>/channel_add Name | Price | Link</code>\n"
        f"<code>/channel_edit &lt;id&gt; &lt;field&gt; &lt;value&gt;</code>\n"
        f"<code>/channel_remove &lt;id&gt;</code>\n"
        f"<code>/channel_restore &lt;id&gt;</code>"
    )

    if len(text) > 4000:
        text = text[:4000] + "\n<i>(truncated)</i>"

    await update.message.reply_html(
        text, disable_web_page_preview=True
    )

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

def get_proof_mode() -> str:
    """Return current proof mode: 'both', 'upi', 'screenshot', 'none'."""
    with db() as conn:
        r = conn.execute(
            "SELECT value FROM admin_settings WHERE key='proof_mode'"
        ).fetchone()
    return r["value"] if r else "both"

def is_sleep_mode() -> bool:
    """Check if bot is in sleep mode."""
    with db() as conn:
        r = conn.execute(
            "SELECT value FROM admin_settings WHERE key='sleep_mode'"
        ).fetchone()
    return r["value"].lower() == "true" if r else False

def get_maintenance_message() -> str:
    """Get admin-configured maintenance return time message."""
    with db() as conn:
        r = conn.execute(
            "SELECT value FROM admin_settings WHERE key='maintenance_message'"
        ).fetchone()
    return r["value"] if r else ""

def set_maintenance_message(msg: str):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO admin_settings (key, value) VALUES ('maintenance_message', ?)",
            (msg,)
        )

def log_sleep_visitor(user):
    """Record a user who visited during sleep mode."""
    with db() as conn:
        conn.execute("""
            INSERT INTO sleep_visitors
                (user_id, first_name, last_name, username)
            VALUES (?, ?, ?, ?)
        """, (user.id, user.first_name, user.last_name, user.username))

def get_qr_mode() -> str:
    """round_robin | priority | single — default priority."""
    with db() as conn:
        r = conn.execute(
            "SELECT value FROM qr_settings WHERE key='qr_mode'"
        ).fetchone()
    return r["value"] if r else "priority"

def set_qr_mode(mode: str):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO qr_settings (key, value) "
            "VALUES ('qr_mode', ?)", (mode,)
        )

def get_rr_index() -> int:
    with db() as conn:
        r = conn.execute(
            "SELECT value FROM qr_settings WHERE key='rr_index'"
        ).fetchone()
    return int(r["value"]) if r else 0

def set_rr_index(idx: int):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO qr_settings (key, value) "
            "VALUES ('rr_index', ?)", (str(idx),)
        )

def get_active_single_qr_id() -> int | None:
    with db() as conn:
        r = conn.execute(
            "SELECT value FROM qr_settings WHERE key='active_qr_id'"
        ).fetchone()
    return int(r["value"]) if r else None

def set_active_single_qr_id(qr_id: int):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO qr_settings (key, value) "
            "VALUES ('active_qr_id', ?)", (str(qr_id),)
        )

def pick_qr_code() -> dict | None:
    """Pick a QR code dict based on current routing mode.
    Returns None if no QR codes in DB — caller falls back to env var QRs."""
    with db() as conn:
        active_qrs = conn.execute("""
            SELECT id, name, file_id, upi_label, priority, position
            FROM qr_codes
            WHERE is_active=1
            ORDER BY position ASC, id ASC
        """).fetchall()

    if not active_qrs:
        return None

    active_qrs = [dict(r) for r in active_qrs]
    mode = get_qr_mode()

    if mode == "single":
        selected_id = get_active_single_qr_id()
        selected = next(
            (q for q in active_qrs if q["id"] == selected_id),
            active_qrs[0]  # fallback to first if selected ID not found
        )

    elif mode == "round_robin":
        idx = get_rr_index() % len(active_qrs)
        selected = active_qrs[idx]
        set_rr_index(idx + 1)

    else:  # priority (default)
        weights = [max(q["priority"], 1) for q in active_qrs]
        selected = random.choices(active_qrs, weights=weights, k=1)[0]

    # Track usage
    with db() as conn:
        conn.execute(
            "UPDATE qr_codes SET use_count=use_count+1 WHERE id=?",
            (selected["id"],)
        )

    return selected

def build_channel_buttons(owned_channel_ids: set,
                          is_paid: bool,
                          unpaid_mode: bool = False) -> list:
    """Build channel button rows from DB.

    Key rule for removed channels (is_active=0):
      - Unpaid users  → never shown (hidden from purchase)
      - Paid users    → shown as ✅ join button ONLY if they own it
                        hidden entirely if they don't own it

    Active channels follow normal rules:
      - Unpaid        → ⭐ buy button (tier gate respected)
      - Paid + owned  → ✅ join button
      - Paid + unpaid → 🔒 buy button
    """
    # Active channels — for unpaid flow and unowned paid buttons
    active_channels = get_channels()  # WHERE is_active=1, sorted by position

    # All channels including removed — for owned detection in paid flow
    with db() as conn:
        all_channel_rows = conn.execute("""
            SELECT id, name, price, link, is_active,
                   position, group_label, separator_after
            FROM channels
            ORDER BY position ASC, id ASC
        """).fetchall()
    all_channels = [dict(r) for r in all_channel_rows]

    tier_gate = is_tier_gate_enabled()
    owned_int = {int(x) for x in owned_channel_ids if x is not None}

    rows = []

    if unpaid_mode:
        # Unpaid flow — only show active channels
        for idx, c in enumerate(active_channels):
            c_id = int(c["id"])

            label = (c.get("group_label") or "").strip()
            if label:
                rows.append([InlineKeyboardButton(
                    f"── {label} ──",
                    callback_data="noop"
                )])

            if tier_gate and idx > 0:
                # Tier gate on — only show first active channel
                try:
                    sep = int(c.get("separator_after") or 0)
                except (ValueError, TypeError):
                    sep = 0
                if sep == 1:
                    rows.append([InlineKeyboardButton(
                        "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄",
                        callback_data="noop"
                    )])
                continue

            rows.append([InlineKeyboardButton(
                f"⭐ {c['name']} — ₹{c['price']}",
                callback_data=f"buy:{c_id}",
            )])

            try:
                sep = int(c.get("separator_after") or 0)
            except (ValueError, TypeError):
                sep = 0
            if sep == 1:
                rows.append([InlineKeyboardButton(
                    "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄",
                    callback_data="noop"
                )])

    else:
        # Paid flow — iterate ALL channels so owned-but-removed ones appear
        for c in all_channels:
            c_id      = int(c["id"])
            is_active = int(c.get("is_active") or 0)

            # Removed channel — show ONLY if user owns it, skip otherwise
            if not is_active:
                if c_id in owned_int:
                    rows.append([InlineKeyboardButton(
                        f"✅ {c['name']}", url=c["link"]
                    )])
                # Either way no separator/label for removed channels —
                # keeps the menu clean
                continue

            # Active channel — normal label/separator/button logic
            label = (c.get("group_label") or "").strip()
            if label:
                rows.append([InlineKeyboardButton(
                    f"── {label} ──",
                    callback_data="noop"
                )])

            if c_id in owned_int:
                rows.append([InlineKeyboardButton(
                    f"✅ {c['name']}", url=c["link"]
                )])
            else:
                rows.append([InlineKeyboardButton(
                    f"🔒 {c['name']} — ₹{c['price']}",
                    callback_data=f"buy:{c_id}",
                )])

            try:
                sep = int(c.get("separator_after") or 0)
            except (ValueError, TypeError):
                sep = 0
            if sep == 1:
                rows.append([InlineKeyboardButton(
                    "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄",
                    callback_data="noop"
                )])

    return rows
    """Build channel button rows respecting position, group labels, separators.

    unpaid_mode=True  → show purchasable ⭐ buttons for all channels
    unpaid_mode=False → owned=✅ direct link, unowned=🔒 buy button

    Group labels and separators always apply regardless of mode.
    Access is tied to channel_id — position never affects access.
    """
    channels = get_channels()  # always fresh from DB — respects position order
    tier_gate = is_tier_gate_enabled()
    rows = []

    for c in channels:
        # Group label — non-tappable section header
        if c.get("group_label"):
            rows.append([InlineKeyboardButton(
                f"── {c['group_label']} ──",
                callback_data="noop"
            )])

        if unpaid_mode:
            # Unpaid user — all channels show as purchasable
            if tier_gate and channels and c["id"] == channels[0]["id"]:
                # Tier gate on — only show first channel as entry point
                rows.append([InlineKeyboardButton(
                    f"⭐ {c['name']} — ₹{c['price']}",
                    callback_data=f"buy:{c['id']}",
                )])
            elif not tier_gate:
                # Tier gate off — all channels purchasable directly
                rows.append([InlineKeyboardButton(
                    f"⭐ {c['name']} — ₹{c['price']}",
                    callback_data=f"buy:{c['id']}",
                )])
            else:
                # Tier gate on, not first channel — skip
                # Still add separator if set
                if c.get("separator_after"):
                    rows.append([InlineKeyboardButton(
                        "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄",
                        callback_data="noop"
                    )])
                continue
        else:
            # Paid user — owned=✅, unowned=🔒
            if c["id"] in owned_channel_ids:
                rows.append([InlineKeyboardButton(
                    f"✅ {c['name']}", url=c["link"]
                )])
            else:
                rows.append([InlineKeyboardButton(
                    f"🔒 {c['name']} — ₹{c['price']}",
                    callback_data=f"buy:{c['id']}",
                )])

        # Separator after this channel
        if c.get("separator_after"):
            rows.append([InlineKeyboardButton(
                "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄",
                callback_data="noop"
            )])

    return rows

# ==================================================================
# USER: /start
# ==================================================================

# ============================================================================
# MULTI-TIER ENTRY POINTS - 3x3 MATRIX LAYOUT
# ============================================================================

MULTI_TIER_ENABLED = os.getenv("MULTI_TIER_ENABLED", "false").lower() == "true"
TIER_OFFERS = {}
COMBO_OFFERS = {}

# Parse tier offers from environment
for i in range(1, 10):  # Support up to 9 tiers (3x3 matrix)
    env_key = f"TIER_{i}"
    tier_data = os.getenv(env_key)
    if tier_data:
        parts = tier_data.split("|")
        if len(parts) >= 2:
            TIER_OFFERS[i] = {
                "label": parts[0].strip(),
                "price": int(parts[1].strip()),
                "emoji": parts[2].strip() if len(parts) > 2 else "💎"
            }

# Parse combo offers (can be placed at any position)
for i in range(1, 4):  # Support up to 3 combo offers
    env_key = f"COMBO_{i}"
    combo_data = os.getenv(env_key)
    if combo_data:
        parts = combo_data.split("|")
        if len(parts) >= 2:
            # Auto-assign position: COMBO_1 after row 1, COMBO_2 after row 2, COMBO_3 after row 3
            position = int(parts[3].strip()) if len(parts) > 3 else i
            COMBO_OFFERS[i] = {
                "label": parts[0].strip(),
                "price": int(parts[1].strip()),
                "emoji": parts[2].strip() if len(parts) > 2 else "🎁",
                "position": position  # Position in grid (1=after row 1, 2=after row 2, etc.)
            }

def build_multi_tier_keyboard():
    """Build 3×3 matrix keyboard with combo buttons between sections."""
    if not MULTI_TIER_ENABLED or not TIER_OFFERS:
        return None  # Use default single-tier layout
    
    keyboard = []
    tier_ids = sorted(TIER_OFFERS.keys())
    
    # Build 3×3 matrix (3 tiers per row)
    row_number = 0
    for i in range(0, len(tier_ids), 3):
        row_number += 1
        row = []
        
        # Add up to 3 buttons per row
        for j in range(3):
            if i + j < len(tier_ids):
                tier_id = tier_ids[i + j]
                tier_info = TIER_OFFERS[tier_id]
                btn_text = f"{tier_info['emoji']}\n{tier_info['label']}\n₹{tier_info['price']}"
                row.append(
                    InlineKeyboardButton(
                        btn_text,
                        callback_data=f"buy_tier:{tier_id}"
                    )
                )
        
        # Add this row to keyboard
        if row:
            keyboard.append(row)
        
        # Insert combo button after this row (if configured)
        for combo_id in sorted(COMBO_OFFERS.keys()):
            combo_info = COMBO_OFFERS[combo_id]
            if combo_info.get("position") == row_number:
                combo_text = f"{combo_info['emoji']} {combo_info['label']}\n₹{combo_info['price']}"
                keyboard.append([
                    InlineKeyboardButton(
                        combo_text,
                        callback_data=f"buy_combo:{combo_id}"
                    )
                ])
    
    # Add bundle offers button if configured
    if os.getenv("BUNDLE_1"):
        keyboard.append([
            InlineKeyboardButton("🎁 See Bundle Offers", callback_data="show_bundles")
        ])
    
    # Add back button
    keyboard.append([
        InlineKeyboardButton("🔙 Back", callback_data="main_menu")
    ])
    
    return InlineKeyboardMarkup(keyboard)

def build_single_tier_keyboard():
    """Build default single-tier keyboard (₹99 entry point)."""
    keyboard = [
        [InlineKeyboardButton("⭐ Enjoy 15+ Channels — ₹99", callback_data="buy_tier:1")],
        [InlineKeyboardButton("🎁 See Bundle Offers", callback_data="show_bundles")],
    ]
    return InlineKeyboardMarkup(keyboard)


async def cmd_start(update, context):
    user = update.effective_user

    # Check if user is blocked — ignore silently
    if is_blocked(user.id):
        return

    upsert_user(user)

    # ── Session guard — runs for ALL users including sleep mode ────
    # Must run before sleep check so stale jobs from prior /start
    # are always cancelled regardless of bot state.
    cancel_all_user_jobs(context, user.id)
    bump_session_gen(user.id)

    # ── Wipe ALL prior messages before doing anything ──────────────
    # This ensures no stale menu/maintenance/approval messages linger
    # regardless of which flow runs next.
    _prior_ids = set(get_tracked_msgs(user.id))
    with db() as conn:
        _u = conn.execute(
            "SELECT menu_msg_id FROM users WHERE user_id=?", (user.id,)
        ).fetchone()
        if _u and _u["menu_msg_id"]:
            _prior_ids.add(_u["menu_msg_id"])
        _pmsgs = conn.execute(
            "SELECT main_msg_id FROM purchases WHERE user_id=?", (user.id,)
        ).fetchall()
        for _pm in _pmsgs:
            if _pm["main_msg_id"]:
                _prior_ids.add(_pm["main_msg_id"])
    for _mid in _prior_ids:
        await safe_delete(context, user.id, _mid)
    with db() as conn:
        conn.execute(
            "UPDATE users SET menu_msg_id=NULL, tracked_msgs='[]' WHERE user_id=?",
            (user.id,))
        conn.execute(
            "UPDATE purchases SET main_msg_id=NULL WHERE user_id=?",
            (user.id,))

    # Sleep / maintenance mode
    if is_sleep_mode() and user.id != ADMIN_ID:
        log_sleep_visitor(user)
        log.info(f"Sleep mode: recorded visitor {user.id}")

        maint_msg = get_maintenance_message()
        return_line = (
            f"🕐 <b>{maint_msg}</b>" if maint_msg
            else "🕐 Please check back later."
        )

        with db() as conn:
            owned_purchases = conn.execute(
                "SELECT DISTINCT channel_id, amount FROM purchases "
                "WHERE user_id=? AND status='approved'",
                (user.id,)
            ).fetchall()

        owned_channel_ids = {
            row["channel_id"] for row in owned_purchases
            if row["channel_id"] and row["channel_id"] > 0
        }
        owned_bundle_prices = set()
        for row in owned_purchases:
            if row["channel_id"] == 0 and row["amount"]:
                try:
                    owned_bundle_prices.add(int(row["amount"]))
                except (ValueError, TypeError):
                    pass

        if not owned_channel_ids and not owned_bundle_prices:
            # New / unpaid user — maintenance message only, no buttons
            text = (
                f"🔧 <b>Under Maintenance</b>\n\n"
                f"Hi {user.first_name}!\n\n"
                f"Our service is temporarily unavailable.\n\n"
                f"{return_line}"
            )
            try:
                m = await send_and_autodelete(
                    context, user.id, text,
                    delay=300,
                    parse_mode=ParseMode.HTML,
                    disable_notification=True,
                )
            except Exception as e:
                log.debug(f"maintenance msg send failed: {e}")
            return

        # Paid user — show only purchased channels
        kb_rows = []
        for c in get_channels():
            if c["id"] in owned_channel_ids:
                kb_rows.append([InlineKeyboardButton(
                    f"✅ {c['name']}", url=c["link"]
                )])
        for price in sorted(owned_bundle_prices):
            if price in BUNDLES:
                bundle = BUNDLES[price]
                kb_rows.append([InlineKeyboardButton(
                    f"✅ {bundle['name']}", url=bundle["link"]
                )])

        text = (
            f"🔧 <b>Maintenance Mode</b>\n\n"
            f"Hi {user.first_name}! The bot is under maintenance.\n"
            f"You can still access your purchased channels below.\n\n"
            f"{return_line}"
        )
        try:
            m = await send_and_autodelete(
                context, user.id, text,
                delay=300,
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(kb_rows) if kb_rows else None,
                protect_content=True,
                disable_notification=True,
            )
            with db() as conn:
                conn.execute(
                    "UPDATE users SET menu_msg_id=? WHERE user_id=?",
                    (m.message_id, user.id)
                )
        except Exception as e:
            log.debug(f"maintenance paid msg failed: {e}")
        return

    reset_inactivity_timer(context, user.id)

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
    
    # paid_t1 = user owns ANY channel — never position-dependent
    # Position changes must never affect access detection
    paid_t1 = bool(owned_channel_ids) or bool(owned_bundle_prices)

    if not CHANNELS:
        log.error(
            f"cmd_start: no channels for user {user.id}. "
            f"ENV channels: {len(_ENV_CHANNELS)}, "
            f"DB channels: {len(get_channels())}"
        )
        m = await context.bot.send_message(
            user.id,
            "⚠️ No channels available right now. Please try again shortly.",
            protect_content=True,
            disable_notification=True,
        )
        track_msg(user.id, m.message_id)
        return

    rows = []
    
    if not paid_t1:
        # ====== UNPAID USER FLOW ======
        if MULTI_TIER_ENABLED and TIER_OFFERS:
            # ========== MULTI-TIER 3×3 MATRIX WITH COMBOS ==========
            tier_ids = sorted(TIER_OFFERS.keys())
            row_number = 0
            for i in range(0, len(tier_ids), 3):
                row_number += 1
                row = []
                for j in range(3):
                    if i + j < len(tier_ids):
                        tier_id = tier_ids[i + j]
                        tier_info = TIER_OFFERS[tier_id]
                        btn_text = (f"{tier_info['emoji']}\n"
                                    f"{tier_info['label']}\n"
                                    f"₹{tier_info['price']}")
                        row.append(InlineKeyboardButton(
                            btn_text, callback_data=f"buy_tier:{tier_id}"
                        ))
                if row:
                    rows.append(row)
                for combo_id in sorted(COMBO_OFFERS.keys()):
                    combo_info = COMBO_OFFERS[combo_id]
                    if combo_info.get("position") == row_number:
                        combo_text = (f"{combo_info['emoji']} "
                                      f"{combo_info['label']}\n"
                                      f"₹{combo_info['price']}")
                        rows.append([InlineKeyboardButton(
                            combo_text, callback_data=f"buy_combo:{combo_id}"
                        )])
            intro = (f"👋 <b>Hi {user.first_name}!</b>\n\n"
                     f"<b>Choose Your Entry Point</b>\n\n"
                     f"Select the tier that works best for you:")

        else:
            # ========== DEFAULT MODE — use build_channel_buttons ==========
            # Respects position, group_label, separator_after from DB
            if is_tier_gate_enabled():
                intro = (f"👋 <b>Hi {user.first_name}!</b>\n\n"
                         f"<b>Get started below:</b>")
            else:
                intro = (f"👋 <b>Hi {user.first_name}!</b>\n\n"
                         f"<b>Choose any channel to get started:</b>")

            rows.extend(build_channel_buttons(
                owned_channel_ids=set(),
                is_paid=False,
                unpaid_mode=True,
            ))

        # Fallback bundles (if enabled and not multi-tier)
        if is_fallback_enabled() and not (MULTI_TIER_ENABLED and TIER_OFFERS):
            rows.append([InlineKeyboardButton(
                "🎁 See Budget Bundles", callback_data="fallback_menu"
            )])

    else:
        # ====== TIER 1+ OWNER FLOW ======
        # Show: Owned channels + Owned bundles + Available upgrades
        owned_names = [
            c["name"] for c in get_channels()
            if c["id"] in owned_channel_ids
        ]
        intro = (
            f"👋 <b>Welcome back, {user.first_name}!</b>\n\n"
            f"<b>You have access to your purchased content.\n"
            f"Tap any ✅ button below to join.</b>"
        )
        
        # Channel buttons — position, group labels, separators from DB
        rows.extend(build_channel_buttons(
            owned_channel_ids=owned_channel_ids,
            is_paid=True,
            unpaid_mode=False,
        ))

        # Owned bundles
        for price in sorted(owned_bundle_prices):
            if price in BUNDLES:
                bundle = BUNDLES[price]
                rows.append([InlineKeyboardButton(
                    f"✅ {bundle['name']}", url=bundle["link"]
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
        # Wipe ALL old bot messages before sending fresh /start.
        # This covers: old menu, approval messages, QR messages,
        # verifying messages, admin messages — everything.
        # Safe for paid users because ✅ buttons are rebuilt from
        # DB queries, not from the old approval message.

        # 1. Delete stale menu message
        if existing_id:
            await safe_delete(context, user.id, existing_id)

        # 2. Delete all tracked messages (approval, QR, verifying, etc.)
        tracked = get_tracked_msgs(user.id)
        for mid in tracked:
            if mid != existing_id:  # already deleted above
                await safe_delete(context, user.id, mid)

        # 3. Delete any main_msg_id from purchases (approval messages)
        with db() as conn:
            purchase_msgs = conn.execute(
                "SELECT main_msg_id FROM purchases WHERE user_id=?",
                (user.id,)
            ).fetchall()
        for row in purchase_msgs:
            if row["main_msg_id"] and row["main_msg_id"] not in tracked \
                    and row["main_msg_id"] != existing_id:
                await safe_delete(context, user.id, row["main_msg_id"])

        # 4. Reset all message refs in DB
        with db() as conn:
            conn.execute(
                "UPDATE users SET menu_msg_id=NULL, tracked_msgs='[]' "
                "WHERE user_id=?", (user.id,))
            conn.execute(
                "UPDATE purchases SET main_msg_id=NULL WHERE user_id=?",
                (user.id,))

        # 5. Send fresh /start menu
        m = await context.bot.send_message(
            chat_id=user.id, text=intro, parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
            protect_content=True,
            disable_notification=True,
        )
        track_msg(user.id, m.message_id)
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

    # Block during maintenance
    if is_sleep_mode() and user.id != ADMIN_ID:
        maint_msg = get_maintenance_message()
        return_line = f" {maint_msg}" if maint_msg else ""
        await q.answer(
            f"🔧 Under maintenance.{return_line}",
            show_alert=True
        )
        return
    
    if is_blocked(user.id):
        return
    reset_inactivity_timer(context, user.id)
    
    # Bundle options: (display_name, price_rupees)
    bundles = [
        ("Premium Collection", 299),
    ]
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⭐ {name} — ₹{price}", callback_data=f"buy_bundle:{price}")]
        for name, price in bundles
    ] + [[InlineKeyboardButton("⬅️ Back", callback_data="back_to_start")]])
    
    text = (f"<b>💰 100% TRUSTED</b>\n\n"
            f"Mallu Premium Collection\n\n"
            f"• <b>Premium Updates</b> — ₹299\n\n"
            f"<i>Enjoy And Stay For More!!!</i>")
    
    try:
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except Exception as e:
        log.debug(f"fallback menu edit failed: {e}")


async def cb_buy_bundle(update, context):
    """User selected a bundle. Show payment QR with "I've Paid" button."""
    q = update.callback_query
    await q.answer()
    user = q.from_user

    # Block during maintenance
    if is_sleep_mode() and user.id != ADMIN_ID:
        maint_msg = get_maintenance_message()
        return_line = f" {maint_msg}" if maint_msg else ""
        await q.answer(
            f"🔧 Under maintenance.{return_line}",
            show_alert=True
        )
        return
    
    if is_blocked(user.id):
        return
    reset_inactivity_timer(context, user.id)
    
    try:
        bundle_price = int(q.data.split(":")[1])
    except (ValueError, IndexError):
        return
    
    # Pick QR code
    qr_db = pick_qr_code()
    bundle_channel = {"id": 0, "name": f"Bundle (₹{bundle_price})", "price": bundle_price}
    pid = create_purchase(user.id, bundle_channel, 0)

    if qr_db:
        update_purchase(pid, qr_code_id=qr_db["id"])
        qr_photo = qr_db["file_id"]
        qr_source = "file_id"
    else:
        qr_idx = pick_qr()
        qr_path = QR_FILES[qr_idx]
        update_purchase(pid, qr_used=qr_idx)
        if not os.path.exists(qr_path):
            await context.bot.send_message(
                user.id,
                "⚠️ QR code not available. Please contact admin.",
                disable_notification=True,
            )
            return
        qr_photo = qr_path
        qr_source = "file"
    
    # Delete the menu message (transitioning to QR flow)
    try:
        await q.message.delete()
    except Exception:
        pass
    with db() as conn:
        conn.execute("UPDATE users SET menu_msg_id=NULL WHERE user_id=?", (user.id,))
    await clear_tracked(context, user.id)
    
    # Step 1 + Step 2 sent back-to-back — no DB calls in between
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ I've Paid — Click Here", callback_data=f"upi:start:{pid}")
    ]])

    caption = (
        f"💳 <b>Pay ₹{bundle_price}</b> via UPI\n\n"
        f"📲 <b>STEP 1</b> — Tap image → top-right <b>⋮</b> → "
        f"<b>Share</b> → choose UPI app\n\n"
        f"✅ <b>STEP 2</b> — After paying, tap the button below 👇"
    )

    import asyncio

    async def _send_qr_with_retry(retries=3, delay=2):
        for attempt in range(retries):
            try:
                if qr_source == "file_id":
                    return await context.bot.send_photo(
                        chat_id=user.id, photo=qr_photo,
                        caption=caption, parse_mode=ParseMode.HTML,
                        reply_markup=kb,
                        disable_notification=True,
                        read_timeout=30,
                        write_timeout=30,
                        connect_timeout=30,
                    )
                else:
                    with open(qr_photo, "rb") as fh:
                        return await context.bot.send_photo(
                            chat_id=user.id, photo=fh,
                            caption=caption, parse_mode=ParseMode.HTML,
                            reply_markup=kb,
                            disable_notification=True,
                            read_timeout=30,
                            write_timeout=30,
                            connect_timeout=30,
                        )
            except Exception as e:
                log.warning(
                    f"send_photo attempt {attempt+1}/{retries} failed: {e}"
                )
                if attempt < retries - 1:
                    await asyncio.sleep(delay)
                else:
                    raise
        return None

    qr_msg = await _send_qr_with_retry()
    if not qr_msg:
        m = await context.bot.send_message(
            user.id,
            "⚠️ Failed to send QR. Please tap /start and try again.",
            disable_notification=True,
        )
        track_msg(user.id, m.message_id)
        return

    track_msg(user.id, qr_msg.message_id)
    update_purchase(pid, main_msg_id=qr_msg.message_id,
                    qr_downloaded_at=datetime.utcnow().isoformat())

    # DB updates after both messages are delivered
    track_msg(user.id, qr_msg.message_id)
    track_msg(user.id, pay_msg.message_id)
    update_purchase(pid, main_msg_id=pay_msg.message_id,
                    qr_downloaded_at=datetime.utcnow().isoformat())

    # Schedule QR expiry on the QR photo message
    if QR_EXPIRY_MINUTES > 0:
        context.job_queue.run_once(
            expire_qr,
            when=timedelta(minutes=QR_EXPIRY_MINUTES),
            data={"user_id": user.id, "purchase_id": pid,
                  "qr_msg_id": qr_msg.message_id},
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
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
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


async def cb_noop(update, context):
    """No-op callback for label/separator buttons — just dismiss the alert."""
    await update.callback_query.answer()


# ==================================================================
# USER: tap channel button
# ==================================================================
async def cb_buy(update, context):
    q = update.callback_query
    await q.answer()
    user = q.from_user
    cid = int(q.data.split(":")[1])

    # Block purchases during maintenance
    if is_sleep_mode() and user.id != ADMIN_ID:
        maint_msg = get_maintenance_message()
        return_line = f" {maint_msg}" if maint_msg else ""
        await q.answer(
            f"🔧 Under maintenance.{return_line}",
            show_alert=True
        )
        return

    channel = next((c for c in CHANNELS if c["id"] == cid), None)
    if not channel:
        return

    # Hard ownership guard — catches stale buttons on old messages.
    # If user already owns this channel, never show QR regardless of
    # which message/button they tapped.
    owned_check = get_owned_channel_ids(user.id)
    if cid in owned_check:
        await q.answer(
            "✅ You already own this! Tap the green ✅ button to join.",
            show_alert=True
        )
        # Refresh the menu so stale buy: buttons become ✅ url buttons
        fresh_rows = build_channel_buttons(
            owned_channel_ids=owned_check,
            is_paid=True,
            unpaid_mode=False,
        )
        # Try to update whichever message the user tapped
        try:
            await q.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup(fresh_rows)
            )
        except Exception as e:
            log.debug(f"owned channel menu refresh failed: {e}")
            # If that message can't be edited (e.g. it's old), send a
            # fresh /start menu instead
            await cmd_start(update, context)
        return

    # Already owned — answer with popup only, no extra button needed.
    # (kept for belt-and-suspenders; the block above covers this)
    if cid in owned_check:
        return
    # build_channel_buttons gives owned channels a url= button so cb_buy
    # should rarely fire for owned channels — but handle it gracefully.
    if cid in get_owned_channel_ids(user.id):
        await q.answer(
            "✅ You already have access! Use the green tick button to join.",
            show_alert=True
        )
        # Rebuild the menu in-place so owned channel shows ✅ url button
        owned_now = get_owned_channel_ids(q.from_user.id)
        fresh_rows = build_channel_buttons(
            owned_channel_ids=owned_now,
            is_paid=True,
            unpaid_mode=False,
        )
        try:
            await q.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup(fresh_rows)
            )
        except Exception as e:
            log.debug(f"owned channel menu refresh failed: {e}")
        return

    # TIER 1 MANDATORY CHECK: only enforce if gate is enabled
    if (is_tier_gate_enabled()
            and cid != CHANNELS[0]["id"]
            and not has_paid_tier1(user.id)):
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

    # Try DB QR codes first, fall back to env var file paths
    qr_db = pick_qr_code()
    pid = create_purchase(user.id, channel, 0)

    if qr_db:
        # DB-managed QR — use Telegram file_id directly
        update_purchase(pid, qr_code_id=qr_db["id"])
        qr_photo = qr_db["file_id"]
        qr_source = "file_id"
    else:
        # Fallback to env var QR files
        qr_idx = pick_qr()
        qr_path = QR_FILES[qr_idx]
        update_purchase(pid, qr_used=qr_idx)
        if not os.path.exists(qr_path):
            m = await context.bot.send_message(
                user.id,
                "⚠️ Payment QR not configured. Please contact admin.",
                protect_content=True,
                disable_notification=True,
            )
            track_msg(user.id, m.message_id)
            try:
                await context.bot.send_message(
                    ADMIN_ID,
                    f"⚠️ No QR codes in DB and file missing at {qr_path}. "
                    f"Use /qr_add to upload a QR."
                )
            except Exception:
                pass
            return
        qr_photo = qr_path
        qr_source = "file"

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
    # Step 1 — QR photo only, no button
    # Step 2 — Separate message with button
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ I've Paid — Click Here", callback_data=f"upi:start:{pid}")
    ]])

    caption = (
        f"💳 <b>Pay ₹{channel['price']}</b> via UPI\n\n"
        f"📲 <b>STEP 1</b> — Tap image → top-right <b>⋮</b> → "
        f"<b>Share</b> → choose UPI app\n\n"
        f"✅ <b>STEP 2</b> — After paying, tap the button below 👇"
    )

    import asyncio

    async def _send_qr_with_retry(retries=3, delay=2):
        for attempt in range(retries):
            try:
                if qr_source == "file_id":
                    return await context.bot.send_photo(
                        chat_id=user.id, photo=qr_photo,
                        caption=caption, parse_mode=ParseMode.HTML,
                        reply_markup=kb,
                        disable_notification=True,
                        read_timeout=30,
                        write_timeout=30,
                        connect_timeout=30,
                    )
                else:
                    with open(qr_photo, "rb") as fh:
                        return await context.bot.send_photo(
                            chat_id=user.id, photo=fh,
                            caption=caption, parse_mode=ParseMode.HTML,
                            reply_markup=kb,
                            disable_notification=True,
                            read_timeout=30,
                            write_timeout=30,
                            connect_timeout=30,
                        )
            except Exception as e:
                log.warning(
                    f"send_photo attempt {attempt+1}/{retries} failed: {e}"
                )
                if attempt < retries - 1:
                    await asyncio.sleep(delay)
                else:
                    raise
        return None

    qr_msg = await _send_qr_with_retry()
    if not qr_msg:
        m = await context.bot.send_message(
            user.id,
            "⚠️ Failed to send QR. Please tap /start and try again.",
            disable_notification=True,
        )
        track_msg(user.id, m.message_id)
        return

    track_msg(user.id, qr_msg.message_id)
    update_purchase(pid, main_msg_id=qr_msg.message_id,
                    qr_downloaded_at=datetime.utcnow().isoformat())

    # Schedule QR expiry on the QR photo message
    if QR_EXPIRY_MINUTES > 0:
        context.job_queue.run_once(
            expire_qr,
            when=timedelta(minutes=QR_EXPIRY_MINUTES),
            data={"user_id": user.id, "purchase_id": pid,
                  "qr_msg_id": qr_msg.message_id},
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
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
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
        InlineKeyboardButton("✅✅✅ I've Paid ✅✅✅",
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
    reset_inactivity_timer(context, user.id)

    # Delete the QR photo — user has paid, no need to keep it
    try:
        await q.message.delete()
    except Exception as e:
        log.debug(f"QR delete failed: {e}")

    proof_mode = get_proof_mode()

    # proof_mode == "none" — skip proof entirely, notify admin to manually approve
    if proof_mode == "none":
        update_purchase(pid, status="verifying",
                        upi_submitted_at=datetime.utcnow().isoformat())

        m = await context.bot.send_message(
            chat_id=user.id,
            text=(
                "✅ <b>PAYMENT RECORDED</b>\n\n"
                "<i>Admin will verify and approve shortly.\n"
                "Please wait here.</i>"
            ),
            parse_mode=ParseMode.HTML,
            protect_content=True,
            disable_notification=True,
        )
        update_purchase(pid, main_msg_id=m.message_id)
        track_msg(user.id, m.message_id)

        # Notify admin with approve/reject buttons
        u_row = get_user_row(user.id)
        p_row = get_purchase(pid)
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    f"💸 <b>Payment Claimed (No Proof)</b>\n\n"
                    f"{fmt_user_block(u_row, p_row)}\n\n"
                    f"⚠️ Proof collection is OFF — verify manually."
                ),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=admin_action_kb(pid),
            )
        except Exception as e:
            log.error(f"Admin no-proof notify failed: {e}")
        return

    # proof_mode == "upi" — UPI name only, skip choice screen
    if proof_mode == "upi":
        AWAITING_UPI[user.id] = pid
        m = await context.bot.send_message(
            chat_id=user.id,
            text=(
                "✍️ <b>UPI NAME?</b>\n\n"
                "Type the name on your UPI account.\n\n"
                "Example: <b>Sakshi</b>"
            ),
            parse_mode=ParseMode.HTML,
            protect_content=True,
            disable_notification=True,
        )
        update_purchase(pid, main_msg_id=m.message_id)
        track_msg(user.id, m.message_id)
        return

    # proof_mode == "screenshot" — screenshot only, skip choice screen
    if proof_mode == "screenshot":
        update_purchase(pid, status="screenshot_requested")
        m = await context.bot.send_message(
            chat_id=user.id,
            text=(
                "📸 <b>SEND SCREENSHOT</b>\n\n"
                "Send your payment success screenshot here."
            ),
            parse_mode=ParseMode.HTML,
            protect_content=True,
            disable_notification=True,
        )
        update_purchase(pid, main_msg_id=m.message_id)
        track_msg(user.id, m.message_id)
        return

    # proof_mode == "both" — show choice screen (original behaviour)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 Send UPI Name",
                              callback_data=f"proof:name:{pid}")],
        [InlineKeyboardButton("📸 Send Screenshot",
                              callback_data=f"proof:shot:{pid}")],
    ])
    m = await context.bot.send_message(
        chat_id=user.id,
        text="✅ <b>HOW TO VERIFY?</b>\n\nChoose your verification method:",
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
    reset_inactivity_timer(context, user.id)

    if choice == "name":
        AWAITING_UPI[user.id] = pid
        await edit_main(context, user.id, q.message.message_id,
                        "💳 💰 <b>ENTER UPI NAME?</b>\n\n"
                        "Enter your correct UPI Name to verify!\n\n"
                        "Example: <b>Sakshi</b>")
    else:  # shot
        update_purchase(pid, status="screenshot_requested")
        await edit_main(context, user.id, q.message.message_id,
                        "📸 <b>SEND SCREENSHOT</b>\n\n"
                        "Send your payment success screenshot here.")


async def forward_user_message_to_admin(update, context, user):
    """Forward any unsolicited user message to admin with context + reply button."""
    text = update.message.text or ""
    if not text.strip():
        return

    # Log it
    log_user_message(user.id, "free_text", text)

    # Get latest purchase for context
    p = get_active_purchase(user.id)
    purchase_info = ""
    if p:
        purchase_info = (
            f"\n\n🛒 <b>Active Purchase</b>\n"
            f"• Channel : {p['channel_name']}\n"
            f"• Amount  : ₹{p['amount']}\n"
            f"• Status  : {p['status']}\n"
            f"• ID      : #{p['id']}"
        )
    else:
        # Check last purchase even if completed
        with db() as conn:
            lp = conn.execute("""
                SELECT * FROM purchases WHERE user_id=?
                ORDER BY id DESC LIMIT 1
            """, (user.id,)).fetchone()
        if lp:
            purchase_info = (
                f"\n\n🛒 <b>Last Purchase</b>\n"
                f"• Channel : {lp['channel_name']}\n"
                f"• Amount  : ₹{lp['amount']}\n"
                f"• Status  : {lp['status']}\n"
                f"• ID      : #{lp['id']}"
            )

    # Build user info
    name = f"{user.first_name or ''} {user.last_name or ''}".strip() or "—"
    un = f"@{user.username}" if user.username else "(no username)"

    admin_text = (
        f"💬 <b>User Message</b>\n\n"
        f"👤 {name} {un}\n"
        f"🆔 <code>{user.id}</code>"
        f"{purchase_info}\n\n"
        f"📩 <b>Message:</b>\n"
        f"{text}"
    )

    # Reply button for admin to quickly message back
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "💬 Reply to User",
            url=f"tg://user?id={user.id}"
        ),
    ]])

    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=admin_text,
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
            disable_web_page_preview=True,
        )
    except Exception as e:
        log.error(f"forward_user_message_to_admin failed: {e}")


async def on_text_message(update, context):
    user = update.effective_user

    # Check if user is blocked
    if is_blocked(user.id):
        return
    reset_inactivity_timer(context, user.id)

    if user.id not in AWAITING_UPI:
        # Check if it's a greeting — reply with /start prompt
        greetings = {
            "hi", "hello", "hey", "hii", "helo", "heyy", "helloo",
            "hai", "start", "begin", "help", "who are you", "what",
            "sup", "yo", "ello", "good morning", "good evening",
            "good afternoon", "gm", "ge", "ga", "bot", "?", "??",
        }
        msg_lower = update.message.text.strip().lower()
        if msg_lower in greetings or len(msg_lower) <= 4:
            try:
                m = await context.bot.send_message(
                    chat_id=user.id,
                    text=(
                        f"👋 <b>Hi {user.first_name}!</b>\n\n"
                        f"Tap the button below to get started 👇"
                    ),
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "🚀 Start", callback_data="trigger_start"
                        )
                    ]]),
                    disable_notification=True,
                )
                track_msg(user.id, m.message_id)
            except Exception as e:
                log.debug(f"greeting reply failed: {e}")
            return

        # Non-greeting free-text — forward to admin as before
        await forward_user_message_to_admin(update, context, user)
        return

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
                    "🚫 No Service! Come back after 24 hours.",
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
            away_m = await context.bot.send_message(
                chat_id=user.id,
                text=f"⏰ <b>ipepsi is currently Down</b>\n\n{away_msg}",
                parse_mode=ParseMode.HTML,
                disable_notification=True,
            )
            # Track so wipe/reset deletes it
            track_msg(user.id, away_m.message_id)
            # Auto-delete after 30 seconds
            schedule_auto_delete(context, user.id, away_m.message_id, delay_seconds=30)
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
    data = context.job.data
    # First check
    p = get_purchase(data["purchase_id"])
    if not p or p["status"] != "verifying":
        return
    # Second check right before the API call — if admin approved/rejected
    # between these two lines, this edit will either fail (message deleted)
    # or be immediately overwritten by reconfirm_approval job
    p = get_purchase(data["purchase_id"])
    if not p or p["status"] != "verifying":
        return
    # Only edit if message still exists (not deleted by approval flow)
    if not p.get("main_msg_id"):
        return
    if p["main_msg_id"] != data["msg_id"]:
        # Message was replaced by a new one — stop animating old message
        return
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
    reset_inactivity_timer(context, user.id)

    p = get_active_purchase(user.id)
    if not p or p["status"] != "screenshot_requested":
        # Photo sent outside screenshot flow — forward to admin
        file_id = update.message.photo[-1].file_id
        log_user_message(user.id, "unsolicited_photo", file_id)

        name = f"{user.first_name or ''} {user.last_name or ''}".strip() or "—"
        un = f"@{user.username}" if user.username else "(no username)"

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("💬 Reply to User", url=f"tg://user?id={user.id}")
        ]])

        try:
            await context.bot.send_photo(
                chat_id=ADMIN_ID,
                photo=file_id,
                caption=(
                    f"🖼 <b>User Sent a Photo</b>\n\n"
                    f"👤 {name} {un}\n"
                    f"🆔 <code>{user.id}</code>\n\n"
                    f"<i>Sent outside of screenshot flow.</i>"
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )
        except Exception as e:
            log.error(f"forward unsolicited photo failed: {e}")
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

async def on_admin_media(update, context):
    """Handle media (photo/video/document) sent by admin.
    
    For broadcast:
        Send media to admin bot with caption:
        /broadcast [segment] optional text
        
    For msg to one user:
        Send media to admin bot with caption:
        /msg 123456789 optional text
    """
    user = update.effective_user
    if user.id != ADMIN_ID:
        return

    msg = update.message
    caption = (msg.caption or "").strip()

    if not caption:
        await msg.reply_text(
            "Add a caption to use this media:\n\n"
            "<code>/broadcast [segment] text</code>\n"
            "<code>/msg user_id text</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    # Parse first word as command
    parts = caption.split(maxsplit=2)
    command = parts[0].lower().lstrip("/")

    # ── /qr_add <name> [priority] ────────────────────────────────────
    if command == "qr_add":
        if not msg.photo:
            await msg.reply_text("Send a photo with caption /qr_add <name>")
            return

        name_parts = parts[1:]
        priority = 1
        if name_parts and name_parts[-1].isdigit():
            priority = int(name_parts[-1])
            name = " ".join(name_parts[:-1]) or "QR"
        else:
            name = " ".join(name_parts) if name_parts else "QR"

        file_id = msg.photo[-1].file_id

        with db() as conn:
            max_pos = conn.execute(
                "SELECT COALESCE(MAX(position), 0) m FROM qr_codes"
            ).fetchone()["m"]
            cur = conn.execute("""
                INSERT INTO qr_codes (name, file_id, priority, position)
                VALUES (?, ?, ?, ?)
            """, (name, file_id, priority, max_pos + 1))
            new_id = cur.lastrowid

        await msg.reply_html(
            f"✅ <b>QR Code Added</b>\n\n"
            f"ID       : <code>{new_id}</code>\n"
            f"Name     : {name}\n"
            f"Priority : {priority}\n\n"
            f"<i>Use /qr_list to see all QRs.\n"
            f"Use /qr_mode to change routing.</i>"
        )
        return

    # ── /msg user_id [text] ──────────────────────────────────────────
    if command == "msg":
        if len(parts) < 2:
            await msg.reply_text("Usage in caption: /msg <user_id> [text]")
            return
        try:
            target_id = int(parts[1])
        except ValueError:
            await msg.reply_text("⚠️ Invalid user_id.")
            return

        extra_text = parts[2] if len(parts) > 2 else ""
        full_caption = f"📩 <b>Message from Admin</b>"
        if extra_text:
            full_caption += f"\n\n{extra_text}"

        try:
            if msg.photo:
                sent = await context.bot.send_photo(
                    chat_id=target_id,
                    photo=msg.photo[-1].file_id,
                    caption=full_caption,
                    parse_mode=ParseMode.HTML,
                    disable_notification=True,
                )
            elif msg.video:
                sent = await context.bot.send_video(
                    chat_id=target_id,
                    video=msg.video.file_id,
                    caption=full_caption,
                    parse_mode=ParseMode.HTML,
                    disable_notification=True,
                )
            elif msg.document:
                sent = await context.bot.send_document(
                    chat_id=target_id,
                    document=msg.document.file_id,
                    caption=full_caption,
                    parse_mode=ParseMode.HTML,
                    disable_notification=True,
                )
            else:
                await msg.reply_text("Unsupported media type.")
                return

            track_msg(target_id, sent.message_id)
            await msg.reply_html(f"✅ Media sent to user <code>{target_id}</code>")

        except Exception as e:
            await msg.reply_html(f"❌ Failed: {e}")
        return

    # ── /broadcast [segment] [text] ──────────────────────────────────
    if command == "broadcast":
        KNOWN_SEGMENTS = {"all", "unpaid"}

        def is_segment(word):
            import re
            return word.lower() in KNOWN_SEGMENTS or bool(
                re.match(r'^T\d+(,T\d+)*$', word, re.IGNORECASE)
            )

        second = parts[1] if len(parts) > 1 else ""
        if is_segment(second):
            segment = second.lower()
            extra_text = parts[2] if len(parts) > 2 else ""
        else:
            segment = "all"
            extra_text = " ".join(parts[1:])

        # Get blocked IDs
        with db() as conn:
            blocked_ids = {r["user_id"] for r in
                           conn.execute("SELECT user_id FROM blocked_users").fetchall()}
            all_users = conn.execute("SELECT user_id FROM users").fetchall()
            purchases = conn.execute(
                "SELECT user_id, channel_id FROM purchases WHERE status='approved'"
            ).fetchall()

        tier_map = {}
        for p in purchases:
            uid = p["user_id"]
            if uid not in tier_map:
                tier_map[uid] = set()
            tier_map[uid].add(p["channel_id"])

        if segment == "all":
            target_ids = [u["user_id"] for u in all_users]
        elif segment == "unpaid":
            target_ids = [u["user_id"] for u in all_users
                          if u["user_id"] not in tier_map]
        else:
            try:
                required_tiers = {
                    int(t.replace("T", "").replace("t", ""))
                    for t in segment.split(",")
                }
                target_ids = [
                    u["user_id"] for u in all_users
                    if any(t in tier_map.get(u["user_id"], set())
                           for t in required_tiers)
                ]
            except ValueError:
                await msg.reply_text("Invalid segment.")
                return

        target_ids = [uid for uid in target_ids if uid not in blocked_ids]

        if not target_ids:
            await msg.reply_html(
                f"No eligible users in segment <b>{segment}</b>."
            )
            return

        await msg.reply_html(
            f"📢 Broadcasting media to <b>{len(target_ids)}</b> users "
            f"(segment: <b>{segment}</b>)…"
        )

        full_caption = "📢 <b>Announcement</b>"
        if extra_text:
            full_caption += f"\n\n{extra_text}"

        sent = failed = 0
        for uid in target_ids:
            try:
                if msg.photo:
                    m = await context.bot.send_photo(
                        chat_id=uid,
                        photo=msg.photo[-1].file_id,
                        caption=full_caption,
                        parse_mode=ParseMode.HTML,
                        disable_notification=True,
                    )
                elif msg.video:
                    m = await context.bot.send_video(
                        chat_id=uid,
                        video=msg.video.file_id,
                        caption=full_caption,
                        parse_mode=ParseMode.HTML,
                        disable_notification=True,
                    )
                elif msg.document:
                    m = await context.bot.send_document(
                        chat_id=uid,
                        document=msg.document.file_id,
                        caption=full_caption,
                        parse_mode=ParseMode.HTML,
                        disable_notification=True,
                    )
                else:
                    break
                track_msg(uid, m.message_id)
                sent += 1
            except Exception as e:
                log.debug(f"media broadcast to {uid} failed: {e}")
                failed += 1

        await msg.reply_html(
            f"✅ <b>Media broadcast complete.</b>\n"
            f"Segment : <b>{segment}</b>\n"
            f"Sent    : {sent}\n"
            f"Failed  : {failed}\n"
            f"Skipped : {len(blocked_ids)} blocked"
        )
        return

    await msg.reply_text(
        "Unknown command in caption. Use /broadcast or /msg."
    )

# ==================================================================
# ADMIN: approve / reject / request screenshot
# ==================================================================
def build_reject_kb(user_id, p):
    """Build rejection keyboard fresh from DB — retry button for the
    rejected channel, join buttons for owned, locked for others."""
    owned_now = get_owned_channel_ids(user_id)
    owns_tier1 = (CHANNELS and CHANNELS[0]["id"] in owned_now)
    kb_rows = []
    for c in CHANNELS:
        if c["id"] in owned_now:
            kb_rows.append([InlineKeyboardButton(
                f"✅ {c['name']} — Join", url=c["link"]
            )])
        elif c["id"] == p["channel_id"]:
            # The rejected channel — show retry for that specific tier
            kb_rows.append([InlineKeyboardButton(
                f"🔁 Try Again — {c['name']} ₹{c['price']}",
                callback_data=f"buy:{c['id']}",
            )])
        elif owns_tier1:
            kb_rows.append([InlineKeyboardButton(
                f"⭐ {c['name']} — ₹{c['price']}",
                callback_data=f"buy:{c['id']}",
            )])
    return InlineKeyboardMarkup(kb_rows)


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
        # Gather ALL known bot message IDs (tracked + menu_msg_id + main_msg_id)
        all_ids = set()
        all_ids.update(get_tracked_msgs(target_user_id))
        with db() as conn:
            u = conn.execute(
                "SELECT menu_msg_id FROM users WHERE user_id=?",
                (target_user_id,)
            ).fetchone()
            if u and u["menu_msg_id"]:
                all_ids.add(u["menu_msg_id"])
            purchases = conn.execute(
                "SELECT main_msg_id FROM purchases WHERE user_id=?",
                (target_user_id,)
            ).fetchall()
            for p in purchases:
                if p["main_msg_id"]:
                    all_ids.add(p["main_msg_id"])
        for mid in all_ids:
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

    # Block acting on already-closed purchases
    if action in ("approve", "reject", "reqss") and \
            p["status"] in ("approved", "rejected"):
        await q.answer(
            f"Already {p['status']} — no action taken.",
            show_alert=True
        )
        return

    user_id = p["user_id"]
    channel = next((c for c in CHANNELS if c["id"] == p["channel_id"]), None)

    if action == "approve":
        update_purchase(pid, status="approved",
                        approved_at=datetime.utcnow().isoformat())
        cancel_inactivity_timer(context, user_id)

        # Cancel all pending animation jobs for this purchase
        for i in range(8):
            for job in context.job_queue.get_jobs_by_name(f"anim_{user_id}_{pid}_{i}"):
                job.schedule_removal()

        # Build keyboard:
        #  ✅ Just-approved channel → Join URL button (single green tick)
        #  ⭐ Unowned channels → buy callback buttons
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
            all_owned = get_owned_channel_ids(user_id)
            kb_rows.extend(build_channel_buttons(
                owned_channel_ids=all_owned,
                is_paid=True,
                unpaid_mode=False,
            ))
        
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

        # Step 1 — Cancel ALL animation jobs before touching the message
        for i in range(8):
            for job in context.job_queue.get_jobs_by_name(
                    f"anim_{user_id}_{pid}_{i}"):
                job.schedule_removal()

        # Step 2 — Delete the verifying message entirely instead of editing.
        # Editing is unreliable when an in-flight animation coroutine may
        # complete after us and overwrite the approval. Deletion is atomic
        # and guarantees the VERIFYING message is gone permanently.
        if p.get("main_msg_id"):
            await safe_delete(context, user_id, p["main_msg_id"])
            update_purchase(pid, main_msg_id=None)

        # Step 3 — Send a brand-new approval message that can never be
        # overwritten because the old message_id no longer exists.
        delivered = False
        try:
            m = await context.bot.send_message(
                chat_id=user_id,
                text=approval_text,
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
                protect_content=True,
                disable_notification=True,
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

        # Step 4 — Re-confirm at 5s and 12s to catch any edge case where
        # an animation coroutine was already mid-flight before cancellation.
        # Uses the new message_id so it can never hit the deleted message.
        async def reconfirm_approval(ctx):
            rp = get_purchase(pid)
            if not rp or rp["status"] != "approved":
                return
            if not rp.get("main_msg_id"):
                return
            # Rebuild keyboard fresh from DB
            all_owned = get_owned_channel_ids(user_id)
            fresh_rows = []
            if rp["channel_id"] == 0:
                bp = rp["amount"]
                if bp in BUNDLES:
                    fresh_rows.append([InlineKeyboardButton(
                        f"✅ {BUNDLES[bp]['name']}", url=BUNDLES[bp]["link"]
                    )])
            else:
                for c in CHANNELS:
                    if c["id"] in all_owned:
                        fresh_rows.append([InlineKeyboardButton(
                            f"✅ {c['name']}", url=c["link"]
                        )])
                    else:
                        fresh_rows.append([InlineKeyboardButton(
                            f"🔒 {c['name']} — ₹{c['price']}",
                            callback_data=f"buy:{c['id']}",
                        )])
            await edit_main(
                ctx, user_id, rp["main_msg_id"],
                approval_text,
                reply_markup=InlineKeyboardMarkup(fresh_rows),
            )

        context.job_queue.run_once(
            reconfirm_approval, when=5,
            name=f"reconfirm_approve_{pid}_1"
        )
        context.job_queue.run_once(
            reconfirm_approval, when=12,
            name=f"reconfirm_approve_{pid}_2"
        )

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
                            "🎁 See Budget Bundles", callback_data="fallback_menu"
                        )])
                    intro = (f"👋 <b>Hi there!</b>\n\n"
                             f"<b>Get started with {c['name']} at ₹{price}</b>")
                else:
                    intro = f"👋 <b>Welcome back!</b>\n\n<b>You have access to your purchased content.</b>"

                    rows.extend(build_channel_buttons(
                        owned_channel_ids=owned_channel_ids,
                        is_paid=True,
                        unpaid_mode=False,
                    ))

                    for bp in sorted(owned_bundle_prices):
                        if bp in BUNDLES:
                            bundle = BUNDLES[bp]
                            rows.append([InlineKeyboardButton(
                                f"✅ {bundle['name']}", url=bundle["link"]
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
        cancel_inactivity_timer(context, user_id)

        # Cancel all pending animation jobs immediately
        for i in range(8):
            for job in context.job_queue.get_jobs_by_name(
                    f"anim_{user_id}_{pid}_{i}"):
                job.schedule_removal()

        rejection_text = "❌ <b>REJECTED</b>\n\n<i>Payment not verified.</i>"
        reject_kb = build_reject_kb(user_id, p)

        delivered = False
        if p.get("main_msg_id"):
            delivered = await edit_main(
                context, user_id, p["main_msg_id"],
                rejection_text, reply_markup=reject_kb,
            )

        # Fallback — edit failed, send a fresh message with keyboard
        if not delivered:
            try:
                m = await context.bot.send_message(
                    chat_id=user_id,
                    text=rejection_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reject_kb,
                    protect_content=True,
                    disable_notification=True,
                )
                track_msg(user_id, m.message_id)
                update_purchase(pid, main_msg_id=m.message_id)
                delivered = True
            except Exception as e:
                log.error(f"reject delivery failed: {e}")

        # Reconfirm at 10s — catches any in-flight animation that
        # slipped past cancellation and overwrote the rejection.
        # Rebuilds keyboard fresh from DB to avoid stale closure.
        async def reconfirm_rejection(ctx):
            rp = get_purchase(pid)
            if not rp or rp["status"] != "rejected":
                return
            if not rp.get("main_msg_id"):
                return
            fresh_kb = build_reject_kb(user_id, rp)
            await edit_main(
                ctx, user_id, rp["main_msg_id"],
                rejection_text, reply_markup=fresh_kb,
            )

        context.job_queue.run_once(
            reconfirm_rejection,
            when=10,
            name=f"reconfirm_reject_{pid}",
        )

        chat_link = f"tg://user?id={user_id}"
        await _edit_admin_card(q,
            extra=f"\n\n❌ <b>REJECTED</b> {datetime.now(TZ):%d-%b %H:%M}",
            extra_kb=InlineKeyboardMarkup([
                [InlineKeyboardButton("🚨 Open User Chat", url=chat_link)],
                [InlineKeyboardButton("🧹 Wipe Now",
                                       callback_data=f"adm:wipe:{user_id}")],
            ]))

        schedule_auto_wipe(context, user_id, AUTO_WIPE_MINUTES)
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
    """Admin: /reset <user_id> — full nuclear reset of a user."""
    if update.effective_user.id != ADMIN_ID:
        return

    args = context.args or []
    if not args:
        await update.message.reply_html(
            "Usage: <code>/reset &lt;user_id&gt;</code>\n\n"
            "Or use <code>/resetme</code> to reset your own account."
        )
        return

    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Invalid user_id. Must be a number.")
        return

    # Confirm user exists in DB
    with db() as conn:
        u = conn.execute(
            "SELECT user_id, first_name FROM users WHERE user_id=?",
            (target_id,)
        ).fetchone()

    if not u:
        await update.message.reply_html(
            f"⚠️ User <code>{target_id}</code> not found in DB.\n\n"
            f"Use <code>/find</code> to search by name."
        )
        return

    name = u["first_name"] or f"ID {target_id}"
    progress = await update.message.reply_html(
        f"🔄 Resetting <b>{name}</b> (<code>{target_id}</code>)…"
    )

    try:
        await full_reset(context, target_id)
        await progress.edit_text(
            f"✅ <b>Reset complete</b> for {name} "
            f"(<code>{target_id}</code>).\n\n"
            f"All messages deleted, all DB state cleared.\n"
            f"User can /start fresh.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        log.error(f"cmd_reset failed for {target_id}: {e}")
        await progress.edit_text(
            f"❌ Reset failed for <code>{target_id}</code>: {e}",
            parse_mode=ParseMode.HTML,
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
    """Admin: /resetall DELETE-EVERYTHING — nuclear reset for ALL users."""
    if update.effective_user.id != ADMIN_ID:
        return

    args = context.args or []
    confirm = args and args[0].upper() == "DELETE-EVERYTHING"

    with db() as conn:
        user_count = conn.execute(
            "SELECT COUNT(*) c FROM users"
        ).fetchone()["c"]
        purchase_count = conn.execute(
            "SELECT COUNT(*) c FROM purchases"
        ).fetchone()["c"]

    if not confirm:
        await update.message.reply_html(
            f"☢️ <b>NUCLEAR RESET — ALL USERS</b>\n\n"
            f"This will permanently delete:\n"
            f"• Bot messages for <b>{user_count}</b> users\n"
            f"• <b>{purchase_count}</b> purchase records (incl. approved)\n\n"
            f"<b>Users will lose their access. They'll have to pay again.</b>\n\n"
            f"To confirm: <code>/resetall DELETE-EVERYTHING</code>"
        )
        return

    with db() as conn:
        rows = conn.execute(
            "SELECT user_id FROM users"
        ).fetchall()
    user_ids = [r["user_id"] for r in rows]

    progress = await update.message.reply_html(
        f"☢️ Resetting <b>{len(user_ids)}</b> users…\n"
        f"<i>This may take a while.</i>"
    )

    success = 0
    failed = 0
    for i, user_id in enumerate(user_ids):
        try:
            await full_reset(context, user_id)
            success += 1
        except Exception as e:
            log.error(f"resetall failed for {user_id}: {e}")
            failed += 1

        # Update progress every 10 users
        if (i + 1) % 10 == 0:
            try:
                await progress.edit_text(
                    f"☢️ Resetting… {i + 1}/{len(user_ids)}\n"
                    f"✅ {success} done | ❌ {failed} failed",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

        # Small delay every 5 users to avoid Telegram rate limits
        if (i + 1) % 5 == 0:
            import asyncio
            await asyncio.sleep(0.5)

    await progress.edit_text(
        f"☢️ <b>Reset complete.</b>\n\n"
        f"✅ Success : {success}\n"
        f"❌ Failed  : {failed}\n\n"
        f"All purchases deleted. DB is now clean.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_broadcast(update, context):
    """Admin: /broadcast [segment] <message>
    
    Segment is optional — defaults to 'all'.
    Blocked users are always skipped.
    
    Examples:
    /broadcast Hello everyone!                     → all users
    /broadcast unpaid Hey, still interested?       → unpaid users only
    /broadcast T1 Upgrade to Tier 2 now!          → Tier 1 owners only
    /broadcast all Big announcement!               → all users (explicit)
    """
    if update.effective_user.id != ADMIN_ID:
        return

    args = update.message.text.split(maxsplit=2)
    # args[0] = /broadcast

    if len(args) < 2:
        await update.message.reply_html(
            "Usage: <code>/broadcast [segment] &lt;message&gt;</code>\n\n"
            "Segment is optional (default: all)\n\n"
            "Segments:\n"
            "  <code>all</code> — everyone\n"
            "  <code>unpaid</code> — no approved purchases\n"
            "  <code>T1</code> — Tier 1 owners\n"
            "  <code>T1,T2</code> — Tier 1 or 2 owners\n\n"
            "Examples:\n"
            "<code>/broadcast Hey everyone!</code>\n"
            "<code>/broadcast unpaid Still interested?</code>\n"
            "<code>/broadcast T1 Upgrade now!</code>\n\n"
            "<i>Blocked users are always skipped.</i>"
        )
        return

    # Detect if second word is a known segment keyword
    KNOWN_SEGMENTS = {"all", "unpaid"}
    # Also detect T1, T1,T2, T1,T2,T3 patterns
    def is_segment(word):
        if word.lower() in KNOWN_SEGMENTS:
            return True
        # Match T1 / T1,T2 / T1,T2,T3 etc.
        import re
        return bool(re.match(r'^T\d+(,T\d+)*$', word, re.IGNORECASE))

    second_word = args[1] if len(args) > 1 else ""
    if is_segment(second_word):
        segment = second_word.lower()
        if len(args) < 3:
            await update.message.reply_html(
                f"⚠️ You specified segment <b>{segment}</b> but no message.\n\n"
                f"Usage: <code>/broadcast {segment} your message here</code>"
            )
            return
        text = args[2]
    else:
        segment = "all"
        text = " ".join(args[1:])

    # Get blocked user IDs to exclude
    with db() as conn:
        blocked_rows = conn.execute(
            "SELECT user_id FROM blocked_users"
        ).fetchall()
        blocked_ids = {r["user_id"] for r in blocked_rows}

    # Build target user list based on segment
    with db() as conn:
        all_users = conn.execute(
            "SELECT user_id FROM users"
        ).fetchall()
        purchases = conn.execute(
            "SELECT user_id, channel_id FROM purchases WHERE status='approved'"
        ).fetchall()

    # Build tier map
    tier_map = {}
    for p in purchases:
        uid = p["user_id"]
        if uid not in tier_map:
            tier_map[uid] = set()
        tier_map[uid].add(p["channel_id"])

    # Select users by segment
    if segment == "all":
        target_ids = [u["user_id"] for u in all_users]
    elif segment == "unpaid":
        target_ids = [u["user_id"] for u in all_users if u["user_id"] not in tier_map]
    else:
        # T1 / T1,T2 / T1,T2,T3 etc.
        try:
            required_tiers = {int(t.replace("T", "").replace("t", "")) for t in segment.split(",")}
            target_ids = [
                u["user_id"] for u in all_users
                if any(t in tier_map.get(u["user_id"], set()) for t in required_tiers)
            ]
        except ValueError:
            await update.message.reply_text("Invalid segment format.")
            return

    # Remove blocked users
    target_ids = [uid for uid in target_ids if uid not in blocked_ids]

    if not target_ids:
        await update.message.reply_html(
            f"No eligible users found in segment <b>{segment}</b> "
            f"(after removing blocked users)."
        )
        return

    await update.message.reply_html(
        f"📢 Broadcasting to <b>{len(target_ids)}</b> users "
        f"(segment: <b>{segment}</b>, blocked excluded)…"
    )

    sent = 0
    failed = 0
    for uid in target_ids:
        try:
            m = await context.bot.send_message(
                chat_id=uid,
                text=f"📢 <b>Announcement</b>\n\n{text}",
                parse_mode=ParseMode.HTML,
                disable_notification=True,
            )
            # Track so wipe/reset cleans it up later
            track_msg(uid, m.message_id)
            sent += 1
        except Exception as e:
            log.debug(f"broadcast to {uid} failed: {e}")
            failed += 1

    await update.message.reply_html(
        f"✅ <b>Broadcast complete.</b>\n"
        f"Segment : <b>{segment}</b>\n"
        f"Sent    : {sent}\n"
        f"Failed  : {failed} <i>(inactive / deactivated)</i>\n"
        f"Skipped : {len(blocked_ids)} blocked users"
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

async def cmd_approve(update, context):
    """Admin: /approve <user_id> [channel_id]
    
    Manually approve the latest pending purchase for a user.
    If channel_id is given, approves that specific channel's purchase.
    
    Examples:
    /approve 123456789          → approves latest pending purchase
    /approve 123456789 1        → approves pending purchase for channel 1
    /approve 123456789 pid:55   → approves by exact purchase ID
    """
    if update.effective_user.id != ADMIN_ID:
        return

    args = context.args or []
    if not args:
        await update.message.reply_html(
            "Usage: <code>/approve &lt;user_id&gt; [channel_id]</code>\n\n"
            "Examples:\n"
            "<code>/approve 123456789</code> — latest pending\n"
            "<code>/approve 123456789 1</code> — channel 1 pending\n"
            "<code>/approve 123456789 pid:55</code> — exact purchase ID"
        )
        return

    try:
        user_id = int(args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Invalid user_id.")
        return

    # Find the purchase
    p = None
    if len(args) >= 2:
        second = args[1]
        if second.startswith("pid:"):
            # Exact purchase ID
            try:
                pid = int(second.replace("pid:", ""))
                p = get_purchase(pid)
                if p and p["user_id"] != user_id:
                    await update.message.reply_text(
                        "⚠️ Purchase ID does not belong to that user."
                    )
                    return
            except ValueError:
                await update.message.reply_text("⚠️ Invalid purchase ID.")
                return
        else:
            # Channel ID — find latest pending for that channel
            try:
                channel_id = int(second)
                with db() as conn:
                    r = conn.execute("""
                        SELECT * FROM purchases
                        WHERE user_id=? AND channel_id=?
                        AND status NOT IN ('approved','rejected','cancelled')
                        ORDER BY id DESC LIMIT 1
                    """, (user_id, channel_id)).fetchone()
                p = dict(r) if r else None
            except ValueError:
                await update.message.reply_text("⚠️ Invalid channel_id.")
                return
    else:
        # Latest pending purchase for this user
        with db() as conn:
            r = conn.execute("""
                SELECT * FROM purchases
                WHERE user_id=? AND status NOT IN ('approved','rejected')
                ORDER BY id DESC LIMIT 1
            """, (user_id,)).fetchone()
        p = dict(r) if r else None

    if not p:
        await update.message.reply_html(
            f"⚠️ No pending purchase found for user <code>{user_id}</code>.\n\n"
            f"Use <code>/whoami {user_id}</code> to check their purchase history."
        )
        return

    pid = p["id"]

    # Step 1 — Cancel animation and inactivity jobs immediately
    for i in range(8):
        for job in context.job_queue.get_jobs_by_name(f"anim_{user_id}_{pid}_{i}"):
            job.schedule_removal()
    cancel_inactivity_timer(context, user_id)

    # Step 2 — Mark approved
    update_purchase(pid, status="approved",
                    approved_at=datetime.utcnow().isoformat())

    # Step 3 — Wipe entire user chat (QR, Step 2 button, verifying, etc.)
    all_ids = set()
    all_ids.update(get_tracked_msgs(user_id))
    with db() as conn:
        u_row_db = conn.execute(
            "SELECT menu_msg_id FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if u_row_db and u_row_db["menu_msg_id"]:
            all_ids.add(u_row_db["menu_msg_id"])
        all_purchases = conn.execute(
            "SELECT main_msg_id FROM purchases WHERE user_id=?", (user_id,)
        ).fetchall()
        for row in all_purchases:
            if row["main_msg_id"]:
                all_ids.add(row["main_msg_id"])
    for mid in all_ids:
        await safe_delete(context, user_id, mid)
    with db() as conn:
        conn.execute(
            "UPDATE users SET menu_msg_id=NULL, tracked_msgs='[]' "
            "WHERE user_id=?", (user_id,))
        conn.execute(
            "UPDATE purchases SET main_msg_id=NULL WHERE user_id=?",
            (user_id,))

    # Step 4 — Build fresh approval keyboard from DB
    # All owned channels → ✅ direct link, all unowned → 🔒 buy button
    all_owned = get_owned_channel_ids(user_id)
    kb_rows = []

    if p["channel_id"] == 0:
        bundle_price = p["amount"]
        if bundle_price in BUNDLES:
            bundle = BUNDLES[bundle_price]
            kb_rows.append([InlineKeyboardButton(
                f"✅ {bundle['name']}", url=bundle["link"]
            )])
    else:
        kb_rows.extend(build_channel_buttons(
            owned_channel_ids=all_owned,
            is_paid=True,
            unpaid_mode=False,
        ))

    kb = InlineKeyboardMarkup(kb_rows)
    approval_text = (
        f"🎉 <b>APPROVED</b>\n\n"
        f"✅ <b>{p.get('channel_name', 'Channel')}</b>\n\n"
        f"<b>You have access to your purchased content.</b>\n"
        f"Tap the button below to join 👇"
    )

    # Step 5 — Send guaranteed fresh approval message
    delivered = False
    try:
        m = await context.bot.send_message(
            chat_id=user_id,
            text=approval_text,
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
            protect_content=True,
            disable_notification=True,
        )
        track_msg(user_id, m.message_id)
        update_purchase(pid, main_msg_id=m.message_id)
        delivered = True
    except Exception as e:
        log.error(f"cmd_approve delivery failed: {e}")

    schedule_auto_wipe(context, user_id, AUTO_WIPE_MINUTES)
    await event_backup(context)

    await update.message.reply_html(
        f"✅ <b>Approved</b>\n\n"
        f"User    : <code>{user_id}</code>\n"
        f"Channel : {p['channel_name']}\n"
        f"Amount  : ₹{p['amount']}\n"
        f"Purchase: #{pid}\n"
        f"Delivered: {'✅ Yes' if delivered else '❌ Failed — user may need to /start'}"
    )


async def cmd_reject(update, context):
    """Admin: /reject <user_id> [channel_id]
    
    Manually reject the latest pending purchase for a user.
    
    Examples:
    /reject 123456789           → rejects latest pending purchase
    /reject 123456789 1         → rejects pending purchase for channel 1
    /reject 123456789 pid:55    → rejects by exact purchase ID
    """
    if update.effective_user.id != ADMIN_ID:
        return

    args = context.args or []
    if not args:
        await update.message.reply_html(
            "Usage: <code>/reject &lt;user_id&gt; [channel_id]</code>\n\n"
            "Examples:\n"
            "<code>/reject 123456789</code> — latest pending\n"
            "<code>/reject 123456789 1</code> — channel 1 pending\n"
            "<code>/reject 123456789 pid:55</code> — exact purchase ID"
        )
        return

    try:
        user_id = int(args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Invalid user_id.")
        return

    # Find the purchase
    p = None
    if len(args) >= 2:
        second = args[1]
        if second.startswith("pid:"):
            try:
                pid = int(second.replace("pid:", ""))
                p = get_purchase(pid)
                if p and p["user_id"] != user_id:
                    await update.message.reply_text(
                        "⚠️ Purchase ID does not belong to that user."
                    )
                    return
            except ValueError:
                await update.message.reply_text("⚠️ Invalid purchase ID.")
                return
        else:
            try:
                channel_id = int(second)
                with db() as conn:
                    r = conn.execute("""
                        SELECT * FROM purchases
                        WHERE user_id=? AND channel_id=?
                        AND status NOT IN ('approved','rejected','cancelled')
                        ORDER BY id DESC LIMIT 1
                    """, (user_id, channel_id)).fetchone()
                p = dict(r) if r else None
            except ValueError:
                await update.message.reply_text("⚠️ Invalid channel_id.")
                return
    else:
        with db() as conn:
            r = conn.execute("""
                SELECT * FROM purchases
                WHERE user_id=? AND status NOT IN ('approved','rejected')
                ORDER BY id DESC LIMIT 1
            """, (user_id,)).fetchone()
        p = dict(r) if r else None

    if not p:
        await update.message.reply_html(
            f"⚠️ No pending purchase found for user <code>{user_id}</code>.\n\n"
            f"Use <code>/whoami {user_id}</code> to check their purchase history."
        )
        return

    pid = p["id"]

    # Step 1 — Cancel animation and inactivity jobs immediately
    for i in range(8):
        for job in context.job_queue.get_jobs_by_name(f"anim_{user_id}_{pid}_{i}"):
            job.schedule_removal()
    cancel_inactivity_timer(context, user_id)

    # Step 2 — Mark rejected
    update_purchase(pid, status="rejected",
                    rejected_at=datetime.utcnow().isoformat())

    # Step 3 — Wipe entire user chat (QR, Step 2 button, verifying, etc.)
    all_ids = set()
    all_ids.update(get_tracked_msgs(user_id))
    with db() as conn:
        u_row_db = conn.execute(
            "SELECT menu_msg_id FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if u_row_db and u_row_db["menu_msg_id"]:
            all_ids.add(u_row_db["menu_msg_id"])
        all_purchases = conn.execute(
            "SELECT main_msg_id FROM purchases WHERE user_id=?", (user_id,)
        ).fetchall()
        for row in all_purchases:
            if row["main_msg_id"]:
                all_ids.add(row["main_msg_id"])
    for mid in all_ids:
        await safe_delete(context, user_id, mid)
    with db() as conn:
        conn.execute(
            "UPDATE users SET menu_msg_id=NULL, tracked_msgs='[]' "
            "WHERE user_id=?", (user_id,))
        conn.execute(
            "UPDATE purchases SET main_msg_id=NULL WHERE user_id=?",
            (user_id,))

    # Step 4 — Build rejection keyboard fresh
    rejection_text = "❌ <b>REJECTED</b>\n\n<i>Payment not verified.</i>"
    reject_kb = build_reject_kb(user_id, p)

    # Step 5 — Send guaranteed fresh rejection message
    delivered = False
    try:
        m = await context.bot.send_message(
            chat_id=user_id,
            text=rejection_text,
            parse_mode=ParseMode.HTML,
            reply_markup=reject_kb,
            protect_content=True,
            disable_notification=True,
        )
        track_msg(user_id, m.message_id)
        update_purchase(pid, main_msg_id=m.message_id)
        delivered = True
    except Exception as e:
        log.error(f"cmd_reject delivery failed: {e}")

    schedule_auto_wipe(context, user_id, AUTO_WIPE_MINUTES)
    await event_backup(context)

    await update.message.reply_html(
        f"❌ <b>Rejected</b>\n\n"
        f"User    : <code>{user_id}</code>\n"
        f"Channel : {p['channel_name']}\n"
        f"Amount  : ₹{p['amount']}\n"
        f"Purchase: #{pid}\n"
        f"Delivered: {'✅ Yes' if delivered else '❌ Failed — user may need to /start'}"
    )

async def cmd_qr_add(update, context):
    """Admin: Send a photo to the bot with caption /qr_add <name> [priority]

    Uploads a QR code and stores it by Telegram file_id.
    No file storage needed — works across restarts.

    Examples (send as photo caption):
    /qr_add Main QR
    /qr_add Backup QR 2
    /qr_add VIP QR 5
    """
    if update.effective_user.id != ADMIN_ID:
        return

    msg = update.message
    if not msg.photo:
        await msg.reply_html(
            "📸 <b>Send a QR code photo</b> with this caption:\n\n"
            "<code>/qr_add &lt;name&gt; [priority]</code>\n\n"
            "Examples:\n"
            "<code>/qr_add Main QR</code>\n"
            "<code>/qr_add Backup QR 2</code>\n\n"
            "<i>Priority is optional (default 1). Higher = more likely in priority mode.</i>"
        )
        return

    caption = (msg.caption or "").strip()
    parts = caption.split(maxsplit=2)

    if len(parts) < 2:
        await msg.reply_html(
            "⚠️ Add a name in the caption.\n"
            "Example: <code>/qr_add Main QR</code>"
        )
        return

    # Parse name and optional priority
    name_parts = parts[1:]
    priority = 1
    if name_parts and name_parts[-1].isdigit():
        priority = int(name_parts[-1])
        name = " ".join(name_parts[:-1]) or "QR"
    else:
        name = " ".join(name_parts)

    file_id = msg.photo[-1].file_id

    with db() as conn:
        max_pos = conn.execute(
            "SELECT COALESCE(MAX(position), 0) m FROM qr_codes"
        ).fetchone()["m"]
        cur = conn.execute("""
            INSERT INTO qr_codes (name, file_id, priority, position)
            VALUES (?, ?, ?, ?)
        """, (name, file_id, priority, max_pos + 1))
        new_id = cur.lastrowid

    mode = get_qr_mode()
    await msg.reply_html(
        f"✅ <b>QR Code Added</b>\n\n"
        f"ID       : <code>{new_id}</code>\n"
        f"Name     : {name}\n"
        f"Priority : {priority}\n"
        f"Position : {max_pos + 1}\n"
        f"Mode     : {mode}\n\n"
        f"<i>Active immediately. Use /qr_list to see all QRs.</i>"
    )


async def cmd_qr_list(update, context):
    """Admin: /qr_list — show all QR codes with stats."""
    if update.effective_user.id != ADMIN_ID:
        return

    with db() as conn:
        rows = conn.execute("""
            SELECT id, name, priority, position, is_active, use_count, created_at
            FROM qr_codes
            ORDER BY position ASC, id ASC
        """).fetchall()

    if not rows:
        await update.message.reply_html(
            "No QR codes yet.\n\n"
            "Upload one: send a photo with caption <code>/qr_add Name</code>"
        )
        return

    mode = get_qr_mode()
    rr_idx = get_rr_index()
    single_id = get_active_single_qr_id()

    active_qrs = [r for r in rows if r["is_active"]]
    rr_next = active_qrs[rr_idx % len(active_qrs)]["id"] if active_qrs else None

    text = (
        f"📋 <b>QR Codes</b>\n"
        f"Mode: <b>{mode}</b>"
    )
    if mode == "round_robin" and rr_next:
        text += f" | Next: ID {rr_next}"
    if mode == "single" and single_id:
        text += f" | Active: ID {single_id}"
    text += f"\n\n"

    for r in rows:
        status = "✅" if r["is_active"] else "🚫"
        marker = ""
        if mode == "single" and r["id"] == single_id:
            marker = " 👈 ACTIVE"
        elif mode == "round_robin" and r["id"] == rr_next:
            marker = " 👈 NEXT"
        text += (
            f"{status} <b>ID {r['id']}</b> — {r['name']}{marker}\n"
            f"   Priority: {r['priority']} | "
            f"Pos: {r['position']} | "
            f"Used: {r['use_count']}x\n\n"
        )

    text += (
        f"<b>Commands:</b>\n"
        f"Send photo + <code>/qr_add Name [priority]</code>\n"
        f"<code>/qr_mode &lt;round_robin|priority|single&gt;</code>\n"
        f"<code>/qr_priority &lt;id&gt; &lt;value&gt;</code>\n"
        f"<code>/qr_active &lt;id&gt;</code> — set single active\n"
        f"<code>/qr_remove &lt;id&gt;</code>\n"
        f"<code>/qr_restore &lt;id&gt;</code>\n"
        f"<code>/qr_stats</code>"
    )

    if len(text) > 4000:
        text = text[:4000] + "\n<i>(truncated)</i>"

    await update.message.reply_html(text)


async def cmd_qr_mode(update, context):
    """Admin: /qr_mode <round_robin|priority|single|status>

    round_robin → cycle through active QRs in order
    priority    → weighted random based on priority values (default)
    single      → always use one specific QR (set with /qr_active <id>)
    """
    if update.effective_user.id != ADMIN_ID:
        return

    args = context.args or []
    if not args:
        await update.message.reply_html(
            "Usage: <code>/qr_mode &lt;round_robin|priority|single|status&gt;</code>\n\n"
            "<code>priority</code>    — weighted random (default)\n"
            "<code>round_robin</code> — cycle in order\n"
            "<code>single</code>      — one fixed QR (/qr_active to pick which)\n\n"
            "Example: <code>/qr_mode round_robin</code>"
        )
        return

    action = args[0].lower()

    if action == "status":
        mode = get_qr_mode()
        single_id = get_active_single_qr_id()
        rr_idx = get_rr_index()
        await update.message.reply_html(
            f"<b>QR Mode:</b> {mode}\n"
            f"<b>Round-robin index:</b> {rr_idx}\n"
            f"<b>Single active ID:</b> {single_id or 'not set'}\n\n"
            f"Use /qr_list to see all QR codes."
        )
        return

    valid = ("round_robin", "priority", "single")
    if action not in valid:
        await update.message.reply_text(
            f"Valid modes: {', '.join(valid)}"
        )
        return

    set_qr_mode(action)

    extra = ""
    if action == "single":
        single_id = get_active_single_qr_id()
        extra = (
            f"\n\nUse <code>/qr_active &lt;id&gt;</code> to set which QR to use."
            if not single_id else
            f"\n\nCurrently using QR ID <code>{single_id}</code>."
        )

    await update.message.reply_html(
        f"✅ <b>QR Mode: {action}</b>{extra}"
    )


async def cmd_qr_priority(update, context):
    """Admin: /qr_priority <id> <value>

    Set priority weight for a QR code (used in priority mode).
    Higher value = selected more often.

    Example:
    /qr_priority 1 5   → QR 1 gets 5x weight
    /qr_priority 2 1   → QR 2 gets 1x weight
    """
    if update.effective_user.id != ADMIN_ID:
        return

    args = context.args or []
    if len(args) < 2:
        await update.message.reply_html(
            "Usage: <code>/qr_priority &lt;id&gt; &lt;value&gt;</code>\n\n"
            "Example: <code>/qr_priority 1 5</code>"
        )
        return

    try:
        qr_id = int(args[0])
        priority = int(args[1])
    except ValueError:
        await update.message.reply_text("⚠️ Both ID and value must be numbers.")
        return

    if priority < 1:
        await update.message.reply_text("⚠️ Priority must be at least 1.")
        return

    with db() as conn:
        r = conn.execute(
            "SELECT name FROM qr_codes WHERE id=?", (qr_id,)
        ).fetchone()
        if not r:
            await update.message.reply_text(f"⚠️ QR ID {qr_id} not found.")
            return
        conn.execute(
            "UPDATE qr_codes SET priority=? WHERE id=?", (priority, qr_id)
        )

    await update.message.reply_html(
        f"✅ <b>Priority Updated</b>\n\n"
        f"QR ID : <code>{qr_id}</code> — {r['name']}\n"
        f"Priority : {priority}\n\n"
        f"<i>Takes effect immediately in priority mode.</i>"
    )


async def cmd_qr_active(update, context):
    """Admin: /qr_active <id> — set the single active QR code.

    Only used when mode is 'single'.
    Example: /qr_active 2
    """
    if update.effective_user.id != ADMIN_ID:
        return

    args = context.args or []
    if not args:
        await update.message.reply_html(
            "Usage: <code>/qr_active &lt;id&gt;</code>\n\n"
            "Sets which QR is used in single mode.\n"
            "Example: <code>/qr_active 2</code>"
        )
        return

    try:
        qr_id = int(args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Invalid ID.")
        return

    with db() as conn:
        r = conn.execute(
            "SELECT name, is_active FROM qr_codes WHERE id=?", (qr_id,)
        ).fetchone()

    if not r:
        await update.message.reply_text(f"⚠️ QR ID {qr_id} not found.")
        return
    if not r["is_active"]:
        await update.message.reply_text(
            f"⚠️ QR ID {qr_id} is deactivated. "
            f"Use /qr_restore {qr_id} first."
        )
        return

    set_active_single_qr_id(qr_id)

    mode = get_qr_mode()
    extra = ""
    if mode != "single":
        extra = (
            f"\n\n⚠️ Current mode is <b>{mode}</b>. "
            f"Switch with <code>/qr_mode single</code> to use this QR exclusively."
        )

    await update.message.reply_html(
        f"✅ <b>Active QR Set</b>\n\n"
        f"ID   : <code>{qr_id}</code>\n"
        f"Name : {r['name']}{extra}"
    )


async def cmd_qr_remove(update, context):
    """Admin: /qr_remove <id> — deactivate a QR code."""
    if update.effective_user.id != ADMIN_ID:
        return

    args = context.args or []
    if not args:
        await update.message.reply_html(
            "Usage: <code>/qr_remove &lt;id&gt;</code>"
        )
        return

    try:
        qr_id = int(args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Invalid ID.")
        return

    with db() as conn:
        r = conn.execute(
            "SELECT name FROM qr_codes WHERE id=?", (qr_id,)
        ).fetchone()
        if not r:
            await update.message.reply_text(f"⚠️ QR ID {qr_id} not found.")
            return
        conn.execute(
            "UPDATE qr_codes SET is_active=0 WHERE id=?", (qr_id,)
        )

    await update.message.reply_html(
        f"🚫 <b>QR Removed</b>\n\n"
        f"ID   : <code>{qr_id}</code>\n"
        f"Name : {r['name']}\n\n"
        f"<i>Use /qr_restore {qr_id} to bring it back.</i>"
    )


async def cmd_qr_restore(update, context):
    """Admin: /qr_restore <id> — reactivate a removed QR code."""
    if update.effective_user.id != ADMIN_ID:
        return

    args = context.args or []
    if not args:
        await update.message.reply_html(
            "Usage: <code>/qr_restore &lt;id&gt;</code>"
        )
        return

    try:
        qr_id = int(args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Invalid ID.")
        return

    with db() as conn:
        r = conn.execute(
            "SELECT name FROM qr_codes WHERE id=?", (qr_id,)
        ).fetchone()
        if not r:
            await update.message.reply_text(f"⚠️ QR ID {qr_id} not found.")
            return
        conn.execute(
            "UPDATE qr_codes SET is_active=1 WHERE id=?", (qr_id,)
        )

    await update.message.reply_html(
        f"✅ <b>QR Restored</b>\n\n"
        f"ID   : <code>{qr_id}</code>\n"
        f"Name : {r['name']}\n\n"
        f"<i>Active immediately.</i>"
    )


async def cmd_qr_stats(update, context):
    """Admin: /qr_stats — show usage stats for all QR codes."""
    if update.effective_user.id != ADMIN_ID:
        return

    with db() as conn:
        rows = conn.execute("""
            SELECT id, name, priority, is_active, use_count, position
            FROM qr_codes
            ORDER BY use_count DESC
        """).fetchall()
        total_uses = conn.execute(
            "SELECT COALESCE(SUM(use_count), 0) c FROM qr_codes"
        ).fetchone()["c"]

    if not rows:
        await update.message.reply_text(
            "No QR codes yet. Upload one with /qr_add"
        )
        return

    mode = get_qr_mode()
    text = (
        f"📊 <b>QR Code Stats</b>\n"
        f"Mode: <b>{mode}</b> | Total uses: <b>{total_uses}</b>\n\n"
    )

    for r in rows:
        status = "✅" if r["is_active"] else "🚫"
        pct = round(r["use_count"] / total_uses * 100) if total_uses else 0
        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
        text += (
            f"{status} <b>ID {r['id']}</b> — {r['name']}\n"
            f"   {bar} {pct}% ({r['use_count']} uses)\n"
            f"   Priority: {r['priority']} | Pos: {r['position']}\n\n"
        )

    await update.message.reply_html(text)


async def cmd_channel_group(update, context):
    """Admin: /channel_group <id> <label|none>"""
    if update.effective_user.id != ADMIN_ID:
        return

    args = context.args or []
    if len(args) < 2:
        await update.message.reply_html(
            "Usage: <code>/channel_group &lt;id&gt; &lt;label&gt;</code>\n\n"
            "Examples:\n"
            "<code>/channel_group 1 🎬 Entertainment</code>\n"
            "<code>/channel_group 3 💎 Premium</code>\n"
            "<code>/channel_group 1 none</code> — remove label"
        )
        return

    try:
        channel_id = int(args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Invalid channel ID.")
        return

    label_raw = " ".join(args[1:]).strip()
    label = None if label_raw.lower() == "none" else label_raw

    with db() as conn:
        r = conn.execute(
            "SELECT name FROM channels WHERE id=?", (channel_id,)
        ).fetchone()
        if not r:
            await update.message.reply_text(
                f"⚠️ Channel ID {channel_id} not found. "
                f"Use /channel_list to see valid IDs."
            )
            return
        conn.execute(
            "UPDATE channels SET group_label=? WHERE id=?",
            (label, channel_id)
        )

    if label:
        await update.message.reply_html(
            f"✅ <b>Group Label Set</b>\n\n"
            f"Channel : {r['name']} (ID {channel_id})\n"
            f"Label   : {label}\n\n"
            f"<i>Appears as a header above this channel in the menu.\n"
            f"Users will see it on next /start.</i>"
        )
    else:
        await update.message.reply_html(
            f"✅ Group label removed from <b>{r['name']}</b> (ID {channel_id}).\n\n"
            f"<i>Takes effect on next /start.</i>"
        )

async def cmd_channel_separator(update, context):
    """Admin: /channel_separator <id> <on|off>"""
    if update.effective_user.id != ADMIN_ID:
        return

    args = context.args or []
    if len(args) < 2:
        await update.message.reply_html(
            "Usage: <code>/channel_separator &lt;id&gt; &lt;on|off&gt;</code>\n\n"
            "Adds a visual divider line after the channel button.\n\n"
            "Examples:\n"
            "<code>/channel_separator 2 on</code>\n"
            "<code>/channel_separator 2 off</code>"
        )
        return

    try:
        channel_id = int(args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Invalid channel ID.")
        return

    action = args[1].lower()
    if action not in ("on", "off"):
        await update.message.reply_text("⚠️ Use: on or off")
        return

    value = 1 if action == "on" else 0

    with db() as conn:
        r = conn.execute(
            "SELECT name FROM channels WHERE id=?", (channel_id,)
        ).fetchone()
        if not r:
            await update.message.reply_text(
                f"⚠️ Channel ID {channel_id} not found. "
                f"Use /channel_list to see valid IDs."
            )
            return
        conn.execute(
            "UPDATE channels SET separator_after=? WHERE id=?",
            (value, channel_id)
        )

    status = "✅ ON" if value else "🚫 OFF"
    await update.message.reply_html(
        f"<b>Separator after {r['name']}:</b> {status}\n\n"
        f"<i>Visible in menu on next /start.</i>"
    )

async def _pin_msg_expire(context):
    """JobQueue callback: delete a pinned message after its duration."""
    data = context.job.data
    chat_id    = data["chat_id"]
    message_id = data["message_id"]
    try:
        await context.bot.delete_message(
            chat_id=chat_id, message_id=message_id
        )
        log.debug(f"pin_msg expired: chat={chat_id} msg={message_id}")
    except Exception as e:
        log.debug(f"pin_msg expire delete failed: {e}")

async def cmd_pin_msg(update, context):
    """Admin: /pin_msg <duration_hours|forever> <segment> <message>

    Send a persistent message to users that stays until manually wiped
    or expires after the configured duration.

    Duration:
      forever     — never auto-deletes
      24          — deletes after 24 hours
      48          — deletes after 48 hours
      0.5         — deletes after 30 minutes (decimals supported)

    Segment:
      all         — every user
      unpaid      — no approved purchases
      T1          — Tier 1 owners
      T1,T2       — Tier 1 or Tier 2 owners

    Examples:
    /pin_msg forever all 🎉 New channels added! Tap /start to see them.
    /pin_msg 24 unpaid 🔥 Special offer ends tonight — tap /start now!
    /pin_msg 48 T1 ⭐ Upgrade available — tap /start to unlock Tier 2.
    /pin_msg 0.5 all 🔧 Maintenance in 30 minutes. Please save your access links.
    """
    if update.effective_user.id != ADMIN_ID:
        return

    args = update.message.text.split(maxsplit=3)
    # args[0] = /pin_msg

    if len(args) < 4:
        await update.message.reply_html(
            "Usage: <code>/pin_msg &lt;duration|forever&gt; &lt;segment&gt; &lt;message&gt;</code>\n\n"
            "<b>Duration:</b> hours (e.g. 24, 48, 0.5) or <code>forever</code>\n"
            "<b>Segments:</b> all, unpaid, T1, T1,T2 etc.\n\n"
            "Examples:\n"
            "<code>/pin_msg forever all 🎉 New channels added!</code>\n"
            "<code>/pin_msg 24 unpaid 🔥 Offer ends tonight!</code>\n"
            "<code>/pin_msg 48 T1 ⭐ Upgrade now available.</code>"
        )
        return

    duration_raw = args[1].strip().lower()
    segment      = args[2].strip().lower()
    message_text = args[3].strip()

    # Parse duration
    is_forever = duration_raw == "forever"
    duration_hours = 0.0
    if not is_forever:
        try:
            duration_hours = float(duration_raw)
            if duration_hours <= 0:
                await update.message.reply_text(
                    "⚠️ Duration must be positive, or use 'forever'."
                )
                return
        except ValueError:
            await update.message.reply_text(
                "⚠️ Invalid duration. Use a number (hours) or 'forever'."
            )
            return

    # Build target user list
    with db() as conn:
        all_users = conn.execute("SELECT user_id FROM users").fetchall()
        purchases = conn.execute(
            "SELECT user_id, channel_id FROM purchases WHERE status='approved'"
        ).fetchall()
        blocked_ids = {
            r["user_id"] for r in
            conn.execute("SELECT user_id FROM blocked_users").fetchall()
        }

    tier_map = {}
    for p in purchases:
        uid = p["user_id"]
        if uid not in tier_map:
            tier_map[uid] = set()
        tier_map[uid].add(p["channel_id"])

   # Check if segment is a numeric user ID
    if segment.lstrip("-").isdigit():
        single_uid = int(segment)
        with db() as conn:
            u = conn.execute(
                "SELECT user_id FROM users WHERE user_id=?",
                (single_uid,)
            ).fetchone()
        if not u:
            await update.message.reply_html(
                f"⚠️ User <code>{single_uid}</code> not found in DB.\n"
                f"Use <code>/find</code> to search by name."
            )
            return
        target_ids = [single_uid]
    elif segment == "all":
        target_ids = [u["user_id"] for u in all_users]
    elif segment == "unpaid":
        target_ids = [u["user_id"] for u in all_users
                      if u["user_id"] not in tier_map]
    else:
        try:
            required_tiers = {
                int(t.replace("T", "").replace("t", ""))
                for t in segment.split(",")
            }
            target_ids = [
                u["user_id"] for u in all_users
                if any(t in tier_map.get(u["user_id"], set())
                       for t in required_tiers)
            ]
        except ValueError:
            await update.message.reply_text("⚠️ Invalid segment format.")
            return

    target_ids = [uid for uid in target_ids if uid not in blocked_ids]

    if not target_ids:
        await update.message.reply_html(
            f"No eligible users in segment <b>{segment}</b>."
        )
        return

    duration_label = "forever" if is_forever else f"{duration_hours}h"
    await update.message.reply_html(
        f"📌 Sending pinned message to <b>{len(target_ids)}</b> users "
        f"(segment: <b>{segment}</b>, expires: <b>{duration_label}</b>)…"
    )

    full_text = f"📌 <b>Notice</b>\n\n{message_text}"

    sent = 0
    failed = 0
    sent_message_ids = {}  # {user_id: message_id}

    for uid in target_ids:
        try:
            m = await context.bot.send_message(
                chat_id=uid,
                text=full_text,
                parse_mode=ParseMode.HTML,
                disable_notification=False,  # notify — this is intentional
            )
            track_msg(uid, m.message_id)
            sent_message_ids[uid] = m.message_id
            sent += 1
        except Exception as e:
            log.debug(f"pin_msg to {uid} failed: {e}")
            failed += 1

    # Schedule auto-delete job for each message if not forever
    if not is_forever:
        delay_seconds = duration_hours * 3600
        for uid, mid in sent_message_ids.items():
            context.job_queue.run_once(
                _pin_msg_expire,
                when=delay_seconds,
                data={"chat_id": uid, "message_id": mid},
                name=f"pinmsg_{uid}_{mid}",
            )

    await update.message.reply_html(
        f"✅ <b>Pinned message sent.</b>\n"
        f"Segment  : <b>{segment}</b>\n"
        f"Sent     : {sent}\n"
        f"Failed   : {failed}\n"
        f"Expires  : <b>{duration_label}</b>\n\n"
        f"<i>Messages are tracked — they will be wiped automatically "
        f"on /wipe, /reset, or user's next /start.</i>"
    ) 

_HELP_DETAILS = {
    "stats":     ("<b>/stats</b>\n\nOverall bot totals.\n\n"
                  "Shows total users, approved revenue, and purchase count per status "
                  "(started / verifying / approved / rejected).\n\n"
                  "<b>Example:</b> <code>/stats</code>"),
    "pending":   ("<b>/pending</b>\n\nList all purchases awaiting your action.\n\n"
                  "Shows purchases with status <i>verifying</i> or <i>screenshot_requested</i> — "
                  "name, UPI, amount, and a direct chat link.\n\n"
                  "<b>Example:</b> <code>/pending</code>"),
    "summary":   ("<b>/summary</b>\n\nYesterday's daily report + CSV attachment.\n\n"
                  "Shows new users, attempts, approvals, rejections, revenue. "
                  "CSV has every transaction for that day.\n\n"
                  "<b>Example:</b> <code>/summary</code>\n"
                  "Optional date: <code>/summary 2025-06-01</code>"),
    "listusers": ("<b>/listusers</b>\n\nLast 30 registered users with quick stats.\n\n"
                  "Shows name, username, ID, approved purchase count, revenue. "
                  "Tap a name to open their chat.\n\n"
                  "<b>Example:</b> <code>/listusers</code>"),
    "find":      ("<b>/find &lt;text&gt;</b>\n\nSearch users by name or username.\n\n"
                  "Returns up to 20 matches with direct chat links.\n\n"
                  "<b>Example:</b> <code>/find Sakshi</code>"),
    "whoami":    ("<b>/whoami [user_id]</b>\n\nFull DB snapshot for a user.\n\n"
                  "Shows profile info, all purchase records, statuses, and tracked message IDs. "
                  "Omit the ID to inspect your own account.\n\n"
                  "<b>Example:</b> <code>/whoami 123456789</code>"),
    "wipe":      ("<b>/wipe &lt;user_id&gt;</b>\n\nDelete all bot messages in user's chat. "
                  "Purchase records are kept — user stays recognised as paid on next /start.\n\n"
                  "<b>Example:</b> <code>/wipe 123456789</code>"),
    "reset":     ("<b>/reset &lt;user_id&gt;</b>\n\nNuclear reset: deletes bot messages AND all "
                  "purchase records. User starts completely fresh as if brand new.\n\n"
                  "<b>Example:</b> <code>/reset 123456789</code>"),
    "resetme":   ("<b>/resetme</b>\n\nReset your own admin account. Useful for testing the full "
                  "user flow without affecting real users.\n\n"
                  "<b>Example:</b> <code>/resetme</code>"),
    "wipeall":   ("<b>/wipeall YES</b>\n\nWipe bot messages for every user in the DB. "
                  "Purchase history is preserved. Requires the YES argument as confirmation.\n\n"
                  "<b>Example:</b> <code>/wipeall YES</code>"),
    "resetall":  ("<b>/resetall DELETE-EVERYTHING</b>\n\nNuclear reset for ALL users — deletes "
                  "bot messages AND all purchase records for everyone. Irreversible.\n\n"
                  "<b>Example:</b> <code>/resetall DELETE-EVERYTHING</code>"),
    "broadcast": ("<b>/broadcast &lt;message&gt;</b>\n\nSend an announcement to every user in the DB. "
                  "Supports HTML formatting. Reports sent/failed counts.\n\n"
                  "<b>Example:</b> <code>/broadcast 🎉 New channel added! Check /start</code>"),
    "msg":       ("<b>/msg &lt;user_id&gt; [message]</b>\n\nSend a custom message to one specific user. "
                  "Omit the message text to get a tap-to-open chat link instead.\n\n"
                  "<b>Example:</b> <code>/msg 123456789 Your payment is confirmed!</code>"),
    "away":      ("<b>/away &lt;message&gt;</b>\n\nSet an away notice shown to users after they submit "
                  "payment proof. Auto-clears after 30 s on the user's side, and instantly on "
                  "approve / reject / wipe / reset.\n\n"
                  "<b>Example:</b> <code>/away Back in 2 hours!</code>\n"
                  "Disable: <code>/away off</code>"),
    "block":     ("<b>/block &lt;user_id&gt; [reason]</b>\n\nBlock a user — all their interactions "
                  "are silently ignored by the bot.\n\n"
                  "<b>Example:</b> <code>/block 123456789 repeated fake screenshots</code>"),
    "unblock":   ("<b>/unblock &lt;user_id&gt;</b>\n\nRemove a block so the user can interact "
                  "with the bot again.\n\n"
                  "<b>Example:</b> <code>/unblock 123456789</code>"),
    "backup":    ("<b>/backup</b>\n\nSend the live database file to you as a Telegram document. "
                  "Instant snapshot of the current state.\n\n"
                  "<b>Example:</b> <code>/backup</code>"),
    "restore":   ("<b>/restore</b>\n\nReply to a <code>.db</code> backup file with this command "
                  "to restore the database from it.\n\n"
                  "<b>Example:</b> Reply to a .db file → <code>/restore</code>"),
    "import_csv":("<b>/import_csv</b>\n\nUpload a <code>master_summary.csv</code> file to import "
                  "purchase records into the database.\n\n"
                  "<b>Example:</b> Send CSV file → <code>/import_csv</code>"),
    "logs":      ("<b>/logs &lt;user_id&gt;</b>\n\nShow the last 50 logged inputs from a user "
                  "(text messages, UPI names, screenshots).\n\n"
                  "<b>Example:</b> <code>/logs 123456789</code>"),
    "msg_adm":   ("<b>/msg &lt;user_id&gt;</b>\n\nOpen a direct chat link for a user without "
                  "sending a message.\n\n"
                  "<b>Example:</b> <code>/msg 123456789</code>"),
    "retarget":  ("<b>/retarget &lt;rejected|cancelled|all&gt; &lt;channel_id&gt; &lt;price&gt; [CONFIRM]</b>\n\n"
                  "Send offer to users whose payments were rejected or cancelled "
                  "(only targets users with no approved purchases).\n\n"
                  "<b>Examples:</b>\n"
                  "<code>/retarget rejected 1 99 CONFIRM</code>\n"
                  "<code>/retarget cancelled 1 79 CONFIRM</code>\n"
                  "<code>/retarget all 1 89 CONFIRM</code>"),
    "offer_users": ("<b>/offer_users &lt;channel_id&gt; &lt;price&gt; &lt;user_id1&gt; [user_id2] ...</b>\n\n"
                  "Send the same offer to multiple specific users by space-separated IDs.\n\n"
                  "<b>Example:</b> <code>/offer_users 1 150 123456789 987654321</code>"),
    "bulk_ids":  ("<b>/bulk_ids &lt;segment&gt;</b>\n\nGet comma-separated User ID list for any segment. "
                  "Ready to paste into /bulk_promo_users.\n\n"
                  "Segments: unpaid, T1, T1,T2, all\n\n"
                  "<b>Example:</b> <code>/bulk_ids unpaid</code>"),
    "bulk_promo_users": ("<b>/bulk_promo_users &lt;user_ids&gt; &lt;channel_id&gt; &lt;price&gt; CONFIRM</b>\n\n"
                  "Send promotion to multiple users by comma-separated IDs.\n\n"
                  "<b>Example:</b> <code>/bulk_promo_users 123456,789123 1 99 CONFIRM</code>"),
    "offer_tier": ("<b>/offer_tier &lt;tier&gt; &lt;channel_id&gt; &lt;price&gt; CONFIRM</b>\n\n"
                  "Send offer to all users in a tier segment.\n\n"
                  "Tiers: unpaid, T1, T1,T2, all\n\n"
                  "<b>Example:</b> <code>/offer_tier unpaid 1 150 CONFIRM</code>"),
    "offer_user": ("<b>/offer_user &lt;user_id&gt; &lt;channel_id&gt; &lt;price&gt;</b>\n\n"
                  "Send a specific offer to one user.\n\n"
                  "<b>Example:</b> <code>/offer_user 123456789 1 150</code>"),
    "unpaid":    ("<b>/unpaid</b>\n\nShow all users segmented by tier with prominent IDs. "
                  "Also sends a CSV for copy-paste targeting.\n\n"
                  "<b>Example:</b> <code>/unpaid</code>"),
    "promo_set": ("<b>/promo_set &lt;channel_id&gt; &lt;segment&gt; &lt;price&gt;</b>\n\n"
                  "Set an active promotion price for a channel+segment. "
                  "Overrides custom and default prices.\n\n"
                  "<b>Example:</b> <code>/promo_set 1 unpaid 99</code>"),
    "promo_clear": ("<b>/promo_clear &lt;channel_id&gt; &lt;segment&gt;</b>\n\n"
                  "Deactivate a promotion. Price reverts to custom or default.\n\n"
                  "<b>Example:</b> <code>/promo_clear 1 unpaid</code>"),
    "promo_status": ("<b>/promo_status</b>\n\nShow all currently active promotions.\n\n"
                  "<b>Example:</b> <code>/promo_status</code>"),
    "promo_send": ("<b>/promo_send &lt;channel_id&gt; &lt;segment&gt; CONFIRM</b>\n\n"
                  "Blast the active promotion price to all users in a segment.\n\n"
                  "<b>Example:</b> <code>/promo_send 1 unpaid CONFIRM</code>"),
    "promo_personal": ("<b>/promo_personal &lt;user_id&gt; &lt;channel_id&gt; &lt;price&gt;</b>\n\n"
                  "Send an exclusive personal offer to one specific user.\n\n"
                  "<b>Example:</b> <code>/promo_personal 123456789 1 99</code>"),
    "fallback_toggle": ("<b>/fallback_toggle &lt;on|off|status&gt;</b>\n\n"
                  "Enable or disable the budget bundle offers for unpaid users.\n\n"
                  "<b>Example:</b> <code>/fallback_toggle on</code>"),
    "special_offers_toggle": ("<b>/special_offers_toggle &lt;on|off|status&gt;</b>\n\n"
                  "Enable or disable all special offer and promo commands.\n\n"
                  "<b>Example:</b> <code>/special_offers_toggle on</code>"),
    "approve": ("<b>/approve &lt;user_id&gt; [channel_id or pid:N]</b>\n\n"
                "Manually approve a user's pending purchase by command.\n\n"
                "<b>Examples:</b>\n"
                "<code>/approve 123456789</code> — latest pending\n"
                "<code>/approve 123456789 1</code> — channel 1 pending\n"
                "<code>/approve 123456789 pid:55</code> — exact purchase ID"),
    "reject":  ("<b>/reject &lt;user_id&gt; [channel_id or pid:N]</b>\n\n"
                "Manually reject a user's pending purchase by command.\n\n"
                "<b>Examples:</b>\n"
                "<code>/reject 123456789</code> — latest pending\n"
                "<code>/reject 123456789 1</code> — channel 1 pending\n"
                "<code>/reject 123456789 pid:55</code> — exact purchase ID"),
    "tier_gate": ("<b>/tier_gate &lt;on|off|status&gt;</b>\n\n"
                  "Control whether Tier 1 is mandatory before higher tiers.\n\n"
                  "<code>on</code> — Tier 1 gate active (default)\n"
                  "<code>off</code> — All tiers directly visible\n"
                  "<code>status</code> — Check current setting\n\n"
                  "<b>Examples:</b>\n"
                  "<code>/tier_gate off</code>\n"
                  "<code>/tier_gate on</code>"),
    "proof_mode": ("<b>/proof_mode &lt;upi|screenshot|both|none|status&gt;</b>\n\n"
                   "Control what proof users must submit after paying.\n\n"
                   "<code>both</code>       — UPI name + Screenshot (default)\n"
                   "<code>upi</code>        — UPI name only\n"
                   "<code>screenshot</code> — Screenshot only\n"
                   "<code>none</code>       — No proof, manual approval queue\n\n"
                   "<b>Example:</b> <code>/proof_mode upi</code>"),
    "sleep":      ("<b>/sleep &lt;on|off|status&gt;</b>\n\n"
                   "Put bot in silent mode. Records all /start visitors "
                   "without replying. Use /sleep_visitors to see who came.\n\n"
                   "<b>Example:</b> <code>/sleep on</code>"),
    "sleep_visitors": ("<b>/sleep_visitors [clear]</b>\n\n"
                   "List all users who clicked /start during sleep mode. "
                   "Also sends a CSV ready for /bulk_promo_users.\n\n"
                   "<code>/sleep_visitors</code> — show list\n"
                   "<code>/sleep_visitors clear</code> — wipe list\n\n"
                   "<b>Example:</b> <code>/sleep_visitors</code>"),
    "channel_list":    ("<b>/channel_list</b>\n\nShow all channels with ID, price, "
                        "link, and status.\n\n"
                        "<b>Example:</b> <code>/channel_list</code>"),
    "channel_add":     ("<b>/channel_add &lt;name&gt; | &lt;price&gt; | &lt;link&gt;</b>\n\n"
                        "Add a new channel. Live immediately.\n\n"
                        "<b>Example:</b>\n"
                        "<code>/channel_add VIP Pack | 299 | https://t.me/+HASH</code>"),
    "channel_edit":    ("<b>/channel_edit &lt;id&gt; &lt;field&gt; &lt;value&gt;</b>\n\n"
                        "Edit name, price, link, or position of a channel.\n\n"
                        "<b>Examples:</b>\n"
                        "<code>/channel_edit 1 price 399</code>\n"
                        "<code>/channel_edit 1 name Gold Pack</code>"),
    "channel_remove":  ("<b>/channel_remove &lt;id&gt;</b>\n\nSoft-delete a channel "
                        "(hidden from users, data kept).\n\n"
                        "<b>Example:</b> <code>/channel_remove 3</code>"),
    "channel_restore": ("<b>/channel_restore &lt;id&gt;</b>\n\nRestore a removed channel.\n\n"
                        "<b>Example:</b> <code>/channel_restore 3</code>"),
    "qr_list":     ("<b>/qr_list</b>\n\nShow all QR codes with mode, "
                    "priority, position and usage count.\n\n"
                    "<b>Example:</b> <code>/qr_list</code>"),
    "qr_mode":     ("<b>/qr_mode &lt;priority|round_robin|single|status&gt;</b>\n\n"
                    "Set QR routing method.\n\n"
                    "<code>priority</code>    — weighted random by priority\n"
                    "<code>round_robin</code> — cycle in order\n"
                    "<code>single</code>      — one fixed QR only\n\n"
                    "<b>Example:</b> <code>/qr_mode round_robin</code>"),
    "qr_priority": ("<b>/qr_priority &lt;id&gt; &lt;value&gt;</b>\n\n"
                    "Set weight for priority mode. Higher = more frequent.\n\n"
                    "<b>Example:</b> <code>/qr_priority 1 5</code>"),
    "qr_active":   ("<b>/qr_active &lt;id&gt;</b>\n\n"
                    "Set which QR is used in single mode.\n\n"
                    "<b>Example:</b> <code>/qr_active 2</code>"),
    "qr_remove":   ("<b>/qr_remove &lt;id&gt;</b>\n\nDeactivate a QR code.\n\n"
                    "<b>Example:</b> <code>/qr_remove 3</code>"),
    "qr_restore":  ("<b>/qr_restore &lt;id&gt;</b>\n\nReactivate a removed QR code.\n\n"
                    "<b>Example:</b> <code>/qr_restore 3</code>"),
    "qr_stats":    ("<b>/qr_stats</b>\n\nUsage breakdown per QR with percentage bar.\n\n"
                    "<b>Example:</b> <code>/qr_stats</code>"),
    "channel_group":     ("<b>/channel_group &lt;id&gt; &lt;label&gt;</b>\n\n"
                          "Set a section header shown above a channel button. "
                          "Use 'none' to remove.\n\n"
                          "<b>Examples:</b>\n"
                          "<code>/channel_group 1 🎬 Entertainment</code>\n"
                          "<code>/channel_group 5 none</code>"),
    "channel_separator": ("<b>/channel_separator &lt;id&gt; &lt;on|off&gt;</b>\n\n"
                          "Add a visual divider line after a channel button.\n\n"
                          "<b>Examples:</b>\n"
                          "<code>/channel_separator 2 on</code>\n"
                          "<code>/channel_separator 2 off</code>"),
}

_HELP_SECTIONS = [
    ("📊 Stats & Reports",      ["stats", "pending", "summary", "listusers", "find", "whoami", "unpaid"]),
    ("📺 Channels", ["channel_list", "channel_add", "channel_edit", "channel_remove", "channel_restore", "channel_group", "channel_separator"]),
    ("💳 QR Codes",             ["qr_list", "qr_mode", "qr_priority", "qr_active", "qr_remove", "qr_restore", "qr_stats"]),
    ("🧹 Cleanup (single)",     ["wipe", "reset", "resetme"]),
    ("☢️ Cleanup (ALL)",        ["wipeall", "resetall"]),
    ("📢 Communication",        ["broadcast", "msg", "away", "block", "unblock"]),
    ("🎯 Targeting & Offers",   ["offer_tier", "offer_user", "offer_users", "retarget", "bulk_ids", "bulk_promo_users"]),
    ("🎉 Promotions",           ["promo_set", "promo_clear", "promo_status", "promo_send", "promo_personal"]),
    ("⚙️ Settings",             ["fallback_toggle", "special_offers_toggle", "tier_gate", "proof_mode", "sleep", "sleep_visitors"]),
    ("💾 DB Backup",            ["backup", "restore", "import_csv"]),
    ("📋 Diagnostics",          ["logs"]),
    ("✅ Manual Approval",      ["approve", "reject"]),
]


async def cmd_help(update, context):
    """Admin: /help — summary of all admin commands."""
    if update.effective_user.id != ADMIN_ID:
        return

    lines = ["🛠 <b>Admin Commands</b>\n"]
    for section, cmds in _HELP_SECTIONS:
        lines.append(f"\n<b>{section}</b>")
        for cmd in cmds:
            lines.append(f"  /{cmd}")

    await update.message.reply_html("\n".join(lines))


async def cb_help_detail(update, context):
    """Edit /help message in-place to show detail for tapped command."""
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        await q.answer()
        return
    await q.answer()

    cmd = q.data.split(":", 1)[1]

    if cmd == "__back__":
        # Rebuild summary
        lines = ["🛠 <b>Admin Commands</b>\n<i>Tap a button below for usage &amp; example.</i>"]
        rows = []
        for section, cmds in _HELP_SECTIONS:
            lines.append(f"\n<b>{section}</b>")
            for c in cmds:
                lines.append(f"  /{c}")
            btn_row = []
            for c in cmds:
                btn_row.append(InlineKeyboardButton(f"/{c}", callback_data=f"help_d:{c}"))
                if len(btn_row) == 4:
                    rows.append(btn_row)
                    btn_row = []
            if btn_row:
                rows.append(btn_row)
        try:
            await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML,
                                      reply_markup=InlineKeyboardMarkup(rows))
        except Exception as e:
            log.debug(f"cb_help_detail back failed: {e}")
        return

    detail = _HELP_DETAILS.get(cmd, f"No detail found for <code>/{cmd}</code>.")
    back_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Back", callback_data="help_d:__back__")
    ]])
    try:
        await q.edit_message_text(detail, parse_mode=ParseMode.HTML, reply_markup=back_kb)
    except Exception as e:
        log.debug(f"cb_help_detail failed: {e}")

async def cb_trigger_start(update, context):
    """User tapped the Start button from greeting reply — trigger /start."""
    q = update.callback_query
    await q.answer()
    try:
        await q.message.delete()
    except Exception:
        pass
    await cmd_start(update, context)


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
        sent_m = await context.bot.send_message(
            chat_id=user_id,
            text=f"📩 <b>Message from ipepsi</b>\n\n{message_text}",
            parse_mode=ParseMode.HTML,
            disable_notification=True,
        )
        # Track so wipe / reset / resetall / wipeall will delete it
        track_msg(user_id, sent_m.message_id)
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
    
    When enabled: Unpaid users see "🎁 See Fallback Offers" button
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
async def cmd_retarget(update, context):
    """Admin: /retarget <rejected|cancelled> <channel_id> <price> [CONFIRM]
    
    Send offer to users whose payments were rejected or cancelled.
    Only targets users with NO approved purchases.
    
    Examples:
    /retarget rejected 1 99 CONFIRM
    /retarget cancelled 1 79 CONFIRM
    /retarget all 1 89 CONFIRM   ← both rejected + cancelled
    """
    if update.effective_user.id != ADMIN_ID:
        return

    args = context.args or []
    if len(args) < 3:
        await update.message.reply_html(
            "Usage: <code>/retarget &lt;rejected|cancelled|all&gt; &lt;channel_id&gt; &lt;price&gt; [CONFIRM]</code>\n\n"
            "Examples:\n"
            "<code>/retarget rejected 1 99 CONFIRM</code>\n"
            "<code>/retarget cancelled 1 79 CONFIRM</code>\n"
            "<code>/retarget all 1 89 CONFIRM</code>\n\n"
            "<i>Only targets users with no approved purchases.</i>"
        )
        return

    segment = args[0].lower()
    try:
        channel_id = int(args[1])
        price = int(args[2])
    except ValueError:
        await update.message.reply_text("Invalid channel_id or price.")
        return

    channel = next((c for c in CHANNELS if c["id"] == channel_id), None)
    if not channel:
        await update.message.reply_text(f"Channel {channel_id} not found.")
        return

    # Gather target users
    if segment == "rejected":
        users = get_users_by_purchase_status("rejected")
    elif segment == "cancelled":
        users = get_users_by_purchase_status("cancelled")
    elif segment == "all":
        r = get_users_by_purchase_status("rejected")
        c = get_users_by_purchase_status("cancelled")
        # Deduplicate by user_id
        seen = set()
        users = []
        for u in r + c:
            if u["user_id"] not in seen:
                seen.add(u["user_id"])
                users.append(u)
    else:
        await update.message.reply_text("Segment must be: rejected, cancelled, or all")
        return

    if not users:
        await update.message.reply_text(f"No unapproved users found in '{segment}' segment.")
        return

    # Preview without CONFIRM
    if len(args) < 4 or args[3].upper() != "CONFIRM":
        preview_names = ", ".join(
            (u["first_name"] or f"ID {u['user_id']}") for u in users[:5]
        )
        await update.message.reply_html(
            f"⚠️ About to retarget <b>{len(users)}</b> users.\n\n"
            f"<b>Segment:</b> {segment}\n"
            f"<b>Channel:</b> {channel['name']}\n"
            f"<b>Price:</b> ₹{price}\n"
            f"<b>Preview:</b> {preview_names}{'…' if len(users) > 5 else ''}\n\n"
            f"To confirm:\n"
            f"<code>/retarget {segment} {channel_id} {price} CONFIRM</code>"
        )
        return

    await update.message.reply_text(f"📢 Sending to {len(users)} users…")

    offer_msg = (
        f"🔁 <b>STILL INTERESTED?</b>\n\n"
        f"<b>{channel['name']}</b>\n"
        f"<b>₹{price}</b>\n\n"
        f"Give it another shot! Send /start to try again."
    )

    sent = 0
    failed = 0
    for u in users:
        try:
            m = await context.bot.send_message(
                chat_id=u["user_id"],
                text=offer_msg,
                parse_mode=ParseMode.HTML,
                disable_notification=True,
            )
            track_msg(u["user_id"], m.message_id)
            log_user_message(u["user_id"], "retarget_sent", f"Ch{channel_id} ₹{price}")
            sent += 1
        except Exception as e:
            log.debug(f"retarget to {u['user_id']} failed: {e}")
            failed += 1

    await update.message.reply_html(
        f"✅ <b>Retarget complete.</b>\n"
        f"Sent: {sent}\nFailed/blocked: {failed}"
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
    """Admin: /wipe <user_id> — delete bot messages only. Purchases preserved.
    Use /reset to also delete purchase records (nuclear)."""
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args or []
    if not args:
        await update.message.reply_html(
            "Usage: <code>/wipe &lt;user_id&gt;</code>\n\n"
            "Deletes bot messages only — purchases are preserved.\n"
            "Use <code>/reset &lt;user_id&gt;</code> for full nuclear reset."
        )
        return
    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Invalid user_id. Must be a number.")
        return

    # Gather ALL known bot message IDs
    all_ids = set()
    all_ids.update(get_tracked_msgs(target_id))
    with db() as conn:
        u = conn.execute(
            "SELECT menu_msg_id FROM users WHERE user_id=?", (target_id,)
        ).fetchone()
        if u and u["menu_msg_id"]:
            all_ids.add(u["menu_msg_id"])
        purchases = conn.execute(
            "SELECT main_msg_id FROM purchases WHERE user_id=?", (target_id,)
        ).fetchall()
        for p in purchases:
            if p["main_msg_id"]:
                all_ids.add(p["main_msg_id"])

    deleted = 0
    for mid in all_ids:
        try:
            await context.bot.delete_message(chat_id=target_id, message_id=mid)
            deleted += 1
        except Exception as e:
            log.debug(f"wipe delete {mid} failed: {e}")

    # Reset message refs ONLY — purchases untouched
    with db() as conn:
        conn.execute(
            "UPDATE users SET menu_msg_id=NULL, tracked_msgs='[]' WHERE user_id=?",
            (target_id,))
        conn.execute(
            "UPDATE purchases SET main_msg_id=NULL WHERE user_id=?",
            (target_id,))

    await update.message.reply_html(
        f"🧹 Wiped <b>{deleted}</b> messages for user <code>{target_id}</code>.\n"
        f"✅ Purchase records preserved — user's access is intact."
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

async def cmd_check_imports(update, context):
    """Admin: /check_imports — show purchase rows with bad channel_id."""
    if update.effective_user.id != ADMIN_ID:
        return
    with db() as conn:
        bad = conn.execute("""
            SELECT id, user_id, channel_name, channel_id, status
            FROM purchases
            WHERE channel_id <= 0
            ORDER BY id DESC LIMIT 30
        """).fetchall()
        total_approved = conn.execute(
            "SELECT COUNT(*) c FROM purchases WHERE status='approved'"
        ).fetchone()["c"]
        good = conn.execute(
            "SELECT COUNT(*) c FROM purchases "
            "WHERE status='approved' AND channel_id > 0"
        ).fetchone()["c"]

    if not bad:
        await update.message.reply_html(
            f"✅ No bad rows found.\n"
            f"Approved purchases with valid channel_id: <b>{good}</b>"
        )
        return

    text = (
        f"⚠️ <b>Purchases with bad channel_id ({len(bad)} rows)</b>\n"
        f"Approved total: {total_approved} | Valid: {good}\n\n"
    )
    for r in bad:
        text += (
            f"• #{r['id']} user <code>{r['user_id']}</code> "
            f"ch_id={r['channel_id']} "
            f"name=<code>{r['channel_name']}</code> "
            f"status={r['status']}\n"
        )
    text += (
        f"\n<b>Fix:</b> Re-upload CSV with caption <code>overwrite</code>\n"
        f"or run <code>/channel_list</code> to check name mismatches."
    )
    await update.message.reply_html(text)


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
        file = await context.bot.get_file(doc.file_id)
        file_bytes = await file.download_as_bytearray()
        file_content = file_bytes.decode('utf-8-sig')

        csv_reader = csv.DictReader(io.StringIO(file_content))
        rows = list(csv_reader)

        if not rows:
            await update.message.reply_text("❌ CSV file is empty!")
            return

        # Report detected columns so admin can verify format
        detected_cols = csv_reader.fieldnames or []
        await update.message.reply_html(
            f"📋 <b>CSV detected</b>\n"
            f"Rows: <b>{len(rows)}</b>\n"
            f"Columns: <code>{', '.join(detected_cols)}</code>\n\n"
            f"Processing…"
        )

        caption = update.message.caption or ""
        overwrite_mode = caption.strip().lower() == "overwrite"

        # Build channel name → id map from DB (live, not CHANNELS proxy)
        # Also build a case-insensitive fallback map
        with db() as conn:
            ch_rows = conn.execute(
                "SELECT id, name FROM channels"
            ).fetchall()
        channel_name_map = {r["name"]: r["id"] for r in ch_rows}
        channel_name_map_lower = {
            r["name"].lower().strip(): r["id"] for r in ch_rows
        }

        imported = 0
        updated = 0
        skipped = 0
        unresolved_channels = []
        errors = []

        with db() as conn:
            for row_num, row in enumerate(rows, start=2):  # row 1 = header
                try:
                    # ── Parse fields ──────────────────────────────────────
                    raw_pid      = str(row.get('PurchaseID', '') or '').strip()
                    created_at   = str(row.get('CreatedAt', '') or '').strip()
                    raw_uid      = str(row.get('UserID', '') or '').strip()
                    first_name   = str(row.get('Name', '') or '').strip().split()[0] \
                                   if str(row.get('Name', '') or '').strip() else 'User'
                    full_name    = str(row.get('Name', '') or '').strip()
                    username     = str(row.get('Username', '') or '').strip()
                    channel_name = str(row.get('Channel', '') or '').strip()
                    raw_amount   = str(row.get('Amount', '') or '').strip()
                    upi_name     = str(row.get('UPIName', '') or '').strip()
                    status       = str(row.get('Status', 'approved') or 'approved').strip().lower()
                    approved_at  = str(row.get('ApprovedAt', '') or '').strip() or None
                    rejected_at  = str(row.get('RejectedAt', '') or '').strip() or None

                    # ── Validate required fields ──────────────────────────
                    if not raw_uid:
                        skipped += 1
                        errors.append(f"Row {row_num}: missing UserID")
                        continue
                    if not channel_name:
                        skipped += 1
                        errors.append(f"Row {row_num}: missing Channel")
                        continue
                    if not raw_amount:
                        skipped += 1
                        errors.append(f"Row {row_num}: missing Amount")
                        continue

                    user_id = int(raw_uid)
                    amount  = int(raw_amount)
                    purchase_id = int(raw_pid) if raw_pid else None

                    # ── Resolve channel_id — THIS is the critical step ────
                    # Exact match first, then case-insensitive strip
                    channel_id = channel_name_map.get(channel_name)
                    if channel_id is None:
                        channel_id = channel_name_map_lower.get(
                            channel_name.lower().strip()
                        )
                    if channel_id is None:
                        # Try partial match (channel name contains CSV value)
                        for db_name, db_id in channel_name_map.items():
                            if (channel_name.lower() in db_name.lower()
                                    or db_name.lower() in channel_name.lower()):
                                channel_id = db_id
                                break

                    if channel_id is None:
                        # Still unresolved — record it but still import with
                        # a placeholder so we don't lose the approval record.
                        # Admin will see the warning and can fix channel names.
                        unresolved_channels.append(
                            f"Row {row_num}: '{channel_name}' "
                            f"(user {user_id})"
                        )
                        # Use -1 as sentinel for "channel name unknown"
                        # This prevents collision with bundle sentinel (0)
                        # and still lets ownership check work if admin
                        # later re-imports with correct names.
                        channel_id = -1

                    # ── Upsert user record ────────────────────────────────
                    # Always upsert so name/username stay current
                    name_parts = full_name.split(None, 1)
                    fn = name_parts[0] if name_parts else 'User'
                    ln = name_parts[1] if len(name_parts) > 1 else ''
                    conn.execute("""
                        INSERT INTO users (user_id, first_name, last_name, username)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(user_id) DO UPDATE SET
                            first_name = COALESCE(excluded.first_name, first_name),
                            last_name  = COALESCE(excluded.last_name,  last_name),
                            username   = COALESCE(excluded.username,   username)
                    """, (user_id, fn, ln, username or None))

                    # ── Insert or update purchase ─────────────────────────
                    if purchase_id:
                        existing = conn.execute(
                            "SELECT id, channel_id, status FROM purchases WHERE id=?",
                            (purchase_id,)
                        ).fetchone()
                    else:
                        # No PurchaseID in CSV — check by user+channel+status
                        existing = conn.execute("""
                            SELECT id, channel_id, status FROM purchases
                            WHERE user_id=? AND channel_id=? AND status=?
                            ORDER BY id DESC LIMIT 1
                        """, (user_id, channel_id, status)).fetchone()

                    if existing:
                        if overwrite_mode:
                            conn.execute("""
                                UPDATE purchases SET
                                    channel_id   = ?,
                                    channel_name = ?,
                                    amount       = ?,
                                    upi_name     = ?,
                                    status       = ?,
                                    approved_at  = ?,
                                    rejected_at  = ?
                                WHERE id = ?
                            """, (channel_id, channel_name, amount,
                                  upi_name or None, status,
                                  approved_at, rejected_at,
                                  existing["id"]))
                            updated += 1
                        else:
                            skipped += 1
                    else:
                        if purchase_id:
                            conn.execute("""
                                INSERT INTO purchases
                                    (id, user_id, channel_id, channel_name,
                                     amount, upi_name, status,
                                     created_at, approved_at, rejected_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """, (purchase_id, user_id, channel_id,
                                  channel_name, amount, upi_name or None,
                                  status, created_at or None,
                                  approved_at, rejected_at))
                        else:
                            conn.execute("""
                                INSERT INTO purchases
                                    (user_id, channel_id, channel_name,
                                     amount, upi_name, status,
                                     created_at, approved_at, rejected_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """, (user_id, channel_id, channel_name,
                                  amount, upi_name or None, status,
                                  created_at or None,
                                  approved_at, rejected_at))
                        imported += 1

                except Exception as e:
                    errors.append(f"Row {row_num}: {e}")
                    continue

        # ── Re-map any channel_id=-1 rows if channel names now resolve ────
        # (Handles case where channels table was empty during import but
        #  gets populated later — running /import_csv again fixes them.)
        remapped = 0
        if unresolved_channels:
            with db() as conn:
                bad_rows = conn.execute("""
                    SELECT id, channel_name FROM purchases WHERE channel_id=-1
                """).fetchall()
                for br in bad_rows:
                    cid = channel_name_map.get(br["channel_name"])
                    if cid is None:
                        cid = channel_name_map_lower.get(
                            br["channel_name"].lower().strip()
                        )
                    if cid is not None:
                        conn.execute(
                            "UPDATE purchases SET channel_id=? WHERE id=?",
                            (cid, br["id"])
                        )
                        remapped += 1

        # ── Build summary ─────────────────────────────────────────────────
        mode_label = "overwrite" if overwrite_mode else "append"
        summary = (
            f"✅ <b>CSV Import Complete</b> <i>({mode_label} mode)</i>\n\n"
            f"📊 <b>Results:</b>\n"
            f"✅ Inserted  : {imported} new records\n"
            f"♻️ Updated   : {updated} existing records\n"
            f"🔁 Re-mapped : {remapped} channel_id fixes\n"
            f"⏭️ Skipped   : {skipped} duplicates / bad rows\n"
        )

        if unresolved_channels:
            summary += (
                f"\n⚠️ <b>Unresolved channel names ({len(unresolved_channels)}):</b>\n"
                f"These rows were imported with channel_id=-1 and will NOT "
                f"show ✅ buttons until fixed.\n\n"
            )
            for uc in unresolved_channels[:10]:
                summary += f"  • {uc}\n"
            if len(unresolved_channels) > 10:
                summary += f"  … and {len(unresolved_channels) - 10} more\n"
            summary += (
                f"\n<b>Fix:</b> Ensure channel names in your CSV exactly match "
                f"what's in /channel_list, then re-upload with caption "
                f"<code>overwrite</code>."
            )

        if errors:
            summary += f"\n❌ <b>Row errors ({len(errors)}):</b>\n"
            for err in errors[:5]:
                summary += f"  • {err}\n"
            if len(errors) > 5:
                summary += f"  … and {len(errors) - 5} more\n"

        # Show DB channel names so admin can compare against CSV
        summary += f"\n\n<b>Channel names in DB (for reference):</b>\n"
        for db_name in channel_name_map:
            summary += f"  • <code>{db_name}</code>\n"

        await update.message.reply_html(summary)
        log.info(
            f"CSV Import: {imported} imported, {updated} updated, "
            f"{skipped} skipped, {len(unresolved_channels)} unresolved channels, "
            f"{len(errors)} errors"
        )

    except Exception as e:
        await update.message.reply_html(
            f"❌ Error processing CSV:\n<code>{e}</code>"
        )
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

def main():
    if not BOT_TOKEN or not ADMIN_ID:
        raise RuntimeError("Set BOT_TOKEN and ADMIN_ID env vars.")

    # Check database path
    db_size = 0
    if os.path.exists(DB_PATH):
        db_size = os.path.getsize(DB_PATH) / (1024 * 1024)
        log.info(f"✅ Using local database: {DB_PATH} ({db_size:.2f} MB)")
    else:
        log.info(f"📝 Database will be created at: {DB_PATH}")

    log.info(f"✅ Database is committed to git - all data persists across deploys!")

    # init_db FIRST — must run before anything queries CHANNELS
    init_db()

    # Startup diagnostics — always visible in Railway logs
    log.info(f"ENV channels detected: {len(_ENV_CHANNELS)}")
    for c in _ENV_CHANNELS:
        log.info(f"  ENV channel: {c}")

    db_channels = get_channels()
    log.info(f"DB channels loaded: {len(db_channels)}")
    for c in db_channels:
        log.info(f"  DB channel: {c}")

    if not db_channels:
        log.error(
            "STARTUP: No channels available. "
            "Set CHANNEL_1=Name|Price|Link in Railway variables "
            "or use /channel_add after the bot starts."
        )
    else:
        log.info(f"STARTUP: {len(db_channels)} channel(s) ready.")
    app = Application.builder().token(BOT_TOKEN).build()

    # User
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(cb_fallback_menu,  pattern=r"^fallback_menu$"))
    app.add_handler(CallbackQueryHandler(cb_buy_bundle,     pattern=r"^buy_bundle:"))
    app.add_handler(CallbackQueryHandler(cb_back_to_start,  pattern=r"^back_to_start$"))
    app.add_handler(CallbackQueryHandler(cb_buy,          pattern=r"^buy:"))
    app.add_handler(CallbackQueryHandler(cb_upi_start,    pattern=r"^upi:start:"))
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
    app.add_handler(CommandHandler("resetme", cmd_resetme))
    app.add_handler(CommandHandler("wipe",    cmd_wipe))
    app.add_handler(CommandHandler("whoami",    cmd_whoami))
    app.add_handler(CommandHandler("wipeall",   cmd_wipeall))
    app.add_handler(CommandHandler("resetall",  cmd_resetall))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("listusers", cmd_listusers))
    app.add_handler(CommandHandler("find",      cmd_find))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CallbackQueryHandler(cb_help_detail, pattern=r"^help_d:"))
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
    app.add_handler(CommandHandler("retarget", cmd_retarget))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("reject",  cmd_reject))
    app.add_handler(CommandHandler("tier_gate", cmd_tier_gate))
    app.add_handler(MessageHandler(
        filters.Document.ALL & filters.ChatType.PRIVATE,
        on_csv_import_file
    ))
    app.add_handler(CommandHandler("restore",     cmd_restore))
    app.add_handler(CallbackQueryHandler(cb_admin, pattern=r"^adm:"))
    app.add_handler(MessageHandler(
        (filters.PHOTO | filters.VIDEO | filters.Document.ALL)
        & filters.ChatType.PRIVATE
        & filters.User(ADMIN_ID),
        on_admin_media,
    ))
    app.add_handler(CommandHandler("proof_mode",      cmd_proof_mode))
    app.add_handler(CommandHandler("sleep",           cmd_sleep))
    app.add_handler(CommandHandler("sleep_visitors",  cmd_sleep_visitors))
    app.add_handler(CommandHandler("channel_add",     cmd_channel_add))
    app.add_handler(CommandHandler("channel_edit",    cmd_channel_edit))
    app.add_handler(CommandHandler("channel_remove",  cmd_channel_remove))
    app.add_handler(CommandHandler("channel_restore", cmd_channel_restore))
    app.add_handler(CommandHandler("channel_list",    cmd_channel_list))
    app.add_handler(CommandHandler("channel_group",     cmd_channel_group))
    app.add_handler(CommandHandler("channel_separator", cmd_channel_separator))
    app.add_handler(CallbackQueryHandler(cb_noop, pattern=r"^noop$"))
    app.add_handler(CommandHandler("qr_list",     cmd_qr_list))
    app.add_handler(CommandHandler("qr_mode",     cmd_qr_mode))
    app.add_handler(CommandHandler("qr_priority", cmd_qr_priority))
    app.add_handler(CommandHandler("qr_active",   cmd_qr_active))
    app.add_handler(CommandHandler("qr_remove",   cmd_qr_remove))
    app.add_handler(CommandHandler("qr_restore",  cmd_qr_restore))
    app.add_handler(CommandHandler("qr_stats",    cmd_qr_stats))
    app.add_handler(CommandHandler("check_imports", cmd_check_imports))
    app.add_handler(CommandHandler("pin_msg", cmd_pin_msg))
    app.add_handler(CallbackQueryHandler(cb_trigger_start, pattern=r"^trigger_start$"))


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
    # Suppress httpx INFO logs — they print the full bot token URL
    import logging as _logging
    _logging.getLogger("httpx").setLevel(_logging.WARNING)

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        poll_interval=0.0,
        timeout=10,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
