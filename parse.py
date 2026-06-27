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


# ---------------------------------------------------------------------------
# ИСТОЧНИК 1: kdlolymp.kz (КДЛ Олимп) — лаборатория, прайс одной страницей
# ---------------------------------------------------------------------------
# Проверено руками: robots.txt НЕ блокирует, на странице
# /pricelist/<город> весь прайс лежит прямо в HTML (не JS), без пагинации.
#
# СТРУКТУРА ПОДТВЕРЖДЕНА на реальном полном HTML страницы (не предположение):
#   каждая услуга - это <a class="analysis">, внутри которого:
#     <div class="title">Название услуги</div>
#     <div class="category">Категория</div>
#     <div class="duration">Срок (например "1 день")</div>
#     <div class="price">Цена с "₸" (например "1 880 ₸ ")</div>
# Старый селектор "a.service-item" был ОШИБОЧНЫМ предположением -
# теперь заменён на подтверждённый "a.analysis".
#
# ВЫБОР ГОРОДОВ - ВАЖНОЕ РЕШЕНИЕ:
# Раньше здесь стояли Астана и Алматы как "первые попавшиеся" города.
# Изменено по итогам обсуждения: суть сервиса по ТЗ - СРАВНЕНИЕ цен
# клиник МЕЖДУ СОБОЙ ("по аналогии с Aviasales"), а сравнивать можно
# только клиники одного города. Если разные источники покрывают разные
# города - в каждом городе будет всего одна клиника, и сравнения не
# получится, только список несвязанных городов.
# Поэтому здесь выбраны города, которые ТАКЖЕ покрывают другие источники
# (103.kz по Казахстану даёт Алматы; alataulab.kz даёт Алматы/Атырау;
# invitro.kz даёт Шымкент) - так на демо в одном городе можно показать
# несколько клиник с разными ценами на одну услугу, что и есть ценность
# агрегатора. Список городов kdlolymp.kz (для расширения при наличии
# времени) виден в __NUXT_DATA__ страницы - десятки городов РК.
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

            # Каждая услуга - это <a class="analysis">, подтверждено на реальном HTML
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


# ---------------------------------------------------------------------------
# ИСТОЧНИК 2: 103.kz - КАТАЛОГ КЛИНИК ПО ВСЕМУ КАЗАХСТАНУ
# ---------------------------------------------------------------------------
# ОБНОВЛЕНО: вместо одного запроса на один город (/list/analizy/astana/)
# берём ОДИН запрос на /list/analizy/kazakhstan/ - там сразу видны клиники
# из разных городов (в реальном HTML, который проверяли, были Алматы,
# Караганда, Семей на одной странице). Это прямой выигрыш для критерия
# ТЗ "Охват рынка" (вес 15%) - больше городов почти бесплатно, без
# отдельного запроса на каждый город.
#
# ВАЖНО ПРО <meta name="robots" content="noindex, nofollow">:
# Эта страница помечена noindex/nofollow - но это инструкция ДЛЯ ПОИСКОВЫХ
# СИСТЕМ про индексацию в выдаче (Google/Yandex не покажут её в поиске),
# а не запрет на доступ. Запрет на доступ задаётся в robots.txt, а его
# мы перепроверили: Disallow на /list/analizy/kazakhstan/ там нет.
# Страница технически доступна, и данные на ней - публичные цены клиник,
# которые сами клиники открыто выложили для пациентов (ТЗ п.8).
#
# Структура (та же, что и на городской странице, подтверждено на реальном
# HTML страницы /list/analizy/kazakhstan/):
#   каждая клиника - блок <div class="PlaceList__itemWrapper">
#     внутри <a class="Place__headerLink"> - название клиники
#     внутри <span class="Place__addressText"> - "Город, ул. ..." (город
#       вытаскиваем как текст до первой запятой - проверено на тесте)
#     каждая услуга - <div class="PlacePrices__itemWrapper">
#       <span class="PlacePrices__itemTitle"> - название услуги
#       <span class="PlacePrices__itemText"> - цена ("8 980 тенге",
#         "от 200 тенге") ИЛИ слово "уточняйте" без всякого числа -
#         такие строки пропускаем, цены там просто нет (см. ниже).
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
        # Используем именно этот уровень (а не сам div.Place напрямую),
        # чтобы гарантированно не зацепить услуги соседней клиники -
        # проверено на тесте, без этого подмешиваются чужие услуги.
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

                        # ВАЖНЫЙ EDGE CASE: цена может быть словом "уточняйте"
                        # без единой цифры - такую строку просто пропускаем,
                        # цены там реально нет, незачем тащить мусор в БД
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


