#!/usr/bin/env python3
"""
МИНИ-ТЕСТ: VK ID login через настоящий headless Chromium (Playwright)
вместо curl_cffi, чтобы проверить гипотезу — что error_code=9
"Flood control: too many requests" на auth.validateAccount вызван
несовпадением TLS-отпечатка (curl_cffi умеет максимум chrome152, а
UA заявляет Chrome/156) и/или отсутствием настоящего браузерного
JS-фингерпринта, а не блокировкой самого proxy/IP (это уже исключено —
тот же proxy через GoLogin вошёл в аккаунт без проблем).

НЕ встраивается в app.py/vk_auth.py. Отдельный скрипт для одного
прогона на ОДНОМ реальном аккаунте, чтобы проверить саму гипотезу
до того, как переписывать весь pipeline.

Идея: прокси настраивается НЕ в HTTP-клиенте, а на запуске самого
Chromium (как в GoLogin) — весь трафик идёт через настоящий браузерный
TCP/TLS-стек. Запросы (connect_internal, getAuthCode, checkAuthCode,
checkPhoneReuse, validateAccount, connect_authorize) выполняются ТЕМИ
ЖЕ телами, что и в vk_auth.py, но через page.evaluate(fetch(...)) —
то есть их реально отправляет сам Chrome со своей страницы id.vk.ru
(прошедшей полную загрузку и исполнение JS), а не наш Python-код
напрямую. Капча (RuCaptcha vkcaptcha) не меняется — это отдельный
HTTP-вызов к API RuCaptcha, браузер в нём не участвует.

Запуск:
    python3 test_browser_vk_login.py --login "+48..." --password "..." \
        --proxy "ip:port:user:pass" --rucaptcha-key "..." [--headful]

ПРОВЕРЕНО В ЭТОЙ СРЕДЕ (песочница разработки, не прод):
  - Playwright 1.63.0 + Chromium запускаются и работают.
  - proxy настраивается на запуске browser.launch(proxy=...), а не в
    HTTP-клиенте — подтверждено (goto через proxy прошёл).
  - page.evaluate(fetch(...)) механически работает (протестировано на
    allowlisted-хосте; на том конкретном тестовом хосте упёрлось в его
    собственный CSP default-src 'none' — это особенность ТОГО сайта,
    у id.vk.ru такого быть не может, т.к. сам id.vk.ru работает только
    за счёт таких же fetch-вызовов к api.vk.ru/login.vk.ru).
  - id.vk.ru из этой песочницы НЕ доступен (egress-прокси отдаёт 403 —
    политика окружения, не баг). Поэтому реальный прогон против VK
    можно сделать только там, где есть доступ к VK и к твоим прокси —
    на Render или локально у тебя. Здесь только синтаксис и механика
    Playwright проверены, сам сценарий против живого VK — нет.
"""

import argparse
import base64
import hashlib
import json
import sys
import time
import urllib.parse

from playwright.sync_api import sync_playwright

from vk_auth import (
    VK_ID_APP_ID,
    VK_API_VERSION,
    UA_BROWSER,
    SUPPORTED_WAYS,
    TWINBY_VK_APP_ID,
    TWINBY_REDIRECT_URI,
    _normalize_proxy,
    _uuid,
    _device_id,
    _human_pause,
    _pkce,
    _state,
    _extract_code_from_url,
    get_pinned_identity,
    _solve_not_robot_captcha,
    twinby_auth_v3,
)

import re as _re


def log(msg: str) -> None:
    print(msg, flush=True)


def playwright_proxy_dict(proxy_url: str) -> dict | None:
    """http://user:pass@host:port → {"server","username","password"} для Playwright."""
    if not proxy_url:
        return None
    parsed = urllib.parse.urlparse(proxy_url)
    server = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
    out = {"server": server}
    if parsed.username:
        out["username"] = parsed.username
    if parsed.password:
        out["password"] = parsed.password
    return out


def browser_fetch(page, url: str, data: dict | None = None,
                   extra_headers: dict | None = None) -> dict:
    """
    POST (или GET если data=None) ИЗ КОНТЕКСТА СТРАНИЦЫ — настоящий
    fetch() самого браузера, с его собственными cookies/TLS/заголовками,
    а не наш Python-HTTP-клиент.
    """
    body = urllib.parse.urlencode(data) if data is not None else None
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if extra_headers:
        headers.update(extra_headers)

    js = """
    async ({url, body, headers}) => {
        try {
            const opts = {method: body !== null ? 'POST' : 'GET', headers, credentials: 'include'};
            if (body !== null) opts.body = body;
            const r = await fetch(url, opts);
            const text = await r.text();
            return {ok: true, status: r.status, text: text};
        } catch (e) {
            return {ok: false, error: String(e)};
        }
    }
    """
    result = page.evaluate(js, {"url": url, "body": body, "headers": headers})
    if not result.get("ok"):
        raise RuntimeError(f"browser_fetch failed: {result.get('error')}")
    try:
        parsed = json.loads(result["text"])
    except Exception:
        parsed = None
    return {"status": result["status"], "text": result["text"], "json": parsed}


def extract_tokens_from_html(html: str) -> tuple[str, str]:
    """Та же логика, что в vk_auth._vk_id_login — вынесена сюда отдельно,
    чтобы не трогать production-код для этого теста."""
    auth_token = ""
    anonymous_token = ""

    init_start = _re.search(r"window\.init\s*=\s*\{", html)
    if init_start:
        start_pos = init_start.start() + init_start.group().index("{")
        depth = 0
        end_pos = start_pos
        in_string = False
        escape_next = False
        for i in range(start_pos, min(start_pos + 100000, len(html))):
            ch = html[i]
            if escape_next:
                escape_next = False
                continue
            if ch == "\\":
                escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end_pos = i + 1
                    break
        if end_pos > start_pos:
            try:
                init_data = json.loads(html[start_pos:end_pos])
                auth_section = init_data.get("auth", {})
                if isinstance(auth_section, dict):
                    auth_token = auth_section.get("access_token", "") or auth_section.get("auth_token", "")
                    anonymous_token = auth_section.get("anonymous_token", "")
                if not auth_token:
                    auth_token = init_data.get("access_token", "") or init_data.get("auth_token", "")
                if not anonymous_token:
                    anonymous_token = init_data.get("anonymous_token", "")
            except json.JSONDecodeError:
                pass

    if not anonymous_token:
        m = _re.search(r'"anonymous_token"\s*:\s*"(anonym[^"]+)"', html)
        if m:
            anonymous_token = m.group(1)
    if not auth_token:
        m = _re.search(r'"access_token"\s*:\s*"(eyJ[^"]+)"', html)
        if m:
            auth_token = m.group(1)

    return auth_token, anonymous_token


