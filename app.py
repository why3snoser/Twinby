#!/usr/bin/env python3
"""
Локальный веб-интерфейс поверх twinby_onboard.py (только localhost).
Онбординг + заполнение профиля + лайк/дизлайк по ID. Все варианты выбираются кнопками, город ищется подсказками.
Порядок и тела запросов профиля скопированы из HAR (PATCH /api/users/me/ со страницы /app/profile/info).

Запуск:
    pip install flask httpx curl_cffi
    python app.py
Потом открой: http://127.0.0.1:5000
"""
from __future__ import annotations

import subprocess
import tempfile
import os
import io
import json
import time
import traceback
import uuid
import base64
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request, redirect, send_from_directory
from curl_cffi import requests as cffi_requests
from curl_cffi import CurlHttpVersion
import httpx

# This copy lives one directory below the shared project helpers.
PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import twinby_onboard as tw
from cities import RUSSIAN_CITIES, get_cities_json

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

# Пул фото для анкет (вкладка Настройки): любое кол-во файлов, при регистрации
# новой анкеты софт случайно берёт одно, если сервер отклоняет ("Bad photo") —
# пробует следующее случайное, пока не найдёт подходящее или не исчерпает пул.
PHOTO_POOL_DIR = Path(__file__).parent / "photo_pool"
PHOTO_POOL_DIR.mkdir(exist_ok=True)
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
VIDEO_POOL_DIR = Path(__file__).parent / "video_pool"
VIDEO_POOL_DIR.mkdir(exist_ok=True)
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

AUTH_VK_PATH = "/api/auth/v2/oauth/by-token"   # прямой VK access token → JWT
DATING_SEARCH_PATH = "/api/users/search/?size=50"  # GET → список анкет для свайпа (confirmed via mitmproxy)
USERS_PUBLIC_PATH = "/api/users/{uid}/"           # GET → публичный профиль по ID

# Чаты/мэтчи — все 3 эндпоинта подтверждены реальным трафиком через mitmproxy
# 2026-11-23 (Frida-редирект на этом же билде Twinby):
CHATS_LIST_PATH = "/api/chats/list/"              # GET ?size=&page= → {count,next,previous,results:[{id,interlocutor,lastMessage:{ownerId,...},unreadMessagesCount,...}]}
CHAT_MESSAGES_PATH_TMPL = "/api/chats/chat/{chat_id}/message/"  # GET ?page=&size= → история; POST {id(uuid),text,attaches:[]} → отправка (201)
# Снято через Frida-перехват 2026-11-26: мэтчи без переписки ("New matches" в
# приложении) НЕ попадают в CHATS_LIST_PATH вообще (проверено — список из
# CHATS_LIST_PATH пустых чатов не содержит). Они отдаются отдельным
# эндпоинтом ниже, формат ответа такой же (results[]), но без lastMessage —
# вместо него есть interlocutor/compatibility/distance/isNew.
EMPTY_CHATS_PATH = "/api/chats/chat/empty/v2/"   # GET ?size=&page= → {count,newEmptyChats,results:[{id,createdAt,interlocutor,compatibility,distance,isNew}]}

# Хранилище аккаунтов — файл, переживает рестарт. Формат: {label: {vk_token, jwt, user_id, name}}
ACCOUNTS_FILE = Path(__file__).parent / "accounts.json"
SETTINGS_FILE = Path(__file__).parent / "onboarding_settings.json"
# Пул сырых VK access token'ов, вставленных в Настройках списком. Формат: список
# {"token": str, "used": bool, "user_id": str, "name": str, "error": str|None}.
# Регистрация (вкладка Пресеты) берёт отсюда неиспользованные токены и превращает их
# в Twinby-аккаунты (JWT) через тот же /api/auth/v2/oauth/by-token, что уже подтверждён
# рабочим в этом инструменте (AUTH_VK_PATH ниже).
VK_POOL_FILE = Path(__file__).parent / "vk_pool.json"

SETTING_TEXT_FIELDS = ("name", "birthday", "gender", "target_id", "interview_id", "about", "height",
                      "city_id", "city_name", "religion_id", "education", "children", "alcohol", "smoking")

def _load_onboarding_settings() -> dict:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}

def _save_onboarding_settings(data: dict) -> None:
    SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def _load_accounts() -> dict:
    if ACCOUNTS_FILE.exists():
        try:
            return json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def _save_accounts(data: dict):
    ACCOUNTS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

ACCOUNTS: dict[str, dict] = _load_accounts()


