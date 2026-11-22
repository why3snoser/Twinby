#!/usr/bin/env python3
"""
Локальный веб-интерфейс поверх twinby_onboard.py (только localhost).
Онбординг + заполнение профиля + лайк/дизлайк по ID. Все варианты выбираются кнопками, город ищется подсказками.
Порядок и тела запросов профиля скопированы из HAR (PATCH /api/users/me/ со страницы /app/profile/info).

Запуск:
    pip install -r requirements.txt
    python -m playwright install chromium
    python app.py
Потом открой: http://127.0.0.1:5000
"""
from __future__ import annotations

import subprocess
import tempfile
import os
import io
import json
import contextlib
import threading
import re
import time
import traceback
import uuid
import base64
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request
from curl_cffi import requests as cffi_requests
from curl_cffi import CurlHttpVersion

import twinby_onboard as tw
from cities import RUSSIAN_CITIES, get_cities_json
import vk_auth

_VK_BROWSER_AUTH_LOCK = threading.Lock()

app = Flask(__name__)

# ---- конфиг (источник: HAR 2026-11-20) -------------------------------------------------------
API_PROFILE = {
    "city": "/api/city/",          # ?name=<строка>   -> [{id,name,cityId,regionName}]
    "religion": "/api/religion/",  # -> [{id,name,icon}]
    "language": "/api/language/",  # -> [{id,code,name,icon}]
    "interest": "/api/interest/",  # -> [{id,group,name,icon}]
}
RELIGIONS = [
    ("1000096204387942411", "Буддизм"), ("1000096763409825800", "Еврейство"),
    ("1000097003157913601", "Индуизм"), ("1000097220562550792", "Ислам"),
    ("1020795595809488907", "Иудаизм"), ("1000097464401821698", "Католицизм"),
    ("1000097877049311242", "Конфуцианство"), ("1000098184962867202", "Православие"),
    ("1000098431279661065", "Протестантизм"), ("1000098728882503684", "Светский гуманизм"),
    ("1000100355977117697", "Агностицизм"), ("1000099600556851210", "Атеизм"),
    ("1020796526629814282", "Деизм"),
]
# ПОДПИСИ взяты со скриншотов сайта (порядок кнопок). ЧИСЛА НЕ ПРОВЕРЕНЫ: в HAR виден только факт
# education=0, children=0, alcohol=1, smoking=1, а соответствие подписям там не записано.
# Предположение: число = номер кнопки сверху вниз, начиная с 0. Проверь на сайте после первого запуска.
EDUCATION = [("0", "Среднее"), ("1", "Высшее"), ("2", "Кандидат наук/PhD")]
CHILDREN = [("0", "Нет"), ("1", "Нет, но хотелось бы"), ("2", "Уже есть")]
HABIT = [("0", "Резко негативно"), ("1", "Нейтрально"), ("2", "Положительно")]
HEIGHT_MIN, HEIGHT_MAX = 50, 250   # из валидатора формы на сайте (Validators.min(50)/max(250))
MIME = {".webp": "image/webp", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
PHOTO_ORDER = "0"   # НЕ проверено: сервер требует поле order в /api/photo/, значение угадано
# ПОДТВЕРЖДЕНО: снято с реального запроса мобильного приложения (mitmproxy, build 2026-11-20).
# Запрос: POST /api/photo/liveness/v2, multipart с ОДНОЙ частью "data" (video/mp4, filename REC<число>.mp4),
# плюс заголовок idempotency-key: <uuid4>. Доп. полей в форме нет.
LIVENESS_PATH = "/api/photo/liveness/v2"
LIVENESS_MAX_BYTES = 50_000_000
# ПОДТВЕРЖДЕНО: снято в mitmproxy 2026-11-21 через Frida-перехват реального трафика мобильного приложения.
# POST /api/dating/action/from-search-action/ с телом {"user": "<id анкеты>", "type": 0|1}.
# type=0 — лайк, type=1 — дизлайк. Соответствие проверено вручную: оба запроса сняты в момент
# осознанного свайпа вправо/влево в приложении и сверены построчно, не угадано.
DATING_ACTION_PATH = "/api/dating/action/from-search-action/"
# Списки для кнопок взяты из ответов сервера в HAR; при отправке id сверяются с живыми списками.
INTEREST_GROUPS = {
 "Социальная активность": [
  [
   "1010901735261667334",
   "Волонтерство"
  ],
  [
   "1010901883795570699",
   "Благотворительность"
  ],
  [
   "1010902400503218186",
   "Экоактивизм"
  ],
  [
   "1010902559097028615",
   "Помощь пожилым"
  ],
  [
   "1010902697893756937",
   "Работа с детьми"
  ],
  [
   "1010902786146205697",
   "Защита животных"
  ],
  [
   "1048846444015484939",
   "Субботники"
  ],
  [
   "1048846581367406598",
   "Социальные проекты"
  ],
  [
   "1048846704971612166",
   "Образовательные проекты"
  ],
  [
   "1048846823464861706",
   "Донорство крови"
  ],
  [
   "1048846922955128836",
   "Карьерное наставничество"
  ],
  [
   "1048847032370200587",
   "Мотивационные лекции"
  ],
  [
   "1048847136946454530",
   "Помощь бездомным"
  ],
  [
   "1048847234531360770",
   "Участие в дискуссионных клубах"
  ],
  [
   "1050181294765441031",
   "Реставрация исторических памятников"
  ]
 ],
 "Спорт и активный отдых": [
  [
   "1010903038438080518",
   "Бег"
  ],
  [
   "1010903517113090054",
   "Плавание"
  ],
  [
   "1010904065047789569",
   "Йога"
  ],
  [
   "1010904526665154567",
   "Велоспорт"
  ],
  [
   "1010904705400930311",
   "Спортзал"
  ],
  [
   "1010904901343641606",
   "Лыжи"
  ],
  [
   "1010905188711497734",
   "Сноуборд"
  ],
  [
   "1010905412107206666",
   "Танцы"
  ],
  [
   "1010905654921265163",
   "Боевые искусства"
  ],
  [
   "1010906009282478082",
   "Серфинг"
  ],
  [
   "1010906166883024905",
   "Хайкинг"
  ],
  [
   "1010906384335372292",
   "Теннис"
  ],
  [
   "1010906912161693700",
   "Скалолазание"
  ],
  [
   "1048837498353287174",
   "Гольф"
  ],
  [
   "1048837613248380930",
   "Кёрлинг"
  ],
  [
   "1048837770439426058",
   "Конный спорт"
  ],
  [
   "1048837874226495496",
   "Дартс"
  ],
  [
   "1048837968004349958",
   "Гимнастика"
  ],
  [
   "1048838098489671687",
   "Парашютный спорт"
  ],
  [
   "1048838189814251521",
   "Гребля"
  ],
  [
   "1048838301663133706",
   "Подводное плавание"
  ],
  [
   "1048838398280138760",
   "Каякинг"
  ],
  [
   "1048838582739402756",
   "Бокс"
  ],
  [
   "1048838671450210315",
   "Бейсбол"
  ],
  [
   "1048838777853018121",
   "Регби"
  ],
  [
   "1048838873505562625",
   "Хоккей"
  ],
  [
   "1048839084513656833",
   "Лёгкая атлетика"
  ],
  [
   "1048839246226817026",
   "Ролики"
  ],
  [
   "1048839530890887174",
   "Велотриал"
  ],
  [
   "1048839864922898440",
   "Настольный теннис"
  ],
  [
   "1048839984907354120",
   "Пляжный волейбол"
  ],
  [
   "1048840073341370378",
   "Скейтбординг"
  ],
  [
   "1048840163854876680",
   "Скандинавская ходьба"
  ],
  [
   "1048840256940965894",
   "Бадминтон"
  ],
  [
   "1097226263167336457",
   "Футбол"
  ],
  [
   "1097232679299416072",
   "Баскетбол"
  ],
  [
   "1097234794704994313",
   "Формула-1"
  ],
  [
   "1097724965119983622",
   "Пилатес"
  ],
  [
   "1097725186525102086",
   "Стретчинг"
  ],
  [
   "1097725516979732487",
   "Картинг"
  ],
  [
   "1097812709786255370",
   "Шахматы"
  ],
  [
   "1098004323540008966",
   "Нарды"
  ],
  [
   "1098004421231149058",
   "Сапсерфинг"
  ],
  [
   "1098080563485573126",
   "Падел"
  ],
  [
   "1103185693815898122",
   "Бильярд"
  ],
  [
   "1103185778302255110",
   "Волейбол"
  ]
 ],
 "Образование и саморазвитие": [
  [
   "1010903053766819851",
   "Изучение языков"
  ],
  [
   "1010903180108824587",
   "Научные лекции"
  ],
  [
   "1010903287063248907",
   "Онлайн-курсы"
  ],
  [
   "1010903380144914442",
   "Самообразование"
  ],
  [
   "1010903476827422730",
   "Медитация"
  ],
  [
   "1010903598708195329",
   "Психология"
  ],
  [
   "1010903700893761540",
   "Философия"
  ],
  [
   "1010903798281109513",
   "История"
  ],
  [
   "1010903881942237185",
   "Технологии"
  ],
  [
   "1010904018580602882",
   "Чтение"
  ],
  [
   "1048845123448471560",
   "Ораторское искусство"
  ],
  [
   "1048845212391440392",
   "Финансовая грамотность"
  ],
  [
   "1048845352834793476",
   "Лидерские навыки"
  ],
  [
   "1048845457875206145",
   "История кино"
  ],
  [
   "1098006918748700679",
   "Нейросети"
  ],
  [
   "1098007703076143106",
   "Культура других стран"
  ],
  [
   "1098007996056698887",
   "Программирование"
  ],
  [
   "1098009167392571398",
   "Инвестиции"
  ],
  [
   "1098009398285074439",
   "Управление финансами"
  ],
  [
   "1098010994835390466",
   "Трейдинг"
  ]
 ],
 "Хобби и развлечения": [
  [
   "1010905094158680075",
   "Литература"
  ],
  [
   "1010905210941014020",
   "Видеоигры"
  ],
  [
   "1010905318983991302",
   "Настольные игры"
  ],
  [
   "1010905441840660490",
   "Путешествия"
  ],
  [
   "1010905551292792833",
   "Выращивание растений"
  ],
  [
   "1010905658919944196",
   "Рыбалка"
  ],
  [
   "1010905753889570820",
   "Прогулки с собаками"
  ],
  [
   "1010905891292512266",
   "Любитель кошек"
  ],
  [
   "1010905992692334598",
   "Автомобили и мотоциклы"
  ],
  [
   "1010906107192541194",
   "Концерты"
  ],
  [
   "1048844028771663878",
   "Караоке"
  ],
  [
   "1048844116746436610",
   "Подкасты"
  ],
  [
   "1048844458906320903",
   "Посещение музеев"
  ],
  [
   "1048844568140677128",
   "Участие в театральных постановках"
  ],
  [
   "1048844673557561351",
   "Ретро-игры"
  ],
  [
   "1048844778201513991",
   "Косплей"
  ],
  [
   "1097236122879721483",
   "Стендап"
  ],
  [
   "1097783958085271556",
   "Астрология"
  ],
  [
   "1097789349476040713",
   "Сериалы"
  ],
  [
   "1097813958000574474",
   "Моделинг"
  ],
  [
   "1097814126608449542",
   "Мода"
  ],
  [
   "1097814455352819713",
   "Диджеинг"
  ],
  [
   "1098005875588923402",
   "Встречи с друзьями"
  ],
  [
   "1098006151453016070",
   "Коллекционирование"
  ],
  [
   "1098006364001271814",
   "Керамика"
  ],
  [
   "1098006520690049026",
   "Тру-крайм"
  ],
  [
   "1098006754306064388",
   "Онлайн-шопинг"
  ],
  [
   "1098065135906193412",
   "Игра на гитаре"
  ],
  [
   "1102278660633919496",
   "Ведение блога"
  ],
  [
   "1115923811809820678",
   "LOUNGE"
  ]
 ],
 "Гастрономия": [
  [
   "1010906641335123976",
   "Приготовление блюд"
  ],
  [
   "1010906739019513858",
   "Дегустация вин"
  ],
  [
   "1010906843597176841",
   "Тур по барам"
  ],
  [
   "1010907068421144586",
   "Кофейный эксперт"
  ],
  [
   "1010907170091663367",
   "Чайные церемонии"
  ],
  [
   "1010907403828985860",
   "Вегетарианская кухня"
  ],
  [
   "1010907535142223873",
   "Ресторанный критик"
  ],
  [
   "1010907649697873926",
   "Любитель сладкого"
  ],
  [
   "1048842367667830790",
   "Любитель пиццы"
  ],
  [
   "1048842539623972866",
   "Мексиканская кухня"
  ],
  [
   "1048842658457419785",
   "Суши и роллы"
  ],
  [
   "1048843400815017995",
   "Любитель сыра"
  ],
  [
   "1048843506196709382",
   "Азиатская кухня (рамен, фо)"
  ],
  [
   "1048843601279877130",
   "Острая кухня"
  ],
  [
   "1048843700383023106",
   "Коктейли своими руками"
  ],
  [
   "1048843788995231754",
   "Кето-кухня"
  ],
  [
   "1097732456320499714",
   "Доставка еды"
  ]
 ],
 "Творчество и искусство": [
  [
   "1010907552103333897",
   "Живопись"
  ],
  [
   "1010907695278587912",
   "Фотография"
  ],
  [
   "1010907851965431814",
   "Музыка"
  ],
  [
   "1010908013444661257",
   "Пение"
  ],
  [
   "1010908248224301063",
   "Писательство"
  ],
  [
   "1010908429118734338",
   "Скульптура"
  ],
  [
   "1010908567611768840",
   "Театр"
  ],
  [
   "1010908720831397892",
   "Кино"
  ],
  [
   "1010908863665537032",
   "Рукоделие"
  ],
  [
   "1048840977801084935",
   "Иллюстрация"
  ],
  [
   "1048841062264340488",
   "Каллиграфия"
  ],
  [
   "1048841180402286594",
   "Вышивка"
  ],
  [
   "1048841281234206722",
   "Создание электронной музыки"
  ],
  [
   "1048841531146665992",
   "Дизайн интерьеров"
  ],
  [
   "1048841639437729796",
   "Графический дизайн"
  ],
  [
   "1048841723098628107",
   "Резьба по дереву"
  ],
  [
   "1048841815774363650",
   "Комиксы и манга"
  ],
  [
   "1048841935779594251",
   "Флористика"
  ],
  [
   "1048842030227324939",
   "Декорирование свечей"
  ],
  [
   "1048842122822418438",
   "Оригами"
  ],
  [
   "1097733357672693768",
   "Архитектура"
  ],
  [
   "1097812496110977035",
   "Стрит-арт"
  ],
  [
   "1098064706459959300",
   "Выставки"
  ],
  [
   "1103185905183096841",
   "Рок-музыка"
  ],
  [
   "1103186134017736712",
   "Поп-музыка"
  ],
  [
   "1103186224638361608",
   "Рэп"
  ],
  [
   "1113903967636094984",
   "Макияж"
  ]
 ]
}
LANGUAGES = [["753298335899156481", "Азербайджанский"], ["753298335914754049", "Албанский"], ["753298335928254465", "Амхарский"], ["753298335940608001", "Английский"], ["753298335953027073", "Арабский"], ["753298335970492417", "Армянский"], ["753298335984877569", "Африкаанс"], ["753298335999787009", "Белорусский"], ["753298336015220737", "Бенгальский"], ["753298336028360705", "Бирманский"], ["753298336040550401", "Болгарский"], ["753298336052674561", "Боснийский"], ["753298336068042753", "Валлийский"], ["753298336083247105", "Венгерский"], ["753298336097435649", "Вьетнамский"], ["753298336113229825", "Голландский"], ["753298336127254529", "Греческий"], ["753298336144916481", "Грузинский"], ["753298336162119681", "Гуджарати"], ["753298336188399617", "Датский"], ["753298336203833345", "Иврит"], ["753298336217792513", "Индонезийский"], ["753298336233979905", "Ирландский"], ["753298336247283713", "Итальянский"], ["753298336264192001", "Исландский"], ["753298336278642689", "Испанский"], ["753298336295059457", "Казахский"], ["753298336309313537", "Каталанский"], ["753298336322912257", "Киргизский"], ["753298336336412673", "Китайский"], ["753298336349650945", "Корейский"], ["753298336363118593", "Кхмерский"], ["753298336378126337", "Лаосский"], ["753298336394706945", "Латышский"], ["753298336409714689", "Литовский"], ["753298336424820737", "Люксембургский"], ["753298336437731329", "Малагасийский"], ["753298336450838529", "Малазийский"], ["753298336465551361", "Мальтийский"], ["753298336485310465", "Македонский"], ["753298336504643585", "Монгольский"], ["753298336517685249", "Немецкий"], ["753298336534921217", "Непальский"], ["753298336548651009", "Норвежский"], ["753298336562511873", "Пенджабский"], ["753298336578076673", "Персидский"], ["753298336592461825", "Польский"], ["753298336605634561", "Португальский"], ["753298336621527041", "Румынский"], ["753298336634404865", "Русский"], ["753298336648462337", "Сербский"], ["753298336662749185", "Сингальский"], ["753298336678182913", "Словацкий"], ["753298336690077697", "Словенский"], ["753298336701808641", "Суахили"], ["753298336715767809", "Таджикский"], ["753298336730218497", "Тайский"], ["753298336746242049", "Филиппинский"], ["753298336759578625", "Тамильский"], ["753298336772685825", "Татарский"], ["753298336786776065", "Телугу"], ["753298336799326209", "Турецкий"], ["753298336817840129", "Узбекский"], ["753298336830914561", "Украинский"], ["753298336843923457", "Урду"], ["753298336896516097", "Финский"], ["753298336957661185", "Французский"], ["753298337057603585", "Хинди"], ["753298337251459073", "Хорватский"], ["753298337451933697", "Чешский"], ["753298337651490817", "Шведский"], ["753298337851015169", "Эстонский"], ["753298338052440065", "Японский"]]
# -----------------------------------------------------------------------------------------------

AUTH_VK_V2_PATH = "/api/auth/v2/oauth/by-token"  # legacy, НЕ РАБОТАЕТ для онбординга
AUTH_VK_V3_PATH = "/api/auth/v3/oauth/by-token"  # VK ID PKCE — рабочий
DATING_SEARCH_PATH = "/api/users/search/?size=50"  # GET → список анкет для свайпа (confirmed via mitmproxy)
USERS_PUBLIC_PATH = "/api/users/{uid}/"           # GET → публичный профиль по ID

# Хранилище аккаунтов — файл, переживает рестарт. Формат: {label: {vk_token, jwt, user_id, name}}
ACCOUNTS_FILE = Path(__file__).parent / "accounts.json"

def _load_accounts() -> dict:
    if ACCOUNTS_FILE.exists():
        try:
            return json.loads(ACCOUNTS_FILE.read_text())
        except Exception:
            return {}
    return {}

def _save_accounts(data: dict):
    ACCOUNTS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))

