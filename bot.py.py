"""
Telegram Referral Payout Bot
=============================
- Every user who DMs the bot gets a unique invite link to your group.
- Telegram itself tracks who joined via which link (native invite-link tracking).
- Bot counts joins per referrer, builds a balance ($100 / 1000 invites = $0.10/invite).
- At 1000 invites, the user can request a withdrawal (choose crypto + wallet address).
- Withdrawal requests are NOT paid automatically. They go into a "pending" queue that
  only YOU (the admin) can see and approve/deny. You send the crypto yourself, manually,
  then mark the request approved so the bot won't double-count it.

Environment variables required (set these on your host, not in this file):
  BOT_TOKEN   - from @BotFather
  GROUP_ID    - the numeric ID of your group (looks like -1001234567890)
  ADMIN_ID    - your personal numeric Telegram user ID

See README.md for how to get all three of these from your phone.
"""

import os
import sqlite3
import logging
from datetime import datetime, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CommandHandler,
    ChatMemberHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ["BOT_TOKEN"]
GROUP_ID = int(os.environ["GROUP_ID"])
ADMIN_ID = int(os.environ["ADMIN_ID"])

TARGET_INVITES = 1000
PAYOUT_USD = 100.00
PER_INVITE_USD = round(PAYOUT_USD / TARGET_INVITES, 4)  # 0.10

DB_PATH = os.environ.get("DB_PATH", "referral_bot.db")

SUPPORTED_CRYPTO = ["BTC", "ETH", "USDT (TRC20)", "USDT (ERC20)", "SOL", "LTC"]

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("referral_bot")

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    c = conn.cursor()
    c.execute(
        """CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            invite_link TEXT UNIQUE,
            ref_count INTEGER DEFAULT 0,
            balance REAL DEFAULT 0,
            paid_out REAL DEFAULT 0,
            created_at TEXT
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS join_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            new_user_id INTEGER UNIQUE,
            referrer_id INTEGER,
            joined_at TEXT
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            crypto TEXT,
            wallet_address TEXT,
            amount REAL,
            status TEXT DEFAULT 'pending',
            requested_at TEXT,
            resolved_at TEXT,
            note TEXT
        )"""
    )
    conn.commit()
    conn.close()


def now():
    return datetime.now(timezone.utc).isoformat()


def get_user(user_id):
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row


def upsert_user(user_id, username):
    conn = db()
    conn.execute(
        "INSERT INTO users (user_id, username, created_at) VALUES (?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username",
        (user_id, username, now()),
    )
    conn.commit()
    conn.close()


def set_invite_link(user_id, link):
    conn = db()
    conn.execute("UPDATE users SET invite_link=? WHERE user_id=?", (link, user_id))
    conn.commit()
    conn.close()


def has_pending_withdrawal(user_id):
    conn = db()
    row = conn.execute(
        "SELECT * FROM withdrawals WHERE user_id=? AND status='pending'", (user_id,)
    ).fetchone()
    conn.close()
    return row


# ---------------------------------------------------------------------------
# User-facing commands
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return  # only respond in DM

    user = update.effective_user
    upsert_user(user.id, user.username or user.first_name)

    row = get_user(user.id)
    if row["invite_link"]:
        link = row["invite_link"]
    else:
        invite = await context.bot.create_chat_invite_link(
            chat_id=GROUP_ID,
            name=f"ref_{user.id}",
            creates_join_request=False,
        )
        link = invite.invite_link
        set_invite_link(user.id, link)

    text = (
        f"Welcome! Here's your personal invite link:\n\n{link}\n\n"
        f"Share it — every new person who joins the group through this link counts "
        f"toward your total.\n\n"
        f"At {TARGET_INVITES} joins you unlock a ${PAYOUT_USD:.2f} payout in the crypto "
        f"of your choice (${PER_INVITE_USD:.2f} per invite).\n\n"
        f"Check progress any time with /balance."
    )
    await update.message.reply_text(text)


async def mylink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    row = get_user(update.effective_user.id)
    if not row or not row["invite_link"]:
        await update.message.reply_text("Run /start first to get your link.")
        return
    await update.message.reply_text(row["invite_link"])


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    row = get_user(update.effective_user.id)
    if not row:
        await update.message.reply_text("Run /start first.")
        return

    remaining = max(TARGET_INVITES - row["ref_count"], 0)
    pct = min(row["ref_count"] / TARGET_INVITES * 100, 100)
    lines = [
        f"Invites: {row['ref_count']} / {TARGET_INVITES} ({pct:.1f}%)",
        f"Balance: ${row['balance']:.2f}",
        f"Already paid out: ${row['paid_out']:.2f}",
    ]
    if remaining > 0:
        lines.append(f"{remaining} more invite(s) to unlock withdrawal.")
    else:
        pending = has_pending_withdrawal(update.effective_user.id)
        if pending:
            lines.append("You have a withdrawal request pending admin approval.")
        else:
            lines.append("You've hit the target! Use /withdraw to request payout.")
    await update.message.reply_text("\n".join(lines))