def run_test(login: str, password: str, proxy_raw: str, rucaptcha_key: str, headful: bool,
             app_id: str = VK_ID_APP_ID) -> dict:
    proxy = _normalize_proxy(proxy_raw)
    identity = get_pinned_identity(login, requested_proxy=proxy)
    device_id = identity["device_id"]
    my_uuid = identity["uuid"]
    pinned_proxy = identity.get("proxy") or proxy
    if pinned_proxy != proxy:
        log(f"[identity] proxy переопределён на закреплённый: {pinned_proxy}")
    proxy = pinned_proxy

    log(f"[identity] device_id={device_id[:20]}... uuid={my_uuid}")
    log(f"[proxy] {proxy.split('@')[-1] if '@' in proxy else proxy}")

    pw_proxy = playwright_proxy_dict(proxy)

    with sync_playwright() as p:
        launch_kwargs = {"headless": not headful}
        if pw_proxy:
            launch_kwargs["proxy"] = pw_proxy
        browser = p.chromium.launch(**launch_kwargs)
        page = browser.new_page(user_agent=UA_BROWSER)

        try:
            # ── Step 1: открыть настоящую страницу id.vk.ru/auth ──
            #
            # ВАЖНО (найдено по реальному трафику, а не предположением):
            # redirect_uri здесь должен указывать на id.vk.ru/account
            # (страница управления аккаунтом VK ID — "vkid_landing"),
            # А НЕ сам на себя (/auth?...). В реальном капче именно тот
            # запрос, где redirect_uri = .../account?flow_service=
            # vkid_landing_/id, в ОДНОМ И ТОМ ЖЕ ответе и ставит cookie
            # remixuas, и отдаёт непустой auth_token в window.init — то
            # есть дело было не в "двух заходах", а в неверном redirect_uri
            # (self-referencing на /auth — это была ошибка в коде, унаследованная
            # из прежней версии vk_auth.py, не подтверждённая трафиком).
            redirect_uri = urllib.parse.quote(
                "https://id.vk.ru/account?flow_service=vkid_landing_/id", safe=""
            )
            auth_url = (
                "https://id.vk.ru/auth"
                f"?uuid={my_uuid}&response_type=silent_token&v=1.3.0"
                f"&sdk_type=vkid&app_id={app_id}"
                f"&redirect_uri={redirect_uri}"
            )
            log("[1] page.goto id.vk.ru/auth (redirect_uri=.../account?flow_service=vkid_landing_/id)")
            # networkidle часто не наступает на SPA с фоновым polling —
            # берём domcontentloaded (страница реально получена и разобрана)
            # + ручная пауза, чтобы JS успел отработать и выставить window.init.
            page.goto(auth_url, wait_until="domcontentloaded", timeout=30000)
            log(f"  domcontentloaded получен, жду доп. 3с на отработку JS...")
            page.wait_for_timeout(3000)
            html = page.content()
            log(f"  загружено, len={len(html)}")

            # ВАЖНО: читаем ЖИВОЙ window.init из JS-рантайма страницы, а не
            # сериализованный DOM через page.content(). SPA после гидратации
            # может убрать/переписать исходный <script>window.init=...</script>
            # из DOM, хотя сама JS-переменная в runtime остаётся на месте.
            # page.content() в этом тесте показал auth_token пустым именно
            # по этой причине — не потому что VK его не прислал.
            live_init = page.evaluate("() => { try { return window.init || null; } catch (e) { return null; } }")
            auth_token, anonymous_token = "", ""
            if isinstance(live_init, dict):
                auth_section = live_init.get("auth", {})
                if isinstance(auth_section, dict):
                    auth_token = auth_section.get("access_token", "") or auth_section.get("auth_token", "")
                    anonymous_token = auth_section.get("anonymous_token", "")
                log(f"  window.init (live JS) прочитан: auth_token={'<есть>' if auth_token else '<пусто>'}, "
                    f"anon={'<есть>' if anonymous_token else '<пусто>'}")
            else:
                log("  window.init (live JS) недоступен — fallback на regex по HTML")

            if not auth_token or not anonymous_token:
                fb_auth, fb_anon = extract_tokens_from_html(html)
                auth_token = auth_token or fb_auth
                anonymous_token = anonymous_token or fb_anon
            log(f"  auth_token: {'<empty, норма для нового логина>' if not auth_token else auth_token[:40] + '...'}")
            log(f"  anonymous_token: {'<НЕ НАЙДЕН>' if not anonymous_token else anonymous_token[:40] + '...'}")
            if not anonymous_token:
                return {"ok": False, "error": "anonymous_token не найден на странице", "log": "see above"}

            _human_pause(1.0, 2.0)

            # ── Step 2: connect_internal ──
            log("[2] fetch connect_internal (из браузера)")
            r = browser_fetch(page, "https://login.vk.ru/?act=connect_internal", data={
                "uuid": my_uuid, "device_id": device_id, "version": "1", "app_id": app_id,
            })
            log(f"  → {r['status']}: {r['json']}")

            _human_pause(0.5, 1.2)

            # ── Step 2b: getAuthCode ──
            log("[2b] fetch auth.getAuthCode")
            m_os = _re.search(r"\(([^)]+)\)", UA_BROWSER)
            device_name = m_os.group(1) if m_os else "Windows NT 10.0; Win64; x64"
            r = browser_fetch(page, f"https://api.vk.ru/method/auth.getAuthCode?v={VK_API_VERSION}&client_id={app_id}", data={
                "device_name": device_name, "auth_code_flow": "0", "verification_hash": "",
                "force_regenerate": "1", "anonymous_token": anonymous_token, "is_switcher_flow": "",
            })
            auth_hash = ((r["json"] or {}).get("response") or {}).get("auth_hash", "")
            log(f"  → {r['status']}: auth_hash={'<got>' if auth_hash else '<none>'}")

            _human_pause(0.3, 0.8)

            # ── Step 2c: checkAuthCode ──
            if auth_hash:
                log("[2c] fetch auth.checkAuthCode")
                r = browser_fetch(page, f"https://api.vk.ru/method/auth.checkAuthCode?v={VK_API_VERSION}&client_id={app_id}", data={
                    "auth_hash": auth_hash, "web_auth": "1", "anonymous_token": anonymous_token,
                })
                log(f"  → {r['status']}: {r['json']}")
                _human_pause(0.3, 0.8)

            # ── Step 2d: checkPhoneReuse ──
            log("[2d] fetch ecosystem.checkPhoneReuse")
            r = browser_fetch(page, f"https://api.vk.ru/method/ecosystem.checkPhoneReuse?v={VK_API_VERSION}&client_id={app_id}", data={
                "login": login, "auth_token": auth_token, "super_app_token": "",
                "is_edu_flow": "0", "anonymous_token": anonymous_token,
            })
            log(f"  → {r['status']}: {r['json']}")

            _human_pause(0.4, 1.0)

            # ── Step 3: validateAccount (с капча-циклом) ──
            log("[3] fetch auth.validateAccount")
            validate_data = {
                "login": login, "client_id": app_id, "device_id": device_id,
                "sid": "", "super_app_token": "", "service_group": "",
                "supported_ways": SUPPORTED_WAYS, "auth_token": auth_token,
                "is_edu_flow": "0", "anonymous_token": anonymous_token,
            }
            sid = ""
            captcha_success_token = ""
            for attempt in range(4):
                post_data = dict(validate_data)
                if captcha_success_token:
                    post_data["success_token"] = captcha_success_token
                r = browser_fetch(page, f"https://api.vk.ru/method/auth.validateAccount?v={VK_API_VERSION}&client_id={app_id}", data=post_data)
                d = r["json"] or {}
                if "error" in d:
                    err = d["error"]
                    code = err.get("error_code", 0)
                    msg = err.get("error_msg", str(err))
                    log(f"  → ERROR [{code}] {msg}")
                    if code == 9:
                        # Это и есть проверяемая гипотеза.
                        return {"ok": False, "error": f"FLOOD CONTROL всё ещё срабатывает (code 9): {msg}",
                                "hypothesis_result": "НЕ подтвердилась — браузер не помог"}
                    if code == 14:
                        redirect_uri = err.get("redirect_uri", "")
                        if redirect_uri and rucaptcha_key:
                            token = _solve_not_robot_captcha(None, redirect_uri, rucaptcha_key, [], proxy=proxy)
                            if token:
                                captcha_success_token = token
                                continue
                        return {"ok": False, "error": f"Captcha required, не решена: {msg}"}
                    return {"ok": False, "error": f"validateAccount: [{code}] {msg}"}
                resp = d.get("response", {})
                flow_names = resp.get("flow_names", [])
                sid = resp.get("sid", "")
                log(f"  → OK flow_names={flow_names} sid={sid[:20]}...")
                break
            else:
                return {"ok": False, "error": "Captcha: превышено число попыток"}

            if "password" not in flow_names:
                return {"ok": False, "error": f"Неожиданный flow_names (нет 'password'): {flow_names}"}

            _human_pause(2.0, 4.0)

            # ── Step 4: connect_authorize (пароль) ──
            log("[4] fetch connect_authorize (пароль)")
            to_url = "https://id.vk.ru/account?flow_service=vkid_landing_/id"
            to_b64 = base64.urlsafe_b64encode(to_url.encode()).decode().rstrip("=")
            authorize_data = {
                "username": login, "password": password, "auth_token": auth_token,
                "sid": sid, "uuid": my_uuid, "v": VK_API_VERSION, "device_id": device_id,
                "service_group": "", "agreement_hash": "", "oauth_version": "",
                "max_messenger_hash": "", "oauth_force_hash": "0", "is_registration": "0",
                "oauth_response_type": "silent_token", "vkid_oauth_hash": "",
                "is_oauth_migrated_flow": "0", "oauth_state": "", "to": to_b64,
                "version": "1", "app_id": app_id,
            }
            r = browser_fetch(page, "https://login.vk.ru/?act=connect_authorize", data=authorize_data)
            d = r["json"] or {}
            log(f"  → {r['status']}: {json.dumps(d, ensure_ascii=False)[:500]}")

            if d.get("type") == "error":
                err_info = d.get("error_info", {})
                return {"ok": False, "error": f"connect_authorize error: {err_info}",
                        "hypothesis_result": "смотри текст ошибки — flood ушёл или нет?"}

            log("  VK ID логин полностью успешен (пароль принят, VK session в браузере).")

            _human_pause(1.0, 2.0)

            # ═══════════════════════════════════════════════════════════════
            # ДИАГНОСТИКА (по твоей просьбе): полный OAuth-обмен через
            # oauth2/auth и oauth.vk.ru/authorize оказался нерабочим для
            # VK-приложения Twinby (404 и 401 "no right to authorize") —
            # похоже, это мобильное SDK-приложение, обычный web OAuth ему
            # не разрешён. Вместо дальнейшего угадывания эндпоинтов — пробуем
            # ЧЕСТНО полученный access_token (из настоящего браузерного
            # VK-логина выше, не из DirectAuth) напрямую через СТАРЫЙ v2-путь
            # Twinby (/api/auth/v2/oauth/by-token). Раньше v2 давал "фейковый
            # успех" именно с токеном от левого DirectAuth — проверяем, было
            # ли дело в происхождении токена, а не в самом эндпоинте.
            # Это ТОЛЬКО диагностика. В реальном софте v2 остаётся удалённым
            # в любом случае — вопрос только в том, откуда брать Twinby JWT.
            # ═══════════════════════════════════════════════════════════════
            vk_access_token = (d.get("data") or {}).get("access_token", "")
            log(f"[5] ДИАГНОСТИКА: честный vk_access_token → Twinby v2 (/api/auth/v2/oauth/by-token)")
            log(f"  vk_access_token: {'<есть>' if vk_access_token else '<ПУСТО, тест невозможен>'}")

            if not vk_access_token:
                return {"ok": False, "error": "Нет access_token для теста v2",
                        "hypothesis_result": "ПОДТВЕРДИЛАСЬ (VK логин работает), Phase 2 (JWT для Twinby) не готов"}

            import httpx as _httpx_diag

            try:
                r_v2 = _httpx_diag.post(
                    "https://twinby.ru/api/auth/v2/oauth/by-token",
                    json={"provider": "vk", "token": vk_access_token},
                    headers={"Content-Type": "application/json"},
                    proxy=proxy if proxy else None,
                    timeout=30,
                )
                log(f"  v2 by-token → {r_v2.status_code}: {r_v2.text[:300]}")
            except Exception as e:
                return {"ok": False, "error": f"v2 by-token запрос упал: {e}",
                        "hypothesis_result": "ПОДТВЕРДИЛАСЬ (VK логин работает), v2-тест не прошёл технически"}

            if r_v2.status_code >= 400:
                return {"ok": False, "error": f"v2 by-token отказал: {r_v2.status_code} {r_v2.text[:300]}",
                        "hypothesis_result": "ПОДТВЕРДИЛАСЬ (VK логин работает), v2 не принял честный токен — значит дело не в происхождении токена"}

            d_v2 = r_v2.json()
            jwt_token = d_v2.get("accessToken", "")
            if not jwt_token:
                return {"ok": False, "error": f"v2 ответил без accessToken: {json.dumps(d_v2, ensure_ascii=False)[:300]}",
                        "hypothesis_result": "v2 принял токен, но JWT не вернул — странный случай, разбираться отдельно"}

            log(f"  Twinby JWT получен через v2: {jwt_token[:30]}...")

            # Проверка: реально ли сохраняется имя после онбординга, или
            # опять залипнет на "Макс" (это и была исходная жалоба на v2).
            test_name = "ТестИмя"
            try:
                with _httpx_diag.Client(headers={"Authorization": f"JWT {jwt_token}",
                                                  "Content-Type": "application/json"},
                                         proxy=proxy if proxy else None, timeout=30) as hc:
                    r_patch = hc.patch("https://twinby.ru/api/users/me/",
                                        json={"name": test_name, "onboardingStep": 1})
                    log(f"  PATCH name → {r_patch.status_code}")
                    r_me = hc.get("https://twinby.ru/api/users/me/")
                    me_data = r_me.json() if r_me.status_code < 400 else {}
                    actual_name = me_data.get("name", "")
                    log(f"  GET /me после PATCH → name={actual_name!r}")
            except Exception as e:
                return {"ok": True, "jwt": jwt_token,
                        "error_secondary": f"JWT получен, но проверка имени упала: {e}",
                        "hypothesis_result": "JWT получен через v2 с честным токеном, но имя проверить не удалось"}

            if actual_name == test_name:
                verdict = "ИМЯ СОХРАНИЛОСЬ — дело было в происхождении токена (DirectAuth), не в самом v2-эндпоинте"
            else:
                verdict = f"ИМЯ НЕ СОХРАНИЛОСЬ (пришло {actual_name!r} вместо {test_name!r}) — v2 сам по себе сломан, честный токен не помог"

            return {"ok": True, "jwt": jwt_token, "actual_name_after_patch": actual_name,
                    "expected_name": test_name, "hypothesis_result": verdict}

            # ---------------------------------------------------------------
            # (Код ниже — старая попытка полного OAuth code-обмена через
            # oauth.vk.ru/authorize. Оставлен закомментированным по коду
            # (не удалён физически), на случай если понадобится вернуться
            # к нему после получения реального трафика мобильного приложения.
            # Сейчас не выполняется — выше стоит return.)
            # ---------------------------------------------------------------
            code_verifier, code_challenge = _pkce()
            oauth_state = _state()

            captured_redirect = {}

            def _on_request(req):
                if req.url.startswith(TWINBY_REDIRECT_URI.split("://")[0] + "://"):
                    captured_redirect["url"] = req.url

            # Полная видимость сетевого обмена на этом шаге: статус, заголовки,
            # тело КАЖДОГО ответа в цепочке (включая редиректы 3xx), а не
            # только то, что в итоге отрисовалось в DOM. Прошлый прогон
            # показал пустой DOM — значит там либо не HTML, либо редирект,
            # который не дошёл до финальной отрисовки.
            seen_responses = []

            def _on_response(resp):
                try:
                    headers = dict(resp.headers)
                    body_preview = ""
                    ct = headers.get("content-type", "")
                    if "image" not in ct and "font" not in ct:
                        try:
                            body_preview = resp.text()[:500]
                        except Exception as e:
                            body_preview = f"<не удалось прочитать тело: {e}>"
                    seen_responses.append({
                        "url": resp.url[:200],
                        "status": resp.status,
                        "location": headers.get("location", ""),
                        "content_type": ct,
                        "body_preview": body_preview,
                    })
                except Exception as e:
                    seen_responses.append({"url": resp.url[:200], "error": str(e)})

            page.on("request", _on_request)
            page.on("response", _on_response)

            # id.vk.ru/oauth2/auth вернул ЧИСТЫЙ 404 (страницы не существует
            # вообще) — это был угаданный, не подтверждённый трафиком путь из
            # старого кода. Пробуем "legacy"-эндпоинт oauth.vk.ru/authorize,
            # который тоже был в старом коде как запасной вариант — передаём
            # ему access_token текущей VK-сессии напрямую (classic VK OAuth
            # умеет молча авторизовать новое приложение по уже имеющемуся
            # access_token, без повторного запроса пароля).
            vk_access_token = (d.get("data") or {}).get("access_token", "")
            oauth2_url = (
                "https://oauth.vk.ru/authorize"
                f"?client_id={TWINBY_VK_APP_ID}"
                f"&redirect_uri={urllib.parse.quote(TWINBY_REDIRECT_URI, safe='')}"
                f"&response_type=code&scope="
                f"&state={oauth_state}"
                f"&code_challenge={code_challenge}&code_challenge_method=S256"
                f"&v=5.199"
                f"&access_token={urllib.parse.quote(vk_access_token, safe='')}"
            )
            log(f"  vk_access_token для этой попытки: {'<есть>' if vk_access_token else '<ПУСТО — не сработает>'}")
            try:
                page.goto(oauth2_url, timeout=10000)
            except Exception as e:
                # Ожидаемо: переход на кастомную схему всегда обрывается
                # (net::ERR_ABORTED) — это не ошибка, а побочный эффект
                # перехвата. URL нам уже отдало событие "request" выше.
                log(f"  (ожидаемый обрыв навигации на кастомную схему: {type(e).__name__})")

            page.wait_for_timeout(500)
            page.remove_listener("request", _on_request)
            page.remove_listener("response", _on_response)

            log(f"  --- все сетевые ответы на этом шаге ({len(seen_responses)}) ---")
            for r in seen_responses:
                log(f"  [{r.get('status', '?')}] {r['url']}")
                if r.get("location"):
                    log(f"      Location: {r['location'][:200]}")
                if r.get("content_type"):
                    log(f"      Content-Type: {r['content_type']}")
                if r.get("body_preview"):
                    log(f"      Body: {r['body_preview'][:300]}")
                if r.get("error"):
                    log(f"      (ошибка чтения: {r['error']})")

            redirect_url = captured_redirect.get("url", "")
            if not redirect_url:
                log(f"  Twinby redirect НЕ пойман. page.url сейчас: {page.url[:150]}")
                # Диагностика: похоже, что это НЕ мгновенный редирект, а
                # страница подтверждения доступа (первая авторизация этого
                # VK-аккаунта в приложении Twinby) — ищем кнопку "Разрешить"
                # и текст страницы, чтобы понять, что там на самом деле.
                page_text = page.inner_text("body") if page.query_selector("body") else ""
                log(f"  --- текст страницы (первые 800 симв.) ---")
                log(f"  {page_text[:800]}")
                buttons = page.eval_on_selector_all(
                    "button, a[role=button], [class*=button], input[type=submit]",
                    "els => els.slice(0, 15).map(e => ({tag: e.tagName, text: (e.innerText||e.value||'').trim().slice(0,60), id: e.id, cls: (e.className||'').toString().slice(0,80)}))"
                )
                log(f"  --- найденные кнопки/клики (до 15) ---")
                for b in buttons:
                    log(f"  {b}")
                return {"ok": False, "error": "Не удалось получить authorization code для Twinby (redirect не пойман) — см. дамп страницы выше",
                        "hypothesis_result": "ПОДТВЕРДИЛАСЬ (VK логин работает), но Phase 2 (код для Twinby) не готов",
                        "vk_login_ok": True}

            log(f"  Поймали redirect: {redirect_url[:150]}")
            twinby_code = _extract_code_from_url(redirect_url)
            if not twinby_code:
                return {"ok": False, "error": f"redirect пойман, но code не найден в нём: {redirect_url[:200]}",
                        "hypothesis_result": "ПОДТВЕРДИЛАСЬ (VK логин работает), но code не распарсился",
                        "vk_login_ok": True}

            log(f"  Twinby authorization code: {twinby_code[:30]}...")

            # ── Phase 3: обменять code на Twinby JWT — обычный backend-to-backend
            # запрос к twinby.ru, фингерпринт тут не нужен, делаем его как раньше,
            # не через браузер. ──
            log("[6] twinby_auth_v3: обмен code → Twinby JWT")
            tw_result = twinby_auth_v3(twinby_code, code_verifier, device_id, state, proxy=proxy)
            if not tw_result.get("ok"):
                return {"ok": False, "error": f"twinby_auth_v3 failed: {tw_result.get('error')}",
                        "hypothesis_result": "ПОДТВЕРДИЛАСЬ (VK логин + code получены), но обмен на JWT не прошёл",
                        "vk_login_ok": True, "twinby_code_ok": True}

            log(f"  Twinby JWT получен: {tw_result['accessToken'][:30]}...")

            return {"ok": True, "jwt": tw_result["accessToken"],
                    "refresh_token": tw_result.get("refreshToken", ""),
                    "hypothesis_result": "ПОЛНОСТЬЮ ПОДТВЕРЖДЕНО — VK логин + code + Twinby JWT, весь путь через браузер"}

        finally:
            browser.close()


