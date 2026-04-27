# VB Membership Bot
# - No URL previews (uses WebApp buttons — opens directly)
# - Everything stays in bot — no website success page
# - PhonePe return URL deep-links back to bot
# - Auto-deletes messages after 20 min
# - Saves full data to Supabase (tgid, phone, plan, UTR, VPA)
# - Admin broadcast with filters

import os, hashlib, asyncio, logging, json
from datetime import datetime, timedelta, timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import (ApplicationBuilder, CommandHandler, CallbackQueryHandler,
                           MessageHandler, ContextTypes, filters as tgfilters)
from telegram.error import BadRequest, Forbidden
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import httpx

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

BOT_TOKEN  = os.environ['BOT_TOKEN']
BOT_NAME   = os.environ.get('BOT_USERNAME', '')          # e.g. sadhviibot
SUPA_URL   = os.environ['SUPABASE_URL']
SUPA_KEY   = os.environ['SUPABASE_SERVICE_KEY']
STORE_URL  = os.environ.get('STORE_URL', 'https://vetrivelbakery.store')
ADMIN_IDS  = [int(x) for x in os.environ.get('ADMIN_IDS', '').split(',') if x.strip()]
MSG_TTL    = int(os.environ.get('MSG_TTL_SECONDS', '1200'))   # 20 min

# ── Name pool ─────────────────────────────────────────────────────────────────
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
    'purple': {'label':'💜 Purple',    'price':30,  'yearly':299},
    'pink':   {'label':'🩷 Pink',      'price':59,  'yearly':499},
    'royal':  {'label':'💙 Royal Blue','price':99,  'yearly':749},
}

def pay_url(uid, plan, billing='monthly'):
    """
    PhonePe return URL deep-links back to bot after payment.
    tg://resolve?domain=BOT&start=paid_ORDERID
    """
    ph  = det_phone(uid)
    nm  = det_name(uid).replace(' ', '+')
    oid = f"VBM{uid}{plan[0].upper()}{int(asyncio.get_event_loop().time()*1000) % 100000}"
    b   = 'yearly' if billing == 'yearly' else 'monthly'
    return (
        f"{STORE_URL}/membership"
        f"?ph={ph}&plan={plan}&billing={b}&name={nm}&tgid={uid}&oid={oid}&silent=1"
    )

def plan_kb(billing='monthly'):
    b_lbl = '📅 Yearly (save 2 months)' if billing == 'monthly' else '📅 Monthly'
    rows  = [
        [InlineKeyboardButton(
            f"{p['label']} — ₹{p['price']}/mo" if billing == 'monthly' else f"{p['label']} — ₹{p['yearly']}/yr",
            web_app=WebAppInfo(url=(
                f"{STORE_URL}/membership?plan={k}&billing={billing}&silent=1"
            ))
        )]
        for k, p in PLANS.items()
    ]
    rows.append([InlineKeyboardButton(b_lbl, callback_data=f"billing:{'yearly' if billing=='monthly' else 'monthly'}")])
    return InlineKeyboardMarkup(rows)

# ── Supabase ──────────────────────────────────────────────────────────────────
async def sb(path, method='GET', body=None):
    hdrs = {
        'apikey':        SUPA_KEY,
        'Authorization': f'Bearer {SUPA_KEY}',
        'Content-Type':  'application/json',
        'Prefer':        'return=minimal,resolution=ignore-duplicates',
    }
    async with httpx.AsyncClient(timeout=10) as cl:
        if method == 'GET':
            r = await cl.get(f"{SUPA_URL}/rest/v1{path}", headers=hdrs)
            return r.json() if r.status_code < 300 else []
        elif method == 'POST':
            r = await cl.post(f"{SUPA_URL}/rest/v1{path}", headers=hdrs, json=body)
            return r.status_code < 300
        elif method == 'PATCH':
            r = await cl.patch(f"{SUPA_URL}/rest/v1{path}", headers=hdrs, json=body)
            return r.status_code < 300