# ---------------------------------------------------------------------------
# ИСТОЧНИК 2Б (ЗАПАСНОЙ): страница ОДНОЙ клиники на 103.kz - если хочешь
# добрать полный список услуг конкретной клиники (а не только превью
# из каталога) - открой /pricing/.../.../ у конкретной клиники.
# ВАЖНО: тут точно действует Crawl-Delay: 60 - подтверждено в её
# собственном robots.txt (отдельный хост green-clinic.103.kz).
# ---------------------------------------------------------------------------
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

            # ЗНАЕМ ПРО БАГ: если у услуги есть строка-описание метода
            # ПЕРЕД ценой, она попадёт в название вместо настоящего.
            # Такие кривые строки не смэтчатся в normalize.py и уйдут
            # в unmatched.csv - штатный путь, не чиним тут.
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


# ---------------------------------------------------------------------------
# ИСТОЧНИК 3: invitro.kz — структура ПОДТВЕРЖДЕНА на реальном HTML с сайта
# ---------------------------------------------------------------------------
# ИСПРАВЛЕНИЕ (важно): я была не права в более ранней версии этого файла -
# думала что цены на invitro.kz видны только на отдельных страницах каждой
# услуги. Это неверно. Пользователь прислал реальный HTML страницы-категории,
# и оказалось, что ВСЕ услуги категории с ценами лежат на ОДНОЙ странице,
# причём цена доступна прямо в data-атрибуте - даже проще чем у kdlolymp.kz.
#
# Структура (подтверждено на реальном коде страницы
# /analizes/for-doctors/<город>/<id_категории>/):
#   каждая услуга - это <div class="analyzes-list" data-item-id="...">
#   внутри - <a data-type="analysis" data-product-name="..."
#                data-product-price="..." data-product-article="...">
# Название и цена читаются прямо из атрибутов, без регулярок по тексту.
#
# ВАЖНО про пагинацию: если в категории много услуг, внизу есть кнопка
# "Показать ещё" (она работает через дополнительный AJAX-запрос).
# Для хакатона НЕ гонимся за пагинацией - берём только то что сразу
# отдаётся на первой загрузке страницы (для большинства категорий это
# уже 15-30+ услуг, больше чем достаточно).
#
# Robots.txt проверен: /analizes/for-doctors/... НЕ запрещён (нет такого
# правила Disallow ни в одной секции User-agent), так что парсить можно.
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

            # Каждая услуга - это блок с этим атрибутом (подтверждено выше)
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


