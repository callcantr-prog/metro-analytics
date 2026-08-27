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
            "/register", "/callback"
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

    message = (
        "🟢 YCLIENTS — получен webhook\n\n"
        f"🕐 {now_text()}\n"
        f"🏢 Филиал: {company_id}\n"
        f"📦 Ресурс: {resource}\n"
        f"🆔 ID: {resource_id}\n"
        f"⚙️ Событие: {status}\n\n"
        "📋 Данные:\n"
        f"{compact(data)}"
    )

    log.info(
        "YCLIENTS webhook: company_id=%s resource=%s resource_id=%s status=%s",
        company_id, resource, resource_id, status
    )

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
            f"Время сервера: {now_text()}"
        )
    else:
        telegram_send(
            chat_id,
            "Я на связи. Команды:\n"
            "/test — проверить Telegram\n"
            "/status — статус сервера",
        )

    return jsonify({"ok": True})


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