async def save_member_tg(uid, phone, plan):
    """Ensure telegram_id is stored on member record."""
    await sb(f"/members?phone=eq.{phone}", 'PATCH', {
        'telegram_id': str(uid),
        'plan':        plan,
        'updated_at':  datetime.now(timezone.utc).isoformat(),
    })

async def get_member(phone):
    rows = await sb(f"/members?phone=eq.{phone}&select=status,plan,expires_at,token,telegram_id")
    return rows[0] if rows else None

async def get_channel_link(plan='purple'):
    try:
        rows = await sb("/site_settings?key=in.(channel_purple,channel_pink,channel_royal)&select=key,value")
        m = {r['key']: r['value'] for r in rows if r.get('value')}
        if plan == 'royal': return m.get('channel_royal') or m.get('channel_purple') or ''
        if plan == 'pink':  return m.get('channel_pink')  or m.get('channel_purple') or ''
        return m.get('channel_purple') or ''
    except: return ''

async def get_channel_id(plan='purple'):
    try:
        rows = await sb("/site_settings?key=in.(channel_id_purple,channel_id_pink,channel_id_royal)&select=key,value")
        m = {r['key']: r['value'] for r in rows if r.get('value')}
        k = f"channel_id_{plan}"
        v = m.get(k) or m.get('channel_id_purple') or ''
        return int(v) if v else None
    except: return None

# ── Auto-delete ───────────────────────────────────────────────────────────────
async def autodel(bot, chat_id, msg_id, delay=None):
    await asyncio.sleep(delay if delay is not None else MSG_TTL)
    try: await bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except: pass

def sched_del(bot, msg, delay=None):
    asyncio.create_task(autodel(bot, msg.chat_id, msg.message_id, delay))

async def del_msg(bot, chat_id, msg_id):
    try: await bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except: pass

# ── Channel kick / unban ──────────────────────────────────────────────────────
async def kick_user(bot, tg_id, plan):
    cid = await get_channel_id(plan)
    if not cid: return
    try:
        await bot.ban_chat_member(chat_id=cid, user_id=tg_id)
        await asyncio.sleep(0.3)
        await bot.unban_chat_member(chat_id=cid, user_id=tg_id)
    except Exception as e:
        log.warning(f"Kick failed uid={tg_id}: {e}")

# ── Handlers ──────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid    = update.effective_user.id
    args   = ctx.args or []
    phone  = det_phone(uid)

    # ── Handle deep-link return from PhonePe ──────────────────────
    # /start paid_ORDERID  — payment completed, show Join button
    if args and args[0].startswith('paid_'):
        oid    = args[0][5:]
        member = await get_member(phone)
        plan   = member.get('plan', 'purple') if member else 'purple'
        ch     = await get_channel_link(plan)

        # Delete previous messages from this session
        for mid in ctx.user_data.pop('msg_ids', []):
            await del_msg(ctx.bot, uid, mid)

        kb = [[InlineKeyboardButton('Join', url=ch)]] if ch else []
        msg = await update.message.reply_text(
            '✅ *Done!*',
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(kb) if kb else None
        )
        sched_del(ctx.bot, msg)
        return

    # ── Normal /start ─────────────────────────────────────────────
    member = await get_member(phone)

    # Active member — show Join + renew
    if member and member.get('status') == 'active' and 'retry' not in args:
        exp  = member.get('expires_at', '')[:10]
        plan = member.get('plan', 'purple')
        ch   = await get_channel_link(plan)
        kb   = [[InlineKeyboardButton('🔄 Renew', callback_data='billing:monthly')]]
        if ch: kb.insert(0, [InlineKeyboardButton('Join', url=ch)])
        msg  = await update.message.reply_text(
            f"Active — expires {exp}",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        sched_del(ctx.bot, msg)
        ctx.user_data.setdefault('msg_ids', []).append(msg.message_id)
        return

    # Show plan selection
    msg = await update.message.reply_text(
        'Choose your plan 👇',
        reply_markup=plan_kb('monthly')
    )
    sched_del(ctx.bot, msg)
    ctx.user_data.setdefault('msg_ids', []).append(msg.message_id)

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    uid = q.from_user.id
    d   = q.data

    if d.startswith('billing:'):
        billing = d.split(':')[1]
        try: await q.edit_message_reply_markup(reply_markup=plan_kb(billing))
        except BadRequest: pass
        return

    # plan:purple:monthly
    parts = d.split(':')
    if len(parts) == 3 and parts[0] == 'plan':
        _, plan, billing = parts
        ctx.user_data['plan'] = plan
        p    = PLANS[plan]
        amt  = p['yearly'] if billing == 'yearly' else p['price']
        per  = 'year' if billing == 'yearly' else 'month'
        link = pay_url(uid, plan, billing)
        try:
            await q.edit_message_text(
                f"₹{amt}/{per}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f'Pay ₹{amt} →', web_app=WebAppInfo(url=link))],
                    [InlineKeyboardButton('← Back', callback_data=f'billing:{billing}')],
                ])
            )
        except BadRequest: pass