# ---------------------------------------------------------------------------
# ИСТОЧНИК 4: alataulab.kz - сеть лабораторий по МНОЖЕСТВУ городов РК
# ---------------------------------------------------------------------------
# robots.txt проверен пользователем: запрещён только /ru/admin/,
# к страницам с прайсом это не относится - можно парсить.
#
# СИЛЬНАЯ СТОРОНА ЭТОГО ИСТОЧНИКА: один шаблон URL
#   /ru/<город>/analyzes/<категория>/
# работает для ДЕСЯТКОВ городов (Алматы, Астана, Шымкент, Караганда,
# Атырау, Павлодар, Петропавловск, Тараз и т.д. - видели полный список
# на сайте) и 24 категорий анализов. Это прямой выигрыш для критерия
# ТЗ "охват максимального числа городов" почти бесплатно - меняется
# только название города/категории в URL, код тот же.
#
# ---------------------------------------------------------------------------
# ИСТОЧНИК 4: alataulab.kz - сеть лабораторий по МНОЖЕСТВУ городов РК
# ---------------------------------------------------------------------------
# robots.txt проверен пользователем: запрещён только /ru/admin/,
# к страницам с прайсом это не относится - можно парсить.
#
# СИЛЬНАЯ СТОРОНА ЭТОГО ИСТОЧНИКА: один шаблон URL
#   /ru/<город>/analyzes/<категория>/
# работает для ДЕСЯТКОВ городов (Алматы, Астана, Шымкент, Караганда,
# Атырау, Павлодар, Петропавловск, Тараз и т.д.) и 24 категорий анализов.
# Прямой выигрыш для критерия ТЗ "охват максимального числа городов".
#
# ПОДТВЕРЖДЕНО НА РЕАЛЬНОМ ПОЛНОМ HTML (пользователь прислал исходный код
# страницы /ru/almaty/analyzes/gematologiia/ целиком):
# Страница ОДНОЙ конкретной категории рендерится на сервере и отдаёт
# реальные данные сразу в HTML - НЕ через AJAX. (В отличие от общей
# страницы /ru/<город>/analyzes/ со всеми 730 услугами и поиском - та
# действительно грузит данные через JS-запрос на
# /ru/ajax/<город>/a-and-c-search-with-panels/, но мы её не используем -
# нам нужна только страница конкретной категории, она безопасна.)
#
# РЕАЛЬНАЯ HTML-СТРУКТУРА (подтверждена, не предположение):
#   <div class="results results-sales">          <- контейнер всех услуг
#     <div class="result">                       <- ОДНА услуга
#       <div class="cell cell--main">
#         <div class="cell__label">Код 01-001</div>     <- код услуги
#         <div class="cell__value open-research-detail" data-service-id="444">
#           ОАК с лейкоцитарной формулой...               <- название
#         </div>
#       </div>
#       <div class="cell">
#         <div class="cell__label">СРОК</div>
#         <p> до 24 часов</p>                             <- срок
#       </div>
#       <div class="cell">
#         <div class="cell__label">ЦЕНА</div>
#         <div class="cell__value">2620 ₸</div>           <- цена
#       </div>
#     </div>
#   </div>
# ВАЖНЫЙ НЮАНС: и название услуги, и цена имеют ОДИНАКОВЫЙ class
# "cell__value" - различать их нужно по соседнему cell__label
# ("ЦЕНА" -> это блок с ценой) либо по дополнительному классу
# "open-research-detail" у названия (есть только у названия, не у цены).
# Это уже учтено в коде ниже, проверено на реальном фрагменте.
def parse_alataulab(conn):
    source_name = "alataulab"

    # ИЗМЕНЕНО по итогам обсуждения: раньше здесь стояли Атырау и Павлодар
    # как "новые" города, которых не было у других источников. Но это
    # давало изолированные точки - в Атырау/Павлодаре была всего ОДНА
    # клиника без возможности сравнения цен, а сравнение цен внутри
    # города - это и есть смысл агрегатора по ТЗ ("по аналогии с Aviasales").
    # Теперь города выбраны так, чтобы СОВПАДАТЬ с другими источниками:
    #   Алматы  - там же есть kdlolymp.kz и 103.kz -> 3 источника на демо
    #   Шымкент - там же есть kdlolymp.kz и invitro.kz -> 3 источника на демо
    # Караганда и Семей всё равно остаются в базе бонусом от 103.kz
    # (тот источник отдаёт их сам за один проход по всей стране), так что
    # охват по городам не теряется - просто перестаёт быть единственной
    # целью в ущерб возможности сравнения.
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
