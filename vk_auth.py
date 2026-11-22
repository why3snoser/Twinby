"""
VK ID browser-style login → Twinby JWT.

Flow (из реального mitmproxy трафика, app_id=7497650):
  1. GET id.vk.ru/auth → auth_token + anonymous_token из window.init
  2. POST login.vk.ru/?act=connect_internal
  3. POST api.vk.ru/method/auth.validateAccount (может вернуть капчу)
  4. POST login.vk.ru/?act=connect_authorize → access_token + silent_token
  5. PKCE flow с Twinby app_id 7984771 → Twinby JWT
"""
from __future__ import annotations

import hashlib
import base64
import secrets
import json
import os
import random
import re
import time
import urllib.parse
import uuid as _uuid_mod
from pathlib import Path
from typing import Optional

import httpx
from curl_cffi import requests as cffi_requests

# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

# Twinby VK App
TWINBY_VK_APP_ID = "7984771"
TWINBY_REDIRECT_URI = "vk7984771://vk.ru/blank.html"

# VK ID landing app (из реального трафика id.vk.ru)
VK_ID_APP_ID = "7497650"

# VK API version (из трафика)
VK_API_VERSION = "5.280"

UA_BROWSER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.8078.47 Safari/537.36"
)
UA_DART = "Dart/3.11 (dart:io)"

# curl_cffi: какой браузер TLS/HTTP2-фингерпринт имитировать для VK ID флоу.
# ВАЖНО: curl_cffi 0.17.0 максимум умеет "chrome152" — реального JA3 для
# Chrome/156 в библиотеке ещё нет. UA_BROWSER выше specifies 156 (взят из
# реального GoLogin-профиля, которым прошёл ручной тест), а TLS-рукопожатие
# будет от chrome152 — ближайшее доступное. Это не идеальное совпадение, но
# расхождение в несколько минорных версий Chrome гораздо менее заметно, чем
# прежний httpx-отпечаток, который не похож на Chrome вообще никак.
VK_IMPERSONATE = "chrome152"

