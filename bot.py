# Vetrivel Bakery — Telegram Bot
# Features:
#   - Deterministic phone/name per user
#   - Plan selection + payment link
#   - Channel management (add/remove per plan)
#   - Day 28 renewal reminder
#   - Day 32 auto-removal from channel
#   - Auto-delete bot messages after 10 min
#   - Message cleanup after payment

import os, hashlib, asyncio, logging
from datetime import datetime, timedelta, timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.error import BadRequest, Forbidden
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import httpx

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ['BOT_TOKEN']
SUPA_URL  = os.environ['SUPABASE_URL']
SUPA_KEY  = os.environ['SUPABASE_SERVICE_KEY']
STORE_URL = os.environ.get('STORE_URL', 'https://vetrivelbakery.store')

MSG_TTL = int(os.environ.get('MSG_TTL_SECONDS', '600'))  # auto-delete after 10 min

# ── Names ─────────────────────────────────────────────────────────────────────
TN_FIRST = [
    'Murugan','Selvam','Karthikeyan','Senthilkumar','Vijayakumar','Balamurugan',
    'Dhineshkumar','Ganeshkumar','Hariharan','Jayakumar','Kannadasan','Logeshwaran',
    'Manivannan','Naveen','Prabakaran','Rajasekaran','Sureshkumar','Tamilarasan',
    'Udhayakumar','Vigneshwaran','Priya','Kavitha','Divyabharathi','Anitha',
    'Lakshmiprabha','Sumathi','Bharathidevi','Deepalakshmi','Elavarasi','Geetha',
    'Hemavathi','Indhumathi','Janani','Kamalaselvi','Malarvizhi','Nirmala',
    'Padmavathi','Revathi','Selvarani','Thenmozhiselvi','Arun','Surya','Dinesh',
    'Ramesh','Manoj','Suresh','Prakash','Deepa','Uma','Vani','Rekha','Shanthi',
]
TN_LAST = ['K','R','S','M','P','V','T','N','Kumar','Raja','Devi','Selvan','Rajan']

def det_name(uid):
    h = int(hashlib.sha256(str(uid).encode()).hexdigest(), 16)
    return f"{TN_FIRST[h % len(TN_FIRST)]} {TN_LAST[(h>>8) % len(TN_LAST)]}"

def det_phone(uid):
    h = int(hashlib.sha256(f"vb_ph_{uid}".encode()).hexdigest(), 16)
    return f"{[6,7,8,9][h%4]}{str(h>>4).zfill(20)[:9]}"

# ── Plans ─────────────────────────────────────────────────────────────────────
PLANS = {
    'purple': {'name':'💜 Purple',     'price':30,  'yearly':299},
    'pink':   {'name':'🩷 Pink',       'price':59,  'yearly':499},
    'royal':  {'name':'💙 Royal Blue', 'price':99,  'yearly':749},
}

def pay_link(uid, plan, billing='monthly'):
    ph = det_phone(uid)
    nm = det_name(uid).replace(' ','+')
    b  = 'yearly' if billing=='yearly' else 'monthly'
    return f"{STORE_URL}/membership?ph={ph}&plan={plan}&billing={b}&name={nm}&tgid={uid}"

def plan_kb(billing='monthly'):
    b_lbl = '📅 Switch to Yearly' if billing=='monthly' else '📅 Switch to Monthly'
    rows  = [
        [InlineKeyboardButton(
            f"{p['name']} — ₹{p['price']}/mo" if billing=='monthly' else f"{p['name']} — ₹{p['yearly']}/yr",
            callback_data=f"plan:{k}:{billing}"
        )]
        for k,p in PLANS.items()
    ]
    rows.append([InlineKeyboardButton(b_lbl, callback_data=f"billing:{'yearly' if billing=='monthly' else 'monthly'}")])
    return InlineKeyboardMarkup(rows)

# ── Supabase ──────────────────────────────────────────────────────────────────
async def sb_get(path):
    async with httpx.AsyncClient() as cl:
        r = await cl.get(f"{SUPA_URL}/rest/v1{path}",
            headers={'apikey':SUPA_KEY,'Authorization':f'Bearer {SUPA_KEY}'},timeout=10)
        return r.json() if r.status_code < 300 else []

async def sb_patch(path, body):
    async with httpx.AsyncClient() as cl:
        await cl.patch(f"{SUPA_URL}/rest/v1{path}",
            headers={'apikey':SUPA_KEY,'Authorization':f'Bearer {SUPA_KEY}',
                     'Content-Type':'application/json','Prefer':'return=minimal'},
            json=body, timeout=10)

