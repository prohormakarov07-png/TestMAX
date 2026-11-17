"""Демо-бот барбершопа «Чёрный дуб» (aiogram 3.x).

Запуск: python bot.py   (см. README.md)
Бизнес-логика и база данных в storage.py, все тексты и цены в config.py.
"""
import asyncio
import html
import logging
import re
from datetime import date, datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config as C
import storage as S

log = logging.getLogger("barbershop")
BOT_USERNAME = ""

# ============================ Кнопки главного меню ============================
B_BOOK = "📅 Записаться"
B_BARBERS = "💈 Барберы"
B_SERVICES = "💰 Услуги и цены"
B_MY = "📋 Мои записи"
B_REVIEWS = "⭐ Отзывы"
B_PROMO = "🎁 Акции"
B_CONTACTS = "📍 Адрес и контакты"
B_FAQ = "❓ Вопросы и ответы"
B_WRITE = "✉️ Написать администратору"
B_CANCEL = "❌ Отмена"
MENU_TEXTS = {B_BOOK, B_BARBERS, B_SERVICES, B_MY, B_REVIEWS, B_PROMO, B_CONTACTS, B_FAQ, B_WRITE, B_CANCEL}

MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=B_BOOK)],
        [KeyboardButton(text=B_BARBERS), KeyboardButton(text=B_SERVICES)],
        [KeyboardButton(text=B_MY), KeyboardButton(text=B_REVIEWS)],
        [KeyboardButton(text=B_PROMO), KeyboardButton(text=B_CONTACTS)],
        [KeyboardButton(text=B_FAQ), KeyboardButton(text=B_WRITE)],
    ],
    resize_keyboard=True,
    input_field_placeholder="Выберите раздел",
)

PHONE_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📱 Отправить мой номер", request_contact=True)],
        [KeyboardButton(text=B_CANCEL)],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)

# Базовая дата для «каталожных» цен: будний день после 12:00, без скидок
LIST_DT = datetime(2030, 1, 7, 15, 0)


# ================================== Состояния =================================
class Book(StatesGroup):
    phone = State()


class Review(StatesGroup):
    comment = State()


class Support(StatesGroup):
    text = State()


class AdminReply(StatesGroup):
    text = State()


class Broadcast(StatesGroup):
    text = State()


# ================================== Хелперы ===================================
WD = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


def esc(s):
    return html.escape(str(s or ""))


def money(n):
    return f"{int(n):,}".replace(",", " ") + " ₽"


def fmt_day(d):
    return f"{WD[d.weekday()]}, {d.day} {MONTHS[d.month - 1]}"


def fmt_dt(dt):
    return f"{fmt_day(dt)}, {dt:%H:%M}"


def plural(n, one, few, many):
    n = abs(int(n)) % 100
    if 11 <= n <= 19:
        return many
    return {1: one, 2: few, 3: few, 4: few}.get(n % 10, many)


def stars(avg):
    full = int(round(avg))
    return "★" * full + "☆" * (5 - full)


def rating_line(key=None):
    avg, n = S.barber_rating(key)
    if not n:
        return "пока нет оценок"
    return f"{stars(avg)} {avg} ({n} {plural(n, 'отзыв', 'отзыва', 'отзывов')})"


def barber_price(service_key, barber_key):
    return S.calc_price(service_key, barber_key, LIST_DT)[0]


def price_text(service_key):
    lo, hi = S.price_range(service_key)
    return money(lo) if lo == hi else f"{money(lo)} – {money(hi)}"


def days_off_text(barber_key):
    off = sorted(C.BARBERS[barber_key]["days_off"])
    return ", ".join(WD[i] for i in off) if off else "без выходных"


def is_admin(uid):
    return uid in C.ADMIN_IDS


def ikb(rows):
    """rows: список рядов [(текст, callback_data | http-ссылка), ...]"""
    b = InlineKeyboardBuilder()
    for row in rows:
        buttons = []
        for text, data in row:
            if data.startswith("http"):
                buttons.append(InlineKeyboardButton(text=text, url=data))
            else:
                buttons.append(InlineKeyboardButton(text=text, callback_data=data))
        b.row(*buttons)
    return b.as_markup()


async def show(target, text, markup=None):
    """Показать экран: редактируем сообщение для callback, отправляем новое для message."""
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                await target.message.answer(text, reply_markup=markup)
        try:
            await target.answer()
        except TelegramBadRequest:  # на callback уже отвечали (например, alert)
            pass
    else:
        await target.answer(text, reply_markup=markup)


async def safe_send(bot, chat_id, text, **kw):
    try:
        await bot.send_message(chat_id, text, **kw)
        return True
    except (TelegramForbiddenError, TelegramBadRequest) as e:
        log.info("не удалось отправить %s: %s", chat_id, e)
        return False


async def notify_admins(bot, text, markup=None):
    for admin_id in C.ADMIN_IDS:
        await safe_send(bot, admin_id, text, reply_markup=markup)


def ensure_user(tg_user):
    user, _ = S.upsert_user(tg_user.id, tg_user.full_name)
    return user


def booking_text(b):
    s = C.SERVICES[b["service"]]
    br = C.BARBERS[b["barber"]]
    lines = [
        f"<b>{s['emoji']} {s['name']}</b>",
        f"💈 {br['name']}",
        f"🕒 {fmt_dt(S.parse(b['start_at']))} ({s['dur']} мин)",
        f"💵 К оплате в салоне: {money(b['price'])}",
    ]
    return "\n".join(lines)


