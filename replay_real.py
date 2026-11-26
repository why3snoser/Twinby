"""
Решающий диагностический тест: берёт РЕАЛЬНЫЙ успешный запрос (сохранённый через
mitmweb "Download request" на настоящем успешном Verification Passed), меняет
ТОЛЬКО authorization и idempotency-key, и шлёт один в один оставшиеся байты
(multipart boundary + видео) — без convert_to_android.py, без нашего построения
заголовков. Это проверяет: проходит ли ТОЧНО тот же реальный файл для нового
аккаунта, или дело не в видео вовсе.

Использование:
    python replay_real.py --raw "путь_к_файлу_Download_request" --token "<JWT нового аккаунта>"
"""
import argparse
import sys
import uuid

from curl_cffi import requests as cffi_requests


def parse_raw_request(raw: bytes):
    """Файл из mitmweb 'Download request' — это тело запроса (multipart), как
    сохранено mitmproxy. Возвращает (boundary: str, body: bytes)."""
    idx = raw.find(b"\r\n\r\n")
    if idx == -1:
        raise RuntimeError("Не нашёл конец заголовка multipart-части (\\r\\n\\r\\n)")
    first_line = raw[:raw.find(b"\r\n")]
    if not first_line.startswith(b"--"):
        raise RuntimeError(f"Не похоже на multipart body, первая строка: {first_line!r}")
    boundary = first_line[2:].decode("ascii")
    return boundary, raw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True, help="Файл из mitmweb Download request (тот самый успешный)")
    ap.add_argument("--token", required=True, help="JWT нового аккаунта (без 'JWT ')")
    ap.add_argument("--proxy", default=None, help="например http://127.0.0.1:8080 (для своей же диагностики через mitm)")
    ap.add_argument("--mitm", action="store_true", help="то же что --proxy 127.0.0.1:8080 но verify=False")
    args = ap.parse_args()

    with open(args.raw, "rb") as f:
        raw = f.read()

    boundary, body = parse_raw_request(raw)
    print(f"[*] boundary: {boundary}")
    print(f"[*] тело (как есть, без изменений): {len(body)} байт")

    token = args.token.strip()
    if token.startswith("JWT "):
        token = token[4:]

    idem = str(uuid.uuid4())
    trace_id = uuid.uuid4().hex
    span_id = uuid.uuid4().hex[:16]
    sentry_trace = f"{trace_id}-{span_id}-0"

    headers = {
        "user-agent": "Dart/3.11 (dart:io)",
        "idempotency-key": idem,
        "accept": "application/json",
        "accept-language": "en",
        "accept-encoding": "gzip",
        "content-length": str(len(body)),
        "host": "twinby.ru",
        "authorization": f"JWT {token}",
        "content-type": f"multipart/form-data; boundary={boundary}",
        "sentry-trace": sentry_trace,
    }

    proxies = None
    verify = True
    if args.mitm:
        proxies = {"https": "http://127.0.0.1:8080", "http": "http://127.0.0.1:8080"}
        verify = False
    elif args.proxy:
        proxies = {"https": args.proxy, "http": args.proxy}
        verify = False

    print(f"[*] idempotency-key: {idem}")
    print("[*] POST /api/photo/liveness/v2 (тело НЕ трогали — байт в байт реальный файл)")

    resp = cffi_requests.post(
        "https://twinby.ru/api/photo/liveness/v2",
        headers=headers,
        data=body,
        timeout=60,
        proxies=proxies,
        verify=verify,
        impersonate="chrome",
    )
    print(f"status: {resp.status_code}")
    print(f"x-process-time: {resp.headers.get('x-process-time')}")
    print(f"body: {resp.text[:1000]}")


if __name__ == "__main__":
    sys.exit(main())
