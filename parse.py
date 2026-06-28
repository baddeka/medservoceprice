"""
parse.py — сбор сырых данных (raw_listings) с источников.

Запуск: python parse.py
Результат: данные пишутся в medprice.db (таблица raw_listings),
ошибки — в parse_errors.log

КАК ДОБАВИТЬ НОВЫЙ ИСТОЧНИК:
1. Скопируй функцию parse_source_template(...)
2. Переименуй её, например parse_kdl()
3. Поменяй URL и правила извлечения (см. комментарии внутри)
4. Добавь вызов новой функции в список SOURCES в самом низу файла
Каждый источник — отдельная функция. Если один сломается — это не должно
останавливать остальные (отказоустойчивость, см. ТЗ п.4).
"""

import sqlite3
import time
import re
import logging
from datetime import datetime

import requests
from bs4 import BeautifulSoup

DB_PATH = "medprice.db"
LOG_PATH = "parse_errors.log"
DELAY_SECONDS = 1.5  # задержка между запросами — не нагружать источник (ТЗ п.8)

# Притворяемся обычным браузером, иначе многие сайты блокируют запрос
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36"
}

logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)


def init_db():
    """Создаёт таблицу raw_listings, если её ещё нет."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
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
    conn.commit()
    return conn


def save_raw_row(conn, source, clinic_name_raw, service_name_raw,
                  price_raw, city, source_url, parse_error=None):
    """Сохраняет одну строку в raw_listings."""
    conn.execute(
        """
        INSERT INTO raw_listings
        (source, clinic_name_raw, service_name_raw, price_raw, city,
         source_url, parsed_at, parse_error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source, clinic_name_raw, service_name_raw, price_raw, city,
            source_url, datetime.now().isoformat(), parse_error,
        ),
    )
    conn.commit()


def fetch_html(url):
    """Скачивает страницу и возвращает BeautifulSoup-объект."""
    response = requests.get(url, headers=HEADERS, timeout=15)
    response.raise_for_status()  # выбросит ошибку, если страница недоступна (404, 500 и т.п.)
    return BeautifulSoup(response.text, "html.parser")


# Источник 1: kdlolymp.kz (КДЛ Олимп). Весь прайс лежит прямо в HTML, без пагинации.
# Каждая услуга - <a class="analysis"> с div.title и div.price внутри.
# Берём города, которые есть и у других источников (Алматы, Шымкент), чтобы
# в одном городе было несколько клиник для сравнения цен.
def parse_kdlolymp(conn):
    source_name = "kdlolymp"

    city_urls = {
        "Алматы": "https://www.kdlolymp.kz/pricelist/almaty",
        "Шымкент": "https://www.kdlolymp.kz/pricelist/shymkent",
        # "Астана": "https://www.kdlolymp.kz/pricelist/astana",  # резерв, если останется время
    }

    for city, list_url in city_urls.items():
        try:
            soup = fetch_html(list_url)

            # каждая услуга - <a class="analysis">
            rows = soup.select("a.analysis")

            if not rows:
                logging.warning(
                    f"{source_name} ({city}): 0 строк по селектору 'a.analysis' "
                    f"- возможно сайт обновился, проверь через F12"
                )
                continue

            count_for_city = 0
            for row in rows:
                try:
                    title_tag = row.select_one("div.title")
                    price_tag = row.select_one("div.price")

                    if not title_tag or not price_tag:
                        continue  # неполная карточка - пропускаем, не падаем

                    service_name_raw = title_tag.get_text(strip=True)
                    price_text = price_tag.get_text(strip=True)

                    # цена приходит как "1 880 ₸ " - вытаскиваем число
                    price_match = re.search(r"(\d[\d\s]*)", price_text)
                    if not price_match:
                        continue
                    price_raw = price_match.group(1)

                    save_raw_row(
                        conn,
                        source=source_name,
                        clinic_name_raw="КДЛ Олимп",
                        service_name_raw=service_name_raw,
                        price_raw=price_raw,
                        city=city,
                        source_url=list_url,
                    )
                    count_for_city += 1
                except Exception as row_error:
                    # одна сломанная строка не должна рушить весь прогон
                    logging.error(f"{source_name} ({city}): ошибка в строке - {row_error}")
                    continue

            logging.info(f"{source_name} ({city}): обработано {count_for_city} строк")

        except Exception as source_error:
            # сайт недоступен / robots поменялся / упал интернет - не валим весь скрипт
            logging.error(f"{source_name} ({city}): источник недоступен - {source_error}")
            save_raw_row(
                conn, source=source_name, clinic_name_raw=None,
                service_name_raw=None, price_raw=None, city=city,
                source_url=list_url, parse_error=str(source_error),
            )

        # задержка МЕЖДУ ГОРОДАМИ (то есть между запросами к этому источнику)
        time.sleep(DELAY_SECONDS)


