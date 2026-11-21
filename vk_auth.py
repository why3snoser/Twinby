"""
VK ID OAuth 2.0 + PKCE: получение authorization code для приложения Twinby.

Стратегия (v2 — browser-style VK ID login):
  1. GET vk.ru → начальные cookies
  2. auth.validateAccount → sid
  3. POST login.vk.ru/?act=connect_authorize → username/password → remixsid
  4. GET oauth.vk.ru/authorize → implicit token (connect flow)
  5. Используем сессию для id.vk.com/authorize → авторизуем приложение Twinby → code

Капча: если VK требует капчу, решаем через RuCaptcha.
"""
from __future__ import annotations

import hashlib
import base64
import secrets
import json
import re
import time
import urllib.parse
import uuid as _uuid_mod
from typing import Optional

import httpx

# Twinby VK App — из реального трафика мобильного приложения
TWINBY_VK_APP_ID = "7984771"
TWINBY_REDIRECT_URI = "vk7984771://vk.ru/blank.html"

# VK app — default app_id из python273/vk_api для browser login
VK_ADMIN_APP_ID = "6222115"

UA_BROWSER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
UA_DART = "Dart/3.11 (dart:io)"

# Способы подтверждения, которые мы "поддерживаем"
SUPPORTED_WAYS = "push_otp,email,callreset,sms,password,passkey,2fa_app,2fa_sms,2fa_callreset,2fa_email"


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------

def _pkce() -> tuple[str, str]:
    """Generate (code_verifier, code_challenge) for PKCE S256."""
    raw = secrets.token_bytes(32)
    verifier = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _device_id() -> str:
    """Random device ID, same length/format as real VK SDK."""
    return base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()