async def withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    user_id = update.effective_user.id
    row = get_user(user_id)
    if not row:
        await update.message.reply_text("Run /start first.")
        return

    if row["ref_count"] < TARGET_INVITES:
        await update.message.reply_text(
            f"Not yet — you need {TARGET_INVITES} invites "
            f"({row['ref_count']}/{TARGET_INVITES} so far)."
        )
        return

    if row["balance"] <= 0:
        await update.message.reply_text("No withdrawable balance.")
        return

    if has_pending_withdrawal(user_id):
        await update.message.reply_text(
            "You already have a withdrawal request pending approval — sit tight."
        )
        return

    keyboard = [
        [InlineKeyboardButton(c, callback_data=f"crypto:{c}")]
        for c in SUPPORTED_CRYPTO
    ]
    await update.message.reply_text(
        "Choose the crypto you'd like to be paid in:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def crypto_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    crypto = query.data.split(":", 1)[1]
    context.user_data["pending_withdrawal_crypto"] = crypto
    await query.edit_message_text(
        f"Got it — {crypto}.\n\nNow reply with your {crypto} wallet address."
    )


async def receive_wallet_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    crypto = context.user_data.get("pending_withdrawal_crypto")
    if not crypto:
        return  # not in the middle of a withdrawal flow, ignore

    address = update.message.text.strip()
    user_id = update.effective_user.id
    row = get_user(user_id)

    if not row or row["ref_count"] < TARGET_INVITES or row["balance"] <= 0:
        await update.message.reply_text("Withdrawal no longer eligible.")
        context.user_data.pop("pending_withdrawal_crypto", None)
        return

    conn = db()
    conn.execute(
        "INSERT INTO withdrawals (user_id, crypto, wallet_address, amount, requested_at) "
        "VALUES (?,?,?,?,?)",
        (user_id, crypto, address, row["balance"], now()),
    )
    withdrawal_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()

    context.user_data.pop("pending_withdrawal_crypto", None)

    await update.message.reply_text(
        f"Withdrawal request #{withdrawal_id} submitted for ${row['balance']:.2f} in "
        f"{crypto}, sent to:\n{address}\n\n"
        f"This is reviewed manually — you'll be paid once approved."
    )

    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            f"New withdrawal request #{withdrawal_id}\n"
            f"User: {row['username']} ({user_id})\n"
            f"Amount: ${row['balance']:.2f}\n"
            f"Crypto: {crypto}\n"
            f"Address: {address}\n\n"
            f"Use /pending to review, /approve {withdrawal_id} once you've paid, "
            f"or /deny {withdrawal_id} <reason>."
        ),
    )


# ---------------------------------------------------------------------------
# Join tracking (native Telegram invite-link tracking)
# ---------------------------------------------------------------------------

async def track_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmu = update.chat_member
    if cmu.chat.id != GROUP_ID:
        return

    old_status = cmu.old_chat_member.status
    new_status = cmu.new_chat_member.status

    became_member = old_status in (
        ChatMemberStatus.LEFT,
        ChatMemberStatus.KICKED,
        ChatMemberStatus.RESTRICTED,
    ) and new_status in (ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED, ChatMemberStatus.ADMINISTRATOR)

    if not became_member:
        return

    new_user = cmu.new_chat_member.user
    if new_user.is_bot:
        return

    invite_link_obj = cmu.invite_link
    if not invite_link_obj:
        return  # joined without a tracked link (e.g. added manually) — nothing to credit

    conn = db()
    referrer = conn.execute(
        "SELECT user_id FROM users WHERE invite_link=?", (invite_link_obj.invite_link,)
    ).fetchone()

    if not referrer:
        conn.close()
        return

    referrer_id = referrer["user_id"]

    if referrer_id == new_user.id:
        conn.close()
        return  # no self-referrals

    already = conn.execute(
        "SELECT 1 FROM join_events WHERE new_user_id=?", (new_user.id,)
    ).fetchone()
    if already:
        conn.close()
        return  # this person has already been credited once, ever (no rejoin farming)

    conn.execute(
        "INSERT INTO join_events (new_user_id, referrer_id, joined_at) VALUES (?,?,?)",
        (new_user.id, referrer_id, now()),
    )
    conn.execute(
        "UPDATE users SET ref_count = ref_count + 1, balance = balance + ? WHERE user_id=?",
        (PER_INVITE_USD, referrer_id),
    )
    conn.commit()
    conn.close()

    try:
        updated = get_user(referrer_id)
        msg = f"🎉 Someone joined via your link! Total: {updated['ref_count']}/{TARGET_INVITES}"
        if updated["ref_count"] >= TARGET_INVITES:
            msg += f"\n\nYou've hit the target! Run /withdraw to request your ${PAYOUT_USD:.2f} payout."
        await context.bot.send_message(chat_id=referrer_id, text=msg)
    except Exception as e:
        log.warning(f"Couldn't DM referrer {referrer_id}: {e}")


