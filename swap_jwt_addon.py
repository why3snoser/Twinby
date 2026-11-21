"""
mitmproxy addon: перехватывает POST /api/photo/liveness/v2 от эмулятора
и подменяет JWT-токен на целевой аккаунт.

Видео остаётся от настоящего Android MediaRecorder — никакой конвертации.
Сервер видит 100% подлинный Android-запрос, только аутентификация от другого аккаунта.

Использование:
    1. Получи JWT целевого аккаунта (из app.py или twinby_onboard.py)
    2. Запусти mitmproxy с этим аддоном:
       mitmdump -s swap_jwt_addon.py --set target_jwt="JWT eyJ..."
    3. Направь BlueStacks через этот прокси
    4. Пройди liveness-верификацию в эмуляторе — запрос уйдёт с JWT целевого аккаунта
"""

from mitmproxy import http, ctx
import json
import uuid
import time


LIVENESS_PATH = "/api/photo/liveness/v2"


def load(loader):
    loader.add_option(
        name="target_jwt",
        typespec=str,
        default="",
        help="JWT-токен целевого аккаунта (полный, с 'JWT ' префиксом)",
    )


def request(flow: http.HTTPFlow) -> None:
    if flow.request.path != LIVENESS_PATH:
        return
    if flow.request.method != "POST":
        return

    target_jwt = ctx.options.target_jwt
    if not target_jwt:
        ctx.log.warn("[swap_jwt] target_jwt не задан! Пропускаю без изменений.")
        return

    # Убеждаемся что есть префикс
    if not target_jwt.startswith("JWT "):
        target_jwt = "JWT " + target_jwt

    original_auth = flow.request.headers.get("authorization", "?")
    flow.request.headers["authorization"] = target_jwt

    # Новый idempotency-key чтобы не было конфликта
    flow.request.headers["idempotency-key"] = str(uuid.uuid4())

    # Новый sentry-trace
    trace_id = uuid.uuid4().hex
    span_id = uuid.uuid4().hex[:16]
    flow.request.headers["sentry-trace"] = f"{trace_id}-{span_id}-0"

    ctx.log.info(
        f"[swap_jwt] Перехвачен liveness POST!\n"
        f"  Оригинальный JWT: {original_auth[:30]}...\n"
        f"  Подменён на:      {target_jwt[:30]}...\n"
        f"  Видео: {len(flow.request.content)} байт (оригинал Android)"
    )


def response(flow: http.HTTPFlow) -> None:
    if flow.request.path != LIVENESS_PATH:
        return
    if flow.request.method != "POST":
        return

    status = flow.response.status_code
    proc_time = flow.response.headers.get("x-process-time", "?")
    body = flow.response.text[:200] if flow.response.text else ""

    if status == 200:
        ctx.log.info(f"[swap_jwt] ✅ УСПЕХ! status={status}, process-time={proc_time}")
    else:
        ctx.log.warn(
            f"[swap_jwt] ❌ status={status}, process-time={proc_time}\n"
            f"  body: {body}"
        )