def run_test_twinby_web(login: str, password: str, proxy_raw: str, rucaptcha_key: str,
                         headful: bool) -> dict:
    """
    ГИПОТЕЗА (не подтверждена трафиком полностью — см. лог рассуждений):
    в HAR с POST https://twinby.ru/api/auth/v3/oauth/by-token значение
    redirectUri было "https://twinby.ru/app" (обычный https, не кастомная
    схема vk7984771://...). Запроса id.vk.ru/authorize из ТОЙ ЖЕ цепочки
    в HAR не было — поэтому неизвестно, принимает ли VK этот redirect_uri
    для client_id=7984771 вообще. Эта функция ПРОВЕРЯЕТ это напрямую:
    запускает весь VK-логин (connect_internal → validateAccount →
    connect_authorize) под app_id=7984771 с redirect_uri=https://twinby.ru/app
    и, если VK реально отдаст код на этот redirect, сразу шлёт его в
    twinby_auth_v3 (тело запроса которой УЖЕ подтверждено тем же HAR).

    НЕПОДТВЕРЖДЁННЫЕ места в этом тесте (если упадёт — вот где искать):
    - Тело connect_authorize для oauth_version=2/response_type=code флоу
      ни разу не было поймано в реальном трафике — используется то же тело,
      что и в рабочем generic-флоу (silent_token), это ДОГАДКА.
    - "Двойной заход" на /auth (device_id пустой, затем тот же URL с
      cookie remixuas) — воспроизводится ТОЧНО как в реальном трафике
      мобильного приложения, но неизвестно, обязателен ли он именно для
      этого redirect_uri.
    """
    app_id = TWINBY_VK_APP_ID
    web_redirect_uri = "https://twinby.ru/app"
    proxy = _normalize_proxy(proxy_raw)
    device_id = _device_id()
    my_uuid = _uuid()
    code_verifier, code_challenge = _pkce()
    oauth_state = _state()

    log(f"[identity] device_id={device_id[:20]}... uuid={my_uuid}")
    log(f"[proxy] {proxy.split('@')[-1] if '@' in proxy else proxy}")
    log(f"[hypothesis] app_id={app_id}, redirect_uri={web_redirect_uri} (из HAR Twinby-бэкенда, "
        f"authorize-запрос той же цепочки НЕ пойман — это проверка, не факт)")

    pw_proxy = playwright_proxy_dict(proxy)

    with sync_playwright() as p:
        launch_kwargs = {"headless": not headful}
        if pw_proxy:
            launch_kwargs["proxy"] = pw_proxy
        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(user_agent=UA_BROWSER, service_workers="block")
        page = context.new_page()

        captured_redirect = {}
        network_log: list = []
        actual_oauth_requests: list[dict[str, str]] = []

        def _on_request(req):
            parsed = urllib.parse.urlsplit(req.url)
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            try:
                form = urllib.parse.parse_qs(req.post_data or "", keep_blank_values=True)
            except Exception:
                form = {}

            if (
                (parsed.hostname == "id.vk.ru" and parsed.path in ("/authorize", "/oauth2/auth"))
                or (parsed.hostname == "login.vk.ru" and query.get("act", [""])[0] == "connect_authorize")
            ):
                def field(name: str, *aliases: str) -> str:
                    for candidate in (name, *aliases):
                        values = query.get(candidate) or form.get(candidate)
                        if values:
                            return values[0]
                    return ""

                actual_oauth_requests.append({
                    "path": parsed.path,
                    "method": field("code_challenge_method"),
                    "challenge": field("code_challenge"),
                    "state": field("state", "oauth_state"),
                    "redirect_uri": field("redirect_uri"),
                    "device_id": field("device_id"),
                })
            if req.url.startswith(web_redirect_uri):
                captured_redirect.setdefault("url", req.url)

        def _on_response(resp):
            # Общий лог сетевых вызовов к VK/Twinby — чтобы увидеть, что
            # реально происходит после клика Continue, вместо гадания по
            # DOM (там только текст "Загружается...", без подробностей).
            try:
                url = resp.url
                if not any(h in url for h in ("vk.ru", "vk.com", "twinby.ru")):
                    return
                if any(url.split("?")[0].endswith(ext) for ext in
                       (".js", ".css", ".png", ".jpg", ".svg", ".woff", ".woff2", ".ico")):
                    return
                body_snip = ""
                try:
                    body_snip = resp.text()[:300]
                except Exception:
                    body_snip = "<no body / не текст>"
                network_log.append(f"{resp.status} {url[:160]} :: {body_snip}")
                if len(network_log) > 60:
                    network_log.pop(0)
            except Exception:
                pass

        page.on("request", _on_request)
        page.on("response", _on_response)

        def _force_pkce_s256(route):
            parsed = urllib.parse.urlsplit(route.request.url)
            if parsed.hostname == "id.vk.ru" and parsed.path == "/authorize":
                params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                methods = params.get("code_challenge_method", [])
                if methods and methods[0].lower() == "s256" and methods[0] != "S256":
                    params["code_challenge_method"] = ["S256"]
                    normalized_url = urllib.parse.urlunsplit(parsed._replace(
                        query=urllib.parse.urlencode(params, doseq=True)
                    ))
                    log(
                        "[route] outgoing VK authorize "
                        f"method={params.get('code_challenge_method', [''])[0]} "
                        f"challenge_match={params.get('code_challenge', [''])[0] == code_challenge} "
                        f"state_match={params.get('state', [''])[0] == oauth_state} "
                        f"redirect_match={params.get('redirect_uri', [''])[0] == web_redirect_uri}"
                    )
                    route.continue_(url=normalized_url)
                    return
            route.continue_()

        context.route("**/authorize**", _force_pkce_s256)

        try:
            # ── Step 1: id.vk.ru/authorize (точка входа из реального трафика
            # мобильного приложения, только с другим redirect_uri) ──
            #
            # ГИПОТЕЗА (проверяем этим прогоном): в реальном веб-входе через
            # twinby.ru в браузере пользователя экран согласия был простым
            # ("«Twinby» запрашивает доступ... Разрешить"), БЕЗ выбора
            # Phone/Email, в отличие от нашего прогона, где кнопка Continue
            # оставалась disabled на экране с выбором контакта. Единственное
            # известное отличие в нашем /authorize — параметр
            # scope=vkid.personal_info email (взят из трафика МОБИЛЬНОГО
            # приложения, не веб-флоу twinby.ru — настоящий scope веб-флоу
            # не пойман, URL в скриншоте обрезался). Убираем scope полностью
            # и смотрим, появится ли простой экран.
            authorize_url = (
                "https://id.vk.ru/authorize"
                f"?client_id={app_id}&response_type=code"
                f"&redirect_uri={urllib.parse.quote(web_redirect_uri, safe='')}"
                f"&code_challenge_method=S256&code_challenge={code_challenge}"
                f"&state={oauth_state}"
            )
            log("[1] page.goto id.vk.ru/authorize (redirect_uri=https://twinby.ru/app)")
            page.goto(authorize_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)
            log(f"  итоговый URL после редиректов: {page.url[:200]}")
            if "/auth" not in page.url and "id.vk.ru" not in page.url:
                body_text = ""
                try:
                    body_text = page.inner_text("body")[:500]
                except Exception:
                    pass
                return {"ok": False,
                        "error": f"authorize не привёл на id.vk.ru/auth, текущий URL: {page.url[:200]}",
                        "page_text": body_text,
                        "verdict": "redirect_uri=https://twinby.ru/app, похоже, НЕ зарегистрирован для этого app_id — нужен другой источник"}

            # ── Step 1b: читаем window.init после первого /auth ──
            live_init = page.evaluate("() => { try { return window.init || null; } catch (e) { return null; } }")
            auth_token, anonymous_token = "", ""
            if isinstance(live_init, dict):
                auth_section = live_init.get("auth", {})
                if isinstance(auth_section, dict):
                    auth_token = auth_section.get("access_token", "") or auth_section.get("auth_token", "")
                    anonymous_token = auth_section.get("anonymous_token", "")
            log(f"  после 1-го /auth: auth_token={'<есть>' if auth_token else '<пусто>'}, "
                f"anon={'<есть>' if anonymous_token else '<пусто>'}")

            # ── Step 1c: если auth_token пустой — повторный заход (как в
            # реальном мобильном трафике, где /auth вызывается дважды) ──
            if not auth_token:
                log("  auth_token пуст — повторный заход на тот же /auth (как в реальном трафике)")
                page.goto(page.url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)
                live_init = page.evaluate("() => { try { return window.init || null; } catch (e) { return null; } }")
                if isinstance(live_init, dict):
                    auth_section = live_init.get("auth", {})
                    if isinstance(auth_section, dict):
                        auth_token = auth_section.get("access_token", "") or auth_section.get("auth_token", "")
                        anonymous_token = anonymous_token or auth_section.get("anonymous_token", "")
                log(f"  после 2-го /auth: auth_token={'<есть>' if auth_token else '<пусто>'}, "
                    f"anon={'<есть>' if anonymous_token else '<пусто>'}")

            if not anonymous_token:
                return {"ok": False, "error": "anonymous_token не найден ни на 1-м, ни на 2-м заходе /auth"}

            _human_pause(1.0, 2.0)

            # ── Step 2: connect_internal ──
            log("[2] fetch connect_internal")
            r = browser_fetch(page, "https://login.vk.ru/?act=connect_internal", data={
                "uuid": my_uuid, "device_id": device_id, "version": "1", "app_id": app_id,
            })
            log(f"  → {r['status']}: {r['json']}")
            _human_pause(0.5, 1.2)

            # ── Step 2b: getAuthCode ──
            log("[2b] fetch auth.getAuthCode")
            m_os = _re.search(r"\(([^)]+)\)", UA_BROWSER)
            device_name = m_os.group(1) if m_os else "Windows NT 10.0; Win64; x64"
            r = browser_fetch(page, f"https://api.vk.ru/method/auth.getAuthCode?v={VK_API_VERSION}&client_id={app_id}", data={
                "device_name": device_name, "auth_code_flow": "0", "verification_hash": "",
                "force_regenerate": "1", "anonymous_token": anonymous_token, "is_switcher_flow": "",
            })
            auth_hash = ((r["json"] or {}).get("response") or {}).get("auth_hash", "")
            log(f"  → {r['status']}: auth_hash={'<got>' if auth_hash else '<none>'}")
            _human_pause(0.3, 0.8)

            # ── Step 2c: checkAuthCode ──
            if auth_hash:
                log("[2c] fetch auth.checkAuthCode")
                r = browser_fetch(page, f"https://api.vk.ru/method/auth.checkAuthCode?v={VK_API_VERSION}&client_id={app_id}", data={
                    "auth_hash": auth_hash, "web_auth": "1", "anonymous_token": anonymous_token,
                })
                log(f"  → {r['status']}: {r['json']}")
                _human_pause(0.3, 0.8)

            # ── Step 2d: checkPhoneReuse ──
            log("[2d] fetch ecosystem.checkPhoneReuse")
            r = browser_fetch(page, f"https://api.vk.ru/method/ecosystem.checkPhoneReuse?v={VK_API_VERSION}&client_id={app_id}", data={
                "login": login, "auth_token": auth_token, "super_app_token": "",
                "is_edu_flow": "0", "anonymous_token": anonymous_token,
            })
            log(f"  → {r['status']}: {r['json']}")
            _human_pause(0.4, 1.0)

            # ── Step 3: validateAccount (с капча-циклом) ──
            log("[3] fetch auth.validateAccount")
            validate_data = {
                "login": login, "client_id": app_id, "device_id": device_id,
                "sid": "", "super_app_token": "", "service_group": "",
                "supported_ways": SUPPORTED_WAYS, "auth_token": auth_token,
                "is_edu_flow": "0", "anonymous_token": anonymous_token,
            }
            sid = ""
            captcha_success_token = ""
            flow_names = []
            for attempt in range(4):
                post_data = dict(validate_data)
                if captcha_success_token:
                    post_data["success_token"] = captcha_success_token
                r = browser_fetch(page, f"https://api.vk.ru/method/auth.validateAccount?v={VK_API_VERSION}&client_id={app_id}", data=post_data)
                d = r["json"] or {}
                if "error" in d:
                    err = d["error"]
                    code_ = err.get("error_code", 0)
                    msg = err.get("error_msg", str(err))
                    log(f"  → ERROR [{code_}] {msg}")
                    log(f"  --- полный объект ошибки (для диагностики капчи) ---")
                    log(f"  {json.dumps(err, ensure_ascii=False)[:500]}")
                    if code_ == 14:
                        redirect_uri_captcha = err.get("redirect_uri", "")
                        if redirect_uri_captcha and rucaptcha_key:
                            captcha_log: list = []
                            log(f"  [captcha] redirect_uri найден, отправляю в RuCaptcha (может занять до 2 минут)...")
                            token = _solve_not_robot_captcha(None, redirect_uri_captcha, rucaptcha_key, captcha_log, proxy=proxy)
                            for line in captcha_log:
                                log(f"  {line}")
                            if token:
                                captcha_success_token = token
                                continue
                        elif not redirect_uri_captcha:
                            log("  [captcha] redirect_uri ОТСУТСТВУЕТ в ответе ошибки — решать нечего")
                        return {"ok": False, "error": f"Captcha required, не решена: {msg}"}
                    return {"ok": False, "error": f"validateAccount: [{code_}] {msg}"}
                resp = d.get("response", {})
                flow_names = resp.get("flow_names", [])
                sid = resp.get("sid", "")
                log(f"  → OK flow_names={flow_names} sid={sid[:20]}...")
                break
            else:
                return {"ok": False, "error": "Captcha: превышено число попыток"}

            if "password" not in flow_names:
                return {"ok": False, "error": f"Неожиданный flow_names (нет 'password'): {flow_names}"}

            _human_pause(2.0, 4.0)

            # ── Step 4: connect_authorize (ДОГАДКА — тело не подтверждено для
            # этого конкретного oauth_version=2/response_type=code флоу,
            # взято из generic-флоу и дополнено известными oauth-полями) ──
            log("[4] fetch connect_authorize (пароль) — ТЕЛО НЕ ПОДТВЕРЖДЕНО трафиком для этого флоу")
            to_b64 = base64.urlsafe_b64encode(web_redirect_uri.encode()).decode().rstrip("=")
            authorize_data = {
                "username": login, "password": password, "auth_token": auth_token,
                "sid": sid, "uuid": my_uuid, "v": VK_API_VERSION, "device_id": device_id,
                "service_group": "", "agreement_hash": "", "oauth_version": "2",
                "max_messenger_hash": "", "oauth_force_hash": "0", "is_registration": "0",
                "oauth_response_type": "code", "vkid_oauth_hash": "",
                "is_oauth_migrated_flow": "0", "oauth_state": oauth_state, "to": to_b64,
                "version": "1", "app_id": app_id,
                "code_challenge": code_challenge, "code_challenge_method": "S256",
                "redirect_uri": web_redirect_uri,
            }
            captcha_retry_token = ""
            for ca_attempt in range(4):
                if captcha_retry_token:
                    # НЕ ПОДТВЕРЖДЕНО реальным трафиком для этого конкретного
                    # эндпоинта (connect_authorize) — поле "success_token"
                    # взято по аналогии с тем, как validateAccount принимает
                    # решённую vkcaptcha (там это подтверждено трафиком).
                    # Форма redirect_uri капчи здесь идентична той, так что
                    # это обоснованная догадка, но именно догадка.
                    authorize_data["success_token"] = captcha_retry_token
                else:
                    authorize_data.pop("success_token", None)
                r = browser_fetch(page, "https://login.vk.ru/?act=connect_authorize", data=authorize_data)
                d = r["json"] or {}
                safe_d = dict(d) if isinstance(d, dict) else d
                if isinstance(safe_d, dict) and isinstance(safe_d.get("data"), dict):
                    safe_data = dict(safe_d["data"])
                    if safe_data.get("access_token"):
                        safe_data["access_token"] = "<redacted>"
                    safe_d["data"] = safe_data
                log(f"  → {r['status']}: {json.dumps(safe_d, ensure_ascii=False)[:500]}")

                if d.get("type") == "captcha":
                    redirect_uri_ca = d.get("redirect_uri", "")
                    log(f"  connect_authorize запросил капчу (captcha_type={d.get('captcha_type')}) "
                        f"— решаю через RuCaptcha (vkcaptcha, ПО АНАЛОГИИ, не подтверждено для этого эндпоинта)")
                    if not redirect_uri_ca or not rucaptcha_key:
                        return {"ok": False, "error": "connect_authorize запросил капчу, но нет "
                                                       "redirect_uri или ключа RuCaptcha — решать нечем",
                                "verdict": "Капча на connect_authorize — механизм решения для ЭТОГО эндпоинта "
                                           "не подтверждён реальным трафиком."}
                    ca_captcha_log: list = []
                    token = _solve_not_robot_captcha(None, redirect_uri_ca, rucaptcha_key, ca_captcha_log, proxy=proxy)
                    for line in ca_captcha_log:
                        log(f"  {line}")
                    if token:
                        captcha_retry_token = token
                    else:
                        # RuCaptcha сама иногда не решает конкретную капчу
                        # (ERROR_CAPTCHA_UNSOLVABLE) — это у неё транзиентная
                        # ошибка, не наш баг. Не сдаёмся сразу: следующая
                        # итерация заново дёрнет connect_authorize (без
                        # success_token, т.к. решить не вышло) — VK, как
                        # правило, выдаёт новую капчу, которую стоит попробовать
                        # решить ещё раз, пока не кончатся попытки цикла.
                        captcha_retry_token = ""
                        log("  [captcha] RuCaptcha не решила эту капчу — пробую снова "
                            "(новая попытка connect_authorize)")
                    continue

                if d.get("type") == "error":
                    err_info = d.get("error_info", {})
                    return {"ok": False, "error": f"connect_authorize error: {err_info}",
                            "verdict": "VK-вход не прошёл под этим app_id/redirect_uri — либо несогласованность "
                                       "параметров (см. ошибку), либо это тело connect_authorize в принципе неверное "
                                       "для oauth_version=2 (не подтверждено трафиком)."}
                break
            else:
                return {"ok": False, "error": "connect_authorize: капча запрашивалась повторно несколько раз подряд"}

            log("  VK ID логин принят паролем.")

            # ОПРОВЕРГНУТО: data.oauth2_auth_code из connect_authorize —
            # это ДРУГОЙ код (сверено по реальным значениям из одной сессии:
            # он не совпадает с кодом в финальном редиректе на twinby.ru/app).
            # Похоже, это внутренний код VK (используется в их же
            # oauth2/auth?grant_type=authorization_code&client_id=51781872,
            # который мы уже видели и отбросили как "не наш"). Реальный код
            # для Twinby появляется только после полной цепочки, которую
            # делает сама страница (check_silent_token → ... → повторный
            # /authorize → редирект). Просто ждём дольше и ничего не трогаем
            # руками — даём странице отработать самой, как при реальном входе.
            log("  ждём, пока страница сама дойдёт до редиректа на "
                f"{web_redirect_uri}?code=... (до 50 сек)...")
            for _ in range(50):
                if captured_redirect.get("url") or page.url.startswith(web_redirect_uri):
                    break
                page.wait_for_timeout(1000)

            redirect_url = captured_redirect.get("url", "")
            if not redirect_url and page.url.startswith(web_redirect_uri):
                redirect_url = page.url
            if not redirect_url:
                body_text = ""
                try:
                    body_text = page.inner_text("body")[:500]
                except Exception:
                    pass
                return {"ok": False,
                        "error": f"connect_authorize принят, но редирект на {web_redirect_uri} не пойман за 25 сек. "
                                 f"page.url={page.url[:200]}",
                        "page_text": body_text,
                        "verdict": "VK-вход прошёл, но страница сама не довела до редиректа — похоже, "
                                   "headless-сессия ведёт себя иначе, чем реальный браузер (возможно, "
                                   "антибот-детект), а не вопрос тайминга."}

            log(f"  Поймали редирект: {redirect_url[:200]}")
            twinby_code = _extract_code_from_url(redirect_url)
            if not twinby_code:
                return {"ok": False, "error": f"редирект пойман, но code не найден: {redirect_url[:200]}"}
            log(f"  Twinby authorization code получен: {twinby_code[:30]}...")

            # Безопасная локальная проверка связки OAuth-параметров: значения
            # секретов не выводятся. HTTPX-обмен не попадает в Playwright HAR.
            expected_challenge = base64.urlsafe_b64encode(
                hashlib.sha256(code_verifier.encode("ascii")).digest()
            ).rstrip(b"=").decode("ascii")
            authorize_query = urllib.parse.parse_qs(
                urllib.parse.urlparse(authorize_url).query
            )
            callback_query = urllib.parse.parse_qs(
                urllib.parse.urlparse(redirect_url).query
            )
            callback_state = callback_query.get("state", [""])[0]
            callback_device_id = callback_query.get("device_id", [""])[0]
            log(f"  [oauth-check] callback device_id present={bool(callback_device_id)} length={len(callback_device_id)}")
            log("  [oauth-check] PKCE verifier→challenge совпадает: "
                f"{expected_challenge == code_challenge and authorize_query.get('code_challenge') == [code_challenge]}")
            log("  [oauth-check] authorize содержит ожидаемые state/redirectUri: "
                f"{authorize_query.get('state') == [oauth_state] and authorize_query.get('redirect_uri') == [web_redirect_uri]}")
            log("  [oauth-check] state в callback совпадает с исходным: "
                f"{bool(callback_state) and callback_state == oauth_state}")
            log("  [oauth-check] code_challenge_method в authorize: "
                f"{authorize_query.get('code_challenge_method', ['<missing>'])[0]}")
            log("  [oauth-check] Twinby deviceId comes from callback device_id: "
                f"{bool(callback_device_id)} (length={len(callback_device_id)})")
            for i, observed in enumerate(actual_oauth_requests):
                log(
                    f"  [request-event/pre-route {i}] path={observed['path']} "
                    f"method={observed['method'] or '<missing>'} "
                    f"challenge_match={observed['challenge'] == expected_challenge} "
                    f"state_match={observed['state'] == oauth_state} "
                    f"redirect_match={observed['redirect_uri'] == web_redirect_uri} "
                    f"device_id_present={bool(observed['device_id'])}"
                )

            # ── Step 5: обмен code → Twinby JWT через уже существующую,
            # подтверждённую структуру запроса (из HAR) ──
            log("[5] twinby_auth_v3: обмен code → Twinby JWT (структура запроса подтверждена HAR)")
            if callback_state != oauth_state or not callback_device_id:
                return {"ok": False, "error": "VK callback state mismatch or missing device_id; Twinby exchange skipped."}

            tw_result = twinby_auth_v3(twinby_code, code_verifier, callback_device_id, callback_state,
                                        redirect_uri=web_redirect_uri, proxy=proxy)
            if not tw_result.get("ok"):
                return {"ok": False, "error": f"twinby_auth_v3 failed: {tw_result.get('error')}",
                        "verdict": "VK-вход + код получены, но обмен на JWT не прошёл"}

            log(f"  Twinby JWT получен: {tw_result['accessToken'][:30]}...")
            return {"ok": True, "jwt": tw_result["accessToken"],
                    "refresh_token": tw_result.get("refreshToken", ""),
                    "verdict": "ПОДТВЕРЖДЕНО — redirect_uri=https://twinby.ru/app реально работает для "
                               "client_id=7984771, весь путь пройден через браузер"}

        finally:
            browser.close()


