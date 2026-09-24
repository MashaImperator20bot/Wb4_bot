import asyncio
import logging
import os
import random
import re
import time

import aiosqlite
from curl_cffi import requests as curl_requests
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ============ НАСТРОЙКИ ============
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не задан. Добавьте переменную окружения BOT_TOKEN "
        "в настройках хостинга."
    )

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHECK_INTERVAL = 30
FREE_CONCURRENT = 3          # одновременно бесплатно
PREMIUM_CONCURRENT = 20      # одновременно в Premium
FREE_TOTAL_LIMIT = 15        # всего бесплатных добавлений за всё время
REFERRAL_BONUS = 8           # +8 добавлений за друга
PREMIUM_PRICE = 100
PREMIUM_DAYS = 30
DB_PATH = "wb_bot.db"
MAX_BASKET = 50
# ====================================

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler()


# ============ БАЗА ДАННЫХ ============
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tracked_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                article INTEGER NOT NULL,
                name TEXT,
                current_price INTEGER,
                target_price INTEGER,
                mode TEXT DEFAULT 'any_drop',
                last_checked TEXT,
                UNIQUE(user_id, article)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                premium_until TEXT,
                total_added INTEGER DEFAULT 0,
                referral_bonus INTEGER DEFAULT 0,
                referred_by INTEGER
            )
        """)
        # Миграция: если таблицы users не было с новыми полями
        for col in ("total_added", "referral_bonus", "referred_by"):
            try:
                await db.execute(f"ALTER TABLE users ADD COLUMN {col} INTEGER")
            except Exception:
                pass
        await db.commit()


async def user_exists(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)) as cur:
            return await cur.fetchone() is not None


async def ensure_user(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, total_added, referral_bonus) VALUES (?, 0, 0)",
            (user_id,)
        )
        await db.commit()


async def get_user_stats(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT total_added, referral_bonus FROM users WHERE user_id = ?",
            (user_id,)
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return (0, 0)
            return (row[0] or 0, row[1] or 0)


async def increment_total_added(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET total_added = COALESCE(total_added, 0) + 1 WHERE user_id = ?",
            (user_id,)
        )
        await db.commit()


async def add_referral_bonus(user_id, amount):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (user_id, referral_bonus)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
              referral_bonus = COALESCE(referral_bonus, 0) + ?
        """, (user_id, amount, amount))
        await db.commit()


async def set_referred_by(user_id, referrer_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET referred_by = ? WHERE user_id = ?",
            (referrer_id, user_id)
        )
        await db.commit()


async def add_item(user_id, article, name, current_price, target_price, mode):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR REPLACE INTO tracked_items
            (user_id, article, name, current_price, target_price, mode, last_checked)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
        """, (user_id, article, name, current_price, target_price, mode))
        await db.commit()


async def get_user_items(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT article, name, current_price, target_price, mode FROM tracked_items WHERE user_id = ?",
            (user_id,)
        ) as cursor:
            return await cursor.fetchall()


async def remove_item(user_id, article):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM tracked_items WHERE user_id = ? AND article = ?",
                         (user_id, article))
        await db.commit()


async def get_all_items():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, user_id, article, name, current_price, target_price, mode FROM tracked_items"
        ) as cursor:
            return await cursor.fetchall()


async def update_price(item_id, new_price):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tracked_items SET current_price = ?, last_checked = datetime('now') WHERE id = ?",
            (new_price, item_id)
        )
        await db.commit()


async def set_premium(user_id, days=PREMIUM_DAYS):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (user_id, premium_until)
            VALUES (?, datetime('now', '+' || ? || ' days'))
            ON CONFLICT(user_id) DO UPDATE SET
              premium_until = datetime(
                CASE WHEN premium_until > datetime('now') THEN premium_until ELSE datetime('now') END,
                '+' || ? || ' days'
              )
        """, (user_id, days, days))
        await db.commit()


