# VB Membership Bot — Fully stateless. All state in Supabase.
# Bot can restart, change token, change server — users never lose their data.
# telegram_id is the permanent key for every user.

import os, hashlib, asyncio, logging, json, secrets
from datetime import datetime, timedelta, timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.error import BadRequest, Forbidden
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import httpx

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

BOT_TOKEN  = os.environ['BOT_TOKEN']
SUPA_URL   = os.environ['SUPABASE_URL']
SUPA_KEY   = os.environ['SUPABASE_SERVICE_KEY']
STORE_URL  = os.environ.get('STORE_URL', 'https://vetrivelbakery.store')
ADMIN_IDS  = [int(x) for x in os.environ.get('ADMIN_IDS', '').split(',') if x.strip()]
MSG_TTL    = int(os.environ.get('MSG_TTL_SECONDS', '1200'))  # 20 min

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

def det_name(uid: int) -> str:
    h = int(hashlib.sha256(str(uid).encode()).hexdigest(), 16)
    return f"{TN_FIRST[h % len(TN_FIRST)]} {TN_LAST[(h>>8) % len(TN_LAST)]}"

def det_phone(uid: int) -> str:
    h = int(hashlib.sha256(f"vb_ph_{uid}".encode()).hexdigest(), 16)
    return f"{[6,7,8,9][h%4]}{str(h>>4).zfill(20)[:9]}"

# ── Plans ─────────────────────────────────────────────────────────────────────
PLANS = {
    'purple': {'label': '💜 Purple',     'price': 30,  'yearly': 299},
    'pink':   {'label': '🩷 Pink',       'price': 59,  'yearly': 499},
    'royal':  {'label': '💙 Royal Blue', 'price': 99,  'yearly': 749},
}

def pay_url(uid: int, plan: str, billing: str = 'monthly') -> str:
    ph = det_phone(uid)
    nm = det_name(uid).replace(' ', '+')
    b  = 'yearly' if billing == 'yearly' else 'monthly'
    return f"{STORE_URL}/membership?ph={ph}&plan={plan}&billing={b}&name={nm}&tgid={uid}&silent=1"

def plan_kb(billing: str = 'monthly') -> InlineKeyboardMarkup:
    b_lbl = '📅 Yearly (2 months free)' if billing == 'monthly' else '📅 Monthly'
    rows  = [
        [InlineKeyboardButton(
            f"{p['label']} — ₹{p['price']}/mo" if billing == 'monthly' else f"{p['label']} — ₹{p['yearly']}/yr",
            callback_data=f"plan:{k}:{billing}"
        )]
        for k, p in PLANS.items()
    ]
    rows.append([InlineKeyboardButton(b_lbl, callback_data=f"billing:{'yearly' if billing=='monthly' else 'monthly'}")])
    return InlineKeyboardMarkup(rows)

# ── Supabase — ALL state lives here ──────────────────────────────────────────
async def sb_get(path: str):
    async with httpx.AsyncClient(timeout=8) as cl:
        r = await cl.get(f"{SUPA_URL}/rest/v1{path}",
            headers={'apikey': SUPA_KEY, 'Authorization': f'Bearer {SUPA_KEY}'})
        return r.json() if r.status_code < 300 else []

async def sb_patch(path: str, body: dict):
    async with httpx.AsyncClient(timeout=8) as cl:
        await cl.patch(f"{SUPA_URL}/rest/v1{path}",
            headers={'apikey': SUPA_KEY, 'Authorization': f'Bearer {SUPA_KEY}',
                     'Content-Type': 'application/json', 'Prefer': 'return=minimal'},
            json=body)

async def sb_post(path: str, body: dict):
    async with httpx.AsyncClient(timeout=8) as cl:
        await cl.post(f"{SUPA_URL}/rest/v1{path}",
            headers={'apikey': SUPA_KEY, 'Authorization': f'Bearer {SUPA_KEY}',
                     'Content-Type': 'application/json',
                     'Prefer': 'return=minimal,resolution=ignore-duplicates'},
            json=body)