ACCOUNTS: dict[str, dict] = _load_accounts()

# ---------------------------------------------------------------------------
# Прокси-пул: парсинг и выделение
# ---------------------------------------------------------------------------
_proxy_index = 0  # round-robin counter

def _parse_proxy_pool(settings: dict | None = None) -> list[str]:
    """
    Парсит proxy_pool из настроек.
    Формат строки: ip:port:user:pass  (user может содержать ';' и другие символы).
    Возвращает список строк вида "http://user:pass@ip:port".
    """
    if settings is None:
        settings = _load_settings()
    raw = settings.get("proxy_pool", "")
    result = []
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split(":")
        if len(parts) >= 4:
            ip = parts[0]
            port = parts[1]
            user = parts[2]
            pwd = ":".join(parts[3:])  # пароль может содержать ':'
            result.append(f"http://{user}:{pwd}@{ip}:{port}")
        elif len(parts) == 2:
            # ip:port без авторизации
            result.append(f"http://{parts[0]}:{parts[1]}")
    return result


def _get_next_proxy(settings: dict | None = None) -> str:
    """Round-robin: следующий прокси из пула. Пустая строка если пул пуст."""
    global _proxy_index
    pool = _parse_proxy_pool(settings)
    if not pool:
        return ""
    proxy = pool[_proxy_index % len(pool)]
    _proxy_index += 1
    return proxy


def _normalize_proxy(raw: str) -> str:
    """Если прокси уже http://... — вернуть как есть. Иначе парсить ip:port:user:pass."""
    raw = raw.strip()
    if not raw:
        return ""
    if raw.startswith("http://") or raw.startswith("https://") or raw.startswith("socks"):
        return raw
    parts = raw.split(":")
    if len(parts) >= 4:
        ip, port, user = parts[0], parts[1], parts[2]
        pwd = ":".join(parts[3:])
        return f"http://{user}:{pwd}@{ip}:{port}"
    elif len(parts) == 2:
        return f"http://{parts[0]}:{parts[1]}"
    return raw


def _make_proxied_httpx_client(proxy: str = "", **extra) -> "httpx.Client":
    """Создаёт httpx.Client с прокси (если задан) и стандартными заголовками Twinby."""
    import httpx as _httpx
    proxy = _normalize_proxy(proxy)
    kw: dict = {
        "timeout": 30,
        "headers": {"User-Agent": tw.UA, "Accept": "application/json"},
        **extra,
    }
    if proxy:
        kw["proxy"] = proxy
    return _httpx.Client(**kw)


def _auth_vk_to_jwt(vk_login: str = "", vk_pass: str = "", vk_token: str = "",
                     rucaptcha_key: str = "", proxy: str = "") -> dict:
    """Get a Twinby JWT using the browser VK ID PKCE flow when credentials are present."""
    log: list[str] = []

    if vk_login and vk_pass:
        try:
            from test_browser_vk_login import run_test_twinby_web_ui
        except Exception as exc:
            return {"ok": False,
                    "error": f"Browser VK auth dependencies unavailable: {type(exc).__name__}: {exc}",
                    "log": log}

        captured = io.StringIO()
        try:
            # Only one synchronous Chromium login runs at a time in this local app.
            with _VK_BROWSER_AUTH_LOCK:
                with contextlib.redirect_stdout(captured):
                    result = run_test_twinby_web_ui(
                        login=vk_login,
                        password=vk_pass,
                        proxy_raw=proxy,
                        rucaptcha_key=rucaptcha_key,
                        headful=True,
                    )
        except Exception as exc:
            detail = str(exc)
            for secret in (vk_login, vk_pass, rucaptcha_key, proxy):
                if secret:
                    detail = detail.replace(secret, "<redacted>")
            result = {"ok": False,
                      "error": f"browser auth raised {type(exc).__name__}: {detail[:300]}"}

        raw_log = captured.getvalue()
        for line in raw_log.splitlines():
            if any(marker in line for marker in (
                "[mode] twinby-web-ui", "[browser]", "[1] page.goto", "waiting for visible phone field",
                "[3] waiting for password field", "[oauth-check]", "VK ID login accepted",
                "Captured Twinby redirect", "Phone login field", "Password field"
            )):
                log.append(line.strip())

        if result.get("ok") and result.get("jwt"):
            log.append("[AUTH] browser VK ID PKCE flow succeeded; Twinby JWT received")
            return {"ok": True, "jwt": result["jwt"],
                    "refresh_token": result.get("refresh_token", ""), "log": log}

        error_text = str(result.get("error", ""))
        status = re.search(r"Twinby v3:\s*(\d{3})", error_text)
        request_id = re.search(r"request_id\W+([0-9a-fA-F]{16,})", error_text)
        if status:
            safe_error = f"Twinby JWT exchange failed (HTTP {status.group(1)})"
            if request_id:
                safe_error += f", request_id={request_id.group(1)}"
        elif "ERR_TUNNEL_CONNECTION_FAILED" in raw_log:
            safe_error = "VK browser could not establish the proxy tunnel."
        elif "Execution context was destroyed" in raw_log:
            safe_error = "VK page navigated during a browser API request."
        elif "missing device_id" in error_text or "state mismatch" in error_text:
            safe_error = error_text
        elif "Phone login field" in error_text:
            safe_error = "VK login form did not become visible within 45 seconds."
        elif "Password field" in error_text:
            safe_error = "VK password form did not become visible within 30 seconds."
        elif "browser auth raised" in error_text:
            safe_error = error_text
        else:
            safe_error = "Browser VK ID UI flow failed; review the safe stage log."
        log.append("[AUTH] browser VK ID PKCE flow failed")
        return {"ok": False, "error": safe_error, "log": log}

    if vk_token:
        log.append("[AUTH] trying legacy VK-token exchange")
        result = vk_auth.full_auth_v2("", "", rucaptcha_key, proxy=proxy, vk_token=vk_token)
        log.extend(result.get("log", []))
        if result.get("ok"):
            log.append("[AUTH] legacy VK-token exchange succeeded")
            return {"ok": True, "jwt": result["jwt"],
                    "refresh_token": result.get("refresh_token", ""), "log": log}
        log.append(f"[AUTH] legacy VK-token exchange failed: {result.get('error', 'unknown')}")

    return {"ok": False, "error": "Provide VK login and password, or a usable VK token.", "log": log}


def _page(tab: str, content: str, log: str = None, error: str = None) -> str:
    """Layout wrapper. tab = 'main'|'accounts'|'swipe'."""
    tabs_html = ""
    for t, href, label in [("main", "/", "Онбординг"), ("accounts", "/accounts", "Аккаунты"), ("swipe", "/swipe", "Свайпы"), ("presets", "/presets", "Пресеты")]:
        cls = "active" if t == tab else ""
        tabs_html += f'<a href="{href}" class="{cls}">{label}</a>'
    log_html = f'<h3 style="margin-top:30px">Консоль</h3><pre>{log}</pre>' if log else ""
    err_html = f'<p class="err">{error}</p>' if error else ""
    return PAGE_LAYOUT.replace("{{TABS}}", tabs_html).replace("{{CONTENT}}", content).replace("{{LOG}}", log_html).replace("{{ERROR}}", err_html)

PAGE_LAYOUT = """
<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Twinby Tool</title>
<style>
*{box-sizing:border-box}
body{font-family:system-ui,sans-serif;max-width:780px;margin:0 auto;padding:0 16px 60px;background:#0d0d0d;color:#eee}
/* --- tabs --- */
.tabs{display:flex;gap:0;margin:20px 0 0;border-bottom:2px solid #333}
.tabs a{padding:12px 22px;color:#888;text-decoration:none;font-weight:600;font-size:15px;border-bottom:2px solid transparent;margin-bottom:-2px;transition:.15s}
.tabs a:hover{color:#ccc}
.tabs a.active{color:#3a7;border-bottom-color:#3a7}
h1{margin:24px 0 0;font-size:22px;font-weight:700;color:#eee}
.subtitle{color:#666;font-size:13px;margin:4px 0 0}
label.l{display:block;margin-top:14px;font-size:14px;color:#aaa}
input[type=text],input[type=number],input[type=date],input[type=file],select,textarea{width:100%;padding:8px;margin-top:4px;background:#1a1a1a;color:#eee;border:1px solid #333;border-radius:6px}
textarea{min-height:70px;resize:vertical}
button.go{margin-top:16px;padding:10px 20px;background:#3a7;color:#000;border:0;border-radius:6px;font-weight:600;cursor:pointer;font-size:14px}
button.go:hover{background:#4b8}
button.go.alt{background:#444;color:#eee}
button.go.alt:hover{background:#555}
button.go.dislike{background:#a33;color:#fff}
button.go.dislike:hover{background:#c44}
button.go.danger{background:#a33;color:#fff}
pre{white-space:pre-wrap;background:#1a1a1a;padding:12px;border-radius:6px;border:1px solid #333;font-size:13px;max-height:400px;overflow-y:auto}
fieldset{margin-top:22px;border:1px solid #333;border-radius:8px;padding:4px 14px 14px}
legend{color:#3a7;padding:0 6px;font-weight:600}
.warn{color:#f90;font-size:13px}.err{color:#f55}.ok{color:#3a7}.hint{color:#666;font-size:12px}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}
.chip{position:relative}
.chip input{position:absolute;opacity:0;pointer-events:none}
.chip span{display:inline-block;padding:7px 13px;border:1px solid #333;border-radius:999px;background:#1a1a1a;color:#ccc;cursor:pointer;font-size:14px;user-select:none;transition:.15s}
.chip input:checked+span{background:#3a7;color:#000;border-color:#3a7;font-weight:600}
.chip input:focus-visible+span{outline:2px solid #fff}
h4{margin:16px 0 0;color:#aaa;font-weight:500;font-size:13px}
.sug button{margin:4px 6px 0 0;padding:6px 12px;border-radius:999px;border:1px solid #333;background:#1a1a1a;color:#eee;cursor:pointer}
.sug button:hover{border-color:#3a7}
#cityPick{margin-top:6px;color:#3a7;font-size:14px}
.row{display:flex;gap:10px}.row>div{flex:1}
.actions-row{display:flex;gap:10px;margin-top:8px}
/* account cards */
.acct-card{background:#1a1a1a;border:1px solid #333;border-radius:8px;padding:12px 16px;margin-top:10px;display:flex;justify-content:space-between;align-items:center}
.acct-card .info{font-size:14px}.acct-card .info b{color:#3a7}
.acct-card .jwt{font-size:11px;color:#666;word-break:break-all;max-width:400px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.copy-btn{background:#333;color:#eee;border:1px solid #555;border-radius:4px;padding:4px 10px;cursor:pointer;font-size:12px}
.copy-btn:hover{background:#444}
/* swipe card */
.swipe-card{background:#1a1a1a;border:1px solid #333;border-radius:10px;padding:16px;margin-top:12px;text-align:center}
.swipe-card img{max-width:200px;max-height:260px;border-radius:8px;margin-bottom:10px}
.swipe-card .name{font-size:18px;font-weight:600;color:#eee}
.swipe-card .meta{font-size:13px;color:#888;margin-top:4px}
table.log-table{width:100%;border-collapse:collapse;margin-top:10px;font-size:13px}
table.log-table td,table.log-table th{padding:6px 10px;border-bottom:1px solid #222;text-align:left}
table.log-table th{color:#888;font-weight:500}
</style></head><body>
<h1>Twinby Tool</h1>
<p class="subtitle">Онбординг, профиль, свайпы. Токены уходят только на twinby.ru.</p>

<div class="tabs">{{TABS}}</div>

{{CONTENT}}

{{LOG}}
{{ERROR}}
</body></html>
"""