async def is_premium(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT premium_until FROM users WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row or not row[0]:
                return False
            async with db.execute(
                "SELECT datetime(?) > datetime('now')", (row[0],)
            ) as c2:
                res = await c2.fetchone()
                return bool(res[0])


# ============ WILDBERRIES: ЦЕНА ЧЕРЕЗ CDN ============
def _extract_price_kop(price_obj):
    if price_obj is None:
        return None
    if isinstance(price_obj, dict):
        return price_obj.get("RUB")
    return price_obj


def get_price(article, retries=2):
    vol = article // 100000
    part = article // 1000

    for attempt in range(retries):
        for basket_num in range(1, MAX_BASKET + 1):
            basket = f"{basket_num:02d}"

            name = f"Товар {article}"
            url_card = (
                f"https://basket-{basket}.wbbasket.ru"
                f"/vol{vol}/part{part}/{article}/info/ru/card.json"
            )
            try:
                r = curl_requests.get(url_card, impersonate="chrome120", timeout=8)
                if r.status_code == 200:
                    card = r.json()
                    name = card.get("imt_name") or card.get("subj_name") or name
            except Exception as e:
                logging.debug(f"[WB] card.json CDN {basket}: {type(e).__name__}")

            url_hist = (
                f"https://basket-{basket}.wbbasket.ru"
                f"/vol{vol}/part{part}/{article}/info/price-history.json"
            )
            try:
                rh = curl_requests.get(url_hist, impersonate="chrome120", timeout=8)
                if rh.status_code == 200:
                    hist = rh.json()
                    if isinstance(hist, list) and hist:
                        entry = hist[-1]
                        price_kop = _extract_price_kop(entry.get("price"))
                        if price_kop:
                            logging.info(f"[WB] CDN {basket}: {name} — {price_kop // 100} ₽")
                            return {"name": name, "price": price_kop // 100}
            except Exception as e:
                logging.debug(f"[WB] price-history CDN {basket}: {type(e).__name__}")

    logging.error(f"[WB] Не удалось получить цену для {article}")
    return None


# ============ СОСТОЯНИЯ ============
class AddItem(StatesGroup):
    waiting_link = State()
    waiting_mode = State()
    waiting_target = State()


# ============ КЛАВИАТУРЫ ============
def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить товар", callback_data="add")],
        [InlineKeyboardButton(text="📋 Мои товары", callback_data="list")],
        [InlineKeyboardButton(text="⭐ Premium", callback_data="premium")],
        [InlineKeyboardButton(text="🎁 Пригласить друга", callback_data="referral")],
        [InlineKeyboardButton(text="❓ Инструкция", callback_data="help")],
    ])


def mode_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎯 Следить до моей цены", callback_data="mode_target")],
        [InlineKeyboardButton(text="📉 Любое снижение", callback_data="mode_any")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back")],
    ])


def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back")]
    ])


def premium_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⭐ Купить Premium — {PREMIUM_PRICE} Stars",
                              callback_data="buy_premium")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back")],
    ])


# ============ ХЭНДЛЕРЫ ============
@dp.message(CommandStart())
async def start(message: types.Message, state: FSMContext):
    await state.clear()

    user_id = message.from_user.id
    is_new = not await user_exists(user_id)
    await ensure_user(user_id)

    # Разбор реферальной ссылки
    args = (message.text or "").split()
    if len(args) > 1 and args[1].startswith("ref_"):
        try:
            referrer_id = int(args[1][4:])
        except ValueError:
            referrer_id = None

        if referrer_id and referrer_id != user_id and is_new:
            await add_referral_bonus(referrer_id, REFERRAL_BONUS)
            await set_referred_by(user_id, referrer_id)
            try:
                await bot.send_message(
                    referrer_id,
                    f"🎉 <b>По твоей ссылке пришёл друг!</b>\n\n"
                    f"Тебе начислено <b>+{REFERRAL_BONUS}</b> добавлений товаров."
                )
            except Exception as e:
                logging.error(f"Не смог уведомить реферера {referrer_id}: {e}")

    text = (
        "👋 <b>WB Цена-Следилка</b>\n\n"
        "Слежу за ценами на Wildberries и пишу, когда они падают.\n\n"
        "<b>Как пользоваться:</b>\n"
        "1. Нажми «➕ Добавить товар»\n"
        "2. Кинь ссылку с WB или артикул\n"
        "3. Выбери режим:\n"
        "   • 🎯 До моей цены — напишу, когда цена упадёт до указанной\n"
        "   • 📉 Любое снижение — напишу при падении ниже текущей\n\n"
        f"<b>Лимиты:</b>\n"
        f"• Бесплатно: <b>{FREE_TOTAL_LIMIT} добавлений</b> за всё время\n"
        f"• За друга: <b>+{REFERRAL_BONUS}</b> добавлений\n"
        f"• ⭐ Premium — снимает лимит\n\n"
        f"Проверка каждые <b>{CHECK_INTERVAL} минут</b>."
    )
    await message.answer(text, reply_markup=main_kb())