async def sb_upsert(path: str, body: dict, conflict_col: str):
    async with httpx.AsyncClient(timeout=8) as cl:
        await cl.post(f"{SUPA_URL}/rest/v1{path}",
            headers={'apikey': SUPA_KEY, 'Authorization': f'Bearer {SUPA_KEY}',
                     'Content-Type': 'application/json',
                     'Prefer': f'resolution=merge-duplicates,return=minimal'},
            json=body)

# ── Core DB operations ────────────────────────────────────────────────────────

async def get_member_by_tgid(tg_uid: int) -> dict | None:
    """Primary lookup — always use telegram_id. Works across bot restarts/token changes."""
    rows = await sb_get(
        f"/members?telegram_id=eq.{tg_uid}"
        f"&select=status,plan,expires_at,token,phone,name,amount,utr,vpa,join_token"
        f"&limit=1"
    )
    return rows[0] if rows else None

async def record_user(tg_uid: int, plan: str = 'purple'):
    """Ensure every user interaction is recorded with their telegram_id."""
    phone = det_phone(tg_uid)
    name  = det_name(tg_uid)
    now   = datetime.now(timezone.utc).isoformat()
    # Upsert into members so the record always exists
    existing = await sb_get(f"/members?telegram_id=eq.{tg_uid}&select=id&limit=1")
    if not existing:
        existing_phone = await sb_get(f"/members?phone=eq.{phone}&select=id,telegram_id&limit=1")
        if existing_phone and not existing_phone[0].get('telegram_id'):
            await sb_patch(f"/members?phone=eq.{phone}", {
                'telegram_id': str(tg_uid), 'updated_at': now,
            })
        elif not existing_phone:
            await sb_post('/members', {
                'phone': phone, 'name': name, 'telegram_id': str(tg_uid),
                'plan': plan, 'status': 'pending_payment',
                'created_at': now, 'updated_at': now,
            })

async def gen_join_token(tg_uid: int, utr: str = '') -> str:
    """Generate opaque join token, save to Supabase, return for button URL."""
    raw   = f"vbjoin_{tg_uid}_{utr}_{secrets.token_hex(6)}"
    token = hashlib.sha256(raw.encode()).hexdigest()[:32]
    now   = datetime.now(timezone.utc).isoformat()
    await sb_patch(f"/members?telegram_id=eq.{tg_uid}", {
        'join_token': token, 'updated_at': now,
    })
    return token

async def get_channel_link(plan: str = 'purple') -> str:
    try:
        rows = await sb_get("/site_settings?key=in.(channel_purple,channel_pink,channel_royal)&select=key,value")
        m    = {r['key']: r['value'] for r in rows if r.get('value')}
        if plan == 'royal': return m.get('channel_royal') or m.get('channel_purple') or ''
        if plan == 'pink':  return m.get('channel_pink')  or m.get('channel_purple') or ''
        return m.get('channel_purple') or ''
    except: return ''

async def get_channel_id(plan: str = 'purple') -> int | None:
    try:
        rows = await sb_get(f"/site_settings?key=eq.channel_id_{plan}&select=value&limit=1")
        v    = rows[0]['value'] if rows else ''
        return int(v) if v else None
    except: return None

# ── Auto-delete messages ──────────────────────────────────────────────────────
async def _autodel(bot, chat_id, msg_id, delay):
    await asyncio.sleep(delay)
    try: await bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except: pass

def autodel(bot, msg, delay=None):
    asyncio.create_task(_autodel(bot, msg.chat_id, msg.message_id, delay or MSG_TTL))

# ── Channel kick/unban ────────────────────────────────────────────────────────
async def kick_user(bot, tg_id: int, plan: str):
    cid = await get_channel_id(plan)
    if not cid: return
    try:
        await bot.ban_chat_member(chat_id=cid, user_id=tg_id)
        await asyncio.sleep(0.3)
        await bot.unban_chat_member(chat_id=cid, user_id=tg_id)
    except Exception as e: log.warning(f"Kick failed uid={tg_id}: {e}")

