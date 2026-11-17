"""Хранилище (SQLite) и бизнес-логика демо-бота.

Модуль не зависит от aiogram и покрыт тестами (test_logic.py).
Время везде локальное для заведения (config.TZ), хранится строкой "ГГГГ-ММ-ДД ЧЧ:ММ".
"""
import sqlite3
from datetime import date, datetime, time, timedelta

import config as C

FMT = "%Y-%m-%d %H:%M"
_conn = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY,
    name TEXT,
    phone TEXT,
    created TEXT
);
CREATE TABLE IF NOT EXISTS bookings(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    service TEXT NOT NULL,
    barber TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    price INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    name TEXT,
    phone TEXT,
    created TEXT,
    rem24 INTEGER NOT NULL DEFAULT 0,
    rem2 INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_bookings_day ON bookings(barber, start_at);
CREATE TABLE IF NOT EXISTS reviews(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id INTEGER UNIQUE,
    user_id INTEGER,
    barber TEXT NOT NULL,
    rating INTEGER NOT NULL,
    text TEXT,
    author TEXT,
    created TEXT
);
"""

# Демо-отзывы, чтобы рейтинги не были пустыми при показе клиенту
SEED_REVIEWS = [
    ("Дмитрий", "artem", 5, "Лучший фейд в городе. Держится три недели, форма не уходит.", 3),
    ("Андрей К.", "artem", 5, "Артём сразу понял, что мне нужно, и предложил вариант лучше моего.", 9),
    ("Сергей", "artem", 4, "Круто, но пришлось подождать минут 10. Результатом доволен.", 15),
    ("Игорь", "mark", 5, "Бритьё опасной бритвой как в кино. Приду ещё.", 2),
    ("Максим", "mark", 5, "Подправил бороду, теперь жена говорит, что мне идёт.", 7),
    ("Олег", "mark", 4, "Спокойная атмосфера, аккуратная работа.", 12),
    ("Наталья (за сына)", "ildar", 5, "Ребёнок остался доволен и не плакал. Спасибо!", 4),
    ("Виктор", "ildar", 5, "Недорого и быстро, стрижка ровная.", 10),
]


# ---------- Служебное ----------
def now_local():
    return datetime.now(C.TZ).replace(tzinfo=None, second=0, microsecond=0)


def init_db(path=None, seed=True):
    """Открыть (или пересоздать подключение к) базу. Для тестов можно передать ':memory:'."""
    global _conn
    if _conn is not None:
        _conn.close()
    _conn = sqlite3.connect(path or C.DB_PATH)
    _conn.row_factory = sqlite3.Row
    _conn.executescript(SCHEMA)
    _conn.commit()
    if seed and not _one("SELECT id FROM reviews LIMIT 1"):
        base = now_local()
        for author, barber, rating, text, days_ago in SEED_REVIEWS:
            _run(
                "INSERT INTO reviews(booking_id,user_id,barber,rating,text,author,created) "
                "VALUES(NULL,0,?,?,?,?,?)",
                (barber, rating, text, author, (base - timedelta(days=days_ago)).strftime(FMT)),
            )


def _db():
    if _conn is None:
        init_db()
    return _conn


def _one(sql, args=()):
    row = _db().execute(sql, args).fetchone()
    return dict(row) if row else None


def _all(sql, args=()):
    return [dict(r) for r in _db().execute(sql, args).fetchall()]


def _run(sql, args=()):
    cur = _db().execute(sql, args)
    _db().commit()
    return cur


def parse(s):
    return datetime.strptime(s, FMT)


# ---------- Пользователи ----------
def get_user(uid):
    return _one("SELECT * FROM users WHERE id=?", (uid,))


def upsert_user(uid, name):
    """Вернуть (пользователь, создан_ли_сейчас)."""
    user = get_user(uid)
    if user:
        return user, False
    _run("INSERT INTO users(id,name,created) VALUES(?,?,?)", (uid, name, now_local().strftime(FMT)))
    return get_user(uid), True


def reviewable(uid, limit=5):
    """Состоявшиеся визиты пользователя, на которые ещё нет отзыва."""
    return _all(
        "SELECT * FROM bookings WHERE user_id=? AND status='done' AND id NOT IN "
        "(SELECT booking_id FROM reviews WHERE booking_id IS NOT NULL) "
        "ORDER BY start_at DESC LIMIT ?",
        (uid, limit),
    )


def set_contact(uid, name, phone):
    _run("UPDATE users SET name=?, phone=? WHERE id=?", (name, phone, uid))


def all_user_ids():
    return [r["id"] for r in _all("SELECT id FROM users")]


# ---------- Расписание и цены ----------
def work_hours(d):
    return C.HOURS_SUNDAY if d.weekday() == 6 else C.HOURS_WEEKDAY


def barber_works(barber_key, d):
    return d.weekday() not in C.BARBERS[barber_key]["days_off"]


def calc_price(service_key, barber_key, start_dt):
    """Вернуть (цена_барбера, скидка_в_процентах, итоговая_цена)."""
    base = C.SERVICES[service_key]["price"] * C.BARBERS[barber_key]["mult"]
    base = int(round(base / 50.0) * 50)
    discount = 0
    if start_dt.weekday() < 5 and start_dt.hour < C.MORNING_BEFORE_HOUR:
        discount = C.MORNING_DISCOUNT
    final = int(round(base * (100 - discount) / 100.0 / 10.0) * 10)
    return base, discount, final


def price_range(service_key):
    prices = [calc_price(service_key, b, datetime(2030, 1, 5, 15, 0))[0] for b in C.BARBERS]
    return min(prices), max(prices)


def _busy(barber_key, d):
    rows = _all(
        "SELECT start_at, end_at FROM bookings WHERE barber=? AND status='active' "
        "AND substr(start_at,1,10)=?",
        (barber_key, d.strftime("%Y-%m-%d")),
    )
    return [(parse(r["start_at"]), parse(r["end_at"])) for r in rows]


def free_slots(barber_key, d, duration, now=None):
    """Свободные времена начала ("ЧЧ:ММ") у барбера на дату."""
    now = now or now_local()
    if not barber_works(barber_key, d):
        return []
    open_h, close_h = work_hours(d)
    t = datetime.combine(d, time(open_h))
    close_dt = datetime.combine(d, time(close_h))
    earliest = now + timedelta(minutes=C.MIN_LEAD_MIN)
    busy = _busy(barber_key, d)
    result = []
    while t + timedelta(minutes=duration) <= close_dt:
        end = t + timedelta(minutes=duration)
        if t >= earliest and all(end <= b_start or t >= b_end for b_start, b_end in busy):
            result.append(t.strftime("%H:%M"))
        t += timedelta(minutes=C.SLOT_STEP)
    return result


def free_slots_any(d, duration, now=None):
    """{"ЧЧ:ММ": [ключи барберов, у которых это время свободно]} по всем барберам."""
    merged = {}
    for key in C.BARBERS:
        for slot in free_slots(key, d, duration, now):
            merged.setdefault(slot, []).append(key)
    return dict(sorted(merged.items()))


def resolve_barber(d, time_str, duration, now=None):
    """Выбрать барбера для «любого свободного»: у кого в этот день меньше записей."""
    candidates = free_slots_any(d, duration, now).get(time_str, [])
    if not candidates:
        return None
    return min(candidates, key=lambda k: len(_busy(k, d)))


# ---------- Записи ----------
def get_booking(bid):
    return _one("SELECT * FROM bookings WHERE id=?", (bid,))


def create_booking(uid, service_key, barber_key, start_str, name, phone, now=None):
    """Создать запись. None, если слот недоступен (занят, выходной, в прошлом и т. д.)."""
    if service_key not in C.SERVICES or barber_key not in C.BARBERS:
        return None
    start = parse(start_str)
    duration = C.SERVICES[service_key]["dur"]
    if start.strftime("%H:%M") not in free_slots(barber_key, start.date(), duration, now):
        return None
    end = start + timedelta(minutes=duration)
    price = calc_price(service_key, barber_key, start)[2]
    cur = _run(
        "INSERT INTO bookings(user_id,service,barber,start_at,end_at,price,status,"
        "name,phone,created) VALUES(?,?,?,?,?,?,'active',?,?,?)",
        (
            uid,
            service_key,
            barber_key,
            start.strftime(FMT),
            end.strftime(FMT),
            price,
            name,
            phone,
            (now or now_local()).strftime(FMT),
        ),
    )
    return get_booking(cur.lastrowid)


def user_bookings(uid, upcoming=True, limit=10, now=None):
    now_s = (now or now_local()).strftime(FMT)
    if upcoming:
        return _all(
            "SELECT * FROM bookings WHERE user_id=? AND status='active' AND start_at>=? "
            "ORDER BY start_at LIMIT ?",
            (uid, now_s, limit),
        )
    return _all(
        "SELECT * FROM bookings WHERE user_id=? AND status IN ('done','noshow','cancelled') "
        "ORDER BY start_at DESC LIMIT ?",
        (uid, limit),
    )


def cancel_booking(bid, uid=None, force=False, now=None):
    """Отменить запись. Возвращает (успех, причина): "ok", "not_found", "not_active", "too_late"."""
    b = get_booking(bid)
    if not b or (uid is not None and b["user_id"] != uid):
        return False, "not_found"
    if b["status"] != "active":
        return False, "not_active"
    now = now or now_local()
    if not force and parse(b["start_at"]) - now < timedelta(hours=C.CANCEL_LIMIT_H):
        return False, "too_late"
    _run("UPDATE bookings SET status='cancelled' WHERE id=?", (bid,))
    return True, "ok"


def mark_done(bid):
    """Визит состоялся. Возвращает запись или None, если она уже не активна."""
    b = get_booking(bid)
    if not b or b["status"] != "active":
        return None
    _run("UPDATE bookings SET status='done' WHERE id=?", (bid,))
    return get_booking(bid)


def mark_noshow(bid):
    b = get_booking(bid)
    if not b or b["status"] != "active":
        return False
    _run("UPDATE bookings SET status='noshow' WHERE id=?", (bid,))
    return True


def bookings_on(d):
    return _all(
        "SELECT * FROM bookings WHERE substr(start_at,1,10)=? "
        "AND status IN ('active','done','noshow') ORDER BY start_at",
        (d.strftime("%Y-%m-%d"),),
    )


def due_reminders(now=None):
    """Список (запись, "24h" | "2h") для напоминаний, которые пора отправить."""
    now = now or now_local()
    out = []
    rows = _all(
        "SELECT * FROM bookings WHERE status='active' AND start_at>? AND (rem24=0 OR rem2=0)",
        (now.strftime(FMT),),
    )
    for b in rows:
        start = parse(b["start_at"])
        delta = start - now
        if delta <= timedelta(hours=2) and not b["rem2"]:
            out.append((b, "2h"))
        elif timedelta(hours=2) < delta <= timedelta(hours=24) and not b["rem24"]:
            created = parse(b["created"]) if b["created"] else now
            # напоминание «за сутки» имеет смысл, только если записались заранее
            if start - created > timedelta(hours=26):
                out.append((b, "24h"))
            else:
                _run("UPDATE bookings SET rem24=1 WHERE id=?", (b["id"],))
    return out


def mark_reminded(bid, kind):
    if kind == "2h":
        _run("UPDATE bookings SET rem2=1, rem24=1 WHERE id=?", (bid,))
    else:
        _run("UPDATE bookings SET rem24=1 WHERE id=?", (bid,))


# ---------- Отзывы ----------
def has_review(booking_id):
    return _one("SELECT id FROM reviews WHERE booking_id=?", (booking_id,)) is not None


def add_review(booking_id, uid, barber_key, rating, text, author):
    if booking_id is not None and has_review(booking_id):
        return False
    _run(
        "INSERT INTO reviews(booking_id,user_id,barber,rating,text,author,created) "
        "VALUES(?,?,?,?,?,?,?)",
        (booking_id, uid, barber_key, int(rating), (text or "").strip(), author, now_local().strftime(FMT)),
    )
    return True


def barber_rating(barber_key=None):
    """(средний рейтинг, количество отзывов); (0, 0) если отзывов нет."""
    if barber_key:
        row = _one("SELECT AVG(rating) a, COUNT(*) n FROM reviews WHERE barber=?", (barber_key,))
    else:
        row = _one("SELECT AVG(rating) a, COUNT(*) n FROM reviews")
    if not row or not row["n"]:
        return 0.0, 0
    return round(row["a"], 1), row["n"]


def last_reviews(barber_key=None, limit=5):
    if barber_key:
        return _all(
            "SELECT * FROM reviews WHERE barber=? ORDER BY created DESC, id DESC LIMIT ?",
            (barber_key, limit),
        )
    return _all("SELECT * FROM reviews ORDER BY created DESC, id DESC LIMIT ?", (limit,))


# ---------- Статистика для админа ----------
def stats(days=30, now=None):
    now = now or now_local()
    since = (now - timedelta(days=days)).strftime(FMT)
    done = _one(
        "SELECT COUNT(*) n, COALESCE(SUM(price),0) revenue FROM bookings "
        "WHERE status='done' AND start_at>=?",
        (since,),
    )
    count = lambda status: _one(  # noqa: E731
        "SELECT COUNT(*) n FROM bookings WHERE status=? AND start_at>=?", (status, since)
    )["n"]
    upcoming = _one(
        "SELECT COUNT(*) n FROM bookings WHERE status='active' AND start_at>=?",
        (now.strftime(FMT),),
    )["n"]
    top = _all(
        "SELECT service, COUNT(*) n FROM bookings WHERE status='done' AND start_at>=? "
        "GROUP BY service ORDER BY n DESC LIMIT 3",
        (since,),
    )
    new_users = _one("SELECT COUNT(*) n FROM users WHERE created>=?", (since,))["n"]
    return {
        "days": days,
        "done": done["n"],
        "revenue": done["revenue"],
        "avg_check": int(done["revenue"] / done["n"]) if done["n"] else 0,
        "cancelled": count("cancelled"),
        "noshow": count("noshow"),
        "upcoming": upcoming,
        "new_users": new_users,
        "top_services": top,
        "rating": barber_rating(),
    }
