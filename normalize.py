"""
normalize.py — Дорожка 1, шаг 2: превращает сырые raw_listings в чистые listings.

Запуск:  python normalize.py   (сразу после parse.py)

Что делает (строго по плану, раздел 2-3 и Дорожка 1):
  1. Заполняет service_dictionary из service_dictionary.json (≥50 позиций — ТЗ п.6).
  2. Читает raw_listings, чистит цену ("1 880 ₸" -> 1880).
  3. Fuzzy-match service_name_raw -> справочник (rapidfuzz, порог ~80%).
  4. Сматченное пишет в listings с дедупликацией по price_hash
     (clinic_name, service_id, price_kzt) — повторный запуск не плодит дубли.
  5. Несматченное складывает в unmatched.csv (без UI — как договорились).

ПРИМЕЧАНИЕ ПО КОМАНДЕ: это файл Дорожки 1 (парсинг/нормализация). Он написан
совместимым с контрактом схемы из плана, чтобы backend (/admin/trigger-parse)
работал end-to-end. Анна может смержить со своей версией справочника синонимов —
формат таблиц менять не нужно.
"""

import os
import re
import csv
import json
import hashlib
import sqlite3
from datetime import datetime

# rapidfuzz предпочтительнее, но если не установлен — fallback на difflib
try:
    from rapidfuzz import fuzz

    def similarity(a, b):
        return fuzz.token_sort_ratio(a, b)
except ImportError:  # pragma: no cover
    from difflib import SequenceMatcher

    def similarity(a, b):
        return SequenceMatcher(None, a, b).ratio() * 100


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "medprice.db")
DICT_PATH = os.path.join(BASE_DIR, "service_dictionary.json")
UNMATCHED_PATH = os.path.join(BASE_DIR, "unmatched.csv")

MATCH_THRESHOLD = 80  # порог уверенности fuzzy-match, %


def _clean(text):
    """Нормализуем строку для сравнения: нижний регистр, без лишних символов."""
    if not text:
        return ""
    text = text.lower().replace("ё", "е")
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def parse_price(price_raw):
    """'1 880 ₸', 'от 8 980 тенге' -> 1880 / 8980. Если числа нет -> None."""
    if price_raw is None:
        return None
    digits = re.sub(r"[^\d]", "", str(price_raw))
    return int(digits) if digits else None


def price_hash(clinic_name, service_id, price_kzt):
    key = f"{(clinic_name or '').strip().lower()}|{service_id}|{price_kzt}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def ensure_schema(conn):
    """Создаём таблицы, если normalize запускается раньше backend."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS service_dictionary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_name TEXT UNIQUE NOT NULL,
            synonyms TEXT,
            category TEXT
        );
        CREATE TABLE IF NOT EXISTS listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            clinic_name TEXT NOT NULL,
            city TEXT,
            address TEXT,
            phone TEXT,
            working_hours TEXT,
            service_id INTEGER,
            service_name_raw TEXT,
            category TEXT,
            price_kzt INTEGER,
            source_url TEXT,
            parsed_at TEXT,
            price_hash TEXT UNIQUE,
            FOREIGN KEY (service_id) REFERENCES service_dictionary(id)
        );
        """
    )
    conn.commit()


def seed_dictionary(conn):
    """Заполняем справочник из JSON. INSERT OR IGNORE — идемпотентно."""
    if not os.path.exists(DICT_PATH):
        print(f"[normalize] нет {DICT_PATH} — справочник не заполнен")
        return 0
    with open(DICT_PATH, encoding="utf-8") as f:
        items = json.load(f)
    n = 0
    for it in items:
        conn.execute(
            "INSERT OR IGNORE INTO service_dictionary (canonical_name, synonyms, category) VALUES (?, ?, ?)",
            (it["canonical_name"], json.dumps(it.get("synonyms", []), ensure_ascii=False), it.get("category")),
        )
        n += 1
    conn.commit()
    return n


def load_index(conn):
    """Готовим список (id, canonical, category, [варианты для сравнения])."""
    index = []
    for row in conn.execute("SELECT id, canonical_name, synonyms, category FROM service_dictionary"):
        variants = [row["canonical_name"]]
        try:
            variants += json.loads(row["synonyms"] or "[]")
        except (json.JSONDecodeError, TypeError):
            variants += [s.strip() for s in (row["synonyms"] or "").split(",") if s.strip()]
        cleaned = [_clean(v) for v in variants if v]
        index.append((row["id"], row["canonical_name"], row["category"], cleaned))
    return index


def match_service(service_name_raw, index):
    """Возвращает (service_id, category, score) лучшего совпадения или (None, None, best)."""
    target = _clean(service_name_raw)
    if not target:
        return None, None, 0
    best_id, best_cat, best_score = None, None, 0
    for sid, _canon, cat, variants in index:
        for v in variants:
            score = similarity(target, v)
            if score > best_score:
                best_id, best_cat, best_score = sid, cat, score
    if best_score >= MATCH_THRESHOLD:
        return best_id, best_cat, best_score
    return None, None, best_score


def run():
    """Полный прогон нормализации. Возвращает dict со статистикой."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    seed_dictionary(conn)
    index = load_index(conn)

    raw_rows = conn.execute(
        "SELECT * FROM raw_listings WHERE parse_error IS NULL AND price_raw IS NOT NULL"
    ).fetchall()

    inserted, duplicates, unmatched = 0, 0, []

    for r in raw_rows:
        price = parse_price(r["price_raw"])
        if price is None:
            continue
        sid, cat, score = match_service(r["service_name_raw"], index)
        if sid is None:
            unmatched.append({
                "source": r["source"],
                "clinic_name_raw": r["clinic_name_raw"],
                "service_name_raw": r["service_name_raw"],
                "price_raw": r["price_raw"],
                "best_score": round(score, 1),
            })
            continue

        clinic = r["clinic_name_raw"]
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
                (clinic, r["city"], None, None, None, sid, r["service_name_raw"],
                 cat, price, r["source_url"], r["parsed_at"] or datetime.now().isoformat(), h),
            )
            inserted += 1
        except sqlite3.IntegrityError:
            duplicates += 1  # дубль по price_hash — пропускаем (дедупликация)

    conn.commit()

    # несматченные -> файл, без UI (как в плане)
    if unmatched:
        with open(UNMATCHED_PATH, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(unmatched[0].keys()))
            w.writeheader()
            w.writerows(unmatched)

    stats = {
        "raw_rows": len(raw_rows),
        "inserted": inserted,
        "duplicates_skipped": duplicates,
        "unmatched": len(unmatched),
        "dictionary_size": len(index),
    }
    conn.close()
    return stats


def main():
    stats = run()
    print("=== normalize.py завершён ===")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    if stats["unmatched"]:
        print(f"  несматченные услуги -> {UNMATCHED_PATH}")


if __name__ == "__main__":
    main()