@dp.callback_query(F.data == "back")
async def back_handler(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Главное меню:", reply_markup=main_kb())
    await call.answer()


@dp.callback_query(F.data == "help")
async def help_handler(call: types.CallbackQuery):
    await call.message.edit_text(
        "📖 <b>Инструкция</b>\n\n"
        "• <b>Добавить товар</b> — пришли ссылку <code>wildberries.ru/catalog/12345678/detail.aspx</code> "
        "или просто число <code>12345678</code>.\n"
        "• <b>До моей цены</b> — бот напишет, когда цена станет ≤ указанной.\n"
        "• <b>Любое снижение</b> — напишет при первом падении.\n"
        "• <b>Мои товары</b> — список, можно удалять.\n\n"
        f"<b>Лимиты:</b> {FREE_TOTAL_LIMIT} добавлений бесплатно, +{REFERRAL_BONUS} за друга. "
        f"Premium снимает лимит.",
        reply_markup=back_kb()
    )
    await call.answer()


# --- Реферальная ссылка ---
@dp.callback_query(F.data == "referral")
async def referral_info(call: types.CallbackQuery):
    bot_info = await bot.get_me()
    link = f"https://t.me/{bot_info.username}?start=ref_{call.from_user.id}"
    stats = await get_user_stats(call.from_user.id)
    text = (
        f"🎁 <b>Пригласи друга — получи +{REFERRAL_BONUS}</b>\n\n"
        f"За каждого друга, который зайдёт по твоей ссылке, "
        f"ты получаешь <b>+{REFERRAL_BONUS}</b> добавлений товаров навсегда.\n\n"
        f"Твоя ссылка:\n<code>{link}</code>\n\n"
        f"<i>Просто скинь её другу — когда он запустит бота, "
        f"бонус начислится автоматически.</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📤 Поделиться",
            url=f"https://t.me/share/url?url={link}&text=Бот для слежки за ценами на WB"
        )],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back")],
    ])
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()


# --- Диагностика ---
@dp.message(Command("test"))
async def test_cdn(message: types.Message):
    article = 1465864387
    args = message.text.split()
    if len(args) > 1 and args[1].isdigit():
        article = int(args[1])

    vol = article // 100000
    part = article // 1000

    working_basket = None
    for i in range(1, MAX_BASKET + 1):
        b = f"{i:02d}"
        url = f"https://basket-{b}.wbbasket.ru/vol{vol}/part{part}/{article}/info/ru/card.json"
        try:
            r = curl_requests.get(url, impersonate="chrome120", timeout=5)
            if r.status_code == 200:
                working_basket = b
                break
        except Exception:
            continue

    if not working_basket:
        await message.answer("❌ Ни один CDN не вернул 200")
        return

    await message.answer(f"✅ CDN: {working_basket}, артикул: {article}, vol={vol}, part={part}")

    url_hist = f"https://basket-{working_basket}.wbbasket.ru/vol{vol}/part{part}/{article}/info/price-history.json"
    try:
        rh = curl_requests.get(url_hist, impersonate="chrome120", timeout=10)
        await message.answer(f"<b>price-history</b> ({rh.status_code}):\n<pre>{rh.text[:3800]}</pre>")
    except Exception as e:
        await message.answer(f"Ошибка: {type(e).__name__}: {e}")


# --- Добавление ---
@dp.callback_query(F.data == "add")
async def add_start(call: types.CallbackQuery, state: FSMContext):
    user_id = call.from_user.id
    await ensure_user(user_id)

    items = await get_user_items(user_id)
    premium = await is_premium(user_id)
    total_added, referral_bonus = await get_user_stats(user_id)

    concurrent_limit = PREMIUM_CONCURRENT if premium else FREE_CONCURRENT
    total_limit = FREE_TOTAL_LIMIT + referral_bonus

    # 1. Проверка одновременного лимита
    if len(items) >= concurrent_limit:
        text = f"⚠️ Одновременно можно следить за <b>{concurrent_limit}</b> товарами."
        if not premium:
            text += (
                f"\n\nУдали что-нибудь из списка или оформи ⭐ Premium — "
                f"тогда будет до {PREMIUM_CONCURRENT}."
            )
        else:
            text += "\nУдали что-нибудь, чтобы добавить новое."
        kb = back_kb() if premium else premium_kb()
        await call.message.edit_text(text, reply_markup=kb)
        await call.answer()
        return

    # 2. Проверка лимита «за всё время» (только для не-Premium)
    if not premium and total_added >= total_limit:
        text = (
            f"🔒 <b>Лимит исчерпан</b>\n\n"
            f"Ты использовал <b>{total_added}</b> из <b>{total_limit}</b> доступных добавлений.\n\n"
            f"<b>Как получить больше:</b>\n"
            f"• 🎁 Пригласи друга — <b>+{REFERRAL_BONUS}</b> добавлений навсегда\n"
            f"• ⭐ Купи Premium — снимает лимит совсем\n"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎁 Пригласить друга", callback_data="referral")],
            [InlineKeyboardButton(text=f"⭐ Premium — {PREMIUM_PRICE} Stars",
                                  callback_data="buy_premium")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back")],
        ])
        await call.message.edit_text(text, reply_markup=kb)
        await call.answer()
        return

    # Всё ок — показываем оставшийся лимит
    if not premium:
        left = total_limit - total_added
        header = f"Осталось бесплатных добавлений: <b>{left}</b>\n\n"
    else:
        header = ""

    await call.message.edit_text(
        header + "🔗 Пришли ссылку на товар с WB или артикул (цифры).",
        reply_markup=back_kb()
    )
    await state.set_state(AddItem.waiting_link)
    await call.answer()