PAGE_MAIN = """
<form method="post" action="/old" enctype="multipart/form-data">
  <label class="l">Access token (JWT, optional if using VK login below)</label>
  <textarea id="token" name="token" placeholder="Paste a JWT, or leave blank to sign in with VK">{{ f.token }}</textarea>
  <fieldset><legend>VK ID sign-in (optional)</legend>
    <label class="l">VK login</label><input type="text" name="vk_id" autocomplete="username">
    <label class="l">VK password</label><input type="password" name="vk_pass" autocomplete="current-password">
    <p class="hint">If the JWT field is empty, the app runs the browser-based VK ID PKCE flow.</p>
  </fieldset>

  <label class="l"><input type="checkbox" name="profile_only" {{ 'checked' if f.profile_only }}>
    Только профиль (онбординг на этом аккаунте уже пройден)</label>

  <fieldset><legend>Онбординг</legend>
    <label class="l"><input type="checkbox" name="accept_twinby_terms">
      I accept Twinby's Privacy Policy and User Agreement and confirm that I am at least 18.</label>
    <label class="l">Имя</label><input type="text" name="name" value="{{ f.name }}">
    <label class="l">Дата рождения</label><input type="date" name="birthday" value="{{ f.birthday }}">
    <label class="l">Пол</label>
    <div class="chips">
      <label class="chip"><input type="radio" name="gender" value="m" {{ 'checked' if f.gender=='m' }}><span>Мужской</span></label>
      <label class="chip"><input type="radio" name="gender" value="f" {{ 'checked' if f.gender=='f' }}><span>Женский</span></label>
    </div>
    <label class="l">Фото (.webp / .jpg / .png)</label><input type="file" name="photo" accept="image/*">
    <label class="l">Target ID (очисти поле, чтобы получить список в логе)</label>
    <input type="text" name="target_id" value="{{ f.target_id }}">
    <label class="l">Interview ID (очисти поле, чтобы получить список в логе)</label>
    <input type="text" name="interview_id" value="{{ f.interview_id }}">
  </fieldset>

  <fieldset><legend>Видео-верификация (опционально)</legend>
    <label class="l">Видео для /api/photo/liveness/v2 (.mp4, как записывает приложение)</label>
    <input type="file" name="liveness_video" accept="video/mp4,.mp4">
    <label><input type="checkbox" name="vflip"> Перевернуть видео (vflip)</label>
    <p class="hint">Видео автоматически конвертируется в формат Android MediaRecorder (640x480, Baseline, без аудио).
      Если поле пустое — шаг пропускается.</p>
  </fieldset>

  <fieldset><legend>Фото профиля</legend>
    <p class="hint">Загрузи фото для анкеты. Первое фото = аватар. При "Bad photo" автоматически попробует следующее.</p>
    <div id="photoSection">
      <div id="currentPhotos" style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px"></div>
      <input type="file" id="photoFiles" multiple accept="image/*" style="margin-bottom:6px">
      <label class="l">Кол-во фото для загрузки (макс)</label>
      <input type="number" id="photoCount" value="3" min="1" max="10" style="max-width:80px">
      <button type="button" class="go" onclick="uploadPhotos()" style="margin-top:6px">Загрузить фото</button>
      <div id="photoLog" style="margin-top:6px;font-size:13px"></div>
    </div>
<script>
function getToken(){
  const t=document.querySelector('textarea[name=token]');
  return t?(t.value||'').trim().replace(/^["']+|["']+$/g,''):'';
}
function loadCurrentPhotos(){
  const token=getToken(); if(!token) return;
  fetch('/my-photos',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token})})
  .then(r=>r.json()).then(data=>{
    const box=document.getElementById('currentPhotos'); box.innerHTML='';
    (data.photos||[]).forEach(p=>{
      const div=document.createElement('div');
      div.style.cssText='position:relative;width:80px;height:80px;border-radius:8px;overflow:hidden;border:2px solid '+(p.is_avatar?'#4f4':'#555');
      div.innerHTML='<img src="'+p.url+'" style="width:100%;height:100%;object-fit:cover">'
        +'<div style="position:absolute;top:0;right:0;background:rgba(0,0,0,.7);color:#f66;cursor:pointer;padding:2px 5px;font-size:11px" onclick="deletePhoto(\''+p.id+'\')">✕</div>'
        +'<div style="position:absolute;bottom:0;left:0;right:0;background:rgba(0,0,0,.7);color:#fff;font-size:10px;text-align:center">'+( p.is_avatar?'аватар':'#'+p.order)+'</div>';
      box.appendChild(div);
    });
  });
}
function deletePhoto(pid){
  if(!confirm('Удалить фото?')) return;
  const token=getToken();
  fetch('/delete-photo',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token,photo_id:pid})})
  .then(r=>r.json()).then(d=>{
    document.getElementById('photoLog').innerText=d.ok?'Удалено':'Ошибка: '+(d.error||d.status);
    loadCurrentPhotos();
  });
}
function uploadPhotos(){
  const token=getToken(); if(!token){alert('Вставь JWT');return;}
  const files=document.getElementById('photoFiles').files;
  const maxCount=parseInt(document.getElementById('photoCount').value)||3;
  if(!files.length){alert('Выбери файлы');return;}
  const log=document.getElementById('photoLog');
  const fd=new FormData();
  fd.append('token',token);
  fd.append('order','1');
  const count=Math.min(files.length,maxCount);
  for(let i=0;i<count;i++) fd.append('photos',files[i]);
  log.innerText='Загружаю '+count+' фото...';
  fetch('/upload-photo',{method:'POST',body:fd}).then(r=>r.json()).then(data=>{
    const res=data.results||[];
    let ok=0,bad=0,badFiles=[];
    res.forEach(r=>{
      if(r.ok) ok++;
      else{ bad++; badFiles.push(r.filename+': '+(r.message||'error')); }
    });
    log.innerHTML='Загружено: '+ok+', отклонено: '+bad+(badFiles.length?'<br>'+badFiles.join('<br>'):'');
    loadCurrentPhotos();
  }).catch(e=>{log.innerText='Ошибка: '+e;});
}
document.addEventListener('DOMContentLoaded',()=>{setTimeout(loadCurrentPhotos,500);});
</script>
  </fieldset>

  <fieldset><legend>Профиль (ничего не выбрано = не трогать)</legend>
    <label class="l">О себе</label><textarea name="about">{{ f.about }}</textarea>

    <label class="l">Рост, см ({{ hmin }}–{{ hmax }})</label>
    <input type="text" name="height" value="{{ f.height }}" inputmode="numeric" style="max-width:140px">

    <label class="l">Город (начни вводить название и выбери из подсказок)</label>
    <input type="text" id="cityQ" autocomplete="off" placeholder="например: Моск">
    <input type="hidden" name="city_id" id="cityId" value="{{ f.city_id }}">
    <input type="hidden" name="city_name" id="cityName" value="{{ f.city_name }}">
    <div class="sug" id="citySug"></div>
    <div id="cityPick">{% if f.city_name %}Выбран: {{ f.city_name }}{% endif %}</div>

    <label class="l">Религия</label>
    <div class="chips">
      {% for rid, rname in religions %}
      <label class="chip"><input type="radio" name="religion_id" value="{{ rid }}" {{ 'checked' if f.religion_id==rid }}><span>{{ rname }}</span></label>
      {% endfor %}
      <label class="chip"><input type="radio" name="religion_id" value="" {{ 'checked' if not f.religion_id }}><span>не менять</span></label>
    </div>

    {% for key, title, opts in singles %}
    <label class="l">{{ title }}</label>
    <div class="chips">
      {% for v, t in opts %}
      <label class="chip"><input type="radio" name="{{ key }}" value="{{ v }}" {{ 'checked' if f[key]==v }}><span>{{ t }}</span></label>
      {% endfor %}
      <label class="chip"><input type="radio" name="{{ key }}" value="" {{ 'checked' if not f[key] }}><span>не менять</span></label>
    </div>
    {% endfor %}
    <p class="hint">Числа за кнопками образования, детей, алкоголя и курения из HAR не подтверждены (см. комментарий в app.py).</p>

    <label class="l">Языки</label>
    <input type="text" id="langFilter" placeholder="фильтр по названию">
    <div class="chips" id="langs">
      {% for lid, lname in languages %}
      <label class="chip" data-n="{{ lname|lower }}"><input type="checkbox" name="language" value="{{ lid }}" {{ 'checked' if lid in f.language }}><span>{{ lname }}</span></label>
      {% endfor %}
    </div>

    <label class="l">Интересы</label>
    {% for g, items in groups.items() %}
    <h4>{{ g }}</h4>
    <div class="chips">
      {% for iid, iname in items %}
      <label class="chip"><input type="checkbox" name="interest" value="{{ iid }}" {{ 'checked' if iid in f.interest }}><span>{{ iname }}</span></label>
      {% endfor %}
    </div>
    {% endfor %}

    <label class="l"><input type="checkbox" name="zodiac_touch" {{ 'checked' if f.zodiac_touch }}> Отправить настройку знака зодиака</label>
    <label class="l"><input type="checkbox" name="zodiac" {{ 'checked' if f.zodiac }}> Показывать знак зодиака</label>
  </fieldset>

  <label class="l"><input type="checkbox" name="dry_run" {{ 'checked' if f.dry_run }}> dry-run (ничего не отправлять, GET-запросы выполняются)</label>
  <button class="go" type="submit" name="mode" value="full">Запустить (онбординг / профиль / верификация)</button>
</form>
<script>
const $ = id => document.getElementById(id);
let timer = null;
$('cityQ').addEventListener('input', () => {
  clearTimeout(timer);
  const q = $('cityQ').value.trim();
  if (q.length < 2) { $('citySug').textContent = ''; return; }
  timer = setTimeout(async () => {
    const box = $('citySug');
    try {
      const r = await fetch('/city-search', {method: 'POST', headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({token: $('token').value.trim(), q})});
      const j = await r.json();
      box.textContent = '';
      if (j.error) { box.textContent = j.error; return; }
      if (!j.length) { box.textContent = 'ничего не найдено'; return; }
      j.slice(0, 12).forEach(c => {
        const b = document.createElement('button');
        b.type = 'button';
        b.textContent = c.name + (c.regionName ? ' (' + c.regionName + ')' : '');
        b.onclick = () => {
          $('cityId').value = c.id; $('cityName').value = c.name;
          $('cityPick').textContent = 'Выбран: ' + c.name + (c.regionName ? ' (' + c.regionName + ')' : '');
          box.textContent = '';
        };
        box.appendChild(b);
      });
    } catch (e) { box.textContent = 'ошибка поиска: ' + e; }
  }, 300);
});
$('langFilter').addEventListener('input', e => {
  const q = e.target.value.trim().toLowerCase();
  document.querySelectorAll('#langs .chip').forEach(l => { l.style.display = l.dataset.n.includes(q) ? '' : 'none'; });
});
</script>
"""

PAGE_ACCOUNTS = """
<form method="post" action="/accounts">
  <fieldset><legend>Добавить аккаунт через VK токен</legend>
    <label class="l">Метка (любое имя для себя)</label>
    <input type="text" name="label" value="" placeholder="например: main или test1" required>
    <label class="l">VK Access Token</label>
    <textarea name="vk_token" required placeholder="vk1.a.SQVIJP3j-7rv..."></textarea>
    <p class="hint">Авторизация через VK OAuth v3 (PKCE) + fallback v2. Формат: id:pass:token
      Полученный токен можно скопировать и вставить на вкладке Онбординг или Свайпы.</p>
    <button class="go" type="submit" name="action" value="add">Получить JWT</button>
  </fieldset>
</form>

{% if accounts %}
<h3 style="margin-top:24px">Сохранённые аккаунты ({{ accounts|length }})</h3>
{% for label, acc in accounts.items() %}
<div class="acct-card">
  <div class="info">
    <b>{{ label }}</b>
    {% if acc.name %} — {{ acc.name }}{% endif %}
    {% if acc.user_id %} <span style="color:#666">(id: {{ acc.user_id }})</span>{% endif %}
    <br><span class="jwt" title="{{ acc.jwt }}">JWT: {{ acc.jwt[:60] }}...</span>
  </div>
  <div style="display:flex;gap:6px;align-items:center">
    <button class="copy-btn" onclick="navigator.clipboard.writeText('{{ acc.jwt }}');this.textContent='OK!';setTimeout(()=>this.textContent='Copy JWT',1000)">Copy JWT</button>
    <form method="post" action="/accounts" style="margin:0"><input type="hidden" name="label" value="{{ label }}">
      <button class="copy-btn" type="submit" name="action" value="remove" style="color:#f55;border-color:#a33">X</button>
    </form>
  </div>
</div>
{% endfor %}
{% endif %}
"""

PAGE_SWIPE = """
<form method="post" action="/swipe">
  <label class="l">Access token (JWT)</label>
  <textarea id="swipe_token" name="token" required placeholder="Вставь JWT или скопируй из вкладки Аккаунты">{{ f.token }}</textarea>

  {% if accounts %}
  <div style="margin-top:8px">
    <span class="hint">Быстрый выбор:</span>
    {% for label, acc in accounts.items() %}
    <button type="button" class="copy-btn" style="margin:2px" onclick="document.getElementById('swipe_token').value='{{ acc.jwt }}'">{{ label }}</button>
    {% endfor %}
  </div>
  {% endif %}

  <fieldset><legend>Фильтр поиска</legend>
    <p class="hint">Локация задаётся координатами (POINT). Выбери город — координаты подставятся, или впиши вручную.</p>
    <div class="row">
      <div style="flex:2">
        <label class="l">Город (поиск)</label>
        <input type="text" id="swipe_city_name" name="city_name" value="{{ f.city_name }}" placeholder="Начни вводить город" autocomplete="off" oninput="swipeCitySearch(this.value)">
        <input type="hidden" id="swipe_city_id" name="city_id" value="{{ f.city_id }}">
        <div id="swipe_city_hints" style="max-height:160px;overflow-y:auto"></div>
      </div>
      <div>
        <label class="l">Радиус, км</label>
        <input type="number" name="distance" value="{{ f.distance or 100 }}" min="1" max="300" style="max-width:120px">
      </div>
    </div>
    <div class="row" style="margin-top:6px">
      <div>
        <label class="l">Широта (lat)</label>
        <input type="text" id="swipe_lat" name="latitude" value="{{ f.latitude }}" placeholder="55.7558" style="max-width:150px">
      </div>
      <div>
        <label class="l">Долгота (lon)</label>
        <input type="text" id="swipe_lon" name="longitude" value="{{ f.longitude }}" placeholder="37.6173" style="max-width:150px">
      </div>
    </div>
    <p class="hint" style="margin-top:2px">Популярные: Москва 55.7558/37.6173 · Питер 59.9386/30.3141 · Саратов 51.5462/46.0154</p>
    <div class="row" style="margin-top:8px">
      <div>
        <label class="l">Возраст от</label>
        <input type="number" name="min_age" value="{{ f.min_age or 18 }}" min="18" max="80" style="max-width:100px">
      </div>
      <div>
        <label class="l">до</label>
        <input type="number" name="max_age" value="{{ f.max_age or 35 }}" min="18" max="80" style="max-width:100px">
      </div>
    </div>
    <label class="l">Пол</label>
    <div class="chips">
      <label class="chip"><input type="radio" name="filter_gender" value="f" {{ 'checked' if f.filter_gender=='f' }}><span>Девушки</span></label>
      <label class="chip"><input type="radio" name="filter_gender" value="m" {{ 'checked' if f.filter_gender=='m' }}><span>Парни</span></label>
      <label class="chip"><input type="radio" name="filter_gender" value="n" {{ 'checked' if f.filter_gender=='n' or not f.filter_gender }}><span>Все</span></label>
    </div>
    <button class="go alt" type="submit" name="mode" value="set_filter" style="margin-top:12px">Применить фильтр</button>
  </fieldset>
<script>
const CITY_COORDS={
  'Moscow':[55.7558,37.6173],'Москва':[55.7558,37.6173],
  'Saint Petersburg':[59.9386,30.3141],'Санкт-Петербург':[59.9386,30.3141],'St Petersburg':[59.9386,30.3141],
  'Saratov':[51.5462,46.0154],'Саратов':[51.5462,46.0154],
  'Novosibirsk':[55.0084,82.9357],'Новосибирск':[55.0084,82.9357],
  'Yekaterinburg':[56.8389,60.6057],'Екатеринбург':[56.8389,60.6057],
  'Kazan':[55.7963,49.1088],'Казань':[55.7963,49.1088],
  'Nizhny Novgorod':[56.2965,43.9361],'Нижний Новгород':[56.2965,43.9361],
  'Chelyabinsk':[55.1644,61.4368],'Челябинск':[55.1644,61.4368],
  'Samara':[53.1959,50.1002],'Самара':[53.1959,50.1002],
  'Omsk':[54.9885,73.3242],'Омск':[54.9885,73.3242],
  'Rostov-on-Don':[47.2357,39.7015],'Ростов-на-Дону':[47.2357,39.7015],
  'Ufa':[54.7388,55.9721],'Уфа':[54.7388,55.9721],
  'Krasnoyarsk':[56.0153,92.8932],'Красноярск':[56.0153,92.8932],
  'Voronezh':[51.6683,39.1340],'Воронеж':[51.6683,39.1340],
  'Perm':[58.0105,56.2502],'Пермь':[58.0105,56.2502],
  'Volgograd':[48.7080,44.5133],'Волгоград':[48.7080,44.5133],
  'Krasnodar':[45.0353,38.9753],'Краснодар':[45.0353,38.9753],
  'Sochi':[43.5855,39.7231],'Сочи':[43.5855,39.7231],
  'Tyumen':[57.1522,65.5272],'Тюмень':[57.1522,65.5272],
};
function swipeFillCoords(name){
  const c=CITY_COORDS[name];
  if(c){document.getElementById('swipe_lat').value=c[0];document.getElementById('swipe_lon').value=c[1];}
}
let _swipeCityTimer;
function swipeCitySearch(q){
  clearTimeout(_swipeCityTimer);
  if(q.length<2){document.getElementById('swipe_city_hints').innerHTML='';return;}
  _swipeCityTimer=setTimeout(()=>{
    const token=document.getElementById('swipe_token').value;
    fetch('/city-search',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:token,q:q})})
    .then(r=>r.json()).then(data=>{
      const list=Array.isArray(data)?data:(data.results||[]);
      const box=document.getElementById('swipe_city_hints');
      box.innerHTML=list.map(c=>{
        const cid=c.id||'';const cname=(c.name||'').replace(/'/g,'');const reg=c.regionName||c.region||'';
        return `<div style="padding:4px 8px;cursor:pointer;border-bottom:1px solid #333"
              onclick="document.getElementById('swipe_city_id').value='${cid}';document.getElementById('swipe_city_name').value='${cname}';swipeFillCoords('${cname}');document.getElementById('swipe_city_hints').innerHTML=''">
          ${c.name}${reg?' <span class=hint>'+reg+'</span>':''}
        </div>`;}).join('');
    });
  },300);
}
</script>

  <fieldset><legend>Лайк / дизлайк по ID</legend>
    <label class="l">ID анкеты</label>
    <input type="text" name="action_user_id" value="{{ f.action_user_id }}" inputmode="numeric" placeholder="ID пользователя">
    <div class="actions-row">
      <button class="go" type="submit" name="mode" value="like">&#128077; Лайк</button>
      <button class="go dislike" type="submit" name="mode" value="dislike">&#128078; Дизлайк</button>
    </div>
  </fieldset>

  <fieldset><legend>Автолайк</legend>
    <p class="hint">Загружает анкеты из ленты и лайкает подряд, пока не кончатся анкеты или сервер не вернёт ошибку (лимит лайков).
      Пауза между лайками: 0.5–1.5 сек (рандом). Все действия пишутся в консоль и серверные логи.</p>
    <div class="actions-row">
      <button class="go" type="submit" name="mode" value="fetch_feed">Загрузить ленту (посмотреть)</button>
      <button class="go" type="submit" name="mode" value="auto_like" style="background:#e90;color:#000">&#9889; Автолайк до лимита</button>
    </div>
  </fieldset>
</form>

{% if feed %}
<h3 style="margin-top:24px">Лента ({{ feed|length }} анкет)</h3>
{% for u in feed %}
<div class="swipe-card" style="text-align:left;display:flex;gap:16px;align-items:center">
  {% if u.photo %}<img src="{{ u.photo }}" alt="photo" style="max-width:80px;max-height:100px;border-radius:6px">{% endif %}
  <div style="flex:1">
    <div class="name">{{ u.name or '—' }}, {{ u.age or '?' }}{% if u.compatibility %} · {{ u.compatibility }}%{% endif %}</div>
    <div class="meta">id: {{ u.id }}{% if u.city %} · {{ u.city }}{% endif %}</div>
  </div>
  <form method="post" action="/swipe" style="display:flex;gap:6px;margin:0">
    <input type="hidden" name="token" value="{{ f.token }}">
    <input type="hidden" name="action_user_id" value="{{ u.id }}">
    <button class="go" type="submit" name="mode" value="like" style="margin:0;padding:6px 14px">&#128077;</button>
    <button class="go dislike" type="submit" name="mode" value="dislike" style="margin:0;padding:6px 14px">&#128078;</button>
  </form>
</div>
{% endfor %}
{% endif %}

{% if stats %}
<div style="margin-top:20px;padding:14px;background:#1a1a1a;border:1px solid #333;border-radius:8px">
  <b style="color:#3a7">Результат автолайка:</b> {{ stats.liked }} лайков
  {% if stats.errors %}, {{ stats.errors }} ошибок{% endif %}
  {% if stats.stopped_reason %}<br><span class="hint">Остановка: {{ stats.stopped_reason }}</span>{% endif %}
</div>
{% endif %}
"""

