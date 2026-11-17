"""Демо-бот барбершопа «Чёрный дуб» для мессенджера MAX (библиотека maxapi).

Запуск: python max_bot.py   (см. README.md)
Логика записи, цен и отзывов общая с Telegram-версией: storage.py и config.py.

Особенности MAX, учтённые в коде:
- нет постоянной клавиатуры внизу, поэтому главное меню это inline-кнопки;
- состояние диалога ("ждём телефон", "ждём отзыв") хранится в данных контекста (ключ mode);
- все экраны перерисовываются редактированием сообщения, при ошибке отправляются заново.
"""
import asyncio
import html
import logging
import os
import re
import tempfile
from datetime import date, datetime, timedelta

from maxapi import Bot, Dispatcher
from maxapi.context import MemoryContext
from maxapi.types import (
    CallbackButton,
    InputMedia,
    LinkButton,
    MessageCallback,
    MessageCreated,
    RequestContactButton,
)
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

try:
    from maxapi.types import BotStarted
except ImportError:  # на случай другой структуры пакета в вашей версии maxapi
    from maxapi.types.updates.bot_started import BotStarted

import config as C
import storage as S

log = logging.getLogger("barbershop-max")
BOT = None  # создаётся в main()
TEXT_LIMIT = 3900  # лимит MAX на сообщение: 4000 символов

# Базовая дата для «каталожных» цен: будний день после 12:00, без скидок
LIST_DT = datetime(2030, 1, 7, 15, 0)


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
    return uid in C.MAX_ADMIN_IDS


def normalize_phone(raw):
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    if len(digits) == 10:
        digits = "7" + digits
    if not 11 <= len(digits) <= 15:
        return None
    return "+" + digits


def booking_text(b):
    s = C.SERVICES[b["service"]]
    br = C.BARBERS[b["barber"]]
    return "\n".join(
        [
            f"<b>{s['emoji']} {s['name']}</b>",
            f"💈 {br['name']}",
            f"🕒 {fmt_dt(S.parse(b['start_at']))} ({s['dur']} мин)",
            f"💵 К оплате в салоне: {money(b['price'])}",
        ]
    )


def make_ics(b):
    f = lambda d: d.strftime("%Y%m%dT%H%M%S")  # noqa: E731
    s, e = S.parse(b["start_at"]), S.parse(b["end_at"])
    rows = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//BlackOak Demo//RU",
        "BEGIN:VEVENT",
        f"UID:booking-{b['id']}@blackoak-demo",
        f"DTSTAMP:{f(S.now_local())}",
        f"DTSTART:{f(s)}",
        f"DTEND:{f(e)}",
        f"SUMMARY:{C.SHOP['name']}: {C.SERVICES[b['service']]['name']}",
        f"LOCATION:{C.SHOP['address']}",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(rows).encode("utf-8")


# ================================ Клавиатуры ==================================
def kb(rows):
    """rows: ряды [(текст, payload | http-ссылка | "!contact"), ...] -> вложение-клавиатура MAX."""
    b = InlineKeyboardBuilder()
    for row in rows:
        buttons = []
        for text, data in row:
            if data == "!contact":
                buttons.append(RequestContactButton(text=text))
            elif data.startswith("http"):
                buttons.append(LinkButton(text=text, url=data))
            else:
                buttons.append(CallbackButton(text=text, payload=data))
        b.row(*buttons)
    return b.as_markup()


HOME = [("🏠 Меню", "home")]


def menu_rows(uid):
    rows = [
        [("📅 Записаться", "bk:new")],
        [("💈 Барберы", "brb:all"), ("💰 Услуги и цены", "svc:all")],
        [("📋 Мои записи", "my:up"), ("⭐ Отзывы", "rvall")],
        [("🎁 Акции", "promo"), ("📍 Контакты", "contacts")],
        [("❓ Вопросы", "faq:all"), ("✉️ Написать нам", "support")],
    ]
    if is_admin(uid):
        rows.append([("🛠 Админ-панель", "adm:home")])
    return rows


def stars_rows(booking_id):
    return [[(f"{i}⭐", f"rv:{booking_id}:{i}") for i in range(1, 6)]]


# ============================== Отправка и экраны =============================
def _cut(text):
    return text if len(text) <= TEXT_LIMIT else text[: TEXT_LIMIT - 1] + "…"


async def render(ev, text, rows, new=False):
    """Показать экран: для нажатия кнопки редактируем сообщение, для текста отправляем новое."""
    text = _cut(text)
    markup = kb(rows)
    msg = getattr(ev, "message", None)
    if msg is None:  # исходное сообщение недоступно
        chat_id, _ = ev.get_ids()
        await BOT.send_message(chat_id=chat_id, text=text, attachments=[markup], format="html")
        return
    if isinstance(ev, MessageCallback) and not new:
        try:
            await msg.edit(text=text, attachments=[markup], format="html")
            return
        except Exception as e:  # noqa: BLE001  редактирование не удалось: пересоздаём экран
            log.info("edit не удался (%s), отправляю заново", e)
            try:
                await msg.delete()
            except Exception:  # noqa: BLE001
                pass
    await msg.answer(text, attachments=[markup], format="html")