# Способы подтверждения (из трафика: supported_ways поле)
SUPPORTED_WAYS = (
    "push,email,qr_code,codegen,sms,callreset,password,"
    "reserve_code,max_messenger,max_code,official_messenger,passkey,call_in"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pkce() -> tuple[str, str]:
    """Generate (code_verifier, code_challenge) for PKCE S256."""
    raw = secrets.token_bytes(32)
    verifier = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _device_id() -> str:
    """Random device_id в формате VK SDK (base64url, ~21 символ)."""
    return base64.urlsafe_b64encode(secrets.token_bytes(15)).rstrip(b"=").decode()


def _state() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


def _uuid() -> str:
    return str(_uuid_mod.uuid4())


def _human_pause(min_s: float, max_s: float) -> None:
    """Случайная пауза в заданном диапазоне — вместо фиксированного sleep,
    который для скрипта выглядит как метроном, а для человека так не бывает."""
    time.sleep(random.uniform(min_s, max_s))


def _normalize_proxy(raw: str) -> str:
    """ip:port:user:pass → http://user:pass@ip:port. Уже http:// — как есть."""
    raw = raw.strip()
    if not raw:
        return ""
    if raw.startswith(("http://", "https://", "socks")):
        return raw
    parts = raw.split(":")
    if len(parts) >= 4:
        ip, port, user = parts[0], parts[1], parts[2]
        pwd = ":".join(parts[3:])
        return f"http://{user}:{pwd}@{ip}:{port}"
    elif len(parts) == 2:
        return f"http://{parts[0]}:{parts[1]}"
    return raw


# ---------------------------------------------------------------------------
# Persistent VK identity (device_id, uuid, закреплённый proxy) — ПО АККАУНТУ
# ---------------------------------------------------------------------------
# Проблема: device_id/uuid генерились случайно на КАЖДУЮ попытку логина.
# Для VK это выглядит как "один и тот же пароль вводят с нового незнакомого
# устройства каждый раз" — сильный сигнал для password_bruteforce/антифрод,
# независимо от фингерпринта транспорта. Плюс proxy мог выбираться заново на
# каждый прогон (round-robin), так что один аккаунт логинился с разных IP в
# разных попытках — ещё один сигнал. Фиксируем оба раз и навсегда на логин.
#
# Файл переживает рестарт процесса (как accounts.json/settings.json рядом).

_VK_IDENTITY_FILE = Path(__file__).parent / "vk_identities.json"
_identity_cache: dict | None = None


def _load_identities() -> dict:
    global _identity_cache
    if _identity_cache is not None:
        return _identity_cache
    if _VK_IDENTITY_FILE.exists():
        try:
            _identity_cache = json.loads(_VK_IDENTITY_FILE.read_text())
        except Exception:
            _identity_cache = {}
    else:
        _identity_cache = {}
    return _identity_cache


def _save_identities(data: dict) -> None:
    global _identity_cache
    _identity_cache = data
    tmp = _VK_IDENTITY_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    os.replace(tmp, _VK_IDENTITY_FILE)


def get_pinned_identity(login: str, requested_proxy: str = "") -> dict:
    """
    Вернуть устойчивые device_id/uuid для этого VK-логина (создать при первом
    обращении, дальше всегда одни и те же — как постоянный профиль GoLogin).

    Прокси: если для логина уже закреплён прокси — он побеждает и
    requested_proxy игнорируется (даже если вызывающий код прислал другой,
    например из round-robin пула). Если ещё не закреплён — закрепляем тот,
    что прислали сейчас, на все будущие попытки с этим логином.

    Returns: {"device_id": str, "uuid": str, "proxy": str, "proxy_pinned_now": bool}
    """
    login = (login or "").strip()
    data = _load_identities()
    entry = data.get(login)

    if entry is None:
        entry = {
            "device_id": _device_id(),
            "uuid": _uuid(),
            "proxy": requested_proxy or "",
        }
        data[login] = entry
        _save_identities(data)
        return {**entry, "proxy_pinned_now": bool(requested_proxy)}

    changed = False
    if not entry.get("device_id"):
        entry["device_id"] = _device_id()
        changed = True
    if not entry.get("uuid"):
        entry["uuid"] = _uuid()
        changed = True
    pinned_now = False
    if not entry.get("proxy") and requested_proxy:
        entry["proxy"] = requested_proxy
        changed = True
        pinned_now = True
    if changed:
        data[login] = entry
        _save_identities(data)

    return {**entry, "proxy_pinned_now": pinned_now}


# ---------------------------------------------------------------------------
# RuCaptcha solver (image captcha)
# ---------------------------------------------------------------------------

def _solve_captcha_image(captcha_img_url: str, api_key: str,
                         proxy: str = "") -> Optional[str]:
    """Решить image captcha через RuCaptcha. Возвращает текст или None."""
    if not api_key:
        return None
    try:
        kw: dict = {"timeout": 15}
        if proxy:
            kw["proxy"] = proxy
        img_bytes = httpx.get(captcha_img_url, **kw).content

        r1 = httpx.post(
            "https://rucaptcha.com/in.php",
            data={"key": api_key, "method": "base64", "json": "1"},
            files={"file": ("captcha.jpg", img_bytes)},
            timeout=30,
        )
        d1 = r1.json()
        if d1.get("status") != 1:
            return None
        task_id = d1["request"]

        for _ in range(24):
            time.sleep(5)
            r2 = httpx.get(
                f"https://rucaptcha.com/res.php?key={api_key}&action=get&id={task_id}&json=1",
                timeout=10,
            )
            d2 = r2.json()
            if d2.get("status") == 1:
                return d2["request"]
            if d2.get("request") != "CAPCHA_NOT_READY":
                return None
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# VK captcha solver через RuCaptcha (метод vkcaptcha)
# ---------------------------------------------------------------------------

def _solve_vk_captcha_rucaptcha(
    redirect_uri: str,
    api_key: str,
    proxy: str,
    log: list,
) -> Optional[str]:
    """
    Решить VK captcha через RuCaptcha API (метод vkcaptcha).
    redirect_uri — из ответа validateAccount (error_code 14).

    RuCaptcha сам открывает страницу капчи, решает её и возвращает token.
    Возвращает success_token или None.
    """
    if not api_key:
        log.append("  [captcha] No RuCaptcha key")
        return None

    log.append(f"  [captcha] Sending to RuCaptcha (vkcaptcha method)...")
    log.append(f"  [captcha] redirect_uri: {redirect_uri[:100]}...")

    # Подготовить данные для RuCaptcha in.php
    in_data = {
        "key": api_key,
        "method": "vkcaptcha",
        "redirect_uri": redirect_uri,
        "userAgent": UA_BROWSER,
        "json": "1",
    }

    # Добавить прокси если есть
    if proxy:
        # RuCaptcha ожидает proxy в формате login:password@ip:port
        # и proxytype отдельно
        parsed_proxy = urllib.parse.urlparse(proxy)
        proxy_type = parsed_proxy.scheme.upper() if parsed_proxy.scheme else "HTTP"
        if proxy_type not in ("HTTP", "HTTPS", "SOCKS4", "SOCKS5"):
            proxy_type = "HTTP"

        proxy_host = parsed_proxy.hostname or ""
        proxy_port = parsed_proxy.port or ""
        proxy_login = parsed_proxy.username or ""
        proxy_pass = parsed_proxy.password or ""

        if proxy_host:
            proxy_str = f"{proxy_host}:{proxy_port}"
            if proxy_login:
                proxy_str = f"{proxy_login}:{proxy_pass}@{proxy_str}"
            in_data["proxy"] = proxy_str
            in_data["proxytype"] = proxy_type

    try:
        r1 = httpx.post(
            "https://rucaptcha.com/in.php",
            data=in_data,
            timeout=30,
        )
        d1 = r1.json()
        log.append(f"  [captcha] in.php response: {json.dumps(d1, ensure_ascii=False)[:200]}")

        if d1.get("status") != 1:
            err_text = d1.get("request", d1.get("error_text", "unknown"))
            log.append(f"  [captcha] in.php error: {err_text}")
            return None

        task_id = d1["request"]
        log.append(f"  [captcha] task_id: {task_id}")
    except Exception as e:
        log.append(f"  [captcha] in.php failed: {e}")
        return None

    # Ждать решения (до 120 секунд)
    for i in range(24):
        time.sleep(5)
        try:
            r2 = httpx.get(
                "https://rucaptcha.com/res.php",
                params={
                    "key": api_key,
                    "action": "get",
                    "id": task_id,
                    "json": "1",
                },
                timeout=10,
            )
            d2 = r2.json()

            status = d2.get("status")
            raw_req = d2.get("request", "")

            # RuCaptcha vkcaptcha возвращает request как dict {'token': 'eyJ...'}
            if isinstance(raw_req, dict):
                req = raw_req.get("token", "")
                if not req:
                    # fallback — первое строковое значение
                    for v in raw_req.values():
                        if isinstance(v, str) and len(v) > 10:
                            req = v
                            break
                    else:
                        req = str(raw_req)
            else:
                req = str(raw_req)

            if status == 1:
                log.append(f"  [captcha] Solved! token: {req[:60]}...")
                return req

            if req == "CAPCHA_NOT_READY":
                if i % 4 == 3:
                    log.append(f"  [captcha] Still solving... ({(i + 1) * 5}s)")
                continue

            # Ошибка
            log.append(f"  [captcha] res.php error: {req}")
            return None
        except Exception as e:
            log.append(f"  [captcha] res.php failed: {type(e).__name__}: {e}")

    log.append("  [captcha] Timeout waiting for solution")
    return None


def _solve_not_robot_captcha(
    s,  # curl_cffi.requests.Session (VK ID флоу) — параметр не используется внутри
    redirect_uri: str,
    api_key: str,
    log: list,
    proxy: str = "",
) -> Optional[str]:
    """
    Обработка VK not_robot_captcha.
    Основной метод: отправить redirect_uri в RuCaptcha (vkcaptcha).
    Возвращает success_token или None.
    """
    if not api_key:
        log.append("  [captcha] No RuCaptcha key, cannot solve captcha")
        return None

    # Основной путь: RuCaptcha vkcaptcha метод
    token = _solve_vk_captcha_rucaptcha(redirect_uri, api_key, proxy, log)
    if token:
        return token

    log.append("  [captcha] Could not solve VK captcha")
    return None


# ---------------------------------------------------------------------------
# Извлечение code из redirect URL
# ---------------------------------------------------------------------------

def _extract_code_from_url(url: str) -> Optional[str]:
    parsed = urllib.parse.urlparse(url)
    for part in (parsed.query, parsed.fragment):
        params = urllib.parse.parse_qs(part)
        code = params.get("code", [""])[0]
        if code:
            return code
    payload_raw = urllib.parse.parse_qs(parsed.query).get("payload", [""])[0]
    if payload_raw:
        try:
            payload = json.loads(payload_raw)
            if payload.get("code"):
                return payload["code"]
        except Exception:
            pass
    return None


# ---------------------------------------------------------------------------
# VK ID auth flow (из реального трафика)
# ---------------------------------------------------------------------------

def _vk_id_login(
    s,  # curl_cffi.requests.Session с impersonate=VK_IMPERSONATE
    login: str,
    password: str,
    log: list,
    rucaptcha_key: str = "",
    proxy: str = "",
    device_id: str = "",
    my_uuid: str = "",
) -> dict:
    """
    Полный VK ID login flow через id.vk.ru (app_id=7497650).

    device_id/my_uuid — закреплённые за логином (см. get_pinned_identity),
    чтобы VK видел один и тот же "девайс" при каждой попытке, а не новый
    случайный каждый раз.

    Returns:
        ok=True:  access_token, silent_token, user_id
        ok=False: error
    """
    my_uuid = my_uuid or _uuid()
    device_id = device_id or _device_id()

    # ═══════════════════════════════════════════════════════════════════════
    # Step 1: GET id.vk.ru/auth — получить auth_token и anonymous_token
    # ═══════════════════════════════════════════════════════════════════════
    log.append("[1] GET id.vk.ru/auth")

    auth_page_params = {
        "uuid": my_uuid,
        "response_type": "silent_token",
        "v": "1.3.0",
        "sdk_type": "vkid",
        "app_id": VK_ID_APP_ID,
        "redirect_uri": f"https://id.vk.ru/auth?uuid={my_uuid}&response_type=silent_token"
                        f"&v=1.3.0&sdk_type=vkid&app_id={VK_ID_APP_ID}",
    }

    r_auth = None
    for _try in range(3):
        try:
            r_auth = s.get(
                "https://id.vk.ru/auth",
                params=auth_page_params,
                allow_redirects=True,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                },
            )
            log.append(f"  → {r_auth.status_code}, len={len(r_auth.text)}")
            break
        except Exception as e:
            log.append(f"  GET attempt {_try + 1} failed: {e}")
            if _try == 2:
                return {"ok": False, "error": f"GET id.vk.ru/auth failed after 3 attempts: {e}"}
            time.sleep(1)

    # Извлечь auth_token и anonymous_token из HTML
    auth_token = ""
    anonymous_token = ""
    page_html = r_auth.text

    # --- Способ 1: window.init JSON (правильный парсинг вложенных {}) ---
    init_start = re.search(r'window\.init\s*=\s*\{', page_html)
    if init_start:
        # Найти закрывающую } с подсчётом вложенности
        start_pos = init_start.start() + init_start.group().index("{")
        depth = 0
        end_pos = start_pos
        in_string = False
        escape_next = False
        for i in range(start_pos, min(start_pos + 100000, len(page_html))):
            ch = page_html[i]
            if escape_next:
                escape_next = False
                continue
            if ch == "\\":
                escape_next = True
                continue
            if ch == '"' and not escape_next:
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
            json_str = page_html[start_pos:end_pos]
            try:
                init_data = json.loads(json_str)
                # auth_token может быть в разных местах:
                # init_data["auth"]["access_token"]
                # init_data["access_token"]
                # init_data["auth"]["auth_token"]
                auth_section = init_data.get("auth", {})
                if isinstance(auth_section, dict):
                    auth_token = auth_section.get("access_token", "") or auth_section.get("auth_token", "")
                    anonymous_token = auth_section.get("anonymous_token", "")
                if not auth_token:
                    auth_token = init_data.get("access_token", "") or init_data.get("auth_token", "")
                if not anonymous_token:
                    anonymous_token = init_data.get("anonymous_token", "")
                # Поиск вглубь — может быть вложенный объект
                if not auth_token:
                    for key, val in init_data.items():
                        if isinstance(val, dict):
                            at = val.get("access_token", "") or val.get("auth_token", "")
                            if at:
                                auth_token = at
                                break
                log.append(f"  window.init parsed OK ({len(json_str)} bytes)")
                log.append(f"  auth_token: {auth_token[:50]}..." if auth_token else "  auth_token: NOT FOUND in init")
                log.append(f"  anon_token: {anonymous_token[:50]}..." if anonymous_token else "  anon_token: NOT FOUND in init")
            except json.JSONDecodeError as e:
                log.append(f"  window.init JSON parse error: {e}")
                log.append(f"  JSON snippet: {json_str[:200]}...")
    else:
        log.append("  window.init NOT FOUND in page")

    # --- Способ 2: regex fallback по всему HTML ---
    if not auth_token:
        # auth_token как JWT (eyJ...)
        m = re.search(r'"access_token"\s*:\s*"(eyJ[^"]+)"', page_html)
        if m:
            auth_token = m.group(1)
            log.append(f"  auth_token (regex eyJ): {auth_token[:50]}...")
        else:
            # auth_token может быть не JWT — любой формат
            m2 = re.search(r'"access_token"\s*:\s*"([^"]{20,})"', page_html)
            if m2:
                auth_token = m2.group(1)
                log.append(f"  auth_token (regex generic): {auth_token[:50]}...")
            else:
                # Может быть "auth_token" вместо "access_token"
                m3 = re.search(r'"auth_token"\s*:\s*"([^"]{10,})"', page_html)
                if m3:
                    auth_token = m3.group(1)
                    log.append(f"  auth_token (key=auth_token): {auth_token[:50]}...")

    if not anonymous_token:
        m = re.search(r'"anonymous_token"\s*:\s*"(anonym[^"]+)"', page_html)
        if m:
            anonymous_token = m.group(1)
            log.append(f"  anon_token (regex): {anonymous_token[:50]}...")

    # auth_token пустой для незалогиненного пользователя — это нормально.
    # VK принимает пустой auth_token в validateAccount и connect_authorize.
    if not auth_token:
        log.append("  auth_token empty (normal for new login)")

    if not anonymous_token:
        return {"ok": False, "error": "Не удалось получить anonymous_token из id.vk.ru/auth"}

    _human_pause(1.2, 3.0)

    # ═══════════════════════════════════════════════════════════════════════
    # Step 2: POST login.vk.ru/?act=connect_internal
    # ═══════════════════════════════════════════════════════════════════════
    log.append("[2] POST login.vk.ru/?act=connect_internal")

    try:
        r_connect = s.post(
            "https://login.vk.ru/?act=connect_internal",
            data={
                "uuid": my_uuid,
                "device_id": device_id,
                "version": "1",
                "app_id": VK_ID_APP_ID,
            },
            headers={
                "Origin": "https://id.vk.ru",
                "Referer": "https://id.vk.ru/",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        d_connect = r_connect.json()
        log.append(f"  → {r_connect.status_code}: type={d_connect.get('type')}")
    except Exception as e:
        log.append(f"  connect_internal failed: {e}")
        # Не фатально, продолжаем

    _human_pause(0.8, 2.2)

    # ═══════════════════════════════════════════════════════════════════════
    # Step 2b: POST api.vk.ru/method/auth.getAuthCode
    # Подтверждено реальным трафиком (f50e5a4f-vk.flow, flow #301): реальный
    # браузер вызывает это между connect_internal и validateAccount, чтобы
    # получить auth_hash (используется кодогенерацией/QR-виджетом на той же
    # странице; отсутствие этого вызова — сигнал "не настоящий браузер").
    # device_name берём из того же UA, что уже используем (OS-часть).
    # ═══════════════════════════════════════════════════════════════════════
    log.append("[2b] POST auth.getAuthCode")

    _m_os = re.search(r"\(([^)]+)\)", UA_BROWSER)
    device_name = _m_os.group(1) if _m_os else "Windows NT 10.0; Win64; x64"

    auth_hash = ""
    try:
        r_getcode = s.post(
            f"https://api.vk.ru/method/auth.getAuthCode?v={VK_API_VERSION}&client_id={VK_ID_APP_ID}",
            data={
                "device_name": device_name,
                "auth_code_flow": "0",
                "verification_hash": "",
                "force_regenerate": "1",
                "anonymous_token": anonymous_token,
                "is_switcher_flow": "",
            },
            headers={
                "Origin": "https://id.vk.ru",
                "Referer": "https://id.vk.ru/",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        d_getcode = r_getcode.json()
        auth_hash = (d_getcode.get("response") or {}).get("auth_hash", "")
        log.append(f"  → {r_getcode.status_code}: auth_hash={'<got>' if auth_hash else '<none>'}")
    except Exception as e:
        log.append(f"  getAuthCode failed (не фатально): {e}")

    _human_pause(0.4, 1.0)

    # ═══════════════════════════════════════════════════════════════════════
    # Step 2c: POST api.vk.ru/method/auth.checkAuthCode
    # Подтверждено трафиком (flow #307), сразу после getAuthCode, с
    # auth_hash из его ответа. Не фатально если auth_hash не получен —
    # просто пропускаем, как и реальный клиент пропустил бы при ошибке.
    # ═══════════════════════════════════════════════════════════════════════
    if auth_hash:
        log.append("[2c] POST auth.checkAuthCode")
        try:
            r_checkcode = s.post(
                f"https://api.vk.ru/method/auth.checkAuthCode?v={VK_API_VERSION}&client_id={VK_ID_APP_ID}",
                data={
                    "auth_hash": auth_hash,
                    "web_auth": "1",
                    "anonymous_token": anonymous_token,
                },
                headers={
                    "Origin": "https://id.vk.ru",
                    "Referer": "https://id.vk.ru/",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            d_checkcode = r_checkcode.json()
            log.append(f"  → {r_checkcode.status_code}: {d_checkcode.get('response')}")
        except Exception as e:
            log.append(f"  checkAuthCode failed (не фатально): {e}")

        _human_pause(0.4, 1.0)

    # ═══════════════════════════════════════════════════════════════════════
    # Step 2d: POST api.vk.ru/method/ecosystem.checkPhoneReuse
    # Подтверждено трафиком (flow #310), прямо перед validateAccount.
    # Использует auth_token (из Step 1, может быть пустым для нового логина).
    # ═══════════════════════════════════════════════════════════════════════
    log.append("[2d] POST ecosystem.checkPhoneReuse")
    try:
        r_phonereuse = s.post(
            f"https://api.vk.ru/method/ecosystem.checkPhoneReuse?v={VK_API_VERSION}&client_id={VK_ID_APP_ID}",
            data={
                "login": login,
                "auth_token": auth_token,
                "super_app_token": "",
                "is_edu_flow": "0",
                "anonymous_token": anonymous_token,
            },
            headers={
                "Origin": "https://id.vk.ru",
                "Referer": "https://id.vk.ru/",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        d_phonereuse = r_phonereuse.json()
        log.append(f"  → {r_phonereuse.status_code}: {d_phonereuse.get('response')}")
    except Exception as e:
        log.append(f"  checkPhoneReuse failed (не фатально): {e}")

    _human_pause(0.5, 1.3)

    # ═══════════════════════════════════════════════════════════════════════
    # Step 3: POST api.vk.ru/method/auth.validateAccount
    # ═══════════════════════════════════════════════════════════════════════
    log.append("[3] POST auth.validateAccount")

    validate_data = {
        "login": login,
        "client_id": VK_ID_APP_ID,
        "device_id": device_id,
        "sid": "",
        "super_app_token": "",
        "service_group": "",
        "supported_ways": SUPPORTED_WAYS,
        "auth_token": auth_token,
        "is_edu_flow": "0",
        "anonymous_token": anonymous_token,
    }

    max_captcha_attempts = 3
    captcha_success_token = ""

    for attempt in range(max_captcha_attempts + 1):
        post_data = dict(validate_data)
        if captcha_success_token:
            # После решения капчи — добавить success_token
            post_data["success_token"] = captcha_success_token

        try:
            r_validate = s.post(
                f"https://api.vk.ru/method/auth.validateAccount?v={VK_API_VERSION}&client_id={VK_ID_APP_ID}",
                data=post_data,
                headers={
                    "Origin": "https://id.vk.ru",
                    "Referer": "https://id.vk.ru/",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            d_validate = r_validate.json()
        except Exception as e:
            return {"ok": False, "error": f"validateAccount failed: {e}"}

        # Проверить на ошибку
        if "error" in d_validate:
            err = d_validate["error"]
            error_code = err.get("error_code", 0) if isinstance(err, dict) else 0
            error_msg = err.get("error_msg", str(err)) if isinstance(err, dict) else str(err)

            # error_code 14 = captcha required
            if error_code == 14:
                redirect_uri = err.get("redirect_uri", "")
                captcha_sid = err.get("captcha_sid", "")
                captcha_img = err.get("captcha_img", "")
                log.append(f"  validateAccount: captcha required (attempt {attempt + 1})")

                if redirect_uri and rucaptcha_key:
                    # VK not_robot_captcha flow
                    success_token = _solve_not_robot_captcha(s, redirect_uri, rucaptcha_key, log, proxy=proxy)
                    if success_token:
                        captcha_success_token = success_token
                        continue
                elif captcha_img and rucaptcha_key:
                    # Классическая image captcha
                    solution = _solve_captcha_image(captcha_img, rucaptcha_key)
                    if solution:
                        post_data["captcha_sid"] = captcha_sid
                        post_data["captcha_key"] = solution
                        log.append(f"  Image captcha solved: {solution}")
                        continue

                return {"ok": False, "error": f"Captcha required, could not solve: {error_msg}"}

            log.append(f"  validateAccount error: [{error_code}] {error_msg}")
            return {"ok": False, "error": f"validateAccount: {error_msg}"}

        # Успешный ответ
        resp = d_validate.get("response", {})
        flow_names = resp.get("flow_names", [])
        sid = resp.get("sid", "")
        is_phone = resp.get("is_phone", False)
        log.append(f"  → flow_names={flow_names}, sid={sid[:30]}..., is_phone={is_phone}")
        break
    else:
        return {"ok": False, "error": "Captcha: exceeded max attempts"}

    # Пауза перед отправкой пароля — имитация времени на набор пароля человеком.
    _human_pause(2.0, 4.5)

    # ═══════════════════════════════════════════════════════════════════════
    # Step 4: POST login.vk.ru/?act=connect_authorize (отправка пароля)
    # ═══════════════════════════════════════════════════════════════════════
    log.append("[4] POST login.vk.ru/?act=connect_authorize")

    # Формат из реального трафика (mitmproxy)
    to_url = "https://id.vk.ru/account?flow_service=vkid_landing_/id"
    to_b64 = base64.urlsafe_b64encode(to_url.encode()).decode().rstrip("=")

    authorize_data = {
        "username": login,
        "password": password,
        "auth_token": auth_token,
        "sid": sid,
        "uuid": my_uuid,
        "v": VK_API_VERSION,
        "device_id": device_id,
        "service_group": "",
        "agreement_hash": "",
        "oauth_version": "",
        "max_messenger_hash": "",
        "oauth_force_hash": "0",
        "is_registration": "0",
        "oauth_response_type": "silent_token",
        "vkid_oauth_hash": "",
        "is_oauth_migrated_flow": "0",
        "oauth_state": "",
        "to": to_b64,
        "version": "1",
        "app_id": VK_ID_APP_ID,
    }

    r_authorize = None
    try:
        r_authorize = s.post(
            "https://login.vk.ru/?act=connect_authorize",
            data=authorize_data,
            headers={
                "Origin": "https://id.vk.ru",
                "Referer": "https://id.vk.ru/",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
    except Exception as e:
        log.append(f"  connect_authorize POST failed: {type(e).__name__}: {e}")
        return {"ok": False, "error": f"connect_authorize POST failed: {e}"}

    log.append(f"  connect_authorize → {r_authorize.status_code}, len={len(r_authorize.text)}")
    log.append(f"  connect_authorize body: {r_authorize.text[:500]}")

    try:
        d_authorize = r_authorize.json()
    except Exception as e:
        log.append(f"  connect_authorize JSON parse failed: {e}")
        return {"ok": False, "error": f"connect_authorize: not JSON"}

    if not isinstance(d_authorize, dict):
        log.append(f"  connect_authorize returned {type(d_authorize).__name__}: {str(d_authorize)[:300]}")
        return {"ok": False, "error": f"connect_authorize: response is {type(d_authorize).__name__}, not dict"}

    log.append(f"  connect_authorize parsed: {json.dumps(d_authorize, ensure_ascii=False)[:500]}")

    # Проверить тип ответа
    resp_type = d_authorize.get("type", "")
    if resp_type == "error":
        err_info = d_authorize.get("error_info", {})
        err_code = d_authorize.get("error_code", "")
        if isinstance(err_info, dict):
            err_name = err_info.get("error", err_code)
            err_desc = err_info.get("error_description", "")
            full_err = f"{err_name}: {err_desc}" if err_desc else str(err_name)
        else:
            # error_info — строка с описанием ошибки
            full_err = f"{err_code}: {err_info}" if err_code else str(err_info)
        log.append(f"  connect_authorize error: {full_err}")
        log.append(f"  Response: {json.dumps(d_authorize, ensure_ascii=False)[:300]}")

        # 2FA check
        if "2fa" in str(d_authorize).lower() or "validation" in str(err_code).lower():
            return {"ok": False, "error": f"2FA required: {full_err}"}

        # Banned
        if d_authorize.get("is_user_banned"):
            return {"ok": False, "error": "Account banned"}

        # Flood / bruteforce (проверять ДО wrong password, т.к. password_bruteforce содержит "password")
        if "flood" in str(d_authorize).lower() or "bruteforce" in str(d_authorize).lower():
            return {"ok": False, "error": f"VK flood control: {full_err}"}

        # Wrong password
        if "password" in str(err_code).lower() or "credentials" in str(err_code).lower():
            return {"ok": False, "error": f"Wrong password: {full_err}"}

        return {"ok": False, "error": f"VK login failed: {full_err}"}

    if resp_type != "okay":
        log.append(f"  Unexpected response type: {resp_type}")
        log.append(f"  Response: {json.dumps(d_authorize, ensure_ascii=False)[:500]}")
        return {"ok": False, "error": f"Unexpected VK response type: {resp_type}"}

    # Извлечь токены из ответа
    log.append(f"  connect_authorize OK response: {json.dumps(d_authorize, ensure_ascii=False)[:500]}")
    data = d_authorize.get("data", {})
    if not isinstance(data, dict):
        log.append(f"  data is {type(data).__name__}: {str(data)[:300]}")
        return {"ok": False, "error": f"connect_authorize: unexpected data type {type(data).__name__}"}
    access_token = data.get("access_token", "")
    auth_info = data.get("auth_info", {})
    if not isinstance(auth_info, dict):
        auth_info = {}
    silent_token = auth_info.get("token", "")
    user_info = auth_info.get("user", {})
    if not isinstance(user_info, dict):
        user_info = {}
    user_id = user_info.get("id", 0)
    token_uuid = auth_info.get("uuid", my_uuid)
    token_hash = auth_info.get("hash", "")

    if not access_token and not silent_token:
        log.append(f"  No tokens in response: {json.dumps(d_authorize, ensure_ascii=False)[:300]}")
        return {"ok": False, "error": "No access_token or silent_token in VK response"}

    log.append(f"  VK login OK! user_id={user_id}")
    log.append(f"  access_token: {access_token[:30]}..." if access_token else "  access_token: NONE")
    log.append(f"  silent_token: {silent_token[:30]}..." if silent_token else "  silent_token: NONE")

    return {
        "ok": True,
        "access_token": access_token,
        "silent_token": silent_token,
        "user_id": user_id,
        "uuid": token_uuid,
        "hash": token_hash,
        "device_id": device_id,
    }


# ---------------------------------------------------------------------------
# Получение authorization code для Twinby через VK ID
# ---------------------------------------------------------------------------

def get_vk_auth_code(
    login: str,
    password: str,
    rucaptcha_key: str = "",
    proxy: str = "",
) -> dict:
    """
    VK ID login → authorization code для Twinby.

    Returns:
        ok=True:  code, code_verifier, device_id, state, redirect_uri, log
        ok=False: error, log
    """
    code_verifier, code_challenge = _pkce()
    state = _state()
    log: list[str] = []

    # Закреплённые за этим логином device_id/uuid/proxy (см. get_pinned_identity) —
    # чтобы VK видел один и тот же "девайс" и один и тот же IP при каждой
    # попытке логина этого аккаунта, а не новую случайную комбинацию каждый раз.
    identity = get_pinned_identity(login, requested_proxy=proxy)
    pinned_device_id = identity["device_id"]
    pinned_uuid = identity["uuid"]
    pinned_proxy = identity["proxy"] or proxy
    if pinned_proxy and pinned_proxy != proxy:
        log.append(f"[identity] proxy переопределён на закреплённый за аккаунтом: "
                    f"{pinned_proxy.split('@')[-1] if '@' in pinned_proxy else pinned_proxy}")
    elif identity.get("proxy_pinned_now"):
        log.append("[identity] proxy закреплён за этим аккаунтом на будущее")
    log.append(f"[identity] device_id={pinned_device_id[:16]}... uuid={pinned_uuid[:8]}... (постоянные для этого логина)")
    proxy = pinned_proxy

    try:
        # ВАЖНО: httpx даёт TLS/HTTP2 ClientHello, который легко отличим от
        # настоящего браузера (JA3-фингерпринт). VK's антибот на connect_authorize
        # банит такие сессии password_bruteforce ещё до проверки пароля — даже
        # на первой попытке со свежего аккаунта. curl_cffi с impersonate=chrome
        # шлёт тот же TLS ClientHello + HTTP2-фреймы, что и настоящий Chrome,
        # так что сессия на сетевом уровне неотличима от входа руками.
        client_kw: dict = {
            "allow_redirects": False,
            "timeout": 30,
            "headers": {
                "User-Agent": UA_BROWSER,
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            },
            "verify": False,  # Прокси часто обрывают TLS (SSL EOF)
            "impersonate": VK_IMPERSONATE,
        }
        if proxy:
            client_kw["proxy"] = proxy

        with cffi_requests.Session(**client_kw) as s:
            # Phase 1: VK ID login
            vk_result = _vk_id_login(s, login, password, log, rucaptcha_key, proxy=proxy,
                                      device_id=pinned_device_id, my_uuid=pinned_uuid)
            if not vk_result.get("ok"):
                return {**vk_result, "log": log}

            vk_access_token = vk_result["access_token"]
            silent_token = vk_result.get("silent_token", "")
            vk_uuid = vk_result.get("uuid", "")
            vk_hash = vk_result.get("hash", "")
            vk_device_id = vk_result["device_id"]

            # Phase 2: Exchange silent_token → auth code для Twinby
            # Через /oauth2/auth endpoint VK ID

            log.append("[5] GET id.vk.ru/oauth2/auth (Twinby PKCE)")

            # Попытка A: Через VK ID /oauth2/auth с cookies сессии
            authorize_params = {
                "response_type": "code",
                "client_id": TWINBY_VK_APP_ID,
                "redirect_uri": TWINBY_REDIRECT_URI,
                "code_challenge": code_challenge,
                "code_challenge_method": "s256",
                "state": state,
                "scope": "",
            }

            try:
                r_auth = s.get(
                    "https://id.vk.ru/oauth2/auth",
                    params=authorize_params,
                    headers={
                        "Referer": "https://id.vk.ru/",
                    },
                )
                log.append(f"  → {r_auth.status_code}")

                if r_auth.status_code in (301, 302, 303, 307, 308):
                    loc = r_auth.headers.get("location", "")
                    log.append(f"  → redirect: {loc[:120]}")
                    code = _extract_code_from_url(loc)
                    if code:
                        log.append(f"  Code from redirect: {code[:30]}...")
                        return _success(code, code_verifier, vk_device_id, state, log)

                # Проверить HTML
                if r_auth.status_code == 200:
                    html = r_auth.text
                    for pat in [r'"code"\s*:\s*"([^"]+)"', r'code=([a-zA-Z0-9._-]+)']:
                        m = re.search(pat, html)
                        if m:
                            val = m.group(1)
                            if len(val) > 20:
                                log.append(f"  Code from HTML: {val[:30]}...")
                                return _success(val, code_verifier, vk_device_id, state, log)
            except Exception as e:
                log.append(f"  oauth2/auth failed: {e}")

            # Попытка B: POST oauth2/auth с silent_token
            if silent_token:
                log.append("[5b] POST id.vk.ru/oauth2/auth (silent_token exchange)")
                try:
                    r_exchange = s.post(
                        "https://id.vk.ru/oauth2/auth",
                        data={
                            "response_type": "code",
                            "client_id": TWINBY_VK_APP_ID,
                            "redirect_uri": TWINBY_REDIRECT_URI,
                            "code_challenge": code_challenge,
                            "code_challenge_method": "s256",
                            "state": state,
                            "scope": "",
                            "token": silent_token,
                            "uuid": vk_uuid,
                            "hash": vk_hash,
                            "v": "1.3.0",
                            "sdk_type": "vkid",
                            "app_id": VK_ID_APP_ID,
                        },
                        headers={
                            "Origin": "https://id.vk.ru",
                            "Referer": "https://id.vk.ru/",
                            "Content-Type": "application/x-www-form-urlencoded",
                        },
                    )
                    log.append(f"  → {r_exchange.status_code}")

                    if r_exchange.status_code in (301, 302, 303, 307, 308):
                        loc = r_exchange.headers.get("location", "")
                        log.append(f"  → redirect: {loc[:120]}")
                        code = _extract_code_from_url(loc)
                        if code:
                            log.append(f"  Code from exchange redirect: {code[:30]}...")
                            return _success(code, code_verifier, vk_device_id, state, log)

                    # JSON ответ
                    try:
                        d_ex = r_exchange.json()
                        log.append(f"  Exchange response: {json.dumps(d_ex, ensure_ascii=False)[:300]}")

                        # Может вернуть redirect_uri с code
                        redir = d_ex.get("redirect_uri", "")
                        if redir:
                            code = _extract_code_from_url(redir)
                            if code:
                                log.append(f"  Code from exchange JSON: {code[:30]}...")
                                return _success(code, code_verifier, vk_device_id, state, log)

                        # Или code прямо в response
                        code_val = d_ex.get("code", "")
                        if code_val:
                            log.append(f"  Code from exchange: {code_val[:30]}...")
                            return _success(code_val, code_verifier, vk_device_id, state, log)
                    except Exception:
                        # Может быть HTML
                        html_ex = r_exchange.text
                        m = re.search(r'code=([a-zA-Z0-9._-]+)', html_ex)
                        if m:
                            return _success(m.group(1), code_verifier, vk_device_id, state, log)
                except Exception as e:
                    log.append(f"  Exchange failed: {e}")

            # Попытка C: Через oauth.vk.ru/authorize (legacy)
            if vk_access_token:
                log.append("[5c] GET oauth.vk.ru/authorize (legacy, with access_token)")
                try:
                    legacy_params = {
                        "client_id": TWINBY_VK_APP_ID,
                        "redirect_uri": TWINBY_REDIRECT_URI,
                        "response_type": "code",
                        "scope": "",
                        "state": state,
                        "code_challenge": code_challenge,
                        "code_challenge_method": "s256",
                        "v": "5.199",
                        "access_token": vk_access_token,
                    }
                    r_legacy = s.get("https://oauth.vk.ru/authorize", params=legacy_params)
                    log.append(f"  → {r_legacy.status_code}")

                    if r_legacy.status_code in (301, 302, 303, 307, 308):
                        loc = r_legacy.headers.get("location", "")
                        log.append(f"  → redirect: {loc[:120]}")
                        code = _extract_code_from_url(loc)
                        if code:
                            log.append(f"  Code from legacy: {code[:30]}...")
                            return _success(code, code_verifier, vk_device_id, state, log)

                    if r_legacy.status_code == 200:
                        html_l = r_legacy.text
                        # grant_access
                        gm = re.search(r'location\.href\s*=\s*["\']([^"\']+)["\']', html_l)
                        if not gm:
                            gm = re.search(r'<form[^>]+action="([^"]+grant_access[^"]*)"', html_l)
                        if gm:
                            grant_url = gm.group(1).replace("&amp;", "&")
                            if not grant_url.startswith("http"):
                                grant_url = f"https://oauth.vk.ru{grant_url}"
                            log.append(f"  Grant URL: {grant_url[:100]}")
                            r_grant = s.get(grant_url)
                            if r_grant.status_code in (301, 302, 303, 307, 308):
                                loc2 = r_grant.headers.get("location", "")
                                code = _extract_code_from_url(loc2)
                                if code:
                                    return _success(code, code_verifier, vk_device_id, state, log)

                        for pat in [r'"code"\s*:\s*"([^"]+)"', r'code=([a-zA-Z0-9._-]+)']:
                            m = re.search(pat, html_l)
                            if m and len(m.group(1)) > 20:
                                return _success(m.group(1), code_verifier, vk_device_id, state, log)
                except Exception as e:
                    log.append(f"  Legacy auth failed: {e}")

            # Попытка D: POST id.vk.ru/oauth2/auth без silent_token (с cookies)
            log.append("[5d] POST id.vk.ru/oauth2/auth (cookies only)")
            try:
                r_cookies = s.post(
                    "https://id.vk.ru/oauth2/auth",
                    data={
                        "response_type": "code",
                        "client_id": TWINBY_VK_APP_ID,
                        "redirect_uri": TWINBY_REDIRECT_URI,
                        "code_challenge": code_challenge,
                        "code_challenge_method": "s256",
                        "state": state,
                        "scope": "",
                        "v": "1.3.0",
                        "sdk_type": "vkid",
                        "app_id": VK_ID_APP_ID,
                    },
                    headers={
                        "Origin": "https://id.vk.ru",
                        "Referer": "https://id.vk.ru/",
                    },
                )
                log.append(f"  → {r_cookies.status_code}")

                if r_cookies.status_code in (301, 302, 303, 307, 308):
                    loc = r_cookies.headers.get("location", "")
                    code = _extract_code_from_url(loc)
                    if code:
                        return _success(code, code_verifier, vk_device_id, state, log)

                try:
                    d_cook = r_cookies.json()
                    log.append(f"  Response: {json.dumps(d_cook, ensure_ascii=False)[:300]}")
                    redir = d_cook.get("redirect_uri", "")
                    if redir:
                        code = _extract_code_from_url(redir)
                        if code:
                            return _success(code, code_verifier, vk_device_id, state, log)
                    code_val = d_cook.get("code", "")
                    if code_val:
                        return _success(code_val, code_verifier, vk_device_id, state, log)
                except Exception:
                    pass
            except Exception as e:
                log.append(f"  Cookies-only auth failed: {e}")

            # Всё провалилось
            cookies_str = "; ".join(f"{c.name}={c.value[:15]}..." for c in s.cookies.jar)
            log.append(f"Cookies: {cookies_str}")

            return {
                "ok": False,
                "error": "Не удалось получить authorization code для Twinby",
                "log": log,
            }

    except Exception as e:
        log.append(f"Exception: {e}")
        return {"ok": False, "error": str(e), "log": log}


def _success(code: str, code_verifier: str, device_id: str, state: str, log: list) -> dict:
    return {
        "ok": True,
        "code": code,
        "code_verifier": code_verifier,
        "device_id": device_id,
        "state": state,
        "redirect_uri": TWINBY_REDIRECT_URI,
        "log": log,
    }


# ---------------------------------------------------------------------------
# Twinby auth v3: VK code → Twinby JWT
# ---------------------------------------------------------------------------

def twinby_auth_v3(
    code: str,
    code_verifier: str,
    device_id: str,
    state: str,
    redirect_uri: str = TWINBY_REDIRECT_URI,
    proxy: str = "",
) -> dict:
    """Отправить VK auth code на Twinby → JWT."""
    body = {
        "provider": "vk",
        "options": {
            "code": code,
            "codeVerifier": code_verifier,
            "deviceId": device_id,
            "state": state,
            "redirectUri": redirect_uri,
        },
    }

    try:
        post_kw: dict = {
            "json": body,
            "headers": {
                "User-Agent": UA_DART,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Host": "twinby.ru",
            },
            "timeout": 30,
        }
        if proxy:
            post_kw["proxy"] = proxy
        resp = httpx.post(
            "https://twinby.ru/api/auth/v3/oauth/by-token",
            **post_kw,
        )
        if resp.status_code >= 400:
            return {"ok": False, "error": f"Twinby v3: {resp.status_code} {resp.text[:300]}"}
        d = resp.json()
        at = d.get("accessToken", "")
        if not at:
            return {"ok": False, "error": f"No accessToken: {json.dumps(d)[:300]}"}
        return {"ok": True, "accessToken": at, "refreshToken": d.get("refreshToken", "")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Полный цикл: VK login → code → Twinby JWT
# ---------------------------------------------------------------------------

def full_auth(login: str, password: str, rucaptcha_key: str = "", proxy: str = "") -> dict:
    """VK ID login → auth code → Twinby JWT (старый путь через id.vk.ru)."""
    proxy = _normalize_proxy(proxy)
    # Резолвим тот же закреплённый за аккаунтом proxy, что get_vk_auth_code
    # использует внутри себя — чтобы шаг отправки кода в Twinby шёл с того же IP.
    proxy = get_pinned_identity(login, requested_proxy=proxy).get("proxy") or proxy

    vk_result = get_vk_auth_code(login, password, rucaptcha_key, proxy=proxy)
    if not vk_result.get("ok"):
        return vk_result

    tw_result = twinby_auth_v3(
        code=vk_result["code"],
        code_verifier=vk_result["code_verifier"],
        device_id=vk_result["device_id"],
        state=vk_result["state"],
        redirect_uri=vk_result["redirect_uri"],
        proxy=proxy,
    )

    log = vk_result.get("log", [])

    if not tw_result.get("ok"):
        log.append(f"Twinby auth FAILED: {tw_result.get('error', '?')}")
        return {"ok": False, "error": tw_result["error"], "log": log}

    log.append(f"Twinby JWT OK: {tw_result['accessToken'][:25]}...")
    return {
        "ok": True,
        "jwt": tw_result["accessToken"],
        "refresh_token": tw_result.get("refreshToken", ""),
        "log": log,
    }


# ---------------------------------------------------------------------------
# УДАЛЕНО: v2-путь (Direct Auth oauth.vk.com + Twinby /v2/oauth/by-token).
#
# Полностью убран по явному требованию: аккаунты, зарегистрированные через
# этот путь, получали JWT и 200 на онбординговых PATCH-запросах, но реальный
# профиль на сервере Twinby оставался недооформленным (имя оставалось
# дефолтным "Макс") — т.е. аккаунт выходил полностью бесполезным. Единственный
# рабочий путь — полный вход через VK ID (см. full_auth() / _vk_id_login()
# выше), строго с капчей через RuCaptcha vkcaptcha, как в реальном трафике.
#
# Раньше здесь были: _vk_direct_auth(), twinby_auth_v2(), full_auth_v2(),
# а также константы VK_ANDROID_APP_ID/VK_ANDROID_APP_SECRET/UA_VK_ANDROID
# (креды официального VK Android-приложения для Direct Auth). Удалены
# целиком, не оставлены как мёртвый код — чтобы их нельзя было случайно
# снова включить как fallback.
# ---------------------------------------------------------------------------