# Источник 2: 103.kz - каталог клиник по всему Казахстану.
# Один запрос на /list/analizy/kazakhstan/ возвращает клиники из разных городов.
# Структура: клиника - div.PlaceList__itemWrapper (название в a.Place__headerLink,
# город из span.Place__addressText до первой запятой). Услуги внутри -
# div.PlacePrices__itemWrapper (название + цена). Строки без цены ("уточняйте")
# пропускаем.
def parse_103kz_catalog(conn):
    source_name = "103kz"

    # Один запрос на ВСЮ страну - даёт сразу несколько городов.
    # На странице есть пагинация ("Показать ещё 25"), но для хакатона
    # не гонимся за ней - первой загрузки достаточно (десятки клиник
    # из разных городов сразу).
    list_url = "https://www.103.kz/list/analizy/kazakhstan/"

    try:
        soup = fetch_html(list_url)

        # Каждая карточка клиники - это PlaceList__itemWrapper.
        # берём именно этот уровень, а не div.Place напрямую, чтобы не
        # зацепить услуги соседней клиники
        clinic_wrappers = soup.select("div.PlaceList__itemWrapper")

        if not clinic_wrappers:
            logging.warning(
                f"{source_name}: 0 клиник найдено - проверь "
                f"структуру через F12, возможно сайт обновился"
            )
        else:
            count_clinics = 0
            count_services = 0
            count_skipped_no_price = 0

            for wrapper in clinic_wrappers:
                clinic_div = wrapper.select_one("div.Place")
                if not clinic_div:
                    continue  # это промо-блок (реклама), не клиника - пропускаем

                name_tag = clinic_div.select_one("a.Place__headerLink")
                clinic_name = name_tag.get_text(strip=True) if name_tag else None
                if not clinic_name:
                    continue

                # Город вытаскиваем из адреса - он всегда идёт первым
                # словом до запятой, например "Алматы, ул. Кенесары хана, 54/17"
                address_tag = clinic_div.select_one("span.Place__addressText")
                address = address_tag.get_text(strip=True) if address_tag else ""
                city = address.split(",")[0].strip() if address else "не указан"

                count_clinics += 1

                # услуги именно этой клиники (внутри clinic_div, не всей страницы)
                service_items = clinic_div.select("div.PlacePrices__itemWrapper")
                for item in service_items:
                    try:
                        title_tag = item.select_one("span.PlacePrices__itemTitle")
                        price_tag = item.select_one("span.PlacePrices__itemText")
                        # у кнопки "Все цены" нет этих тегов - пропускаем её
                        if not title_tag or not price_tag:
                            continue

                        service_name_raw = title_tag.get_text(strip=True)
                        price_text = price_tag.get_text(strip=True)

                        # цена может быть словом "уточняйте" без цифр - пропускаем
                        price_match = re.search(r"(\d[\d\s]*)", price_text)
                        if not price_match:
                            count_skipped_no_price += 1
                            continue
                        price_raw = price_match.group(1)

                        save_raw_row(
                            conn,
                            source=source_name,
                            clinic_name_raw=clinic_name,
                            service_name_raw=service_name_raw,
                            price_raw=price_raw,
                            city=city,
                            source_url=list_url,
                        )
                        count_services += 1
                    except Exception as row_error:
                        logging.error(f"{source_name} ({clinic_name}): ошибка в строке - {row_error}")
                        continue

            logging.info(
                f"{source_name}: обработано {count_clinics} клиник, "
                f"{count_services} строк услуг, "
                f"{count_skipped_no_price} пропущено без цены (уточняйте)"
            )

    except Exception as source_error:
        logging.error(f"{source_name}: источник недоступен - {source_error}")
        save_raw_row(
            conn, source=source_name, clinic_name_raw=None,
            service_name_raw=None, price_raw=None, city=None,
            source_url=list_url, parse_error=str(source_error),
        )

    # перестраховка - не обязательно по robots.txt (там нет Crawl-Delay
    # для www.103.kz), но вежливая пауза, раз это инфраструктура той же
    # компании что и поддомены клиник с подтверждённым Crawl-Delay: 60
    time.sleep(5)