def _load_vk_pool() -> list[dict]:
    if VK_POOL_FILE.exists():
        try:
            data = json.loads(VK_POOL_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []
    return []


def _save_vk_pool(data: list[dict]) -> None:
    VK_POOL_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


VK_POOL: list[dict] = _load_vk_pool()


def _vk_pool_stats() -> dict:
    total = len(VK_POOL)
    used = sum(1 for t in VK_POOL if t.get("used"))
    return {"total": total, "used": used, "available": total - used}


def _parse_vk_pool_line(line: str) -> dict:
    """Формат строки: id:pass:token (как в пуле GitHub-версии сайта). id/pass
    сейчас НЕ используются для регистрации (регаем только по готовому
    vk_token через AUTH_VK_PATH) — держим их просто для совместимости формата
    и на случай если потом добавим вход по логину+паролю.
    Также понимает старые строки "только токен" (для уже добавленных ранее)."""
    line = line.strip().strip('"\'')
    parts = line.split(":")
    if len(parts) >= 3:
        return {"id": parts[0], "pass": parts[1], "token": ":".join(parts[2:])}
    if len(parts) == 2:
        return {"id": parts[0], "pass": "", "token": parts[1]}
    return {"id": "", "pass": "", "token": line}


# ---- Прокси-пул: ip:port:user:pass построчно, round-robin, прокси закрепляется
# за аккаунтом НАВСЕГДА (сохраняется в accounts.json и переиспользуется для
# всех дальнейших действий этого аккаунта — свайпы/лайки/профиль). ------------
PROXY_POOL_FILE = Path(__file__).parent / "proxy_pool.json"


def _load_proxy_pool() -> list[str]:
    if PROXY_POOL_FILE.exists():
        try:
            data = json.loads(PROXY_POOL_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []
    return []


def _save_proxy_pool(data: list[str]) -> None:
    PROXY_POOL_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


PROXY_POOL: list[str] = _load_proxy_pool()
_proxy_rr_index = 0


def _normalize_proxy(raw: str) -> str:
    """ip:port:user:pass -> http://user:pass@ip:port. Уже готовый http(s)/socks — как есть."""
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
    if len(parts) == 2:
        return f"http://{parts[0]}:{parts[1]}"
    return raw


def _get_next_proxy() -> str:
    """Round-robin: следующий (нормализованный) прокси из пула, или '' если пуст."""
    global _proxy_rr_index
    if not PROXY_POOL:
        return ""
    raw = PROXY_POOL[_proxy_rr_index % len(PROXY_POOL)]
    _proxy_rr_index += 1
    return _normalize_proxy(raw)


def _proxy_pool_stats() -> dict:
    return {"total": len(PROXY_POOL)}


def _proxy_for_jwt(jwt_token: str) -> str:
    """Прокси, закреплённый за аккаунтом (по совпадению JWT) при регистрации."""
    for acc in ACCOUNTS.values():
        if acc.get("jwt") == jwt_token:
            return acc.get("proxy", "") or ""
    return ""


# ---- Глобальная память "уже встреченных" анкет-кандидатов (по их Twinby
# user id) — если кандидат уже был лайкнут/дизлайкнут с ЛЮБОЙ из наших
# анкет когда-либо, повторно не лайкаем, а сразу дизлайкаем. Общий файл,
# не привязан к конкретному аккаунту/пресету. --------------------------------
SEEN_USERS_FILE = Path(__file__).parent / "seen_users.json"
_seen_users_lock = threading.Lock()


def _load_seen_users() -> dict:
    if SEEN_USERS_FILE.exists():
        try:
            data = json.loads(SEEN_USERS_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def _save_seen_users(data: dict) -> None:
    SEEN_USERS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


SEEN_USERS: dict = _load_seen_users()


def _mark_seen(uid: str, action: str, by: str) -> None:
    """Потокобезопасно помечает кандидата как лайкнутого/дизлайкнутого и сохраняет на диск."""
    with _seen_users_lock:
        SEEN_USERS[uid] = {"action": action, "ts": datetime.now(timezone.utc).isoformat(), "by": by}
        _save_seen_users(SEEN_USERS)


def _was_seen(uid: str) -> bool:
    with _seen_users_lock:
        return uid in SEEN_USERS


# ---- Глобальная память "уже написали первыми в этот чат" (по Twinby chat
# id) — для Рассылки, чтобы при повторном опросе списка чатов не слать
# повторное сообщение в тот же чат. Общий файл, не привязан к пресету. -------
MESSAGED_CHATS_FILE = Path(__file__).parent / "messaged_chats.json"
_messaged_chats_lock = threading.Lock()


def _load_messaged_chats() -> dict:
    if MESSAGED_CHATS_FILE.exists():
        try:
            data = json.loads(MESSAGED_CHATS_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def _save_messaged_chats(data: dict) -> None:
    MESSAGED_CHATS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


MESSAGED_CHATS: dict = _load_messaged_chats()


def _mark_messaged(chat_id: str, by: str, text: str) -> None:
    with _messaged_chats_lock:
        MESSAGED_CHATS[chat_id] = {"text": text, "ts": datetime.now(timezone.utc).isoformat(), "by": by}
        _save_messaged_chats(MESSAGED_CHATS)


def _was_messaged(chat_id: str) -> bool:
    with _messaged_chats_lock:
        return chat_id in MESSAGED_CHATS


def _register_vk_token(vk_token: str, proxy: str = "") -> dict:
    """Обменивает один VK access token на Twinby JWT через уже подтверждённый
    рабочим путь /api/auth/v2/oauth/by-token (этот конкретный инструмент его
    использует для токенов, уже полученных иначе — не путать с отдельным
    веб-логином по номеру/паролю, где v2-шорткат был отброшен как неверный).
    Возвращает {"ok": True, "jwt","user_id","name"} или {"ok": False, "error"}.
    """
    client_kw: dict = {"timeout": 30}
    if proxy:
        client_kw["proxy"] = proxy
    try:
        with httpx.Client(**client_kw) as client:
            resp = client.post(
                f"{tw.BASE}{AUTH_VK_PATH}",
                json={"provider": "vk", "token": vk_token},
                headers={"User-Agent": tw.UA, "Accept": "application/json",
                         "Content-Type": "application/json"},
            )
            if resp.status_code >= 400:
                return {"ok": False, "error": f"статус {resp.status_code}: {resp.text[:300]}"}
            try:
                data = resp.json()
            except Exception:
                return {"ok": False, "error": f"не json в ответе: {resp.text[:300]}"}
            jwt_token = data.get("accessToken", "")
            if not jwt_token:
                return {"ok": False, "error": f"нет accessToken: {json.dumps(data, ensure_ascii=False)[:300]}"}

            user_name, user_id = "", ""
            try:
                me_resp = client.get(
                    f"{tw.BASE}{tw.API['me']}",
                    headers={"User-Agent": tw.UA, "Accept": "application/json",
                             "Authorization": f"JWT {jwt_token}"},
                )
                if me_resp.status_code < 400:
                    me = me_resp.json()
                    user_name = me.get("name", "")
                    user_id = str(me.get("id", ""))
            except Exception:
                pass
    except Exception as e:
        return {"ok": False, "error": f"сетевая ошибка: {e}"}

    return {"ok": True, "jwt": jwt_token, "user_id": user_id, "name": user_name,
            "refresh_token": data.get("refreshToken", "")}

def _page(tab: str, content: str, log: str = None, error: str = None) -> str:
    """Layout wrapper. tab = 'main'|'accounts'|'swipe'."""
    tabs_html = ""
    for t, href, label in [("accounts", "/accounts", "Аккаунты"), ("settings", "/settings", "Настройки"),
                            ("presets", "/presets", "Пресеты")]:
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
body{max-width:1120px;padding:26px clamp(16px,3vw,38px) 60px;background:radial-gradient(ellipse at 85% -15%,#3b285b 0,transparent 42%),#11121a;color:#f3f1f7}
h1{font-size:28px;letter-spacing:-.5px}.subtitle{color:#a7a2b2}
.tabs{position:sticky;top:0;z-index:5;padding-top:8px;background:#11121aeF;border-color:#302b3b;backdrop-filter:blur(14px)}
.tabs a{padding:12px 16px;color:#aaa4b5;border-radius:10px 10px 0 0;border-bottom:2px solid transparent}
.tabs a.active{color:#e5d8ff;border-color:#bb9cff;background:#bb9cff12}
fieldset,.acct-card,.swipe-card{background:linear-gradient(145deg,#1e1b27,#191720);border-color:#353043;border-radius:14px}
legend{color:#cbb8f1}label.l{color:#c7c1d0}
input[type=text],input[type=number],input[type=date],input[type=file],select,textarea{background:#14131b;border-color:#393446;border-radius:9px;padding:10px;color:#f3f1f7}
input:focus,select:focus,textarea:focus{outline:2px solid #bb9cff55;border-color:#bb9cff}
button.go{background:linear-gradient(110deg,#c0a3ff,#e6a5bf);color:#201a2b;border-radius:10px;padding:11px 17px}
button.go:hover{filter:brightness(1.08)}.copy-btn{border-color:#4a4258;border-radius:8px;padding:7px 11px}
.hint{color:#a29bad}.err{color:#ff9fad}.ok{color:#a8e5ce}
@media(max-width:680px){body{padding:14px 12px 40px}.tabs{overflow-x:auto}.tabs a{white-space:nowrap;padding:10px 12px;font-size:13px}.row{flex-direction:column}.acct-card{gap:12px;align-items:flex-start}}
/* swipe card */
.swipe-card{background:#1a1a1a;border:1px solid #333;border-radius:10px;padding:16px;margin-top:12px;text-align:center}
.swipe-card img{max-width:200px;max-height:260px;border-radius:8px;margin-bottom:10px}
.swipe-card .name{font-size:18px;font-weight:600;color:#eee}
.swipe-card .meta{font-size:13px;color:#888;margin-top:4px}
table.log-table{width:100%;border-collapse:collapse;margin-top:10px;font-size:13px}
table.log-table td,table.log-table th{padding:6px 10px;border-bottom:1px solid #222;text-align:left}
table.log-table th{color:#888;font-weight:500}
.modal-overlay{position:fixed;inset:0;background:#000a;display:flex;align-items:center;justify-content:center;z-index:50;padding:20px}
.modal-box{background:#191720;border:1px solid #353043;border-radius:14px;padding:22px;max-width:480px;width:100%;max-height:85vh;overflow-y:auto}
.modal-box h3{margin:0 0 14px;color:#cbb8f1}
.modal-box .mrow{margin-top:10px;font-size:13px;color:#c7c1d0}
.modal-box .mrow b{color:#f3f1f7}
.modal-box textarea.mono{font-family:monospace;font-size:11px;min-height:60px}
.modal-close{float:right;cursor:pointer;color:#aaa;font-size:20px;line-height:1}
.modal-close:hover{color:#fff}
</style></head><body>
<h1>Twinby Tool</h1>
<p class="subtitle">Онбординг, профиль, свайпы. Токены уходят только на twinby.ru.</p>

<div class="tabs">{{TABS}}</div>

{{CONTENT}}

{{LOG}}
{{ERROR}}

<div id="acctModal" class="modal-overlay" style="display:none" onclick="if(event.target===this)closeAccountModal()">
  <div class="modal-box" id="acctModalBox"></div>
</div>

<script>
function esc_(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');}

function openAccountModal(label){
  fetch('/api/accounts').then(r=>r.json()).then(all=>{
    const a=all[label];
    if(!a){alert('Аккаунт не найден');return;}
    const box=document.getElementById('acctModalBox');
    box.innerHTML=''
      +'<span class="modal-close" id="acctModalCloseBtn">&times;</span>'
      +'<h3>'+esc_(label)+(a.name?' · '+esc_(a.name):'')+'</h3>'
      +'<div class="mrow"><b>Лайков:</b> '+(a.likes_done||0)+' &nbsp; <b>Дизлайков:</b> '+(a.dislikes_done||0)+'</div>'
      +'<div class="mrow"><b>Пресет:</b> '+esc_(a.preset||'—')+' &nbsp; <b>Город:</b> '+esc_(a.city||'—')+'</div>'
      +'<div class="mrow"><b>Онбординг:</b> '+(a.onboarded?'✅':'❌ '+esc_(a.onboard_error||''))+' &nbsp; <b>Видео:</b> '+(a.video_verified?'✅':'—')+'</div>'
      +'<div class="mrow"><label class="l" style="margin-top:12px">VK данные (id:pass:token)</label>'
      +'<textarea class="mono" readonly>'+esc_(a.vk_id||'')+':'+esc_(a.vk_pass||'')+':'+esc_(a.vk_token||'')+'</textarea></div>'
      +'<div class="mrow"><label class="l">JWT</label><textarea class="mono" readonly>'+esc_(a.jwt||'')+'</textarea></div>'
      +'<div class="mrow"><label class="l" style="margin-top:14px"><input type="checkbox" id="mVerified"> Верификация пройдена</label></div>'
      +'<div class="mrow"><label class="l"><input type="checkbox" id="mPremium"> Подписка (премиум)</label></div>';
    document.getElementById('acctModalCloseBtn').addEventListener('click', closeAccountModal);
    const vBox=document.getElementById('mVerified');
    const pBox=document.getElementById('mPremium');
    vBox.checked=!!a.verified;
    pBox.checked=!!a.premium;
    const save=()=>saveAccountFlags(label, vBox.checked, pBox.checked);
    vBox.addEventListener('change', save);
    pBox.addEventListener('change', save);
    document.getElementById('acctModal').style.display='flex';
  });
}

function closeAccountModal(){document.getElementById('acctModal').style.display='none';}

function saveAccountFlags(label, verified, premium){
  fetch('/api/accounts/'+encodeURIComponent(label)+'/flags',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({verified:verified,premium:premium})});
}

async function copyText(button) {
  const value = button.dataset.copy || '';
  const original = button.dataset.original || button.textContent;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(value);
    } else {
      throw new Error('clipboard API unavailable');
    }
  } catch (_) {
    const field = document.createElement('textarea');
    field.value = value;
    field.setAttribute('readonly', '');
    field.style.position = 'fixed';
    field.style.opacity = '0';
    document.body.appendChild(field);
    field.select();
    field.setSelectionRange(0, field.value.length);
    const copied = document.execCommand('copy');
    field.remove();
    if (!copied) {
      button.textContent = 'Copy failed';
      setTimeout(() => button.textContent = original, 1800);
      return;
    }
  }
  button.dataset.original = original;
  button.textContent = 'Copied';
  setTimeout(() => button.textContent = original, 1200);
}
</script>
</body></html>
"""

PAGE_MAIN = """
<form method="post" action="/" enctype="multipart/form-data">
  <label class="l">Access token (JWT)</label>
  <textarea id="token" name="token" required placeholder="Вставь JWT токен или получи через вкладку Аккаунты">{{ f.token }}</textarea>

  <label class="l"><input type="checkbox" name="profile_only" {{ 'checked' if f.profile_only }}>
    Только профиль (онбординг на этом аккаунте уже пройден)</label>

  <fieldset><legend>Онбординг</legend>
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

PAGE_MAIN = r"""
<h2>Онбординг</h2>
<p class="hint">Выбери сохранённый аккаунт. Параметры анкеты настраиваются отдельно во вкладке «Настройки».</p>
{% if accounts %}
<form method="post" action="/_legacy-onboarding" enctype="multipart/form-data">
  <label class="l" for="account_label">Аккаунт</label>
  <select id="account_label" name="account_label" required>
    <option value="">Выбери аккаунт</option>
    {% for label, acc in accounts.items() %}<option value="{{ label|e }}">{{ acc.name or label }}{% if acc.user_id %} · {{ acc.user_id }}{% endif %}</option>{% endfor %}
  </select>
  {% for key in ['name','birthday','gender','target_id','interview_id','about','height','city_id','city_name','religion_id','education','children','alcohol','smoking'] %}<input type="hidden" name="{{ key }}" value="{{ f[key] }}">{% endfor %}
  {% for value in f.language %}<input type="hidden" name="language" value="{{ value }}">{% endfor %}
  {% for value in f.interest %}<input type="hidden" name="interest" value="{{ value }}">{% endfor %}
  {% if f.zodiac %}<input type="hidden" name="zodiac" value="1">{% endif %}
  {% if f.zodiac_touch %}<input type="hidden" name="zodiac_touch" value="1">{% endif %}
  <label class="l"><input type="checkbox" name="profile_only" value="1"> Только обновить профиль, без нового онбординга</label>
  <label class="l">Фото анкеты (.webp / .jpg / .png)</label><input type="file" name="photo" accept="image/*">
  <label class="l">Видео проверки (необязательно)</label><input type="file" name="liveness_video" accept="video/mp4,.mp4">
  <label class="l"><input type="checkbox" name="vflip" value="1"> Отразить видео по вертикали</label>
  <label class="l"><input type="checkbox" name="raw_video" value="1"> Без конвертации (raw, диагностика)</label>
  <label class="l"><input type="checkbox" name="dry_run" value="1"> Предпросмотр запросов (dry-run)</label>
  <button class="go" type="submit" name="mode" value="full">Запустить для выбранного аккаунта</button>
</form>
{% else %}<div class="acct-card"><div class="info">Сохранённых аккаунтов пока нет. Добавь аккаунт на вкладке «Аккаунты», затем вернись сюда.</div><a class="copy-btn" href="/accounts">??????? ????????</a></div>{% endif %}
"""

PAGE_SETTINGS = r"""
<h2>Настройки анкеты</h2>

<fieldset>
  <legend>VK аккаунты (пул для регистрации)</legend>
  <p class="hint">Формат — одна строка на аккаунт: <b>id:pass:token</b> (id и пароль пока не используются,
    регистрация идёт по готовому VK access token; формат на будущее, если добавим вход по логину+паролю).
    Можно вставлять и просто токен без id/pass.</p>
  {% if vk_added is not none %}<p class="ok">Добавлено новых аккаунтов в пул: {{ vk_added }}.</p>{% endif %}
  {% if vk_cleared %}<p class="ok">Пул очищен, удалено токенов: {{ vk_cleared }}.</p>{% endif %}
  <p class="hint">В пуле сейчас: {{ vk_stats.total }} всего · {{ vk_stats.available }} доступно ·
    {{ vk_stats.used }} использовано.</p>
  <form method="post" action="/settings/vk-pool">
    <textarea name="vk_tokens_bulk" placeholder="48450410875:xL4Z9GJBnR2pcU6B:vk1.a.lVBxIepx...&#10;213312::vk1.a.RJTENMG0..."></textarea>
    <button class="go" type="submit">Добавить VK аккаунты</button>
  </form>
  <form method="post" action="/settings/vk-pool/clear" style="margin-top:10px" onsubmit="return confirm('Удалить ВСЕ VK-токены из пула (включая уже использованные)? Отменить нельзя.');">
    <button class="go danger" type="submit">Удалить все VK-токены из пула</button>
  </form>
</fieldset>

<fieldset>
  <legend>Прокси</legend>
  <p class="hint">Формат — одна строка: <b>ip:port:user:pass</b>. При регистрации анкеты софт берёт
    следующий прокси из пула по кругу и закрепляет его за аккаунтом навсегда — все дальнейшие
    действия (свайпы/лайки/профиль) идут через тот же прокси.</p>
  {% if proxy_added is not none %}<p class="ok">Добавлено новых прокси: {{ proxy_added }}.</p>{% endif %}
  <p class="hint">В пуле прокси сейчас: {{ proxy_stats.total }}.</p>
  <form method="post" action="/settings/proxy-pool">
    <textarea name="proxies_bulk" placeholder="74.81.81.81:10000:user:pass&#10;74.81.81.81:10001:user:pass"></textarea>
    <button class="go" type="submit">Добавить прокси</button>
  </form>
</fieldset>

<fieldset>
  <legend>Города</legend>
  <p class="hint">Выбери города (можно сразу все 1М+ и т.п.) — при регистрации анкеты софт сам случайно
    берёт ОДИН город из выбранных и ставит его как локацию анкеты.</p>
  <div class="flex flex-wrap mb" style="gap:6px;display:flex;flex-wrap:wrap">
    <button type="button" class="copy-btn" onclick="filterCities(0)">Все</button>
    <button type="button" class="copy-btn" onclick="filterCities(100000)">100k+</button>
    <button type="button" class="copy-btn" onclick="filterCities(200000)">200k+</button>
    <button type="button" class="copy-btn" onclick="filterCities(500000)">500k+</button>
    <button type="button" class="copy-btn" onclick="filterCities(1000000)">1M+</button>
    <button type="button" class="copy-btn" onclick="selectAllVisibleCities()">Выбрать всё</button>
    <button type="button" class="copy-btn" onclick="deselectAllVisibleCities()">Снять всё</button>
    <span class="hint" id="cityStats" style="align-self:center"></span>
  </div>
  <div id="cityChips" class="chips"></div>
</fieldset>

<script>
let CITIES = [];
let CITY_FILTER = 0;
let SELECTED_CITIES = new Set({{ (f.cities or []) | tojson }});

function loadCities(){
  fetch('/api/cities').then(r=>r.json()).then(data=>{ CITIES = data||[]; renderCities(); });
}
function filterCities(min){ CITY_FILTER = min; renderCities(); }
function saveSelectedCities(){
  fetch('/settings/cities',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({cities:Array.from(SELECTED_CITIES)})});
}
function toggleCity(name){
  if(SELECTED_CITIES.has(name)) SELECTED_CITIES.delete(name); else SELECTED_CITIES.add(name);
  saveSelectedCities(); renderCities();
}
function selectAllVisibleCities(){
  CITIES.filter(c=>c.population>=CITY_FILTER).forEach(c=>SELECTED_CITIES.add(c.name));
  saveSelectedCities(); renderCities();
}
function deselectAllVisibleCities(){
  CITIES.filter(c=>c.population>=CITY_FILTER).forEach(c=>SELECTED_CITIES.delete(c.name));
  saveSelectedCities(); renderCities();
}
function renderCities(){
  const vis = CITIES.filter(c=>c.population>=CITY_FILTER);
  const box = document.getElementById('cityChips');
  box.innerHTML = vis.map(c=>{
    const on = SELECTED_CITIES.has(c.name);
    const pop = c.population>=1e6 ? (c.population/1e6).toFixed(1)+'M' : Math.round(c.population/1e3)+'k';
    return '<label class="chip"><input type="checkbox" '+(on?'checked':'')+' onchange="toggleCity(\''+c.name.replace(/'/g,"\\'")+'\')"><span>'+c.name+' <span style="opacity:.5">'+pop+'</span></span></label>';
  }).join('');
  document.getElementById('cityStats').innerText = 'выбрано: '+SELECTED_CITIES.size+' / показано: '+vis.length;
}
document.addEventListener('DOMContentLoaded', loadCities);
</script>

<fieldset>
  <legend>Пул фото для анкет</legend>
  <p class="hint">Загрузи сколько угодно фото — при регистрации анкеты софт случайно берёт одно из пула;
    если сервер его не примет («Bad photo»), берёт следующее случайное, пока не найдёт подходящее.</p>
  <input type="file" id="photoPoolFiles" multiple accept="image/*">
  <button type="button" class="go" onclick="uploadPhotoPool()" style="margin-top:6px">Добавить фото в пул</button>
  <div id="photoPoolLog" style="margin-top:6px;font-size:13px"></div>
  <div id="photoPoolGrid" style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px"></div>
</fieldset>

<fieldset>
  <legend>Пул видео для верификации</legend>
  <p class="hint">Загрузи видео как для верификации лица в приложении. При регистрации софт случайно
    пробует до 3 видео из пула; если ни одно не прошло — анкета всё равно регистрируется, просто без
    верификации (это не критично, можно добавить видео и прогнать верификацию позже).</p>
  <input type="file" id="videoPoolFiles" multiple accept="video/*">
  <button type="button" class="go" onclick="uploadVideoPool()" style="margin-top:6px">Добавить видео в пул</button>
  <div id="videoPoolLog" style="margin-top:6px;font-size:13px"></div>
  <div id="videoPoolList" style="margin-top:8px;font-size:13px"></div>
</fieldset>

<script>
function loadPhotoPool(){
  fetch('/api/photo-pool').then(r=>r.json()).then(d=>{
    const grid=document.getElementById('photoPoolGrid'); grid.innerHTML='';
    (d.files||[]).forEach(name=>{
      const div=document.createElement('div');
      div.style.cssText='position:relative;width:70px;height:70px;border-radius:8px;overflow:hidden;border:1px solid #333';
      div.innerHTML='<img src="/photo-pool-file/'+encodeURIComponent(name)+'" style="width:100%;height:100%;object-fit:cover">'
        +'<div style="position:absolute;top:0;right:0;background:rgba(0,0,0,.7);color:#f66;cursor:pointer;padding:2px 5px;font-size:11px" onclick="deletePhotoPoolFile(\''+name.replace(/'/g,"\\'")+'\')">✕</div>';
      grid.appendChild(div);
    });
    document.getElementById('photoPoolLog').innerText='В пуле фото: '+(d.files||[]).length;
  });
}
function uploadPhotoPool(){
  const files=document.getElementById('photoPoolFiles').files;
  if(!files.length){alert('Выбери файлы');return;}
  const fd=new FormData();
  for(let i=0;i<files.length;i++) fd.append('photos',files[i]);
  document.getElementById('photoPoolLog').innerText='Загружаю...';
  fetch('/settings/photo-pool',{method:'POST',body:fd})
    .then(r=>{ if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
    .then(d=>{
      document.getElementById('photoPoolLog').innerText='Добавлено: '+d.added+(d.skipped?', пропущено: '+d.skipped:'')+'. Всего в пуле: '+d.total;
      document.getElementById('photoPoolFiles').value='';
      loadPhotoPool();
    })
    .catch(e=>{ document.getElementById('photoPoolLog').innerText='Ошибка загрузки: '+e.message; });
}
function deletePhotoPoolFile(name){
  if(!confirm('Удалить фото из пула?'))return;
  fetch('/settings/photo-pool/'+encodeURIComponent(name),{method:'DELETE'}).then(r=>r.json()).then(()=>loadPhotoPool());
}

function loadVideoPool(){
  fetch('/api/video-pool').then(r=>r.json()).then(d=>{
    const list=document.getElementById('videoPoolList');
    const files=d.files||[];
    list.innerHTML=files.length?files.map(name=>
      '<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 0;border-bottom:1px solid #222">'
      +'<span>'+name+'</span>'
      +'<button type="button" class="copy-btn" style="color:#f55;border-color:#a33" onclick="deleteVideoPoolFile(\''+name.replace(/'/g,"\\'")+'\')">X</button></div>'
    ).join('') : '<span class="hint">Пул пуст</span>';
    document.getElementById('videoPoolLog').innerText='В пуле видео: '+files.length;
  });
}
function uploadVideoPool(){
  const files=document.getElementById('videoPoolFiles').files;
  if(!files.length){alert('Выбери файлы');return;}
  const fd=new FormData();
  for(let i=0;i<files.length;i++) fd.append('videos',files[i]);
  document.getElementById('videoPoolLog').innerText='Загружаю...';
  fetch('/settings/video-pool',{method:'POST',body:fd})
    .then(r=>{ if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
    .then(d=>{
      document.getElementById('videoPoolLog').innerText='Добавлено: '+d.added+(d.skipped?', пропущено: '+d.skipped:'')+'. Всего в пуле: '+d.total;
      document.getElementById('videoPoolFiles').value='';
      loadVideoPool();
    })
    .catch(e=>{ document.getElementById('videoPoolLog').innerText='Ошибка загрузки: '+e.message; });
}
function deleteVideoPoolFile(name){
  if(!confirm('Удалить видео из пула?'))return;
  fetch('/settings/video-pool/'+encodeURIComponent(name),{method:'DELETE'}).then(r=>r.json()).then(()=>loadVideoPool());
}
document.addEventListener('DOMContentLoaded',()=>{loadPhotoPool();loadVideoPool();});
</script>

{% if saved %}<p class="ok">Настройки сохранены.</p>{% endif %}
<form method="post" action="/settings">
  <fieldset><legend>Основное</legend>
    <label class="l">Имя</label><input type="text" name="name" value="{{ f.name }}">
    <label class="l">Дата рождения</label><input type="date" name="birthday" value="{{ f.birthday }}">
    <label class="l">Пол</label><div class="chips">
      <label class="chip"><input type="radio" name="gender" value="m" {{ 'checked' if f.gender=='m' }}><span>Мужской</span></label>
      <label class="chip"><input type="radio" name="gender" value="f" {{ 'checked' if f.gender=='f' }}><span>Женский</span></label>
    </div>
    <label class="l">О себе</label><textarea name="about">{{ f.about }}</textarea>
    <label class="l">Рост, см</label><input type="number" name="height" min="50" max="250" value="{{ f.height }}">
  </fieldset>
  <fieldset><legend>Онбординг</legend>
    <label class="l">Target ID</label><input type="text" name="target_id" value="{{ f.target_id }}">
    <label class="l">Interview ID</label><input type="text" name="interview_id" value="{{ f.interview_id }}">
  </fieldset>
  <fieldset><legend>Религия и прочее</legend>
    <label class="l">Религия</label><div class="chips">{% for rid,rname in religions %}<label class="chip"><input type="radio" name="religion_id" value="{{ rid }}" {{ 'checked' if f.religion_id==rid }}><span>{{ rname }}</span></label>{% endfor %}<label class="chip"><input type="radio" name="religion_id" value="" {{ 'checked' if not f.religion_id }}><span>Не менять</span></label></div>
    {% for key,title,opts in singles %}<label class="l">{{ title }}</label><div class="chips">{% for value,label in opts %}<label class="chip"><input type="radio" name="{{ key }}" value="{{ value }}" {{ 'checked' if f[key]==value }}><span>{{ label }}</span></label>{% endfor %}<label class="chip"><input type="radio" name="{{ key }}" value="" {{ 'checked' if not f[key] }}><span>Не менять</span></label></div>{% endfor %}
  </fieldset>
  <fieldset><legend>Языки и интересы</legend>
    <label class="l">Языки</label><input type="text" id="settingsLangFilter" placeholder="Фильтр по названию">
    <div class="chips" id="settingsLangs">{% for lid,lname in languages %}<label class="chip" data-n="{{ lname|lower }}"><input type="checkbox" name="language" value="{{ lid }}" {{ 'checked' if lid in f.language }}><span>{{ lname }}</span></label>{% endfor %}</div>
    <label class="l">Интересы <span class="hint">(максимум 5)</span></label><div id="settingsInterests">{% for group,items in groups.items() %}<h4>{{ group }}</h4><div class="chips">{% for iid,iname in items %}<label class="chip"><input type="checkbox" name="interest" value="{{ iid }}" {{ 'checked' if iid in f.interest }}><span>{{ iname }}</span></label>{% endfor %}</div>{% endfor %}</div>
  </fieldset>
  <fieldset><legend>Дополнительно</legend>
    <label class="l"><input type="checkbox" name="zodiac_touch" value="1" {{ 'checked' if f.zodiac_touch }}> Отправлять настройку знака зодиака</label>
    <label class="l"><input type="checkbox" name="zodiac" value="1" {{ 'checked' if f.zodiac }}> Показывать знак зодиака</label>
  </fieldset>
  <button class="go" type="submit">Сохранить настройки</button>
</form>
<script>document.getElementById('settingsLangFilter').addEventListener('input',e=>{const q=e.target.value.trim().toLowerCase();document.querySelectorAll('#settingsLangs .chip').forEach(x=>x.style.display=x.dataset.n.includes(q)?'':'none')})</script>
<script>
(function(){
  const MAX_INTERESTS=5;
  const boxes=Array.from(document.querySelectorAll('#settingsInterests input[name="interest"]'));
  function refresh(){
    const checkedCount=boxes.filter(b=>b.checked).length;
    boxes.forEach(b=>{ b.disabled = !b.checked && checkedCount>=MAX_INTERESTS; });
  }
  boxes.forEach(b=>b.addEventListener('change',refresh));
  refresh();
})();
</script>
"""

PAGE_ACCOUNTS = """
<h2>Аккаунты</h2>
<p class="hint">Здесь появляются уже зарегистрированные Twinby-аккаунты (с JWT). Чтобы зарегать новые —
  вставь VK-токены во вкладке «Настройки», а затем жми «Зарегать» во вкладке «Пресеты».</p>

{% if accounts %}
<h3 style="margin-top:24px">Зарегистрированные аккаунты ({{ accounts|length }})</h3>
{% for label, acc in accounts.items() %}
<div class="acct-card" style="cursor:pointer" onclick="openAccountModal('{{ label }}')">
  <div class="info">
    <b>{{ label }}</b>
    {% if acc.name %} · {{ acc.name }}{% endif %}
    {% if acc.user_id %} <span style="color:#666">(id: {{ acc.user_id }})</span>{% endif %}
    {% if acc.preset %} <span style="color:#888">· пресет: {{ acc.preset }}</span>{% endif %}
    <br><span style="font-size:12px;color:#888">
      {% if acc.onboarded %}онбординг ✅{% elif acc.onboarded is defined %}онбординг ❌{% if acc.onboard_error %} ({{ acc.onboard_error }}){% endif %}{% endif %}
      {% if acc.video_verified %} · видео ✅{% elif acc.onboarded is defined %} · видео —{% endif %}
      {% if acc.city %} · {{ acc.city }}{% endif %}
      {% if acc.proxy %} · proxy {{ acc.proxy }}{% endif %}
      · 👍 {{ acc.likes_done or 0 }} · 👎 {{ acc.dislikes_done or 0 }}
      · {% if acc.verified %}верификация ✅{% else %}верификация ❌{% endif %}
      · {% if acc.premium %}премиум ✅{% else %}премиум ❌{% endif %}
    </span>
    <br><span class="jwt" title="{{ acc.jwt }}">JWT: {{ acc.jwt[:60] }}...</span>
  </div>
  <div style="display:flex;gap:6px;align-items:center">
    <button class="copy-btn" type="button" data-copy="{{ acc.jwt|e }}" onclick="event.stopPropagation();copyText(this)">Copy JWT</button>
    <form method="post" action="/accounts" style="margin:0" onclick="event.stopPropagation()"><input type="hidden" name="label" value="{{ label }}">
      <button class="copy-btn" type="submit" name="action" value="remove" style="color:#f55;border-color:#a33">X</button>
    </form>
  </div>
</div>
{% endfor %}
{% endif %}
"""

DEFAULTS = dict(token="", profile_only=False, name="", birthday="", gender="m",
                target_id="887657566886395905", interview_id="689835470004879361",
                about="", height="", city_id="", city_name="", latitude="", longitude="", religion_id="",
                education="", children="", alcohol="", smoking="",
                language=[], interest=[], cities=[], zodiac=True, zodiac_touch=False, dry_run=False,
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


_CITY_BY_NAME: dict = {name: {"name": name, "lat": lat, "lon": lon, "population": pop}
                       for name, lat, lon, pop in RUSSIAN_CITIES}


def _pick_city_for_preset(settings: dict, preset: str | None) -> dict | None:
    """Случайный город из списка, выбранного в Настройках (вкладка «Города»),
    БЕЗ повторов среди анкет уже зарегистрированных через тот же пресет
    (`preset` = label_prefix). Если все выбранные города этого пресета уже
    заняты (анкет больше, чем городов) — повтор неизбежен, тогда просто
    случайный из всего выбранного списка. Возвращает {"name","lat","lon"}
    или None, если список городов пуст/не задан."""
    import random
    names = settings.get("cities") or []
    picked = [n for n in names if n in _CITY_BY_NAME]
    if not picked:
        return None
    if preset:
        used = {acc.get("city") for acc in ACCOUNTS.values()
                if acc.get("preset") == preset and acc.get("city")}
        available = [n for n in picked if n not in used]
        if available:
            picked = available
    return _CITY_BY_NAME[random.choice(picked)]


def _list_photo_pool() -> list[str]:
    if not PHOTO_POOL_DIR.exists():
        return []
    return sorted(p.name for p in PHOTO_POOL_DIR.iterdir() if p.is_file() and p.suffix.lower() in MIME)


def _list_video_pool() -> list[str]:
    if not VIDEO_POOL_DIR.exists():
        return []
    return sorted(p.name for p in VIDEO_POOL_DIR.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTS)


def _try_random_photo(c, tee) -> bool:
    """Случайно берёт фото из пула (Настройки); если сервер отклоняет ("Bad
    photo", 403 -> RuntimeError из WebClient.call) — пробует следующее
    случайное, пока не найдёт подходящее или не исчерпает пул."""
    import random
    files = _list_photo_pool()
    if not files:
        tee("  пул фото пуст — нечего загружать")
        return False
    random.shuffle(files)
    for fname in files:
        path = PHOTO_POOL_DIR / fname
        try:
            data = path.read_bytes()
        except Exception as e:
            tee(f"  не смог прочитать {fname}: {e}")
            continue
        mime = MIME.get(path.suffix.lower())
        if not mime or not data:
            continue
        try:
            c.call("POST", tw.API["photo"], data={"order": PHOTO_ORDER},
                   files={"file": (fname, data, mime)})
            tee(f"  фото принято: {fname}")
            return True
        except RuntimeError as e:
            tee(f"  фото отклонено ({fname}): {e}")
            continue
    return False


def _send_liveness_bytes(c, raw_bytes: bytes, tee) -> bool:
    """Шлёт одно видео из пула на верификацию. Возвращает True при успехе
    (статус < 400), False если сервер отклонил — без исключения, чтобы
    вызывающий код мог просто попробовать следующее видео из пула."""
    if not raw_bytes:
        return False
    if len(raw_bytes) > LIVENESS_MAX_BYTES:
        tee(f"  видео больше {LIVENESS_MAX_BYTES // 1_000_000} МБ — пропущено")
        return False

    from convert_to_android import convert_for_liveness
    try:
        tee("  конвертация видео в формат Android MediaRecorder...")
        video_bytes = convert_for_liveness(raw_bytes, debug_print=tee, vflip=False)
    except Exception as e:
        tee(f"  конвертация не удалась: {e}")
        return False
    tee(f"  видео после конвертации: {len(video_bytes)//1024} КБ")

    auth_header = c.http.headers.get("authorization") or c.http.headers.get("Authorization")
    if not auth_header:
        tee("  нет Authorization в сессии — верификация невозможна")
        return False
    if not auth_header.startswith("JWT "):
        auth_header = "JWT " + auth_header.replace("Bearer ", "")

    fname = f"REC{time.time_ns()}.mp4"
    idem = str(uuid.uuid4())
    trace_id = uuid.uuid4().hex
    span_id = uuid.uuid4().hex[:16]
    sentry_trace = f"{trace_id}-{span_id}-0"
    raw_b = uuid.uuid4().bytes + uuid.uuid4().bytes + uuid.uuid4().bytes[:5]
    bstr = base64.b64encode(raw_b).rstrip(b"=").decode("ascii").replace("/", ".")
    boundary = f"dart-http-boundary-{bstr}"
    header_part = (
        f"--{boundary}\r\n"
        f"content-type: video/mp4\r\n"
        f'content-disposition: form-data; name="data"; filename="{fname}"\r\n'
        f"\r\n"
    ).encode("utf-8")
    footer_part = f"\r\n--{boundary}--\r\n".encode("utf-8")
    body = header_part + video_bytes + footer_part
    url = f"{tw.BASE}{LIVENESS_PATH}"

    from collections import OrderedDict
    # ПОДТВЕРЖДЕНО 2026-11-23 (mitmproxy, реальный успешный запрос, Verification
    # Passed): заголовка "Connection" в реальном запросе НЕТ, порядок другой.
    headers = OrderedDict([
        ("user-agent", "Dart/3.11 (dart:io)"), ("idempotency-key", idem),
        ("accept", "application/json"), ("accept-language", "en"),
        ("accept-encoding", "gzip"), ("content-length", str(len(body))),
        ("host", "twinby.ru"), ("authorization", auth_header),
        ("content-type", f"multipart/form-data; boundary={boundary}"),
        ("sentry-trace", sentry_trace),
    ])
    try:
        resp = cffi_requests.post(url, headers=headers, data=body, timeout=60)
    except Exception as e:
        tee(f"  сетевая ошибка верификации: {e}")
        return False
    if resp.status_code < 400:
        tee(f"  ✅ верификация принята (статус {resp.status_code})")
        return True
    tee(f"  ⚠️ верификация отклонена (статус {resp.status_code}): {resp.text[:200]}")
    return False


def _try_random_video(c, tee, max_attempts: int = 3) -> bool:
    import random
    files = _list_video_pool()
    if not files:
        tee("  пул видео пуст — верификация пропущена")
        return False
    random.shuffle(files)
    for fname in files[:max_attempts]:
        path = VIDEO_POOL_DIR / fname
        try:
            raw = path.read_bytes()
        except Exception as e:
            tee(f"  не смог прочитать {fname}: {e}")
            continue
        tee(f"  пробую видео: {fname}")
        if _send_liveness_bytes(c, raw, tee):
            return True
    return False


class _DictForm:
    """Адаптер dict -> интерфейс form.get/getlist, чтобы переиспользовать
    fill_profile() с обычными Settings (а не werkzeug form)."""
    def __init__(self, data: dict):
        self._data = data

    def get(self, key, default=""):
        v = self._data.get(key, default)
        return v if v is not None else default

    def getlist(self, key):
        v = self._data.get(key, [])
        return v if isinstance(v, list) else []


def _run_full_onboarding(c, settings: dict, tee, preset: str | None = None) -> dict:
    """Полная регистрация анкеты (как раньше делала вкладка «Онбординг»),
    но данные берутся из Settings (одинаковые для всех анкет пресета), фото —
    случайное из пула с ретраями, видео-верификация — тоже из пула, до 3
    попыток, если не прошла — не критично, просто логируем и продолжаем."""
    name = (settings.get("name") or "").strip()
    birthday_raw = (settings.get("birthday") or "").strip()
    gender = (settings.get("gender") or "m").strip()
    target_id = (settings.get("target_id") or "").strip()
    interview_id = (settings.get("interview_id") or "").strip()
    if not name or not birthday_raw:
        return {"ok": False, "error": "В Настройках не заданы имя и/или дата рождения."}
    if not target_id or not interview_id:
        return {"ok": False, "error": "В Настройках не заданы target_id/interview_id."}
    try:
        birthday_iso = tw.valid_birthday(birthday_raw)
    except (SystemExit, ValueError) as e:
        return {"ok": False, "error": f"Дата рождения: {e}"}

    cfg = c.call("GET", tw.API["captcha_cfg"])
    if isinstance(cfg, dict) and cfg.get("isActive"):
        return {"ok": False, "error": "Капча включена на сайте, этот путь её не проходит."}

    c.call("POST", tw.API["agreement"])
    c.call("PATCH", tw.API["me"], json={"name": name, "language": "RU"})
    c.call("PATCH", tw.API["me"], json={"birthday": birthday_iso})
    c.call("PATCH", tw.API["me"], json={"onboardingStep": 1})
    c.call("PATCH", tw.API["filter"], json=tw.FILTER_DEFAULT)
    c.call("PATCH", tw.API["me"], json={"gender": gender})
    c.call("PATCH", tw.API["me"], json={"onboardingStep": 2})
    c.call("PATCH", tw.API["me"], json={"onboardingStep": 3})
    c.call("PATCH", tw.API["me"], json={"targetId": target_id})
    c.call("PATCH", tw.API["me"], json={"onboardingStep": 4})

    city = _pick_city_for_preset(settings, preset)
    if city:
        c.call("PATCH", tw.API["me"], json={"location": f"POINT({city['lat']} {city['lon']})",
                                            "nameCity": city["name"]})
        tee(f"  город (случайно из выбранных в Настройках): {city['name']}")

    if not _try_random_photo(c, tee):
        return {"ok": False, "error": "Ни одно фото из пула не было принято сервером (или пул пуст)."}

    c.call("PATCH", tw.API["me"], json={"onboardingStep": 5})
    c.call("PATCH", tw.API["me"], json={"onboardingStep": 6})

    qs = c.call("GET", tw.API["questions"].format(iid=interview_id))
    if not isinstance(qs, list) or not qs:
        return {"ok": False, "error": "Пустой список вопросов интервью (проверь interview_id в Настройках)."}
    c.call("POST", tw.API["answers"], json={"test": [{"question": q["id"], "answer": 4} for q in qs]})
    c.call("PATCH", tw.API["me"], json={"onboardingCompleted": True})

    fill_profile(c, _DictForm(settings), tee)

    video_ok = _try_random_video(c, tee, max_attempts=3)
    tee("  видео-верификация: " + ("пройдена" if video_ok else "не пройдена/пропущена (не критично)"))

    return {"ok": True, "video_verified": video_ok, "city": city["name"] if city else None}


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

    # Конвертируем в формат Android MediaRecorder (можно пропустить для диагностики —
    # c._raw_video=True шлёт файл как есть, без FFmpeg/патча атомов)
    if getattr(c, "_raw_video", False):
        tee(f"[raw] конвертация ПРОПУЩЕНА — шлём видео как есть ({len(raw_bytes)//1024} КБ)")
        video_bytes = raw_bytes
    else:
        from convert_to_android import convert_for_liveness
        vflip = getattr(c, "_vflip", False)
        tee("Конвертация видео в формат Android MediaRecorder" + (" + vflip" if vflip else "") + "...")
        video_bytes = convert_for_liveness(raw_bytes, debug_print=tee, vflip=vflip)
        tee(f"Видео после конвертации: {len(video_bytes)//1024} КБ")

    # Дамп на диск для диагностики (всегда последний конвертированный файл)
    try:
        debug_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_last_liveness.mp4")
        with open(debug_path, "wb") as _f:
            _f.write(video_bytes)
        tee(f"[debug] конвертированное видео сохранено: {debug_path}")
    except Exception as _e:
        tee(f"[debug] не удалось сохранить debug-файл: {_e}")

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

    # Заголовки в порядке реального Dart-клиента (ПОДТВЕРЖДЕНО 2026-11-23,
    # mitmproxy, реальный успешный запрос Verification Passed). "Connection"
    # в реальном запросе отсутствует вообще.
    from collections import OrderedDict
    headers = OrderedDict([
        ("user-agent", "Dart/3.11 (dart:io)"),
        ("idempotency-key", idem),
        ("accept", "application/json"),
        ("accept-language", "en"),
        ("accept-encoding", "gzip"),
        ("content-length", str(len(body))),
        ("host", "twinby.ru"),
        ("authorization", auth_header),
        ("content-type", f"multipart/form-data; boundary={boundary}"),
        ("sentry-trace", sentry_trace),
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
    """Создаёт WebClient с логированием. proxy (если задан) — закреплённый за
    аккаунтом прокси (ip:port:user:pass или уже готовый http://...)."""
    class WebClient(tw.Client):
        def call(self, method, path, **kw):
            shown = path + ("?" + "&".join(f"{k}={v}" for k, v in kw["params"].items()) if kw.get("params") else "")
            # Точечный опрос чатов (список/история/счётчик/мэтчи-без-переписки)
            # не логируем — это фоновый polling, он забивал лог мусором и не
            # нёс полезной информации по сравнению с самими LIKE/MSG-строками.
            # Ошибки (>=400) всё равно всплывают ниже через RuntimeError.
            is_noisy_poll = path.startswith("/api/chats/")
            if not is_noisy_poll:
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
    norm_proxy = _normalize_proxy(proxy) if proxy else ""
    if norm_proxy:
        c.http = httpx.Client(base_url=tw.BASE, timeout=30,
                               headers={"User-Agent": tw.UA,
                                        "Accept": "application/json, text/plain, */*",
                                        **tw.ORIGIN_HEADERS},
                               proxy=norm_proxy)
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
# ---- Вкладка: Онбординг (главная) ----
@app.route("/", methods=["GET"])
def root_redirect():
    # Вкладка "Онбординг" убрана из навигации — регистрация анкет теперь
    # делается через Пресеты (кнопка "Начать регу"), профиль настраивается
    # в Настройках. Старый роут оставлен ниже на всякий случай, но из UI
    # больше не достижим напрямую.
    return redirect("/presets")


@app.route("/_legacy-onboarding", methods=["GET", "POST"])
def index():
    f = dict(DEFAULTS)
    f.update(_load_onboarding_settings())
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
    account_label = form.get("account_label", "").strip()
    if account_label:
        account = ACCOUNTS.get(account_label)
        f["token"] = (account or {}).get("jwt", "")
        if not f["token"]:
            return _render(PAGE_MAIN, "main", log_buf, error="Выбранный аккаунт не найден или у него нет JWT. Открой вкладку «Аккаунты».", f=f)
    f["language"] = form.getlist("language")
    f["interest"] = form.getlist("interest")
    for k in ("profile_only", "zodiac", "zodiac_touch", "dry_run"):
        f[k] = bool(form.get(k))
    dry = f["dry_run"]
    mode = form.get("mode", "full")

    try:
        c = _make_web_client(f["token"], dry, tee, proxy=_proxy_for_jwt(f["token"]))

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

            c.call("POST", tw.API["agreement"])
            c.call("PATCH", tw.API["me"], json={"name": f["name"], "language": "RU"})
            c.call("PATCH", tw.API["me"], json={"birthday": birthday_iso})
            c.call("PATCH", tw.API["me"], json={"onboardingStep": 1})
            c.call("PATCH", tw.API["filter"], json=tw.FILTER_DEFAULT)
            c.call("PATCH", tw.API["me"], json={"gender": f["gender"]})
            c.call("PATCH", tw.API["me"], json={"onboardingStep": 2})
            c.call("PATCH", tw.API["me"], json={"onboardingStep": 3})
            c.call("PATCH", tw.API["me"], json={"targetId": f["target_id"]})
            c.call("PATCH", tw.API["me"], json={"onboardingStep": 4})
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
        c._raw_video = bool(form.get("raw_video"))
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
        import httpx
        resp = httpx.get(
            f"{tw.BASE}{API_PROFILE['city']}",
            params={"name": q, "hasUsers": "false"},
            headers={"User-Agent": tw.UA, "Accept": "application/json",
                     "Authorization": f"JWT {token}"},
            timeout=15,
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

    import httpx
    results = []
    for i, f in enumerate(files):
        cur_order = order + i
        try:
            file_bytes = f.read()
            resp = httpx.post(
                f"{tw.BASE}/api/photo/",
                headers={"User-Agent": tw.UA, "Authorization": f"JWT {token}"},
                files={"file": (f.filename or f"photo_{cur_order}.jpg", file_bytes, f.content_type or "image/jpeg")},
                data={"order": str(cur_order), "type": "photo"},
                timeout=30,
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
    import httpx
    try:
        resp = httpx.delete(
            f"{tw.BASE}/api/photo/{photo_id}/",
            headers={"User-Agent": tw.UA, "Authorization": f"JWT {token}"},
            timeout=15,
        )
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
    import httpx
    try:
        kw = {"data": {"order": new_order, "type": "photo"}}
        f = request.files.get("file")
        if f:
            kw["files"] = {"file": (f.filename, f.read(), f.content_type or "image/jpeg")}
        resp = httpx.patch(
            f"{tw.BASE}/api/photo/{photo_id}/",
            headers={"User-Agent": tw.UA, "Authorization": f"JWT {token}"},
            timeout=30, **kw,
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
    import httpx
    try:
        resp = httpx.get(
            f"{tw.BASE}/api/users/me/",
            headers={"User-Agent": tw.UA, "Accept": "application/json",
                     "Authorization": f"JWT {token}"},
            timeout=15,
        )
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
            return json.loads(PRESETS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def _save_presets(data):
    PRESETS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _preset_delay(preset_cfg: dict, min_key: str, max_key: str,
                   default_min: float, default_max: float) -> tuple[float, float]:
    """Достаёт пару delay_min/delay_max из настроек пресета (поля из формы —
    строки, могут быть пустыми/некорректными), с отказом на дефолт при любой
    ошибке парсинга. Если min > max после парсинга — меняет местами, чтобы
    random.uniform не упал."""
    try:
        dmin = float(preset_cfg.get(min_key, default_min))
    except (TypeError, ValueError):
        dmin = default_min
    try:
        dmax = float(preset_cfg.get(max_key, default_max))
    except (TypeError, ValueError):
        dmax = default_max
    if dmin < 0:
        dmin = 0.0
    if dmax < dmin:
        dmin, dmax = dmax, dmin
    return dmin, dmax

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
    """Зарегать анкету через VK токен, вернуть JWT + user info."""
    import httpx
    data = request.get_json(force=True)
    vk_token = (data.get("vk_token") or "").strip()
    if not vk_token:
        return jsonify({"ok": False, "error": "Нет VK токена"})

    try:
        resp = httpx.post(
            f"{tw.BASE}{AUTH_VK_PATH}",
            json={"provider": "vk", "token": vk_token},
            headers={"User-Agent": tw.UA, "Accept": "application/json",
                     "Content-Type": "application/json"},
            timeout=30,
        )
        if resp.status_code >= 400:
            return jsonify({"ok": False, "error": f"Статус {resp.status_code}: {resp.text[:300]}"})
        rd = resp.json()
        jwt_token = rd.get("accessToken", "")
        if not jwt_token:
            return jsonify({"ok": False, "error": f"Нет accessToken: {json.dumps(rd, ensure_ascii=False)[:300]}"})

        # Получим имя и ID
        user_name = ""
        user_id = ""
        try:
            me_resp = httpx.get(
                f"{tw.BASE}{tw.API['me']}",
                headers={"User-Agent": tw.UA, "Accept": "application/json",
                         "Authorization": f"JWT {jwt_token}"},
                timeout=15,
            )
            if me_resp.status_code < 400:
                me = me_resp.json()
                user_name = me.get("name", "")
                user_id = str(me.get("id", ""))
        except Exception:
            pass

        # Если переданы координаты — сразу ставим локацию
        lat_s = str(data.get("latitude", "")).strip()
        lon_s = str(data.get("longitude", "")).strip()
        if lat_s and lon_s:
            try:
                httpx.patch(
                    f"{tw.BASE}/api/users/me/",
                    headers={"User-Agent": tw.UA, "Authorization": f"JWT {jwt_token}",
                             "Content-Type": "application/json"},
                    json={"location": f"POINT({lat_s} {lon_s})"},
                    timeout=15,
                )
            except Exception:
                pass

        return jsonify({"ok": True, "jwt": jwt_token, "user_id": user_id, "name": user_name})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/apply-preset", methods=["POST"])
def apply_preset():
    """Применить пресет: location + city + filter через Flask-прокси (обход CORS)."""
    import httpx
    data = request.get_json(force=True)
    token = (data.get("token") or "").strip().strip('"\'')
    p = data.get("preset", {})
    if not token:
        return jsonify({"ok": False, "log": "Нет токена"})

    headers = {"User-Agent": tw.UA, "Authorization": f"JWT {token}",
               "Content-Type": "application/json", "Accept": "application/json"}
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
            r = httpx.patch(f"{tw.BASE}/api/users/me/", headers=headers,
                            json=me_body, timeout=15)
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
            r = httpx.patch(f"{tw.BASE}/api/users/me/filter/", headers=headers,
                            json=filter_body, timeout=15)
            log_lines.append(f"Фильтр: {r.status_code}")
        except Exception as e:
            log_lines.append(f"Фильтр ошибка: {e}")

    log_lines.append("Готово!")
    return jsonify({"ok": True, "log": "\n".join(log_lines)})


# ---- Вкладка: Пресеты ----
PAGE_PRESETS = r"""
<h2>Пресеты</h2>
<p class="hint">Пресет = персона (имя + возраст + пометка). Укажи имя и сколько анкет зарегать —
  софт возьмёт VK-токены из пула (вкладка «Настройки»), прогонит полный онбординг и покажет результат в логе ниже.</p>

<div style="display:flex;gap:12px;margin-bottom:8px;flex-wrap:wrap;align-items:end">
  <div style="flex:1;min-width:140px"><label class="l">Имя пресета</label>
    <input type="text" id="newPresetName" placeholder="Артём, 25 (визи)"></div>
  <div><label class="l">Кол-во анкет</label>
    <input type="number" id="newPresetCount" value="1" min="1" style="max-width:100px"></div>
  <button class="go" style="margin-top:0;height:38px" onclick="createAndRegister()">Зарегать</button>
</div>
<p class="hint" id="poolStatsLine">Пул VK-токенов: загрузка...</p>

<div id="presetsList"></div>
<div id="presetDetail" style="display:none"></div>
<div id="presetLog" style="margin-top:12px"></div>

<script>
let PRESETS={};
let ACCOUNTS_CACHE={};
let OPEN_PRESET=null;
let VK_POOL_STATS={total:0,available:0,used:0};

function loadVkPoolStats(){
  fetch('/api/vk-pool-stats').then(r=>r.json()).then(d=>{
    VK_POOL_STATS=d;
    const line=document.getElementById('poolStatsLine');
    if(line) line.innerText='Пул VK-токенов: '+d.total+' всего · '+d.available+' доступно · '+d.used+' использовано.';
    renderPresetDetail();
  });
}

function loadAccountsCache(cb){
  fetch('/api/accounts').then(r=>r.json()).then(d=>{ACCOUNTS_CACHE=d||{};if(cb)cb();});
}

function accountsForPreset(name){
  return Object.entries(ACCOUNTS_CACHE).filter(([label,a])=>a.preset===name);
}

function createAndRegister(){
  const name=document.getElementById('newPresetName').value.trim();
  const count=parseInt(document.getElementById('newPresetCount').value)||0;
  if(!name){alert('Введи имя пресета');return;}
  if(count<=0){alert('Укажи количество анкет для регистрации');return;}
  if(!PRESETS[name]){PRESETS[name]={};savePresets(renderPresets);}
  document.getElementById('newPresetName').value='';
  runBulkRegister(name,count);
}

function runBulkRegister(name,count){
  logMsg('--- '+name+': регистрирую '+count+' анкет(у) из пула VK-токенов (VK→JWT, онбординг, фото, видео) ---');
  fetch('/api/bulk-register',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({count:count,label_prefix:name})})
  .then(r=>r.json()).then(d=>{
    if(!d.ok){logMsg('Ошибка: '+(d.error||'unknown'));return;}
    logMsg('Зарегано '+d.registered+' из '+d.requested+' запрошенных:');
    (d.results||[]).forEach(r=>{
      if(r.ok){
        const onb=r.onboarded?'онбординг ✅':'онбординг ❌ ('+(r.onboard_error||'?')+')';
        const vid=r.video_verified?'видео ✅':'видео —';
        logMsg('  '+(r.onboarded?'✅':'⚠️')+' '+r.label+' — '+(r.name||'без имени')+' (id='+r.user_id+') · '+(r.city||'город не задан')+' · '+onb+' · '+vid+' · proxy '+(r.proxy||'—'));
      } else {
        logMsg('  ❌ '+(r.token||'')+' — '+(r.error||'ошибка'));
      }
    });
    loadVkPoolStats();
    loadAccountsCache(()=>{renderPresets();renderPresetDetail();});
  }).catch(e=>logMsg('Ошибка запроса: '+e));
}

function loadPresets(){
  fetch('/api/presets').then(r=>r.json()).then(data=>{
    PRESETS=data||{};
    loadAccountsCache(renderPresets);
  });
}

function savePresets(cb){
  fetch('/api/presets',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(PRESETS)})
  .then(r=>r.json()).then(d=>{if(cb)cb();});
}

function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');}

function deletePreset(name){
  if(!confirm('Удалить пресет "'+name+'"? Уже зарегистрированные анкеты останутся во вкладке «Аккаунты».'))return;
  delete PRESETS[name];
  if(OPEN_PRESET===name){OPEN_PRESET=null;document.getElementById('presetDetail').style.display='none';}
  savePresets(renderPresets);
}

function openPreset(name){
  OPEN_PRESET=name;
  renderPresetDetail();
}

function renderPresets(){
  const box=document.getElementById('presetsList');
  let html='';
  for(const name of Object.keys(PRESETS)){
    const cnt=accountsForPreset(name).length;
    const cls=OPEN_PRESET===name?'border-color:#3a7':'';
    html+='<div class="acct-card" style="cursor:pointer;'+cls+'" onclick="openPreset(\''+esc(name)+'\')">'
      +'<div class="info"><b>'+esc(name)+'</b>'
      +'<br><span style="font-size:12px;color:#888">Анкет: '+cnt+'</span></div>'
      +'<div style="display:flex;gap:6px;align-items:center">'
      +'<button class="copy-btn" onclick="event.stopPropagation();deletePreset(\''+esc(name)+'\')" style="color:#f55;border-color:#a33">X</button>'
      +'</div></div>';
  }
  if(!html) html='<p class="hint">Нет пресетов. Создай первый сверху страницы.</p>';
  box.innerHTML=html;
  if(OPEN_PRESET && PRESETS[OPEN_PRESET]) renderPresetDetail();
  else document.getElementById('presetDetail').style.display='none';
}

function renderPresetDetail(){
  const d=document.getElementById('presetDetail');
  if(!OPEN_PRESET||!PRESETS[OPEN_PRESET]){d.style.display='none';return;}
  d.style.display='block';
  const accs=accountsForPreset(OPEN_PRESET);

  let html='<fieldset><legend>'+esc(OPEN_PRESET)+'</legend>';
  html+='<div style="display:flex;gap:8px;align-items:end;flex-wrap:wrap">'
    +'<div><label class="l">Докинуть ещё анкет</label><input type="number" id="bulkRegCount" value="1" min="1" max="'+Math.max(1,VK_POOL_STATS.available)+'" style="max-width:120px"></div>'
    +'<button type="button" class="go" style="margin-top:0;height:38px" onclick="bulkRegisterMore()">Зарегать ещё</button>'
    +'</div>';

  html+='<div style="margin-top:6px"><a href="#" onclick="toggleQuickAdd(event)" style="font-size:11px;color:#666;text-decoration:none">+ добавить готовый аккаунт по VK-токену (без регистрации)</a>'
    +'<div id="quickAddBox" style="display:none;margin-top:6px">'
    +'<textarea id="quickAddTokens" rows="2" placeholder="VK access token(ы), по одному на строку — если уже зареганы на Twinby где-то ещё" style="width:100%;font-size:12px"></textarea>'
    +'<button type="button" class="go" style="margin-top:4px;height:32px;font-size:12px" onclick="quickAddAccounts()">Добавить</button>'
    +'</div></div>';

  const pcfg=PRESETS[OPEN_PRESET]||{};
  const genderVal=pcfg.gender_filter||'n';
  html+='<div style="margin-top:14px;display:flex;gap:8px;align-items:end;flex-wrap:wrap">'
    +'<div><label class="l">Кого лайкать</label><select id="genderFilterSelect" style="max-width:170px">'
    +'<option value="n"'+(genderVal==='n'?' selected':'')+'>Все (муж. и жен.)</option>'
    +'<option value="f"'+(genderVal==='f'?' selected':'')+'>Только женщины</option>'
    +'<option value="m"'+(genderVal==='m'?' selected':'')+'>Только мужчины</option>'
    +'</select></div>'
    +'<button type="button" class="go" style="margin-top:0;height:38px" onclick="applyGenderFilter()">Применить ко всем анкетам</button>'
    +'<span class="hint" id="genderFilterStatusLine"></span>'
    +'</div>';
  html+='<div style="margin-top:14px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">'
    +'<div><label class="l">Пауза лайков, сек (от)</label><input type="number" step="0.1" min="0" id="likeDelayMin" value="'+esc(pcfg.like_delay_min||'0.5')+'" style="max-width:90px" onchange="saveDelaySetting(\'like_delay_min\',\'likeDelayMin\')"></div>'
    +'<div><label class="l">до</label><input type="number" step="0.1" min="0" id="likeDelayMax" value="'+esc(pcfg.like_delay_max||'1.5')+'" style="max-width:90px" onchange="saveDelaySetting(\'like_delay_max\',\'likeDelayMax\')"></div>'
    +'</div>';
  html+='<div style="margin-top:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">'
    +'<button type="button" class="go alt" id="likeBtn" onclick="startPresetLike()" style="margin-top:0">Лайкать</button>'
    +'<button type="button" class="go danger" id="stopLikeBtn" onclick="stopPresetLike()" style="margin-top:0;display:none">Стоп</button>'
    +'<span class="hint" id="likeStatusLine"></span>'
    +'</div>';

  html+='<div style="margin-top:16px"><label class="l">Сообщения для рассылки (по одному на строку — при мэтче без нашего ответа уйдёт случайное)</label>'
    +'<textarea id="broadcastMessages" rows="4" style="width:100%" onchange="saveBroadcastMessages()">'+esc(pcfg.messages||'')+'</textarea></div>';
  html+='<div style="margin-top:10px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">'
    +'<div><label class="l">Пауза рассылки, сек (от)</label><input type="number" step="0.1" min="0" id="msgDelayMin" value="'+esc(pcfg.msg_delay_min||'0.5')+'" style="max-width:90px" onchange="saveDelaySetting(\'msg_delay_min\',\'msgDelayMin\')"></div>'
    +'<div><label class="l">до</label><input type="number" step="0.1" min="0" id="msgDelayMax" value="'+esc(pcfg.msg_delay_max||'1.5')+'" style="max-width:90px" onchange="saveDelaySetting(\'msg_delay_max\',\'msgDelayMax\')"></div>'
    +'</div>';
  html+='<div style="margin-top:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">'
    +'<button type="button" class="go alt" id="broadcastBtn" onclick="startPresetBroadcast()" style="margin-top:0">Рассылка</button>'
    +'<button type="button" class="go danger" id="stopBroadcastBtn" onclick="stopPresetBroadcast()" style="margin-top:0;display:none">Стоп</button>'
    +'<span class="hint" id="broadcastStatusLine"></span>'
    +'</div>';

  html+='<h4 style="margin-top:16px">Анкеты этого пресета ('+accs.length+')</h4>';
  if(!accs.length){
    html+='<p class="hint">Пока ни одной анкеты. Нажми «Зарегать ещё» выше.</p>';
  }
  for(const[label,a] of accs){
    const onb=a.onboarded?'онбординг ✅':'онбординг ❌'+(a.onboard_error?' ('+esc(a.onboard_error)+')':'');
    const deadBadge=a.dead?' · <span style="color:#f55">💀 мёртв'+(a.dead_reason?' ('+esc(a.dead_reason)+')':'')+'</span>':'';
    html+='<div class="acct-card" style="cursor:pointer'+(a.dead?';opacity:0.55':'')+'" onclick="openAccountModal(\''+esc(label)+'\')">'
      +'<div class="info"><b>'+esc(label)+'</b>'
      +(a.name?' · '+esc(a.name):'')
      +(a.user_id?' <span style="color:#666">(id: '+esc(a.user_id)+')</span>':'')
      +'<br><span style="font-size:12px;color:#888">'+onb
      +' · '+(a.video_verified?'видео ✅':'видео —')
      +' · '+(a.city?esc(a.city):'город не задан')
      +' · proxy '+(a.proxy?esc(a.proxy):'—')
      +' · 👍 '+(a.likes_done||0)+' · 👎 '+(a.dislikes_done||0)
      +' · '+(a.verified?'верификация ✅':'верификация ❌')
      +' · '+(a.premium?'премиум ✅':'премиум ❌')
      +deadBadge
      +'</span></div>'
      +'<button class="copy-btn" data-copy="'+esc(a.jwt||'')+'" onclick="event.stopPropagation();copyText(this)">Copy JWT</button>'
      +'</div>';
  }

  html+='</fieldset>';
  d.innerHTML=html;
  refreshLikeButtonState();
  refreshBroadcastButtonState();
}

function saveBroadcastMessages(){
  if(!OPEN_PRESET)return;
  if(!PRESETS[OPEN_PRESET]) PRESETS[OPEN_PRESET]={};
  PRESETS[OPEN_PRESET].messages=document.getElementById('broadcastMessages').value;
  savePresets();
}

function saveDelaySetting(key,inputId){
  if(!OPEN_PRESET)return;
  if(!PRESETS[OPEN_PRESET]) PRESETS[OPEN_PRESET]={};
  PRESETS[OPEN_PRESET][key]=document.getElementById(inputId).value;
  savePresets();
}

function applyGenderFilter(){
  if(!OPEN_PRESET)return;
  const val=document.getElementById('genderFilterSelect').value;
  if(!PRESETS[OPEN_PRESET]) PRESETS[OPEN_PRESET]={};
  PRESETS[OPEN_PRESET].gender_filter=val;
  savePresets();
  const line=document.getElementById('genderFilterStatusLine');
  line.innerText='Применяю...';
  fetch('/api/preset-set-gender',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({preset:OPEN_PRESET,gender:val})})
    .then(r=>{ if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
    .then(d=>{ line.innerText='Готово: '+d.ok_count+' анкет(ы) обновлено'+(d.fail_count?', ошибок: '+d.fail_count:''); })
    .catch(e=>{ line.innerText='Ошибка: '+e.message; });
}

function bulkRegisterMore(){
  if(!OPEN_PRESET){alert('Сначала открой пресет');return;}
  const input=document.getElementById('bulkRegCount');
  const count=parseInt(input.value)||0;
  if(count<=0){alert('Укажи количество анкет для регистрации');return;}
  runBulkRegister(OPEN_PRESET,count);
}

function toggleQuickAdd(ev){
  ev.preventDefault();
  const box=document.getElementById('quickAddBox');
  if(box) box.style.display=(box.style.display==='none')?'block':'none';
}

function quickAddAccounts(){
  if(!OPEN_PRESET){alert('Сначала открой пресет');return;}
  const raw=document.getElementById('quickAddTokens').value;
  const tokens=raw.split('\n').map(s=>s.trim()).filter(s=>s);
  if(!tokens.length){alert('Вставь хотя бы один VK-токен');return;}
  logMsg('--- '+OPEN_PRESET+': добавляю '+tokens.length+' готовый(х) аккаунт(ов) по VK-токену, без регистрации ---');
  fetch('/api/accounts/quick-add',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({preset:OPEN_PRESET,tokens:tokens})})
  .then(r=>r.json()).then(d=>{
    if(!d.ok){logMsg('Ошибка: '+(d.error||'unknown'));return;}
    logMsg('Добавлено '+d.added+' из '+d.requested+':');
    (d.results||[]).forEach(r=>{
      if(r.ok) logMsg('  ✅ '+r.label+' — '+(r.name||'без имени')+' (id='+r.user_id+')');
      else logMsg('  ❌ '+(r.token||'')+' — '+(r.error||'ошибка'));
    });
    document.getElementById('quickAddTokens').value='';
    loadAccountsCache(()=>{renderPresets();renderPresetDetail();});
  }).catch(e=>logMsg('Ошибка запроса: '+e));
}

function logMsg(msg){
  const el=document.getElementById('presetLog');
  el.style.cssText='padding:8px;background:#1a1a1a;border-radius:6px;font-size:13px;white-space:pre-wrap;max-height:300px;overflow-y:auto';
  el.innerText+=(el.innerText?'\n':'')+msg;
  el.scrollTop=el.scrollHeight;
}

// --- Лайкать: параллельно по всем анкетам пресета, с поллингом статуса ---
let LIKE_POLL_TIMER=null;
let LIKE_POLL_PRESET=null;
let LIKE_LOG_SHOWN=0;

function startPresetLike(){
  if(!OPEN_PRESET)return;
  fetch('/api/preset-like/start',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({preset:OPEN_PRESET})})
  .then(r=>r.json()).then(d=>{
    if(!d.ok){alert(d.error||'Не удалось начать');return;}
    LIKE_LOG_SHOWN=0;
    logMsg('--- '+OPEN_PRESET+': запускаю лайканье по всем анкетам пресета ---');
    startLikePolling(OPEN_PRESET);
    refreshLikeButtonState();
  });
}

function stopPresetLike(){
  if(!OPEN_PRESET)return;
  fetch('/api/preset-like/stop',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({preset:OPEN_PRESET})});
}

function startLikePolling(preset){
  if(LIKE_POLL_TIMER) clearInterval(LIKE_POLL_TIMER);
  LIKE_POLL_PRESET=preset;
  LIKE_POLL_TIMER=setInterval(()=>pollPresetLike(preset),3000);
  pollPresetLike(preset);
}

function pollPresetLike(preset){
  fetch('/api/preset-like/status?preset='+encodeURIComponent(preset)).then(r=>r.json()).then(d=>{
    (d.log||[]).slice(LIKE_LOG_SHOWN).forEach(m=>logMsg(m));
    LIKE_LOG_SHOWN=(d.log||[]).length;
    const line=document.getElementById('likeStatusLine');
    if(line) line.innerText=d.running?'лайкает...':'остановлено';
    const likeBtn=document.getElementById('likeBtn');
    const stopBtn=document.getElementById('stopLikeBtn');
    if(likeBtn) likeBtn.style.display=d.running?'none':'';
    if(stopBtn) stopBtn.style.display=d.running?'':'none';
    if(!d.running && LIKE_POLL_TIMER){
      clearInterval(LIKE_POLL_TIMER);LIKE_POLL_TIMER=null;
      loadAccountsCache(()=>{renderPresets();renderPresetDetail();});
    }
  });
}

function refreshLikeButtonState(){
  if(!OPEN_PRESET)return;
  fetch('/api/preset-like/status?preset='+encodeURIComponent(OPEN_PRESET)).then(r=>r.json()).then(d=>{
    const likeBtn=document.getElementById('likeBtn');
    const stopBtn=document.getElementById('stopLikeBtn');
    const line=document.getElementById('likeStatusLine');
    if(likeBtn) likeBtn.style.display=d.running?'none':'';
    if(stopBtn) stopBtn.style.display=d.running?'':'none';
    if(line) line.innerText=d.running?'лайкает...':'';
    if(d.running){LIKE_LOG_SHOWN=0;startLikePolling(OPEN_PRESET);}
  });
}

// --- Рассылка: при мэтче без ответа — случайное сообщение из списка, ---
// непрерывный фоновый опрос (аналогично Лайкать, своя пара кнопок/поллинг) ---
let BROADCAST_POLL_TIMER=null;
let BROADCAST_POLL_PRESET=null;
let BROADCAST_LOG_SHOWN=0;

function startPresetBroadcast(){
  if(!OPEN_PRESET)return;
  saveBroadcastMessages();
  fetch('/api/preset-broadcast/start',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({preset:OPEN_PRESET})})
  .then(r=>r.json()).then(d=>{
    if(!d.ok){alert(d.error||'Не удалось начать');return;}
    BROADCAST_LOG_SHOWN=0;
    logMsg('--- '+OPEN_PRESET+': запускаю рассылку по мэтчам без ответа ---');
    startBroadcastPolling(OPEN_PRESET);
    refreshBroadcastButtonState();
  });
}

function stopPresetBroadcast(){
  if(!OPEN_PRESET)return;
  fetch('/api/preset-broadcast/stop',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({preset:OPEN_PRESET})});
}

function startBroadcastPolling(preset){
  if(BROADCAST_POLL_TIMER) clearInterval(BROADCAST_POLL_TIMER);
  BROADCAST_POLL_PRESET=preset;
  BROADCAST_POLL_TIMER=setInterval(()=>pollPresetBroadcast(preset),3000);
  pollPresetBroadcast(preset);
}

function pollPresetBroadcast(preset){
  fetch('/api/preset-broadcast/status?preset='+encodeURIComponent(preset)).then(r=>r.json()).then(d=>{
    (d.log||[]).slice(BROADCAST_LOG_SHOWN).forEach(m=>logMsg(m));
    BROADCAST_LOG_SHOWN=(d.log||[]).length;
    const line=document.getElementById('broadcastStatusLine');
    if(line) line.innerText=d.running?'рассылка работает...':'остановлено';
    const b=document.getElementById('broadcastBtn');
    const s=document.getElementById('stopBroadcastBtn');
    if(b) b.style.display=d.running?'none':'';
    if(s) s.style.display=d.running?'':'none';
    if(!d.running && BROADCAST_POLL_TIMER){
      clearInterval(BROADCAST_POLL_TIMER);BROADCAST_POLL_TIMER=null;
    }
  });
}

function refreshBroadcastButtonState(){
  if(!OPEN_PRESET)return;
  fetch('/api/preset-broadcast/status?preset='+encodeURIComponent(OPEN_PRESET)).then(r=>r.json()).then(d=>{
    const b=document.getElementById('broadcastBtn');
    const s=document.getElementById('stopBroadcastBtn');
    const line=document.getElementById('broadcastStatusLine');
    if(b) b.style.display=d.running?'none':'';
    if(s) s.style.display=d.running?'':'none';
    if(line) line.innerText=d.running?'рассылка работает...':'';
    if(d.running){BROADCAST_LOG_SHOWN=0;startBroadcastPolling(OPEN_PRESET);}
  });
}

document.addEventListener('DOMContentLoaded',()=>{loadPresets();loadVkPoolStats();});
</script>
"""

@app.route("/presets")
def presets_page():
    return _render(PAGE_PRESETS, "presets", None, f={})


@app.route("/api/vk-pool-stats", methods=["GET"])
def api_vk_pool_stats():
    return jsonify(_vk_pool_stats())


@app.route("/api/bulk-register", methods=["POST"])
def api_bulk_register():
    """Берёт из пула VK-токенов (Настройки) до `count` неиспользованных,
    превращает каждый в Twinby JWT (_register_vk_token) и добавляет как
    новый аккаунт в ACCOUNTS (видно во вкладке «Аккаунты»).
    JSON: {count: int, label_prefix?: str}
    """
    data = request.get_json(force=True)
    try:
        count = int(data.get("count", 0))
    except (TypeError, ValueError):
        count = 0
    label_prefix = (data.get("label_prefix") or "acc").strip() or "acc"
    if count <= 0:
        return jsonify({"ok": False, "error": "count должен быть больше 0"})

    free_tokens = [t for t in VK_POOL if not t.get("used")]
    if not free_tokens:
        return jsonify({"ok": False, "error": "В пуле нет свободных VK-токенов. Добавь их в Настройках."})

    settings = dict(DEFAULTS)
    settings.update(_load_onboarding_settings())

    import random as _random
    # ПОДТВЕРЖДЕНО багом (сессия 2026-11-23): раньше брался фиксированный
    # срез free_tokens[:count] — если хоть один токен в этом срезе оказывался
    # плохим (401 Authentication error и т.п.), счётчик "зарегано" просто
    # уменьшался, а СЛЕДУЮЩИЙ свободный токен из пула не подхватывался.
    # Теперь идём по ВСЕМ свободным токенам по очереди и останавливаемся,
    # когда либо наберём `count` успешных регистраций, либо пул свободных
    # токенов закончится — плохой токен просто пропускается, идём дальше.
    results = []
    registered = 0
    for entry in free_tokens:
        if registered >= count:
            break
        token = entry["token"]
        proxy = _get_next_proxy()  # закрепляется за аккаунтом навсегда

        r = _register_vk_token(token, proxy=proxy)
        if not r.get("ok"):
            entry["error"] = r.get("error")
            entry["used"] = True  # плохой/протухший токен — больше не пробуем его повторно
            results.append({"ok": False, "error": r.get("error"), "token": token[:24] + "..."})
            continue

        # уникальный label: предпочитаем user_id, иначе счётчик
        label = f"{label_prefix}_{r.get('user_id') or uuid.uuid4().hex[:8]}"
        n = 2
        base_label = label
        while label in ACCOUNTS:
            label = f"{base_label}_{n}"
            n += 1
        ACCOUNTS[label] = {
            "vk_token": token,
            "vk_id": entry.get("id", ""),
            "vk_pass": entry.get("pass", ""),
            "jwt": r["jwt"],
            "user_id": r.get("user_id", ""),
            "name": r.get("name", ""),
            "proxy": proxy,
            "preset": label_prefix,
            "verified": False,
            "premium": False,
            "likes_done": 0,
            "dislikes_done": 0,
        }
        entry["used"] = True
        entry["user_id"] = r.get("user_id", "")
        entry["name"] = r.get("name", "")
        entry["error"] = None
        registered += 1

        log_lines: list = []
        tee = lambda m: log_lines.append(m)
        onboard_ok, onboard_error, video_ok, city_name = True, "", False, None
        try:
            c = _make_web_client(r["jwt"], False, tee, proxy=proxy)
            res = _run_full_onboarding(c, settings, tee, preset=label_prefix)
            onboard_ok = bool(res.get("ok"))
            onboard_error = res.get("error", "") if not onboard_ok else ""
            video_ok = bool(res.get("video_verified"))
            city_name = res.get("city")
        except Exception as e:
            onboard_ok = False
            onboard_error = str(e)

        ACCOUNTS[label]["onboarded"] = onboard_ok
        ACCOUNTS[label]["onboard_error"] = onboard_error
        ACCOUNTS[label]["video_verified"] = video_ok
        if city_name:
            ACCOUNTS[label]["city"] = city_name
        results.append({
            "ok": True, "label": label, "user_id": r.get("user_id"), "name": r.get("name"),
            "onboarded": onboard_ok, "onboard_error": onboard_error, "video_verified": video_ok,
            "city": city_name, "proxy": (proxy.split("@")[-1] if "@" in proxy else proxy),
            "log": log_lines,
        })

        # небольшая случайная пауза между регистрациями, чтобы не долбить одновременно
        time.sleep(_random.uniform(1.0, 2.5))

    _save_accounts(ACCOUNTS)
    _save_vk_pool(VK_POOL)

    return jsonify({"ok": True, "registered": registered, "requested": count,
                     "results": results, "pool_stats": _vk_pool_stats(),
                     "proxy_stats": _proxy_pool_stats()})


@app.route("/api/accounts/quick-add", methods=["POST"])
def api_accounts_quick_add():
    """Добавляет УЖЕ готовый (зарегистрированный и настроенный где-то ранее)
    Twinby-аккаунт по голому VK access token'у — БЕЗ прогона онбординга,
    сразу доступен для Лайкать/Рассылка этого пресета.
    JSON: {preset: str, tokens: [str, ...]}  (один или несколько токенов разом)

    ВАЖНО, предположение (не подтверждено трафиком): поле onboarded
    выставляется True просто потому, что аккаунт назван "готовым" — реальная
    заполненность профиля на стороне Twinby этим не проверяется и не
    гарантируется. Если профиль там на деле не дозаполнен — это не всплывёт
    тут, вылезет только при использовании (Лайкать/Рассылка используют лишь
    jwt, им заполненность профиля не важна)."""
    data = request.get_json(force=True)
    preset = (data.get("preset") or "").strip()
    tokens = data.get("tokens") or []
    if not preset:
        return jsonify({"ok": False, "error": "preset обязателен"})
    tokens = [(t or "").strip() for t in tokens if (t or "").strip()]
    if not tokens:
        return jsonify({"ok": False, "error": "Список VK-токенов пуст"})

    results = []
    added = 0
    for token in tokens:
        proxy = _get_next_proxy()  # закрепляется за аккаунтом навсегда, как и при обычной регистрации
        r = _register_vk_token(token, proxy=proxy)
        if not r.get("ok"):
            results.append({"ok": False, "error": r.get("error"), "token": token[:24] + "..."})
            continue
        label = f"{preset}_{r.get('user_id') or uuid.uuid4().hex[:8]}"
        n = 2
        base_label = label
        while label in ACCOUNTS:
            label = f"{base_label}_{n}"
            n += 1
        ACCOUNTS[label] = {
            "vk_token": token,
            "vk_id": "",
            "vk_pass": "",
            "jwt": r["jwt"],
            "user_id": r.get("user_id", ""),
            "name": r.get("name", ""),
            "proxy": proxy,
            "preset": preset,
            "onboarded": True,
            "verified": False,
            "premium": False,
            "likes_done": 0,
            "dislikes_done": 0,
        }
        added += 1
        results.append({"ok": True, "label": label, "name": r.get("name", ""), "user_id": r.get("user_id", "")})

    if added:
        _save_accounts(ACCOUNTS)
    return jsonify({"ok": True, "added": added, "requested": len(tokens), "results": results,
                     "proxy_stats": _proxy_pool_stats()})


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    f = dict(DEFAULTS)
    f.update(_load_onboarding_settings())
    if request.method == "POST":
        for key in SETTING_TEXT_FIELDS:
            f[key] = request.form.get(key, "").strip()
        f["language"] = request.form.getlist("language")
        f["interest"] = request.form.getlist("interest")
        f["zodiac"] = bool(request.form.get("zodiac"))
        f["zodiac_touch"] = bool(request.form.get("zodiac_touch"))
        _save_onboarding_settings({key: f[key] for key in (*SETTING_TEXT_FIELDS, "language", "interest", "cities", "zodiac", "zodiac_touch")})
        return _render(PAGE_SETTINGS, "settings", None, f=f, saved=True, vk_added=None, vk_stats=_vk_pool_stats(),
                       proxy_added=None, proxy_stats=_proxy_pool_stats())
    return _render(PAGE_SETTINGS, "settings", None, f=f, saved=False, vk_added=None, vk_stats=_vk_pool_stats(),
                   proxy_added=None, proxy_stats=_proxy_pool_stats())


@app.route("/api/cities", methods=["GET"])
def api_cities():
    return jsonify(get_cities_json())


@app.route("/settings/cities", methods=["POST"])
def settings_cities_save():
    """Сохраняет список выбранных городов (по именам) для случайного выбора при регистрации."""
    data = request.get_json(force=True)
    names = data.get("cities", [])
    if not isinstance(names, list):
        return jsonify({"ok": False, "error": "cities должен быть списком"})
    valid = [n for n in names if n in _CITY_BY_NAME]
    s = _load_onboarding_settings()
    s["cities"] = valid
    _save_onboarding_settings(s)
    return jsonify({"ok": True, "count": len(valid)})


@app.route("/settings/vk-pool", methods=["POST"])
def settings_vk_pool_add():
    """Добавляет VK-аккаунты (строки id:pass:token, допускается и просто token) в пул для Пресетов."""
    raw = request.form.get("vk_tokens_bulk", "")
    existing = {t.get("token") for t in VK_POOL}
    added = 0
    for line in raw.splitlines():
        if not line.strip():
            continue
        entry = _parse_vk_pool_line(line)
        token = entry["token"]
        if not token or token in existing:
            continue
        VK_POOL.append({"id": entry["id"], "pass": entry["pass"], "token": token,
                        "used": False, "user_id": "", "name": "", "error": None})
        existing.add(token)
        added += 1
    if added:
        _save_vk_pool(VK_POOL)

    f = dict(DEFAULTS)
    f.update(_load_onboarding_settings())
    return _render(PAGE_SETTINGS, "settings", None, f=f, saved=False, vk_added=added, vk_stats=_vk_pool_stats(),
                   proxy_added=None, proxy_stats=_proxy_pool_stats())


@app.route("/settings/vk-pool/clear", methods=["POST"])
def settings_vk_pool_clear():
    """Полностью очищает пул VK-токенов (и использованные, и свободные),
    чтобы можно было вставить список заново с чистого листа."""
    removed = len(VK_POOL)
    VK_POOL.clear()
    _save_vk_pool(VK_POOL)

    f = dict(DEFAULTS)
    f.update(_load_onboarding_settings())
    return _render(PAGE_SETTINGS, "settings", None, f=f, saved=False, vk_added=None,
                   vk_stats=_vk_pool_stats(), proxy_added=None, proxy_stats=_proxy_pool_stats(),
                   vk_cleared=removed)


@app.route("/settings/proxy-pool", methods=["POST"])
def settings_proxy_pool_add():
    """Добавляет прокси (ip:port:user:pass построчно) в пул."""
    raw = request.form.get("proxies_bulk", "")
    existing = set(PROXY_POOL)
    added = 0
    for line in raw.splitlines():
        proxy = line.strip().strip('"\'')
        if not proxy or proxy in existing:
            continue
        PROXY_POOL.append(proxy)
        existing.add(proxy)
        added += 1
    if added:
        _save_proxy_pool(PROXY_POOL)

    f = dict(DEFAULTS)
    f.update(_load_onboarding_settings())
    return _render(PAGE_SETTINGS, "settings", None, f=f, saved=False, vk_added=None, vk_stats=_vk_pool_stats(),
                   proxy_added=added, proxy_stats=_proxy_pool_stats())


@app.route("/api/photo-pool", methods=["GET"])
def api_photo_pool_list():
    return jsonify({"files": _list_photo_pool()})


@app.route("/settings/photo-pool", methods=["POST"])
def settings_photo_pool_upload():
    files = request.files.getlist("photos")
    added, skipped = 0, 0
    for f in files:
        if not f or not f.filename:
            continue
        suffix = Path(f.filename).suffix.lower()
        if suffix not in MIME:
            skipped += 1
            continue
        dest_name = Path(f.filename).name
        dest = PHOTO_POOL_DIR / dest_name
        if dest.exists():
            dest = PHOTO_POOL_DIR / f"{uuid.uuid4().hex[:8]}_{dest_name}"
        f.save(dest)
        added += 1
    return jsonify({"ok": True, "added": added, "skipped": skipped, "total": len(_list_photo_pool())})


@app.route("/settings/photo-pool/<path:filename>", methods=["DELETE"])
def settings_photo_pool_delete(filename):
    safe_name = Path(filename).name
    dest = PHOTO_POOL_DIR / safe_name
    if dest.exists() and dest.is_file():
        dest.unlink()
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "не найдено"})


@app.route("/photo-pool-file/<path:filename>", methods=["GET"])
def photo_pool_file(filename):
    return send_from_directory(PHOTO_POOL_DIR, Path(filename).name)


@app.route("/api/video-pool", methods=["GET"])
def api_video_pool_list():
    return jsonify({"files": _list_video_pool()})


@app.route("/settings/video-pool", methods=["POST"])
def settings_video_pool_upload():
    files = request.files.getlist("videos")
    added, skipped = 0, 0
    for f in files:
        if not f or not f.filename:
            continue
        suffix = Path(f.filename).suffix.lower()
        if suffix not in VIDEO_EXTS:
            skipped += 1
            continue
        dest_name = Path(f.filename).name
        dest = VIDEO_POOL_DIR / dest_name
        if dest.exists():
            dest = VIDEO_POOL_DIR / f"{uuid.uuid4().hex[:8]}_{dest_name}"
        f.save(dest)
        added += 1
    return jsonify({"ok": True, "added": added, "skipped": skipped, "total": len(_list_video_pool())})


@app.route("/settings/video-pool/<path:filename>", methods=["DELETE"])
def settings_video_pool_delete(filename):
    safe_name = Path(filename).name
    dest = VIDEO_POOL_DIR / safe_name
    if dest.exists() and dest.is_file():
        dest.unlink()
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "не найдено"})


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
    """Добавить аккаунт через VK токен. JSON: {label, vk_token}"""
    import httpx
    data = request.get_json(force=True)
    label = (data.get("label") or "").strip()
    vk_token = (data.get("vk_token") or "").strip()
    if not label or not vk_token:
        return jsonify({"ok": False, "error": "need label and vk_token"})
    try:
        resp = httpx.post(
            f"{tw.BASE}{AUTH_VK_PATH}",
            json={"provider": "vk", "token": vk_token},
            headers={"User-Agent": tw.UA, "Accept": "application/json",
                     "Content-Type": "application/json"},
            timeout=30,
        )
        if resp.status_code >= 400:
            return jsonify({"ok": False, "error": f"Статус {resp.status_code}: {resp.text[:300]}"})
        rd = resp.json()
        jwt_token = rd.get("accessToken", "")
        if not jwt_token:
            return jsonify({"ok": False, "error": "Нет accessToken в ответе"})
        user_name, user_id = "", ""
        try:
            me_resp = httpx.get(
                f"{tw.BASE}{tw.API['me']}",
                headers={"User-Agent": tw.UA, "Accept": "application/json",
                         "Authorization": f"JWT {jwt_token}"},
                timeout=15,
            )
            if me_resp.status_code < 400:
                me = me_resp.json()
                user_name = me.get("name", "")
                user_id = str(me.get("id", ""))
        except Exception:
            pass
        ACCOUNTS[label] = {"vk_token": vk_token, "jwt": jwt_token,
                           "user_id": user_id, "name": user_name}
        _save_accounts(ACCOUNTS)
        return jsonify({"ok": True, "jwt": jwt_token, "user_id": user_id, "name": user_name})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/accounts/<label>", methods=["DELETE"])
def api_accounts_remove(label):
    if label in ACCOUNTS:
        del ACCOUNTS[label]
        _save_accounts(ACCOUNTS)
    return jsonify({"ok": True})


@app.route("/api/accounts/<label>/flags", methods=["POST"])
def api_accounts_set_flags(label):
    """Ручные флаги статуса анкеты. JSON: {verified?: bool, premium?: bool}.
    Ни на что в логике лайканья не влияют — чисто отображение/учёт."""
    if label not in ACCOUNTS:
        return jsonify({"ok": False, "error": "аккаунт не найден"})
    data = request.get_json(force=True)
    if "verified" in data:
        ACCOUNTS[label]["verified"] = bool(data["verified"])
    if "premium" in data:
        ACCOUNTS[label]["premium"] = bool(data["premium"])
    _save_accounts(ACCOUNTS)
    return jsonify({"ok": True})


# ---- Массовое лайканье по пресету: параллельно по всем анкетам пресета, -----
# с глобальной памятью уже встреченных анкет-кандидатов (SEEN_USERS) и
# авто-резюмом после лимита (429) через ~20 минут, пока лента не опустеет. --
LIKE_LIMIT_MAX_ROUNDS = 500  # защитный потолок, чтобы цикл не крутился вечно при багах
LIKE_COOLDOWN_SEC = 20 * 60

LIKE_JOBS_LOCK = threading.Lock()
LIKE_JOBS: dict[str, dict] = {}  # preset -> {"running","stop_event","log","accounts"}


def _is_limit_error(msg: str) -> bool:
    low = msg.lower()
    return "429" in msg or "limit" in low or "лимит" in low or "too many" in low


def _is_token_expired_error(msg: str) -> bool:
    low = msg.lower()
    return "token_expired" in low or ("401" in msg and "expired" in low)


def _is_auth_rejected_error(err: str) -> bool:
    """Отличает подтверждённый отказ АУТЕНТИФИКАЦИИ от Twinby (сервер явно
    вернул 4xx на сам обмен VK-токена на JWT — статус "статус 4.." из
    _register_vk_token) от транзитных сетевых сбоев ("сетевая ошибка: ...",
    SSL/timeout/прокси) или 5xx сервера. Используется только чтобы пометить
    аккаунт как "мёртвый" и больше не дёргать его зря — НЕ утверждает, что
    причина именно бан (может быть и то, что VK-токен одноразовый и уже
    использован — обе версии подтверждённым трафиком не различить, но для
    практической цели "хватит пытаться" это не важно)."""
    err = err or ""
    return err.startswith("статус 4")


def _mark_account_dead(label: str, reason: str, tee) -> None:
    acc = ACCOUNTS.get(label)
    if not acc:
        return
    acc["dead"] = True
    acc["dead_reason"] = reason
    _save_accounts(ACCOUNTS)
    tee(f"аккаунт помечен как мёртвый (больше не будет пытаться): {reason}")


def _refresh_account_jwt(label: str, tee) -> bool:
    """На 401 TOKEN_EXPIRED пробуем повторно обменять УЖЕ сохранённый VK-токен
    этого аккаунта на новый Twinby JWT через тот же подтверждённый путь
    _register_vk_token (тот самый by-token обмен, что и при регистрации).
    НЕ ПРОВЕРЕНО живьём, что Twinby вообще разрешает повторный обмен одного и
    того же VK-токена — это предположение, а не подтверждённый факт. Если он
    не сработает, вернётся ok=False и лог покажет точную причину. Если отказ
    — это подтверждённый 4xx именно от Twinby (а не сетевой сбой), аккаунт
    помечается "мёртвым" (_mark_account_dead), чтобы Лайкать/Рассылка больше
    не тратили на него время при следующих запусках."""
    acc = ACCOUNTS.get(label)
    if not acc or not acc.get("vk_token"):
        tee("обновить JWT нечем — нет сохранённого vk_token")
        return False
    r = _register_vk_token(acc["vk_token"], proxy=acc.get("proxy", ""))
    if not r.get("ok"):
        err = r.get("error") or ""
        tee(f"не удалось обновить JWT повторным VK-обменом: {err}")
        if _is_auth_rejected_error(err):
            _mark_account_dead(label, err, tee)
        return False
    acc["jwt"] = r["jwt"]
    _save_accounts(ACCOUNTS)
    tee("JWT обновлён (повторный обмен сохранённого VK-токена)")
    return True


def _cooldown_wait(seconds: int, stop_event: "threading.Event") -> bool:
    """Ждёт `seconds`, проверяя stop_event каждые 5 сек. True, если остановлено досрочно."""
    waited = 0
    step = 5
    while waited < seconds:
        if stop_event.is_set():
            return True
        time.sleep(min(step, seconds - waited))
        waited += step
    return False


def _like_loop_for_account(label: str, stop_event: "threading.Event", tee,
                            delay_min: float = 0.5, delay_max: float = 1.5) -> dict:
    """Лайкает ленту одного аккаунта, пока она не опустеет или не наберётся
    много обычных (не лимитных) ошибок подряд. На лимит (429) — ждёт ~20 минут
    и резюмит (одинаково для всех — премиум просто не должен словить лимит
    на стороне сервера, отдельной ветки на это в коде нет).
    delay_min/delay_max — пауза между действиями (свайпами), настраивается
    в пресете (по умолчанию как было раньше, 0.5-1.5 сек)."""
    import random
    acc = ACCOUNTS.get(label)
    if not acc or not acc.get("jwt"):
        return {"liked": 0, "disliked": 0, "rounds": 0, "errors": 0, "stopped_reason": "нет JWT"}
    proxy = acc.get("proxy", "")

    def build_client():
        return _make_web_client(ACCOUNTS[label]["jwt"], False, tee, proxy=proxy)

    c = build_client()
    liked = disliked = errors = rounds = 0
    stopped_reason = ""
    token_refresh_failed = False
    while rounds < LIKE_LIMIT_MAX_ROUNDS and not stop_event.is_set():
        rounds += 1
        try:
            result = c.call("GET", DATING_SEARCH_PATH)
        except RuntimeError as e:
            msg = str(e)
            if _is_token_expired_error(msg) and not token_refresh_failed:
                if _refresh_account_jwt(label, tee):
                    c = build_client()
                    rounds -= 1  # не считаем эту попытку раундом
                    continue
                token_refresh_failed = True
                stopped_reason = "токен истёк, обновить не удалось"
                break
            if _is_limit_error(msg):
                tee(f"лимит на загрузке ленты, жду {LIKE_COOLDOWN_SEC // 60} мин...")
                if _cooldown_wait(LIKE_COOLDOWN_SEC, stop_event):
                    stopped_reason = "остановлено"
                    break
                continue
            stopped_reason = f"ошибка ленты: {e}"
            break
        batch = _parse_feed(result)
        if not batch:
            stopped_reason = "лента пуста"
            break
        for u in batch:
            if stop_event.is_set():
                stopped_reason = "остановлено"
                break
            uid = u["id"]
            action_type = 1 if _was_seen(uid) else 0
            try:
                c.call("POST", DATING_ACTION_PATH, json={"user": uid, "type": action_type})
            except RuntimeError as e:
                msg = str(e)
                if _is_token_expired_error(msg) and not token_refresh_failed:
                    if _refresh_account_jwt(label, tee):
                        c = build_client()
                        break  # перезапустить раунд (свежая лента) с новым JWT
                    token_refresh_failed = True
                    stopped_reason = "токен истёк, обновить не удалось"
                    break
                if _is_limit_error(msg):
                    tee(f"лимит лайков (сделано {liked}), жду {LIKE_COOLDOWN_SEC // 60} мин...")
                    if _cooldown_wait(LIKE_COOLDOWN_SEC, stop_event):
                        stopped_reason = "остановлено"
                        break
                    continue
                errors += 1
                tee(f"ошибка действия (user={uid}): {msg[:150]}")
                if errors >= 5:
                    stopped_reason = "много обычных ошибок подряд"
                    break
                continue
            if action_type == 0:
                liked += 1
                _mark_seen(uid, "like", label)
                tee(f"LIKE #{liked}: {u.get('name', '')}, {u.get('age', '')} (id={uid})")
            else:
                disliked += 1
                _mark_seen(uid, "dislike", label)
                tee(f"DISLIKE (уже встречался раньше, не дублируем лайк): id={uid}")
            time.sleep(random.uniform(delay_min, delay_max))
        if stopped_reason:
            break
    acc["likes_done"] = acc.get("likes_done", 0) + liked
    acc["dislikes_done"] = acc.get("dislikes_done", 0) + disliked
    return {"liked": liked, "disliked": disliked, "rounds": rounds, "errors": errors,
            "stopped_reason": stopped_reason}


def _run_preset_like_job(preset: str) -> None:
    with LIKE_JOBS_LOCK:
        job = LIKE_JOBS[preset]
        stop_event = job["stop_event"]
    all_preset = [(label, acc) for label, acc in ACCOUNTS.items() if acc.get("preset") == preset]
    dead = [label for label, acc in all_preset if acc.get("dead")]
    accs = [label for label, acc in all_preset if acc.get("jwt") and not acc.get("dead")]
    if dead:
        job["log"].append(f"Пропускаю {len(dead)} помеченных мёртвыми анкет(у): {', '.join(dead)}")
    if not accs:
        job["log"].append("Нет анкет с JWT в этом пресете.")
        job["running"] = False
        return
    preset_cfg = _load_presets().get(preset) or {}
    delay_min, delay_max = _preset_delay(preset_cfg, "like_delay_min", "like_delay_max", 0.5, 1.5)
    job["log"].append(f"Старт: {len(accs)} анкет(ы), лайкаю параллельно (пауза {delay_min}-{delay_max} сек)...")

    def worker(label):
        tee = lambda m: job["log"].append(f"[{label}] {m}")
        res = _like_loop_for_account(label, stop_event, tee, delay_min=delay_min, delay_max=delay_max)
        with LIKE_JOBS_LOCK:
            job["accounts"][label] = res
        tee(f"готово: лайков {res['liked']}, дизлайков {res['disliked']}, стоп: {res.get('stopped_reason') or '-'}")

    threads = [threading.Thread(target=worker, args=(label,), daemon=True) for label in accs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    _save_accounts(ACCOUNTS)
    _save_seen_users(SEEN_USERS)
    job["log"].append("Все анкеты пресета завершили (или были остановлены).")
    job["running"] = False


@app.route("/api/preset-set-gender", methods=["POST"])
def api_preset_set_gender():
    """PATCH /api/users/me/filter/ {"gender": "m"|"f"|"n"} для каждой анкеты пресета.
    Значение "n" (любой пол) подтверждено HAR-думпом из twinby_onboard.FILTER_DEFAULT."""
    data = request.get_json(force=True)
    preset = (data.get("preset") or "").strip()
    gender = (data.get("gender") or "").strip()
    if not preset:
        return jsonify({"ok": False, "error": "preset обязателен"})
    if gender not in ("m", "f", "n"):
        return jsonify({"ok": False, "error": "gender должен быть m/f/n"})
    accs = [(label, acc) for label, acc in ACCOUNTS.items()
            if acc.get("preset") == preset and acc.get("jwt") and not acc.get("dead")]
    if not accs:
        return jsonify({"ok": True, "ok_count": 0, "fail_count": 0, "errors": [], "note": "нет анкет с JWT в этом пресете"})
    ok_count, fail_count, errors = 0, 0, []
    for label, acc in accs:
        try:
            r = httpx.patch(
                f"{tw.BASE}/api/users/me/filter/",
                headers={"User-Agent": tw.UA, "Authorization": f"JWT {acc['jwt']}",
                         "Content-Type": "application/json", "Accept": "application/json"},
                json={"gender": gender}, timeout=15,
            )
            if r.status_code < 400:
                ok_count += 1
            else:
                fail_count += 1
                errors.append(f"{label}: {r.status_code} {r.text[:150]}")
        except Exception as e:
            fail_count += 1
            errors.append(f"{label}: {e}")
    return jsonify({"ok": True, "ok_count": ok_count, "fail_count": fail_count, "errors": errors[:20]})


@app.route("/api/preset-like/start", methods=["POST"])
def api_preset_like_start():
    data = request.get_json(force=True)
    preset = (data.get("preset") or "").strip()
    if not preset:
        return jsonify({"ok": False, "error": "preset обязателен"})
    with LIKE_JOBS_LOCK:
        existing = LIKE_JOBS.get(preset)
        if existing and existing.get("running"):
            return jsonify({"ok": False, "error": "Этот пресет уже лайкает."})
        LIKE_JOBS[preset] = {"running": True, "stop_event": threading.Event(), "log": [], "accounts": {}}
    th = threading.Thread(target=_run_preset_like_job, args=(preset,), daemon=True)
    th.start()
    return jsonify({"ok": True})


@app.route("/api/preset-like/stop", methods=["POST"])
def api_preset_like_stop():
    data = request.get_json(force=True)
    preset = (data.get("preset") or "").strip()
    with LIKE_JOBS_LOCK:
        job = LIKE_JOBS.get(preset)
        if job:
            job["stop_event"].set()
    return jsonify({"ok": True})


@app.route("/api/preset-like/status", methods=["GET"])
def api_preset_like_status():
    preset = request.args.get("preset", "").strip()
    with LIKE_JOBS_LOCK:
        job = LIKE_JOBS.get(preset)
        if not job:
            return jsonify({"ok": True, "running": False, "log": [], "accounts": {}})
        return jsonify({"ok": True, "running": job["running"], "log": job["log"][-300:], "accounts": job["accounts"]})


# ---- Рассылка: при мэтче без нашего ответа — отправить одно случайное -----
# сообщение из списка, заданного в настройках пресета. Непрерывный фоновый
# опрос списка чатов (/api/chats/list/), пока не остановят вручную — новые
# мэтчи могут появляться в любой момент, это не разовая задача как Лайкать.
# Все 3 эндпоинта (список чатов, история сообщений, отправка) подтверждены
# реальным трафиком через mitmproxy 2026-11-23. Поведение этого же эндпоинта
# отправки на 429/401 отдельно НЕ проверялось живьём — предполагаем ту же
# схему лимитов/протухания JWT, что и везде в Twinby API, и переиспользуем
# тот же _is_limit_error/_is_token_expired_error/_refresh_account_jwt. Если
# окажется иначе — лог покажет точную ошибку с сервера.
BROADCAST_POLL_SEC = 20  # пауза между проходами по списку чатов одного аккаунта

BROADCAST_JOBS_LOCK = threading.Lock()
BROADCAST_JOBS: dict[str, dict] = {}  # preset -> {"running","stop_event","log","accounts"}


def _chat_list_all_pages(c, max_pages: int = 10) -> list[dict]:
    """Проходит /api/chats/list/ по всем страницам (через поле next), до
    max_pages на всякий случай, чтобы не крутиться вечно при баге пагинации."""
    chats: list[dict] = []
    page = 1
    while page <= max_pages:
        result = c.call("GET", f"{CHATS_LIST_PATH}?size=20&page={page}")
        chats.extend(result.get("results") or [])
        if not result.get("next"):
            break
        page += 1
    return chats


def _empty_chats_all_pages(c, max_pages: int = 10) -> list[dict]:
    """Проходит EMPTY_CHATS_PATH (мэтчи без переписки — "New matches" в
    приложении) по страницам. В ответе нет lastMessage вообще (там его
    физически не бывает — чат ещё пустой), поэтому элементы отсюда приводим
    к тому же виду, что отдаёт CHATS_LIST_PATH (id + lastMessage=None), чтобы
    их можно было обработать тем же циклом ниже без дублирования логики."""
    size = 20
    raw: list[dict] = []
    page = 1
    while page <= max_pages:
        result = c.call("GET", f"{EMPTY_CHATS_PATH}?size={size}&page={page}")
        batch = result.get("results") or []
        raw.extend(batch)
        has_next = result.get("next")
        if has_next is None:
            # В ответе этого эндпоинта поле next не подтверждено (видели только
            # первую страницу с 2 мэтчами) — подстраховка по длине страницы.
            if len(batch) < size:
                break
        elif not has_next:
            break
        page += 1
    normalized = []
    for item in raw:
        normalized.append({
            "id": item.get("id"),
            "lastMessage": None,
            "interlocutor": item.get("interlocutor"),
        })
    return normalized


def _broadcast_loop_for_account(label: str, messages: list[str], stop_event: "threading.Event", tee,
                                 delay_min: float = 0.5, delay_max: float = 1.5) -> dict:
    """Непрерывно опрашивает список чатов аккаунта. Для каждого чата, где мы
    ещё НИ РАЗУ не писали (проверяется не только по lastMessage.ownerId —
    это только быстрый отсев, а по полной истории первой страницы, т.к.
    lastMessage может снова быть от собеседника после нашего более раннего
    сообщения), отправляет одно случайное сообщение из списка пресета.
    Уже отвеченные чаты запоминаются в MESSAGED_CHATS, чтобы не перепроверять
    их историю на каждом новом проходе.
    delay_min/delay_max — пауза между отправками сообщений, настраивается
    в пресете (по умолчанию как было раньше, 0.5-1.5 сек)."""
    import random
    acc = ACCOUNTS.get(label)
    if not acc or not acc.get("jwt"):
        return {"sent": 0, "checked": 0, "errors": 0, "stopped_reason": "нет JWT"}
    proxy = acc.get("proxy", "")
    my_id = str(acc.get("user_id") or "")

    def build_client():
        return _make_web_client(ACCOUNTS[label]["jwt"], False, tee, proxy=proxy)

    c = build_client()
    sent = checked = errors = 0
    stopped_reason = ""
    token_refresh_failed = False

    while not stop_event.is_set():
        try:
            chats = _chat_list_all_pages(c)
        except RuntimeError as e:
            msg = str(e)
            if _is_token_expired_error(msg) and not token_refresh_failed:
                if _refresh_account_jwt(label, tee):
                    c = build_client()
                    continue
                token_refresh_failed = True
                stopped_reason = "токен истёк, обновить не удалось"
                break
            if _is_limit_error(msg):
                tee(f"лимит на списке чатов, жду {LIKE_COOLDOWN_SEC // 60} мин...")
                if _cooldown_wait(LIKE_COOLDOWN_SEC, stop_event):
                    stopped_reason = "остановлено"
                    break
                continue
            errors += 1
            tee(f"ошибка получения списка чатов: {msg[:150]}")
            if errors >= 5:
                stopped_reason = "много обычных ошибок подряд"
                break
            if _cooldown_wait(BROADCAST_POLL_SEC, stop_event):
                stopped_reason = "остановлено"
                break
            continue

        try:
            empty_chats = _empty_chats_all_pages(c)
        except RuntimeError as e:
            msg = str(e)
            if _is_token_expired_error(msg) and not token_refresh_failed:
                if _refresh_account_jwt(label, tee):
                    c = build_client()
                    continue
                token_refresh_failed = True
                stopped_reason = "токен истёк, обновить не удалось"
                break
            # Мэтчи без переписки — вторичный источник, не валим из-за него
            # весь цикл рассылки (основные chats уже получены выше).
            empty_chats = []
            tee(f"не удалось получить мэтчи без переписки: {msg[:150]}")

        seen_ids = {str(chat.get("id") or "") for chat in chats}
        new_matches = [ec for ec in empty_chats if str(ec.get("id") or "") not in seen_ids]
        if new_matches:
            tee(f"мэтчей без переписки (New matches), не входящих в chats/list: {len(new_matches)}")
        chats = chats + new_matches

        for chat in chats:
            if stop_event.is_set():
                stopped_reason = "остановлено"
                break
            checked += 1
            chat_id = str(chat.get("id") or "")
            if not chat_id or _was_messaged(chat_id):
                continue
            last = chat.get("lastMessage") or {}
            if last and str(last.get("ownerId") or "") == my_id:
                # Последнее сообщение в чате уже от нас — считаем отвеченным,
                # в полную историю за этим не лезем (быстрый, но надёжный путь).
                _mark_messaged(chat_id, label, "(уже отвечали — lastMessage наш)")
                continue
            try:
                hist = c.call("GET", f"{CHAT_MESSAGES_PATH_TMPL.format(chat_id=chat_id)}?page=1&size=40")
            except RuntimeError as e:
                msg = str(e)
                if _is_token_expired_error(msg) and not token_refresh_failed:
                    if _refresh_account_jwt(label, tee):
                        c = build_client()
                        continue
                    token_refresh_failed = True
                    stopped_reason = "токен истёк, обновить не удалось"
                    break
                errors += 1
                tee(f"ошибка истории чата {chat_id}: {msg[:150]}")
                continue
            already_sent = any(str(m.get("ownerId") or "") == my_id for m in (hist.get("results") or []))
            if already_sent:
                _mark_messaged(chat_id, label, "(уже отвечали — найдено в истории)")
                continue
            if not messages:
                continue
            text = random.choice(messages)
            try:
                c.call("POST", CHAT_MESSAGES_PATH_TMPL.format(chat_id=chat_id),
                       json={"id": str(uuid.uuid4()), "text": text, "attaches": []})
            except RuntimeError as e:
                msg = str(e)
                if _is_token_expired_error(msg) and not token_refresh_failed:
                    if _refresh_account_jwt(label, tee):
                        c = build_client()
                        continue
                    token_refresh_failed = True
                    stopped_reason = "токен истёк, обновить не удалось"
                    break
                if _is_limit_error(msg):
                    tee(f"лимит отправки сообщений, жду {LIKE_COOLDOWN_SEC // 60} мин...")
                    if _cooldown_wait(LIKE_COOLDOWN_SEC, stop_event):
                        stopped_reason = "остановлено"
                        break
                    continue
                errors += 1
                tee(f"ошибка отправки в чат {chat_id}: {msg[:150]}")
                if errors >= 5:
                    stopped_reason = "много обычных ошибок подряд"
                    break
                continue
            sent += 1
            _mark_messaged(chat_id, label, text)
            who = (chat.get("interlocutor") or {}).get("name", "?")
            tee(f"MSG #{sent} -> {who}: {text!r}")
            time.sleep(random.uniform(delay_min, delay_max))
        if stopped_reason:
            break
        if _cooldown_wait(BROADCAST_POLL_SEC, stop_event):
            stopped_reason = "остановлено"
            break

    return {"sent": sent, "checked": checked, "errors": errors, "stopped_reason": stopped_reason}


def _run_preset_broadcast_job(preset: str, messages: list[str]) -> None:
    with BROADCAST_JOBS_LOCK:
        job = BROADCAST_JOBS[preset]
        stop_event = job["stop_event"]
    all_preset = [(label, acc) for label, acc in ACCOUNTS.items() if acc.get("preset") == preset]
    dead = [label for label, acc in all_preset if acc.get("dead")]
    accs = [label for label, acc in all_preset if acc.get("jwt") and not acc.get("dead")]
    if dead:
        job["log"].append(f"Пропускаю {len(dead)} помеченных мёртвыми анкет(у): {', '.join(dead)}")
    if not accs:
        job["log"].append("Нет анкет с JWT в этом пресете.")
        job["running"] = False
        return
    preset_cfg = _load_presets().get(preset) or {}
    delay_min, delay_max = _preset_delay(preset_cfg, "msg_delay_min", "msg_delay_max", 0.5, 1.5)
    job["log"].append(f"Старт рассылки: {len(accs)} анкет(ы), сообщений в списке: {len(messages)}, пауза {delay_min}-{delay_max} сек.")

    def worker(label):
        tee = lambda m: job["log"].append(f"[{label}] {m}")
        res = _broadcast_loop_for_account(label, messages, stop_event, tee, delay_min=delay_min, delay_max=delay_max)
        with BROADCAST_JOBS_LOCK:
            job["accounts"][label] = res
        tee(f"готово: отправлено {res['sent']}, проверено чатов {res['checked']}, стоп: {res.get('stopped_reason') or '-'}")

    threads = [threading.Thread(target=worker, args=(label,), daemon=True) for label in accs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    job["log"].append("Все анкеты пресета завершили рассылку (или были остановлены).")
    job["running"] = False


@app.route("/api/preset-broadcast/start", methods=["POST"])
def api_preset_broadcast_start():
    data = request.get_json(force=True)
    preset = (data.get("preset") or "").strip()
    if not preset:
        return jsonify({"ok": False, "error": "preset обязателен"})
    presets = _load_presets()
    raw = (presets.get(preset) or {}).get("messages", "")
    messages = [m.strip() for m in raw.splitlines() if m.strip()]
    if not messages:
        return jsonify({"ok": False, "error": "Список сообщений для рассылки пуст — заполни его ниже и сохрани."})
    with BROADCAST_JOBS_LOCK:
        existing = BROADCAST_JOBS.get(preset)
        if existing and existing.get("running"):
            return jsonify({"ok": False, "error": "Рассылка для этого пресета уже идёт."})
        BROADCAST_JOBS[preset] = {"running": True, "stop_event": threading.Event(), "log": [], "accounts": {}}
    th = threading.Thread(target=_run_preset_broadcast_job, args=(preset, messages), daemon=True)
    th.start()
    return jsonify({"ok": True})


@app.route("/api/preset-broadcast/stop", methods=["POST"])
def api_preset_broadcast_stop():
    data = request.get_json(force=True)
    preset = (data.get("preset") or "").strip()
    with BROADCAST_JOBS_LOCK:
        job = BROADCAST_JOBS.get(preset)
        if job:
            job["stop_event"].set()
    return jsonify({"ok": True})


@app.route("/api/preset-broadcast/status", methods=["GET"])
def api_preset_broadcast_status():
    preset = request.args.get("preset", "").strip()
    with BROADCAST_JOBS_LOCK:
        job = BROADCAST_JOBS.get(preset)
        if not job:
            return jsonify({"ok": True, "running": False, "log": [], "accounts": {}})
        return jsonify({"ok": True, "running": job["running"], "log": job["log"][-300:], "accounts": job["accounts"]})


@app.route("/api/profile", methods=["POST"])
def api_profile():
    """Получить профиль. JSON: {token}"""
    import httpx
    data = request.get_json(force=True)
    token = (data.get("token") or "").strip().strip('"\'')
    if not token:
        return jsonify({"ok": False, "error": "need token"})
    try:
        resp = httpx.get(
            f"{tw.BASE}{tw.API['me']}",
            headers={"User-Agent": tw.UA, "Accept": "application/json",
                     "Authorization": f"JWT {token}"},
            timeout=15,
        )
        if resp.status_code >= 400:
            return jsonify({"ok": False, "error": f"Статус {resp.status_code}"})
        return jsonify({"ok": True, "profile": resp.json()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ---- Новый SPA-фронт ----
@app.route("/spa")
def spa_page():
    """Отдаём index.html из корня проекта."""
    html_path = Path(__file__).parent / "index.html"
    return html_path.read_text(encoding="utf-8"), 200, {"Content-Type": "text/html; charset=utf-8"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