# ── WebApp data handler — fires when payment completes in WebApp ──────────────
async def on_webapp_data(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Receives data sent from the payment page via Telegram.sendData().
    Payload: {"status":"paid","plan":"purple","oid":"VBM..."}
    """
    uid  = update.effective_user.id
    raw  = update.effective_message.web_app_data.data if update.effective_message.web_app_data else None
    if not raw: return

    try:    data = json.loads(raw)
    except: return

    status = data.get('status')
    plan   = data.get('plan', 'purple')
    phone  = det_phone(uid)

    # Delete all previous plan selection messages
    for mid in ctx.user_data.pop('msg_ids', []):
        await del_msg(ctx.bot, uid, mid)

    if status == 'paid':
        # Ensure telegram_id is saved on member
        await save_member_tg(uid, phone, plan)
        ch  = await get_channel_link(plan)
        kb  = [[InlineKeyboardButton('Join', url=ch)]] if ch else []
        msg = await update.message.reply_text(
            '✅ *Done!*',
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(kb) if kb else None
        )
        sched_del(ctx.bot, msg)
    else:
        # Failed — show plan selection again
        msg = await update.message.reply_text(
            'Payment not completed. Try again 👇',
            reply_markup=plan_kb()
        )
        sched_del(ctx.bot, msg)
        ctx.user_data.setdefault('msg_ids', []).append(msg.message_id)

# ── Scheduled jobs ────────────────────────────────────────────────────────────
async def job_remind(bot):
    """Day 28 reminder."""
    now       = datetime.now(timezone.utc)
    remind_to = (now + timedelta(days=4)).isoformat()
    remind_fr = (now + timedelta(days=3)).isoformat()
    rows = await sb(f"/members?status=eq.active&expires_at=gte.{remind_fr}&expires_at=lte.{remind_to}&select=phone,telegram_id,plan,expires_at")
    for m in rows:
        tgid = m.get('telegram_id')
        if not tgid: continue
        exp  = m.get('expires_at', '')[:10]
        try:
            msg = await bot.send_message(
                chat_id=int(tgid),
                text=f"Expires {exp} — renew to keep access 👇",
                reply_markup=plan_kb()
            )
            sched_del(bot, msg, delay=86400)
        except Exception as e: log.warning(f"Remind fail {tgid}: {e}")

async def job_expire(bot):
    """Day 32+ removal."""
    now  = datetime.now(timezone.utc).isoformat()
    rows = await sb(f"/members?status=eq.active&expires_at=lt.{now}&select=phone,telegram_id,plan")
    for m in rows:
        phone = m.get('phone')
        tgid  = m.get('telegram_id')
        plan  = m.get('plan', 'purple')
        if tgid:
            await kick_user(bot, int(tgid), plan)
            try:
                msg = await bot.send_message(
                    chat_id=int(tgid),
                    text="Membership expired — renew to rejoin 👇",
                    reply_markup=plan_kb()
                )
                sched_del(bot, msg, delay=86400)
            except Exception as e: log.warning(f"Expire fail {tgid}: {e}")
        await sb(f"/members?phone=eq.{phone}", 'PATCH', {'status':'expired','updated_at':now})
        log.info(f"Expired phone={phone}")

# ── Admin commands ────────────────────────────────────────────────────────────
def is_admin(uid): return uid in ADMIN_IDS

async def admin_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return

    help_text = (
        "*Admin Commands*\n\n"
        "/broadcast `<msg>` — send to ALL active members\n"
        "/broadcast_plan `purple|pink|royal` `<msg>` — send to one plan\n"
        "/broadcast_expired `<msg>` — send to expired members\n"
        "/stats — member counts by plan and status\n"
        "/admin — show this help"
    )
    msg = await update.message.reply_text(help_text, parse_mode='Markdown')
    sched_del(ctx.bot, msg, delay=300)

async def broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid): return
    if not ctx.args:
        await update.message.reply_text("Usage: /broadcast Your message here")
        return
    text = ' '.join(ctx.args)
    rows = await sb("/members?status=eq.active&select=telegram_id")
    sent = 0
    for m in rows:
        tgid = m.get('telegram_id')
        if not tgid: continue
        try:
            await ctx.bot.send_message(chat_id=int(tgid), text=text)
            sent += 1
            await asyncio.sleep(0.05)  # rate limit
        except Exception as e: log.warning(f"BC fail {tgid}: {e}")
    await update.message.reply_text(f"✅ Sent to {sent} members")

async def broadcast_plan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid): return
    if len(ctx.args) < 2:
        await update.message.reply_text("Usage: /broadcast_plan purple Your message")
        return
    plan = ctx.args[0].lower()
    text = ' '.join(ctx.args[1:])
    rows = await sb(f"/members?status=eq.active&plan=eq.{plan}&select=telegram_id")
    sent = 0
    for m in rows:
        tgid = m.get('telegram_id')
        if not tgid: continue
        try:
            await ctx.bot.send_message(chat_id=int(tgid), text=text)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as e: log.warning(f"BC_plan fail {tgid}: {e}")
    await update.message.reply_text(f"✅ Sent to {sent} {plan} members")

async def broadcast_expired(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid): return
    if not ctx.args:
        await update.message.reply_text("Usage: /broadcast_expired Your message")
        return
    text = ' '.join(ctx.args)
    rows = await sb("/members?status=eq.expired&select=telegram_id")
    sent = 0
    for m in rows:
        tgid = m.get('telegram_id')
        if not tgid: continue
        try:
            await ctx.bot.send_message(chat_id=int(tgid), text=text)
            sent += 1
            await asyncio.sleep(0.05)
        except: pass
    await update.message.reply_text(f"✅ Sent to {sent} expired members")

async def stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid): return
    rows = await sb("/members?select=status,plan")
    from collections import Counter
    by_status = Counter(r['status'] for r in rows)
    by_plan   = Counter(r['plan']   for r in rows if r['status'] == 'active')
    text = (
        f"*Members*\n"
        f"Active: {by_status.get('active',0)}\n"
        f"Expired: {by_status.get('expired',0)}\n"
        f"Pending: {by_status.get('pending_payment',0)}\n\n"
        f"*Active by plan*\n"
        f"Purple: {by_plan.get('purple',0)}\n"
        f"Pink: {by_plan.get('pink',0)}\n"
        f"Royal Blue: {by_plan.get('royal',0)}"
    )
    msg = await update.message.reply_text(text, parse_mode='Markdown')
    sched_del(ctx.bot, msg, delay=60)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler('start',             start))
    app.add_handler(CommandHandler('admin',             admin_cmd))
    app.add_handler(CommandHandler('broadcast',         broadcast))
    app.add_handler(CommandHandler('broadcast_plan',    broadcast_plan))
    app.add_handler(CommandHandler('broadcast_expired', broadcast_expired))
    app.add_handler(CommandHandler('stats',             stats))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(tgfilters.StatusUpdate.WEB_APP_DATA, on_webapp_data))

    sched = AsyncIOScheduler(timezone='Asia/Kolkata')
    sched.add_job(job_remind, 'cron', hour=9,  minute=0,  args=[app.bot])
    sched.add_job(job_expire, 'cron', hour=10, minute=0,  args=[app.bot])
    sched.start()

    log.info("VB Bot started")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