# Источник 2Б (запасной): страница одной клиники на 103.kz - даёт полный
# список её услуг, а не только превью из каталога. У поддомена
# green-clinic.103.kz в robots.txt стоит Crawl-Delay 60, поэтому пауза большая.
DOMPRICE_DELAY_SECONDS = 60  # обязательно соблюдать! сайт сам просит в robots.txt


def parse_103kz_single_clinic(conn):
    source_name = "103kz"

    clinic_pages = {
        "Green Clinic": {
            "city": "Астана",
            "url": "https://green-clinic.103.kz/pricing/2721/103829/",
        },
        # добавляй другие клиники по тому же шаблону, но помни про 60 сек
        # задержки на каждую - не добавляй больше 2-3 если время на исходе
    }

    for clinic_name, info in clinic_pages.items():
        list_url = info["url"]
        city = info["city"]

        try:
            soup = fetch_html(list_url)

            # если перед ценой идёт строка-описание метода, она может попасть
            # в название вместо настоящего - такие строки потом не пройдут
            # нормализацию и уйдут в unmatched.csv
            lines = list(soup.stripped_strings)

            count_for_clinic = 0
            i = 0
            while i < len(lines):
                line = lines[i]
                price_match = re.search(r"([\d\s]+)\s*тенге", line)
                if price_match:
                    price_raw = price_match.group(1)
                    service_name_raw = lines[i - 1] if i > 0 else None
                    if service_name_raw:
                        save_raw_row(
                            conn,
                            source=source_name,
                            clinic_name_raw=clinic_name,
                            service_name_raw=service_name_raw,
                            price_raw=price_raw,
                            city=city,
                            source_url=list_url,
                        )
                        count_for_clinic += 1
                i += 1

            logging.info(f"{source_name} ({clinic_name}): обработано {count_for_clinic} строк")

        except Exception as source_error:
            logging.error(f"{source_name} ({clinic_name}): источник недоступен - {source_error}")
            save_raw_row(
                conn, source=source_name, clinic_name_raw=clinic_name,
                service_name_raw=None, price_raw=None, city=city,
                source_url=list_url, parse_error=str(source_error),
            )

        time.sleep(DOMPRICE_DELAY_SECONDS)


