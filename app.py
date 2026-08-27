import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("metro-analytics")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
YCLIENTS_WEBHOOK_SECRET = os.getenv("YCLIENTS_WEBHOOK_SECRET", "").strip()
YCLIENTS_USER_TOKEN = os.getenv("YCLIENTS_USER_TOKEN", "").strip()
YCLIENTS_PARTNER_TOKEN = os.getenv("YCLIENTS_PARTNER_TOKEN", "").strip()
YCLIENTS_COMPANY_ID = os.getenv("YCLIENTS_COMPANY_ID", "").strip()
YCLIENTS_API_BASE = (
    os.getenv("YCLIENTS_API_BASE", "https://api.yclients.com/api/v1")
    .strip()
    .rstrip("/")
)

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else ""
_known_chats: set[str] = set()
_lock = threading.Lock()


def now_text() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%d.%m.%Y %H:%M:%S")


def telegram_send(chat_id: str, text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        return False

    try:
        response = requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if not response.ok:
            log.error("Telegram error %s: %s", response.status_code, response.text[:500])
            return False
        return True
    except requests.RequestException:
        log.exception("Telegram request failed")
        return False


def notify_telegram(text: str) -> int:
    targets = set()
    if TELEGRAM_CHAT_ID:
        targets.add(TELEGRAM_CHAT_ID)

    with _lock:
        targets.update(_known_chats)

    sent = 0
    for chat_id in targets:
        if telegram_send(chat_id, text):
            sent += 1
    return sent


def compact(value: Any, limit: int = 1800) -> str:
    try:
        result = json.dumps(value, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        result = str(value)
    if len(result) > limit:
        result = result[:limit] + "\n…"
    return result


def yclients_authorization() -> str:
    """Build the YCLIENTS Authorization header without exposing tokens in logs."""
    if YCLIENTS_PARTNER_TOKEN and YCLIENTS_USER_TOKEN:
        return f"Bearer {YCLIENTS_PARTNER_TOKEN},User {YCLIENTS_USER_TOKEN}"
    if YCLIENTS_USER_TOKEN:
        return f"Bearer {YCLIENTS_USER_TOKEN}"
    return ""


def yclients_get_record(company_id: Any, record_id: Any) -> dict[str, Any] | None:
    """
    Fetch the current record from YCLIENTS after a webhook.

    The webhook itself is the event source. This API call enriches a small
    webhook payload (for example, a comment-only update) with the current
    full record so Telegram receives useful data.
    """
    auth = yclients_authorization()
    if not auth or not company_id or not record_id:
        return None

    url = f"{YCLIENTS_API_BASE}/record/{company_id}/{record_id}"
    headers = {
        "Authorization": auth,
        "Accept": "application/vnd.yclients.v2+json",
        "Content-Type": "application/json",
    }

    try:
        response = requests.get(url, headers=headers, timeout=15)
        if not response.ok:
            log.error(
                "YCLIENTS API record fetch failed: status=%s body=%s",
                response.status_code,
                response.text[:500],
            )
            return None

        result = response.json()
        if isinstance(result, dict):
            data = result.get("data")
            if isinstance(data, dict):
                return data
            return result
    except (requests.RequestException, ValueError):
        log.exception("YCLIENTS API record fetch failed")
    return None


def record_summary(data: Any) -> str:
    """Human-readable key fields for a record without assuming a fixed schema."""
    if not isinstance(data, dict):
        return ""

    parts = []
    for label, key in (
        ("Клиент", "client"),
        ("Сотрудник", "staff"),
        ("Дата", "date"),
        ("Время", "datetime"),
        ("Комментарий", "comment"),
        ("Статус визита", "attendance"),
        ("Подтверждение", "confirmed"),
    ):
        value = data.get(key)
        if value not in (None, "", [], {}):
            parts.append(f"• {label}: {compact(value, 500)}")
    return "\n".join(parts)


def webhook_secret_ok() -> bool:
    if not YCLIENTS_WEBHOOK_SECRET:
        return True

    supplied = (
        request.headers.get("X-YCLIENTS-SECRET")
        or request.headers.get("X-Webhook-Secret")
        or request.args.get("secret")
        or ""
    )
    return supplied == YCLIENTS_WEBHOOK_SECRET


@app.get("/")
def index():
    return jsonify({
        "service": "МЕТРО — Аналитик",
        "status": "online",
        "time": now_text(),
        "endpoints": {
            "health": "/health",
            "yclients_webhook": "/webhook",
            "telegram_webhook": "/telegram/webhook",
            "record_lookup": "/record/<record_id>",
            "register": "/register",
            "callback": "/callback",
        },
    })


@app.route("/health", methods=["GET"], strict_slashes=False)
def health():
    return jsonify({
        "status": "ok",
        "telegram_token_configured": bool(TELEGRAM_BOT_TOKEN),
        "telegram_chat_id_configured": bool(TELEGRAM_CHAT_ID),
        "yclients_webhook_secret_configured": bool(YCLIENTS_WEBHOOK_SECRET),
        "yclients_api_token_configured": bool(YCLIENTS_USER_TOKEN),
        "yclients_company_id_configured": bool(YCLIENTS_COMPANY_ID),
        "known_telegram_chats": len(_known_chats),
        "time": now_text(),
    })


@app.errorhandler(404)
def not_found(error):
    return jsonify({
        "ok": False,
        "error": "not_found",
        "path": request.path,
        "method": request.method,
        "available_endpoints": [
            "/", "/health", "/webhook", "/telegram/webhook",
            "/record/<record_id>", "/register", "/callback"
        ],
    }), 404


@app.route("/webhook", methods=["GET", "POST"])
def yclients_webhook():
    if request.method == "GET":
        return jsonify({
            "status": "ok",
            "message": "YCLIENTS webhook endpoint is available. Send POST JSON here.",
        })

    if not webhook_secret_ok():
        log.warning("Rejected YCLIENTS webhook: bad secret")
        return jsonify({"ok": False, "error": "invalid webhook secret"}), 401

    payload = request.get_json(silent=True)
    if payload is None:
        raw = request.get_data(as_text=True)
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"raw": raw}

    company_id = payload.get("company_id")
    resource = payload.get("resource")
    resource_id = payload.get("resource_id")
    status = payload.get("status")
    data = payload.get("data", {})

    # Always log the complete incoming payload. This is important for diagnosing
    # whether YCLIENTS sent a second event or whether the event stopped upstream.
    log.info("YCLIENTS webhook payload: %s", compact(payload, 7000))
    log.info(
        "YCLIENTS webhook: company_id=%s resource=%s resource_id=%s status=%s",
        company_id, resource, resource_id, status
    )

    current_record = None
    if resource == "record" and resource_id:
        current_record = yclients_get_record(company_id, resource_id)
        if current_record is not None:
            # The webhook can contain only the changed fields. Replace it with
            # the current API state so comments and other fields are visible.
            data = current_record
            log.info(
                "YCLIENTS API enrichment successful: company_id=%s record_id=%s",
                company_id, resource_id
            )
        else:
            log.info(
                "YCLIENTS API enrichment skipped/failed: company_id=%s record_id=%s",
                company_id, resource_id
            )

    summary = record_summary(data) if resource == "record" else ""
    message_parts = [
        "🟢 YCLIENTS — событие",
        "",
        f"🕐 {now_text()}",
        f"🏢 Филиал: {company_id}",
        f"📦 Ресурс: {resource}",
        f"🆔 ID: {resource_id}",
        f"⚙️ Событие: {status}",
    ]
    if summary:
        message_parts.extend(["", "📌 Основные данные:", summary])
    message_parts.extend(["", "📋 Полные данные:", compact(data)])
    message = "\n".join(message_parts)

    sent = notify_telegram(message)

    return jsonify({
        "ok": True,
        "telegram_sent": sent,
        "received": {
            "company_id": company_id,
            "resource": resource,
            "resource_id": resource_id,
            "status": status,
        },
    }), 200


@app.route("/telegram/webhook", methods=["POST"])
def telegram_webhook():
    update = request.get_json(silent=True) or {}
    message = update.get("message") or update.get("edited_message")

    if not message:
        return jsonify({"ok": True})

    chat = message.get("chat") or {}
    chat_id = str(chat.get("id", "")).strip()
    text = (message.get("text") or "").strip()

    if chat_id:
        with _lock:
            _known_chats.add(chat_id)

    if text.startswith("/start"):
        telegram_send(
            chat_id,
            "✅ Бот «МЕТРО — Аналитик» подключён.\n\n"
            "Теперь я буду показывать здесь входящие webhook-события от YCLIENTS.\n\n"
            f"Ваш chat_id: {chat_id}\n\n"
            "Проверка: отправьте /test",
        )
    elif text.startswith("/test"):
        telegram_send(
            chat_id,
            "🧪 Тест Telegram успешен.\n"
            "Бот на связи и готов принимать данные YCLIENTS."
        )
    elif text.startswith("/status"):
        telegram_send(
            chat_id,
            "📡 Статус «МЕТРО — Аналитик»\n\n"
            f"Telegram token: {'✅' if TELEGRAM_BOT_TOKEN else '❌'}\n"
            "YCLIENTS webhook: ✅ /webhook\n"
            f"YCLIENTS API: {'✅' if YCLIENTS_USER_TOKEN else '❌'}\n"
            f"Время сервера: {now_text()}"
        )
    elif text.startswith("/record"):
        parts = text.split()
        if len(parts) < 2 or not parts[1].isdigit():
            telegram_send(chat_id, "Использование: /record ID_ЗАПИСИ")
        else:
            record_id = int(parts[1])
            company_id = YCLIENTS_COMPANY_ID
            data = yclients_get_record(company_id, record_id) if company_id else None
            if data is None:
                telegram_send(
                    chat_id,
                    "❌ Не удалось получить запись. Проверь YCLIENTS_USER_TOKEN "
                    "и YCLIENTS_COMPANY_ID в Render."
                )
            else:
                telegram_send(
                    chat_id,
                    "📋 YCLIENTS — текущая запись\n\n"
                    f"🏢 Филиал: {company_id}\n"
                    f"🆔 ID: {record_id}\n\n"
                    f"{compact(data, 7000)}"
                )
    else:
        telegram_send(
            chat_id,
            "Я на связи. Команды:\n"
            "/test — проверить Telegram\n"
            "/status — статус сервера\n"
            "/record ID — получить текущую запись YCLIENTS",
        )

    return jsonify({"ok": True})


@app.get("/record/<int:record_id>")
def record_lookup(record_id: int):
    """Diagnostic endpoint: fetch the current YCLIENTS record by ID."""
    company_id = request.args.get("company_id") or YCLIENTS_COMPANY_ID
    if not company_id:
        return jsonify({
            "ok": False,
            "error": "YCLIENTS_COMPANY_ID is not configured",
        }), 400

    data = yclients_get_record(company_id, record_id)
    if data is None:
        return jsonify({
            "ok": False,
            "error": "YCLIENTS API request failed or token is not configured",
            "company_id": company_id,
            "record_id": record_id,
        }), 502

    return jsonify({
        "ok": True,
        "company_id": company_id,
        "record_id": record_id,
        "data": data,
    })


@app.get("/register")
def register():
    params = {key: value for key, value in request.args.items()}
    return jsonify({
        "service": "МЕТРО — Аналитик",
        "status": "ready",
        "message": "Регистрация/подключение приложения доступна.",
        "received_params": params,
    })


@app.route("/callback", methods=["GET", "POST"])
def callback():
    params = {key: value for key, value in request.args.items()}
    payload = request.get_json(silent=True)

    log.info("YCLIENTS callback: params=%s payload=%s", params, payload)

    return jsonify({
        "ok": True,
        "message": "Callback received",
        "params": params,
        "payload": payload,
    })


def configure_telegram_webhook() -> None:
    if not TELEGRAM_BOT_TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN is not configured; Telegram notifications disabled.")
        return

    public_url = (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
    if not public_url:
        log.info("PUBLIC_URL is not configured; Telegram webhook will not be set automatically.")
        return

    url = f"{public_url}/telegram/webhook"
    try:
        response = requests.post(
            f"{TELEGRAM_API}/setWebhook",
            json={"url": url, "drop_pending_updates": False},
            timeout=15,
        )
        log.info("Telegram setWebhook: %s %s", response.status_code, response.text[:500])
    except requests.RequestException:
        log.exception("Failed to configure Telegram webhook")


configure_telegram_webhook()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
