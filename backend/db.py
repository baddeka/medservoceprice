"""
db.py — единая точка доступа к SQLite для всего backend.

Путь к базе вычисляется относительно КОРНЯ проекта (на уровень выше backend/),
поэтому backend, parse.py, normalize.py и seed_demo.py работают с ОДНИМ
файлом medprice.db независимо от того, из какой папки запущен процесс.
"""

import os
import sqlite3

# .../MedServicePrice/backend/db.py  ->  BASE_DIR = .../MedServicePrice
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "medprice.db")


def get_connection():
    """Соединение с включённым доступом к колонкам по имени (row_factory)."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # включаем поддержку внешних ключей (на будущее)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema():
    """
    Создаёт все таблицы, если их ещё нет. Схема зафиксирована в плане (раздел 2)
    и согласована между Дорожкой 1 (парсинг/нормализация) и Дорожкой 2 (API).
    Вызывается при старте API — чтобы /search не падал на пустой базе.
    """
    conn = get_connection()
    cur = conn.cursor()

    # --- raw-слой: сырые данные как пришли с сайтов (создаёт также parse.py) ---
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS raw_listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT,
            clinic_name_raw TEXT,
            service_name_raw TEXT,
            price_raw TEXT,
            city TEXT,
            source_url TEXT,
            parsed_at TEXT,
            parse_error TEXT
        )
        """
    )

    # --- справочник услуг (минимум 50 позиций по ТЗ п.6) ---
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS service_dictionary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_name TEXT UNIQUE NOT NULL,
            synonyms TEXT,            -- JSON-список синонимов
            category TEXT,
            description TEXT,         -- что это / что показывает услуга
            preparation TEXT,         -- как подготовиться
            biomaterial TEXT,         -- биоматериал (для анализов)
            turnaround TEXT           -- срок выполнения
        )
        """
    )

    # --- нормализованный слой: то, что отдаётся пользователю ---
    cur.execute(
        """
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
            price_hash TEXT UNIQUE,   -- дедупликация (ТЗ п.3.1)
            FOREIGN KEY (service_id) REFERENCES service_dictionary(id)
        )
        """
    )

    # индексы под типовые фильтры/сортировки из /search
    cur.execute("CREATE INDEX IF NOT EXISTS idx_listings_service ON listings(service_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_listings_city ON listings(city)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_listings_category ON listings(category)")

    conn.commit()
    conn.close()