# Источник 3: invitro.kz. На странице категории
# /analizes/for-doctors/<город>/<id_категории>/ все услуги с ценами уже в HTML:
# каждая - div.analyzes-list[data-item-id], название и цена в атрибутах
# data-product-name / data-product-price у ссылки внутри.
# Пагинацию ("Показать ещё", грузится через AJAX) не трогаем - берём первую страницу.
def parse_invitro(conn):
    source_name = "invitro"

    # Берём 2-3 категории для одного города - этого хватит чтобы
    # набрать 30-50+ услуг с одного запуска. Можно добавить город
    # (просто поменять "shymkent" на "almaty"/"astana" и т.п. в URL).
    category_urls = {
        "Гематологические исследования": "https://invitro.kz/analizes/for-doctors/shymkent/137/",
        # добавляй сюда другие категории по такому же шаблону:
        # "Биохимические исследования": "https://invitro.kz/analizes/for-doctors/shymkent/140/",
    }
    city = "Шымкент"

    for category_name, list_url in category_urls.items():
        try:
            soup = fetch_html(list_url)

            # каждая услуга - блок с data-item-id
            rows = soup.select("div.analyzes-list[data-item-id]")

            if not rows:
                logging.warning(
                    f"{source_name} ({category_name}): 0 строк - возможно "
                    f"разметка сайта изменилась, проверь через F12"
                )
                continue

            count_for_category = 0
            for row in rows:
                try:
                    # ссылка с данными лежит внутри каждого блока услуги
                    link = row.select_one("a[data-type='analysis']")
                    if not link:
                        continue  # это может быть заголовок подкатегории, не услуга

                    service_name_raw = link.get("data-product-name")
                    price_raw = link.get("data-product-price")

                    if not service_name_raw or not price_raw:
                        continue

                    save_raw_row(
                        conn,
                        source=source_name,
                        clinic_name_raw="ИНВИТРО",
                        service_name_raw=service_name_raw,
                        price_raw=price_raw,
                        city=city,
                        source_url=list_url,
                    )
                    count_for_category += 1
                except Exception as row_error:
                    logging.error(f"{source_name} ({category_name}): ошибка в строке - {row_error}")
                    continue

            logging.info(f"{source_name} ({category_name}): обработано {count_for_category} строк")

        except Exception as source_error:
            logging.error(f"{source_name} ({category_name}): источник недоступен - {source_error}")
            save_raw_row(
                conn, source=source_name, clinic_name_raw=None,
                service_name_raw=None, price_raw=None, city=city,
                source_url=list_url, parse_error=str(source_error),
            )

        time.sleep(DELAY_SECONDS)


# Источник 4: alataulab.kz - сеть лабораторий во многих городах РК.
# Шаблон URL /ru/<город>/analyzes/<категория>/ работает для десятков городов
# и категорий; страница категории рендерится на сервере (данные сразу в HTML).
# Структура: услуга - div.result внутри div.results.results-sales. Внутри
# несколько div.cell. Название - div.cell__value.open-research-detail, цена -
# cell__value в том блоке, где cell__label == "ЦЕНА" (так отличаем её от названия).
def parse_alataulab(conn):
    source_name = "alataulab"

    # Берём города, которые покрыты и другими источниками (Алматы, Шымкент),
    # чтобы в каждом из них было несколько клиник для сравнения цен.
    pages = {
        ("Алматы", "Гематология"): "https://alataulab.kz/ru/almaty/analyzes/gematologiia/",
        ("Шымкент", "Гематология"): "https://alataulab.kz/ru/shymkent/analyzes/gematologiia/",
        # добавляй другие города или категории по такому же шаблону, но
        # сначала проверь - есть ли там уже другой источник для сравнения,
        # иначе город снова окажется "одиноким"
        # не больше 4-6 страниц за раз, чтобы не съесть всё время часа 1-7
    }

    for (city, category_name), list_url in pages.items():
        try:
            soup = fetch_html(list_url)

            # Каждая услуга - это div.result внутри контейнера результатов
            rows = soup.select("div.results.results-sales div.result")

            if not rows:
                logging.warning(
                    f"{source_name} ({city}, {category_name}): 0 строк - либо страница "
                    f"пуста для этой категории/города, либо разметка изменилась - "
                    f"проверь через F12"
                )
            else:
                count_for_page = 0
                for row in rows:
                    try:
                        # Название - cell__value СОВМЕЩЁННЫЙ с open-research-detail
                        name_tag = row.select_one("div.cell__value.open-research-detail")
                        if not name_tag:
                            continue
                        service_name_raw = name_tag.get_text(strip=True)

                        # Цена - ищем блок cell, где cell__label == "ЦЕНА",
                        # и берём его cell__value (без доп. класса, это отличает
                        # её от названия услуги)
                        price_raw = None
                        for cell in row.select("div.cell"):
                            label_tag = cell.select_one("div.cell__label")
                            if label_tag and label_tag.get_text(strip=True) == "ЦЕНА":
                                price_value_tag = cell.select_one("div.cell__value")
                                if price_value_tag:
                                    price_raw = price_value_tag.get_text(strip=True)
                                break

                        if not price_raw:
                            continue  # цены не нашли - пропускаем строку, не падаем

                        save_raw_row(
                            conn,
                            source=source_name,
                            clinic_name_raw="Alatau Lab",
                            service_name_raw=service_name_raw,
                            price_raw=price_raw,
                            city=city,
                            source_url=list_url,
                        )
                        count_for_page += 1
                    except Exception as row_error:
                        logging.error(f"{source_name} ({city}, {category_name}): ошибка в строке - {row_error}")
                        continue

                logging.info(
                    f"{source_name} ({city}, {category_name}): обработано {count_for_page} строк"
                )

        except Exception as source_error:
            logging.error(f"{source_name} ({city}, {category_name}): источник недоступен - {source_error}")
            save_raw_row(
                conn, source=source_name, clinic_name_raw=None,
                service_name_raw=None, price_raw=None, city=city,
                source_url=list_url, parse_error=str(source_error),
            )

        time.sleep(DELAY_SECONDS)


