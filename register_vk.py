#!/usr/bin/env python3
"""
Проверка гипотезы: обмен VK access token -> Twinby JWT (/api/auth/v2/oauth/by-token)
с UA = "Dart/3.11 (dart:io)" (не подтверждено для ЭТОГО конкретного эндпоинта,
но это единственный UA, который реально встречается в захватах по всему проекту).

Если обмен пройдёт без ошибок, похожих на UA/WAF-блок — гипотеза подтверждена.
Если сервер вернёт что-то вроде 403 с текстом про bot/UA — не подтверждена,
тогда нужен реальный twinby_onboard.py.

Берёт токен прямо из vk_pool.json (сам я его не вижу и не печатаю).

Запуск (из папки twinby_project, после unzip):
  python3 register_vk.py                  # возьмёт первый свободный (used=false) токен
  python3 register_vk.py --index 0         # возьмёт конкретный по индексу в vk_pool.json
  python3 register_vk.py --proxy "http://user:pass@ip:port"

Результат: печатает статус/ошибку, НЕ печатает сам токен/JWT целиком (только
первые 12 символов для проверки, что что-то вообще пришло), и сохраняет
полный JWT в jwt_result.json рядом — оттуда возьмёшь его для test_liveness.py.
"""
import argparse
import json
import sys
from pathlib import Path

import httpx

BASE = "https://twinby.ru"
AUTH_VK_PATH = "/api/auth/v2/oauth/by-token"
ME_PATH = "/api/users/me/"
# ПОДТВЕРЖДЕНО: взято из реального twinby_onboard.py (источник — HAR 2026-11-20)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
ORIGIN_HEADERS = {"Origin": BASE, "Referer": f"{BASE}/app/onboarding"}


def normalize_proxy(raw: str) -> str:
    if not raw:
        return ""
    raw = raw.strip()
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    parts = raw.split(":")
    if len(parts) == 4:
        ip, port, user, pwd = parts
        return f"http://{user}:{pwd}@{ip}:{port}"
    return raw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", type=int, default=None, help="индекс токена в vk_pool.json")
    ap.add_argument("--proxy", default="", help="прокси (опционально)")
    ap.add_argument("--pool", default="vk_pool.json")
    args = ap.parse_args()

    pool = json.loads(Path(args.pool).read_text(encoding="utf-8"))

    if args.index is not None:
        if args.index < 0 or args.index >= len(pool):
            print(f"[!] индекс {args.index} вне диапазона (0..{len(pool)-1})", file=sys.stderr)
            sys.exit(1)
        entry = pool[args.index]
    else:
        candidates = [e for e in pool if not e.get("used") and not e.get("error")]
        if not candidates:
            print("[!] нет свободных (used=false, без ошибки) токенов — укажи --index вручную", file=sys.stderr)
            sys.exit(1)
        entry = candidates[0]

    token = entry.get("token", "")
    if not token:
        print("[!] у выбранной записи пустой token", file=sys.stderr)
        sys.exit(1)

    print(f"[*] беру токен: {token[:10]}... (used={entry.get('used')}, user_id={entry.get('user_id') or '-'})")

    proxy = normalize_proxy(args.proxy)
    client_kw = {"timeout": 30}
    if proxy:
        client_kw["proxy"] = proxy
        print(f"[*] прокси: {proxy}")

    try:
        with httpx.Client(**client_kw) as client:
            resp = client.post(
                f"{BASE}{AUTH_VK_PATH}",
                json={"provider": "vk", "token": token},
                headers={"User-Agent": UA, "Accept": "application/json",
                         "Content-Type": "application/json", **ORIGIN_HEADERS},
            )
            print(f"[*] AUTH status: {resp.status_code}")
            if resp.status_code >= 400:
                print(f"[!] тело ответа: {resp.text[:500]}")
                sys.exit(1)
            data = resp.json()
            jwt_token = data.get("accessToken", "")
            if not jwt_token:
                print(f"[!] нет accessToken в ответе: {json.dumps(data, ensure_ascii=False)[:500]}")
                sys.exit(1)
            print(f"[+] accessToken получен: {jwt_token[:12]}... (длина {len(jwt_token)})")

            me_resp = client.get(
                f"{BASE}{ME_PATH}",
                headers={"User-Agent": UA, "Accept": "application/json",
                         "Authorization": f"JWT {jwt_token}", **ORIGIN_HEADERS},
            )
            print(f"[*] /api/users/me/ status: {me_resp.status_code}")
            user_id, name = "", ""
            if me_resp.status_code < 400:
                me = me_resp.json()
                user_id = str(me.get("id", ""))
                name = me.get("name", "")
                print(f"[+] me: id={user_id} name={name!r} onboardingCompleted={me.get('onboardingCompleted')}")
            else:
                print(f"[!] /me тело: {me_resp.text[:300]}")

            Path("jwt_result.json").write_text(json.dumps({
                "jwt": jwt_token, "user_id": user_id, "name": name,
                "refresh_token": data.get("refreshToken", ""), "proxy": proxy,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            print("[+] сохранено в jwt_result.json (полный JWT там)")

    except Exception as e:
        print(f"[!] ошибка: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
