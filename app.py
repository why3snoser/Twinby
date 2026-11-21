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
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request
from curl_cffi import requests as cffi_requests
from curl_cffi import CurlHttpVersion

import twinby_onboard as tw

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

AUTH_VK_PATH = "/api/auth/v2/oauth/by-token"   # прямой VK access token → JWT
DATING_SEARCH_PATH = "/api/users/search/?size=50"  # GET → список анкет для свайпа (confirmed via mitmproxy)
USERS_PUBLIC_PATH = "/api/users/{uid}/"           # GET → публичный профиль по ID

# Хранилище аккаунтов в памяти (сбрасывается при рестарте). Формат: {label: {vk_token, jwt, user_id, name}}
ACCOUNTS: dict[str, dict] = {}

def _page(tab: str, content: str, log: str = None, error: str = None) -> str:
    """Layout wrapper. tab = 'main'|'accounts'|'swipe'."""
    tabs_html = ""
    for t, href, label in [("main", "/", "Онбординг"), ("accounts", "/accounts", "Аккаунты"), ("swipe", "/swipe", "Свайпы")]:
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
    <p class="hint">Софт сам отправит POST /api/auth/v2/oauth/by-token с provider=vk и получит JWT.
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


def _make_web_client(token: str, dry: bool, tee):
    """Создаёт WebClient с логированием."""
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
            tee(f"Аккаунт '{label}' удалён")

        elif action == "add":
            vk_token = request.form.get("vk_token", "").strip()
            if not label:
                error = "Укажи метку для аккаунта"
            elif not vk_token:
                error = "Вставь VK access token"
            else:
                try:
                    tee(f"POST {AUTH_VK_PATH} (provider=vk)")
                    import httpx
                    resp = httpx.post(
                        f"{tw.BASE}{AUTH_VK_PATH}",
                        json={"provider": "vk", "token": vk_token},
                        headers={"User-Agent": tw.UA, "Accept": "application/json",
                                 "Content-Type": "application/json"},
                        timeout=30,
                    )
                    tee(f"Статус: {resp.status_code}")
                    if resp.status_code >= 400:
                        raise RuntimeError(f"Сервер вернул {resp.status_code}: {resp.text[:400]}")
                    data = resp.json()
                    jwt_token = data.get("accessToken", "")
                    if not jwt_token:
                        raise RuntimeError(f"В ответе нет accessToken: {json.dumps(data, ensure_ascii=False)[:400]}")

                    # Попробуем получить имя и ID профиля
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
                    }
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
            c = _make_web_client(f["token"], False, tee)

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


# ---- Вкладка: Онбординг (главная) ----
@app.route("/", methods=["GET", "POST"])
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
        c = _make_web_client(f["token"], dry, tee)

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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