# ---------------------------------------------------------------------------
# ЗАПАСНОЙ ШАБЛОН - если останется время и решим добавить 5-й источник
# (например doq.kz - там нужно сначала глянуть DevTools -> Network,
# есть ли у них отдельный JSON API, это сильно проще чем HTML)
# Скопируй, переименуй, поправь URL и селекторы - смотри как сделаны
# parse_kdlolymp, parse_103kz и parse_invitro выше, логика та же.
# ---------------------------------------------------------------------------
def parse_source_template(conn):
    source_name = "ИМЯ_ИСТОЧНИКА"
    list_url = "https://example.kz/prices"

    try:
        soup = fetch_html(list_url)
        rows = soup.select(".CLASS_УСЛУГИ_СМОТРИ_ЧЕРЕЗ_F12")

        if not rows:
            logging.warning(f"{source_name}: 0 строк — проверь селектор/JS-рендер")
            return

        for row in rows:
            try:
                service_name_raw = row.select_one(".service-name").get_text(strip=True)
                price_raw = row.select_one(".price").get_text(strip=True)

                save_raw_row(
                    conn,
                    source=source_name,
                    clinic_name_raw=source_name,
                    service_name_raw=service_name_raw,
                    price_raw=price_raw,
                    city="Алматы",
                    source_url=list_url,
                )
            except Exception as row_error:
                logging.error(f"{source_name}: ошибка в строке — {row_error}")
                continue

        logging.info(f"{source_name}: обработано {len(rows)} строк")

    except Exception as source_error:
        logging.error(f"{source_name}: источник недоступен — {source_error}")
        save_raw_row(
            conn, source=source_name, clinic_name_raw=None,
            service_name_raw=None, price_raw=None, city=None,
            source_url=list_url, parse_error=str(source_error),
        )

    time.sleep(DELAY_SECONDS)


# ---------------------------------------------------------------------------
# ТОЧКА ВХОДА
# ---------------------------------------------------------------------------
SOURCES = [
    parse_kdlolymp,
    parse_103kz_catalog,
    parse_invitro,
    # parse_103kz_single_clinic,  # раскомментируй если хочешь добрать ПОЛНЫЙ список
    # услуг одной конкретной клиники сверху того что уже даёт каталог - но помни
    # про Crawl-Delay: 60 для этой функции
    # parse_source_template,  # раскомментируй когда адаптируешь под 5-й источник
]


def main():
    conn = init_db()
    logging.info("=== Запуск parse.py ===")

    for source_func in SOURCES:
        try:
            source_func(conn)
        except Exception as e:
            # На случай если ошибка вылезла мимо внутреннего try/except —
            # всё равно не должна ронять весь скрипт
            logging.error(f"Критическая ошибка в {source_func.__name__}: {e}")
            continue

    conn.close()
    logging.info("=== Завершено ===")
    print(f"Готово. Проверь {DB_PATH} (таблица raw_listings) и {LOG_PATH}")


if __name__ == "__main__":
    main()