@dp.message(AddItem.waiting_link)
async def add_link(message: types.Message, state: FSMContext):
    match = re.search(r"(\d{5,})", message.text or "")
    if not match:
        await message.answer("❌ Не вижу артикул. Пришли ссылку WB или число (например, 12345678).")
        return
    article = int(match.group(1))

    msg = await message.answer("🔍 Ищу цену, подожди 3–10 секунд...")
    data = get_price(article)

    if not data:
        await msg.edit_text("❌ Не смог получить цену. Проверь артикул или попробуй позже.")
        return

    await msg.delete()
    await state.update_data(article=article, name=data["name"], current_price=data["price"])
    await message.answer(
        f"📦 <b>{data['name']}</b>\n"
        f"Артикул: <code>{article}</code>\n"
        f"Текущая цена: <b>{data['price']} ₽</b>\n\n"
        f"Выбери режим слежки:",
        reply_markup=mode_kb()
    )
    await state.set_state(AddItem.waiting_mode)


@dp.callback_query(F.data.startswith("mode_"), AddItem.waiting_mode)
async def add_mode(call: types.CallbackQuery, state: FSMContext):
    mode = "target" if call.data == "mode_target" else "any_drop"
    await state.update_data(mode=mode)
    if mode == "target":
        await call.message.edit_text(
            "🎯 Введи целевую цену в рублях (число).\n"
            "Напишу, когда цена упадёт до неё.",
            reply_markup=back_kb()
        )
        await state.set_state(AddItem.waiting_target)
    else:
        d = await state.get_data()
        await add_item(call.from_user.id, d["article"], d["name"],
                       d["current_price"], None, "any_drop")
        await increment_total_added(call.from_user.id)
        await call.message.edit_text(
            f"✅ Добавлено!\n\n📦 {d['name']}\nРежим: <b>любое снижение</b>\nСлежу 👀",
            reply_markup=main_kb()
        )
        await state.clear()
    await call.answer()


@dp.message(AddItem.waiting_target)
async def add_target(message: types.Message, state: FSMContext):
    if not (message.text or "").strip().isdigit():
        await message.answer("❌ Введи число без пробелов и знаков.")
        return
    target = int(message.text.strip())
    d = await state.get_data()
    if target >= d["current_price"]:
        await message.answer(f"⚠️ Текущая цена уже {d['current_price']} ₽. Целевая должна быть ниже.")
        return
    await add_item(message.from_user.id, d["article"], d["name"],
                   d["current_price"], target, "target")
    await increment_total_added(message.from_user.id)
    await message.answer(
        f"✅ Добавлено!\n\n📦 {d['name']}\n🎯 Целевая: <b>{target} ₽</b>\nНапишу, когда упадёт.",
        reply_markup=main_kb()
    )
    await state.clear()


# --- Список ---
@dp.callback_query(F.data == "list")
async def list_items(call: types.CallbackQuery):
    items = await get_user_items(call.from_user.id)
    if not items:
        await call.message.edit_text("📭 Список пуст.", reply_markup=back_kb())
        await call.answer()
        return
    text = "📋 <b>Твои товары:</b>\n\n"
    kb = []
    for article, name, current, target, mode in items:
        mode_str = f"до {target} ₽" if mode == "target" else "любое снижение"
        text += f"• <b>{name}</b>\n  Артикул: <code>{article}</code>\n  Текущая: {current} ₽ | {mode_str}\n\n"
        kb.append([InlineKeyboardButton(text=f"🗑 Удалить {article}", callback_data=f"del_{article}")])
    kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back")])
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await call.answer()