async def send_safe(uid, text, rows=None):
    try:
        await BOT.send_message(
            user_id=uid,
            text=_cut(text),
            attachments=[kb(rows)] if rows else None,
            format="html",
        )
        return True
    except Exception as e:  # noqa: BLE001  пользователь мог заблокировать бота и т. п.
        log.info("не удалось отправить %s: %s", uid, e)
        return False


async def notify_admins(text, rows=None):
    for admin_id in C.MAX_ADMIN_IDS:
        await send_safe(admin_id, text, rows)


def sender_name(ev):
    user = getattr(ev, "callback", None)
    user = getattr(user, "user", None) if user else None
    if user is None:
        msg = getattr(ev, "message", None)
        user = getattr(msg, "sender", None) if msg else None
    return getattr(user, "full_name", None) or getattr(user, "first_name", None) or "Гость"


def ensure_user(ev):
    _, uid = ev.get_ids()
    S.upsert_user(uid, sender_name(ev))
    return uid


def message_text(ev):
    body = getattr(ev.message, "body", None)
    return (getattr(body, "text", None) or "").strip()


def contact_phone(ev):
    """Номер из вложения-контакта (кнопка «Отправить мой номер») или None."""
    body = getattr(ev.message, "body", None)
    for a in (getattr(body, "attachments", None) or []):
        kind = getattr(a, "type", None)
        kind = getattr(kind, "value", kind)
        if str(kind).lower() != "contact":
            continue
        payload = getattr(a, "payload", None)
        vcf = getattr(payload, "vcf_info", None) or ""
        m = re.search(r"TEL[^:\r\n]*:([^\r\n]+)", vcf)
        phone = normalize_phone(m.group(1)) if m else None
        if not phone:
            phone = normalize_phone(getattr(payload, "vcf_phone", None))
        if phone:
            return phone
    return None


# ================================== Главное ===================================
WELCOME = (
    "👋 Привет, {name}!\n\n"
    "Это <b>{shop} · {tag}</b>. Здесь можно:\n"
    "• записаться онлайн за 30 секунд\n"
    "• выбрать барбера и посмотреть рейтинги\n"
    "• узнать цены, акции и адрес\n"
    "• получать напоминания о записи\n\n"
    "Выберите раздел 👇"
)


async def screen_home(ev, ctx, new=False):
    await ctx.clear()
    uid = ensure_user(ev)
    name = esc(sender_name(ev).split()[0]) if sender_name(ev) else "гость"
    await render(
        ev,
        WELCOME.format(name=name, shop=C.SHOP["name"], tag=C.SHOP["tagline"]),
        menu_rows(uid),
        new=new,
    )


# ================================ Запись ======================================
async def ask_service(ev):
    rows = [
        [(f"{s['emoji']} {s['name']} · {price_text(k)}", f"bk:s:{k}")] for k, s in C.SERVICES.items()
    ]
    rows.append([("✖️ Отмена", "home")])
    await render(ev, "<b>Шаг 1 из 4. Выберите услугу</b>", rows)