def make_ics(b):
    f = lambda d: d.strftime("%Y%m%dT%H%M%S")  # noqa: E731
    s, e = S.parse(b["start_at"]), S.parse(b["end_at"])
    name = C.SERVICES[b["service"]]["name"]
    rows = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//BlackOak Demo//RU",
        "BEGIN:VEVENT",
        f"UID:booking-{b['id']}@blackoak-demo",
        f"DTSTAMP:{f(S.now_local())}",
        f"DTSTART:{f(s)}",
        f"DTEND:{f(e)}",
        f"SUMMARY:{C.SHOP['name']}: {name}",
        f"LOCATION:{C.SHOP['address']}",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(rows).encode("utf-8")


def stars_kb(booking_id):
    return ikb([[(f"{i}⭐", f"rv:{booking_id}:{i}") for i in range(1, 6)]])


router = Router()  # клиентская часть
admin = Router()  # админ-панель
admin.message.filter(F.from_user.id.in_(C.ADMIN_IDS))
admin.callback_query.filter(F.from_user.id.in_(C.ADMIN_IDS))


# ================================== /start ====================================
WELCOME = (
    "👋 Привет, {name}!\n\n"
    "Это <b>{shop} · {tag}</b>. Здесь можно:\n"
    "• записаться онлайн за 30 секунд\n"
    "• выбрать барбера и посмотреть рейтинги\n"
    "• узнать цены, акции и адрес\n"
    "• получать напоминания о записи\n\n"
    "Выберите раздел в меню 👇"
)


@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    S.upsert_user(m.from_user.id, m.from_user.full_name)
    await m.answer(
        WELCOME.format(name=esc(m.from_user.first_name), shop=C.SHOP["name"], tag=C.SHOP["tagline"]),
        reply_markup=MENU,
    )


@router.message(Command("id"))
async def cmd_id(m: Message):
    await m.answer(f"Ваш Telegram ID: <code>{m.from_user.id}</code>")


@router.message(Command("cancel"))
@router.message(F.text == B_CANCEL)
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Действие отменено.", reply_markup=MENU)