def _click_continue_when_enabled(page, texts: list, max_wait_s: int = 15) -> tuple[bool, str]:
    """
    Кнопка "Продолжить"/"Continue" у VK часто disabled до тех пор, пока
    React не обновит состояние формы (например, после посимвольного ввода
    в замаскированное поле). Вместо однократного click с коротким timeout —
    ждём до max_wait_s, пока кнопка реально станет enabled, и только тогда
    кликаем. Возвращает (clicked, какой текст сработал/последняя ошибка).
    """
    last_err = ""
    for _ in range(max_wait_s * 2):
        for text in texts:
            loc = page.locator(f"button:has-text('{text}')").first
            try:
                if loc.count() > 0 and loc.is_enabled():
                    loc.click(timeout=3000)
                    return True, text
            except Exception as e:
                last_err = str(e)
        page.wait_for_timeout(500)
    return False, last_err


def _find_and_solve_captcha_on_page(page, rucaptcha_key: str, proxy: str) -> dict:
    """
    Ищет на странице виджет капчи VK ("не робот", слайдер-пазл) и решает
    его через RuCaptcha (метод vkcaptcha), используя ТОТ ЖЕ session_token,
    что и у видимого виджета — решение проверяется на СТОРОНЕ VK (сервер),
    поэтому страница сама (через свой собственный опрос captchaNotRobot.check)
    должна обнаружить решение и продолжить САМА, без нашего клика по слайдеру.
    НЕ ПОДТВЕРЖДЕНО трафиком как универсальный механизм для ЭТОГО конкретного
    виджета (подтверждено только для капчи на auth.validateAccount) — это
    обоснованная попытка по аналогии (тот же redirect_uri-паттерн
    not_robot_captcha?domain=vk.com&session_token=...).
    """
    try:
        html = page.content()
    except Exception as e:
        return {"found": False, "error": f"не удалось снять content(): {e}"}

    m = _re.search(r"https://id\.vk\.ru/not_robot_captcha\?[^\"'\\\s]+", html)
    if not m:
        return {"found": False}
    redirect_uri_ca = m.group(0).replace("\\u0026", "&").replace("&amp;", "&")
    log(f"  [captcha] найден виджет на странице, redirect_uri: {redirect_uri_ca[:90]}...")
    cap_log: list = []
    token = _solve_not_robot_captcha(None, redirect_uri_ca, rucaptcha_key, cap_log, proxy=proxy)
    for line in cap_log:
        log(f"  {line}")
    return {"found": True, "solved": bool(token), "token": token}