async def check_member(phone):
    rows = await sb_get(f"/members?phone=eq.{phone}&select=status,token,expires_at,amount,plan,telegram_id")
    return rows[0] if rows else None

async def get_settings():
    rows = await sb_get("/site_settings?select=key,value")
    return {r['key']:r['value'] for r in rows if r.get('value')}

async def get_channel_link(plan='purple'):
    s = await get_settings()
    if plan=='royal': return s.get('channel_royal') or s.get('channel_purple') or ''
    if plan=='pink':  return s.get('channel_pink')  or s.get('channel_purple') or ''
    return s.get('channel_purple') or ''

async def get_channel_id(plan='purple'):
    """Returns the numeric channel ID (e.g. -1001234567890) for kicking members."""
    s = await get_settings()
    key = {'royal':'channel_id_royal','pink':'channel_id_pink'}.get(plan,'channel_id_purple')
    val = s.get(key,'')
    try: return int(val) if val else None
    except: return None

# ── Channel actions ───────────────────────────────────────────────────────────
async def kick_from_channel(bot, telegram_id: int, plan: str):
    """Remove user from channel. Unban immediately so they can rejoin after paying."""
    cid = await get_channel_id(plan)
    if not cid or not telegram_id:
        return False
    try:
        await bot.ban_chat_member(chat_id=cid, user_id=telegram_id)
        await asyncio.sleep(0.5)
        await bot.unban_chat_member(chat_id=cid, user_id=telegram_id)
        log.info(f"Kicked uid={telegram_id} from {plan} channel")
        return True
    except (BadRequest, Forbidden) as e:
        log.warning(f"Kick failed uid={telegram_id} plan={plan}: {e}")
        return False

# ── Auto-delete messages ──────────────────────────────────────────────────────
async def schedule_delete(bot, chat_id, message_id, delay=None):
    """Delete a message after delay seconds (default MSG_TTL)."""
    await asyncio.sleep(delay if delay is not None else MSG_TTL)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except (BadRequest, Forbidden):
        pass

def auto_delete(bot, msg, delay=None):
    """Fire-and-forget auto-delete task."""
    asyncio.create_task(schedule_delete(bot, msg.chat_id, msg.message_id, delay))

# ── Handlers ──────────────────────────────────────────────────────────────────
WELCOME = (
    "🧁 *Vetrivel Bakery*\n\n"
    "━━━━━━━━━━━━━━━━\n"
    "☕ Daily Tea & Coffee @ ₹5\n"
    "🎂 Monthly cake + weekly specials\n"
    "⚡ Priority queue — zero wait\n"
    "━━━━━━━━━━━━━━━━\n\n"
    "Choose your plan 👇"
)

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args       = ctx.args or []
    show_plans = 'plans' in args or 'retry' in args
    uid        = update.effective_user.id
    phone      = det_phone(uid)
    member     = await check_member(phone)

    if member and member.get('status') == 'active' and not show_plans:
        exp    = member.get('expires_at','')[:10]
        m_plan = member.get('plan','purple')
        ch     = await get_channel_link(m_plan)
        kb     = [[InlineKeyboardButton('🔄 Renew', callback_data='billing:monthly')]]
        if ch: kb.insert(0, [InlineKeyboardButton('Join', url=ch)])
        msg = await update.message.reply_text(
            f"✅ *Active*\nExpires: {exp}",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(kb)
        )
        auto_delete(ctx.bot, msg)
        return

    msg = await update.message.reply_text(WELCOME, parse_mode='Markdown', reply_markup=plan_kb())
    auto_delete(ctx.bot, msg)

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    uid = q.from_user.id
    d   = q.data

    if d.startswith('billing:'):
        try: await q.edit_message_reply_markup(reply_markup=plan_kb(d.split(':')[1]))
        except BadRequest: pass
        return

    _, plan, billing = d.split(':')
    ctx.user_data['plan'] = plan
    link  = pay_link(uid, plan, billing)
    p     = PLANS[plan]
    amt   = p['yearly'] if billing=='yearly' else p['price']
    per   = 'year' if billing=='yearly' else 'month'

    try:
        await q.edit_message_text(
            f"*{p['name']}* — ₹{amt}/{per}\n\n"
            f"{'📅 2 months free on yearly' if billing=='yearly' else '📆 Cancel anytime'}\n\n"
            f"Tap Pay to proceed 👇",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"Pay ₹{amt} →", url=link)],
                [InlineKeyboardButton('← Back', callback_data=f'billing:{billing}')],
            ])
        )
    except BadRequest: pass