# ── Handlers ──────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    args = ctx.args or []

    # Always record/ensure user in DB
    await record_user(uid)

    # ── /start verify — returning from payment page ────────────────
    if args and args[0] == 'verify':
        member = await get_member_by_tgid(uid)
        is_active = member and member.get('status') == 'active'

        if is_active:
            plan  = member.get('plan', 'purple')
            utr   = member.get('utr', '')
            jt    = await gen_join_token(uid, utr)
            join_url = f"{STORE_URL}/api/join?t={jt}"
            msg   = await update.message.reply_text(
                '✅',
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Join', url=join_url)]])
            )
            autodel(ctx.bot, msg)
        else:
            msg = await update.message.reply_text(
                'Payment not confirmed yet. Wait a moment then tap /start.',
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Try again', callback_data='billing:monthly')]])
            )
            autodel(ctx.bot, msg)
        return

    # ── /start plans or retry ──────────────────────────────────────
    if args and args[0] in ('plans', 'retry'):
        msg = await update.message.reply_text('Choose your plan 👇', reply_markup=plan_kb())
        autodel(ctx.bot, msg)
        return

    # ── Normal /start — read full state from Supabase ─────────────
    member = await get_member_by_tgid(uid)

    if member and member.get('status') == 'active':
        plan   = member.get('plan', 'purple')
        exp    = (member.get('expires_at') or '')[:10]
        utr    = member.get('utr', '')
        jt     = await gen_join_token(uid, utr)
        join_url = f"{STORE_URL}/api/join?t={jt}"
        msg    = await update.message.reply_text(
            f"Active — expires {exp}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton('Join', url=join_url)],
                [InlineKeyboardButton('🔄 Renew', callback_data='billing:monthly')],
            ])
        )
        autodel(ctx.bot, msg)
        return

    if member and member.get('status') == 'expired':
        msg = await update.message.reply_text(
            'Membership expired — renew to rejoin 👇',
            reply_markup=plan_kb()
        )
        autodel(ctx.bot, msg)
        return

    # New or pending user
    msg = await update.message.reply_text('Choose your plan 👇', reply_markup=plan_kb())
    autodel(ctx.bot, msg)

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    uid = q.from_user.id
    d   = q.data

    if d.startswith('billing:'):
        try: await q.edit_message_reply_markup(reply_markup=plan_kb(d.split(':')[1]))
        except BadRequest: pass
        return

    if d.startswith('plan:'):
        _, plan, billing = d.split(':')
        p    = PLANS[plan]
        amt  = p['yearly'] if billing == 'yearly' else p['price']
        per  = 'year' if billing == 'yearly' else 'month'
        link = pay_url(uid, plan, billing)
        try:
            await q.edit_message_text(
                f"₹{amt}/{per}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"Pay ₹{amt} →", url=link)],
                    [InlineKeyboardButton('← Back', callback_data=f'billing:{billing}')],
                ])
            )
        except BadRequest: pass

# ── Scheduled jobs ────────────────────────────────────────────────────────────
async def job_remind(bot):
    now       = datetime.now(timezone.utc)
    remind_to = (now + timedelta(days=4)).isoformat()
    remind_fr = (now + timedelta(days=3)).isoformat()
    rows = await sb_get(
        f"/members?status=eq.active&expires_at=gte.{remind_fr}&expires_at=lte.{remind_to}"
        f"&telegram_id=not.is.null&select=telegram_id,plan,expires_at"
    )
    for m in rows:
        tgid = m.get('telegram_id')
        exp  = (m.get('expires_at') or '')[:10]
        try:
            msg = await bot.send_message(chat_id=int(tgid),
                text=f"Expires {exp} — renew to keep access 👇",
                reply_markup=plan_kb())
            autodel(bot, msg, delay=86400)
        except Exception as e: log.warning(f"Remind fail {tgid}: {e}")

async def job_expire(bot):
    now  = datetime.now(timezone.utc).isoformat()
    rows = await sb_get(
        f"/members?status=eq.active&expires_at=lt.{now}"
        f"&telegram_id=not.is.null&select=phone,telegram_id,plan"
    )
    for m in rows:
        phone = m.get('phone')
        tgid  = m.get('telegram_id')
        plan  = m.get('plan', 'purple')
        if tgid:
            await kick_user(bot, int(tgid), plan)
            try:
                msg = await bot.send_message(chat_id=int(tgid),
                    text="Expired — renew to rejoin 👇", reply_markup=plan_kb())
                autodel(bot, msg, delay=86400)
            except Exception as e: log.warning(f"Expire fail {tgid}: {e}")
        await sb_patch(f"/members?phone=eq.{phone}", {
            'status': 'expired', 'updated_at': now,
        })