# ---------------------------------------------------------------------------
# Admin-only commands
# ---------------------------------------------------------------------------

def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_ID:
            return
        return await func(update, context)
    return wrapper


@admin_only
async def pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    rows = conn.execute(
        "SELECT * FROM withdrawals WHERE status='pending' ORDER BY requested_at"
    ).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("No pending withdrawal requests.")
        return

    lines = []
    for r in rows:
        u = get_user(r["user_id"])
        uname = u["username"] if u else r["user_id"]
        lines.append(
            f"#{r['id']} — {uname} — ${r['amount']:.2f} {r['crypto']} — {r['wallet_address']}"
        )
    await update.message.reply_text("\n\n".join(lines))


@admin_only
async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /approve <withdrawal_id>")
        return
    try:
        wid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Withdrawal id must be a number.")
        return

    conn = db()
    w = conn.execute("SELECT * FROM withdrawals WHERE id=?", (wid,)).fetchone()
    if not w or w["status"] != "pending":
        conn.close()
        await update.message.reply_text("No pending withdrawal with that id.")
        return

    conn.execute(
        "UPDATE withdrawals SET status='approved', resolved_at=? WHERE id=?", (now(), wid)
    )
    conn.execute(
        "UPDATE users SET balance = balance - ?, paid_out = paid_out + ? WHERE user_id=?",
        (w["amount"], w["amount"], w["user_id"]),
    )
    conn.commit()
    conn.close()

    await update.message.reply_text(f"Marked #{wid} approved and paid.")
    try:
        await context.bot.send_message(
            chat_id=w["user_id"],
            text=f"Your withdrawal #{wid} for ${w['amount']:.2f} {w['crypto']} has been paid out. 🎉",
        )
    except Exception as e:
        log.warning(f"Couldn't DM user about approval: {e}")


@admin_only
async def deny(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /deny <withdrawal_id> [reason]")
        return
    try:
        wid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Withdrawal id must be a number.")
        return
    reason = " ".join(context.args[1:]) or "No reason given"

    conn = db()
    w = conn.execute("SELECT * FROM withdrawals WHERE id=?", (wid,)).fetchone()
    if not w or w["status"] != "pending":
        conn.close()
        await update.message.reply_text("No pending withdrawal with that id.")
        return

    conn.execute(
        "UPDATE withdrawals SET status='denied', resolved_at=?, note=? WHERE id=?",
        (now(), reason, wid),
    )
    conn.commit()
    conn.close()

    await update.message.reply_text(f"Marked #{wid} denied.")
    try:
        await context.bot.send_message(
            chat_id=w["user_id"],
            text=f"Your withdrawal #{wid} was denied. Reason: {reason}\n"
                 f"Your balance has not been deducted — contact the admin if you think this is a mistake.",
        )
    except Exception as e:
        log.warning(f"Couldn't DM user about denial: {e}")


@admin_only
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    total_referrals = conn.execute("SELECT COUNT(*) FROM join_events").fetchone()[0]
    total_pending = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(amount),0) FROM withdrawals WHERE status='pending'"
    ).fetchone()
    total_paid = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(amount),0) FROM withdrawals WHERE status='approved'"
    ).fetchone()
    conn.close()

    await update.message.reply_text(
        f"Users started bot: {total_users}\n"
        f"Total tracked joins: {total_referrals}\n"
        f"Pending withdrawals: {total_pending[0]} (${total_pending[1]:.2f})\n"
        f"Paid out: {total_paid[0]} (${total_paid[1]:.2f})"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("mylink", mylink))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("withdraw", withdraw))
    app.add_handler(CallbackQueryHandler(crypto_chosen, pattern=r"^crypto:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_wallet_address))

    app.add_handler(CommandHandler("pending", pending))
    app.add_handler(CommandHandler("approve", approve))
    app.add_handler(CommandHandler("deny", deny))
    app.add_handler(CommandHandler("stats", stats))

    app.add_handler(ChatMemberHandler(track_chat_member, ChatMemberHandler.CHAT_MEMBER))

    log.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