@router.message(Command("menu"))
async def cmd_menu(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Главное меню 👇", reply_markup=MENU)


# ================================ Запись ======================================
async def ask_service(target):
    rows = [
        [(f"{s['emoji']} {s['name']} · {price_text(k)}", f"bk:s:{k}")]
        for k, s in C.SERVICES.items()
    ]
    rows.append([("✖️ Отмена", "bk:x")])
    await show(target, "<b>Шаг 1 из 4. Выберите услугу</b>", ikb(rows))


async def ask_barber(target, state):
    data = await state.get_data()
    svc = data["service"]
    s = C.SERVICES[svc]
    rows = [[("🎲 Любой свободный барбер", "bk:b:any")]]
    for k, br in C.BARBERS.items():
        avg, n = S.barber_rating(k)
        rate = f"★{avg}" if n else "нет оценок"
        rows.append([(f"{br['name']} · {rate} · {money(barber_price(svc, k))}", f"bk:b:{k}")])
    rows.append([("◀️ Другая услуга", "bk:back:svc"), ("✖️ Отмена", "bk:x")])
    await show(
        target,
        f"<b>Шаг 2 из 4. Выберите барбера</b>\n{s['emoji']} {s['name']}, {s['dur']} мин\n\n"
        "Цена зависит от мастера. «Любой свободный» подберёт барбера под выбранное время.",
        ikb(rows),
    )


def available_days(barber, duration):
    today = S.now_local().date()
    result = []
    for i in range(C.BOOK_DAYS):
        d = today + timedelta(days=i)
        slots = S.free_slots_any(d, duration) if barber == "any" else S.free_slots(barber, d, duration)
        if slots:
            result.append(d)
    return result


async def ask_date(target, state):
    data = await state.get_data()
    svc, barber = data["service"], data["barber"]
    days = available_days(barber, C.SERVICES[svc]["dur"])
    back = [("◀️ Назад", "bk:back:barber"), ("✖️ Отмена", "bk:x")]
    if not days:
        await show(target, "😔 В ближайшие дни нет свободных окон. Попробуйте другого барбера.", ikb([back]))
        return
    today = S.now_local().date()
    buttons = []
    for d in days:
        label = "Сегодня" if d == today else "Завтра" if d == today + timedelta(days=1) else fmt_day(d)
        buttons.append((label, f"bk:d:{d.isoformat()}"))
    rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    rows.append(back)
    who = "любой барбер" if barber == "any" else C.BARBERS[barber]["name"]
    await show(target, f"<b>Шаг 3 из 4. Выберите день</b>\nБарбер: {who}", ikb(rows))


async def ask_time(target, state):
    data = await state.get_data()
    svc, barber = data["service"], data["barber"]
    d = date.fromisoformat(data["date"])
    dur = C.SERVICES[svc]["dur"]
    slots = list(S.free_slots_any(d, dur)) if barber == "any" else S.free_slots(barber, d, dur)
    if not slots:
        await show(target, "На этот день свободного времени не осталось.", ikb([[("◀️ Другой день", "bk:back:date")]]))
        return
    buttons = []
    for t in slots:
        h, mi = int(t[:2]), int(t[3:])
        disc = S.calc_price(svc, "ildar", datetime(d.year, d.month, d.day, h, mi))[1]
        buttons.append((("🌅" if disc else "") + t, f"bk:t:{t[:2]}{t[3:]}"))
    rows = [buttons[i : i + 4] for i in range(0, len(buttons), 4)]
    rows.append([("◀️ Другой день", "bk:back:date"), ("✖️ Отмена", "bk:x")])
    note = f"\n🌅 — утренняя скидка {C.MORNING_DISCOUNT}%" if any(b[0].startswith("🌅") for b in buttons) else ""
    await show(target, f"<b>Шаг 4 из 4. Выберите время</b>\n{fmt_day(d)}{note}", ikb(rows))


async def ask_confirm(target, state):
    data = await state.get_data()
    svc, bf = data["service"], data["barber_final"]
    start = datetime.strptime(f"{data['date']} {data['time']}", S.FMT)
    base, disc, final = S.calc_price(svc, bf, start)
    s, br = C.SERVICES[svc], C.BARBERS[bf]
    lines = [
        "<b>Проверьте запись</b>\n",
        f"{s['emoji']} {s['name']} ({s['dur']} мин)",
        f"💈 {br['name']}",
        f"🕒 {fmt_dt(start)}",
        f"📍 {C.SHOP['address']}",
        "",
        f"Стоимость: {money(base)}",
    ]
    if disc:
        lines.append(f"🌅 Утренняя скидка {disc}%: −{money(base - final)}")
    lines.append(f"<b>К оплате в салоне: {money(final)}</b>")
    lines.append(f"\nБесплатная отмена не позднее чем за {C.CANCEL_LIMIT_H} ч до визита.")
    await show(
        target,
        "\n".join(lines),
        ikb([[("✅ Подтвердить запись", "bk:ok")], [("◀️ Изменить время", "bk:back:date"), ("✖️ Отмена", "bk:x")]]),
    )


async def restart_flow(target, state):
    await state.clear()
    await ask_service(target)


@router.message(F.text == B_BOOK)
async def book_start(m: Message, state: FSMContext):
    await state.clear()
    ensure_user(m.from_user)
    await ask_service(m)


@router.callback_query(F.data == "bk:new")
async def bk_new(cb: CallbackQuery, state: FSMContext):
    ensure_user(cb.from_user)
    await restart_flow(cb, state)


@router.callback_query(F.data == "bk:x")
async def bk_abort(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, "Запись отменена. Возвращайтесь, когда будете готовы 👌")


@router.callback_query(F.data.startswith("bk:pb:"))
async def bk_pre_barber(cb: CallbackQuery, state: FSMContext):
    """Запись «к этому барберу» из его карточки."""
    key = cb.data.split(":")[2]
    if key not in C.BARBERS:
        return await cb.answer()
    ensure_user(cb.from_user)
    await state.clear()
    await state.update_data(barber_pre=key)
    await ask_service(cb)


@router.callback_query(F.data.startswith("bk:s:"))
async def bk_service(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[2]
    if key not in C.SERVICES:
        return await cb.answer()
    ensure_user(cb.from_user)
    await state.update_data(service=key)
    pre = (await state.get_data()).get("barber_pre")
    if pre:
        await state.update_data(barber=pre)
        await ask_date(cb, state)
    else:
        await ask_barber(cb, state)


@router.callback_query(F.data.startswith("bk:b:"))
async def bk_barber(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if "service" not in data:
        return await restart_flow(cb, state)
    key = cb.data.split(":")[2]
    if key != "any" and key not in C.BARBERS:
        return await cb.answer()
    await state.update_data(barber=key)
    await ask_date(cb, state)


@router.callback_query(F.data.startswith("bk:d:"))
async def bk_date(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if "service" not in data or "barber" not in data:
        return await restart_flow(cb, state)
    await state.update_data(date=cb.data[5:])
    await ask_time(cb, state)


@router.callback_query(F.data.startswith("bk:back:"))
async def bk_back(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    step = cb.data.split(":")[2]
    if "service" not in data and step != "svc":
        return await restart_flow(cb, state)
    if step == "svc":
        await ask_service(cb)
    elif step == "barber":
        if data.get("barber_pre"):
            await ask_service(cb)
        else:
            await ask_barber(cb, state)
    else:
        await ask_date(cb, state)


@router.callback_query(F.data.startswith("bk:t:"))
async def bk_time(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not all(k in data for k in ("service", "barber", "date")):
        return await restart_flow(cb, state)
    raw = cb.data.split(":")[2]
    tm = f"{raw[:2]}:{raw[2:]}"
    d = date.fromisoformat(data["date"])
    dur = C.SERVICES[data["service"]]["dur"]
    if data["barber"] == "any":
        barber = S.resolve_barber(d, tm, dur)
    else:
        barber = data["barber"] if tm in S.free_slots(data["barber"], d, dur) else None
    if not barber:
        await cb.answer("Это время уже заняли, выберите другое", show_alert=True)
        return await ask_time(cb, state)
    await state.update_data(time=tm, barber_final=barber)
    user = S.get_user(cb.from_user.id)
    if not user or not user.get("phone"):
        await state.set_state(Book.phone)
        await cb.message.answer(
            "📱 Последний шаг: оставьте номер телефона, чтобы администратор мог связаться при необходимости.\n"
            "Нажмите кнопку или напишите номер сообщением.",
            reply_markup=PHONE_KB,
        )
        return await cb.answer()
    await ask_confirm(cb, state)


def normalize_phone(raw):
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    if len(digits) == 10:
        digits = "7" + digits
    if not 11 <= len(digits) <= 15:
        return None
    return "+" + digits


@router.message(Book.phone)
async def bk_phone(m: Message, state: FSMContext):
    raw = m.contact.phone_number if m.contact else m.text
    phone = normalize_phone(raw)
    if not phone:
        return await m.answer("Не похоже на номер телефона. Пример: +7 914 000-00-00", reply_markup=PHONE_KB)
    ensure_user(m.from_user)
    S.set_contact(m.from_user.id, m.from_user.full_name, phone)
    await state.set_state(None)
    await m.answer("Спасибо! Номер сохранён ✅", reply_markup=MENU)
    data = await state.get_data()
    if "barber_final" not in data:
        return await restart_flow(m, state)
    await ask_confirm(m, state)


@router.callback_query(F.data == "bk:ok")
async def bk_confirm(cb: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    user = S.get_user(cb.from_user.id)
    if not user or not user.get("phone") or not all(k in data for k in ("service", "barber_final", "date", "time")):
        return await restart_flow(cb, state)
    b = S.create_booking(
        cb.from_user.id,
        data["service"],
        data["barber_final"],
        f"{data['date']} {data['time']}",
        user["name"] or cb.from_user.full_name,
        user["phone"],
    )
    if not b:
        await cb.answer("Это время только что заняли 😔 Выберите другое", show_alert=True)
        return await ask_time(cb, state)
    await state.clear()
    await show(
        cb,
        "✅ <b>Вы записаны!</b>\n\n"
        + booking_text(b)
        + f"\n📍 {C.SHOP['address']}\n\n"
        "Напомним о визите за сутки и за 2 часа. Отменить запись можно в разделе «Мои записи».",
        ikb([[("📆 Добавить в календарь", f"ics:{b['id']}")]]),
    )
    await notify_admins(
        bot,
        f"🆕 <b>Новая запись #{b['id']}</b>\n{booking_text(b)}\n👤 {esc(b['name'])} {esc(b['phone'])}",
    )


@router.callback_query(F.data.startswith("ics:"))
async def send_ics(cb: CallbackQuery):
    b = S.get_booking(int(cb.data.split(":")[1]))
    if not b or b["user_id"] != cb.from_user.id:
        return await cb.answer("Запись не найдена", show_alert=True)
    await cb.message.answer_document(
        BufferedInputFile(make_ics(b), filename="barbershop-visit.ics"),
        caption="Откройте файл, и событие добавится в календарь телефона.",
    )
    await cb.answer()


# ============================== Мои записи ====================================
@router.message(F.text == B_MY)
async def my_bookings(m: Message, state: FSMContext):
    await state.clear()
    ensure_user(m.from_user)
    await render_my(m, m.from_user.id)


async def render_my(target, uid):
    rows = S.user_bookings(uid, upcoming=True)
    if not rows:
        return await show(
            target,
            "У вас нет предстоящих записей.",
            ikb([[("📅 Записаться", "bk:new")], [("🕘 История визитов", "my:hist")]]),
        )
    text = ["<b>📋 Предстоящие записи</b>\n"]
    kb = []
    for b in rows:
        text.append(f"<b>№{b['id']}</b>\n{booking_text(b)}\n")
        kb.append([(f"❌ Отменить №{b['id']}", f"cx:{b['id']}"), ("📆", f"ics:{b['id']}")])
    kb.append([("🕘 История визитов", "my:hist")])
    await show(target, "\n".join(text), ikb(kb))


@router.callback_query(F.data == "my:hist")
async def my_history(cb: CallbackQuery):
    rows = S.user_bookings(cb.from_user.id, upcoming=False, limit=8)
    names = {"done": "✅", "cancelled": "🚫 отменена", "noshow": "⚠️ не пришли"}
    if not rows:
        text = "История пока пуста."
    else:
        text = "<b>🕘 История визитов</b>\n\n" + "\n".join(
            f"{names[b['status']]} · {fmt_dt(S.parse(b['start_at']))} · "
            f"{C.SERVICES[b['service']]['name']} · {C.BARBERS[b['barber']]['nick']}"
            for b in rows
        )
    await show(cb, text, ikb([[("◀️ К записям", "my:up")]]))


@router.callback_query(F.data == "my:up")
async def my_up(cb: CallbackQuery):
    await render_my(cb, cb.from_user.id)


@router.callback_query(F.data.startswith("cx:"))
async def cancel_flow(cb: CallbackQuery, bot: Bot):
    parts = cb.data.split(":")
    if parts[1] == "n":
        return await render_my(cb, cb.from_user.id)
    if parts[1] == "y":
        bid = int(parts[2])
        ok, why = S.cancel_booking(bid, uid=cb.from_user.id)
        if ok:
            b = S.get_booking(bid)
            await show(cb, "✅ Запись отменена.", ikb([[("📅 Записаться снова", "bk:new")]]))
            await notify_admins(bot, f"❌ <b>Клиент отменил запись #{bid}</b>\n{booking_text(b)}\n👤 {esc(b['name'])} {esc(b['phone'])}")
        elif why == "too_late":
            await show(
                cb,
                f"До визита меньше {C.CANCEL_LIMIT_H} ч, отменить онлайн нельзя. "
                f"Позвоните, пожалуйста: {C.SHOP['phone']}",
                ikb([[("◀️ К записям", "my:up")]]),
            )
        else:
            await show(cb, "Эта запись уже неактивна.", ikb([[("◀️ К записям", "my:up")]]))
        return
    bid = int(parts[1])
    b = S.get_booking(bid)
    if not b or b["user_id"] != cb.from_user.id or b["status"] != "active":
        return await cb.answer("Запись не найдена", show_alert=True)
    await show(
        cb,
        f"Отменить запись?\n\n{booking_text(b)}",
        ikb([[("Да, отменить", f"cx:y:{bid}"), ("Нет, оставить", "cx:n")]]),
    )


# ================================ Барберы =====================================
@router.message(F.text == B_BARBERS)
async def barbers_list(m: Message, state: FSMContext):
    await state.clear()
    await render_barbers(m)


async def render_barbers(target):
    rows = [
        [(f"{br['name']} · {br['role']} · {rating_line(k)}", f"brb:{k}")] for k, br in C.BARBERS.items()
    ]
    await show(target, "<b>💈 Наши барберы</b>\nВыберите мастера, чтобы посмотреть подробности:", ikb(rows))


@router.callback_query(F.data == "brb:all")
async def barbers_back(cb: CallbackQuery):
    await render_barbers(cb)


@router.callback_query(F.data.startswith("brb:"))
async def barber_card(cb: CallbackQuery):
    key = cb.data.split(":")[1]
    br = C.BARBERS.get(key)
    if not br:
        return await cb.answer()
    text = (
        f"<b>{br['name']}</b> · {br['role']}\n"
        f"🏅 {br['exp']}\n"
        f"✂️ {br['spec']}\n"
        f"📆 Выходной: {days_off_text(key)}\n"
        f"⭐ {rating_line(key)}\n\n"
        f"{br['bio']}\n\n"
        f"<b>Цены у мастера</b>\n"
        f"• Мужская стрижка: {money(barber_price('cut', key))}\n"
        f"• Стрижка + борода: {money(barber_price('cutbeard', key))}\n"
        f"• Бритьё: {money(barber_price('shave', key))}"
    )
    await show(
        cb,
        text,
        ikb(
            [
                [(f"📅 Записаться к {br['nick']}", f"bk:pb:{key}")],
                [("⭐ Отзывы о мастере", f"rvl:{key}")],
                [("◀️ Все барберы", "brb:all")],
            ]
        ),
    )


# ============================ Услуги и цены ===================================
@router.message(F.text == B_SERVICES)
async def services_list(m: Message, state: FSMContext):
    await state.clear()
    await render_services(m)


async def render_services(target):
    lines = ["<b>💰 Услуги и цены</b>\n"]
    for k, s in C.SERVICES.items():
        lines.append(f"{s['emoji']} <b>{s['name']}</b> · {s['dur']} мин · {price_text(k)}")
    lines.append(
        f"\n🌅 Пн-Пт до {C.MORNING_BEFORE_HOUR}:00 скидка {C.MORNING_DISCOUNT}%.\n"
        "Цена зависит от барбера. Нажмите услугу, чтобы увидеть подробности."
    )
    rows = [[(f"{s['emoji']} {s['name']}", f"svc:{k}")] for k, s in C.SERVICES.items()]
    await show(target, "\n".join(lines), ikb(rows))


@router.callback_query(F.data == "svc:all")
async def services_back(cb: CallbackQuery):
    await render_services(cb)


@router.callback_query(F.data.startswith("svc:"))
async def service_card(cb: CallbackQuery):
    key = cb.data.split(":")[1]
    s = C.SERVICES.get(key)
    if not s:
        return await cb.answer()
    per_barber = "\n".join(
        f"• {br['name']}: {money(barber_price(key, bk))}" for bk, br in C.BARBERS.items()
    )
    await show(
        cb,
        f"{s['emoji']} <b>{s['name']}</b>\n⏱ {s['dur']} мин\n\n{s['desc']}\n\n<b>Цена</b>\n{per_barber}",
        ikb([[("📅 Записаться на эту услугу", f"bk:s:{key}")], [("◀️ Все услуги", "svc:all")]]),
    )


# ================================== Отзывы ====================================
@router.message(F.text == B_REVIEWS)
async def reviews_main(m: Message, state: FSMContext):
    await state.clear()
    ensure_user(m.from_user)
    await render_reviews_main(m)


async def render_reviews_main(target):
    lines = [f"<b>⭐ Отзывы гостей</b>\nОбщий рейтинг: {rating_line()}\n"]
    for k, br in C.BARBERS.items():
        lines.append(f"• {br['name']}: {rating_line(k)}")
    lines.append("\n<b>Последние отзывы</b>")
    for r in S.last_reviews(limit=3):
        lines.append(review_text(r))
    rows = [[(f"Отзывы: {br['nick']}", f"rvl:{k}")] for k, br in C.BARBERS.items()]
    rows.append([("✍️ Оставить отзыв", "rvw")])
    await show(target, "\n".join(lines), ikb(rows))


def review_text(r):
    who = C.BARBERS[r["barber"]]["nick"]
    body = f"\n«{esc(r['text'])}»" if r["text"] else ""
    return f"\n{stars(r['rating'])} · {esc(r['author'])} → {who}{body}"


@router.callback_query(F.data.startswith("rvl:"))
async def reviews_by_barber(cb: CallbackQuery):
    key = cb.data.split(":")[1]
    if key not in C.BARBERS:
        return await cb.answer()
    br = C.BARBERS[key]
    rows = S.last_reviews(key, limit=6)
    text = f"<b>⭐ Отзывы: {br['name']}</b>\n{rating_line(key)}\n" + (
        "".join(review_text(r) for r in rows) if rows else "\nПока нет отзывов."
    )
    await show(cb, text, ikb([[(f"📅 Записаться к {br['nick']}", f"bk:pb:{key}")], [("◀️ Все отзывы", "rvall")]]))


@router.callback_query(F.data == "rvall")
async def reviews_back(cb: CallbackQuery):
    await render_reviews_main(cb)


@router.callback_query(F.data == "rvw")
async def review_pick(cb: CallbackQuery):
    rows = S.reviewable(cb.from_user.id)
    if not rows:
        return await cb.answer(
            "Оставить отзыв можно после визита. Как только мастер отметит ваш визит, придёт запрос на оценку.",
            show_alert=True,
        )
    kb = [
        [(f"{fmt_day(S.parse(b['start_at']))} · {C.BARBERS[b['barber']]['nick']}", f"rvb:{b['id']}")]
        for b in rows
    ]
    await show(cb, "Выберите визит, который хотите оценить:", ikb(kb))


@router.callback_query(F.data.startswith("rvb:"))
async def review_stars(cb: CallbackQuery):
    bid = int(cb.data.split(":")[1])
    await show(cb, "Как всё прошло? Поставьте оценку:", stars_kb(bid))


@router.callback_query(F.data.startswith("rv:"))
async def review_rate(cb: CallbackQuery, state: FSMContext):
    _, bid, n = cb.data.split(":")
    b = S.get_booking(int(bid))
    if not b or b["user_id"] != cb.from_user.id or b["status"] != "done" or S.has_review(b["id"]):
        return await cb.answer("Отзыв на этот визит уже оставлен", show_alert=True)
    await state.set_state(Review.comment)
    await state.update_data(rv_bid=b["id"], rv_rating=int(n))
    await show(
        cb,
        f"Оценка: {stars(int(n))}\nНапишите пару слов о визите или нажмите «Пропустить».",
        ikb([[("Пропустить", "rvskip")]]),
    )


async def save_review(target_msg, user, state, bot, text):
    data = await state.get_data()
    await state.clear()
    b = S.get_booking(data.get("rv_bid", 0))
    if not b:
        return
    ok = S.add_review(b["id"], user.id, b["barber"], data["rv_rating"], text, user.first_name)
    if not ok:
        return await target_msg.answer("Отзыв уже сохранён 👌", reply_markup=MENU)
    await target_msg.answer("Спасибо за отзыв! 🙏", reply_markup=MENU)
    if data["rv_rating"] <= 3:
        await notify_admins(
            bot,
            f"⚠️ <b>Низкая оценка {data['rv_rating']}/5</b> · {C.BARBERS[b['barber']]['name']}\n"
            f"👤 {esc(b['name'])} {esc(b['phone'])}\n{esc(text) or 'без комментария'}",
        )


@router.callback_query(F.data == "rvskip")
async def review_skip(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await cb.answer()
    await save_review(cb.message, cb.from_user, state, bot, "")


@router.message(Review.comment, F.text)
async def review_comment(m: Message, state: FSMContext, bot: Bot):
    await save_review(m, m.from_user, state, bot, m.text[:500])


# ================================ Акции, FAQ ==================================
@router.message(F.text == B_PROMO)
async def promos(m: Message, state: FSMContext):
    await state.clear()
    text = "<b>🎁 Акции и скидки</b>\n\n" + "\n\n".join(f"<b>{t}</b>\n{d}" for t, d in C.PROMOS)
    await m.answer(text, reply_markup=ikb([[("📅 Записаться", "bk:new")]]))


@router.message(F.text == B_CONTACTS)
async def contacts(m: Message, state: FSMContext):
    await state.clear()
    sh = C.SHOP
    await m.answer_location(sh["lat"], sh["lon"])
    await m.answer(
        f"<b>📍 {sh['name']} · {sh['tagline']}</b>\n\n"
        f"Адрес: {sh['address']}\n"
        f"Как найти: {sh['landmark']}\n"
        f"🕒 {sh['hours']}\n"
        f"🚗 {sh['parking']}\n"
        f"📞 {sh['phone']}",
        reply_markup=ikb(
            [
                [("🗺 Яндекс Карты", f"https://yandex.ru/maps/?pt={sh['lon']},{sh['lat']}&z=17&l=map")],
                [("🗺 2ГИС", f"https://2gis.ru/geo/{sh['lon']},{sh['lat']}")],
            ]
        ),
    )


@router.message(F.text == B_FAQ)
async def faq(m: Message, state: FSMContext):
    await state.clear()
    await render_faq(m)


async def render_faq(target):
    rows = [[(q, f"faq:{i}")] for i, (q, _) in enumerate(C.FAQ)]
    await show(target, "<b>❓ Частые вопросы</b>\nВыберите вопрос:", ikb(rows))


@router.callback_query(F.data == "faq:all")
async def faq_back(cb: CallbackQuery):
    await render_faq(cb)


@router.callback_query(F.data.startswith("faq:"))
async def faq_answer(cb: CallbackQuery):
    i = int(cb.data.split(":")[1])
    if not 0 <= i < len(C.FAQ):
        return await cb.answer()
    q, a = C.FAQ[i]
    await show(
        cb,
        f"<b>{q}</b>\n\n{a}",
        ikb([[("◀️ Все вопросы", "faq:all")], [("✉️ Не нашли ответ? Напишите нам", "support")]]),
    )


# ======================== Связь с администратором =============================
@router.message(F.text == B_WRITE)
async def support_start(m: Message, state: FSMContext):
    await state.clear()
    await state.set_state(Support.text)
    await m.answer(
        "Напишите сообщение, администратор ответит прямо здесь.\n/cancel, чтобы отменить.",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.callback_query(F.data == "support")
async def support_from_faq(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Support.text)
    await cb.message.answer("Напишите сообщение, администратор ответит прямо здесь.\n/cancel, чтобы отменить.",
                            reply_markup=ReplyKeyboardRemove())
    await cb.answer()


@router.message(Support.text, F.text)
async def support_send(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    if not C.ADMIN_IDS:
        return await m.answer(
            "В демо-режиме администратор не подключён. В рабочей версии сообщение уходит администратору.",
            reply_markup=MENU,
        )
    user = S.get_user(m.from_user.id) or {}
    await notify_admins(
        bot,
        f"✉️ <b>Сообщение от клиента</b>\n👤 {esc(m.from_user.full_name)} {esc(user.get('phone'))}\n\n{esc(m.text)}",
        ikb([[("↩️ Ответить", f"adm:reply:{m.from_user.id}")]]),
    )
    await m.answer("Отправлено ✅ Ответим в ближайшее время.", reply_markup=MENU)


# ================================ Админ-панель ================================
def admin_panel_kb():
    return ikb(
        [
            [("📅 Сегодня", "adm:day:0"), ("📅 Завтра", "adm:day:1")],
            [("📊 Статистика за 30 дней", "adm:stats")],
            [("📣 Рассылка клиентам", "adm:bc")],
            [("🧪 Заполнить демо-данными", "adm:demo")],
        ]
    )


@router.message(Command("admin"))
async def admin_denied(m: Message):
    """Сюда попадают только не-админы: админский роутер перехватывает /admin раньше."""
    await m.answer(
        "Админ-панель доступна только администратору. Ваш ID: "
        f"<code>{m.from_user.id}</code>. Добавьте его в ADMIN_IDS в файле .env."
    )


@admin.message(Command("admin"))
async def admin_home(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("<b>🛠 Админ-панель «Чёрный дуб»</b>", reply_markup=admin_panel_kb())


@admin.callback_query(F.data == "adm:home")
async def admin_home_cb(cb: CallbackQuery):
    await show(cb, "<b>🛠 Админ-панель «Чёрный дуб»</b>", admin_panel_kb())


async def render_day(target, off):
    d = S.now_local().date() + timedelta(days=off)
    rows = S.bookings_on(d)
    icon = {"active": "🟡", "done": "✅", "noshow": "🚫"}
    lines = [f"<b>📅 {fmt_day(d)}</b> · записей: {len(rows)}"]
    kb = []
    for b in rows:
        hhmm = b["start_at"][11:]
        lines.append(
            f"{icon[b['status']]} {hhmm} · {C.BARBERS[b['barber']]['nick']} · "
            f"{C.SERVICES[b['service']]['name']} · {esc(b['name'])} {esc(b['phone'])} · {money(b['price'])}"
        )
        if b["status"] == "active":
            kb.append(
                [
                    (f"✅ {hhmm}", f"adm:ok:{b['id']}:{off}"),
                    (f"🚫 {hhmm}", f"adm:ns:{b['id']}:{off}"),
                    (f"❌ {hhmm}", f"adm:cx:{b['id']}:{off}"),
                ]
            )
    if not rows:
        lines.append("\nЗаписей нет.")
    else:
        lines.append("\n✅ визит состоялся · 🚫 не пришёл · ❌ отменить запись")
    kb.append([("◀️", f"adm:day:{off - 1}"), ("▶️", f"adm:day:{off + 1}")])
    kb.append([("🛠 Панель", "adm:home")])
    await show(target, "\n".join(lines), ikb(kb))


@admin.callback_query(F.data.startswith("adm:day:"))
async def admin_day(cb: CallbackQuery):
    await render_day(cb, int(cb.data.split(":")[2]))


@admin.callback_query(F.data.regexp(r"^adm:(ok|ns|cx):"))
async def admin_booking_action(cb: CallbackQuery, bot: Bot):
    _, action, bid, off = cb.data.split(":")
    bid, off = int(bid), int(off)
    b = S.get_booking(bid)
    if not b or b["status"] != "active":
        await cb.answer("Запись уже обработана", show_alert=True)
        return await render_day(cb, off)
    if action == "ok":
        S.mark_done(bid)
        await safe_send(
            bot,
            b["user_id"],
            "Спасибо, что были у нас! 🙌\n\nОцените визит, это займёт 5 секунд:",
            reply_markup=stars_kb(bid),
        )
    elif action == "ns":
        S.mark_noshow(bid)
    else:
        S.cancel_booking(bid, force=True)
        await safe_send(
            bot,
            b["user_id"],
            f"К сожалению, администратор отменил вашу запись:\n\n{booking_text(b)}\n\n"
            f"Приносим извинения. Запишитесь на другое время или позвоните: {C.SHOP['phone']}",
        )
    await cb.answer("Готово")
    await render_day(cb, off)


@admin.callback_query(F.data == "adm:stats")
async def admin_stats(cb: CallbackQuery):
    st = S.stats(30)
    avg, n = st["rating"]
    top = "\n".join(f"  {i}. {C.SERVICES[t['service']]['name']}: {t['n']}" for i, t in enumerate(st["top_services"], 1))
    await show(
        cb,
        f"<b>📊 Статистика за {st['days']} дней</b>\n\n"
        f"Визитов: {st['done']}\n"
        f"Выручка: {money(st['revenue'])}\n"
        f"Средний чек: {money(st['avg_check'])}\n"
        f"Отмены: {st['cancelled']} · Неявки: {st['noshow']}\n"
        f"Предстоящих записей: {st['upcoming']}\n"
        f"Новых клиентов: {st['new_users']}\n"
        f"Рейтинг: {avg} ({n} отзывов)\n\n"
        f"<b>Топ услуг</b>\n{top or '  пока нет данных'}",
        ikb([[("🛠 Панель", "adm:home")]]),
    )


@admin.callback_query(F.data == "adm:demo")
async def admin_demo(cb: CallbackQuery):
    created = seed_demo()
    await cb.answer(f"Создано записей: {created}", show_alert=True)


def seed_demo():
    """Заполнить базу вымышленными клиентами, чтобы показать админку не пустой."""
    guests = [
        ("Дмитрий", "+79140000001"),
        ("Андрей", "+79140000002"),
        ("Сергей", "+79140000003"),
        ("Игорь", "+79140000004"),
        ("Максим", "+79140000005"),
        ("Олег", "+79140000006"),
    ]
    plan = [
        (1, "cut", "artem", "12:00"),
        (1, "cutbeard", "mark", "14:00"),
        (1, "kid", "ildar", "16:00"),
        (2, "shave", "mark", "11:00"),
        (2, "royal", "artem", "15:00"),
        (2, "cut", "ildar", "17:30"),
    ]
    today = S.now_local().date()
    created = 0
    for i, ((name, phone), (off, svc, barber, tm)) in enumerate(zip(guests, plan), 1):
        uid = -i
        S.upsert_user(uid, name)
        S.set_contact(uid, name, phone)
        start = f"{(today + timedelta(days=off)).isoformat()} {tm}"
        b = S.create_booking(uid, svc, barber, start, name, phone)
        if b:
            created += 1
            if i <= 2:
                S.mark_done(b["id"])  # чтобы в статистике были цифры
    return created


@admin.callback_query(F.data == "adm:bc")
async def admin_broadcast(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Broadcast.text)
    await cb.message.answer("Отправьте текст рассылки одним сообщением. /cancel, чтобы отменить.")
    await cb.answer()


@admin.message(Broadcast.text, F.text, ~F.text.in_(MENU_TEXTS))
async def admin_broadcast_preview(m: Message, state: FSMContext):
    await state.update_data(text=m.html_text)
    count = len([u for u in S.all_user_ids() if u > 0])
    await m.answer(
        f"<b>Предпросмотр рассылки</b> ({count} получателей):\n\n{m.html_text}",
        reply_markup=ikb([[("🚀 Отправить", "adm:bcgo"), ("✖️ Отмена", "adm:bcno")]]),
    )


@admin.callback_query(F.data == "adm:bcno")
async def admin_broadcast_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, "Рассылка отменена.")


@admin.callback_query(F.data == "adm:bcgo")
async def admin_broadcast_go(cb: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    text = data.get("text")
    if not text:
        return await cb.answer("Нет текста рассылки", show_alert=True)
    await cb.answer("Отправляю…")
    sent = failed = 0
    for uid in S.all_user_ids():
        if uid <= 0:
            continue
        if await safe_send(bot, uid, text):
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(0.05)  # лимиты Telegram
    await cb.message.answer(f"📣 Рассылка завершена. Доставлено: {sent}, не доставлено: {failed}.")


@admin.callback_query(F.data.startswith("adm:reply:"))
async def admin_reply_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminReply.text)
    await state.update_data(to=int(cb.data.split(":")[2]))
    await cb.message.answer("Напишите ответ клиенту. /cancel, чтобы отменить.")
    await cb.answer()


@admin.message(AdminReply.text, F.text, ~F.text.in_(MENU_TEXTS))
async def admin_reply_send(m: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    ok = await safe_send(bot, data["to"], f"💬 <b>Ответ администратора:</b>\n\n{esc(m.text)}")
    await m.answer("Отправлено ✅" if ok else "Не удалось доставить сообщение.")


# =============================== Напоминания ==================================
async def reminder_loop(bot: Bot):
    while True:
        try:
            for b, kind in S.due_reminders():
                head = "⏰ Напоминаем: ваша запись в ближайшие сутки" if kind == "24h" else "⏰ Ваша запись совсем скоро"
                await safe_send(
                    bot,
                    b["user_id"],
                    f"{head}\n\n{booking_text(b)}\n📍 {C.SHOP['address']}",
                    reply_markup=ikb([[("❌ Отменить запись", f"cx:{b['id']}")]]),
                )
                S.mark_reminded(b["id"], kind)
        except Exception:  # noqa: BLE001  цикл не должен падать из-за одной ошибки
            log.exception("ошибка в цикле напоминаний")
        await asyncio.sleep(60)


# ============================== Запасной обработчик ===========================
@router.message(F.text)
async def fallback(m: Message, state: FSMContext):
    if await state.get_state():
        return
    await m.answer("Не совсем понял 🤔 Выберите раздел в меню ниже.", reply_markup=MENU)


# ================================== Запуск ====================================
async def main():
    global BOT_USERNAME
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not C.BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN. Скопируйте .env.example в .env и впишите токен.")
    S.init_db()
    session = AiohttpSession(proxy=C.PROXY) if C.PROXY else None
    bot = Bot(C.BOT_TOKEN, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    BOT_USERNAME = (await bot.get_me()).username or ""
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(admin)  # админ раньше, чтобы /admin админа не ушёл в клиентский роутер
    dp.include_router(router)
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="menu", description="Показать меню"),
            BotCommand(command="cancel", description="Отменить действие"),
            BotCommand(command="id", description="Узнать свой Telegram ID"),
            BotCommand(command="admin", description="Админ-панель"),
        ]
    )
    reminders = asyncio.create_task(reminder_loop(bot))
    log.info("Бот @%s запущен", BOT_USERNAME)
    try:
        await dp.start_polling(bot)
    finally:
        reminders.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