# ── Admin commands ────────────────────────────────────────────────────────────
def is_admin(uid): return uid in ADMIN_IDS

async def stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    rows = await sb_get("/members?select=status,plan")
    from collections import Counter
    by_status = Counter(r.get('status','') for r in rows)
    by_plan   = Counter(r.get('plan','')   for r in rows if r.get('status') == 'active')
    text = (
        f"*Members*\nActive: {by_status.get('active',0)}\n"
        f"Expired: {by_status.get('expired',0)}\nPending: {by_status.get('pending_payment',0)}\n\n"
        f"*Active by plan*\nPurple: {by_plan.get('purple',0)}\n"
        f"Pink: {by_plan.get('pink',0)}\nRoyal Blue: {by_plan.get('royal',0)}"
    )
    msg = await update.message.reply_text(text, parse_mode='Markdown')
    autodel(ctx.bot, msg, delay=60)

async def broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not ctx.args: await update.message.reply_text("Usage: /broadcast Your message"); return
    text = ' '.join(ctx.args)
    rows = await sb_get("/members?status=eq.active&telegram_id=not.is.null&select=telegram_id")
    sent = 0
    for m in rows:
        try: await ctx.bot.send_message(chat_id=int(m['telegram_id']), text=text); sent += 1; await asyncio.sleep(0.05)
        except: pass
    await update.message.reply_text(f"✅ Sent to {sent}")

async def broadcast_plan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(ctx.args) < 2: await update.message.reply_text("Usage: /broadcast_plan purple Your message"); return
    plan, text = ctx.args[0], ' '.join(ctx.args[1:])
    rows = await sb_get(f"/members?status=eq.active&plan=eq.{plan}&telegram_id=not.is.null&select=telegram_id")
    sent = 0
    for m in rows:
        try: await ctx.bot.send_message(chat_id=int(m['telegram_id']), text=text); sent += 1; await asyncio.sleep(0.05)
        except: pass
    await update.message.reply_text(f"✅ Sent to {sent} {plan} members")

async def broadcast_expired(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not ctx.args: await update.message.reply_text("Usage: /broadcast_expired Your message"); return
    text = ' '.join(ctx.args)
    rows = await sb_get("/members?status=eq.expired&telegram_id=not.is.null&select=telegram_id")
    sent = 0
    for m in rows:
        try: await ctx.bot.send_message(chat_id=int(m['telegram_id']), text=text); sent += 1; await asyncio.sleep(0.05)
        except: pass
    await update.message.reply_text(f"✅ Sent to {sent} expired members")

async def admin_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    msg = await update.message.reply_text(
        "*Admin*\n\n"
        "/stats — member counts\n"
        "/broadcast `<msg>` — all active\n"
        "/broadcast\\_plan `purple|pink|royal` `<msg>`\n"
        "/broadcast\\_expired `<msg>`",
        parse_mode='Markdown'
    )
    autodel(ctx.bot, msg, delay=120)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler('start',             start))
    app.add_handler(CommandHandler('admin',             admin_cmd))
    app.add_handler(CommandHandler('stats',             stats))
    app.add_handler(CommandHandler('broadcast',         broadcast))
    app.add_handler(CommandHandler('broadcast_plan',    broadcast_plan))
    app.add_handler(CommandHandler('broadcast_expired', broadcast_expired))
    app.add_handler(CallbackQueryHandler(on_callback))

    sched = AsyncIOScheduler(timezone='Asia/Kolkata')
    sched.add_job(job_remind, 'cron', hour=9,  minute=0,  args=[app.bot])
    sched.add_job(job_expire, 'cron', hour=10, minute=0,  args=[app.bot])
    sched.start()

    log.info("VB Bot started — stateless, all data in Supabase")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
