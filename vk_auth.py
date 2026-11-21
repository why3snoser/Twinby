"""
VK ID OAuth 2.0 + PKCE: получение authorization code (vk2.a.*) для приложения Twinby.

Стратегия:
  1. Логин через oauth.vk.com/token (password grant) → получаем VK access token + cookies
  2. Используем сессию для id.vk.com/authorize → авторизуем приложение Twinby
  3. Ловим редирект с authorization code

Капча: если VK требует капчу, возвращаем captcha_sid + captcha_img для решения через RuCaptcha.
"""
from __future__ import annotations

import hashlib
import base64
import secrets
import json
import re
import time
import urllib.parse
from typing import Optional

import httpx

# Twinby VK App — из реального трафика мобильного приложения (mitmproxy 2026-11-21)
TWINBY_VK_APP_ID = "7984771"
TWINBY_REDIRECT_URI = "vk7984771://vk.ru/blank.html"

# Официальные credentials Android-приложения ВКонтакте (публичные, из VK SDK / jkulvich/vkauth)
VK_ANDROID_CLIENT_ID = "2274003"
VK_ANDROID_CLIENT_SECRET = "hHbZxrka2uZ6jB1inYsH"

UA_ANDROID = (
    "Mozilla/5.0 (Linux; Android 13; SM-G998B Build/TP1A.220624.014) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.6099.230 Mobile Safari/537.36"
)
UA_DART = "Dart/3.11 (dart:io)"


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
    """Random state parameter."""
    return base64.urlsafe_b64encode(secrets.token_bytes(22)).rstrip(b"=").decode()


# ---------------------------------------------------------------------------
# RuCaptcha solver
# ---------------------------------------------------------------------------

def solve_captcha_rucaptcha(captcha_img_url: str, api_key: str) -> Optional[str]:
    """Решить капчу через RuCaptcha API. Возвращает текст капчи или None."""
    if not api_key:
        return None
    try:
        # Шаг 1: отправить капчу
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

        # Шаг 2: ждать решения (макс 60 сек)
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
    # Код может быть в query или fragment
    for part in (parsed.query, parsed.fragment):
        params = urllib.parse.parse_qs(part)
        code = params.get("code", [""])[0]
        if code:
            return code
    return None


def _follow_redirects_for_code(client: httpx.Client, resp: httpx.Response,
                                max_hops: int = 10, log: list = None) -> Optional[str]:
    """Следовать редиректам, пока не найдём code в redirect URL или не упрёмся в страницу."""
    for _ in range(max_hops):
        if resp.status_code not in (301, 302, 303, 307, 308):
            return None
        location = resp.headers.get("location", "")
        if log is not None:
            log.append(f"  → {resp.status_code} → {location[:120]}")

        # Если редирект на наш redirect_uri — извлечь code
        if location.startswith(TWINBY_REDIRECT_URI) or location.startswith("vk7984771://"):
            return _extract_code_from_url(location)

        # Если это относительный URL, сделать абсолютным
        if location.startswith("/"):
            base = f"{resp.url.scheme}://{resp.url.host}"
            location = base + location

        # Если это не HTTP(S) — это custom scheme, извлечь код
        if not location.startswith("http"):
            return _extract_code_from_url(location)

        try:
            resp = client.get(location)
        except Exception:
            return None
    return None


# ---------------------------------------------------------------------------
# Main: получение VK auth code
# ---------------------------------------------------------------------------

