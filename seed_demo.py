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

# Базовая цена услуги вычисляется детерминированно из её названия и категории —
# так каждая услуга справочника получает реалистичную цену, и демо покрывает
# ВСЕ позиции справочника (чтобы число услуг с ценами совпадало со справочником).
import hashlib

PRICE_RANGES = {
    "лаборатория": (1500, 12000),
    "приём врача": (6000, 12000),
    "диагностика": (4000, 16000),
    "процедура":   (500, 3000),
}

def base_price(name, category):
    lo, hi = PRICE_RANGES.get(category, (1500, 9000))
    h = int(hashlib.sha1(name.encode("utf-8")).hexdigest(), 16)
    return int(round((lo + h % (hi - lo)) / 50) * 50)

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
    all_clinics = list(CLINICS.keys())

    def add_offer(clinic, sid, canonical, category, base):
        nonlocal inserted, skipped
        factor = random.uniform(0.65, 1.35)
        price = int(round(base * factor / 50) * 50)
        if random.random() < 0.08:  # часть записей намеренно «устаревшие» (>30 дн)
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
            return True
        except sqlite3.IntegrityError:
            skipped += 1
            return False

    # Покрываем ВСЕ услуги справочника, чтобы каталог был полным.
    for canonical, (sid, category) in dict_map.items():
        base = base_price(canonical, category)
        chosen = [c for c in all_clinics if random.random() >= 0.45]
        # гарантируем минимум 3 клиники на услугу (для сравнения цен)
        while len(chosen) < 3:
            c = random.choice(all_clinics)
            if c not in chosen:
                chosen.append(c)
        for clinic in chosen:
            add_offer(clinic, sid, canonical, category, base)

    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    cities = conn.execute("SELECT COUNT(DISTINCT city) FROM listings").fetchone()[0]
    clinics = conn.execute("SELECT COUNT(DISTINCT clinic_name) FROM listings").fetchone()[0]
    services = conn.execute("SELECT COUNT(DISTINCT service_id) FROM listings").fetchone()[0]
    conn.close()

    print("=== seed_demo.py завершён ===")
    print(f"  вставлено: {inserted}, пропущено (дубли): {skipped}")
    print(f"  listings: {total} | клиник: {clinics} | городов: {cities} | услуг с ценами: {services}")


if __name__ == "__main__":
    main()
