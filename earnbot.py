import asyncio
import json
import logging
import os
import random
import sqlite3
import string
import time
from contextlib import closing
from datetime import datetime, timedelta
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# =========================================================
#   CONFIGURATION
# =========================================================
BOT_TOKEN = os.getenv("BOT_TOKEN")  # Render এ এনভায়ারনমেন্ট ভেরিয়েবল থেকে নিবে
ADMIN_ID = int(os.getenv("ADMIN_ID", "8502686983"))  # আপনার আইডি দিন
DB_PATH = os.getenv("DB_PATH", "/data/earnbot.db")  # Render persistent disk এ

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
log = logging.getLogger("earnbot")

# =========================================================
#   DATABASE
# =========================================================
def db():
    # ডাটাবেস ডিরেক্টরি তৈরি করুন যদি না থাকে
    db_dir = os.path.dirname(DB_PATH)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
    
    con = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA foreign_keys=ON;")
    return con


def db_init():
    with closing(db()) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            user_id      INTEGER PRIMARY KEY,
            username     TEXT,
            first_name   TEXT,
            lang         TEXT DEFAULT 'en',
            coins        INTEGER DEFAULT 0,
            stars        INTEGER DEFAULT 0,
            xp           INTEGER DEFAULT 0,
            level        INTEGER DEFAULT 1,
            energy       INTEGER DEFAULT 100,
            energy_ts    INTEGER DEFAULT 0,
            join_date    TEXT,
            last_active  TEXT,
            referrer_id  INTEGER,
            ref_count    INTEGER DEFAULT 0,
            vip          INTEGER DEFAULT 0,
            vip_until    TEXT,
            banned       INTEGER DEFAULT 0,
            streak       INTEGER DEFAULT 0,
            last_daily   TEXT,
            tasks_done   INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS channels(
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id   TEXT NOT NULL,
            title     TEXT NOT NULL,
            url       TEXT NOT NULL,
            ord       INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS tasks(
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            kind         TEXT NOT NULL,
            title        TEXT NOT NULL,
            url          TEXT,
            chat_id      TEXT,
            coin_reward  INTEGER DEFAULT 0,
            star_reward  INTEGER DEFAULT 0,
            xp_reward    INTEGER DEFAULT 0,
            energy_cost  INTEGER DEFAULT 1,
            cooldown_s   INTEGER DEFAULT 0,
            daily_limit  INTEGER DEFAULT 1,
            min_level    INTEGER DEFAULT 1,
            active       INTEGER DEFAULT 1,
            clicks       INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS completed_tasks(
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id   INTEGER,
            task_id   INTEGER,
            ts        INTEGER,
            day       TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_ct_user ON completed_tasks(user_id);
        CREATE INDEX IF NOT EXISTS ix_ct_day  ON completed_tasks(day);

        CREATE TABLE IF NOT EXISTS referrals(
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            ref_id    INTEGER,
            user_id   INTEGER UNIQUE,
            ts        INTEGER
        );

        CREATE TABLE IF NOT EXISTS transactions(
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id   INTEGER,
            amount    INTEGER,
            currency  TEXT,
            note      TEXT,
            ts        INTEGER
        );

        CREATE TABLE IF NOT EXISTS withdrawals(
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id   INTEGER,
            method    TEXT,
            number    TEXT,
            amount    INTEGER,
            coins     INTEGER,
            status    TEXT DEFAULT 'pending',
            ts        INTEGER
        );

        CREATE TABLE IF NOT EXISTS shop_items(
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            name      TEXT,
            kind      TEXT,
            price     INTEGER,
            payload   TEXT,
            active    INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS buttons(
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            label     TEXT,
            emoji     TEXT,
            action    TEXT,
            payload   TEXT,
            ord       INTEGER DEFAULT 0,
            active    INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS giveaways(
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            title     TEXT,
            kind      TEXT,
            prize     INTEGER,
            ends_at   INTEGER,
            active    INTEGER DEFAULT 1,
            entries   TEXT DEFAULT '[]'
        );

        CREATE TABLE IF NOT EXISTS settings(
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        """)

        defaults = {
            "max_energy":            "100",
            "energy_regen_min":      "5",
            "vip_max_energy":        "200",
            "daily_coin":            "50",
            "daily_star":            "1",
            "streak_bonus":          "10",
            "mystery_min":           "10",
            "mystery_max":           "200",
            "min_withdraw":          "1000",
            "coin_per_taka":         "100",
            "withdraw_fee_pct":      "5",
            "daily_withdraw_limit":  "1",
            "ref_coin":              "100",
            "ref_star":              "1",
            "ref_xp":                "20",
            "xp_per_level":          "200",
            "spin_cost":             "5",
            "vip_price_stars":       "100",
            "vip_days":              "30",
            "vip_daily_bonus":       "100",
            "welcome_image":         "",
            "channel_msg":           "🌟 Welcome To Premium Earn Bot\n🔐 Join all channels first to continue",
        }
        for k, v in defaults.items():
            con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))

        # Seed shop
        if con.execute("SELECT COUNT(*) FROM shop_items").fetchone()[0] == 0:
            con.executemany(
                "INSERT INTO shop_items(name,kind,price,payload) VALUES(?,?,?,?)",
                [
                    ("⚡ Energy Refill (100)", "energy", 5, "100"),
                    ("🎡 Spin Ticket x5",     "spin",   3, "5"),
                    ("🚀 Coin Boost 2x (1h)", "boost",  10, "boost_2x_3600"),
                    ("👑 VIP 30 Days",         "vip",    100, "30"),
                ],
            )


# ---- Settings helpers ----
def s_get(key: str, default: str = "") -> str:
    with closing(db()) as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

def s_geti(key: str, default: int = 0) -> int:
    try: 
        return int(s_get(key, str(default)))
    except: 
        return default

def s_set(key: str, value):
    with closing(db()) as con:
        con.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

# =========================================================
#   I18N
# =========================================================
DIV  = "━━━━━━━━━━━━━━━━━━"
SDIV = "─── ◆ ───"

T = {
    "en": {
        "welcome":    f"╔══════════════════╗\n   ✨ <b>PREMIUM EARN BOT</b> ✨\n╚══════════════════╝\n\n🔐 <b>Verification Required</b>\n💎 Join all channels below to unlock\n   premium earning features!\n\n{SDIV}",
        "joined":     "✅ <b>Verified Successfully!</b>\n💎 Welcome to the Premium Club.",
        "not_joined": "❌ <b>Access Denied</b>\nPlease join all required channels first.",
        "menu":       f"╔══════════════════╗\n   🏠 <b>MAIN MENU</b>   \n╚══════════════════╝\n\n💎 Hello <b>{{name}}</b>!\n💰 Coins: <b>{{coins}}</b>   ⭐ Stars: <b>{{stars}}</b>\n⚡ Energy: <b>{{energy}}</b>   🏆 Lv.<b>{{lvl}}</b>\n\n{SDIV}\n✨ Choose an option below:",
        "profile":    f"╔══════════════════╗\n   👤 <b>MY PROFILE</b>   \n╚══════════════════╝\n\n🆔 <b>ID:</b> <code>{{id}}</code>\n👤 <b>Name:</b> {{name}}\n\n{SDIV}\n💰 <b>Coins:</b>     {{coins}}\n⭐ <b>Stars:</b>     {{stars}}\n⚡ <b>Energy:</b>    {{energy}}/{{maxe}}\n🏆 <b>Level:</b>     {{lvl}}\n📊 <b>XP:</b>        {{xp}} / {{nxt}}\n🔥 <b>Streak:</b>    {{streak}} days\n👥 <b>Referrals:</b> {{ref}}\n👑 <b>VIP:</b>       {{vip}}\n{SDIV}",
        "earn":       f"╔══════════════════╗\n   💰 <b>EARNING HUB</b>   \n╚══════════════════╝\n\n✨ Pick your favourite way to earn!\n{SDIV}",
        "wallet":     f"╔══════════════════╗\n   👛 <b>MY WALLET</b>   \n╚══════════════════╝\n\n💰 <b>Coins:</b>  {{c}}\n⭐ <b>Stars:</b>  {{s}}\n🏆 <b>XP:</b>     {{x}}\n{SDIV}",
        "no_energy":  "⚡ Not enough energy! Recharge soon.",
        "cooldown":   "⏱ This task is on cooldown.",
        "daily_done": "✅ Daily limit reached for this task.",
        "task_ok":    "🎉 <b>Task Completed!</b>\n💰 +{c}   ⭐ +{s}   🏆 +{x}",
        "back":       "⬅️ Back",
        "lang_set":   "✅ Language updated.",
        "banned":     "🚫 You are banned from this bot.",
        "ref_ok":     "🎁 Referral reward credited!",
        "self_ref":   "❌ You cannot refer yourself.",
    },
    "bn": {
        "welcome":    f"╔══════════════════╗\n   ✨ <b>প্রিমিয়াম আর্ন বট</b> ✨\n╚══════════════════╝\n\n🔐 <b>ভেরিফিকেশন প্রয়োজন</b>\n💎 প্রিমিয়াম ফিচার আনলক করতে\n   সব চ্যানেলে জয়েন করুন!\n\n{SDIV}",
        "joined":     "✅ <b>সফলভাবে ভেরিফাইড!</b>\n💎 প্রিমিয়াম ক্লাবে স্বাগতম।",
        "not_joined": "❌ <b>অ্যাক্সেস ডিনাইড</b>\nঅনুগ্রহ করে আগে সব চ্যানেলে জয়েন করুন।",
        "menu":       f"╔══════════════════╗\n   🏠 <b>প্রধান মেনু</b>   \n╚══════════════════╝\n\n💎 হ্যালো <b>{{name}}</b>!\n💰 কয়েন: <b>{{coins}}</b>   ⭐ স্টার: <b>{{stars}}</b>\n⚡ এনার্জি: <b>{{energy}}</b>   🏆 Lv.<b>{{lvl}}</b>\n\n{SDIV}\n✨ একটি অপশন বাছুন:",
        "profile":    f"╔══════════════════╗\n   👤 <b>আমার প্রোফাইল</b>   \n╚══════════════════╝\n\n🆔 <b>আইডি:</b> <code>{{id}}</code>\n👤 <b>নাম:</b> {{name}}\n\n{SDIV}\n💰 <b>কয়েন:</b>    {{coins}}\n⭐ <b>স্টার:</b>     {{stars}}\n⚡ <b>এনার্জি:</b>   {{energy}}/{{maxe}}\n🏆 <b>লেভেল:</b>    {{lvl}}\n📊 <b>XP:</b>       {{xp}} / {{nxt}}\n🔥 <b>স্ট্রিক:</b>    {{streak}} দিন\n👥 <b>রেফার:</b>     {{ref}}\n👑 <b>VIP:</b>      {{vip}}\n{SDIV}",
        "earn":       f"╔══════════════════╗\n   💰 <b>আর্নিং হাব</b>   \n╚══════════════════╝\n\n✨ আপনার পছন্দের উপায় বাছুন!\n{SDIV}",
        "wallet":     f"╔══════════════════╗\n   👛 <b>আমার ওয়ালেট</b>   \n╚══════════════════╝\n\n💰 <b>কয়েন:</b> {{c}}\n⭐ <b>স্টার:</b> {{s}}\n🏆 <b>XP:</b>    {{x}}\n{SDIV}",
        "no_energy":  "⚡ পর্যাপ্ত এনার্জি নেই! রিচার্জ করুন।",
        "cooldown":   "⏱ এই টাস্ক কুলডাউনে আছে।",
        "daily_done": "✅ আজকের লিমিট শেষ।",
        "task_ok":    "🎉 <b>টাস্ক সম্পন্ন!</b>\n💰 +{c}   ⭐ +{s}   🏆 +{x}",
        "back":       "⬅️ পিছনে",
        "lang_set":   "✅ ভাষা পরিবর্তন হয়েছে।",
        "banned":     "🚫 আপনি ব্যান হয়েছেন।",
        "ref_ok":     "🎁 রেফার রিওয়ার্ড পেয়েছেন!",
        "self_ref":   "❌ নিজেকে রেফার করা যাবে না।",
    },
}

def tr(lang: str, key: str, **kw) -> str:
    s = T.get(lang, T["en"]).get(key) or T["en"].get(key, key)
    return s.format(**kw) if kw else s

# =========================================================
#   USER UTILITIES
# =========================================================
def now_ts() -> int: 
    return int(time.time())

def today() -> str:  
    return datetime.utcnow().strftime("%Y-%m-%d")

def get_user(uid: int) -> Optional[sqlite3.Row]:
    with closing(db()) as con:
        return con.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()

def upsert_user(msg_or_cb) -> sqlite3.Row:
    u = msg_or_cb.from_user
    with closing(db()) as con:
        existing = con.execute("SELECT user_id FROM users WHERE user_id=?", (u.id,)).fetchone()
        if not existing:
            con.execute(
                "INSERT INTO users(user_id,username,first_name,join_date,last_active,energy,energy_ts) "
                "VALUES(?,?,?,?,?,?,?)",
                (u.id, u.username or "", u.first_name or "", today(), today(),
                 s_geti("max_energy", 100), now_ts()),
            )
        else:
            con.execute(
                "UPDATE users SET username=?, first_name=?, last_active=? WHERE user_id=?",
                (u.username or "", u.first_name or "", today(), u.id),
            )
    return get_user(u.id)

def add_balance(uid: int, coins=0, stars=0, xp=0, note=""):
    with closing(db()) as con:
        con.execute(
            "UPDATE users SET coins=coins+?, stars=stars+?, xp=xp+? WHERE user_id=?",
            (coins, stars, xp, uid),
        )
        if coins or stars:
            con.execute(
                "INSERT INTO transactions(user_id,amount,currency,note,ts) VALUES(?,?,?,?,?)",
                (uid, coins if coins else stars, "coin" if coins else "star", note, now_ts()),
            )
        # Level up
        u = con.execute("SELECT xp, level FROM users WHERE user_id=?", (uid,)).fetchone()
        per = s_geti("xp_per_level", 200)
        new_level = max(1, (u["xp"] // per) + 1)
        if new_level != u["level"]:
            con.execute("UPDATE users SET level=? WHERE user_id=?", (new_level, uid))

def regen_energy(uid: int):
    u = get_user(uid)
    if not u: 
        return
    max_e = s_geti("vip_max_energy", 200) if u["vip"] else s_geti("max_energy", 100)
    if u["energy"] >= max_e: 
        return
    mins = max(1, s_geti("energy_regen_min", 5))
    current_ts = u["energy_ts"] or now_ts()
    elapsed = (now_ts() - current_ts) // (mins * 60)
    if elapsed > 0:
        new_e = min(max_e, u["energy"] + int(elapsed))
        with closing(db()) as con:
            con.execute("UPDATE users SET energy=?, energy_ts=? WHERE user_id=?",
                        (new_e, now_ts(), uid))

def consume_energy(uid: int, amount: int) -> bool:
    regen_energy(uid)
    u = get_user(uid)
    if not u or u["energy"] < amount: 
        return False
    with closing(db()) as con:
        con.execute("UPDATE users SET energy=energy-? WHERE user_id=?", (amount, uid))
    return True

def is_admin(uid: int) -> bool: 
    return uid == ADMIN_ID

# =========================================================
#   FORCE-JOIN VERIFICATION
# =========================================================
async def check_joined(bot: Bot, uid: int) -> tuple[bool, list]:
    with closing(db()) as con:
        chans = con.execute("SELECT * FROM channels ORDER BY ord, id").fetchall()
    missing = []
    for c in chans:
        try:
            m = await bot.get_chat_member(c["chat_id"], uid)
            if m.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
                missing.append(c)
        except TelegramBadRequest:
            missing.append(c)
        except Exception:
            missing.append(c)
    return len(missing) == 0, chans

def join_kb(chans) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"📢 ᴊᴏɪɴ • {c['title']}", url=c["url"])] for c in chans]
    rows.append([InlineKeyboardButton(text="✅ ɪ ʜᴀᴠᴇ ᴊᴏɪɴᴇᴅ ✓", callback_data="check_join")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# =========================================================
#   KEYBOARDS
# =========================================================
def main_menu_kb(lang="en", admin=False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="👤 ᴘʀᴏғɪʟᴇ",  callback_data="profile"),
         InlineKeyboardButton(text="👛 ᴡᴀʟʟᴇᴛ",   callback_data="wallet")],
        [InlineKeyboardButton(text="💰 ᴇᴀʀɴ ᴄᴏɪɴ", callback_data="earn"),
         InlineKeyboardButton(text="🎮 ɢᴀᴍᴇ ᴢᴏɴᴇ", callback_data="games")],
        [InlineKeyboardButton(text="🎁 ᴅᴀɪʟʏ ʙᴏɴᴜs", callback_data="daily"),
         InlineKeyboardButton(text="🛍 sʜᴏᴘ",        callback_data="shop")],
        [InlineKeyboardButton(text="👥 ʀᴇғᴇʀʀᴀʟ",  callback_data="ref"),
         InlineKeyboardButton(text="💸 ᴡɪᴛʜᴅʀᴀᴡ",   callback_data="withdraw")],
        [InlineKeyboardButton(text="🏆 ʟᴇᴀᴅᴇʀʙᴏᴀʀᴅ", callback_data="lb"),
         InlineKeyboardButton(text="👑 ᴠɪᴘ ᴄʟᴜʙ",     callback_data="vip")],
        [InlineKeyboardButton(text="🌍 ʟᴀɴɢᴜᴀɢᴇ", callback_data="lang"),
         InlineKeyboardButton(text="💬 sᴜᴘᴘᴏʀᴛ",   callback_data="support")],
    ]
    # Custom buttons
    with closing(db()) as con:
        cbs = con.execute("SELECT * FROM buttons WHERE active=1 ORDER BY ord, id").fetchall()
    for cb in cbs:
        rows.append([InlineKeyboardButton(
            text=f"{cb['emoji'] or '✨'} {cb['label']}",
            callback_data=f"cbtn_{cb['id']}")])
    if admin:
        rows.append([InlineKeyboardButton(text="👑 ＡＤＭＩＮ ＰＡＮＥＬ", callback_data="admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def back_kb(cb="menu", label="⬅️ Back") -> InlineKeyboardMarkup:
    if cb == "menu":
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏠 Main Menu", callback_data="menu")]
        ])
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=cb),
         InlineKeyboardButton(text="🏠 Main Menu", callback_data="menu")]
    ])

def menu_text(user, lang="en") -> str:
    """Build the dynamic main-menu greeting text."""
    return tr(lang, "menu",
              name=user["first_name"] or user["username"] or "User",
              coins=user["coins"], stars=user["stars"],
              energy=user["energy"], lvl=user["level"])

# =========================================================
#   ROUTERS
# =========================================================
router = Router()
admin_router = Router()

# ---------- /start ----------
@router.message(CommandStart())
async def start_handler(msg: Message, state: FSMContext, bot: Bot):
    await state.clear()
    args = msg.text.split(maxsplit=1)
    payload = args[1] if len(args) > 1 else ""
    user = upsert_user(msg)
    if user["banned"]:
        return await msg.answer(tr(user["lang"], "banned"))

    # Referral
    if payload.startswith("ref_"):
        try: 
            rid = int(payload[4:])
        except: 
            rid = 0
        if rid and rid != msg.from_user.id and not user["referrer_id"]:
            with closing(db()) as con:
                exists = con.execute("SELECT 1 FROM referrals WHERE user_id=?",
                                     (msg.from_user.id,)).fetchone()
                if not exists and con.execute("SELECT 1 FROM users WHERE user_id=?",
                                              (rid,)).fetchone():
                    con.execute("INSERT INTO referrals(ref_id,user_id,ts) VALUES(?,?,?)",
                                (rid, msg.from_user.id, now_ts()))
                    con.execute("UPDATE users SET referrer_id=? WHERE user_id=?",
                                (rid, msg.from_user.id))
                    con.execute("UPDATE users SET ref_count=ref_count+1 WHERE user_id=?", (rid,))
            add_balance(rid, coins=s_geti("ref_coin", 100),
                        stars=s_geti("ref_star", 1),
                        xp=s_geti("ref_xp", 20),
                        note=f"Referral {msg.from_user.id}")
            try:
                await bot.send_message(rid, f"🎁 New referral! +{s_geti('ref_coin',100)} 💰")
            except Exception: 
                pass
        elif rid == msg.from_user.id:
            await msg.answer(tr(user["lang"], "self_ref"))

    # Force join check
    ok, chans = await check_joined(bot, msg.from_user.id)
    if chans and not ok and not is_admin(msg.from_user.id):
        return await msg.answer(tr(user["lang"], "welcome"),
                                reply_markup=join_kb(chans))
    user = get_user(msg.from_user.id)
    await msg.answer(menu_text(user, user["lang"]),
                     reply_markup=main_menu_kb(user["lang"], is_admin(msg.from_user.id)))


@router.callback_query(F.data == "check_join")
async def check_join_cb(cb: CallbackQuery, bot: Bot):
    user = upsert_user(cb)
    ok, chans = await check_joined(bot, cb.from_user.id)
    if not ok:
        return await cb.answer(tr(user["lang"], "not_joined"), show_alert=True)
    user = get_user(cb.from_user.id)
    await cb.message.edit_text(
        tr(user["lang"], "joined") + "\n\n" + menu_text(user, user["lang"]),
        reply_markup=main_menu_kb(user["lang"], is_admin(cb.from_user.id)))


@router.callback_query(F.data == "menu")
async def menu_cb(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    user = upsert_user(cb)
    regen_energy(cb.from_user.id)
    user = get_user(cb.from_user.id)
    text = menu_text(user, user["lang"])
    kb = main_menu_kb(user["lang"], is_admin(cb.from_user.id))
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()

# ---------- Profile ----------
@router.callback_query(F.data == "profile")
async def profile_cb(cb: CallbackQuery):
    user = upsert_user(cb)
    regen_energy(cb.from_user.id)
    user = get_user(cb.from_user.id)
    per = s_geti("xp_per_level", 200)
    nxt = user["level"] * per
    max_e = s_geti("vip_max_energy", 200) if user["vip"] else s_geti("max_energy", 100)
    vip_status = "Active 👑" if user["vip"] else "No"
    text = tr(user["lang"], "profile",
              id=user["user_id"], name=user["first_name"] or user["username"] or "-",
              coins=user["coins"], stars=user["stars"], energy=user["energy"], maxe=max_e,
              lvl=user["level"], xp=user["xp"], nxt=nxt,
              streak=user["streak"], ref=user["ref_count"],
              vip=vip_status)
    await cb.message.edit_text(text, reply_markup=back_kb())
    await cb.answer()

# ---------- Wallet ----------
@router.callback_query(F.data == "wallet")
async def wallet_cb(cb: CallbackQuery):
    user = upsert_user(cb)
    with closing(db()) as con:
        txs = con.execute(
            "SELECT * FROM transactions WHERE user_id=? ORDER BY ts DESC LIMIT 6",
            (cb.from_user.id,)).fetchall()
        wd = con.execute(
            "SELECT * FROM withdrawals WHERE user_id=? ORDER BY ts DESC LIMIT 4",
            (cb.from_user.id,)).fetchall()
    txt = tr(user["lang"], "wallet", c=user["coins"], s=user["stars"], x=user["xp"])
    if txs:
        txt += "\n\n📒 <b>Recent</b>\n" + "\n".join(
            f"• {t['note'] or t['currency']}: {'+' if t['amount']>=0 else ''}{t['amount']}"
            for t in txs)
    if wd:
        txt += "\n\n💸 <b>Withdrawals</b>\n" + "\n".join(
            f"• {w['method']} {w['amount']}৳ — {w['status']}" for w in wd)
    await cb.message.edit_text(txt, reply_markup=back_kb())
    await cb.answer()

# ---------- Earn Center ----------
@router.callback_query(F.data == "earn")
async def earn_cb(cb: CallbackQuery):
    user = upsert_user(cb)
    rows = [
        [InlineKeyboardButton(text="📢 ᴄʜᴀɴɴᴇʟ ᴊᴏɪɴ",   callback_data="t_kind_channel"),
         InlineKeyboardButton(text="🔗 sʜᴏʀᴛʟɪɴᴋ",      callback_data="t_kind_shortlink")],
        [InlineKeyboardButton(text="📣 sᴘᴏɴsᴏʀᴇᴅ",     callback_data="t_kind_sponsor"),
         InlineKeyboardButton(text="🎥 ᴡᴀᴛᴄʜ ᴀᴅs",      callback_data="t_kind_ad")],
        [InlineKeyboardButton(text="🎯 ᴏғғᴇʀᴡᴀʟʟ",      callback_data="t_kind_offerwall"),
         InlineKeyboardButton(text="💎 ᴠɪᴘ ᴛᴀsᴋs",      callback_data="t_kind_vip")],
        [InlineKeyboardButton(text="🏠 Main Menu",       callback_data="menu")],
    ]
    await cb.message.edit_text(tr(user["lang"], "earn"),
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()

# ---------- Task list ----------
@router.callback_query(F.data.startswith("t_kind_"))
async def task_list_cb(cb: CallbackQuery):
    user = upsert_user(cb)
    kind = cb.data.split("_", 2)[2]
    with closing(db()) as con:
        tasks = con.execute(
            "SELECT * FROM tasks WHERE kind=? AND active=1 AND min_level<=?",
            (kind, user["level"])).fetchall()
    if not tasks:
        return await cb.answer("No active tasks here yet. Check back soon!", show_alert=True)
    rows = []
    for t in tasks:
        rows.append([InlineKeyboardButton(
            text=f"{t['title']} • +{t['coin_reward']}💰 +{t['star_reward']}⭐",
            callback_data=f"task_{t['id']}")])
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="earn")])
    await cb.message.edit_text(f"🎯 <b>{kind.title()} Tasks</b>",
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()

@router.callback_query(F.data.startswith("task_"))
async def task_open(cb: CallbackQuery, bot: Bot):
    user = upsert_user(cb)
    tid = int(cb.data.split("_")[1])
    with closing(db()) as con:
        t = con.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    if not t: 
        return await cb.answer("Task not found.", show_alert=True)

    # Daily limit
    with closing(db()) as con:
        done_today = con.execute(
            "SELECT COUNT(*) FROM completed_tasks WHERE user_id=? AND task_id=? AND day=?",
            (cb.from_user.id, tid, today())).fetchone()[0]
        last = con.execute(
            "SELECT MAX(ts) FROM completed_tasks WHERE user_id=? AND task_id=?",
            (cb.from_user.id, tid)).fetchone()[0] or 0
    if done_today >= t["daily_limit"]:
        return await cb.answer(tr(user["lang"], "daily_done"), show_alert=True)
    if t["cooldown_s"] and now_ts() - last < t["cooldown_s"]:
        return await cb.answer(tr(user["lang"], "cooldown"), show_alert=True)

    # Channel join task → verify membership
    if t["kind"] == "channel" and t["chat_id"]:
        try:
            m = await bot.get_chat_member(t["chat_id"], cb.from_user.id)
            if m.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
                return await cb.message.edit_text(
                    f"📢 <b>{t['title']}</b>\nJoin and tap verify.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🔗 Open", url=t["url"] or "https://t.me")],
                        [InlineKeyboardButton(text="✅ Verify", callback_data=f"task_{tid}")],
                        [InlineKeyboardButton(text="⬅️ Back", callback_data=f"t_kind_{t['kind']}")],
                    ]))
        except Exception:
            return await cb.answer("Cannot verify — make bot admin in channel.", show_alert=True)

    if not consume_energy(cb.from_user.id, t["energy_cost"]):
        return await cb.answer(tr(user["lang"], "no_energy"), show_alert=True)

    add_balance(cb.from_user.id,
                coins=t["coin_reward"], stars=t["star_reward"], xp=t["xp_reward"],
                note=f"Task #{tid}")
    with closing(db()) as con:
        con.execute(
            "INSERT INTO completed_tasks(user_id,task_id,ts,day) VALUES(?,?,?,?)",
            (cb.from_user.id, tid, now_ts(), today()))
        con.execute("UPDATE users SET tasks_done=tasks_done+1 WHERE user_id=?",
                    (cb.from_user.id,))
        con.execute("UPDATE tasks SET clicks=clicks+1 WHERE id=?", (tid,))
    await cb.answer(tr(user["lang"], "task_ok",
                       c=t["coin_reward"], s=t["star_reward"], x=t["xp_reward"]),
                    show_alert=True)
    await earn_cb(cb)

# ---------- Daily / Streak / Mystery ----------
@router.callback_query(F.data == "daily")
async def daily_cb(cb: CallbackQuery):
    user = upsert_user(cb)
    rows = [
        [InlineKeyboardButton(text="🎁 ᴄʟᴀɪᴍ ᴅᴀɪʟʏ ʙᴏɴᴜs", callback_data="daily_claim")],
        [InlineKeyboardButton(text="📦 ᴍʏsᴛᴇʀʏ ʙᴏx",       callback_data="daily_box"),
         InlineKeyboardButton(text="💎 ᴛʀᴇᴀsᴜʀᴇ ᴄʜᴇsᴛ",    callback_data="daily_chest")],
        [InlineKeyboardButton(text="🏠 Main Menu",          callback_data="menu")],
    ]
    text = (f"╔══════════════════╗\n   🎁 <b>DAILY REWARDS</b>   \n╚══════════════════╝\n\n"
            f"🔥 <b>Current Streak:</b> {user['streak']} days\n"
            f"📅 <b>Last Claim:</b> {user['last_daily'] or 'never'}\n"
            f"🎯 <b>Next Bonus:</b> +{s_geti('daily_coin',50)+s_geti('streak_bonus',10)*user['streak']}💰\n"
            f"{SDIV}\n✨ Keep your streak alive for bigger rewards!")
    await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()

@router.callback_query(F.data == "daily_claim")
async def daily_claim(cb: CallbackQuery):
    user = upsert_user(cb)
    if user["last_daily"] == today():
        return await cb.answer("⏳ Already claimed today. Come back tomorrow!", show_alert=True)
    yest = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
    streak = user["streak"] + 1 if user["last_daily"] == yest else 1
    base = s_geti("daily_coin", 50)
    bonus = s_geti("streak_bonus", 10) * (streak - 1)
    coins = base + bonus
    if user["vip"]: 
        coins += s_geti("vip_daily_bonus", 100)
    stars = s_geti("daily_star", 1)
    add_balance(cb.from_user.id, coins=coins, stars=stars, xp=10, note="Daily bonus")
    with closing(db()) as con:
        con.execute("UPDATE users SET streak=?, last_daily=? WHERE user_id=?",
                    (streak, today(), cb.from_user.id))
    await cb.answer(f"🎉 +{coins}💰  +{stars}⭐\n🔥 Streak: {streak}", show_alert=True)
    await daily_cb(cb)

@router.callback_query(F.data == "daily_box")
async def daily_box(cb: CallbackQuery):
    upsert_user(cb)
    if not consume_energy(cb.from_user.id, 5):
        return await cb.answer("⚡ Need 5 energy.", show_alert=True)
    win = random.randint(s_geti("mystery_min", 10), s_geti("mystery_max", 200))
    add_balance(cb.from_user.id, coins=win, note="Mystery box")
    await cb.answer(f"🎁 You found {win}💰!", show_alert=True)
    await daily_cb(cb)

@router.callback_query(F.data == "daily_chest")
async def daily_chest(cb: CallbackQuery):
    upsert_user(cb)
    if not consume_energy(cb.from_user.id, 10):
        return await cb.answer("⚡ Need 10 energy.", show_alert=True)
    coin = random.randint(50, 500)
    star = random.choice([0, 0, 0, 1, 2])
    add_balance(cb.from_user.id, coins=coin, stars=star, note="Chest")
    await cb.answer(f"💰 Chest: +{coin}💰 +{star}⭐", show_alert=True)
    await daily_cb(cb)

# ---------- Games ----------
@router.callback_query(F.data == "games")
async def games_cb(cb: CallbackQuery):
    upsert_user(cb)
    rows = [
        [InlineKeyboardButton(text="🎡 ʟᴜᴄᴋʏ sᴘɪɴ",     callback_data="g_spin")],
        [InlineKeyboardButton(text="🎲 ᴅɪᴄᴇ ʀᴏʟʟ",      callback_data="g_dice"),
         InlineKeyboardButton(text="🪙 ᴄᴏɪɴ ғʟɪᴘ",      callback_data="g_flip")],
        [InlineKeyboardButton(text="🎮 ᴛᴀᴘ ɢᴀᴍᴇ",       callback_data="g_tap")],
        [InlineKeyboardButton(text="🏠 Main Menu",       callback_data="menu")],
    ]
    text = (f"╔══════════════════╗\n   🎮 <b>GAME ZONE</b>   \n╚══════════════════╝\n\n"
            f"✨ Pick a game and win big rewards!\n"
            f"💎 Each play uses energy.\n{SDIV}")
    await cb.message.edit_text(text,
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()

@router.callback_query(F.data == "g_spin")
async def g_spin(cb: CallbackQuery):
    user = upsert_user(cb)
    cost = s_geti("spin_cost", 5)
    if user["stars"] < cost:
        return await cb.answer(f"Need {cost}⭐ to spin.", show_alert=True)
    with closing(db()) as con:
        con.execute("UPDATE users SET stars=stars-? WHERE user_id=?", (cost, cb.from_user.id))
    prizes = [10, 25, 50, 100, 200, 500, 1000, 0]
    win = random.choice(prizes)
    if win: 
        add_balance(cb.from_user.id, coins=win, note="Spin")
    await cb.answer(f"🎡 Spin result: +{win}💰" if win else "🎡 No win, try again!",
                    show_alert=True)
    await games_cb(cb)

@router.callback_query(F.data == "g_dice")
async def g_dice(cb: CallbackQuery, bot: Bot):
    upsert_user(cb)
    if not consume_energy(cb.from_user.id, 2):
        return await cb.answer("⚡ Need 2 energy.", show_alert=True)
    msg = await bot.send_dice(cb.from_user.id, emoji="🎲")
    await asyncio.sleep(3)
    val = msg.dice.value
    win = val * 10
    add_balance(cb.from_user.id, coins=win, note="Dice")
    await bot.send_message(cb.from_user.id, f"🎲 You rolled {val} → +{win}💰")
    await cb.answer()

@router.callback_query(F.data == "g_flip")
async def g_flip(cb: CallbackQuery):
    upsert_user(cb)
    if not consume_energy(cb.from_user.id, 1):
        return await cb.answer("⚡ Need 1 energy.", show_alert=True)
    win = random.choice([0, 20, 0, 50, 0, 100])
    if win: 
        add_balance(cb.from_user.id, coins=win, note="Coin flip")
    await cb.answer(f"🪙 +{win}💰" if win else "🪙 Tails! No reward.", show_alert=True)

class TapState(StatesGroup): 
    playing = State()

@router.callback_query(F.data == "g_tap")
async def g_tap(cb: CallbackQuery, state: FSMContext):
    upsert_user(cb)
    if not consume_energy(cb.from_user.id, 3):
        return await cb.answer("⚡ Need 3 energy.", show_alert=True)
    await state.set_state(TapState.playing)
    await state.update_data(taps=0, ends=now_ts() + 10)
    rows = [[InlineKeyboardButton(text="👆 TAP! (0)", callback_data="tap_hit")],
            [InlineKeyboardButton(text="✅ Finish",   callback_data="tap_end")]]
    await cb.message.edit_text("🎮 <b>Tap Game</b> — tap as fast as you can in 10s!",
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()

@router.callback_query(F.data == "tap_hit", TapState.playing)
async def tap_hit(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if now_ts() > data.get("ends", 0):
        return await tap_end(cb, state)
    taps = data["taps"] + 1
    await state.update_data(taps=taps)
    rows = [[InlineKeyboardButton(text=f"👆 TAP! ({taps})", callback_data="tap_hit")],
            [InlineKeyboardButton(text="✅ Finish",          callback_data="tap_end")]]
    try:
        await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except TelegramBadRequest: 
        pass
    await cb.answer()

@router.callback_query(F.data == "tap_end", TapState.playing)
async def tap_end(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    taps = data.get("taps", 0)
    win = taps * 2
    add_balance(cb.from_user.id, coins=win, xp=taps, note="Tap game")
    await state.clear()
    await cb.message.edit_text(f"🎮 Done! Taps: <b>{taps}</b>  →  +{win}💰",
                               reply_markup=back_kb("games"))
    await cb.answer()

# ---------- Referral ----------
@router.callback_query(F.data == "ref")
async def ref_cb(cb: CallbackQuery, bot: Bot):
    user = upsert_user(cb)
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{cb.from_user.id}"
    text = (f"╔══════════════════╗\n   👥 <b>REFERRAL PROGRAM</b>\n╚══════════════════╝\n\n"
            f"🔗 <b>Your Invite Link:</b>\n<code>{link}</code>\n\n"
            f"{SDIV}\n"
            f"👥 <b>Total Referrals:</b>  {user['ref_count']}\n"
            f"💰 <b>Per Referral:</b>     {s_geti('ref_coin',100)} coins\n"
            f"⭐ <b>Per Referral:</b>     {s_geti('ref_star',1)} stars\n"
            f"🏆 <b>Per Referral:</b>     {s_geti('ref_xp',20)} XP\n"
            f"{SDIV}\n✨ Share your link and earn forever!")
    rows = [
        [InlineKeyboardButton(text="📤 Share Link",
                              url=f"https://t.me/share/url?url={link}&text=💎 Join the Premium Earn Bot!")],
        [InlineKeyboardButton(text="🏆 Leaderboard", callback_data="lb"),
         InlineKeyboardButton(text="🏠 Main Menu",   callback_data="menu")],
    ]
    await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()

@router.callback_query(F.data == "lb")
async def lb_cb(cb: CallbackQuery):
    upsert_user(cb)
    with closing(db()) as con:
        top = con.execute(
            "SELECT user_id, first_name, ref_count, coins FROM users "
            "ORDER BY ref_count DESC, coins DESC LIMIT 10").fetchall()
    txt = f"╔══════════════════╗\n   🏆 <b>TOP REFERRERS</b>   \n╚══════════════════╝\n\n"
    medals = ["🥇","🥈","🥉"] + ["🎖"]*7
    if not top:
        txt += "No referrers yet — be the first!"
    for i, u in enumerate(top):
        name = (u['first_name'] or str(u['user_id']))[:16]
        txt += f"{medals[i]} <b>{name}</b> — {u['ref_count']} refs · {u['coins']}💰\n"
    txt += f"\n{SDIV}\n✨ Climb the ranks and win prestige!"
    await cb.message.edit_text(txt, reply_markup=back_kb("ref"))
    await cb.answer()

# ---------- Shop ----------
@router.callback_query(F.data == "shop")
async def shop_cb(cb: CallbackQuery):
    user = upsert_user(cb)
    with closing(db()) as con:
        items = con.execute("SELECT * FROM shop_items WHERE active=1").fetchall()
    rows = [[InlineKeyboardButton(text=f"🛍 {it['name']}  ·  {it['price']}⭐",
                                  callback_data=f"buy_{it['id']}")] for it in items]
    rows.append([InlineKeyboardButton(text="🏠 Main Menu", callback_data="menu")])
    text = (f"╔══════════════════╗\n   🛍 <b>PREMIUM SHOP</b>   \n╚══════════════════╝\n\n"
            f"⭐ Your Stars: <b>{user['stars']}</b>\n"
            f"{SDIV}\n✨ Tap an item to purchase:")
    await cb.message.edit_text(text,
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()

@router.callback_query(F.data.startswith("buy_"))
async def buy_cb(cb: CallbackQuery):
    user = upsert_user(cb)
    iid = int(cb.data.split("_")[1])
    with closing(db()) as con:
        it = con.execute("SELECT * FROM shop_items WHERE id=?", (iid,)).fetchone()
    if not it: 
        return await cb.answer("Item missing.", show_alert=True)
    if user["stars"] < it["price"]:
        return await cb.answer("⭐ Not enough stars.", show_alert=True)
    with closing(db()) as con:
        con.execute("UPDATE users SET stars=stars-? WHERE user_id=?",
                    (it["price"], cb.from_user.id))
    if it["kind"] == "energy":
        add_e = int(it["payload"] or 100)
        with closing(db()) as con:
            con.execute("UPDATE users SET energy=energy+? WHERE user_id=?",
                        (add_e, cb.from_user.id))
        await cb.answer(f"⚡ +{add_e} energy!", show_alert=True)
    elif it["kind"] == "vip":
        days = int(it["payload"] or 30)
        until = (datetime.utcnow() + timedelta(days=days)).strftime("%Y-%m-%d")
        with closing(db()) as con:
            con.execute("UPDATE users SET vip=1, vip_until=? WHERE user_id=?",
                        (until, cb.from_user.id))
        await cb.answer(f"👑 VIP activated for {days} days!", show_alert=True)
    elif it["kind"] == "spin":
        add_balance(cb.from_user.id, stars=int(it["payload"] or 5), note="Spin tickets")
        await cb.answer("🎡 Spin tickets credited!", show_alert=True)
    else:
        await cb.answer("✅ Purchase complete.", show_alert=True)
    await shop_cb(cb)

# ---------- VIP ----------
@router.callback_query(F.data == "vip")
async def vip_cb(cb: CallbackQuery):
    user = upsert_user(cb)
    status = ('🟢 Active until ' + user['vip_until']) if user['vip'] else '🔴 Inactive'
    text = (f"╔══════════════════╗\n   👑 <b>VIP CLUB</b>   \n╚══════════════════╝\n\n"
            f"💎 <b>Status:</b> {status}\n"
            f"{SDIV}\n"
            f"<b>✨ VIP Benefits:</b>\n"
            f"  • 🚀 Higher rewards on every task\n"
            f"  • 💎 Access to premium tasks\n"
            f"  • ⚡ {s_geti('vip_max_energy',200)} max energy\n"
            f"  • 🎁 +{s_geti('vip_daily_bonus',100)}💰 daily bonus\n"
            f"  • ⏱ Lower cooldowns\n"
            f"  • 👑 VIP badge\n\n"
            f"{SDIV}\n💳 <b>Price:</b> {s_geti('vip_price_stars',100)}⭐ / {s_geti('vip_days',30)} days")
    rows = [[InlineKeyboardButton(text="💎 Buy VIP Now",  callback_data="shop")],
            [InlineKeyboardButton(text="🏠 Main Menu",     callback_data="menu")]]
    await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()

# ---------- Withdraw ----------
class WithdrawState(StatesGroup):
    method = State()
    amount = State()
    number = State()
    confirm = State()

@router.callback_query(F.data == "withdraw")
async def withdraw_cb(cb: CallbackQuery, state: FSMContext):
    user = upsert_user(cb)
    minw = s_geti("min_withdraw", 1000)
    rate = s_geti("coin_per_taka", 100)
    text = (f"╔══════════════════╗\n   💸 <b>WITHDRAW</b>   \n╚══════════════════╝\n\n"
            f"💰 <b>Your Coins:</b>     {user['coins']}\n"
            f"💵 <b>Available:</b>      {user['coins']//rate}৳\n"
            f"{SDIV}\n"
            f"🔁 <b>Rate:</b>           {rate} coins = 1৳\n"
            f"⬇️ <b>Minimum:</b>        {minw} coins\n"
            f"📅 <b>Daily Limit:</b>    {s_geti('daily_withdraw_limit',1)} request\n"
            f"💳 <b>Fee:</b>            {s_geti('withdraw_fee_pct',5)}%\n"
            f"{SDIV}\n✨ Choose payment method below:")
    rows = [[InlineKeyboardButton(text="📲 ʙᴋᴀsʜ",  callback_data="wd_bkash"),
             InlineKeyboardButton(text="📲 ɴᴀɢᴀᴅ",  callback_data="wd_nagad")],
            [InlineKeyboardButton(text="🏠 Main Menu", callback_data="menu")]]
    await state.set_state(WithdrawState.method)
    await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()

@router.callback_query(F.data.in_({"wd_bkash", "wd_nagad"}), WithdrawState.method)
async def wd_method(cb: CallbackQuery, state: FSMContext):
    method = "Bkash" if cb.data == "wd_bkash" else "Nagad"
    await state.update_data(method=method)
    await state.set_state(WithdrawState.amount)
    await cb.message.edit_text(f"💸 <b>{method}</b>\nEnter amount in <b>coins</b>:",
                               reply_markup=back_kb("withdraw"))
    await cb.answer()

@router.message(WithdrawState.amount)
async def wd_amount(msg: Message, state: FSMContext):
    try: 
        amt = int((msg.text or "").strip())
    except: 
        return await msg.answer("❌ Invalid number.")
    minw = s_geti("min_withdraw", 1000)
    user = get_user(msg.from_user.id)
    if amt < minw: 
        return await msg.answer(f"❌ Minimum {minw} coins.")
    if amt > user["coins"]: 
        return await msg.answer("❌ Not enough coins.")
    with closing(db()) as con:
        cnt = con.execute(
            "SELECT COUNT(*) FROM withdrawals WHERE user_id=? AND date(ts,'unixepoch')=date('now')",
            (msg.from_user.id,)).fetchone()[0]
    if cnt >= s_geti("daily_withdraw_limit", 1):
        return await msg.answer("❌ Daily withdraw limit reached.")
    await state.update_data(amount=amt)
    await state.set_state(WithdrawState.number)
    await msg.answer("📱 Send your payment number:")

@router.message(WithdrawState.number)
async def wd_number(msg: Message, state: FSMContext):
    num = (msg.text or "").strip()
    if not (10 <= len(num) <= 14):
        return await msg.answer("❌ Invalid number.")
    await state.update_data(number=num)
    data = await state.get_data()
    rate = s_geti("coin_per_taka", 100)
    fee = s_geti("withdraw_fee_pct", 5)
    coins = data["amount"]
    taka = (coins // rate)
    taka_net = taka - (taka * fee // 100)
    await state.set_state(WithdrawState.confirm)
    text = (f"📝 <b>Confirm Withdraw</b>\n\n"
            f"Method: {data['method']}\nNumber: <code>{num}</code>\n"
            f"Coins: {coins}\nGross: {taka}৳\nFee {fee}%: {taka-taka_net}৳\n"
            f"Net: <b>{taka_net}৳</b>")
    rows = [[InlineKeyboardButton(text="✅ Submit",  callback_data="wd_submit"),
             InlineKeyboardButton(text="❌ Cancel",  callback_data="menu")]]
    await msg.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

@router.callback_query(F.data == "wd_submit", WithdrawState.confirm)
async def wd_submit(cb: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    rate = s_geti("coin_per_taka", 100)
    coins = data["amount"]
    taka = coins // rate
    with closing(db()) as con:
        con.execute("UPDATE users SET coins=coins-? WHERE user_id=?", (coins, cb.from_user.id))
        cur = con.execute(
            "INSERT INTO withdrawals(user_id,method,number,amount,coins,ts) VALUES(?,?,?,?,?,?)",
            (cb.from_user.id, data["method"], data["number"], taka, coins, now_ts()))
        wid = cur.lastrowid
    await state.clear()
    await cb.message.edit_text("✅ Withdraw request submitted. Awaiting admin review.",
                               reply_markup=back_kb())
    try:
        await bot.send_message(
            ADMIN_ID,
            f"💸 <b>NEW WITHDRAW</b>\nUser: <code>{cb.from_user.id}</code>\n"
            f"Method: {data['method']} {data['number']}\n"
            f"Amount: {taka}৳ ({coins} coins)",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Approve", callback_data=f"wapp_{wid}"),
                 InlineKeyboardButton(text="❌ Reject",  callback_data=f"wrej_{wid}")]
            ]))
    except Exception: 
        pass
    await cb.answer()

# ---------- Language ----------
@router.callback_query(F.data == "lang")
async def lang_cb(cb: CallbackQuery):
    upsert_user(cb)
    rows = [[InlineKeyboardButton(text="🇬🇧 English", callback_data="setlang_en"),
             InlineKeyboardButton(text="🇧🇩 বাংলা",  callback_data="setlang_bn")],
            [InlineKeyboardButton(text="🏠 Main Menu", callback_data="menu")]]
    text = (f"╔══════════════════╗\n   🌍 <b>LANGUAGE</b>   \n╚══════════════════╝\n\n"
            f"🌐 Choose your preferred language\n"
            f"আপনার পছন্দের ভাষা বেছে নিন\n{SDIV}")
    await cb.message.edit_text(text,
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()

@router.callback_query(F.data.startswith("setlang_"))
async def setlang_cb(cb: CallbackQuery, state: FSMContext):
    lang = cb.data.split("_")[1]
    with closing(db()) as con:
        con.execute("UPDATE users SET lang=? WHERE user_id=?", (lang, cb.from_user.id))
    await cb.answer(tr(lang, "lang_set"), show_alert=True)
    user = get_user(cb.from_user.id)
    await state.clear()
    text = menu_text(user, user["lang"])
    kb = main_menu_kb(user["lang"], is_admin(cb.from_user.id))
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        await cb.message.answer(text, reply_markup=kb)

# ---------- Support ----------
@router.callback_query(F.data == "support")
async def support_cb(cb: CallbackQuery):
    upsert_user(cb)
    text = (f"╔══════════════════╗\n   💬 <b>SUPPORT</b>   \n╚══════════════════╝\n\n"
            f"💎 Need help? We're here for you!\n{SDIV}\n"
            f"👤 <b>Admin ID:</b> <code>{ADMIN_ID}</code>\n"
            f"📩 Send a direct message to admin for any issue.\n"
            f"⏱ Average reply time: under 24 hours.\n{SDIV}")
    await cb.message.edit_text(text, reply_markup=back_kb())
    await cb.answer()

# ---------- Custom Buttons ----------
@router.callback_query(F.data.startswith("cbtn_"))
async def custom_button(cb: CallbackQuery):
    upsert_user(cb)
    bid = int(cb.data.split("_")[1])
    with closing(db()) as con:
        b = con.execute("SELECT * FROM buttons WHERE id=?", (bid,)).fetchone()
    if not b: 
        return await cb.answer("Missing.", show_alert=True)
    if b["action"] == "link":
        await cb.message.answer(f"🔗 {b['payload']}")
    elif b["action"] == "text":
        await cb.message.answer(b["payload"] or "")
    elif b["action"] == "reward":
        try: 
            amt = int(b["payload"] or 10)
        except: 
            amt = 10
        add_balance(cb.from_user.id, coins=amt, note=f"Custom btn {bid}")
        await cb.answer(f"🎁 +{amt}💰", show_alert=True)
    else:
        await cb.message.answer(b["payload"] or "✨")
    await cb.answer()

# =========================================================
#   ADMIN PANEL
# =========================================================
def admin_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📢 Manage Channels", callback_data="a_chan"),
         InlineKeyboardButton(text="🎁 Manage Tasks",    callback_data="a_tasks")],
        [InlineKeyboardButton(text="💰 Reward Settings", callback_data="a_set_reward"),
         InlineKeyboardButton(text="⚡ Energy Settings", callback_data="a_set_energy")],
        [InlineKeyboardButton(text="🏆 Level Settings",  callback_data="a_set_level"),
         InlineKeyboardButton(text="👥 Referral Settings",callback_data="a_set_ref")],
        [InlineKeyboardButton(text="🎡 Game Settings",   callback_data="a_set_game"),
         InlineKeyboardButton(text="💳 Withdraw Reqs",   callback_data="a_wd")],
        [InlineKeyboardButton(text="👑 VIP Manager",     callback_data="a_vip"),
         InlineKeyboardButton(text="🛒 Shop Settings",   callback_data="a_shop")],
        [InlineKeyboardButton(text="➕ Add Custom Btn",  callback_data="a_cb_add"),
         InlineKeyboardButton(text="✏ Edit Buttons",     callback_data="a_cb_list")],
        [InlineKeyboardButton(text="👥 Manage Users",    callback_data="a_users"),
         InlineKeyboardButton(text="🚫 Ban / Unban",     callback_data="a_ban")],
        [InlineKeyboardButton(text="📨 Broadcast",       callback_data="a_bc"),
         InlineKeyboardButton(text="🎉 Giveaways",       callback_data="a_gw")],
        [InlineKeyboardButton(text="📊 Statistics",      callback_data="a_stats"),
         InlineKeyboardButton(text="⚙ Bot Settings",     callback_data="a_settings")],
        [InlineKeyboardButton(text="⬅️ Back",            callback_data="menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

@admin_router.callback_query(F.data == "admin")
async def admin_cb(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): 
        return await cb.answer("⛔", show_alert=True)
    await cb.message.edit_text("👑 <b>SUPER ADMIN PANEL</b>", reply_markup=admin_kb())
    await cb.answer()

# ---- Statistics ----
@admin_router.callback_query(F.data == "a_stats")
async def a_stats(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): 
        return
    with closing(db()) as con:
        total   = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        joins   = con.execute("SELECT COUNT(*) FROM users WHERE join_date=?", (today(),)).fetchone()[0]
        tasks   = con.execute("SELECT COUNT(*) FROM completed_tasks").fetchone()[0]
        coins   = con.execute("SELECT SUM(coins) FROM users").fetchone()[0] or 0
        wd_pen  = con.execute("SELECT COUNT(*) FROM withdrawals WHERE status='pending'").fetchone()[0]
        vipc    = con.execute("SELECT COUNT(*) FROM users WHERE vip=1").fetchone()[0]
        top     = con.execute("SELECT first_name, ref_count FROM users ORDER BY ref_count DESC LIMIT 3").fetchall()
    text = (f"📊 <b>Bot Statistics</b>\n\n"
            f"👥 Total users: <b>{total}</b>\n"
            f"🆕 Today's joins: <b>{joins}</b>\n"
            f"✅ Tasks completed: <b>{tasks}</b>\n"
            f"💰 Coins in circulation: <b>{coins}</b>\n"
            f"💸 Pending withdrawals: <b>{wd_pen}</b>\n"
            f"👑 VIP users: <b>{vipc}</b>\n\n"
            f"🏆 Top referrers:\n" +
            "\n".join(f"• {t['first_name']}: {t['ref_count']}" for t in top))
    await cb.message.edit_text(text, reply_markup=back_kb("admin"))
    await cb.answer()

# ---- Channels (force join) ----
class AddChan(StatesGroup): 
    waiting = State()

@admin_router.callback_query(F.data == "a_chan")
async def a_chan(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): 
        return
    with closing(db()) as con:
        chans = con.execute("SELECT * FROM channels ORDER BY ord, id").fetchall()
    rows = [[InlineKeyboardButton(text=f"🗑 {c['title']}", callback_data=f"a_chan_del_{c['id']}")]
            for c in chans]
    rows.append([InlineKeyboardButton(text="➕ Add Channel", callback_data="a_chan_add")])
    rows.append([InlineKeyboardButton(text="⬅️ Back",        callback_data="admin")])
    await cb.message.edit_text("📢 <b>Force-Join Channels</b>",
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()

@admin_router.callback_query(F.data == "a_chan_add")
async def a_chan_add(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): 
        return
    await state.set_state(AddChan.waiting)
    await cb.message.edit_text(
        "Send channel as: <code>@username|Title|https://t.me/username</code>\n"
        "Or for private: <code>-1001234567|Title|https://t.me/+invite</code>",
        reply_markup=back_kb("a_chan"))
    await cb.answer()

@admin_router.message(AddChan.waiting)
async def a_chan_save(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): 
        return
    parts = (msg.text or "").split("|")
    if len(parts) != 3: 
        return await msg.answer("❌ Format wrong.")
    cid, title, url = [p.strip() for p in parts]
    with closing(db()) as con:
        con.execute("INSERT INTO channels(chat_id,title,url) VALUES(?,?,?)",
                    (cid, title, url))
    await state.clear()
    await msg.answer("✅ Channel added.", reply_markup=back_kb("a_chan"))

@admin_router.callback_query(F.data.startswith("a_chan_del_"))
async def a_chan_del(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): 
        return
    cid = int(cb.data.split("_")[3])
    with closing(db()) as con:
        con.execute("DELETE FROM channels WHERE id=?", (cid,))
    await cb.answer("Deleted.", show_alert=True)
    await a_chan(cb)

# ---- Tasks ----
class AddTask(StatesGroup): 
    waiting = State()

@admin_router.callback_query(F.data == "a_tasks")
async def a_tasks(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): 
        return
    with closing(db()) as con:
        tasks = con.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT 30").fetchall()
    rows = [[InlineKeyboardButton(
        text=f"{'✅' if t['active'] else '⛔'} {t['kind']}: {t['title'][:25]}",
        callback_data=f"a_task_{t['id']}")] for t in tasks]
    rows.append([InlineKeyboardButton(text="➕ Add Task", callback_data="a_task_add")])
    rows.append([InlineKeyboardButton(text="⬅️ Back",     callback_data="admin")])
    await cb.message.edit_text("🎁 <b>Tasks</b>",
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()

@admin_router.callback_query(F.data == "a_task_add")
async def a_task_add(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): 
        return
    await state.set_state(AddTask.waiting)
    await cb.message.edit_text(
        "Add task in format:\n"
        "<code>kind|title|url|chat_id|coin|star|xp|energy_cost|cooldown|daily|min_lvl</code>\n\n"
        "kind = channel | shortlink | sponsor | ad | offerwall | vip\n"
        "Use <code>-</code> for empty fields.",
        reply_markup=back_kb("a_tasks"))
    await cb.answer()

@admin_router.message(AddTask.waiting)
async def a_task_save(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): 
        return
    p = [x.strip() for x in (msg.text or "").split("|")]
    if len(p) < 11: 
        return await msg.answer("❌ Need 11 fields.")
    nz = lambda v: None if v in ("-", "") else v
    with closing(db()) as con:
        con.execute(
            "INSERT INTO tasks(kind,title,url,chat_id,coin_reward,star_reward,xp_reward,"
            "energy_cost,cooldown_s,daily_limit,min_level) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (p[0], p[1], nz(p[2]), nz(p[3]),
             int(p[4] or 0), int(p[5] or 0), int(p[6] or 0),
             int(p[7] or 1), int(p[8] or 0), int(p[9] or 1), int(p[10] or 1)))
    await state.clear()
    await msg.answer("✅ Task added.", reply_markup=back_kb("a_tasks"))

@admin_router.callback_query(F.data.regexp(r"^a_task_\d+$"))
async def a_task_view(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): 
        return
    tid = int(cb.data.split("_")[2])
    with closing(db()) as con:
        t = con.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    if not t: 
        return await cb.answer("Missing", show_alert=True)
    text = (f"🎯 <b>Task #{t['id']}</b>\n\n"
            f"Kind: <b>{t['kind']}</b>\nTitle: {t['title']}\n"
            f"💰 {t['coin_reward']}  ⭐ {t['star_reward']}  🏆 {t['xp_reward']}\n"
            f"Energy Cost: {t['energy_cost']}\n"
            f"Cooldown: {t['cooldown_s']}s   Daily: {t['daily_limit']}\n"
            f"Min level: {t['min_level']}   Clicks: {t['clicks']}\n"
            f"Active: {'✅' if t['active'] else '⛔'}")
    rows = [[InlineKeyboardButton(text="🔁 Toggle Active",
                                  callback_data=f"a_task_toggle_{tid}")],
            [InlineKeyboardButton(text="🗑 Delete", callback_data=f"a_task_del_{tid}")],
            [InlineKeyboardButton(text="⬅️ Back",   callback_data="a_tasks")]]
    await cb.message.edit_text(text,
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()

@admin_router.callback_query(F.data.startswith("a_task_toggle_"))
async def a_task_toggle(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): 
        return
    tid = int(cb.data.split("_")[3])
    with closing(db()) as con:
        con.execute("UPDATE tasks SET active=1-active WHERE id=?", (tid,))
    await cb.answer("Toggled.", show_alert=True)
    await a_tasks(cb)

@admin_router.callback_query(F.data.startswith("a_task_del_"))
async def a_task_del(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): 
        return
    tid = int(cb.data.split("_")[3])
    with closing(db()) as con:
        con.execute("DELETE FROM tasks WHERE id=?", (tid,))
    await cb.answer("Deleted.", show_alert=True)
    await a_tasks(cb)

# ---- Settings groups ----
SET_GROUPS = {
    "a_set_reward": ("💰 Reward Settings",
                     ["daily_coin","daily_star","streak_bonus","mystery_min","mystery_max"]),
    "a_set_energy": ("⚡ Energy Settings",
                     ["max_energy","energy_regen_min","vip_max_energy"]),
    "a_set_level":  ("🏆 Level Settings", ["xp_per_level"]),
    "a_set_ref":    ("👥 Referral Settings", ["ref_coin","ref_star","ref_xp"]),
    "a_set_game":   ("🎡 Game Settings", ["spin_cost"]),
    "a_settings":   ("⚙ Bot Settings",
                     ["min_withdraw","coin_per_taka","withdraw_fee_pct",
                      "daily_withdraw_limit","vip_price_stars","vip_days",
                      "vip_daily_bonus","channel_msg"]),
}

class EditSet(StatesGroup): 
    waiting = State()

@admin_router.callback_query(F.data.in_(SET_GROUPS.keys()))
async def a_setgrp(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): 
        return
    title, keys = SET_GROUPS[cb.data]
    rows = [[InlineKeyboardButton(text=f"✏ {k} = {s_get(k)[:25]}",
                                  callback_data=f"a_seted_{k}")] for k in keys]
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="admin")])
    await cb.message.edit_text(f"<b>{title}</b>\nTap a value to edit:",
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()

@admin_router.callback_query(F.data.startswith("a_seted_"))
async def a_seted(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): 
        return
    key = cb.data[len("a_seted_"):]
    await state.set_state(EditSet.waiting)
    await state.update_data(key=key)
    await cb.message.edit_text(
        f"✏ Editing <code>{key}</code>\nCurrent: <b>{s_get(key)}</b>\nSend new value:",
        reply_markup=back_kb("admin"))
    await cb.answer()

@admin_router.message(EditSet.waiting)
async def a_setsave(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): 
        return
    data = await state.get_data()
    s_set(data["key"], (msg.text or "").strip())
    await state.clear()
    await msg.answer("✅ Saved.", reply_markup=back_kb("admin"))

# ---- Withdraw approval ----
@admin_router.callback_query(F.data == "a_wd")
async def a_wd(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): 
        return
    with closing(db()) as con:
        wds = con.execute(
            "SELECT * FROM withdrawals WHERE status='pending' ORDER BY ts DESC LIMIT 20"
        ).fetchall()
    if not wds:
        return await cb.message.edit_text("✅ No pending withdrawals.",
                                          reply_markup=back_kb("admin"))
    rows = []
    for w in wds:
        rows.append([InlineKeyboardButton(
            text=f"#{w['id']} {w['method']} {w['amount']}৳ uid:{w['user_id']}",
            callback_data=f"a_wd_view_{w['id']}")])
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="admin")])
    await cb.message.edit_text("💸 <b>Pending Withdrawals</b>",
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()

@admin_router.callback_query(F.data.startswith("a_wd_view_"))
async def a_wd_view(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): 
        return
    wid = int(cb.data.split("_")[3])
    with closing(db()) as con:
        w = con.execute("SELECT * FROM withdrawals WHERE id=?", (wid,)).fetchone()
    text = (f"💸 <b>Withdraw #{wid}</b>\n\n"
            f"User: <code>{w['user_id']}</code>\nMethod: {w['method']}\n"
            f"Number: <code>{w['number']}</code>\n"
            f"Amount: {w['amount']}৳ ({w['coins']} coins)\nStatus: {w['status']}")
    rows = [[InlineKeyboardButton(text="✅ Approve", callback_data=f"wapp_{wid}"),
             InlineKeyboardButton(text="❌ Reject",  callback_data=f"wrej_{wid}")],
            [InlineKeyboardButton(text="⬅️ Back",    callback_data="a_wd")]]
    await cb.message.edit_text(text,
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()

@admin_router.callback_query(F.data.startswith("wapp_") | F.data.startswith("wrej_"))
async def wd_decide(cb: CallbackQuery, bot: Bot):
    if not is_admin(cb.from_user.id): 
        return
    action, wid = cb.data.split("_")
    wid = int(wid)
    with closing(db()) as con:
        w = con.execute("SELECT * FROM withdrawals WHERE id=?", (wid,)).fetchone()
    if not w or w["status"] != "pending":
        return await cb.answer("Already processed.", show_alert=True)
    new = "approved" if action == "wapp" else "rejected"
    with closing(db()) as con:
        con.execute("UPDATE withdrawals SET status=? WHERE id=?", (new, wid))
        if new == "rejected":
            con.execute("UPDATE users SET coins=coins+? WHERE user_id=?",
                        (w["coins"], w["user_id"]))
    try:
        await bot.send_message(
            w["user_id"],
            f"💸 Your withdraw #{wid} of {w['amount']}৳ was <b>{new.upper()}</b>.")
    except Exception: 
        pass
    await cb.answer(f"Marked {new}", show_alert=True)
    await a_wd(cb)

# ---- VIP manager ----
class VIPMgr(StatesGroup): 
    waiting = State()

@admin_router.callback_query(F.data == "a_vip")
async def a_vip(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): 
        return
    await state.set_state(VIPMgr.waiting)
    await cb.message.edit_text(
        "👑 Send: <code>user_id|days</code>  (days=0 to remove VIP)",
        reply_markup=back_kb("admin"))
    await cb.answer()

@admin_router.message(VIPMgr.waiting)
async def a_vip_save(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): 
        return
    try:
        uid_s, days_s = (msg.text or "").split("|")
        uid = int(uid_s)
        days = int(days_s)
    except: 
        return await msg.answer("❌ Wrong format.")
    if days <= 0:
        with closing(db()) as con:
            con.execute("UPDATE users SET vip=0, vip_until=NULL WHERE user_id=?", (uid,))
        await msg.answer("✅ VIP removed.")
    else:
        until = (datetime.utcnow() + timedelta(days=days)).strftime("%Y-%m-%d")
        with closing(db()) as con:
            con.execute("UPDATE users SET vip=1, vip_until=? WHERE user_id=?", (until, uid))
        await msg.answer(f"✅ VIP set until {until}.")
    await state.clear()

# ---- Shop manager ----
class ShopMgr(StatesGroup): 
    waiting = State()

@admin_router.callback_query(F.data == "a_shop")
async def a_shop(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): 
        return
    with closing(db()) as con:
        items = con.execute("SELECT * FROM shop_items").fetchall()
    rows = [[InlineKeyboardButton(
        text=f"{'✅' if it['active'] else '⛔'} {it['name']} — {it['price']}⭐",
        callback_data=f"a_shop_t_{it['id']}")] for it in items]
    rows.append([InlineKeyboardButton(text="➕ Add Item", callback_data="a_shop_add")])
    rows.append([InlineKeyboardButton(text="⬅️ Back",     callback_data="admin")])
    await cb.message.edit_text("🛒 <b>Shop Items</b>",
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()

@admin_router.callback_query(F.data == "a_shop_add")
async def a_shop_add(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): 
        return
    await state.set_state(ShopMgr.waiting)
    await cb.message.edit_text(
        "Format: <code>name|kind|price|payload</code>\n"
        "kind = energy|spin|vip|boost",
        reply_markup=back_kb("a_shop"))
    await cb.answer()

@admin_router.message(ShopMgr.waiting)
async def a_shop_save(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): 
        return
    p = [x.strip() for x in (msg.text or "").split("|")]
    if len(p) < 4: 
        return await msg.answer("❌ Need 4 fields.")
    with closing(db()) as con:
        con.execute("INSERT INTO shop_items(name,kind,price,payload) VALUES(?,?,?,?)",
                    (p[0], p[1], int(p[2]), p[3]))
    await state.clear()
    await msg.answer("✅ Added.", reply_markup=back_kb("a_shop"))

@admin_router.callback_query(F.data.startswith("a_shop_t_"))
async def a_shop_toggle(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): 
        return
    iid = int(cb.data.split("_")[3])
    with closing(db()) as con:
        con.execute("UPDATE shop_items SET active=1-active WHERE id=?", (iid,))
    await cb.answer("Toggled.", show_alert=True)
    await a_shop(cb)

# ---- Custom buttons ----
class CBAdd(StatesGroup): 
    waiting = State()

@admin_router.callback_query(F.data == "a_cb_add")
async def a_cb_add(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): 
        return
    await state.set_state(CBAdd.waiting)
    await cb.message.edit_text(
        "Format: <code>label|emoji|action|payload</code>\n"
        "action = link | text | reward | sponsor",
        reply_markup=back_kb("admin"))
    await cb.answer()

@admin_router.message(CBAdd.waiting)
async def a_cb_save(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): 
        return
    p = [x.strip() for x in (msg.text or "").split("|")]
    if len(p) < 4: 
        return await msg.answer("❌ Need 4 fields.")
    with closing(db()) as con:
        con.execute("INSERT INTO buttons(label,emoji,action,payload) VALUES(?,?,?,?)",
                    (p[0], p[1], p[2], p[3]))
    await state.clear()
    await msg.answer("✅ Button added.", reply_markup=back_kb("admin"))

@admin_router.callback_query(F.data == "a_cb_list")
async def a_cb_list(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): 
        return
    with closing(db()) as con:
        bs = con.execute("SELECT * FROM buttons").fetchall()
    if not bs:
        return await cb.message.edit_text("No custom buttons yet.", reply_markup=back_kb("admin"))
    rows = [[InlineKeyboardButton(text=f"🗑 {b['emoji']} {b['label']}",
                                  callback_data=f"a_cb_del_{b['id']}")] for b in bs]
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="admin")])
    await cb.message.edit_text("🧩 Tap to delete a button:",
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()

@admin_router.callback_query(F.data.startswith("a_cb_del_"))
async def a_cb_del(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): 
        return
    bid = int(cb.data.split("_")[3])
    with closing(db()) as con:
        con.execute("DELETE FROM buttons WHERE id=?", (bid,))
    await cb.answer("Deleted.", show_alert=True)
    await a_cb_list(cb)

# ---- Users / Ban ----
class BanState(StatesGroup): 
    waiting = State()

@admin_router.callback_query(F.data == "a_users")
async def a_users(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): 
        return
    with closing(db()) as con:
        latest = con.execute(
            "SELECT user_id,first_name,coins,banned FROM users ORDER BY user_id DESC LIMIT 15"
        ).fetchall()
    txt = "👥 <b>Latest Users</b>\n\n" + "\n".join(
        f"{'🚫' if u['banned'] else '✅'} <code>{u['user_id']}</code> {u['first_name']} — {u['coins']}💰"
        for u in latest)
    await cb.message.edit_text(txt, reply_markup=back_kb("admin"))
    await cb.answer()

@admin_router.callback_query(F.data == "a_ban")
async def a_ban(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): 
        return
    await state.set_state(BanState.waiting)
    await cb.message.edit_text("🚫 Send <code>user_id</code> to toggle ban.",
                               reply_markup=back_kb("admin"))
    await cb.answer()

@admin_router.message(BanState.waiting)
async def a_ban_do(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): 
        return
    try: 
        uid = int((msg.text or "").strip())
    except: 
        return await msg.answer("❌ Bad id.")
    with closing(db()) as con:
        con.execute("UPDATE users SET banned=1-banned WHERE user_id=?", (uid,))
    await state.clear()
    await msg.answer("✅ Toggled.", reply_markup=back_kb("admin"))

# ---- Broadcast ----
class BcState(StatesGroup): 
    waiting = State()

@admin_router.callback_query(F.data == "a_bc")
async def a_bc(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): 
        return
    await state.set_state(BcState.waiting)
    await cb.message.edit_text("📨 Send the broadcast message (HTML allowed):",
                               reply_markup=back_kb("admin"))
    await cb.answer()

@admin_router.message(BcState.waiting)
async def a_bc_send(msg: Message, state: FSMContext, bot: Bot):
    if not is_admin(msg.from_user.id): 
        return
    await state.clear()
    with closing(db()) as con:
        ids = [r[0] for r in con.execute("SELECT user_id FROM users WHERE banned=0").fetchall()]
    sent = fail = 0
    status = await msg.answer(f"📨 Sending to {len(ids)} users…")
    for uid in ids:
        try:
            await bot.send_message(uid, msg.html_text or msg.text or "")
            sent += 1
        except Exception:
            fail += 1
        if (sent + fail) % 25 == 0:
            try: 
                await status.edit_text(f"📨 Sent: {sent}  Failed: {fail}")
            except: 
                pass
        await asyncio.sleep(0.05)
    await status.edit_text(f"✅ Broadcast done.\nSent: {sent}\nFailed: {fail}")

# ---- Giveaways ----
class GwState(StatesGroup): 
    waiting = State()

@admin_router.callback_query(F.data == "a_gw")
async def a_gw(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): 
        return
    with closing(db()) as con:
        gws = con.execute("SELECT * FROM giveaways ORDER BY id DESC LIMIT 10").fetchall()
    rows = [[InlineKeyboardButton(
        text=f"{'🟢' if g['active'] else '🔴'} #{g['id']} {g['title']} ({g['prize']}💰)",
        callback_data=f"a_gw_end_{g['id']}")] for g in gws]
    rows.append([InlineKeyboardButton(text="➕ New Giveaway", callback_data="a_gw_add")])
    rows.append([InlineKeyboardButton(text="⬅️ Back",         callback_data="admin")])
    await cb.message.edit_text("🎉 <b>Giveaways</b>\nTap a giveaway to draw a winner.",
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()

@admin_router.callback_query(F.data == "a_gw_add")
async def a_gw_add(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): 
        return
    await state.set_state(GwState.waiting)
    await cb.message.edit_text(
        "Format: <code>title|prize_coins|hours</code>",
        reply_markup=back_kb("a_gw"))
    await cb.answer()

@admin_router.message(GwState.waiting)
async def a_gw_save(msg: Message, state: FSMContext, bot: Bot):
    if not is_admin(msg.from_user.id): 
        return
    p = [x.strip() for x in (msg.text or "").split("|")]
    if len(p) < 3: 
        return await msg.answer("❌ Need 3 fields.")
    ends = now_ts() + int(p[2]) * 3600
    with closing(db()) as con:
        cur = con.execute(
            "INSERT INTO giveaways(title,kind,prize,ends_at) VALUES(?,?,?,?)",
            (p[0], "coin", int(p[1]), ends))
        gid = cur.lastrowid
        ids = [r[0] for r in con.execute("SELECT user_id FROM users WHERE banned=0").fetchall()]
    await state.clear()
    await msg.answer(f"✅ Giveaway #{gid} created. Notifying users…")
    for uid in ids[:500]:
        try:
            await bot.send_message(uid, f"🎉 New Giveaway: <b>{p[0]}</b>\nPrize: {p[1]}💰")
        except Exception: 
            pass
        await asyncio.sleep(0.03)

@admin_router.callback_query(F.data.startswith("a_gw_end_"))
async def a_gw_end(cb: CallbackQuery, bot: Bot):
    if not is_admin(cb.from_user.id): 
        return
    gid = int(cb.data.split("_")[3])
    with closing(db()) as con:
        g = con.execute("SELECT * FROM giveaways WHERE id=?", (gid,)).fetchone()
        users = [r[0] for r in con.execute(
            "SELECT user_id FROM users WHERE banned=0").fetchall()]
    if not users: 
        return await cb.answer("No users.", show_alert=True)
    winner = random.choice(users)
    add_balance(winner, coins=g["prize"], note=f"Giveaway #{gid}")
    with closing(db()) as con:
        con.execute("UPDATE giveaways SET active=0 WHERE id=?", (gid,))
    try:
        await bot.send_message(
            winner, f"🎉 You won the giveaway <b>{g['title']}</b>: +{g['prize']}💰")
    except Exception: 
        pass
    await cb.answer(f"Winner: {winner}", show_alert=True)
    await a_gw(cb)

# =========================================================
#   STARTUP
# =========================================================
async def main():
    db_init()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(admin_router)
    dp.include_router(router)
    log.info("🚀 Premium Earn Bot starting…")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Stopped.")