async def ask_barber(ev, ctx):
    data = await ctx.get_data()
    svc = data["service"]
    s = C.SERVICES[svc]
    rows = [[("🎲 Любой свободный барбер", "bk:b:any")]]
    for k, br in C.BARBERS.items():
        avg, n = S.barber_rating(k)
        rate = f"★{avg}" if n else "нет оценок"
        rows.append([(f"{br['name']} · {rate} · {money(barber_price(svc, k))}", f"bk:b:{k}")])
    rows.append([("◀️ Другая услуга", "bk:back:svc"), ("✖️ Отмена", "home")])
    await render(
        ev,
        f"<b>Шаг 2 из 4. Выберите барбера</b>\n{s['emoji']} {s['name']}, {s['dur']} мин\n\n"
        "Цена зависит от мастера. «Любой свободный» подберёт барбера под выбранное время.",
        rows,
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


async def ask_date(ev, ctx):
    data = await ctx.get_data()
    svc, barber = data["service"], data["barber"]
    days = available_days(barber, C.SERVICES[svc]["dur"])
    back = [("◀️ Назад", "bk:back:barber"), ("✖️ Отмена", "home")]
    if not days:
        await render(ev, "😔 В ближайшие дни нет свободных окон. Попробуйте другого барбера.", [back])
        return
    today = S.now_local().date()
    buttons = []
    for d in days:
        label = "Сегодня" if d == today else "Завтра" if d == today + timedelta(days=1) else fmt_day(d)
        buttons.append((label, f"bk:d:{d.isoformat()}"))
    rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    rows.append(back)
    who = "любой барбер" if barber == "any" else C.BARBERS[barber]["name"]
    await render(ev, f"<b>Шаг 3 из 4. Выберите день</b>\nБарбер: {who}", rows)


async def ask_time(ev, ctx):
    data = await ctx.get_data()
    svc, barber = data["service"], data["barber"]
    d = date.fromisoformat(data["date"])
    dur = C.SERVICES[svc]["dur"]
    slots = list(S.free_slots_any(d, dur)) if barber == "any" else S.free_slots(barber, d, dur)
    if not slots:
        await render(ev, "На этот день свободного времени не осталось.", [[("◀️ Другой день", "bk:back:date")]])
        return
    buttons = []
    discounted = False
    for t in slots:
        h, mi = int(t[:2]), int(t[3:])
        disc = S.calc_price(svc, "ildar", datetime(d.year, d.month, d.day, h, mi))[1]
        discounted = discounted or bool(disc)
        buttons.append((("🌅" if disc else "") + t, f"bk:t:{t[:2]}{t[3:]}"))
    rows = [buttons[i : i + 4] for i in range(0, len(buttons), 4)]
    rows.append([("◀️ Другой день", "bk:back:date"), ("✖️ Отмена", "home")])
    note = f"\n🌅 — утренняя скидка {C.MORNING_DISCOUNT}%" if discounted else ""
    await render(ev, f"<b>Шаг 4 из 4. Выберите время</b>\n{fmt_day(d)}{note}", rows)


async def ask_confirm(ev, ctx, new=False):
    data = await ctx.get_data()
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
    await render(
        ev,
        "\n".join(lines),
        [[("✅ Подтвердить запись", "bk:ok")], [("◀️ Изменить время", "bk:back:date"), ("✖️ Отмена", "home")]],
        new=new,
    )


async def restart_flow(ev, ctx):
    await ctx.clear()
    await ask_service(ev)


async def bk_time(ev, ctx, raw):
    data = await ctx.get_data()
    if not all(k in data for k in ("service", "barber", "date")):
        return await restart_flow(ev, ctx)
    tm = f"{raw[:2]}:{raw[2:]}"
    d = date.fromisoformat(data["date"])
    dur = C.SERVICES[data["service"]]["dur"]
    if data["barber"] == "any":
        barber = S.resolve_barber(d, tm, dur)
    else:
        barber = data["barber"] if tm in S.free_slots(data["barber"], d, dur) else None
    if not barber:
        await ask_time(ev, ctx)  # слот только что заняли: показываем актуальные
        return
    await ctx.update_data(time=tm, barber_final=barber)
    _, uid = ev.get_ids()
    user = S.get_user(uid)
    if not user or not user.get("phone"):
        await ctx.update_data(mode="phone")
        await render(
            ev,
            "📱 Последний шаг: оставьте номер телефона, чтобы администратор мог связаться при необходимости.\n"
            "Нажмите кнопку или напишите номер сообщением.",
            [[("📱 Отправить мой номер", "!contact")], [("✖️ Отмена", "home")]],
        )
        return
    await ask_confirm(ev, ctx)


async def bk_confirm(ev, ctx):
    data = await ctx.get_data()
    _, uid = ev.get_ids()
    user = S.get_user(uid)
    if not user or not user.get("phone") or not all(k in data for k in ("service", "barber_final", "date", "time")):
        return await restart_flow(ev, ctx)
    b = S.create_booking(
        uid,
        data["service"],
        data["barber_final"],
        f"{data['date']} {data['time']}",
        user["name"] or sender_name(ev),
        user["phone"],
    )
    if not b:
        await ask_time(ev, ctx)  # время только что заняли
        return
    await ctx.clear()
    await render(
        ev,
        "✅ <b>Вы записаны!</b>\n\n"
        + booking_text(b)
        + f"\n📍 {C.SHOP['address']}\n\n"
        "Напомним о визите за сутки и за 2 часа. Отменить запись можно в разделе «Мои записи».",
        [[("📆 Добавить в календарь", f"ics:{b['id']}")], HOME],
    )
    await notify_admins(
        f"🆕 <b>Новая запись #{b['id']}</b>\n{booking_text(b)}\n👤 {esc(b['name'])} {esc(b['phone'])}"
    )


async def send_ics(ev, bid):
    _, uid = ev.get_ids()
    b = S.get_booking(bid)
    if not b or b["user_id"] != uid:
        return
    folder = tempfile.mkdtemp()
    path = os.path.join(folder, "barbershop-visit.ics")
    try:
        with open(path, "wb") as f:
            f.write(make_ics(b))
        await ev.message.answer("📆 Откройте файл, и событие добавится в календарь телефона.", attachments=[InputMedia(path=path)])
    except Exception as e:  # noqa: BLE001
        log.info("не удалось отправить .ics: %s", e)
        await ev.message.answer("Не удалось отправить файл календаря. Запись при этом сохранена.")
    finally:
        try:
            os.remove(path)
            os.rmdir(folder)
        except OSError:
            pass


# ============================== Мои записи ====================================
async def screen_my(ev):
    _, uid = ev.get_ids()
    rows = S.user_bookings(uid, upcoming=True)
    if not rows:
        return await render(
            ev, "У вас нет предстоящих записей.", [[("📅 Записаться", "bk:new")], [("🕘 История визитов", "my:hist")], HOME]
        )
    text = ["<b>📋 Предстоящие записи</b>\n"]
    kbd = []
    for b in rows:
        text.append(f"<b>№{b['id']}</b>\n{booking_text(b)}\n")
        kbd.append([(f"❌ Отменить №{b['id']}", f"cx:{b['id']}"), ("📆", f"ics:{b['id']}")])
    kbd.append([("🕘 История визитов", "my:hist")])
    kbd.append(HOME)
    await render(ev, "\n".join(text), kbd)


async def screen_history(ev):
    _, uid = ev.get_ids()
    rows = S.user_bookings(uid, upcoming=False, limit=8)
    names = {"done": "✅", "cancelled": "🚫 отменена", "noshow": "⚠️ не пришли"}
    if not rows:
        text = "История пока пуста."
    else:
        text = "<b>🕘 История визитов</b>\n\n" + "\n".join(
            f"{names[b['status']]} · {fmt_dt(S.parse(b['start_at']))} · "
            f"{C.SERVICES[b['service']]['name']} · {C.BARBERS[b['barber']]['nick']}"
            for b in rows
        )
    await render(ev, text, [[("◀️ К записям", "my:up")], HOME])


async def cancel_flow(ev, parts):
    _, uid = ev.get_ids()
    if parts[1] == "n":
        return await screen_my(ev)
    if parts[1] == "y":
        bid = int(parts[2])
        ok, why = S.cancel_booking(bid, uid=uid)
        if ok:
            b = S.get_booking(bid)
            await render(ev, "✅ Запись отменена.", [[("📅 Записаться снова", "bk:new")], HOME])
            await notify_admins(
                f"❌ <b>Клиент отменил запись #{bid}</b>\n{booking_text(b)}\n👤 {esc(b['name'])} {esc(b['phone'])}"
            )
        elif why == "too_late":
            await render(
                ev,
                f"До визита меньше {C.CANCEL_LIMIT_H} ч, отменить онлайн нельзя. Позвоните, пожалуйста: {C.SHOP['phone']}",
                [[("◀️ К записям", "my:up")]],
            )
        else:
            await render(ev, "Эта запись уже неактивна.", [[("◀️ К записям", "my:up")]])
        return
    bid = int(parts[1])
    b = S.get_booking(bid)
    if not b or b["user_id"] != uid or b["status"] != "active":
        return await screen_my(ev)
    await render(
        ev,
        f"Отменить запись?\n\n{booking_text(b)}",
        [[("Да, отменить", f"cx:y:{bid}"), ("Нет, оставить", "cx:n")]],
    )


# ================================ Барберы =====================================
async def screen_barbers(ev):
    rows = [[(f"{br['name']} · {br['role']}", f"brb:{k}")] for k, br in C.BARBERS.items()]
    rows.append(HOME)
    lines = ["<b>💈 Наши барберы</b>\n"] + [
        f"• {br['name']}: {rating_line(k)}" for k, br in C.BARBERS.items()
    ]
    await render(ev, "\n".join(lines) + "\n\nВыберите мастера:", rows)


async def screen_barber(ev, key):
    br = C.BARBERS.get(key)
    if not br:
        return
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
    await render(
        ev,
        text,
        [
            [(f"📅 Записаться к {br['nick']}", f"bk:pb:{key}")],
            [("⭐ Отзывы о мастере", f"rvl:{key}")],
            [("◀️ Все барберы", "brb:all")],
        ],
    )


# ============================ Услуги и цены ===================================
async def screen_services(ev):
    lines = ["<b>💰 Услуги и цены</b>\n"]
    for k, s in C.SERVICES.items():
        lines.append(f"{s['emoji']} <b>{s['name']}</b> · {s['dur']} мин · {price_text(k)}")
    lines.append(
        f"\n🌅 Пн-Пт до {C.MORNING_BEFORE_HOUR}:00 скидка {C.MORNING_DISCOUNT}%.\n"
        "Цена зависит от барбера. Нажмите услугу, чтобы увидеть подробности."
    )
    rows = [[(f"{s['emoji']} {s['name']}", f"svc:{k}")] for k, s in C.SERVICES.items()]
    rows.append(HOME)
    await render(ev, "\n".join(lines), rows)


async def screen_service(ev, key):
    s = C.SERVICES.get(key)
    if not s:
        return
    per_barber = "\n".join(f"• {br['name']}: {money(barber_price(key, bk))}" for bk, br in C.BARBERS.items())
    await render(
        ev,
        f"{s['emoji']} <b>{s['name']}</b>\n⏱ {s['dur']} мин\n\n{s['desc']}\n\n<b>Цена</b>\n{per_barber}",
        [[("📅 Записаться на эту услугу", f"bk:ps:{key}")], [("◀️ Все услуги", "svc:all")]],
    )


# ================================== Отзывы ====================================
def review_text(r):
    who = C.BARBERS[r["barber"]]["nick"]
    body = f"\n«{esc(r['text'])}»" if r["text"] else ""
    return f"\n{stars(r['rating'])} · {esc(r['author'])} → {who}{body}"


async def screen_reviews(ev):
    lines = [f"<b>⭐ Отзывы гостей</b>\nОбщий рейтинг: {rating_line()}\n"]
    for k, br in C.BARBERS.items():
        lines.append(f"• {br['name']}: {rating_line(k)}")
    lines.append("\n<b>Последние отзывы</b>")
    for r in S.last_reviews(limit=3):
        lines.append(review_text(r))
    rows = [[(f"Отзывы: {br['nick']}", f"rvl:{k}")] for k, br in C.BARBERS.items()]
    rows.append([("✍️ Оставить отзыв", "rvw")])
    rows.append(HOME)
    await render(ev, "\n".join(lines), rows)


async def screen_reviews_barber(ev, key):
    if key not in C.BARBERS:
        return
    br = C.BARBERS[key]
    rows = S.last_reviews(key, limit=6)
    text = f"<b>⭐ Отзывы: {br['name']}</b>\n{rating_line(key)}\n" + (
        "".join(review_text(r) for r in rows) if rows else "\nПока нет отзывов."
    )
    await render(ev, text, [[(f"📅 Записаться к {br['nick']}", f"bk:pb:{key}")], [("◀️ Все отзывы", "rvall")]])


async def review_pick(ev):
    _, uid = ev.get_ids()
    rows = S.reviewable(uid)
    if not rows:
        return await render(
            ev,
            "Оставить отзыв можно после визита. Как только мастер отметит ваш визит, придёт запрос на оценку.",
            [[("◀️ Все отзывы", "rvall")]],
        )
    kbd = [
        [(f"{fmt_day(S.parse(b['start_at']))} · {C.BARBERS[b['barber']]['nick']}", f"rvb:{b['id']}")]
        for b in rows
    ]
    kbd.append([("◀️ Все отзывы", "rvall")])
    await render(ev, "Выберите визит, который хотите оценить:", kbd)


async def review_rate(ev, ctx, bid, n):
    _, uid = ev.get_ids()
    b = S.get_booking(bid)
    if not b or b["user_id"] != uid or b["status"] != "done" or S.has_review(b["id"]):
        return await render(ev, "Отзыв на этот визит уже оставлен 👌", [HOME])
    await ctx.update_data(mode="review", rv_bid=b["id"], rv_rating=n)
    await render(
        ev,
        f"Оценка: {stars(n)}\nНапишите пару слов о визите или нажмите «Пропустить».",
        [[("Пропустить", "rvskip")]],
    )


async def save_review(ev, ctx, text):
    _, uid = ev.get_ids()
    data = await ctx.get_data()
    await ctx.clear()
    b = S.get_booking(data.get("rv_bid", 0))
    if not b:
        return
    author = sender_name(ev).split()[0] if sender_name(ev) else "Гость"
    ok = S.add_review(b["id"], uid, b["barber"], data["rv_rating"], text, author)
    await render(
        ev,
        "Спасибо за отзыв! 🙏" if ok else "Отзыв уже сохранён 👌",
        [[("⭐ Все отзывы", "rvall")], HOME],
        new=True,
    )
    if ok and data["rv_rating"] <= 3:
        await notify_admins(
            f"⚠️ <b>Низкая оценка {data['rv_rating']}/5</b> · {C.BARBERS[b['barber']]['name']}\n"
            f"👤 {esc(b['name'])} {esc(b['phone'])}\n{esc(text) or 'без комментария'}"
        )


# ============================== Акции, контакты, FAQ ==========================
async def screen_promo(ev):
    text = "<b>🎁 Акции и скидки</b>\n\n" + "\n\n".join(f"<b>{t}</b>\n{d}" for t, d in C.PROMOS)
    await render(ev, text, [[("📅 Записаться", "bk:new")], HOME])


async def screen_contacts(ev):
    sh = C.SHOP
    await render(
        ev,
        f"<b>📍 {sh['name']} · {sh['tagline']}</b>\n\n"
        f"Адрес: {sh['address']}\n"
        f"Как найти: {sh['landmark']}\n"
        f"🕒 {sh['hours']}\n"
        f"🚗 {sh['parking']}\n"
        f"📞 {sh['phone']}",
        [
            [("🗺 Яндекс Карты", f"https://yandex.ru/maps/?pt={sh['lon']},{sh['lat']}&z=17&l=map")],
            [("🗺 2ГИС", f"https://2gis.ru/geo/{sh['lon']},{sh['lat']}")],
            HOME,
        ],
    )


async def screen_faq(ev):
    rows = [[(q, f"faq:{i}")] for i, (q, _) in enumerate(C.FAQ)]
    rows.append(HOME)
    await render(ev, "<b>❓ Частые вопросы</b>\nВыберите вопрос:", rows)


async def screen_faq_answer(ev, i):
    if not 0 <= i < len(C.FAQ):
        return
    q, a = C.FAQ[i]
    await render(
        ev,
        f"<b>{q}</b>\n\n{a}",
        [[("◀️ Все вопросы", "faq:all")], [("✉️ Не нашли ответ? Напишите нам", "support")]],
    )


# ======================== Связь с администратором =============================
async def support_start(ev, ctx):
    await ctx.clear()
    await ctx.update_data(mode="support")
    await render(ev, "Напишите сообщение одним текстом, администратор ответит прямо здесь.", [[("✖️ Отмена", "home")]])


async def support_send(ev, ctx, text):
    _, uid = ev.get_ids()
    await ctx.clear()
    if not C.MAX_ADMIN_IDS:
        return await render(
            ev,
            "В демо-режиме администратор не подключён. В рабочей версии сообщение уходит администратору.",
            [HOME],
            new=True,
        )
    user = S.get_user(uid) or {}
    await notify_admins(
        f"✉️ <b>Сообщение от клиента</b>\n👤 {esc(sender_name(ev))} {esc(user.get('phone'))}\n\n{esc(text)}",
        [[("↩️ Ответить", f"adm:reply:{uid}")]],
    )
    await render(ev, "Отправлено ✅ Ответим в ближайшее время.", [HOME], new=True)


# ================================ Админ-панель ================================
ADMIN_ROWS = [
    [("📅 Сегодня", "adm:day:0"), ("📅 Завтра", "adm:day:1")],
    [("📊 Статистика за 30 дней", "adm:stats")],
    [("📣 Рассылка клиентам", "adm:bc")],
    [("🧪 Заполнить демо-данными", "adm:demo")],
    HOME,
]


async def screen_admin(ev, ctx=None):
    if ctx is not None:
        await ctx.clear()
    await render(ev, "<b>🛠 Админ-панель «Чёрный дуб»</b>", ADMIN_ROWS)


async def render_day(ev, off):
    d = S.now_local().date() + timedelta(days=off)
    rows = S.bookings_on(d)
    icon = {"active": "🟡", "done": "✅", "noshow": "🚫"}
    lines = [f"<b>📅 {fmt_day(d)}</b> · записей: {len(rows)}"]
    kbd = []
    for b in rows:
        hhmm = b["start_at"][11:]
        lines.append(
            f"{icon[b['status']]} {hhmm} · {C.BARBERS[b['barber']]['nick']} · "
            f"{C.SERVICES[b['service']]['name']} · {esc(b['name'])} {esc(b['phone'])} · {money(b['price'])}"
        )
        if b["status"] == "active":
            kbd.append(
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
    kbd.append([("◀️", f"adm:day:{off - 1}"), ("▶️", f"adm:day:{off + 1}")])
    kbd.append([("🛠 Панель", "adm:home")])
    await render(ev, "\n".join(lines), kbd)


async def admin_booking_action(ev, action, bid, off):
    b = S.get_booking(bid)
    if not b or b["status"] != "active":
        return await render_day(ev, off)
    if action == "ok":
        S.mark_done(bid)
        await send_safe(
            b["user_id"],
            "Спасибо, что были у нас! 🙌\n\nОцените визит, это займёт 5 секунд:",
            stars_rows(bid),
        )
    elif action == "ns":
        S.mark_noshow(bid)
    else:
        S.cancel_booking(bid, force=True)
        await send_safe(
            b["user_id"],
            f"К сожалению, администратор отменил вашу запись:\n\n{booking_text(b)}\n\n"
            f"Приносим извинения. Запишитесь на другое время или позвоните: {C.SHOP['phone']}",
            [[("📅 Записаться", "bk:new")]],
        )
    await render_day(ev, off)


async def admin_stats(ev):
    st = S.stats(30)
    avg, n = st["rating"]
    top = "\n".join(
        f"  {i}. {C.SERVICES[t['service']]['name']}: {t['n']}" for i, t in enumerate(st["top_services"], 1)
    )
    await render(
        ev,
        f"<b>📊 Статистика за {st['days']} дней</b>\n\n"
        f"Визитов: {st['done']}\n"
        f"Выручка: {money(st['revenue'])}\n"
        f"Средний чек: {money(st['avg_check'])}\n"
        f"Отмены: {st['cancelled']} · Неявки: {st['noshow']}\n"
        f"Предстоящих записей: {st['upcoming']}\n"
        f"Новых клиентов: {st['new_users']}\n"
        f"Рейтинг: {avg} ({n} отзывов)\n\n"
        f"<b>Топ услуг</b>\n{top or '  пока нет данных'}",
        [[("🛠 Панель", "adm:home")]],
    )


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
        b = S.create_booking(uid, svc, barber, f"{(today + timedelta(days=off)).isoformat()} {tm}", name, phone)
        if b:
            created += 1
            if i <= 2:
                S.mark_done(b["id"])  # чтобы в статистике были цифры
    return created


async def broadcast_preview(ev, ctx):
    body = getattr(ev.message, "body", None)
    text = getattr(body, "html_text", None) or esc(message_text(ev))
    await ctx.update_data(mode=None, bc_text=text)
    count = len([u for u in S.all_user_ids() if u > 0])
    await render(
        ev,
        f"<b>Предпросмотр рассылки</b> ({count} получателей):\n\n{text}",
        [[("🚀 Отправить", "adm:bcgo"), ("✖️ Отмена", "adm:home")]],
        new=True,
    )


async def broadcast_go(ev, ctx):
    data = await ctx.get_data()
    text = data.get("bc_text")
    await ctx.clear()
    if not text:
        return await screen_admin(ev)
    sent = failed = 0
    for uid in S.all_user_ids():
        if uid <= 0:
            continue
        if await send_safe(uid, text):
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(0.1)  # не упираться в лимиты API
    await render(ev, f"📣 Рассылка завершена. Доставлено: {sent}, не доставлено: {failed}.", [[("🛠 Панель", "adm:home")]])


# ============================ Обработка кнопок ================================
async def on_callback(event: MessageCallback, context: MemoryContext):
    try:
        await event.ack()  # убрать «часики» на кнопке
    except Exception:  # noqa: BLE001
        pass
    try:
        await route_callback(event, context, event.callback.payload or "")
    except Exception:  # noqa: BLE001  один сбой не должен ронять бота
        log.exception("ошибка в обработчике кнопки %r", getattr(event.callback, "payload", None))


async def route_callback(ev, ctx, p):
    _, uid = ev.get_ids()
    parts = p.split(":")
    head = parts[0]
    ensure_user(ev)

    if p == "home":
        return await screen_home(ev, ctx)

    # ---- запись ----
    if head == "bk":
        sub = parts[1]
        if sub == "new":
            return await restart_flow(ev, ctx)
        if sub == "pb" and parts[2] in C.BARBERS:  # «записаться к этому барберу»
            await ctx.clear()
            await ctx.update_data(barber_pre=parts[2])
            return await ask_service(ev)
        if sub == "ps" and parts[2] in C.SERVICES:  # услуга из каталога: начинаем запись с чистого листа
            await ctx.clear()
            await ctx.update_data(service=parts[2])
            return await ask_barber(ev, ctx)
        if sub == "s" and parts[2] in C.SERVICES:
            await ctx.update_data(service=parts[2], mode=None)
            pre = (await ctx.get_data()).get("barber_pre")
            if pre:
                await ctx.update_data(barber=pre)
                return await ask_date(ev, ctx)
            return await ask_barber(ev, ctx)
        data = await ctx.get_data()
        if "service" not in data:
            return await restart_flow(ev, ctx)
        if sub == "b" and (parts[2] == "any" or parts[2] in C.BARBERS):
            await ctx.update_data(barber=parts[2])
            return await ask_date(ev, ctx)
        if sub == "d":
            if "barber" not in data:
                return await restart_flow(ev, ctx)
            await ctx.update_data(date=parts[2])
            return await ask_time(ev, ctx)
        if sub == "t":
            return await bk_time(ev, ctx, parts[2])
        if sub == "back":
            step = parts[2]
            if step == "svc":
                return await ask_service(ev)
            if step == "barber":
                return await (ask_service(ev) if data.get("barber_pre") else ask_barber(ev, ctx))
            return await ask_date(ev, ctx)
        if sub == "ok":
            return await bk_confirm(ev, ctx)
        return

    # ---- разделы ----
    if p == "my:up":
        return await screen_my(ev)
    if p == "my:hist":
        return await screen_history(ev)
    if head == "cx":
        return await cancel_flow(ev, parts)
    if head == "ics":
        return await send_ics(ev, int(parts[1]))
    if p == "brb:all":
        return await screen_barbers(ev)
    if head == "brb":
        return await screen_barber(ev, parts[1])
    if p == "svc:all":
        return await screen_services(ev)
    if head == "svc":
        return await screen_service(ev, parts[1])
    if p == "rvall":
        return await screen_reviews(ev)
    if head == "rvl":
        return await screen_reviews_barber(ev, parts[1])
    if p == "rvw":
        return await review_pick(ev)
    if head == "rvb":
        return await render(ev, "Как всё прошло? Поставьте оценку:", stars_rows(int(parts[1])) + [HOME])
    if head == "rv":
        return await review_rate(ev, ctx, int(parts[1]), int(parts[2]))
    if p == "rvskip":
        return await save_review(ev, ctx, "")
    if p == "promo":
        return await screen_promo(ev)
    if p == "contacts":
        return await screen_contacts(ev)
    if p == "faq:all":
        return await screen_faq(ev)
    if head == "faq":
        return await screen_faq_answer(ev, int(parts[1]))
    if p == "support":
        return await support_start(ev, ctx)

    # ---- админ ----
    if head == "adm":
        if not is_admin(uid):
            return
        sub = parts[1]
        if sub == "home":
            return await screen_admin(ev, ctx)
        if sub == "day":
            return await render_day(ev, int(parts[2]))
        if sub in ("ok", "ns", "cx"):
            return await admin_booking_action(ev, sub, int(parts[2]), int(parts[3]))
        if sub == "stats":
            return await admin_stats(ev)
        if sub == "demo":
            created = seed_demo()
            return await render(ev, f"🧪 Создано демо-записей: {created}. Откройте «Сегодня»/«Завтра».", ADMIN_ROWS)
        if sub == "bc":
            await ctx.clear()
            await ctx.update_data(mode="broadcast")
            return await render(ev, "Отправьте текст рассылки одним сообщением.", [[("✖️ Отмена", "adm:home")]])
        if sub == "bcgo":
            return await broadcast_go(ev, ctx)
        if sub == "reply":
            await ctx.clear()
            await ctx.update_data(mode="adm_reply", to=int(parts[2]))
            return await render(ev, "Напишите ответ клиенту одним сообщением.", [[("✖️ Отмена", "adm:home")]])


# ============================ Обработка сообщений =============================
async def on_message(event: MessageCreated, context: MemoryContext):
    try:
        await route_message(event, context)
    except Exception:  # noqa: BLE001
        log.exception("ошибка в обработчике сообщения")


async def route_message(ev, ctx):
    _, uid = ev.get_ids()
    if uid is None or getattr(ev.message, "body", None) is None:
        return
    text = message_text(ev)
    data = await ctx.get_data()
    mode = data.get("mode")
    ensure_user(ev)

    cmd = text.split()[0][1:].split("@")[0].lower() if text.startswith("/") else None
    if cmd in ("start", "menu", "cancel"):
        return await screen_home(ev, ctx, new=True)
    if cmd == "id":
        return await ev.message.answer(f"Ваш ID в MAX: {uid}")
    if cmd == "admin":
        if is_admin(uid):
            return await screen_admin(ev, ctx)
        return await ev.message.answer(
            f"Админ-панель доступна только администратору. Ваш ID: {uid}. Добавьте его в MAX_ADMIN_IDS в файле .env."
        )

    if mode == "phone":
        phone = contact_phone(ev) or normalize_phone(text)
        if not phone:
            return await render(
                ev,
                "Не похоже на номер телефона. Пример: +7 914 000-00-00\nМожно нажать кнопку ниже.",
                [[("📱 Отправить мой номер", "!contact")], [("✖️ Отмена", "home")]],
                new=True,
            )
        S.set_contact(uid, sender_name(ev), phone)
        await ctx.update_data(mode=None)
        if "barber_final" not in data:
            return await screen_home(ev, ctx, new=True)
        return await ask_confirm(ev, ctx, new=True)
    if mode == "review" and text:
        return await save_review(ev, ctx, text[:500])
    if mode == "support" and text:
        return await support_send(ev, ctx, text)
    if mode == "adm_reply" and text and is_admin(uid):
        await ctx.clear()
        ok = await send_safe(data["to"], f"💬 <b>Ответ администратора:</b>\n\n{esc(text)}", [HOME])
        return await render(ev, "Отправлено ✅" if ok else "Не удалось доставить сообщение.", [[("🛠 Панель", "adm:home")]], new=True)
    if mode == "broadcast" and text and is_admin(uid):
        return await broadcast_preview(ev, ctx)

    await render(ev, "Не совсем понял 🤔 Выберите раздел:", menu_rows(uid), new=True)


async def on_started(event: BotStarted, context: MemoryContext = None):
    """Пользователь нажал «Начать» в диалоге с ботом."""
    user = event.user
    S.upsert_user(user.user_id, getattr(user, "full_name", None) or user.first_name)
    name = esc(getattr(user, "first_name", "") or "гость")
    await BOT.send_message(
        chat_id=event.chat_id,
        text=WELCOME.format(name=name, shop=C.SHOP["name"], tag=C.SHOP["tagline"]),
        attachments=[kb(menu_rows(user.user_id))],
        format="html",
    )


# =============================== Напоминания ==================================
async def reminder_loop():
    while True:
        try:
            for b, kind in S.due_reminders():
                head = "⏰ Напоминаем: ваша запись в ближайшие сутки" if kind == "24h" else "⏰ Ваша запись совсем скоро"
                await send_safe(
                    b["user_id"],
                    f"{head}\n\n{booking_text(b)}\n📍 {C.SHOP['address']}",
                    [[("❌ Отменить запись", f"cx:{b['id']}")]],
                )
                S.mark_reminded(b["id"], kind)
        except Exception:  # noqa: BLE001
            log.exception("ошибка в цикле напоминаний")
        await asyncio.sleep(60)


# ================================== Запуск ====================================
def build_dispatcher():
    dp = Dispatcher()
    dp.bot_started()(on_started)
    dp.message_callback()(on_callback)
    dp.message_created()(on_message)
    return dp


async def main():
    global BOT
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not C.MAX_BOT_TOKEN:
        raise SystemExit("Не задан MAX_BOT_TOKEN. Скопируйте .env.example в .env и впишите токен из business.max.ru.")
    S.init_db(C.MAX_DB_PATH)
    BOT = Bot(token=C.MAX_BOT_TOKEN)
    dp = build_dispatcher()
    try:
        await BOT.delete_webhook()  # polling не работает при активной webhook-подписке
    except Exception as e:  # noqa: BLE001
        log.info("delete_webhook: %s", e)
    reminders = asyncio.create_task(reminder_loop())
    log.info("Бот для MAX запущен")
    try:
        await dp.start_polling(BOT)
    finally:
        reminders.cancel()


if __name__ == "__main__":
    asyncio.run(main())