@dp.callback_query(F.data.startswith("del_"))
async def delete_item(call: types.CallbackQuery):
    article = int(call.data.split("_")[1])
    await remove_item(call.from_user.id, article)
    await call.answer("Удалено")
    await list_items(call)


# --- Premium ---
@dp.callback_query(F.data == "premium")
async def premium_info(call: types.CallbackQuery):
    user_id = call.from_user.id
    premium = await is_premium(user_id)
    total_added, referral_bonus = await get_user_stats(user_id)
    total_limit = FREE_TOTAL_LIMIT + referral_bonus

    if premium:
        status = "⭐ <b>Premium активен</b>\n\nДо 20 товаров одновременно, лимит добавлений снят."
    else:
        status = (
            f"⭐ <b>Premium</b>\n\n"
            f"• Снимает лимит добавлений\n"
            f"• До 20 товаров одновременно\n"
            f"• Цена: {PREMIUM_PRICE} Stars / 30 дней\n\n"
            f"<b>Твой статус:</b> использовано {total_added} из {total_limit} добавлений\n\n"
            f"Оплата через Telegram Stars — без карт."
        )
    await call.message.edit_text(status, reply_markup=premium_kb())
    await call.answer()


@dp.callback_query(F.data == "buy_premium")
async def buy_premium(call: types.CallbackQuery):
    await bot.send_invoice(
        chat_id=call.from_user.id,
        title="Premium на 30 дней",
        description="Снимает лимит добавлений, до 20 товаров одновременно",
        payload=f"premium_{call.from_user.id}",
        provider_token="",
        currency="XTR",
        prices=[types.LabeledPrice(label="Premium 30 дней", amount=PREMIUM_PRICE)],
    )
    await call.answer()


@dp.pre_checkout_query()
async def pre_checkout(q: types.PreCheckoutQuery):
    await q.answer(ok=True)


@dp.message(F.successful_payment)
async def on_payment(message: types.Message):
    await set_premium(message.from_user.id, days=PREMIUM_DAYS)
    await message.answer(
        f"✅ <b>Premium активирован на {PREMIUM_DAYS} дней!</b>\n\n"
        f"Теперь лимит добавлений снят, и можно следить за 20 товарами одновременно.",
        reply_markup=main_kb()
    )


# --- Админ: выдать Premium для теста ---
@dp.message(Command("grant"))
async def grant(message: types.Message):
    if ADMIN_ID == 0 or message.from_user.id != ADMIN_ID:
        return
    await ensure_user(message.from_user.id)
    await set_premium(message.from_user.id, days=PREMIUM_DAYS)
    await message.answer(f"✅ Premium выдан на {PREMIUM_DAYS} дней (тест).")


# --- Админ: сбросить счётчики для теста ---
@dp.message(Command("reset"))
async def reset_user(message: types.Message):
    if ADMIN_ID == 0 or message.from_user.id != ADMIN_ID:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET total_added = 0, referral_bonus = 0 WHERE user_id = ?",
            (message.from_user.id,)
        )
        await db.commit()
    await message.answer("✅ Счётчики сброшены.")


# ============ ФОНОВАЯ ПРОВЕРКА ============
async def check_prices():
    logging.info("Проверка цен...")
    items = await get_all_items()
    for item_id, user_id, article, name, old_price, target, mode in items:
        data = get_price(article)
        await asyncio.sleep(2)
        if not data:
            continue
        new_price = data["price"]
        if new_price == old_price:
            continue
        notify = False
        if mode == "target" and target and new_price <= target:
            notify = True
        elif mode == "any_drop" and new_price < old_price:
            notify = True
        if notify:
            try:
                await bot.send_message(
                    user_id,
                    f"🔔 <b>Цена упала!</b>\n\n"
                    f"📦 {name}\n"
                    f"Артикул: <code>{article}</code>\n"
                    f"Было: {old_price} ₽ → Стало: <b>{new_price} ₽</b>\n\n"
                    f"https://wildberries.ru/catalog/{article}/detail.aspx"
                )
            except Exception as e:
                logging.error(f"Не смог отправить {user_id}: {e}")
        await update_price(item_id, new_price)


# ============ ЗАПУСК ============
async def main():
    await init_db()
    scheduler.add_job(check_prices, "interval", minutes=CHECK_INTERVAL)
    scheduler.start()
    logging.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