def run_test_twinby_web_ui(login: str, password: str, proxy_raw: str, rucaptcha_key: str,
                            headful: bool) -> dict:
    """
    ПОДТВЕРЖДЕНО этим же прогоном (разными способами), что ПРЯМЫЕ вызовы
    VK API в обход настоящей формы (connect_internal/validateAccount/
    connect_authorize через fetch) НЕ доводят до финального редиректа:
    сервер логин принимает (ответы okay), но страница — это React-SPA,
    и переход к экрану согласия/редиректу запускается из колбэка
    ОБРАБОТЧИКА КЛИКА по кнопке, а не из пассивного наблюдения за сетью.
    Раз мы не кликаем настоящую кнопку — React не узнаёт об успехе.

    Эта функция ведёт РЕАЛЬНУЮ форму: вводит телефон и пароль в настоящие
    поля страницы, кликает настоящие кнопки — как живой пользователь.
    Капча (слайдер) решается в фоне через RuCaptcha (vkcaptcha) по тому же
    session_token, который виден в HTML виджета — страница должна сама
    обнаружить решение через свой collection captchaNotRobot.check и
    продолжить без нашего участия в самом слайдере.

    НЕ ПОДТВЕРЖДЕНО:
    - точный селектор поля телефона на ПЕРВОМ экране (до пароля) — другой
      HTML, чем на уже изученном экране SILENT_AUTH после пароля;
    - сработает ли решение капчи "со стороны" (через RuCaptcha), когда
      капча показана как виджет в реальной форме, а не как JSON-ошибка.
    """
    app_id = TWINBY_VK_APP_ID
    web_redirect_uri = "https://twinby.ru/app"
    proxy = _normalize_proxy(proxy_raw)
    code_verifier, code_challenge = _pkce()
    oauth_state = _state()

    log(f"[proxy] {proxy.split('@')[-1] if '@' in proxy else proxy}")
    log("[mode] twinby-web-ui — ведём настоящую форму на странице (не fetch напрямую)")

    pw_proxy = playwright_proxy_dict(proxy)

    with sync_playwright() as p:
        launch_kwargs = {"headless": not headful}
        if pw_proxy:
            launch_kwargs["proxy"] = pw_proxy
        browser = p.chromium.launch(**launch_kwargs)
        page = browser.new_page(user_agent=UA_BROWSER)

        captured_redirect = {}

        def _on_request(req):
            if req.url.startswith(web_redirect_uri):
                captured_redirect.setdefault("url", req.url)

        page.on("request", _on_request)

        try:
            authorize_url = (
                "https://id.vk.ru/authorize"
                f"?client_id={app_id}&response_type=code"
                f"&redirect_uri={urllib.parse.quote(web_redirect_uri, safe='')}"
                f"&code_challenge_method=S256&code_challenge={code_challenge}"
                f"&state={oauth_state}"
            )
            log("[1] page.goto id.vk.ru/authorize")
            page.goto(authorize_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1500)
            log(f"  URL: {page.url[:200]}")

            # ── Шаг 2: ввести телефон в реальное поле и нажать "Продолжить" ──
            log("[2] вводим телефон в форму")
            phone_input = page.locator(
                "input[type='tel']:visible, input[inputmode='tel']:visible"
            ).first
            try:
                phone_input.click(timeout=5000)
                phone_input.press("Control+A")
                phone_input.press("Backspace")
                phone_input.fill("")
                # fill() ставит значение программно — маска телефона (React,
                # слушает keydown/input посимвольно) могла не заметить его,
                # и кнопка осталась disabled. Печатаем по символу (slowly=True),
                # как живой ввод, чтобы маска отработала и состояние React
                # обновилось нормально.
                phone_input.press_sequentially(f"+{login.lstrip('+')}", delay=80)
                log("  поле телефона очищено и номер введён посимвольно")
            except Exception as e:
                dump = page.inner_text("body")[:600] if True else ""
                return {"ok": False, "error": f"не нашли/не заполнили поле телефона: {e}",
                        "page_text": dump}

            clicked, info = _click_continue_when_enabled(page, ["Продолжить", "Continue"], max_wait_s=15)
            if clicked:
                log(f"  клик по '{info}'")
            else:
                return {"ok": False, "error": f"кнопка 'Продолжить' после ввода телефона не стала активной за 15с: {info}",
                        "page_text": page.inner_text("body")[:600]}

            page.wait_for_timeout(2500)

            # ── Шаг 3: возможная капча ПЕРЕД паролем — решаем в фоне ──
            cap = _find_and_solve_captcha_on_page(page, rucaptcha_key, proxy)
            if cap.get("found"):
                log(f"  капча перед паролем: solved={cap.get('solved')}, ждём, пока страница сама её примет...")
                page.wait_for_timeout(4000)

            # ── Шаг 4: пароль в реальное поле, клик "Продолжить" ──
            log("[3] ищем поле пароля")
            pwd_input = page.locator("input[type='password']:visible").first
            found_pwd = False
            for _ in range(10):
                if pwd_input.count() > 0:
                    found_pwd = True
                    break
                page.wait_for_timeout(1000)
            if not found_pwd:
                return {"ok": False, "error": "поле пароля не появилось за 10 сек",
                        "page_text": page.inner_text("body")[:600]}

            pwd_input.click(timeout=5000)
            pwd_input.fill(password, timeout=5000)
            log("  вписали пароль")
            page.wait_for_timeout(500)

            clicked, info = _click_continue_when_enabled(page, ["Продолжить", "Continue"], max_wait_s=15)
            if clicked:
                log(f"  клик по '{info}' (пароль)")
            else:
                return {"ok": False, "error": f"кнопка 'Продолжить' после пароля не стала активной за 15с: {info}",
                        "page_text": page.inner_text("body")[:600]}

            page.wait_for_timeout(2500)

            # ── Шаг 5: возможная капча ПОСЛЕ пароля (видели в трафике:
            # captcha_type=regular на connect_authorize) — решаем так же ──
            cap2 = _find_and_solve_captcha_on_page(page, rucaptcha_key, proxy)
            if cap2.get("found"):
                log(f"  капча после пароля: solved={cap2.get('solved')}, ждём, пока страница сама её примет...")
                page.wait_for_timeout(4000)

            # ── Шаг 6: экран согласия. VK ID разметка может быть role=button/div,
            # поэтому сначала ищем доступную кнопку, затем видимый точный текст.
            consent_clicked = False
            for text in ["Разрешить", "Allow"]:
                candidates = [
                    page.get_by_role("button", name=text, exact=True).first,
                    page.get_by_text(text, exact=True).first,
                ]
                for loc in candidates:
                    try:
                        if loc.count() and loc.is_visible():
                            loc.click(timeout=5000)
                            log(f"  клик по '{text}' (согласие)")
                            consent_clicked = True
                            break
                    except Exception:
                        continue
                if consent_clicked:
                    break

            # ── Шаг 7: ждём финальный редирект на twinby.ru/app?code=... ──
            log(f"  ждём редирект на {web_redirect_uri}?code=... (до 50 сек)...")
            for _ in range(50):
                if captured_redirect.get("url") or page.url.startswith(web_redirect_uri):
                    break
                page.wait_for_timeout(1000)

            redirect_url = captured_redirect.get("url", "")
            if not redirect_url and page.url.startswith(web_redirect_uri):
                redirect_url = page.url
            if not redirect_url:
                return {"ok": False,
                        "error": f"редирект на {web_redirect_uri} не пойман за 50 сек. page.url={page.url[:200]}",
                        "page_text": page.inner_text("body")[:600],
                        "verdict": "Даже через реальную форму редирект не случился — нужен новый раунд диагностики."}

            log(f"  Поймали редирект: {redirect_url[:200]}")
            twinby_code = _extract_code_from_url(redirect_url)
            if not twinby_code:
                return {"ok": False, "error": f"редирект пойман, но code не найден: {redirect_url[:200]}"}
            callback_query = urllib.parse.parse_qs(urllib.parse.urlparse(redirect_url).query)
            callback_state = callback_query.get("state", [""])[0]
            callback_device_id = callback_query.get("device_id", [""])[0]
            log(f"  [oauth-check] callback device_id present={bool(callback_device_id)} length={len(callback_device_id)}")
            log(f"  Twinby authorization code получен: {twinby_code[:30]}...")

            log("[8] twinby_auth_v3: обмен code → Twinby JWT")
            # Один deviceId на весь OAuth-транзакционный поток.
            if callback_state != oauth_state or not callback_device_id:
                return {"ok": False, "error": "VK callback state mismatch or missing device_id; Twinby exchange skipped."}

            tw_result = twinby_auth_v3(twinby_code, code_verifier, callback_device_id, callback_state,
                                        redirect_uri=web_redirect_uri, proxy=proxy)
            if not tw_result.get("ok"):
                return {"ok": False, "error": f"twinby_auth_v3 failed: {tw_result.get('error')}",
                        "verdict": "VK-вход + код получены через реальную форму, но обмен на JWT не прошёл"}

            log(f"  Twinby JWT получен: {tw_result['accessToken'][:30]}...")
            return {"ok": True, "jwt": tw_result["accessToken"],
                    "refresh_token": tw_result.get("refreshToken", ""),
                    "verdict": "ПОДТВЕРЖДЕНО — полный путь через реальную форму работает для "
                               "client_id=7984771, redirect_uri=https://twinby.ru/app"}

        finally:
            browser.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--proxy", required=True, help="ip:port:user:pass или http://user:pass@ip:port")
    ap.add_argument("--rucaptcha-key", default="")
    ap.add_argument("--headful", action="store_true", help="видимое окно (только локально, не на Render)")
    ap.add_argument("--app-id", default=VK_ID_APP_ID,
                     help=f"VK app_id для ВСЕГО flow логина. По умолчанию {VK_ID_APP_ID} "
                          f"(VK ID landing). Передай {TWINBY_VK_APP_ID} (Twinby), чтобы сразу "
                          f"получить access_token, выданный для приложения Twinby, а не для "
                          f"чужого контекста.")
    ap.add_argument("--mode", choices=["generic", "twinby-web", "twinby-web-ui"], default="generic",
                     help="generic: обычный VK ID логин (--app-id применяется ко всему флоу). "
                          "twinby-web: прямые fetch-вызовы VK API в обход формы (ПОДТВЕРЖДЕНО: "
                          "сервер логинит, но React-страница не реагирует — редирект не случается). "
                          "twinby-web-ui: ведём РЕАЛЬНУЮ форму на странице (ввод телефона/пароля, "
                          "клики по кнопкам) — это рабочая гипотеза взамен twinby-web.")
    args = ap.parse_args()

    log("=== МИНИ-ТЕСТ: VK ID login через headless Chromium ===")
    if args.mode == "twinby-web-ui":
        result = run_test_twinby_web_ui(args.login, args.password, args.proxy, args.rucaptcha_key,
                                         args.headful)
    elif args.mode == "twinby-web":
        log("[mode] twinby-web (проверка redirect_uri=https://twinby.ru/app для app_id=7984771)")
        result = run_test_twinby_web(args.login, args.password, args.proxy, args.rucaptcha_key,
                                      args.headful)
    else:
        log(f"[app_id] {args.app_id}")
        result = run_test(args.login, args.password, args.proxy, args.rucaptcha_key, args.headful,
                           app_id=args.app_id)
    log("=== РЕЗУЛЬТАТ ===")
    log(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
