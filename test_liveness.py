#!/usr/bin/env python3
"""
Автономный скрипт диагностики /api/photo/liveness/v2 — без app.py, без Flask.
Повторяет запрос 1-в-1 как _send_liveness_bytes в app.py (multipart "data",
Dart/3.11 UA, idempotency-key, sentry-trace), но:
  - печатает статус/время/тело ответа сразу в консоль (mitmproxy не нужен);
  - флагом --no-convert можно отправить видео СЫРЫМ, без convert_for_liveness
    (это и есть приоритетный тест: та самая 200-OK видео без доп. конвертации).

Запуск (в папке twinby_project, после unzip):
  python3 test_liveness.py --token "JWT_ТОКЕН" --video in.mp4 --no-convert
  python3 test_liveness.py --token "JWT_ТОКЕН" --video other.mp4   # с конвертацией

Токен бери из accounts.json -> поле "jwt" (без "JWT " спереди, просто сам токен).
Прокси (опционально) --proxy "http://user:pass@ip:port" — тот же формат, что в accounts.json/proxy_pool.json.
"""
import argparse
import base64
import sys
import time
import uuid
from collections import OrderedDict

try:
    from curl_cffi import requests as cffi_requests
except ImportError:
    print("Нужен curl_cffi: pip install curl_cffi", file=sys.stderr)
    sys.exit(1)

BASE = "https://twinby.ru"
LIVENESS_PATH = "/api/photo/liveness/v2"


def normalize_proxy(raw: str) -> str:
    """Поддерживает и готовый http://user:pass@ip:port, и ip:port:user:pass."""
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


def build_multipart(raw_bytes: bytes):
    fname = f"REC{time.time_ns()}.mp4"
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
    body = header_part + raw_bytes + footer_part
    return body, boundary


def build_headers(token: str, body_len: int, boundary: str):
    idem = str(uuid.uuid4())
    trace_id = uuid.uuid4().hex
    span_id = uuid.uuid4().hex[:16]
    sentry_trace = f"{trace_id}-{span_id}-0"
    # ПОДТВЕРЖДЕНО: порядок и состав заголовков взяты из реального успешного
    # запроса (mitmproxy, 2026-11-23, Verification Passed). Заголовка
    # "Connection" в реальном запросе НЕТ вообще.
    return OrderedDict([
        ("user-agent", "Dart/3.11 (dart:io)"),
        ("idempotency-key", idem),
        ("accept", "application/json"),
        ("accept-language", "en"),
        ("accept-encoding", "gzip"),
        ("content-length", str(body_len)),
        ("host", "twinby.ru"),
        ("authorization", f"JWT {token}"),
        ("content-type", f"multipart/form-data; boundary={boundary}"),
        ("sentry-trace", sentry_trace),
    ])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", required=True, help="JWT токен аккаунта (без 'JWT ' спереди)")
    ap.add_argument("--video", required=True, help="путь к видеофайлу")
    ap.add_argument("--proxy", default="", help="прокси accounts.json-формата (опционально)")
    ap.add_argument("--no-convert", action="store_true",
                     help="отправить видео как есть, БЕЗ convert_for_liveness")
    ap.add_argument("--vflip", action="store_true", help="передать vflip=True в convert_for_liveness")
    ap.add_argument("--mitm", default="", help="адрес mitmproxy, напр. http://127.0.0.1:8080 (отключает проверку TLS-сертификата, перебивает --proxy)")
    args = ap.parse_args()

    with open(args.video, "rb") as f:
        raw_bytes = f.read()
    print(f"[*] Исходный файл: {args.video} ({len(raw_bytes)} байт)")

    if not args.no_convert:
        try:
            from convert_to_android import convert_for_liveness
        except ImportError as e:
            print(f"[!] Не удалось импортировать convert_to_android.py: {e}", file=sys.stderr)
            sys.exit(1)
        print("[*] Прогоняю через convert_for_liveness...")
        raw_bytes = convert_for_liveness(raw_bytes, debug_print=print, vflip=args.vflip)
        print(f"[*] После конвертации: {len(raw_bytes)} байт")
    else:
        print("[*] --no-convert: отправляю сырые байты БЕЗ конвертации")

    verify = True
    if args.mitm:
        norm_proxy = args.mitm
        verify = False
        print(f"[*] mitmproxy: {norm_proxy} (TLS verify отключена)")
    else:
        norm_proxy = normalize_proxy(args.proxy)
        if norm_proxy:
            print(f"[*] Прокси: {norm_proxy}")
        else:
            print("[*] Без прокси")
    proxies = {"https": norm_proxy, "http": norm_proxy} if norm_proxy else None

    body, boundary = build_multipart(raw_bytes)
    headers = build_headers(args.token, len(body), boundary)

    url = f"{BASE}{LIVENESS_PATH}"
    print(f"[*] POST {url} ...")
    t0 = time.time()
    try:
        resp = cffi_requests.post(url, headers=headers, data=body, timeout=60, proxies=proxies, verify=verify)
        elapsed = round(time.time() - t0, 2)
        print(f"\n=== РЕЗУЛЬТАТ ===")
        print(f"status      : {resp.status_code}")
        print(f"elapsed_sec : {elapsed}")
        print(f"x-process-time header: {resp.headers.get('x-process-time')}")
        print(f"body (first 1000 chars):\n{resp.text[:1000]}")
    except Exception as e:
        elapsed = round(time.time() - t0, 2)
        print(f"\n=== ОШИБКА после {elapsed}s ===\n{e}")


if __name__ == "__main__":
    main()
