"""
main.py — FastAPI backend для MedServicePrice.kz (Дорожка 2).

Запуск (из КОРНЯ проекта MedServicePrice):
    uvicorn backend.main:app --reload

Контракт API (зафиксирован в плане, раздел 4.2 — не менять без согласования):
    GET  /search                 — поиск+фильтры+сортировка, список предложений
    GET  /services/autocomplete  — автодополнение по справочнику услуг
    POST /admin/trigger-parse    — ручной запуск парсинга «через интерфейс» (ТЗ 3.1)

Дополнительно (удобство фронта, не ломает контракт):
    GET  /cities, /categories, /stats, /clinic/{name}, /health
    /                            — отдаёт собранный фронтенд (frontend/index.html)
"""

import os
import sys
import json
import subprocess
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

# делаем корень проекта импортируемым (для parse/normalize в trigger-parse)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from backend.db import get_connection, init_schema  # noqa: E402

FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")
STALE_DAYS = 30  # ТЗ п.4: данные старше 30 дней не считаются актуальными

app = FastAPI(
    title="MedServicePrice.kz API",
    description="Агрегатор цен на медицинские услуги в Казахстане (MVP, Хакатон 2025)",
    version="1.0.0",
)

# CORS — на случай если фронт поднимут отдельно (Vite на :5173)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup():
    init_schema()
    # На бесплатном хостинге диск эфемерный (сбрасывается при рестарте):
    # если база пустая — наполняем демо-данными, чтобы страница не была пустой.
    try:
        conn = get_connection()
        empty = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 0
        conn.close()
        if empty:
            import seed_demo
            seed_demo.main()
    except Exception as e:  # noqa: BLE001
        print(f"[startup] авто-сид пропущен: {e}")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _row_to_offer(row):
    """Преобразует строку listings в объект ответа /search (формат из контракта)."""
    parsed_at = row["parsed_at"]
    is_stale = _is_stale(parsed_at)
    return {
        # --- зафиксированные контрактом поля ---
        "clinic_name": row["clinic_name"],
        "service_name": row["service_name_norm"] if "service_name_norm" in row.keys() else row["service_name_raw"],
        "category": row["category"],
        "price_kzt": row["price_kzt"],
        "city": row["city"],
        "parsed_at": parsed_at,
        "source_url": row["source_url"],
        # --- доп. поля для красивой карточки (фронт использует, контракт не нарушает) ---
        "id": row["id"],
        "address": row["address"],
        "phone": row["phone"],
        "working_hours": row["working_hours"],
        "service_name_raw": row["service_name_raw"],
        "is_stale": is_stale,
        "updated_days_ago": _days_ago(parsed_at),
    }


def _days_ago(iso_str):
    dt = _parse_dt(iso_str)
    if dt is None:
        return None
    return (datetime.now() - dt).days


def _is_stale(iso_str):
    days = _days_ago(iso_str)
    return days is not None and days > STALE_DAYS


def _parse_dt(iso_str):
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------
# GET /search — основной эндпоинт (контракт)
# --------------------------------------------------------------------------
@app.get("/search")
def search(
    q: str = Query("", description="поисковый запрос по названию услуги"),
    city: str = Query("", description="фильтр по городу"),
    category: str = Query("", description="фильтр по категории"),
    price_min: int = Query(0, ge=0),
    price_max: int = Query(0, ge=0, description="0 = без верхней границы"),
    sort: str = Query("price_asc", description="price_asc | price_desc | date_desc"),
    include_stale: bool = Query(False, description="включать записи старше 30 дней"),
    limit: int = Query(300, ge=1, le=2000),
):
    conn = get_connection()

    # service_name_norm берём из справочника (canonical_name), если услуга смэтчена
    sql = """
        SELECT l.*, sd.canonical_name AS service_name_norm
        FROM listings l
        LEFT JOIN service_dictionary sd ON sd.id = l.service_id
        WHERE 1=1
    """
    params = []

    if q:
        sql += " AND (sd.canonical_name LIKE ? OR l.service_name_raw LIKE ? OR sd.synonyms LIKE ?)"
        like = f"%{q}%"
        params += [like, like, like]
    if city:
        sql += " AND l.city = ?"
        params.append(city)
    if category:
        sql += " AND l.category = ?"
        params.append(category)
    if price_min:
        sql += " AND l.price_kzt >= ?"
        params.append(price_min)
    if price_max:
        sql += " AND l.price_kzt <= ?"
        params.append(price_max)

    # фильтр актуальности: по умолчанию прячем записи старше 30 дней (ТЗ п.4)
    if not include_stale:
        cutoff = (datetime.now() - timedelta(days=STALE_DAYS)).isoformat()
        sql += " AND l.parsed_at >= ?"
        params.append(cutoff)

    order = {
        "price_asc": "l.price_kzt ASC",
        "price_desc": "l.price_kzt DESC",
        "date_desc": "l.parsed_at DESC",
    }.get(sort, "l.price_kzt ASC")
    sql += f" ORDER BY {order} LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    conn.close()

    results = [_row_to_offer(r) for r in rows]
    # «лучшая цена» — пометим минимальную в выдаче (полезно для UX, как Aviasales)
    if results:
        cheapest = min(r["price_kzt"] for r in results if r["price_kzt"] is not None)
        for r in results:
            r["is_cheapest"] = (r["price_kzt"] == cheapest)

    return {"count": len(results), "results": results}