def get_vk_auth_code(
    login: str,
    password: str,
    rucaptcha_key: str = "",
    captcha_sid: str = "",
    captcha_key: str = "",
) -> dict:
    """
    Получить VK ID authorization code для приложения Twinby.

    Args:
        login: VK логин (телефон/email/id)
        password: VK пароль
        rucaptcha_key: API ключ RuCaptcha (для автоматического решения капчи)
        captcha_sid: sid капчи (если уже получена)
        captcha_key: решение капчи (если уже есть)

    Returns:
        dict с полями:
          ok=True:  code, code_verifier, device_id, state, redirect_uri, log
          ok=False: error, log, [captcha_sid, captcha_img если нужна капча]
    """
    code_verifier, code_challenge = _pkce()
    device_id = _device_id()
    state = _state()
    log: list[str] = []

    try:
        with httpx.Client(
            follow_redirects=False,
            timeout=30,
            headers={"User-Agent": UA_ANDROID},
        ) as s:
            # ═══════════════════════════════════════════════════════════
            # Шаг 1: VK password grant → access_token + session cookies
            # ═══════════════════════════════════════════════════════════
            token_params: dict = {
                "grant_type": "password",
                "client_id": VK_ANDROID_CLIENT_ID,
                "client_secret": VK_ANDROID_CLIENT_SECRET,
                "username": login,
                "password": password,
                "v": "5.199",
                "2fa_supported": "1",
                "force_sms": "0",
            }
            if captcha_sid and captcha_key:
                token_params["captcha_sid"] = captcha_sid
                token_params["captcha_key"] = captcha_key

            r_token = s.get("https://oauth.vk.com/token", params=token_params)
            d_token = r_token.json()
            log.append(f"[1] oauth.vk.com/token → {r_token.status_code}")

            if "error" in d_token:
                err = d_token["error"]
                if err == "need_captcha":
                    img_url = d_token.get("captcha_img", "")
                    sid = d_token.get("captcha_sid", "")
                    log.append(f"  Captcha required: sid={sid}")

                    # Автоматическое решение через RuCaptcha
                    if rucaptcha_key and img_url:
                        log.append("  Solving captcha via RuCaptcha...")
                        solution = solve_captcha_rucaptcha(img_url, rucaptcha_key)
                        if solution:
                            log.append(f"  Captcha solved: {solution}")
                            # Рекурсивный вызов с решённой капчей
                            return get_vk_auth_code(
                                login, password, rucaptcha_key,
                                captcha_sid=sid, captcha_key=solution,
                            )
                        else:
                            log.append("  Captcha solve FAILED")

                    return {
                        "ok": False,
                        "error": "need_captcha",
                        "captcha_sid": sid,
                        "captcha_img": img_url,
                        "log": log,
                    }

                elif err == "need_validation":
                    return {
                        "ok": False,
                        "error": f"2FA required: {d_token.get('redirect_uri', '')}",
                        "log": log,
                    }
                elif err == "invalid_client":
                    return {"ok": False, "error": "Неверный логин или пароль VK", "log": log}
                else:
                    desc = d_token.get("error_description", err)
                    return {"ok": False, "error": f"VK login error: {desc}", "log": log}

            vk_access_token = d_token.get("access_token", "")
            if not vk_access_token:
                return {"ok": False, "error": f"No access_token: {json.dumps(d_token)[:300]}", "log": log}

            log.append(f"  VK token OK: {vk_access_token[:25]}...")

            # ═══════════════════════════════════════════════════════════
            # Шаг 2: Авторизация приложения Twinby через oauth.vk.com
            # ═══════════════════════════════════════════════════════════
            # Пробуем через oauth.vk.com/authorize с полученной сессией.
            # Это старый endpoint, но он может выдать code для VK ID приложения.
            authorize_params = {
                "client_id": TWINBY_VK_APP_ID,
                "redirect_uri": TWINBY_REDIRECT_URI,
                "response_type": "code",
                "scope": "",
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "s256",
                "v": "5.199",
            }

            r_auth = s.get("https://oauth.vk.com/authorize", params=authorize_params)
            log.append(f"[2] oauth.vk.com/authorize → {r_auth.status_code}")

            # Проверить на авто-редирект
            code = _follow_redirects_for_code(s, r_auth, log=log)
            if code:
                log.append(f"  Code from oauth.vk.com: {code[:30]}...")
                return _success(code, code_verifier, device_id, state, log)

            # Если вернулась страница — ищем grant_access URL в HTML
            if r_auth.status_code == 200:
                html = r_auth.text
                # VK часто возвращает страницу с автоматическим одобрением
                # или с формой подтверждения. Ищем redirect URL в HTML.
                grant_match = re.search(
                    r'location\.href\s*=\s*["\']([^"\']+)["\']', html
                ) or re.search(
                    r'<form[^>]+action="([^"]+grant_access[^"]*)"', html
                )
                if grant_match:
                    grant_url = grant_match.group(1).replace("&amp;", "&")
                    log.append(f"  Found grant URL: {grant_url[:100]}")
                    try:
                        r_grant = s.get(grant_url) if grant_url.startswith("http") else s.get(f"https://oauth.vk.com{grant_url}")
                        code = _follow_redirects_for_code(s, r_grant, log=log)
                        if code:
                            log.append(f"  Code from grant: {code[:30]}...")
                            return _success(code, code_verifier, device_id, state, log)
                    except Exception as e:
                        log.append(f"  Grant request failed: {e}")

                # Ищем hash с token/code в inline JS
                for pattern in [
                    r'access_token=([a-zA-Z0-9._-]+)',
                    r'code=([a-zA-Z0-9._-]+)',
                    r'"code"\s*:\s*"([^"]+)"',
                ]:
                    m = re.search(pattern, html)
                    if m:
                        val = m.group(1)
                        log.append(f"  Found in HTML: {val[:30]}...")
                        if "code" in pattern or val.startswith("vk2."):
                            return _success(val, code_verifier, device_id, state, log)

            # ═══════════════════════════════════════════════════════════
            # Шаг 3: Попробовать id.vk.com/authorize с cookies сессии
            # ═══════════════════════════════════════════════════════════
            authorize_params_vkid = {
                "response_type": "code",
                "client_id": TWINBY_VK_APP_ID,
                "redirect_uri": TWINBY_REDIRECT_URI,
                "code_challenge": code_challenge,
                "code_challenge_method": "s256",
                "state": state,
                "device_id": device_id,
            }

            r_vkid = s.get("https://id.vk.com/authorize", params=authorize_params_vkid)
            log.append(f"[3] id.vk.com/authorize → {r_vkid.status_code}")

            code = _follow_redirects_for_code(s, r_vkid, log=log)
            if code:
                log.append(f"  Code from id.vk.com: {code[:30]}...")
                return _success(code, code_verifier, device_id, state, log)

            # Если вернулась страница, проверить содержимое
            if r_vkid.status_code == 200:
                html_vkid = r_vkid.text
                for pattern in [
                    r'code=([a-zA-Z0-9._-]+)',
                    r'"code"\s*:\s*"([^"]+)"',
                ]:
                    m = re.search(pattern, html_vkid)
                    if m:
                        val = m.group(1)
                        if val.startswith("vk2.") or len(val) > 50:
                            log.append(f"  Found code in VK ID page: {val[:30]}...")
                            return _success(val, code_verifier, device_id, state, log)

                # Показать начало HTML для диагностики
                log.append(f"  VK ID page (first 300 chars): {html_vkid[:300]}")

            # ═══════════════════════════════════════════════════════════
            # Шаг 4: Попробовать authorize с access_token в заголовке
            # ═══════════════════════════════════════════════════════════
            s.headers["Authorization"] = f"Bearer {vk_access_token}"
            r_auth2 = s.get("https://oauth.vk.com/authorize", params=authorize_params)
            log.append(f"[4] oauth.vk.com/authorize + Bearer → {r_auth2.status_code}")

            code = _follow_redirects_for_code(s, r_auth2, log=log)
            if code:
                log.append(f"  Code with Bearer: {code[:30]}...")
                return _success(code, code_verifier, device_id, state, log)

            del s.headers["Authorization"]

            # ═══════════════════════════════════════════════════════════
            # Шаг 5: Попробовать через VK API method apps.get + authorize
            # ═══════════════════════════════════════════════════════════
            # Некоторые VK-приложения используют скрытый grant через API
            try:
                r_api = s.get(
                    "https://api.vk.com/method/auth.getProfileInfoBySilentToken",
                    params={
                        "access_token": vk_access_token,
                        "v": "5.199",
                    },
                )
                log.append(f"[5] auth.getProfileInfoBySilentToken → {r_api.status_code}: {r_api.text[:200]}")
            except Exception as e:
                log.append(f"[5] API call failed: {e}")

            # ═══════════════════════════════════════════════════════════
            # Не удалось — возвращаем подробности для отладки
            # ═══════════════════════════════════════════════════════════
            cookies_str = "; ".join(f"{c.name}={c.value[:20]}..." for c in s.cookies.jar)
            log.append(f"Cookies in jar: {cookies_str}")

            return {
                "ok": False,
                "error": (
                    "Не удалось получить authorization code. "
                    "VK ID может требовать браузерный OAuth flow. "
                    "Смотри log для подробностей."
                ),
                "log": log,
                "vk_access_token": vk_access_token,
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
) -> dict:
    """
    Авторизация в Twinby через v3 endpoint.

    Returns:
        dict: {"ok": True, "accessToken": "...", "refreshToken": "..."}
              или {"ok": False, "error": "..."}
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
        resp = httpx.post(
            "https://twinby.ru/api/auth/v3/oauth/by-token",
            json=body,
            headers={
                "User-Agent": UA_DART,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Host": "twinby.ru",
            },
            timeout=30,
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

def full_auth(login: str, password: str, rucaptcha_key: str = "") -> dict:
    """
    Полный цикл авторизации: VK login → code → Twinby JWT.

    Returns:
        dict: {"ok": True, "jwt": "...", "log": [...]}
              или {"ok": False, "error": "...", "log": [...]}
    """
    # Шаг 1: получить VK auth code
    vk_result = get_vk_auth_code(login, password, rucaptcha_key)
    if not vk_result.get("ok"):
        return vk_result

    # Шаг 2: авторизоваться в Twinby
    tw_result = twinby_auth_v3(
        code=vk_result["code"],
        code_verifier=vk_result["code_verifier"],
        device_id=vk_result["device_id"],
        state=vk_result["state"],
        redirect_uri=vk_result["redirect_uri"],
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
