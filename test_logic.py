"""Тесты бизнес-логики (без aiogram): python -m unittest -v"""
import unittest
from datetime import date, datetime, timedelta

import config as C
import storage as S

# Фиксированное «сейчас»: понедельник, 16 ноября 2026, 09:00
NOW = datetime(2026, 11, 16, 9, 0)
TUE = date(2026, 11, 17)  # вторник: работают все
MON = date(2026, 11, 16)  # понедельник: у Артёма выходной
WED = date(2026, 11, 18)  # среда: у Ильдара выходной
SUN = date(2026, 11, 22)  # воскресенье: у Марка выходной, часы 11-19


class LogicTest(unittest.TestCase):
    def setUp(self):
        S.init_db(":memory:", seed=False)
        S.upsert_user(1, "Тест")

    def book(self, uid=1, svc="cut", barber="mark", start="2026-11-17 15:00"):
        return S.create_booking(uid, svc, barber, start, "Тест", "+79000000000", now=NOW)

    # --- расписание ---
    def test_work_hours(self):
        self.assertEqual(S.work_hours(TUE), (10, 21))
        self.assertEqual(S.work_hours(SUN), (11, 19))

    def test_day_off(self):
        self.assertEqual(S.free_slots("artem", MON, 60, NOW), [])
        self.assertEqual(S.free_slots("ildar", WED, 60, NOW), [])
        self.assertEqual(S.free_slots("mark", SUN, 60, NOW), [])
        self.assertTrue(S.free_slots("mark", MON, 60, NOW))

    def test_min_lead_time(self):
        # сейчас 09:00 в понедельник, окно начинается не раньше 10:00
        slots = S.free_slots("mark", MON, 60, NOW)
        self.assertEqual(slots[0], "10:00")
        later = S.free_slots("mark", MON, 60, datetime(2026, 11, 16, 12, 10))
        self.assertEqual(later[0], "13:30")

    def test_last_slot_fits_duration(self):
        slots = S.free_slots("mark", TUE, 120, NOW)  # закрытие в 21:00
        self.assertEqual(slots[-1], "19:00")

    def test_sunday_slots(self):
        slots = S.free_slots("artem", SUN, 60, NOW)
        self.assertEqual(slots[0], "11:00")
        self.assertEqual(slots[-1], "18:00")

    # --- запись и конфликты ---
    def test_booking_blocks_overlapping_slots(self):
        self.assertIsNotNone(self.book(start="2026-11-17 15:00"))  # 15:00-16:00
        slots = S.free_slots("mark", TUE, 60, NOW)
        self.assertNotIn("14:30", slots)  # пересекается
        self.assertNotIn("15:00", slots)
        self.assertNotIn("15:30", slots)
        self.assertIn("14:00", slots)  # 14:00-15:00 встык
        self.assertIn("16:00", slots)

    def test_double_booking_rejected(self):
        self.assertIsNotNone(self.book())
        self.assertIsNone(self.book())

    def test_other_barber_still_free(self):
        self.book(barber="mark")
        self.assertIsNotNone(self.book(barber="ildar"))

    def test_invalid_slot_rejected(self):
        self.assertIsNone(self.book(start="2026-11-17 03:00"))
        self.assertIsNone(self.book(barber="artem", start="2026-11-16 12:00"))  # выходной
        self.assertIsNone(self.book(svc="nope"))

    def test_resolve_any_barber_picks_least_loaded(self):
        self.book(barber="mark", start="2026-11-17 12:00")
        self.book(barber="mark", start="2026-11-17 13:00")
        chosen = S.resolve_barber(TUE, "15:00", 60, NOW)
        self.assertIn(chosen, ("artem", "ildar"))
        self.assertIsNone(S.resolve_barber(TUE, "03:00", 60, NOW))

    # --- цены ---
    def test_price_multiplier_and_morning_discount(self):
        base, disc, final = S.calc_price("cut", "artem", datetime(2026, 11, 17, 15, 0))
        self.assertEqual((base, disc, final), (1950, 0, 1950))
        base, disc, final = S.calc_price("cut", "ildar", datetime(2026, 11, 17, 10, 0))
        self.assertEqual((base, disc), (1500, 10))
        self.assertEqual(final, 1350)
        _, disc_sat, _ = S.calc_price("cut", "ildar", datetime(2026, 11, 21, 10, 0))
        self.assertEqual(disc_sat, 0)  # суббота: скидки нет

    def test_price_range(self):
        low, high = S.price_range("cut")
        self.assertEqual((low, high), (1500, 1950))

    def test_done_only_once(self):
        b = self.book()
        self.assertIsNotNone(S.mark_done(b["id"]))
        self.assertIsNone(S.mark_done(b["id"]))

    # --- отмена ---
    def test_cancel_rules(self):
        b = self.book(start="2026-11-17 15:00")
        ok, why = S.cancel_booking(b["id"], uid=2, now=NOW)
        self.assertEqual((ok, why), (False, "not_found"))  # чужая запись
        late = datetime(2026, 11, 17, 14, 0)  # за час до визита
        self.assertEqual(S.cancel_booking(b["id"], uid=1, now=late), (False, "too_late"))
        self.assertEqual(S.cancel_booking(b["id"], uid=1, now=NOW), (True, "ok"))
        self.assertEqual(S.cancel_booking(b["id"], uid=1, now=NOW), (False, "not_active"))
        self.assertIn("15:00", S.free_slots("mark", TUE, 60, NOW))  # слот снова свободен

    def test_admin_force_cancel(self):
        b = self.book()
        late = datetime(2026, 11, 17, 14, 50)
        self.assertEqual(S.cancel_booking(b["id"], force=True, now=late), (True, "ok"))

    # --- напоминания ---
    def test_reminders(self):
        b = self.book(start="2026-11-20 15:00")  # создана 16.11 09:00 -> «за сутки» уместно
        self.assertEqual(S.due_reminders(datetime(2026, 11, 19, 10, 0)), [])
        due = S.due_reminders(datetime(2026, 11, 19, 16, 0))
        self.assertEqual([(x["id"], k) for x, k in due], [(b["id"], "24h")])
        S.mark_reminded(b["id"], "24h")
        self.assertEqual(S.due_reminders(datetime(2026, 11, 19, 16, 0)), [])
        due2 = S.due_reminders(datetime(2026, 11, 20, 13, 30))
        self.assertEqual([k for _, k in due2], ["2h"])
        S.mark_reminded(b["id"], "2h")
        self.assertEqual(S.due_reminders(datetime(2026, 11, 20, 14, 0)), [])

    def test_no_24h_reminder_for_late_booking(self):
        self.book(start="2026-11-17 10:00")  # создана 16.11 09:00, до визита 25 ч (< 26 ч)
        self.assertEqual(S.due_reminders(datetime(2026, 11, 16, 11, 0)), [])
        # а «за 2 часа» всё равно придёт
        due = S.due_reminders(datetime(2026, 11, 17, 8, 30))
        self.assertEqual([k for _, k in due], ["2h"])

    # --- отзывы ---
    def test_reviews(self):
        b = self.book()
        S.mark_done(b["id"])
        self.assertTrue(S.add_review(b["id"], 1, "mark", 5, "Отлично", "Тест"))
        self.assertFalse(S.add_review(b["id"], 1, "mark", 1, "повтор", "Тест"))
        b2 = self.book(start="2026-11-17 18:00")
        S.mark_done(b2["id"])
        S.add_review(b2["id"], 1, "mark", 4, "", "Тест")
        avg, n = S.barber_rating("mark")
        self.assertEqual((avg, n), (4.5, 2))
        self.assertEqual(S.barber_rating("artem"), (0.0, 0))
        self.assertEqual(len(S.last_reviews("mark")), 2)

    def test_reviewable(self):
        b = self.book()
        self.assertEqual(S.reviewable(1), [])  # визит ещё не состоялся
        S.mark_done(b["id"])
        self.assertEqual([x["id"] for x in S.reviewable(1)], [b["id"]])
        S.add_review(b["id"], 1, "mark", 5, "", "Тест")
        self.assertEqual(S.reviewable(1), [])

    def test_seed_reviews(self):
        S.init_db(":memory:", seed=True)
        for key in C.BARBERS:
            avg, n = S.barber_rating(key)
            self.assertGreater(n, 0)
            self.assertGreaterEqual(avg, 4)

    # --- статистика ---
    def test_stats(self):
        b = self.book(start="2026-11-17 15:00")
        S.mark_done(b["id"])
        c = self.book(start="2026-11-17 17:00")
        S.cancel_booking(c["id"], force=True)
        st = S.stats(30, now=datetime(2026, 11, 30, 12, 0))
        self.assertEqual(st["done"], 1)
        self.assertEqual(st["cancelled"], 1)
        self.assertEqual(st["revenue"], b["price"])
        self.assertEqual(st["top_services"][0]["service"], "cut")


if __name__ == "__main__":
    unittest.main()