# --------------------------------------------------------------------------
# GET /services/autocomplete — автодополнение (контракт)
# --------------------------------------------------------------------------
@app.get("/services/autocomplete")
def autocomplete(q: str = Query("", description="префикс/подстрока названия услуги"),
                 limit: int = Query(10, ge=1, le=50)):
    conn = get_connection()
    if q:
        rows = conn.execute(
            """
            SELECT canonical_name FROM service_dictionary
            WHERE canonical_name LIKE ? OR synonyms LIKE ?
            ORDER BY canonical_name LIMIT ?
            """,
            (f"%{q}%", f"%{q}%", limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT canonical_name FROM service_dictionary ORDER BY canonical_name LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return [r["canonical_name"] for r in rows]


# --------------------------------------------------------------------------
# POST /admin/trigger-parse — ручной запуск пайплайна «через интерфейс» (ТЗ 3.1)
# --------------------------------------------------------------------------
@app.post("/admin/trigger-parse")
def trigger_parse():
    """
    Запускает parse.py -> normalize.py как подпроцессы (изоляция: падение
    парсера не роняет API). Возвращает {status, new_records, errors}
    в точности по зафиксированной сигнатуре контракта.

    В README показано, как тот же вызов превращается в cron:
        0 3 * * * curl -X POST http://localhost:8000/admin/trigger-parse
    """
    conn = get_connection()
    before = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    conn.close()

    errors = []

    def _run(script):
        try:
            proc = subprocess.run(
                [sys.executable, script],
                cwd=BASE_DIR, capture_output=True, text=True, timeout=600,
            )
            if proc.returncode != 0:
                errors.append(f"{script}: {(proc.stderr or proc.stdout)[-500:]}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{script}: {e}")

    _run("parse.py")
    _run("normalize.py")

    conn = get_connection()
    after = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    conn.close()

    # читаем лог парсинга, чтобы показать ошибки прямо в ответе (демо)
    log_path = os.path.join(BASE_DIR, "parse_errors.log")
    if os.path.exists(log_path):
        with open(log_path, encoding="utf-8", errors="ignore") as f:
            tail = [ln.strip() for ln in f.readlines()[-15:] if "ERROR" in ln or "WARNING" in ln]
        errors += tail

    return {
        "status": "ok" if not errors else "completed_with_errors",
        "new_records": after - before,
        "errors": errors,
    }


# --------------------------------------------------------------------------
# Вспомогательные эндпоинты для фронта
# --------------------------------------------------------------------------
@app.get("/cities")
def cities():
    conn = get_connection()
    rows = conn.execute(
        "SELECT DISTINCT city FROM listings WHERE city IS NOT NULL AND city != '' ORDER BY city"
    ).fetchall()
    conn.close()
    return [r["city"] for r in rows]


@app.get("/categories")
def categories():
    conn = get_connection()
    rows = conn.execute(
        "SELECT DISTINCT category FROM listings WHERE category IS NOT NULL AND category != '' ORDER BY category"
    ).fetchall()
    conn.close()
    return [r["category"] for r in rows]


@app.get("/clinic/{clinic_name}")
def clinic_card(clinic_name: str):
    """Карточка клиники: все услуги данной клиники (ТЗ п.3.3)."""
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT l.*, sd.canonical_name AS service_name_norm
        FROM listings l LEFT JOIN service_dictionary sd ON sd.id = l.service_id
        WHERE l.clinic_name = ? ORDER BY l.category, sd.canonical_name
        """,
        (clinic_name,),
    ).fetchall()
    conn.close()
    if not rows:
        return JSONResponse(status_code=404, content={"detail": "клиника не найдена"})
    first = rows[0]
    return {
        "clinic_name": first["clinic_name"],
        "city": first["city"],
        "address": first["address"],
        "phone": first["phone"],
        "working_hours": first["working_hours"],
        "source_url": first["source_url"],
        "services": [_row_to_offer(r) for r in rows],
    }


@app.get("/stats")
def stats():
    """Сводка для шапки интерфейса и для демо качества данных."""
    conn = get_connection()
    g = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    cutoff = (datetime.now() - timedelta(days=STALE_DAYS)).isoformat()
    data = {
        "clinics": g("SELECT COUNT(DISTINCT clinic_name) FROM listings"),
        "cities": g("SELECT COUNT(DISTINCT city) FROM listings"),
        "services_in_dictionary": g("SELECT COUNT(*) FROM service_dictionary"),
        "total_offers": g("SELECT COUNT(*) FROM listings"),
        "fresh_offers": conn.execute(
            "SELECT COUNT(*) FROM listings WHERE parsed_at >= ?", (cutoff,)
        ).fetchone()[0],
        "sources": g("SELECT COUNT(DISTINCT source) FROM raw_listings"),
        "last_update": g("SELECT MAX(parsed_at) FROM listings"),
    }
    conn.close()
    return data


@app.get("/health")
def health():
    return {"status": "ok", "time": datetime.now().isoformat()}


# --------------------------------------------------------------------------
# Статика фронтенда (монтируется ПОСЛЕ всех API-роутов, чтобы их не перекрыть)
# --------------------------------------------------------------------------
if os.path.isdir(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