DEFAULTS = dict(token="", profile_only=False, name="", birthday="", gender="m",
                target_id="887657566886395905", interview_id="689835470004879361",
                about="", height="", city_id="", city_name="", latitude="", longitude="", religion_id="",
                education="", children="", alcohol="", smoking="",
                language=[], interest=[], zodiac=True, zodiac_touch=False, dry_run=False,
                action_user_id="", fetch_count=10,
                distance=100, min_age=18, max_age=35, filter_gender="f")


def resolve_by_ids(wanted: list[str], items: list[dict], what: str) -> list[dict]:
    index = {str(i.get("id")): i for i in items}
    missing = [w for w in wanted if w not in index]
    if missing:
        raise RuntimeError(f"{what}: сервер не знает id {missing} (список мог измениться).")
    return [index[w] for w in wanted]


def opt_int(form, key: str, label: str, lo: int, hi: int):
    v = form.get(key, "").strip()
    if not v:
        return None
    if not v.isdigit() or not lo <= int(v) <= hi:
        raise RuntimeError(f"{label}: ожидается целое число {lo}..{hi}")
    return int(v)


def fill_profile(c, form, tee) -> None:
    """Порядок как в HAR: один PATCH с атрибутами, затем interests, затем about."""
    main: dict = {}
    about = form.get("about", "").strip()

    height = opt_int(form, "height", "Рост", HEIGHT_MIN, HEIGHT_MAX)
    if height is not None:
        main["height"] = str(height)                # HAR: строка "176"
    for key, label in (("education", "Образование"), ("children", "Дети"),
                       ("alcohol", "Алкоголь"), ("smoking", "Курение")):
        v = opt_int(form, key, label, 0, 9)
        if v is not None:
            main[key] = v
    if form.get("zodiac_touch"):
        main["showZodiacSign"] = bool(form.get("zodiac"))

    city_id, city_name = form.get("city_id", "").strip(), form.get("city_name", "").strip()
    if city_id:
        if not city_id.isdigit() or not city_name:
            raise RuntimeError("Город: выбери вариант из подсказок.")
        main["city"] = city_id
        main["nameCity"] = city_name
        tee(f"город: {city_name} id={city_id}")

    rid = form.get("religion_id", "").strip()
    if rid:
        rels = c.call("GET", API_PROFILE["religion"])
        main["religion"] = resolve_by_ids([rid], rels if isinstance(rels, list) else [], "Религия")[0]
        main["religionId"] = rid

    lang_ids = form.getlist("language")
    if lang_ids:
        langs = c.call("GET", API_PROFILE["language"])
        picked = resolve_by_ids(lang_ids, langs if isinstance(langs, list) else [], "Языки")
        main["languages"] = [{**x, "isSelected": False} for x in picked]   # форма как в HAR

    interests_body = None
    int_ids = form.getlist("interest")
    if int_ids:
        ints = c.call("GET", API_PROFILE["interest"])
        picked = resolve_by_ids(int_ids, ints if isinstance(ints, list) else [], "Интересы")
        interests_body = {"interests": [{"id": x["id"]} for x in picked]}

    if main:
        c.call("PATCH", tw.API["me"], json=main)
    if interests_body:
        c.call("PATCH", tw.API["me"], json=interests_body)
    if about:
        c.call("PATCH", tw.API["me"], json={"about": about})
    if not (main or interests_body or about):
        tee("профиль: ничего не выбрано, пропускаю")


def send_dating_action(c, user_id: str, action_type: int, tee) -> None:
    """type=0 — лайк, type=1 — дизлайк (подтверждено вручную, см. комментарий у DATING_ACTION_PATH)."""
    if not user_id:
        return
    if not user_id.isdigit():
        raise RuntimeError("ID анкеты для лайка/дизлайка должен быть числом.")
    label = "Лайк" if action_type == 0 else "Дизлайк"
    try:
        c.call("POST", DATING_ACTION_PATH, json={"user": user_id, "type": action_type})
    except RuntimeError as e:
        tee(f"❌ {label} НЕ прошёл (user={user_id}): {e}")
        return
    if getattr(c, "dry", False):
        tee(f"[dry-run] {label.lower()} не отправлен по-настоящему (user={user_id})")
    else:
        tee(f"✅ {label} поставлен (user={user_id})")


