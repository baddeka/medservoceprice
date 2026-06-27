"""
seed_demo.py — наполняет базу реалистичными демо-данными.

Зачем: чтобы фронт/бэк работали СРАЗУ, не дожидаясь живого парсинга (Дорожка 2
по плану стартует на фикстурах). Также демонстрирует функции, которые иначе
не видно на маленькой базе:
  • сравнение цен одной услуги между клиниками в одном городе (суть агрегатора);
  • метку «устарело» для записей старше 30 дней (ТЗ п.4, нефункц. требования).

Запуск:  python seed_demo.py
Идемпотентно: повторный запуск не плодит дубли (дедуп по price_hash).

ВАЖНО: это демонстрационные данные для разработки/демо. Реальные спарсенные
данные добавляются поверх через parse.py -> normalize.py (или кнопкой
/admin/trigger-parse). price_hash гарантирует, что демо и реальные данные
не задвоятся.
"""

import os
import random
import sqlite3
from datetime import datetime, timedelta

from normalize import ensure_schema, seed_dictionary, price_hash, DB_PATH

random.seed(42)

# Клиники по городам: name -> (city, address, phone, working_hours)
CLINICS = {
    "КДЛ Олимп":        ("Алматы",  "ул. Кунаева, 21",            "+7 727 311 11 11", "Пн-Сб 07:00-19:00"),
    "Инвитро":          ("Алматы",  "пр. Абая, 150",              "+7 727 322 22 22", "Пн-Вс 08:00-20:00"),
    "Олимп":            ("Алматы",  "ул. Толе би, 187",           "+7 727 333 33 33", "Пн-Сб 08:00-18:00"),
    "Хеликс":           ("Алматы",  "мкр. Самал-2, 58",           "+7 727 344 44 44", "Пн-Вс 07:30-19:00"),
    "МЕДЭЛ":            ("Астана",  "пр. Кабанбай батыра, 40",    "+7 717 255 55 55", "Пн-Сб 08:00-20:00"),
    "Green Clinic":     ("Астана",  "ул. Сыганак, 18",            "+7 717 266 66 66", "Пн-Вс 09:00-21:00"),
    "КДЛ Олимп Астана": ("Астана",  "пр. Туран, 55",              "+7 717 277 77 77", "Пн-Сб 07:00-19:00"),
    "Инвитро Астана":   ("Астана",  "ул. Достык, 13",             "+7 717 288 88 88", "Пн-Вс 08:00-20:00"),
    "Alatau Lab":       ("Шымкент", "ул. Тауке хана, 21",         "+7 725 299 99 99", "Пн-Сб 08:00-18:00"),
    "МЦК":              ("Шымкент", "пр. Республики, 7",          "+7 725 210 10 10", "Пн-Вс 08:00-19:00"),
    "Инвитро Шымкент":  ("Шымкент", "ул. Байтурсынова, 44",       "+7 725 211 11 12", "Пн-Сб 08:00-20:00"),
}

# (canonical_name, базовая цена) — для каждой услуги генерим разброс по клиникам города
SERVICES = [
    ("Общий анализ крови (ОАК)", 2500),
    ("Общий анализ мочи (ОАМ)", 1800),
    ("Биохимический анализ крови", 9000),
    ("Глюкоза крови", 1500),
    ("Гликированный гемоглобин (HbA1c)", 4500),
    ("Холестерин общий", 1700),
    ("Липидный профиль (липидограмма)", 6500),
    ("ТТГ (тиреотропный гормон)", 3200),
    ("Витамин D (25-OH)", 9500),
    ("Ферритин", 4200),
    ("С-реактивный белок (СРБ)", 3000),
    ("Коагулограмма", 5500),
    ("ПСА общий (простатспецифический антиген)", 5000),
    ("Гепатит B (HBsAg)", 2800),
    ("Гепатит C (anti-HCV)", 3100),
    ("УЗИ органов брюшной полости", 8000),
    ("УЗИ щитовидной железы", 6000),
    ("ЭКГ (электрокардиография)", 3500),
    ("Приём терапевта", 7000),
    ("Приём кардиолога", 9000),
    ("Приём гинеколога", 8500),
    ("Приём эндокринолога", 9000),
    ("Забор крови из вены", 800),
]

CITY_CLINICS = {}
for name, (city, *_rest) in CLINICS.items():
    CITY_CLINICS.setdefault(city, []).append(name)


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    seed_dictionary(conn)

    # сопоставление canonical_name -> (id, category)
    dict_map = {
        r["canonical_name"]: (r["id"], r["category"])
        for r in conn.execute("SELECT id, canonical_name, category FROM service_dictionary")
    }

    now = datetime.now()
    inserted, skipped = 0, 0

    for canonical, base_price in SERVICES:
        if canonical not in dict_map:
            continue
        sid, category = dict_map[canonical]
        for city, clinic_names in CITY_CLINICS.items():
            # не каждая клиника предлагает каждую услугу — реалистичный разброс
            for clinic in clinic_names:
                if random.random() < 0.25:
                    continue
                # цена ±35% от базовой, округлённая до 50 тг
                factor = random.uniform(0.65, 1.35)
                price = int(round(base_price * factor / 50) * 50)

                # большинство записей свежие; пара — намеренно старше 30 дней,
                # чтобы показать метку «устарело»
                if random.random() < 0.08:
                    parsed_at = (now - timedelta(days=random.randint(35, 70))).isoformat()
                else:
                    parsed_at = (now - timedelta(days=random.randint(0, 6))).isoformat()

                city_c, address, phone, hours = CLINICS[clinic]
                h = price_hash(clinic, sid, price)
                try:
                    conn.execute(
                        """
                        INSERT INTO listings
                            (clinic_name, city, address, phone, working_hours,
                             service_id, service_name_raw, category, price_kzt,
                             source_url, parsed_at, price_hash)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (clinic, city_c, address, phone, hours, sid, canonical,
                         category, price, "https://example.kz/price", parsed_at, h),
                    )
                    inserted += 1
                except sqlite3.IntegrityError:
                    skipped += 1

    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    cities = conn.execute("SELECT COUNT(DISTINCT city) FROM listings").fetchone()[0]
    clinics = conn.execute("SELECT COUNT(DISTINCT clinic_name) FROM listings").fetchone()[0]
    conn.close()

    print("=== seed_demo.py завершён ===")
    print(f"  вставлено: {inserted}, пропущено (дубли): {skipped}")
    print(f"  всего в listings: {total} | клиник: {clinics} | городов: {cities}")


if __name__ == "__main__":
    main()