# ── Scheduled jobs ────────────────────────────────────────────────────────────
async def send_collect_request(phone, vpa, amount, plan):
    """UPI collect request — user gets tap-to-approve notification in their UPI app."""
    if not vpa: return False
    import time
    order_id = f"VBR{int(time.time()*1000)}"
    try:
        async with httpx.AsyncClient() as cl:
            r = await cl.post(
                f"{STORE_URL}/api/send-collect",
                headers={'x-internal-secret': os.environ.get('INTERNAL_SECRET','')},
                json={'phone':phone,'vpa':vpa,'amount':amount,'plan':plan,'orderId':order_id},
                timeout=15,
            )
            return r.status_code == 200
    except Exception as e:
        log.warning(f"Collect failed: {e}")
        return False

async def job_remind(bot):
    """Day 28 — remind members to renew (4 days before expiry)."""
    now       = datetime.now(timezone.utc)
    remind_at = (now + timedelta(days=4)).isoformat()
    remind_from = (now + timedelta(days=3)).isoformat()

    rows = await sb_get(
        f"/members?status=eq.active&expires_at=gte.{remind_from}&expires_at=lte.{remind_at}"
        f"&select=phone,telegram_id,plan,expires_at"
    )
    for m in rows:
        tgid = m.get('telegram_id')
        if not tgid: continue
        plan = m.get('plan','purple')
        exp  = m.get('expires_at','')[:10]
        try:
            msg = await bot.send_message(
                chat_id=int(tgid),
                text=f"⏰ *Renewal Reminder*\n\nYour membership expires on *{exp}*.\n\nRenew now to keep your access 👇",
                parse_mode='Markdown',
                reply_markup=plan_kb()
            )
            auto_delete(bot, msg, delay=86400)  # reminder stays 24 hrs
            log.info(f"Reminder sent to tgid={tgid}")
        except Exception as e:
            log.warning(f"Reminder failed tgid={tgid}: {e}")

async def job_expire(bot):
    """Day 32+ — remove expired members from their channels."""
    now  = datetime.now(timezone.utc).isoformat()
    rows = await sb_get(
        f"/members?status=eq.active&expires_at=lt.{now}"
        f"&select=phone,telegram_id,plan"
    )
    for m in rows:
        phone = m.get('phone')
        tgid  = m.get('telegram_id')
        plan  = m.get('plan','purple')

        # Kick from channel
        if tgid:
            kicked = await kick_from_channel(bot, int(tgid), plan)
            # Notify user
            try:
                msg = await bot.send_message(
                    chat_id=int(tgid),
                    text="⚠️ Your VB membership has expired.\n\nRenew to rejoin the channel 👇",
                    reply_markup=plan_kb()
                )
                auto_delete(bot, msg, delay=86400)
            except Exception as e:
                log.warning(f"Expire notify failed tgid={tgid}: {e}")

        # Mark as expired in DB
        await sb_patch(f"/members?phone=eq.{phone}", {
            'status': 'expired',
            'updated_at': now,
        })
        log.info(f"Expired phone={phone} tgid={tgid}")

# ── Payment confirmed (called by webhook via Supabase or direct API) ──────────
async def on_payment_confirmed(bot, telegram_id: int, new_plan: str,
                                old_plan: str, amount, channel_link: str):
    """
    Called after PhonePe webhook confirms payment.
    - Kicks from old channel if plan changed
    - Sends channel join link
    - Deletes message after TTL
    """
    # Kick from old channel if switching plans
    if old_plan and old_plan != new_plan:
        await kick_from_channel(bot, telegram_id, old_plan)

    kb = [[InlineKeyboardButton('Join', url=channel_link)]] if channel_link else []
    try:
        msg = await bot.send_message(
            chat_id=telegram_id,
            text=f"✅ *Payment Confirmed!*\n\n₹{amount}/month — membership active.",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(kb) if kb else None
        )
        auto_delete(bot, msg)  # delete after MSG_TTL
    except Exception as e:
        log.error(f"Payment notify failed tgid={telegram_id}: {e}")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler('start',  start))
    app.add_handler(CallbackQueryHandler(on_callback))

    # Scheduler
    sched = AsyncIOScheduler(timezone='Asia/Kolkata')
    sched.add_job(job_remind, 'cron', hour=9,  minute=0,  args=[app.bot])  # 9 AM daily
    sched.add_job(job_expire, 'cron', hour=10, minute=0,  args=[app.bot])  # 10 AM daily
    sched.start()

    log.info("🧁 VB Bot started")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
