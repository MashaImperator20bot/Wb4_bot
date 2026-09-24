import asyncio
import logging
import os
import re

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
BOT_TOKEN = os.getenv("BOT_TOKEN")  # токен берётся из переменной окружения хостинга
if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не задан. Добавьте переменную окружения BOT_TOKEN "
        "в настройках хостинга (не храните токен в коде!)."
    )

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))   # твой user_id (для /grant), 0 = отключено
CHECK_INTERVAL = 30               # как часто проверять цены (минуты)
FREE_LIMIT = 3                    # товаров бесплатно
PREMIUM_LIMIT = 20                # товаров в Premium
PREMIUM_PRICE = 100               # Stars за 30 дней
PREMIUM_DAYS = 30
DB_PATH = "wb_bot.db"
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
                premium_until TEXT
            )
        """)
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


# ============ WILDBERRIES API ============
def get_price(article):
    url = "https://card.wb.ru/cards/v4/detail"
    params = {"appType": 1, "curr": "rub", "dest": -1257786, "spp": 30, "nm": article}
    try:
        r = curl_requests.get(url, params=params, impersonate="chrome", timeout=15)
        r.raise_for_status()
        products = r.json().get("data", {}).get("products", [])
        if not products:
            return None
        p = products[0]
        name = p.get("name", f"Товар {article}")
        sizes = p.get("sizes", [])
        if sizes and sizes[0].get("price"):
            price_kop = sizes[0]["price"].get("product") or sizes[0]["price"].get("basic")
            if price_kop:
                return {"name": name, "price": price_kop // 100}
        sale = p.get("salePriceU")
        if sale:
            return {"name": name, "price": sale // 100}
        return None
    except Exception as e:
        logging.error(f"WB API error: {e}")
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
    text = (
        "👋 <b>WB Цена-Следилка</b>\n\n"
        "Слежу за ценами на Wildberries и пишу, когда они падают.\n\n"
        "<b>Как пользоваться:</b>\n"
        "1. Нажми «➕ Добавить товар»\n"
        "2. Кинь ссылку с WB или артикул\n"
        "3. Выбери режим:\n"
        "   • 🎯 До моей цены — напишу, когда цена упадёт до указанной\n"
        "   • 📉 Любое снижение — напишу при падении ниже текущей\n\n"
        f"Бесплатно — <b>{FREE_LIMIT} товара</b>.\n"
        f"⭐ Premium — <b>{PREMIUM_LIMIT} товаров</b> за {PREMIUM_PRICE} Stars/мес.\n\n"
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
        f"Бесплатно: {FREE_LIMIT} товара. Premium: {PREMIUM_LIMIT}.",
        reply_markup=back_kb()
    )
    await call.answer()


# --- Добавление ---
@dp.callback_query(F.data == "add")
async def add_start(call: types.CallbackQuery, state: FSMContext):
    items = await get_user_items(call.from_user.id)
    premium = await is_premium(call.from_user.id)
    limit = PREMIUM_LIMIT if premium else FREE_LIMIT
    if len(items) >= limit:
        text = f"⚠️ Лимит — {limit} товаров."
        if not premium:
            text += f"\n\nОформи ⭐ Premium — до {PREMIUM_LIMIT} товаров."
        else:
            text += "\nУдали что-нибудь, чтобы добавить новое."
        kb = back_kb() if premium else premium_kb()
        await call.message.edit_text(text, reply_markup=kb)
        await call.answer()
        return
    await call.message.edit_text(
        "🔗 Пришли ссылку на товар с WB или артикул (цифры).",
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
    data = get_price(article)
    if not data:
        await message.answer("❌ Не смог получить цену. Проверь артикул или попробуй позже.")
        return
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
    premium = await is_premium(call.from_user.id)
    if premium:
        text = f"⭐ <b>Premium активен</b>\n\nДо {PREMIUM_LIMIT} товаров в слежке."
    else:
        text = (
            f"⭐ <b>Premium</b>\n\n"
            f"• До {PREMIUM_LIMIT} товаров вместо {FREE_LIMIT}\n"
            f"• Цена: {PREMIUM_PRICE} Stars / 30 дней\n\n"
            "Оплата через Telegram Stars — без карт."
        )
    await call.message.edit_text(text, reply_markup=premium_kb())
    await call.answer()


@dp.callback_query(F.data == "buy_premium")
async def buy_premium(call: types.CallbackQuery):
    await bot.send_invoice(
        chat_id=call.from_user.id,
        title="Premium на 30 дней",
        description=f"До {PREMIUM_LIMIT} товаров в слежке за ценой",
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
        f"Теперь можно следить за {PREMIUM_LIMIT} товарами.",
        reply_markup=main_kb()
    )


# --- Админ: выдать Premium для теста ---
@dp.message(Command("grant"))
async def grant(message: types.Message):
    if ADMIN_ID == 0 or message.from_user.id != ADMIN_ID:
        return
    await set_premium(message.from_user.id, days=PREMIUM_DAYS)
    await message.answer(f"✅ Premium выдан на {PREMIUM_DAYS} дней (тест).")


# ============ ФОНОВАЯ ПРОВЕРКА ============
async def check_prices():
    logging.info("Проверка цен...")
    items = await get_all_items()
    for item_id, user_id, article, name, old_price, target, mode in items:
        data = get_price(article)
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