def _state() -> str:
    """Random state parameter (>=32 chars, a-zA-Z0-9_-)."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


def _uuid() -> str:
    return str(_uuid_mod.uuid4())


# ---------------------------------------------------------------------------
# RuCaptcha solver
# ---------------------------------------------------------------------------

def solve_captcha_rucaptcha(captcha_img_url: str, api_key: str) -> Optional[str]:
    """Решить капчу через RuCaptcha API. Возвращает текст капчи или None."""
    if not api_key:
        return None
    try:
        r1 = httpx.post(
            "https://rucaptcha.com/in.php",
            data={
                "key": api_key,
                "method": "base64",
                "json": "1",
            },
            files={"file": ("captcha.jpg", httpx.get(captcha_img_url, timeout=10).content)},
            timeout=30,
        )
        d1 = r1.json()
        if d1.get("status") != 1:
            return None
        task_id = d1["request"]

        for _ in range(12):
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
# Извлечение кода из redirect URL
# ---------------------------------------------------------------------------

def _extract_code_from_url(url: str) -> Optional[str]:
    """Извлечь authorization code из redirect URL."""
    parsed = urllib.parse.urlparse(url)
    for part in (parsed.query, parsed.fragment):
        params = urllib.parse.parse_qs(part)
        code = params.get("code", [""])[0]
        if code:
            return code
    # VK ID кладёт payload в query
    payload_raw = urllib.parse.parse_qs(parsed.query).get("payload", [""])[0]
    if payload_raw:
        try:
            payload = json.loads(payload_raw)
            if payload.get("code"):
                return payload["code"]
        except Exception:
            pass
    return None


def _extract_device_id_from_url(url: str) -> Optional[str]:
    """Извлечь device_id из redirect URL (VK ID кладёт его в payload)."""
    parsed = urllib.parse.urlparse(url)
    payload_raw = urllib.parse.parse_qs(parsed.query).get("payload", [""])[0]
    if payload_raw:
        try:
            payload = json.loads(payload_raw)
            return payload.get("device_id")
        except Exception:
            pass
    return None


def _follow_redirects_for_code(client: httpx.Client, resp: httpx.Response,
                                max_hops: int = 10, log: list = None) -> Optional[str]:
    """Следовать редиректам, пока не найдём code."""
    for _ in range(max_hops):
        if resp.status_code not in (301, 302, 303, 307, 308):
            return None
        location = resp.headers.get("location", "")
        if log is not None:
            log.append(f"  → {resp.status_code} → {location[:120]}")

        if location.startswith(TWINBY_REDIRECT_URI) or location.startswith("vk7984771://"):
            return _extract_code_from_url(location)

        if location.startswith("/"):
            base = f"{resp.url.scheme}://{resp.url.host}"
            location = base + location

        if not location.startswith("http"):
            return _extract_code_from_url(location)

        try:
            resp = client.get(location)
        except Exception:
            return None
    return None


# ---------------------------------------------------------------------------
# VK ID browser-style login (port of python273/vk_api flow)
# ---------------------------------------------------------------------------

def _vk_browser_login(
    s: httpx.Client,
    login: str,
    password: str,
    log: list,
    rucaptcha_key: str = "",
) -> dict:
    """
    Эмуляция браузерного логина VK через VK ID.
    Результат: remixsid cookie в сессии s.

    Returns:
        {"ok": True} или {"ok": False, "error": "..."}
    """
    # --- Шаг 1: GET vk.ru для начальных cookies ---
    log.append("[1] GET vk.ru")
    try:
        r_init = s.get("https://vk.ru/", follow_redirects=True)
        log.append(f"  → {r_init.status_code}, url={str(r_init.url)[:80]}")
    except Exception as e:
        log.append(f"  GET vk.ru failed: {e}")
        return {"ok": False, "error": f"GET vk.ru failed: {e}"}

    # Извлечь auth_token (может быть в cookies или в HTML)
    auth_token = ""
    for c in s.cookies.jar:
        if c.name == "auth_token":
            auth_token = c.value
            break

    # sid из cookies
    sid = ""
    for c in s.cookies.jar:
        if c.name == "auth_sid":
            sid = c.value
            break

    device_id = _device_id()

    # --- Шаг 2: auth.validateAccount ---
    log.append("[2] auth.validateAccount")
    validate_params = {
        "v": "5.207",
        "client_id": VK_ADMIN_APP_ID,
        "login": login,
        "sid": sid,
        "device_id": device_id,
        "auth_token": auth_token,
        "super_app_token": "",
        "supported_ways": SUPPORTED_WAYS,
        "is_switcher_flow": "0",
        "is_edu_flow": "",
        "is_registration": "",
        "access_token": "",
        "with_cookies": "1",
    }

    try:
        r_validate = s.post(
            "https://api.vk.ru/method/auth.validateAccount",
            data=validate_params,
            headers={
                "Origin": "https://id.vk.ru",
                "Referer": "https://id.vk.ru/",
            },
        )
        d_validate = r_validate.json()
        log.append(f"  → {r_validate.status_code}")
    except Exception as e:
        log.append(f"  validateAccount failed: {e}")
        return {"ok": False, "error": f"validateAccount failed: {e}"}

    resp_data = d_validate.get("response", {})
    if "error" in d_validate and "response" not in d_validate:
        err = d_validate["error"]
        err_msg = err.get("error_msg", str(err)) if isinstance(err, dict) else str(err)
        log.append(f"  validateAccount error: {err_msg}")
        return {"ok": False, "error": f"validateAccount: {err_msg}"}

    # Получить sid и flow_name из ответа
    new_sid = resp_data.get("sid", sid)
    if new_sid:
        sid = new_sid
    flow_name = resp_data.get("flow_name", "")
    can_skip_password = resp_data.get("can_skip_password", False)

    log.append(f"  sid={sid[:20]}..., flow={flow_name}, skip_pwd={can_skip_password}")

    time.sleep(0.5)

    # --- Шаг 3: POST login.vk.ru/?act=connect_authorize ---
    log.append("[3] POST login.vk.ru/?act=connect_authorize")

    connect_data = {
        "username": login,
        "password": password,
        "auth_token": auth_token,
        "sid": sid,
        "uuid": _uuid(),
        "device_id": device_id,
        "app_id": VK_ADMIN_APP_ID,
        "v": "5.207",
        "service_group": "",
        "save_user": "1",
        "version": "1",
    }

    try:
        r_login = s.post(
            "https://login.vk.ru/?act=connect_authorize",
            data=connect_data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://id.vk.ru",
                "Referer": "https://id.vk.ru/",
            },
            follow_redirects=True,
        )
        log.append(f"  → {r_login.status_code}")
    except Exception as e:
        log.append(f"  connect_authorize failed: {e}")
        return {"ok": False, "error": f"connect_authorize failed: {e}"}

    # Проверить ответ
    try:
        d_login = r_login.json()
    except Exception:
        d_login = {}

    if d_login.get("type") == "error" or d_login.get("error"):
        err_name = d_login.get("error_info", {}).get("error", "")
        err_desc = d_login.get("error_info", {}).get("error_description", "")
        if not err_name:
            err_name = d_login.get("error", "")
            err_desc = d_login.get("error_description", "")
        err_msg = f"{err_name}: {err_desc}" if err_desc else str(err_name)
        log.append(f"  Login error: {err_msg}")
        log.append(f"  Full response: {json.dumps(d_login, ensure_ascii=False)[:500]}")

        # Проверить на бан
        if d_login.get("is_user_banned"):
            return {"ok": False, "error": "Аккаунт VK заблокирован"}

        # Проверить на капчу
        if "captcha" in str(err_name).lower() or "captcha" in str(d_login).lower():
            captcha_sid = d_login.get("captcha_sid", "")
            captcha_img = d_login.get("captcha_img", "")
            if rucaptcha_key and captcha_img:
                log.append("  Solving captcha via RuCaptcha...")
                solution = solve_captcha_rucaptcha(captcha_img, rucaptcha_key)
                if solution:
                    log.append(f"  Captcha solved: {solution}")
                    # TODO: retry with captcha
            return {"ok": False, "error": f"Captcha required: {err_msg}"}

        # Flood control / bruteforce
        if "flood" in str(d_login).lower() or "bruteforce" in str(d_login).lower():
            return {"ok": False, "error": f"VK flood control: {err_msg}"}

        return {"ok": False, "error": f"VK login failed: {err_msg}"}

    # Проверить что получили remixsid
    has_remixsid = False
    for c in s.cookies.jar:
        if c.name in ("remixsid", "remixsid6"):
            has_remixsid = True
            break

    if not has_remixsid:
        # Иногда ответ содержит данные в JSON
        if d_login.get("access_token") or d_login.get("code"):
            log.append(f"  Got token/code from connect_authorize response")
        else:
            log.append(f"  No remixsid cookie set. Response: {json.dumps(d_login, ensure_ascii=False)[:500]}")
            # Не фатально — может быть что cookie на другом домене
            log.append("  Continuing without remixsid...")

    log.append("  VK browser login OK")
    return {"ok": True, "device_id": device_id}


# ---------------------------------------------------------------------------
# Main: получение VK auth code через VK ID browser login
# ---------------------------------------------------------------------------

def get_vk_auth_code(
    login: str,
    password: str,
    rucaptcha_key: str = "",
    captcha_sid: str = "",
    captcha_key: str = "",
    proxy: str = "",
) -> dict:
    """
    Получить VK ID authorization code для приложения Twinby.

    Использует browser-style VK ID login flow вместо deprecated password grant.

    Returns:
        dict с полями:
          ok=True:  code, code_verifier, device_id, state, redirect_uri, log
          ok=False: error, log
    """
    code_verifier, code_challenge = _pkce()
    state = _state()
    log: list[str] = []

    try:
        client_kw: dict = {
            "follow_redirects": False,
            "timeout": 30,
            "headers": {"User-Agent": UA_BROWSER},
        }
        if proxy:
            client_kw["proxy"] = proxy

        with httpx.Client(**client_kw) as s:
            # ═══════════════════════════════════════════════════════════
            # Фаза 1: Browser-style VK login → remixsid cookie
            # ═══════════════════════════════════════════════════════════
            login_result = _vk_browser_login(s, login, password, log, rucaptcha_key)
            if not login_result.get("ok"):
                return {**login_result, "log": log}

            vk_device_id = login_result.get("device_id", _device_id())

            # ═══════════════════════════════════════════════════════════
            # Фаза 2: Получить implicit access_token через oauth.vk.ru
            # ═══════════════════════════════════════════════════════════
            log.append("[4] GET oauth.vk.ru/authorize (implicit)")

            implicit_params = {
                "client_id": VK_ADMIN_APP_ID,
                "scope": "",
                "response_type": "token",
                "v": "5.207",
            }
            r_oauth = s.get("https://oauth.vk.ru/authorize", params=implicit_params)
            log.append(f"  → {r_oauth.status_code}")

            vk_access_token = ""

            # Проверить на редирект с token
            if r_oauth.status_code in (301, 302, 303, 307, 308):
                loc = r_oauth.headers.get("location", "")
                log.append(f"  → redirect: {loc[:120]}")
                parsed = urllib.parse.urlparse(loc)
                frag_params = urllib.parse.parse_qs(parsed.fragment)
                vk_access_token = frag_params.get("access_token", [""])[0]

            # Если 200 — ищем в HTML (может быть connect flow)
            if not vk_access_token and r_oauth.status_code == 200:
                html = r_oauth.text

                # Проверяем window.init JSON для connect flow
                init_match = re.search(r'window\.init\s*=\s*(\{.*?\});', html, re.DOTALL)
                if init_match:
                    try:
                        init_data = json.loads(init_match.group(1))
                        return_auth_hash = init_data.get("return_auth_hash", "")
                        if return_auth_hash:
                            log.append("[4b] POST login.vk.ru/?act=connect_internal")
                            r_connect = s.post(
                                "https://login.vk.ru/?act=connect_internal",
                                data={
                                    "uuid": _uuid(),
                                    "service_group": "",
                                    "return_auth_hash": return_auth_hash,
                                    "version": "1",
                                    "app_id": VK_ADMIN_APP_ID,
                                },
                                headers={
                                    "Origin": "https://id.vk.ru",
                                    "Referer": "https://id.vk.ru/",
                                },
                            )
                            log.append(f"  → {r_connect.status_code}")
                            try:
                                d_connect = r_connect.json()
                                connect_token = d_connect.get("access_token", "")
                                auth_hash = d_connect.get("auth_hash", "")
                                auth_user_hash = d_connect.get("auth_user_hash", "")
                                if connect_token or auth_hash:
                                    log.append("[4c] POST auth.getOauthToken")
                                    r_oauth_token = s.post(
                                        "https://api.vk.ru/method/auth.getOauthToken",
                                        data={
                                            "hash": auth_hash,
                                            "auth_user_hash": auth_user_hash,
                                            "app_id": VK_ADMIN_APP_ID,
                                            "client_id": VK_ADMIN_APP_ID,
                                            "scope": "",
                                            "access_token": connect_token,
                                            "is_seamless_auth": "1",
                                            "v": "5.207",
                                        },
                                    )
                                    log.append(f"  → {r_oauth_token.status_code}")
                                    try:
                                        d_ot = r_oauth_token.json()
                                        resp_ot = d_ot.get("response", {})
                                        vk_access_token = resp_ot.get("access_token", "")
                                        if not vk_access_token:
                                            # Может быть authorize_url с token в fragment
                                            auth_url = resp_ot.get("authorize_url", "")
                                            if auth_url:
                                                frag = urllib.parse.urlparse(auth_url).fragment
                                                fp = urllib.parse.parse_qs(frag)
                                                vk_access_token = fp.get("access_token", [""])[0]
                                    except Exception as e:
                                        log.append(f"  getOauthToken parse error: {e}")
                            except Exception as e:
                                log.append(f"  connect_internal parse error: {e}")
                    except Exception as e:
                        log.append(f"  window.init parse error: {e}")

                # Прямой access_token в URL fragment или HTML
                if not vk_access_token:
                    m = re.search(r'access_token=([a-zA-Z0-9._-]+)', html)
                    if m:
                        vk_access_token = m.group(1)

                # Fallback: follow redirect if there's authorize_url in HTML
                if not vk_access_token:
                    m = re.search(r'"authorize_url"\s*:\s*"([^"]+)"', html)
                    if m:
                        auth_url = m.group(1).replace("\\/", "/")
                        frag = urllib.parse.urlparse(auth_url).fragment
                        fp = urllib.parse.parse_qs(frag)
                        vk_access_token = fp.get("access_token", [""])[0]
                        if not vk_access_token:
                            try:
                                r_follow = s.get(auth_url, follow_redirects=True)
                                frag2 = urllib.parse.urlparse(str(r_follow.url)).fragment
                                fp2 = urllib.parse.parse_qs(frag2)
                                vk_access_token = fp2.get("access_token", [""])[0]
                            except Exception:
                                pass

            if vk_access_token:
                log.append(f"  VK access_token: {vk_access_token[:25]}...")
            else:
                log.append("  No VK access_token from implicit flow, trying direct PKCE...")

            # ═══════════════════════════════════════════════════════════
            # Фаза 3: Получить authorization code для Twinby
            # ═══════════════════════════════════════════════════════════

            # Метод A: id.vk.ru/authorize с cookies сессии (PKCE flow)
            log.append("[5] GET id.vk.ru/authorize (Twinby PKCE)")
            authorize_params = {
                "response_type": "code",
                "client_id": TWINBY_VK_APP_ID,
                "redirect_uri": TWINBY_REDIRECT_URI,
                "code_challenge": code_challenge,
                "code_challenge_method": "s256",
                "state": state,
                "device_id": vk_device_id,
                "scope": "",
            }

            r_twinby = s.get("https://id.vk.ru/authorize", params=authorize_params)
            log.append(f"  → {r_twinby.status_code}")

            code = _follow_redirects_for_code(s, r_twinby, log=log)
            if code:
                log.append(f"  Code from id.vk.ru: {code[:30]}...")
                return _success(code, code_verifier, vk_device_id, state, log)

            # Если 200 — ищем code в HTML/payload
            if r_twinby.status_code == 200:
                html_vkid = r_twinby.text

                # Проверить payload в URL после redirect
                for pattern in [
                    r'"code"\s*:\s*"([^"]+)"',
                    r'code=([a-zA-Z0-9._-]+)',
                ]:
                    m = re.search(pattern, html_vkid)
                    if m:
                        val = m.group(1)
                        if val.startswith("vk2.") or len(val) > 50:
                            log.append(f"  Found code in VK ID page: {val[:30]}...")
                            return _success(val, code_verifier, vk_device_id, state, log)

                log.append(f"  VK ID page (300 chars): {html_vkid[:300]}")

            # Метод B: oauth.vk.ru/authorize (старый endpoint, с cookies)
            log.append("[6] GET oauth.vk.ru/authorize (Twinby, legacy)")
            legacy_params = {
                "client_id": TWINBY_VK_APP_ID,
                "redirect_uri": TWINBY_REDIRECT_URI,
                "response_type": "code",
                "scope": "",
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "s256",
                "v": "5.199",
            }
            r_legacy = s.get("https://oauth.vk.ru/authorize", params=legacy_params)
            log.append(f"  → {r_legacy.status_code}")

            code = _follow_redirects_for_code(s, r_legacy, log=log)
            if code:
                log.append(f"  Code from oauth.vk.ru: {code[:30]}...")
                return _success(code, code_verifier, vk_device_id, state, log)

            if r_legacy.status_code == 200:
                html_legacy = r_legacy.text
                # Ищем grant_access URL
                grant_match = re.search(
                    r'location\.href\s*=\s*["\']([^"\']+)["\']', html_legacy
                ) or re.search(
                    r'<form[^>]+action="([^"]+grant_access[^"]*)"', html_legacy
                )
                if grant_match:
                    grant_url = grant_match.group(1).replace("&amp;", "&")
                    log.append(f"  Found grant URL: {grant_url[:100]}")
                    try:
                        full_url = grant_url if grant_url.startswith("http") else f"https://oauth.vk.ru{grant_url}"
                        r_grant = s.get(full_url)
                        code = _follow_redirects_for_code(s, r_grant, log=log)
                        if code:
                            log.append(f"  Code from grant: {code[:30]}...")
                            return _success(code, code_verifier, vk_device_id, state, log)
                    except Exception as e:
                        log.append(f"  Grant request failed: {e}")

                for pattern in [
                    r'access_token=([a-zA-Z0-9._-]+)',
                    r'code=([a-zA-Z0-9._-]+)',
                    r'"code"\s*:\s*"([^"]+)"',
                ]:
                    m = re.search(pattern, html_legacy)
                    if m:
                        val = m.group(1)
                        if "code" in pattern or val.startswith("vk2."):
                            log.append(f"  Found in legacy HTML: {val[:30]}...")
                            return _success(val, code_verifier, vk_device_id, state, log)

            # ═══════════════════════════════════════════════════════════
            # Не удалось
            # ═══════════════════════════════════════════════════════════
            cookies_str = "; ".join(f"{c.name}={c.value[:20]}..." for c in s.cookies.jar)
            log.append(f"Cookies: {cookies_str}")

            return {
                "ok": False,
                "error": "Не удалось получить authorization code для Twinby.",
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
# Twinby auth v3: отправить VK code на Twinby
# ---------------------------------------------------------------------------

def twinby_auth_v3(
    code: str,
    code_verifier: str,
    device_id: str,
    state: str,
    redirect_uri: str = TWINBY_REDIRECT_URI,
    proxy: str = "",
) -> dict:
    """
    Авторизация в Twinby через v3 endpoint.
    """
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
            return {"ok": False, "error": f"Twinby auth v3: {resp.status_code} {resp.text[:300]}"}
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


def full_auth(login: str, password: str, rucaptcha_key: str = "", proxy: str = "") -> dict:
    """
    Полный цикл авторизации: VK browser login → code → Twinby JWT.
    """
    proxy = _normalize_proxy(proxy)
    # Шаг 1: получить VK auth code
    vk_result = get_vk_auth_code(login, password, rucaptcha_key, proxy=proxy)
    if not vk_result.get("ok"):
        return vk_result

    # Шаг 2: авторизоваться в Twinby
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