def process_liveness_video(video_bytes: bytes, tee) -> bytes:
    """Обрезает видео по лицу (по центру) и приводит к формату 720x1280 (9:16)."""
    tee("Анализ видео для верификации (поиск лица)...")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp_in:
        tmp_in.write(video_bytes)
        tmp_in_path = tmp_in.name
    tmp_out_path = tmp_in_path + "_out.mp4"

    try:
        # 1. Пытаемся найти лицо через OpenCV/Mediapipe
        face_crop_cmd = None
        try:
            import cv2
            import mediapipe as mp

            cap = cv2.VideoCapture(tmp_in_path)
            ret, frame = cap.read()
            if ret:
                mp_face = mp.solutions.face_detection
                with mp_face.FaceDetection(model_selection=0, min_detection_confidence=0.5) as fd:
                    res = fd.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                    if res.detections:
                        bbox = res.detections[0].location_data.relative_bounding_box
                        h, w = frame.shape[:2]

                        # Берем лицо и добавляем отступы (чтобы было видно плечи, как в овале)
                        cx, cy = int((bbox.xmin + bbox.width/2) * w), int((bbox.ymin + bbox.height/2) * h)
                        size = int(max(bbox.width * w, bbox.height * h) * 2.2) # Масштаб лица в кадре

                        x1 = max(0, cx - size // 2)
                        y1 = max(0, cy - size // 2)
                        x2 = min(w, cx + size // 2)
                        y2 = min(h, cy + size // 2)

                        # Обрезаем квадрат и масштабируем в 720x1280
                        face_crop_cmd = f"crop={x2-x1}:{y2-y1}:{x1}:{y1},scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2"
                        tee(f"Лицо найдено. Будет обрезано по координатам.")
            cap.release()
        except ImportError:
            tee("OpenCV/Mediapipe не установлены, пропускаю поиск лица.")
        except Exception as e:
            tee(f"Ошибка при поиске лица: {e}. Будет просто изменен размер.")

        # 2. Запускаем FFmpeg
        if face_crop_cmd:
            # Если лицо найдено — используем кастомный кроп
            vf = face_crop_cmd
        else:
            # Если лицо не найдено — просто растягиваем/подгоняем без искажений пропорций
            vf = "scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2"

        cmd = [
            "ffmpeg", "-y", "-i", tmp_in_path,
            "-vf", vf,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac",
            tmp_out_path
        ]

        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        with open(tmp_out_path, "rb") as f:
            return f.read()

    finally:
        try:
            if os.path.exists(tmp_in_path):
                os.remove(tmp_in_path)
        except PermissionError:
            pass

        if os.path.exists(tmp_out_path):
            os.remove(tmp_out_path)


def send_liveness(c, video_file, tee) -> None:
    """Отправка liveness-видео через curl_cffi.

    Сервер Twinby за ~6ms проверяет метаданные MP4-контейнера и отклоняет
    видео, не похожее на запись Android MediaRecorder (FFmpeg encoder tags,
    неправильный ftyp, отсутствие creation_time, наличие аудиодорожки и т.д.).

    Конвертер пересобирает видео: 640x480, Baseline profile, 30fps, без аудио,
    moov перед mdat, ftyp как у Android, зачищенные метаданные.
    """
    if not video_file or not video_file.filename:
        return

    raw_bytes = video_file.read()
    if not raw_bytes:
        raise RuntimeError("Видео для верификации пустое.")
    if len(raw_bytes) > LIVENESS_MAX_BYTES:
        raise RuntimeError(f"Видео больше {LIVENESS_MAX_BYTES // 1_000_000} МБ.")

    # Конвертируем в формат Android MediaRecorder
    from convert_to_android import convert_for_liveness
    vflip = getattr(c, "_vflip", False)
    tee("Конвертация видео в формат Android MediaRecorder" + (" + vflip" if vflip else "") + "...")
    video_bytes = convert_for_liveness(raw_bytes, debug_print=tee, vflip=vflip)
    tee(f"Видео после конвертации: {len(video_bytes)//1024} КБ")

    # --- авторизация ---
    auth_header = c.http.headers.get("authorization") or c.http.headers.get("Authorization")
    if not auth_header:
        raise RuntimeError("Нет Authorization в сессии — сначала авторизуйся.")
    if not auth_header.startswith("JWT "):
        auth_header = "JWT " + auth_header.replace("Bearer ", "")

    # --- имя файла и idempotency ---
    fname = f"REC{time.time_ns()}.mp4"
    idem = str(uuid.uuid4())

    # --- sentry-trace ---
    trace_id = uuid.uuid4().hex
    span_id = uuid.uuid4().hex[:16]
    sentry_trace = f"{trace_id}-{span_id}-0"

    # --- boundary в стиле Dart (base64 с / → .) ---
    raw_b = uuid.uuid4().bytes + uuid.uuid4().bytes + uuid.uuid4().bytes[:5]
    bstr = base64.b64encode(raw_b).rstrip(b"=").decode("ascii").replace("/", ".")
    boundary = f"dart-http-boundary-{bstr}"

    # --- multipart body: content-type ПЕРЕД content-disposition, lowercase ---
    header_part = (
        f"--{boundary}\r\n"
        f"content-type: video/mp4\r\n"
        f'content-disposition: form-data; name="data"; filename="{fname}"\r\n'
        f"\r\n"
    ).encode("utf-8")
    footer_part = f"\r\n--{boundary}--\r\n".encode("utf-8")
    body = header_part + video_bytes + footer_part

    url = f"{tw.BASE}{LIVENESS_PATH}"

    tee(f"POST {LIVENESS_PATH} (curl_cffi, {len(video_bytes)//1024} КБ, idem {idem})")

    if getattr(c, "dry", False):
        tee("[dry-run] верификация не отправлена по-настоящему")
        return

    # Заголовки в порядке реального Dart-клиента (из mitmproxy дампа успешного 200)
    from collections import OrderedDict
    headers = OrderedDict([
        ("Host", "twinby.ru"),
        ("Connection", "keep-alive"),
        ("user-agent", "Dart/3.11 (dart:io)"),
        ("accept", "application/json"),
        ("accept-language", "en"),
        ("accept-encoding", "gzip"),
        ("authorization", auth_header),
        ("idempotency-key", idem),
        ("sentry-trace", sentry_trace),
        ("content-type", f"multipart/form-data; boundary={boundary}"),
        ("Content-Length", str(len(body))),
    ])

    # Прокси через mitmproxy для диагностики (убрать после отладки)
    _proxy = os.environ.get("LIVENESS_PROXY")  # например: http://127.0.0.1:8080
    _proxies = {"https": _proxy, "http": _proxy} if _proxy else None

    max_attempts = 1 if _proxy else 3
    for attempt in range(1, max_attempts + 1):
        try:
            resp = cffi_requests.post(
                url,
                headers=headers,
                data=body,
                timeout=60,
                proxies=_proxies,
                verify=not bool(_proxy),
            )
            status = resp.status_code
            resp_body = resp.text
            proc_time = resp.headers.get("x-process-time", "?")
        except Exception as e:
            tee(f"⚠️ Попытка {attempt}/{max_attempts}: сетевая ошибка: {e}")
            if attempt < max_attempts:
                time.sleep(3)
            continue

        if status < 400:
            tee(f"✅ Верификация пройдена (попытка {attempt}/{max_attempts}, статус {status}), "
                f"process-time {proc_time}s, ответ: {resp_body[:300]}")
            return

        tee(f"⚠️ Попытка {attempt}/{max_attempts}: статус {status} "
            f"(process-time {proc_time}s): {resp_body[:300]}")
        if attempt < max_attempts:
            time.sleep(3)

    tee(f"❌ Верификация НЕ пройдена после {max_attempts} попыток")


def _make_web_client(token: str, dry: bool, tee, proxy: str = ""):
    """Создаёт WebClient с логированием. Если proxy задан — все запросы через него."""
    class WebClient(tw.Client):
        def call(self, method, path, **kw):
            shown = path + ("?" + "&".join(f"{k}={v}" for k, v in kw["params"].items()) if kw.get("params") else "")
            tee(f"{method} {shown}" + (" [dry-run]" if self.dry and method != "GET" else ""))
            if self.dry and method != "GET":
                return {}
            r = self.http.request(method, path, **kw)
            if r.status_code >= 400:
                raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
            return r.json() if r.content else {}

    if "refreshToken" in token or '"' in token:
        raise RuntimeError("В поле токена попал лишний хвост (refreshToken). Вставь только первый JWT.")
    c = WebClient(dry)
    # Если нужен прокси — пересоздаём httpx.Client с proxy
    proxy = _normalize_proxy(proxy)
    if proxy:
        import httpx as _httpx
        c.http = _httpx.Client(
            base_url=tw.BASE, timeout=30, proxy=proxy,
            headers={"User-Agent": tw.UA, "Accept": "application/json, text/plain, */*",
                     **tw.ORIGIN_HEADERS},
        )
    c.auth(token)
    return c


def _render(template_str: str, tab: str, log_buf, error=None, **extra):
    """Рендерит страницу: сначала Jinja2 для контента, потом вставляет в layout."""
    from markupsafe import Markup
    singles = [("education", "Образование", EDUCATION), ("children", "Дети", CHILDREN),
               ("alcohol", "Алкоголь", HABIT), ("smoking", "Курение", HABIT)]
    content_html = render_template_string(
        template_str,
        religions=RELIGIONS, singles=singles,
        languages=LANGUAGES, groups=INTEREST_GROUPS,
        hmin=HEIGHT_MIN, hmax=HEIGHT_MAX,
        accounts=ACCOUNTS,
        **extra
    )
    log_text = log_buf.getvalue() if log_buf else None
    return _page(tab, Markup(content_html), log=log_text, error=error)


# ---- Вкладка: Аккаунты ----
@app.route("/accounts", methods=["GET", "POST"])
def accounts_page():
    log_buf = io.StringIO()
    tee = lambda msg: log_buf.write(msg + "\n")
    error = None

    if request.method == "POST":
        action = request.form.get("action")
        label = request.form.get("label", "").strip()

        if action == "remove" and label in ACCOUNTS:
            del ACCOUNTS[label]
            _save_accounts(ACCOUNTS)
            tee(f"Аккаунт '{label}' удалён")

        elif action == "add":
            vk_token = request.form.get("vk_token", "").strip()
            vk_login = request.form.get("vk_login", "").strip()
            vk_pass = request.form.get("vk_pass", "").strip()
            if not label:
                error = "Укажи метку для аккаунта"
            elif not vk_login and not vk_token:
                error = "Нужен VK логин+пароль (или токен для legacy)"
            else:
                try:
                    import httpx
                    tee(f"Auth VK → JWT (v3 PKCE)")
                    settings = _load_settings()
                    rucaptcha_key = settings.get("rucaptcha_key", "")
                    proxy = _get_next_proxy(settings)
                    auth = _auth_vk_to_jwt(vk_login=vk_login, vk_pass=vk_pass,
                                           vk_token=vk_token, rucaptcha_key=rucaptcha_key,
                                           proxy=proxy)
                    for line in auth.get("log", []):
                        tee(line)
                    if not auth.get("ok"):
                        raise RuntimeError(auth.get("error", "Auth failed"))
                    jwt_token = auth["jwt"]

                    # Попробуем получить имя и ID профиля
                    user_name = ""
                    user_id = ""
                    try:
                        with _make_proxied_httpx_client(proxy) as hc:
                            hc.headers["Authorization"] = f"JWT {jwt_token}"
                            me_resp = hc.get(f"{tw.BASE}{tw.API['me']}")
                        if me_resp.status_code < 400:
                            me_data = me_resp.json()
                            user_name = me_data.get("name", "")
                            user_id = str(me_data.get("id", ""))
                    except Exception:
                        pass

                    ACCOUNTS[label] = {
                        "vk_token": vk_token,
                        "jwt": jwt_token,
                        "user_id": user_id,
                        "name": user_name,
                        "proxy": proxy,
                    }
                    _save_accounts(ACCOUNTS)
                    tee(f"Аккаунт '{label}' добавлен (JWT получен, id={user_id}, name={user_name})")
                    tee(f"JWT: {jwt_token[:80]}...")
                except Exception as e:
                    tee(f"ERROR: {e}")
                    tee(traceback.format_exc(limit=3))

    return _render(PAGE_ACCOUNTS, "accounts", log_buf, error, f={})


def _parse_feed(result) -> list[dict]:
    """Парсит ответ /api/users/search/?size=50 в унифицированный список.
    Формат подтверждён mitmproxy 2026-11-21:
      {"results": [{"id","name","ages","nameCity","avatar":{"preview":url},...}]}
    """
    items = []
    if isinstance(result, list):
        raw = result
    elif isinstance(result, dict):
        raw = result.get("results", result.get("data", []))
        if not isinstance(raw, list):
            raw = []
    else:
        raw = []
    for item in raw:
        u = item if isinstance(item, dict) else {}
        uid = str(u.get("id", u.get("userId", "")))
        if not uid:
            continue
        name = u.get("name", "")
        age = u.get("ages", u.get("age", ""))  # API отдаёт "ages"
        city = u.get("nameCity", u.get("city", ""))
        compat = u.get("compatibility", "")

        # Аватар: avatar.preview (основная), fallback на photos[0]
        photo_url = ""
        avatar = u.get("avatar")
        if isinstance(avatar, dict):
            photo_url = avatar.get("preview", avatar.get("file", ""))
        if not photo_url:
            photos = u.get("photos", [])
            if photos and isinstance(photos, list) and isinstance(photos[0], dict):
                photo_url = photos[0].get("url", photos[0].get("src", ""))

        items.append({
            "id": uid, "name": name, "age": age, "city": city,
            "photo": photo_url, "compatibility": compat,
        })
    return items


# ---- Вкладка: Свайпы ----
@app.route("/swipe", methods=["GET", "POST"])
def swipe_page():
    import random
    f = dict(DEFAULTS)
    log_buf = io.StringIO()
    tee = lambda msg: log_buf.write(msg + "\n")
    feed = []
    stats = None

    if request.method == "POST":
        f["token"] = request.form.get("token", "").strip().strip('"\'')
        f["action_user_id"] = request.form.get("action_user_id", "").strip()
        f["distance"] = request.form.get("distance", "100").strip()
        f["min_age"] = request.form.get("min_age", "18").strip()
        f["max_age"] = request.form.get("max_age", "35").strip()
        f["filter_gender"] = request.form.get("filter_gender", "f").strip()
        f["city_id"] = request.form.get("city_id", "").strip()
        f["city_name"] = request.form.get("city_name", "").strip()
        f["latitude"] = request.form.get("latitude", "").strip()
        f["longitude"] = request.form.get("longitude", "").strip()
        mode = request.form.get("mode", "")

        try:
            _proxy = request.form.get("proxy", "").strip() or _get_next_proxy()
            c = _make_web_client(f["token"], False, tee, proxy=_proxy)

            if mode == "set_filter":
                # 1) Обновляем location (координаты) + город в профиле
                me_body = {}
                lat_s, lon_s = f.get("latitude", ""), f.get("longitude", "")
                if lat_s and lon_s:
                    try:
                        lat_f, lon_f = float(lat_s), float(lon_s)
                        me_body["location"] = f"POINT({lat_f} {lon_f})"
                        tee(f"Обновляю location: POINT({lat_f} {lon_f})")
                    except ValueError:
                        tee("Ошибка: некорректные координаты")
                if f["city_id"] and f["city_name"]:
                    me_body["city"] = f["city_id"]
                    me_body["nameCity"] = f["city_name"]
                    tee(f"Обновляю город: {f['city_name']} (id={f['city_id']})")
                if me_body:
                    c.call("PATCH", tw.API["me"], json=me_body)

                # 2) Обновляем фильтр поиска
                filter_body = {
                    "minAges": int(f["min_age"]),
                    "maxAges": int(f["max_age"]),
                    "distance": int(f["distance"]),
                    "gender": f["filter_gender"],
                }
                tee(f"Обновляю фильтр: {json.dumps(filter_body)}")
                c.call("PATCH", tw.API["filter"], json=filter_body)
                tee("Фильтр, локация и город обновлены")

            elif mode in ("like", "dislike"):
                uid = f["action_user_id"]
                action_type = 0 if mode == "like" else 1
                label = "LIKE" if action_type == 0 else "DISLIKE"
                send_dating_action(c, uid, action_type, tee)
                print(f"[SWIPE] {label} user={uid}")

            elif mode == "fetch_feed":
                tee("Загружаю ленту...")
                result = c.call("GET", DATING_SEARCH_PATH)
                feed = _parse_feed(result)
                for u in feed:
                    tee(f"  {u['name']}, {u['age']} — id={u['id']}" + (f", {u['city']}" if u['city'] else ""))
                tee(f"Загружено {len(feed)} анкет")
                if not feed:
                    tee(f"Сырой ответ: {json.dumps(result, ensure_ascii=False)[:600]}")
                print(f"[FEED] Loaded {len(feed)} profiles")

            elif mode == "auto_like":
                tee("Автолайк: загружаю ленту и лайкаю подряд до лимита...")
                liked = 0
                errors = 0
                stopped_reason = ""
                rounds = 0
                max_rounds = 50  # максимум 50 подгрузок ленты

                while rounds < max_rounds:
                    rounds += 1
                    try:
                        result = c.call("GET", DATING_SEARCH_PATH)
                    except RuntimeError as e:
                        stopped_reason = f"Ошибка загрузки ленты: {e}"
                        tee(stopped_reason)
                        break

                    batch = _parse_feed(result)
                    if not batch:
                        stopped_reason = "Лента пуста — анкеты закончились или достигнут лимит"
                        tee(stopped_reason)
                        break

                    tee(f"Раунд {rounds}: загружено {len(batch)} анкет")

                    for u in batch:
                        uid = u["id"]
                        try:
                            c.call("POST", DATING_ACTION_PATH, json={"user": uid, "type": 0})
                            liked += 1
                            tee(f"  LIKE #{liked}: {u['name']}, {u['age']} (id={uid})")
                            print(f"[SWIPE] LIKE #{liked} user={uid} ({u['name']}, {u['age']})")
                        except RuntimeError as e:
                            err_str = str(e)
                            errors += 1
                            tee(f"  FAIL: {u['name']} (id={uid}): {err_str[:200]}")
                            # Если 429 или похожее на лимит — останавливаемся
                            if "429" in err_str or "limit" in err_str.lower() or "too many" in err_str.lower():
                                stopped_reason = f"Лимит лайков достигнут ({err_str[:100]})"
                                tee(stopped_reason)
                                break
                            if errors >= 5:
                                stopped_reason = f"Слишком много ошибок подряд ({errors})"
                                tee(stopped_reason)
                                break

                        # Рандомная пауза 0.5–1.5 сек
                        time.sleep(random.uniform(0.5, 1.5))

                    if stopped_reason:
                        break

                tee(f"Итого: {liked} лайков, {errors} ошибок, {rounds} раундов")
                stats = {"liked": liked, "errors": errors, "stopped_reason": stopped_reason}

        except Exception as e:
            tee(f"ERROR: {e}")
            tee(traceback.format_exc(limit=3))

    return _render(PAGE_SWIPE, "swipe", log_buf, feed=feed, f=f, stats=stats)


# ---- Вкладка: Онбординг (старая, перенесена на /old) ----
@app.route("/old", methods=["GET", "POST"])
def index():
    f = dict(DEFAULTS)
    log_buf = io.StringIO()
    tee = lambda msg: log_buf.write(msg + "\n")

    if request.method == "GET":
        return _render(PAGE_MAIN, "main", log_buf, f=f)

    form = request.form
    for k in ("token", "name", "birthday", "gender", "target_id", "interview_id", "about", "height",
              "city_id", "city_name", "religion_id", "education", "children", "alcohol", "smoking",
              "action_user_id"):
        f[k] = form.get(k, "").strip()
    f["token"] = f["token"].strip('"\'')
    f["language"] = form.getlist("language")
    f["interest"] = form.getlist("interest")
    for k in ("profile_only", "zodiac", "zodiac_touch", "dry_run"):
        f[k] = bool(form.get(k))
    dry = f["dry_run"]
    mode = form.get("mode", "full")

    try:
        _proxy = form.get("proxy", "").strip() or _get_next_proxy()
        if not f["profile_only"] and not form.get("accept_twinby_terms"):
            raise RuntimeError("Confirm Twinby's Privacy Policy, User Agreement, and 18+ requirement before onboarding.")

        if not f["token"]:
            vk_login = form.get("vk_id", "").strip()
            vk_pass = form.get("vk_pass", "")
            if not vk_login or not vk_pass:
                raise RuntimeError("Paste a Twinby JWT or provide both VK login and password.")
            settings = _load_settings()
            auth = _auth_vk_to_jwt(
                vk_login=vk_login,
                vk_pass=vk_pass,
                rucaptcha_key=settings.get("rucaptcha_key", ""),
                proxy=_proxy,
            )
            for line in auth.get("log", []):
                tee(line)
            if not auth.get("ok"):
                raise RuntimeError(auth.get("error", "VK browser authorization failed."))
            f["token"] = auth["jwt"]
            tee("[AUTH] Twinby JWT acquired; continuing with onboarding.")

        c = _make_web_client(f["token"], dry, tee, proxy=_proxy)

        if not f["profile_only"]:
            photo_file = request.files.get("photo")
            if not f["name"] or not f["birthday"] or not photo_file or not photo_file.filename:
                raise RuntimeError("Для онбординга нужны имя, дата рождения и фото.")
            mime = MIME.get(Path(photo_file.filename).suffix.lower())
            if not mime:
                raise RuntimeError("Фото должно быть .webp/.jpg/.png")
            photo_bytes = photo_file.read()
            if not photo_bytes or len(photo_bytes) > 10_000_000:
                raise RuntimeError("Фото пустое или больше 10 МБ.")
            photo_name = Path(photo_file.filename).name
            try:
                birthday_iso = tw.valid_birthday(f["birthday"])
            except (SystemExit, ValueError) as e:
                raise RuntimeError(f"Дата рождения: {e}")

            cfg = c.call("GET", tw.API["captcha_cfg"])
            if isinstance(cfg, dict) and cfg.get("isActive"):
                raise RuntimeError("Капча включена на сайте, этот путь её не проходит.")

            if not f["target_id"] or not f["interview_id"]:
                tee("targets: " + json.dumps(c.call("GET", tw.API["targets"]), ensure_ascii=False)[:1500])
                tee("interviews: " + json.dumps(c.call("GET", tw.API["interviews"]), ensure_ascii=False)[:1500])
                return _render(PAGE_MAIN, "main", log_buf, error="Укажи target_id и interview_id из списков выше и запусти снова.", f=f)

            # -- Онбординг: точная копия реального трафика (mitmproxy 2026-11-21) --
            c.call("POST", tw.API["agreement"])
            birthday_app = birthday_iso + "T00:00:00.000" if "T" not in birthday_iso else birthday_iso

            c.call("PATCH", tw.API["me"], json={"name": f["name"], "onboardingStep": 1})
            c.call("PATCH", tw.API["me"], json={"birthday": birthday_app, "onboardingStep": 2})
            c.call("PATCH", tw.API["me"], json={"gender": f["gender"], "onboardingStep": 3})
            c.call("PATCH", tw.API["me"], json={"targetId": f["target_id"], "onboardingStep": 4})

            c.call("POST", tw.API["photo"], data={"order": PHOTO_ORDER},
                   files={"file": (photo_name, photo_bytes, mime)})
            c.call("PATCH", tw.API["me"], json={"onboardingStep": 5})
            c.call("PATCH", tw.API["me"], json={"onboardingStep": 6})

            qs = c.call("GET", tw.API["questions"].format(iid=f["interview_id"]))
            if not dry:
                if not isinstance(qs, list) or not qs:
                    raise RuntimeError("Пустой список вопросов интервью.")
                c.call("POST", tw.API["answers"],
                       json={"test": [{"question": q["id"], "answer": 4} for q in qs]})
            c.call("PATCH", tw.API["me"], json={"onboardingCompleted": True})

        fill_profile(c, form, tee)
        c._vflip = bool(form.get("vflip"))
        send_liveness(c, request.files.get("liveness_video"), tee)

        me = c.call("GET", tw.API["me"])
        if isinstance(me, dict) and me:
            keys = ("id", "name", "onboardingCompleted", "profileProgress", "height", "nameCity",
                    "education", "children", "alcohol", "smoking", "moderationStatus")
            tee("done: " + json.dumps({k: me.get(k) for k in keys}, ensure_ascii=False))
    except Exception as e:
        tee("ERROR: " + str(e))
        tee(traceback.format_exc(limit=3))

    return _render(PAGE_MAIN, "main", log_buf, f=f)


@app.route("/city-search", methods=["POST"])
def city_search():
    data = request.get_json(force=True)
    token = (data.get("token") or "").strip().strip('"\'')
    q = (data.get("q") or "").strip()
    if not token or not q:
        return jsonify({"error": "need token and q"})
    try:
        proxy = (data.get("proxy") or "").strip() or _get_next_proxy()
        with _make_proxied_httpx_client(proxy) as hc:
            hc.headers["Authorization"] = f"JWT {token}"
            resp = hc.get(
                f"{tw.BASE}{API_PROFILE['city']}",
                params={"name": q, "hasUsers": "false"},
            )
        if resp.status_code >= 400:
            return jsonify({"error": f"status {resp.status_code}"})
        return jsonify(resp.json())
    except Exception as e:
        return jsonify({"error": str(e)})


# ---- API: загрузка фото ----
@app.route("/upload-photo", methods=["POST"])
def upload_photo():
    """Загрузить фото в анкету. multipart: token, order, file(s)"""
    token = (request.form.get("token") or "").strip().strip('"\'')
    if not token:
        return jsonify({"error": "need token"})
    order = int(request.form.get("order", 1))
    files = request.files.getlist("photos")
    if not files:
        return jsonify({"error": "no files"})

    proxy = (request.form.get("proxy") or "").strip() or _get_next_proxy()
    results = []
    for i, f in enumerate(files):
        cur_order = order + i
        try:
            file_bytes = f.read()
            with _make_proxied_httpx_client(proxy) as hc:
                hc.headers["Authorization"] = f"JWT {token}"
                resp = hc.post(
                    f"{tw.BASE}/api/photo/",
                    files={"file": (f.filename or f"photo_{cur_order}.jpg", file_bytes, f.content_type or "image/jpeg")},
                    data={"order": str(cur_order), "type": "photo"},
                )
            if resp.status_code == 201:
                r = resp.json()
                results.append({"ok": True, "id": r.get("id"), "order": r.get("order"),
                                "file": r.get("file"), "filename": f.filename})
            elif resp.status_code == 403:
                body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                results.append({"ok": False, "status": 403,
                                "message": body.get("Message", "Bad photo"),
                                "filename": f.filename, "order": cur_order})
            else:
                results.append({"ok": False, "status": resp.status_code,
                                "message": resp.text[:200], "filename": f.filename})
        except Exception as e:
            results.append({"ok": False, "message": str(e), "filename": f.filename})

    return jsonify({"results": results})


@app.route("/delete-photo", methods=["POST"])
def delete_photo():
    """Удалить фото. JSON: {token, photo_id}"""
    data = request.get_json(force=True)
    token = (data.get("token") or "").strip().strip('"\'')
    photo_id = (data.get("photo_id") or "").strip()
    if not token or not photo_id:
        return jsonify({"error": "need token and photo_id"})
    try:
        proxy = (data.get("proxy") or "").strip() or _get_next_proxy()
        with _make_proxied_httpx_client(proxy) as hc:
            hc.headers["Authorization"] = f"JWT {token}"
            resp = hc.delete(f"{tw.BASE}/api/photo/{photo_id}/")
        return jsonify({"ok": resp.status_code == 204, "status": resp.status_code})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/reorder-photo", methods=["POST"])
def reorder_photo():
    """Сменить порядок / заменить фото. multipart: token, photo_id, order, file (optional)"""
    token = (request.form.get("token") or "").strip().strip('"\'')
    photo_id = (request.form.get("photo_id") or "").strip()
    new_order = request.form.get("order", "0")
    if not token or not photo_id:
        return jsonify({"error": "need token and photo_id"})
    try:
        proxy = (request.form.get("proxy") or "").strip() or _get_next_proxy()
        kw = {"data": {"order": new_order, "type": "photo"}}
        f = request.files.get("file")
        if f:
            kw["files"] = {"file": (f.filename, f.read(), f.content_type or "image/jpeg")}
        with _make_proxied_httpx_client(proxy) as hc:
            hc.headers["Authorization"] = f"JWT {token}"
            resp = hc.patch(
                f"{tw.BASE}/api/photo/{photo_id}/",
                **kw,
            )
        if resp.status_code in (200, 201):
            return jsonify({"ok": True, **resp.json()})
        else:
            body = {}
            try:
                body = resp.json()
            except Exception:
                pass
            return jsonify({"ok": False, "status": resp.status_code,
                            "message": body.get("Message", resp.text[:200])})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/my-photos", methods=["POST"])
def my_photos():
    """Получить список фото анкеты. JSON: {token}"""
    data = request.get_json(force=True)
    token = (data.get("token") or "").strip().strip('"\'')
    if not token:
        return jsonify({"error": "need token"})
    try:
        proxy = (data.get("proxy") or "").strip() or _get_next_proxy()
        with _make_proxied_httpx_client(proxy) as hc:
            hc.headers["Authorization"] = f"JWT {token}"
            resp = hc.get(f"{tw.BASE}/api/users/me/")
        me = resp.json()
        avatar = me.get("avatar")
        photos = me.get("photos", [])
        result = []
        if avatar and isinstance(avatar, dict):
            result.append({"id": avatar.get("id"), "url": avatar.get("preview") or avatar.get("file"),
                           "order": avatar.get("order", 0), "is_avatar": True})
        for p in photos:
            if isinstance(p, dict):
                result.append({"id": p.get("id"), "url": p.get("preview") or p.get("file"),
                               "order": p.get("order"), "is_avatar": False})
        return jsonify({"photos": result})
    except Exception as e:
        return jsonify({"error": str(e)})


# ---- API: пресеты (server-side JSON storage) ----
PRESETS_FILE = Path(__file__).parent / "presets.json"

def _load_presets():
    if PRESETS_FILE.exists():
        try:
            return json.loads(PRESETS_FILE.read_text())
        except Exception:
            return {}
    return {}

def _save_presets(data):
    PRESETS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))

@app.route("/api/presets", methods=["GET"])
def get_presets():
    return jsonify(_load_presets())

@app.route("/api/presets", methods=["POST"])
def save_presets():
    data = request.get_json(force=True)
    _save_presets(data)
    return jsonify({"ok": True})


@app.route("/preset-register", methods=["POST"])
def preset_register():
    """Зарегать анкету через VK → JWT (v3 PKCE)."""
    import httpx
    data = request.get_json(force=True)
    vk_token = (data.get("vk_token") or "").strip()
    vk_login = (data.get("vk_id") or "").strip()
    vk_pass = (data.get("vk_pass") or "").strip()

    settings = _load_settings()
    rucaptcha_key = settings.get("rucaptcha_key", "")
    proxy = data.get("proxy") or _get_next_proxy(settings)

    if not vk_login:
        return jsonify({"ok": False, "error": "Нужен VK логин и пароль"})

    try:
        auth = _auth_vk_to_jwt(vk_login=vk_login, vk_pass=vk_pass,
                               vk_token=vk_token, rucaptcha_key=rucaptcha_key,
                               proxy=proxy)
        if not auth.get("ok"):
            return jsonify({"ok": False, "error": auth.get("error", "Auth failed"),
                            "log": auth.get("log", [])})
        jwt_token = auth["jwt"]

        # Получим имя и ID (через прокси)
        user_name = ""
        user_id = ""
        try:
            with _make_proxied_httpx_client(proxy) as hc:
                hc.headers["Authorization"] = f"JWT {jwt_token}"
                me_resp = hc.get(f"{tw.BASE}{tw.API['me']}")
                if me_resp.status_code < 400:
                    me = me_resp.json()
                    user_name = me.get("name", "")
                    user_id = str(me.get("id", ""))
        except Exception:
            pass

        # Если переданы координаты — сразу ставим локацию (через прокси)
        lat_s = str(data.get("latitude", "")).strip()
        lon_s = str(data.get("longitude", "")).strip()
        if lat_s and lon_s:
            try:
                with _make_proxied_httpx_client(proxy) as hc:
                    hc.headers["Authorization"] = f"JWT {jwt_token}"
                    hc.headers["Content-Type"] = "application/json"
                    hc.patch(f"{tw.BASE}/api/users/me/",
                             json={"location": f"POINT({lat_s} {lon_s})"})
            except Exception:
                pass

        return jsonify({"ok": True, "jwt": jwt_token, "user_id": user_id, "name": user_name,
                        "proxy": proxy, "auth_log": auth.get("log", [])})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/apply-preset", methods=["POST"])
def apply_preset():
    """Применить пресет: location + city + filter через прокси."""
    data = request.get_json(force=True)
    token = (data.get("token") or "").strip().strip('"\'')
    proxy = (data.get("proxy") or "").strip()
    p = data.get("preset", {})
    if not token:
        return jsonify({"ok": False, "log": "Нет токена"})

    log_lines = []

    # 1) PATCH /api/users/me/ — location + city
    me_body = {}
    lat_s = str(p.get("latitude", "")).strip()
    lon_s = str(p.get("longitude", "")).strip()
    if lat_s and lon_s:
        me_body["location"] = f"POINT({lat_s} {lon_s})"
    city_name = p.get("city_name", "")
    if city_name:
        me_body["nameCity"] = city_name
    if me_body:
        try:
            with _make_proxied_httpx_client(proxy) as hc:
                hc.headers.update({"Authorization": f"JWT {token}", "Content-Type": "application/json"})
                r = hc.patch(f"{tw.BASE}/api/users/me/", json=me_body)
            log_lines.append(f"Профиль: {r.status_code}")
        except Exception as e:
            log_lines.append(f"Профиль ошибка: {e}")

    # 2) PATCH /api/users/me/filter/ — фильтры
    filter_body = {}
    if p.get("min_age"):
        filter_body["minAges"] = int(p["min_age"])
    if p.get("max_age"):
        filter_body["maxAges"] = int(p["max_age"])
    if p.get("distance"):
        filter_body["distance"] = int(p["distance"])
    if p.get("filter_gender"):
        filter_body["gender"] = p["filter_gender"]
    if filter_body:
        try:
            with _make_proxied_httpx_client(proxy) as hc:
                hc.headers.update({"Authorization": f"JWT {token}", "Content-Type": "application/json"})
                r = hc.patch(f"{tw.BASE}/api/users/me/filter/", json=filter_body)
            log_lines.append(f"Фильтр: {r.status_code}")
        except Exception as e:
            log_lines.append(f"Фильтр ошибка: {e}")

    log_lines.append("Готово!")
    return jsonify({"ok": True, "log": "\n".join(log_lines)})


# ---- Вкладка: Пресеты ----
PAGE_PRESETS = r"""
<h2>Пресеты</h2>
<p class="hint">Пресет = персона (имя + возраст + пометка). Внутри — анкеты по городам.
  Каждая анкета регистрируется через VK и получает свой JWT. Клик по анкете — детали + действия.</p>

<div style="display:flex;gap:12px;margin-bottom:16px;flex-wrap:wrap;align-items:end">
  <div style="flex:1;min-width:140px"><label class="l">Имя пресета</label>
    <input type="text" id="newPresetName" placeholder="Артём, 25 (визи)"></div>
  <div style="min-width:200px"><label class="l">VK данные <span class="hint">(id:pass:token)</span></label>
    <input type="text" id="newPresetVk" placeholder="48450464782:pass:vk1.a.SQV..."></div>
  <button class="go" style="margin-top:0;height:38px" onclick="createPreset()">+ Создать пресет</button>
</div>

<div id="presetsList"></div>
<div id="presetDetail" style="display:none"></div>
<div id="presetLog" style="margin-top:12px"></div>

<script>
let PRESETS={};
let OPEN_PRESET=null;
let OPEN_ANKETA=null;

function loadPresets(){
  fetch('/api/presets').then(r=>r.json()).then(data=>{
    PRESETS=data||{};
    renderPresets();
  });
}

function savePresets(cb){
  fetch('/api/presets',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(PRESETS)})
  .then(r=>r.json()).then(d=>{if(cb)cb();});
}

function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');}

function createPreset(){
  const name=document.getElementById('newPresetName').value.trim();
  const vk=document.getElementById('newPresetVk').value.trim();
  if(!name){alert('Введи имя пресета');return;}
  if(PRESETS[name]){alert('Пресет с таким именем уже есть');return;}
  let vk_id='',vk_pass='',vk_token='';
  if(vk){
    const parts=vk.split(':');
    if(parts.length>=3){vk_id=parts[0];vk_pass=parts[1];vk_token=parts.slice(2).join(':');}
    else if(parts.length==2){vk_id=parts[0];vk_token=parts[1];}
    else{vk_token=vk;}
  }
  PRESETS[name]={vk_id:vk_id,vk_pass:vk_pass,vk_token:vk_token,ankety:{}};
  savePresets(renderPresets);
  document.getElementById('newPresetName').value='';
  document.getElementById('newPresetVk').value='';
}

function deletePreset(name){
  if(!confirm('Удалить пресет "'+name+'" со всеми анкетами?'))return;
  delete PRESETS[name];
  if(OPEN_PRESET===name){OPEN_PRESET=null;OPEN_ANKETA=null;document.getElementById('presetDetail').style.display='none';}
  savePresets(renderPresets);
}

function openPreset(name){
  OPEN_PRESET=name;OPEN_ANKETA=null;
  renderPresetDetail();
}

function renderPresets(){
  const box=document.getElementById('presetsList');
  let html='';
  for(const[name,p]of Object.entries(PRESETS)){
    const anketaCount=Object.keys(p.ankety||{}).length;
    const cls=OPEN_PRESET===name?'border-color:#3a7':'';
    html+='<div class="acct-card" style="cursor:pointer;'+cls+'" onclick="openPreset(\''+esc(name)+'\')">'
      +'<div class="info"><b>'+esc(name)+'</b>'
      +'<br><span style="font-size:12px;color:#888">VK: '+(p.vk_id?esc(p.vk_id):'не задан')
      +' · Анкет: '+anketaCount+'</span></div>'
      +'<div style="display:flex;gap:6px;align-items:center">'
      +'<button class="copy-btn" onclick="event.stopPropagation();deletePreset(\''+esc(name)+'\')" style="color:#f55;border-color:#a33">X</button>'
      +'</div></div>';
  }
  if(!html) html='<p class="hint">Нет пресетов. Создай первый.</p>';
  box.innerHTML=html;
  if(OPEN_PRESET && PRESETS[OPEN_PRESET]) renderPresetDetail();
  else document.getElementById('presetDetail').style.display='none';
}

function renderPresetDetail(){
  const d=document.getElementById('presetDetail');
  if(!OPEN_PRESET||!PRESETS[OPEN_PRESET]){d.style.display='none';return;}
  d.style.display='block';
  const p=PRESETS[OPEN_PRESET];
  const ankety=p.ankety||{};

  let html='<fieldset><legend>'+esc(OPEN_PRESET)+'</legend>';
  html+='<div class="row"><div><label class="l">VK ID</label><input type="text" value="'+esc(p.vk_id)+'" onchange="updP(\'vk_id\',this.value)"></div>'
    +'<div><label class="l">VK пароль</label><input type="text" value="'+esc(p.vk_pass)+'" onchange="updP(\'vk_pass\',this.value)"></div>'
    +'<div style="flex:2"><label class="l">VK токен</label><input type="text" value="'+esc(p.vk_token)+'" onchange="updP(\'vk_token\',this.value)"></div></div>';

  // --- список анкет ---
  html+='<h4 style="margin-top:16px">Анкеты ('+Object.keys(ankety).length+')</h4>';
  for(const[city,a]of Object.entries(ankety)){
    const sel=OPEN_ANKETA===city?'background:#222;border-color:#3a7':'';
    const likes=a.likes_done||0;
    html+='<div class="acct-card" style="cursor:pointer;margin-top:6px;'+sel+'" onclick="openAnketa(\''+esc(city)+'\')">'
      +'<div class="info"><b>'+esc(city)+'</b>'
      +'<br><span style="font-size:12px;color:#888">'
      +(a.name?esc(a.name)+' · ':'')
      +'лайков: '+likes
      +(a.jwt?' · JWT: '+esc(a.jwt.substring(0,30))+'...':'')
      +'</span></div>'
      +'<button class="copy-btn" onclick="event.stopPropagation();deleteAnketa(\''+esc(city)+'\')" style="color:#f55;border-color:#a33">X</button>'
      +'</div>';
  }

  // --- добавить анкету ---
  html+='<div style="display:flex;gap:8px;margin-top:10px;align-items:end">'
    +'<div style="flex:1"><label class="l">Город для новой анкеты</label><input type="text" id="newAnketaCity" placeholder="Москва"></div>'
    +'<div><label class="l">Широта</label><input type="text" id="newAnketaLat" placeholder="55.7558" style="max-width:100px"></div>'
    +'<div><label class="l">Долгота</label><input type="text" id="newAnketaLon" placeholder="37.6173" style="max-width:100px"></div>'
    +'<button class="go" style="margin-top:0;height:38px" onclick="addAnketa()">+ Анкета</button></div>';

  // --- кнопка регистрации всех ---
  if(Object.keys(ankety).length>0){
    html+='<div style="margin-top:12px;display:flex;gap:8px">'
      +'<button class="go" onclick="registerAllAnkety()">Зарегать все анкеты (VK→JWT)</button>'
      +'</div>';
  }

  html+='</fieldset>';

  // --- деталь открытой анкеты ---
  if(OPEN_ANKETA && ankety[OPEN_ANKETA]){
    const a=ankety[OPEN_ANKETA];
    html+='<fieldset style="margin-top:12px"><legend>Анкета: '+esc(OPEN_ANKETA)+'</legend>';
    html+='<div class="row">'
      +'<div><label class="l">JWT</label><textarea style="min-height:50px;font-size:11px" readonly>'+esc(a.jwt||'не получен')+'</textarea>'
      +'<button class="copy-btn" style="margin-top:4px" onclick="navigator.clipboard.writeText(\''+esc(a.jwt||'')+'\')">Copy JWT</button></div></div>';
    html+='<div class="row" style="margin-top:8px">'
      +'<div><label class="l">VK ID</label><input type="text" value="'+esc(p.vk_id)+'" readonly></div>'
      +'<div><label class="l">VK пароль</label><input type="text" value="'+esc(p.vk_pass)+'" readonly></div>'
      +'<div><label class="l">User ID (twinby)</label><input type="text" value="'+esc(a.user_id||'')+'" readonly></div></div>';
    html+='<div class="row" style="margin-top:8px">'
      +'<div><label class="l">Имя в анкете</label><input type="text" value="'+esc(a.name||'')+'" readonly></div>'
      +'<div><label class="l">Город</label><input type="text" value="'+esc(OPEN_ANKETA)+'" readonly></div>'
      +'<div><label class="l">Лайков</label><input type="text" value="'+(a.likes_done||0)+'" readonly></div></div>';
    html+='<div class="row" style="margin-top:8px">'
      +'<div><label class="l">Координаты</label><input type="text" value="'+esc((a.latitude||'')+', '+(a.longitude||''))+'" readonly></div>'
      +'<div><label class="l">Радиус</label><input type="number" value="'+(a.distance||100)+'" onchange="updA(\'distance\',parseInt(this.value))"></div>'
      +'<div><label class="l">Возраст</label><div style="display:flex;gap:4px"><input type="number" value="'+(a.min_age||18)+'" onchange="updA(\'min_age\',parseInt(this.value))" style="width:60px"><span style="color:#666;padding-top:8px">-</span><input type="number" value="'+(a.max_age||35)+'" onchange="updA(\'max_age\',parseInt(this.value))" style="width:60px"></div></div>'
      +'<div><label class="l">Пол</label><select onchange="updA(\'filter_gender\',this.value)">'
      +'<option value="f"'+(a.filter_gender==="f"?' selected':'')+'>Девушки</option>'
      +'<option value="m"'+(a.filter_gender==="m"?' selected':'')+'>Парни</option>'
      +'<option value="n"'+(a.filter_gender==="n"?' selected':'')+'>Все</option></select></div></div>';

    html+='<div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap">'
      +'<button class="go alt" onclick="applyAnketa()">Применить (локация + фильтр)</button>'
      +'<button class="go alt" onclick="registerAnketa(\''+esc(OPEN_ANKETA)+'\')">Зарегать / обновить JWT</button>'
      +'</div>';
    html+='</fieldset>';
  }

  d.innerHTML=html;
}

function updP(field,value){
  if(!OPEN_PRESET||!PRESETS[OPEN_PRESET])return;
  PRESETS[OPEN_PRESET][field]=value;
  savePresets();
}

function updA(field,value){
  if(!OPEN_PRESET||!OPEN_ANKETA||!PRESETS[OPEN_PRESET])return;
  const a=PRESETS[OPEN_PRESET].ankety;
  if(!a[OPEN_ANKETA])return;
  a[OPEN_ANKETA][field]=value;
  savePresets();
}

function addAnketa(){
  if(!OPEN_PRESET||!PRESETS[OPEN_PRESET])return;
  const city=document.getElementById('newAnketaCity').value.trim();
  const lat=document.getElementById('newAnketaLat').value.trim();
  const lon=document.getElementById('newAnketaLon').value.trim();
  if(!city){alert('Введи город');return;}
  if(!PRESETS[OPEN_PRESET].ankety) PRESETS[OPEN_PRESET].ankety={};
  if(PRESETS[OPEN_PRESET].ankety[city]){alert('Анкета в '+city+' уже есть');return;}
  PRESETS[OPEN_PRESET].ankety[city]={jwt:'',user_id:'',name:'',
    latitude:lat,longitude:lon,distance:100,min_age:18,max_age:35,filter_gender:'f',
    likes_done:0};
  savePresets(renderPresetDetail);
  document.getElementById('newAnketaCity').value='';
}

function deleteAnketa(city){
  if(!confirm('Удалить анкету "'+city+'"?'))return;
  delete PRESETS[OPEN_PRESET].ankety[city];
  if(OPEN_ANKETA===city)OPEN_ANKETA=null;
  savePresets(renderPresetDetail);
}

function openAnketa(city){OPEN_ANKETA=city;renderPresetDetail();}

function logMsg(msg){
  const el=document.getElementById('presetLog');
  el.style.cssText='padding:8px;background:#1a1a1a;border-radius:6px;font-size:13px;white-space:pre-wrap';
  el.innerText+=(el.innerText?'\n':'')+msg;
}

function registerAnketa(city){
  const p=PRESETS[OPEN_PRESET];
  if(!p||(!p.vk_token&&!p.vk_id)){alert('Укажи VK данные (id:pass:token) в пресете');return;}
  logMsg('Регистрирую анкету '+city+'...');
  return fetch('/preset-register',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({vk_token:p.vk_token||'',vk_id:p.vk_id||'',vk_pass:p.vk_pass||'',
      city:city,
      latitude:(p.ankety[city]||{}).latitude||'',
      longitude:(p.ankety[city]||{}).longitude||''})})
  .then(r=>r.json()).then(d=>{
    if(d.ok){
      const a=PRESETS[OPEN_PRESET].ankety[city];
      a.jwt=d.jwt||'';a.user_id=d.user_id||'';a.name=d.name||'';
      savePresets(renderPresetDetail);
      logMsg(city+': JWT получен ('+d.user_id+' '+d.name+')');
      if(d.auth_log)d.auth_log.forEach(l=>logMsg('  '+l));
    }else{
      logMsg(city+': ошибка — '+(d.error||'unknown'));
      if(d.log)d.log.forEach(l=>logMsg('  '+l));
    }
  }).catch(e=>logMsg(city+': fetch error — '+e));
}

async function registerAllAnkety(){
  const p=PRESETS[OPEN_PRESET];
  if(!p)return;
  for(const city of Object.keys(p.ankety||{})){
    await registerAnketa(city);
  }
}

function applyAnketa(){
  if(!OPEN_PRESET||!OPEN_ANKETA)return;
  const a=PRESETS[OPEN_PRESET].ankety[OPEN_ANKETA];
  if(!a||!a.jwt){alert('Сначала зарегай анкету (получи JWT)');return;}
  logMsg('Применяю '+OPEN_ANKETA+'...');
  fetch('/apply-preset',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({token:a.jwt,preset:a})})
  .then(r=>r.json()).then(d=>{logMsg(OPEN_ANKETA+': '+(d.log||'done'));})
  .catch(e=>logMsg(OPEN_ANKETA+': '+e));
}

document.addEventListener('DOMContentLoaded',loadPresets);
</script>
"""

@app.route("/presets")
def presets_page():
    return _render(PAGE_PRESETS, "presets", None, f={})


# ---- Передаём ACCOUNTS в JS для пресетов ----
_orig_render = _render.__wrapped__ if hasattr(_render, '__wrapped__') else None

@app.after_request
def inject_accounts_js(response):
    if response.content_type and 'text/html' in response.content_type:
        js = f'<script>window._ACCOUNTS={json.dumps(ACCOUNTS, ensure_ascii=False)};</script>'
        data = response.get_data(as_text=True)
        data = data.replace('</body>', js + '</body>')
        response.set_data(data)
    return response


# ===================== JSON API для нового фронта =====================

@app.route("/api/accounts", methods=["GET"])
def api_accounts_list():
    """Список всех аккаунтов."""
    return jsonify(ACCOUNTS)


@app.route("/api/accounts", methods=["POST"])
def api_accounts_add():
    """Добавить аккаунт через VK. JSON: {label, vk_token, vk_id?, vk_pass?}"""
    data = request.get_json(force=True)
    label = (data.get("label") or "").strip()
    vk_token = (data.get("vk_token") or "").strip()
    vk_login = (data.get("vk_id") or "").strip()
    vk_pass = (data.get("vk_pass") or "").strip()
    if not label or not vk_login:
        return jsonify({"ok": False, "error": "Нужен label и VK логин+пароль"})

    settings = _load_settings()
    rucaptcha_key = settings.get("rucaptcha_key", "")
    proxy = _get_next_proxy(settings)

    try:
        auth = _auth_vk_to_jwt(vk_login=vk_login, vk_pass=vk_pass,
                               vk_token=vk_token, rucaptcha_key=rucaptcha_key,
                               proxy=proxy)
        if not auth.get("ok"):
            return jsonify({"ok": False, "error": auth.get("error", "Auth failed"),
                            "log": auth.get("log", [])})
        jwt_token = auth["jwt"]

        user_name, user_id = "", ""
        try:
            with _make_proxied_httpx_client(proxy) as hc:
                hc.headers["Authorization"] = f"JWT {jwt_token}"
                me_resp = hc.get(f"{tw.BASE}{tw.API['me']}")
            if me_resp.status_code < 400:
                me = me_resp.json()
                user_name = me.get("name", "")
                user_id = str(me.get("id", ""))
        except Exception:
            pass
        ACCOUNTS[label] = {"vk_token": vk_token, "jwt": jwt_token,
                           "user_id": user_id, "name": user_name, "proxy": proxy}
        _save_accounts(ACCOUNTS)
        return jsonify({"ok": True, "jwt": jwt_token, "user_id": user_id, "name": user_name, "proxy": proxy})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/accounts/<label>", methods=["DELETE"])
def api_accounts_remove(label):
    if label in ACCOUNTS:
        del ACCOUNTS[label]
        _save_accounts(ACCOUNTS)
    return jsonify({"ok": True})


@app.route("/api/feed", methods=["POST"])
def api_feed():
    """Загрузить ленту. JSON: {token}"""
    data = request.get_json(force=True)
    token = (data.get("token") or "").strip().strip('"\'')
    if not token:
        return jsonify({"ok": False, "error": "need token"})
    try:
        proxy = (data.get("proxy") or "").strip() or _get_next_proxy()
        log_buf = io.StringIO()
        c = _make_web_client(token, False, lambda m: log_buf.write(m + "\n"), proxy=proxy)
        result = c.call("GET", DATING_SEARCH_PATH)
        feed = _parse_feed(result)
        return jsonify({"ok": True, "feed": feed, "count": len(feed)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/action", methods=["POST"])
def api_action():
    """Лайк/дизлайк. JSON: {token, user_id, type: 0|1}"""
    data = request.get_json(force=True)
    token = (data.get("token") or "").strip().strip('"\'')
    user_id = str(data.get("user_id", ""))
    action_type = int(data.get("type", 0))
    if not token or not user_id:
        return jsonify({"ok": False, "error": "need token and user_id"})
    try:
        proxy = (data.get("proxy") or "").strip() or _get_next_proxy()
        log_buf = io.StringIO()
        c = _make_web_client(token, False, lambda m: log_buf.write(m + "\n"), proxy=proxy)
        c.call("POST", DATING_ACTION_PATH, json={"user": user_id, "type": action_type})
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/auto-like", methods=["POST"])
def api_auto_like():
    """Автолайк. JSON: {token, max_rounds?, proxy?}"""
    import random as rnd
    data = request.get_json(force=True)
    token = (data.get("token") or "").strip().strip('"\'')
    proxy = (data.get("proxy") or "").strip()
    max_rounds = int(data.get("max_rounds", 50))
    if not token:
        return jsonify({"ok": False, "error": "need token"})
    try:
        log_buf = io.StringIO()
        c = _make_web_client(token, False, lambda m: log_buf.write(m + "\n"), proxy=proxy)
        liked, errors, rounds = 0, 0, 0
        stopped_reason = ""
        while rounds < max_rounds:
            rounds += 1
            try:
                result = c.call("GET", DATING_SEARCH_PATH)
            except RuntimeError as e:
                stopped_reason = f"Ошибка ленты: {e}"
                break
            batch = _parse_feed(result)
            if not batch:
                stopped_reason = "Лента пуста"
                break
            for u in batch:
                try:
                    c.call("POST", DATING_ACTION_PATH, json={"user": u["id"], "type": 0})
                    liked += 1
                except RuntimeError as e:
                    errors += 1
                    if "429" in str(e) or "limit" in str(e).lower():
                        stopped_reason = f"Лимит лайков"
                        break
                    if errors >= 5:
                        stopped_reason = "Много ошибок"
                        break
                time.sleep(rnd.uniform(0.5, 1.5))
            if stopped_reason:
                break
        return jsonify({"ok": True, "liked": liked, "errors": errors,
                        "rounds": rounds, "stopped_reason": stopped_reason})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/profile", methods=["POST"])
def api_profile():
    """Получить профиль. JSON: {token, proxy?}"""
    data = request.get_json(force=True)
    token = (data.get("token") or "").strip().strip('"\'')
    proxy = (data.get("proxy") or "").strip()
    if not token:
        return jsonify({"ok": False, "error": "need token"})
    try:
        with _make_proxied_httpx_client(proxy) as hc:
            hc.headers["Authorization"] = f"JWT {token}"
            resp = hc.get(f"{tw.BASE}{tw.API['me']}")
        if resp.status_code >= 400:
            return jsonify({"ok": False, "error": f"Статус {resp.status_code}"})
        return jsonify({"ok": True, "profile": resp.json()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ---- Новый SPA-фронт ----
# ===================== НОВЫЙ SPA =====================

SETTINGS_FILE = Path(__file__).parent / "settings.json"
PRESETS_FILE = Path(__file__).parent / "presets.json"

def _load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try: return json.loads(SETTINGS_FILE.read_text())
        except Exception: return {}
    return {}

def _save_settings(data: dict):
    SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


@app.route("/")
def spa_root():
    """Главная — SPA из index.html в корне проекта."""
    html_path = Path(__file__).parent / "index.html"
    return html_path.read_text(encoding="utf-8"), 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    return jsonify(_load_settings())

@app.route("/api/settings", methods=["POST"])
def api_settings_save():
    data = request.get_json(force=True)
    _save_settings(data)
    return jsonify({"ok": True})


@app.route("/api/cities", methods=["GET"])
def api_cities():
    return jsonify(get_cities_json())


@app.route("/api/register-account", methods=["POST"])
def api_register_account():
    """VK browser authorization followed by Twinby onboarding and profile setup."""
    data = request.get_json(force=True)
    vk_token = (data.get("vk_token") or "").strip()
    vk_login = (data.get("vk_id") or "").strip()
    vk_pass = (data.get("vk_pass") or "").strip()
    if data.get("accept_twinby_terms") is not True:
        return jsonify({"ok": False, "error": "Confirm Twinby's Privacy Policy, User Agreement, and 18+ requirement."}), 400
    if not vk_login or not vk_pass:
        return jsonify({"ok": False, "error": "VK ID login and password are required."}), 400

    name = str(data.get("name") or "").strip()
    birthday_raw = str(data.get("birthday") or "").strip()
    gender = data.get("gender", "m")
    photo_data = str(data.get("first_photo_data_url") or "")
    photo_name = Path(str(data.get("first_photo_name") or "photo.jpg")).name
    if not name or not birthday_raw:
        return jsonify({"ok": False, "error": "Name and date of birth are required."}), 400
    if gender not in ("m", "f"):
        return jsonify({"ok": False, "error": "Gender must be m or f."}), 400
    try:
        birthday_iso = tw.valid_birthday(birthday_raw)
    except (SystemExit, ValueError) as e:
        return jsonify({"ok": False, "error": f"Date of birth: {e}"}), 400

    match = re.fullmatch(r"data:image/(jpeg|png|webp);base64,([A-Za-z0-9+/]+={0,2})", photo_data)
    if not match:
        return jsonify({"ok": False, "error": "Add a first photo in JPEG, PNG, or WebP format."}), 400
    photo_mime = "image/" + match.group(1)
    try:
        photo_bytes = base64.b64decode(match.group(2), validate=True)
    except (ValueError, base64.binascii.Error):
        return jsonify({"ok": False, "error": "Could not decode the first photo."}), 400
    if not photo_bytes or len(photo_bytes) > 10_000_000:
        return jsonify({"ok": False, "error": "The first photo is empty or exceeds 10 MB."}), 400
    extension_by_mime = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
    if MIME.get(Path(photo_name).suffix.lower()) != photo_mime:
        photo_name = "photo" + extension_by_mime[photo_mime]

    settings = _load_settings()
    rucaptcha_key = settings.get("rucaptcha_key", "")
    proxy = data.get("proxy") or _get_next_proxy(settings)
    log_buf = io.StringIO()
    try:
        auth = _auth_vk_to_jwt(vk_login=vk_login, vk_pass=vk_pass,
                               vk_token=vk_token, rucaptcha_key=rucaptcha_key,
                               proxy=proxy)
        if not auth.get("ok"):
            return jsonify({"ok": False, "error": auth.get("error", "VK authorization failed."),
                            "log": auth.get("log", [])})
        jwt_token = auth["jwt"]
        for line in auth.get("log", []):
            log_buf.write(line + "\n")
        tee = lambda message: log_buf.write(message + "\n")
        c = _make_web_client(jwt_token, False, tee, proxy=proxy)

        captcha_cfg = c.call("GET", tw.API["captcha_cfg"])
        if isinstance(captcha_cfg, dict) and captcha_cfg.get("isActive"):
            return jsonify({"ok": False, "error": "Twinby CAPTCHA is active; onboarding stopped before profile changes.",
                            "jwt": jwt_token, "log": log_buf.getvalue()}), 409

        interview_id = str(data.get("interview_id") or DEFAULTS["interview_id"])
        target_id = str(data.get("target_id") or DEFAULTS["target_id"])
        targets = c.call("GET", tw.API["targets"])
        if not isinstance(targets, list) or not targets:
            raise RuntimeError("Twinby returned no onboarding targets.")
        valid_target_ids = {str(item.get("id")) for item in targets
                            if isinstance(item, dict) and item.get("id") is not None}
        if target_id not in valid_target_ids:
            raise RuntimeError("Configured target_id is absent from Twinby's current target list.")

        def call_step(label, method, endpoint, **kwargs):
            result = c.call(method, endpoint, **kwargs)
            tee(f"  OK: {label}")
            return result

        call_step("agreement", "POST", tw.API["agreement"])
        birthday_app = birthday_iso + "T00:00:00.000" if "T" not in birthday_iso else birthday_iso
        call_step("name+step1", "PATCH", tw.API["me"], json={"name": name, "onboardingStep": 1})
        call_step("birthday+step2", "PATCH", tw.API["me"], json={"birthday": birthday_app, "onboardingStep": 2})
        call_step("gender+step3", "PATCH", tw.API["me"], json={"gender": gender, "onboardingStep": 3})
        call_step("target+step4", "PATCH", tw.API["me"], json={"targetId": target_id, "onboardingStep": 4})
        call_step("first photo", "POST", tw.API["photo"], data={"order": PHOTO_ORDER},
                  files={"file": (photo_name, photo_bytes, photo_mime)})
        call_step("step5", "PATCH", tw.API["me"], json={"onboardingStep": 5})
        call_step("step6", "PATCH", tw.API["me"], json={"onboardingStep": 6})
        questions = c.call("GET", tw.API["questions"].format(iid=interview_id))
        if not isinstance(questions, list) or not questions or any(not isinstance(q, dict) or "id" not in q for q in questions):
            raise RuntimeError("Twinby returned an empty or invalid interview question list.")
        # Existing HAR-derived implementation uses answer=4 for every item; confirm semantics in a real account.
        call_step("interview answers (HAR-derived answer=4)", "POST", tw.API["answers"],
                  json={"test": [{"question": q["id"], "answer": 4} for q in questions]})
        call_step("onboardingCompleted", "PATCH", tw.API["me"], json={"onboardingCompleted": True})

        profile_body = {}
        if data.get("height"):
            profile_body["height"] = str(data["height"])
        for key in ("education", "children", "alcohol", "smoking"):
            if data.get(key) not in (None, ""):
                profile_body[key] = int(data[key])
        if data.get("about"):
            profile_body["about"] = data["about"]
        if profile_body:
            call_step("profile fields", "PATCH", tw.API["me"], json=profile_body)

        lat, lon = data.get("latitude"), data.get("longitude")
        if lat is not None and lon is not None:
            loc_body = {"location": f"POINT({lat} {lon})"}
            if data.get("city_name"):
                loc_body["nameCity"] = data["city_name"]
            call_step("location", "PATCH", tw.API["me"], json=loc_body)

        filter_body = {}
        if data.get("min_age"): filter_body["minAges"] = int(data["min_age"])
        if data.get("max_age"): filter_body["maxAges"] = int(data["max_age"])
        if data.get("distance"): filter_body["distance"] = int(data["distance"])
        if data.get("filter_gender"): filter_body["gender"] = data["filter_gender"]
        if filter_body:
            call_step("filters", "PATCH", tw.API["filter"], json=filter_body)

        me = c.call("GET", tw.API["me"])
        if not isinstance(me, dict) or me.get("onboardingCompleted") is not True:
            raise RuntimeError("Twinby did not confirm onboardingCompleted=true in the profile readback.")
        user_id = str(me.get("id", ""))
        return jsonify({"ok": True, "jwt": jwt_token, "user_id": user_id,
                        "name": me.get("name", name), "proxy": proxy,
                        "first_photo_uploaded": True, "log": log_buf.getvalue()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "log": log_buf.getvalue()})


@app.route("/api/verify-video", methods=["POST"])
def api_verify_video():
    """Верификация через видео. FormData: token + video + proxy?."""
    token = request.form.get("token", "").strip().strip('"\'')
    proxy = request.form.get("proxy", "").strip()
    video_file = request.files.get("video")
    if not token or not video_file:
        return jsonify({"ok": False, "error": "Нужен token и video"})

    log_buf = io.StringIO()
    tee = lambda m: log_buf.write(m + "\n")
    try:
        c = _make_web_client(token, False, tee, proxy=proxy)
        send_liveness(c, video_file, tee)

        # Проверяем verified после отправки (через прокси)
        verified = False
        try:
            with _make_proxied_httpx_client(proxy) as hc:
                hc.headers["Authorization"] = f"JWT {token}"
                me_resp = hc.get(f"{tw.BASE}{tw.API['me']}")
            if me_resp.status_code < 400:
                me = me_resp.json()
                verified = me.get("verified", False)
        except Exception:
            pass

        return jsonify({"ok": True, "verified": verified, "log": log_buf.getvalue()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "verified": False, "log": log_buf.getvalue()})


@app.route("/api/check-status", methods=["POST"])
def api_check_status():
    """Проверка верификации и премиума. JSON: {jwt, proxy?}."""
    data = request.get_json(force=True)
    jwt = (data.get("jwt") or "").strip().strip('"\'')
    proxy = (data.get("proxy") or "").strip()
    if not jwt:
        return jsonify({"ok": False, "error": "Нет JWT"})

    try:
        with _make_proxied_httpx_client(proxy) as hc:
            hc.headers["Authorization"] = f"JWT {jwt}"
            resp = hc.get(f"{tw.BASE}{tw.API['me']}")
        if resp.status_code >= 400:
            return jsonify({"ok": False, "error": f"Статус {resp.status_code}"})

        me = resp.json()
        verified = me.get("verified", False)
        premium_expire = me.get("premiumExpire")  # null = нет, "2026-12-05T..." = есть
        subscription_status = me.get("subscriptionStatus")  # null = нет, 0 = активна

        premium = premium_expire is not None and premium_expire != ""

        return jsonify({
            "ok": True,
            "verified": verified,
            "premium": premium,
            "premium_expire": premium_expire,
            "subscription_status": subscription_status
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/recover-account", methods=["POST"])
def api_recover_account():
    """Восстановление: VK → JWT (v3 PKCE) → /api/users/me/ → данные аккаунта."""
    import httpx
    data = request.get_json(force=True)
    vk_token = (data.get("vk_token") or "").strip()
    vk_login = (data.get("vk_id") or "").strip()
    vk_pass = (data.get("vk_pass") or "").strip()

    if not vk_login:
        return jsonify({"ok": False, "error": "Нужен VK логин и пароль"})

    settings = _load_settings()
    rucaptcha_key = settings.get("rucaptcha_key", "")
    proxy = data.get("proxy") or _get_next_proxy(settings)

    try:
        auth = _auth_vk_to_jwt(vk_login=vk_login, vk_pass=vk_pass,
                               vk_token=vk_token, rucaptcha_key=rucaptcha_key,
                               proxy=proxy)
        if not auth.get("ok"):
            return jsonify({"ok": False, "error": auth.get("error", "Auth failed"),
                            "log": auth.get("log", [])})
        jwt_token = auth["jwt"]

        # Get account info (через прокси)
        with _make_proxied_httpx_client(proxy) as hc:
            hc.headers["Authorization"] = f"JWT {jwt_token}"
            me_resp = hc.get(f"{tw.BASE}{tw.API['me']}")
        me = me_resp.json() if me_resp.status_code < 400 else {}

        verified = me.get("verified", False)
        premium_expire = me.get("premiumExpire")
        premium = premium_expire is not None and premium_expire != ""
        name = me.get("name", "")
        user_id = str(me.get("id", ""))

        # Extract location — format may be "POINT(lat lon)" or "POINT (lat lon)"
        city_name = me.get("city", "") or me.get("cityName", "") or ""
        location = me.get("location") or ""
        lat, lon = 0, 0
        if location:
            import re
            m = re.search(r'POINT\s*\(\s*([-\d.]+)\s+([-\d.]+)\s*\)', str(location))
            if m:
                lat, lon = float(m.group(1)), float(m.group(2))

        return jsonify({
            "ok": True,
            "jwt": jwt_token,
            "user_id": user_id,
            "name": name,
            "verified": verified,
            "premium": premium,
            "premium_expire": premium_expire,
            "latitude": lat,
            "longitude": lon,
            "city_name": city_name,
            "proxy": proxy,
            "raw_location": str(location)[:200],
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/rucaptcha-balance", methods=["POST"])
def api_rucaptcha_balance():
    """Проверка баланса RuCaptcha. JSON: {key}."""
    import httpx
    data = request.get_json(force=True)
    key = (data.get("key") or "").strip()
    if not key:
        return jsonify({"ok": False, "error": "Нет API ключа"})

    try:
        proxy = _get_next_proxy()
        kw = {"timeout": 10}
        if proxy:
            kw["proxy"] = proxy
        resp = httpx.get(
            f"https://rucaptcha.com/res.php?key={key}&action=getbalance&json=1",
            **kw,
        )
        d = resp.json()
        if d.get("status") == 1:
            return jsonify({"ok": True, "balance": d.get("request", "?")})
        else:
            return jsonify({"ok": False, "error": d.get("request", "Неизвестная ошибка")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/test-vk-auth", methods=["POST"])
def api_test_vk_auth():
    """Тест VK auth v3: проверить получение кода + JWT без онбординга.
    JSON: {vk_id, vk_pass} или {vk_token}"""
    data = request.get_json(force=True)
    vk_login = (data.get("vk_id") or "").strip()
    vk_pass = (data.get("vk_pass") or "").strip()
    vk_token = (data.get("vk_token") or "").strip()

    settings = _load_settings()
    rucaptcha_key = settings.get("rucaptcha_key", "")

    if not vk_login and not vk_token:
        return jsonify({"ok": False, "error": "Нужен vk_id+vk_pass или vk_token"})

    proxy = (data.get("proxy") or "").strip() or _get_next_proxy(settings)
    result = _auth_vk_to_jwt(vk_login=vk_login, vk_pass=vk_pass,
                             vk_token=vk_token, rucaptcha_key=rucaptcha_key,
                             proxy=proxy)
    return jsonify(result)


@app.route("/api/test-v2-auth", methods=["POST"])
def api_test_v2_auth():
    """Тест VK Direct Auth + Twinby v2.
    JSON: {vk_id, vk_pass} и/или {vk_token}, опционально {proxy}
    Если vk_token есть — сразу на v2.
    Если нет — Direct Auth → v2."""
    data = request.get_json(force=True)
    vk_login = (data.get("vk_id") or "").strip()
    vk_pass = (data.get("vk_pass") or "").strip()
    vk_token = (data.get("vk_token") or "").strip()

    settings = _load_settings()
    rucaptcha_key = settings.get("rucaptcha_key", "")

    if not vk_login and not vk_token:
        return jsonify({"ok": False, "error": "Нужен vk_id+vk_pass или vk_token"})

    proxy = (data.get("proxy") or "").strip() or _get_next_proxy(settings)

    result = vk_auth.full_auth_v2(
        login=vk_login,
        password=vk_pass,
        rucaptcha_key=rucaptcha_key,
        proxy=proxy,
        vk_token=vk_token,
    )
    return jsonify(result)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
