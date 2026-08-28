import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ============================================================
# МЕТРО — ANALYTICS
# Филиал: Транссибирская 1
#
# Render ENV (уже настроенные):
# TELEGRAM_BOT_TOKEN
# YCLIENTS_COMPANY_ID
# YCLIENTS_USER_TOKEN
# YCLIENTS_PARTNER_TOKEN (required for YCLIENTS API authorization)
# PUBLIC_URL
#
# Администраторы НЕ используются.
# ============================================================

PUBLIC_URL = os.getenv("PUBLIC_URL", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").rstrip("/")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
YCLIENTS_COMPANY_ID = os.getenv("YCLIENTS_COMPANY_ID", "").strip()
YCLIENTS_USER_TOKEN = os.getenv("YCLIENTS_USER_TOKEN", "").strip()
# YCLIENTS API requires BOTH partner and user authorization.
# Support several common env names so the existing Render secret can be reused.
YCLIENTS_PARTNER_TOKEN = (
    os.getenv("YCLIENTS_PARTNER_TOKEN", "").strip()
    or os.getenv("YCLIENTS_PARTNER_ID", "").strip()
    or os.getenv("YCLIENTS_TOKEN_PARTNER", "").strip()
)

COMPANY_ID = int(YCLIENTS_COMPANY_ID or "0")
BRANCH_NAME = "МЕТРО — Транссибирская 1"
TZ = ZoneInfo("Asia/Omsk")
TG = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else ""
YC = "https://api.yclients.com/api/v1"

DB = "/tmp/metro_analytics.sqlite3"
chats = set()
lock = threading.Lock()


# -------------------- База --------------------

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS chats(
            chat_id TEXT PRIMARY KEY,
            added_at TEXT NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS webhook_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at TEXT NOT NULL,
            payload TEXT NOT NULL
        )
    """)
    for row in con.execute("SELECT chat_id FROM chats"):
        chats.add(row[0])
    con.commit()
    con.close()


def remember_chat(chat_id):
    if not chat_id:
        return
    cid = str(chat_id)
    with lock:
        chats.add(cid)
    con = sqlite3.connect(DB)
    con.execute(
        "INSERT OR IGNORE INTO chats(chat_id, added_at) VALUES(?,?)",
        (cid, datetime.now(TZ).isoformat())
    )
    con.commit()
    con.close()


# -------------------- Утилиты --------------------

def money(value):
    try:
        return f"{float(value):,.0f} ₽".replace(",", " ")
    except Exception:
        return "0 ₽"


def number(value):
    try:
        return f"{float(value):,.0f}".replace(",", " ")
    except Exception:
        return "0"


def change(current, previous):
    try:
        current = float(current or 0)
        previous = float(previous or 0)
        if previous == 0:
            return "—"
        value = (current - previous) / abs(previous) * 100
        icon = "🟢" if value > 0 else "🔴" if value < 0 else "⚪"
        return f"{icon} {value:+.1f}%"
    except Exception:
        return "—"


def parse_date(value):
    if not value:
        return None
    value = str(value).replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(value)
        if d.tzinfo is None:
            d = d.replace(tzinfo=TZ)
        return d.astimezone(TZ)
    except Exception:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(str(value)[:19], fmt).replace(tzinfo=TZ)
            except Exception:
                pass
    return None


# -------------------- Telegram --------------------

def tg_send(chat_id, text, keyboard=None):
    if not TG:
        return False

    payload = {"chat_id": chat_id, "text": text}

    if keyboard:
        payload["reply_markup"] = keyboard

    try:
        response = requests.post(
            f"{TG}/sendMessage",
            json=payload,
            timeout=30
        )
        print("Telegram:", response.status_code, response.text[:500])
        return response.ok
    except Exception as exc:
        print("Telegram error:", repr(exc))
        return False


def broadcast(text):
    for chat_id in list(chats):
        tg_send(chat_id, text)


# -------------------- YCLIENTS --------------------

def yclients_headers():
    # YCLIENTS expects partner authorization first and the user token after it:
    # Authorization: Bearer <partner_token>, User <user_token>
    authorization = f"Bearer {YCLIENTS_PARTNER_TOKEN}"
    if YCLIENTS_USER_TOKEN:
        authorization += f", User {YCLIENTS_USER_TOKEN}"
    return {
        "Accept": "application/vnd.yclients.v2+json",
        "Content-Type": "application/json",
        "Authorization": authorization,
    }


def yclients_get(path, params=None):
    if not COMPANY_ID or not YCLIENTS_USER_TOKEN or not YCLIENTS_PARTNER_TOKEN:
        print("YCLIENTS: missing COMPANY_ID, USER_TOKEN or PARTNER_TOKEN")
        return None

    try:
        response = requests.get(
            f"{YC}/{path.lstrip('/')}",
            headers=yclients_headers(),
            params=params or {},
            timeout=30
        )

        if not response.ok:
            print("YCLIENTS:", response.status_code, response.text[:1000])
            return None

        data = response.json()
        return data.get("data", data)

    except Exception as exc:
        print("YCLIENTS error:", repr(exc))
        return None


def yclients_list(path, params=None, max_pages=50):
    params = dict(params or {})
    params.setdefault("count", 100)

    result = []

    for page in range(1, max_pages + 1):
        params["page"] = page
        data = yclients_get(path, params)

        if data is None:
            break

        if isinstance(data, dict):
            rows = data.get("data", [])
        else:
            rows = data

        if not isinstance(rows, list):
            break

        result.extend(rows)

        if len(rows) < params["count"]:
            break

    return result


def get_records(start_date, end_date):
    return yclients_list(
        f"records/{COMPANY_ID}",
        {
            "start_date": start_date,
            "end_date": end_date,
            "count": 100
        },
        100
    )


def get_clients():
    return yclients_list(
        f"clients/{COMPANY_ID}",
        {"count": 100},
        100
    )


# -------------------- Разбор YCLIENTS --------------------

def get_attendance(record):
    try:
        return int(record.get("attendance", 0) or 0)
    except Exception:
        return 0


def get_client(record):
    client = record.get("client") or {}
    if isinstance(client, list):
        return client[0] if client else {}
    return client


def get_staff(record):
    staff = record.get("staff") or record.get("master") or {}
    if isinstance(staff, list):
        return staff[0] if staff else {}
    return staff


def record_revenue(record):
    total = 0.0

    for service in record.get("services") or []:
        try:
            total += float(
                service.get(
                    "cost_to_pay",
                    service.get("cost", 0)
                ) or 0
            )
        except Exception:
            pass

    for good in record.get("goods_transactions") or []:
        try:
            total += float(
                good.get(
                    "cost",
                    good.get("amount", 0)
                ) or 0
            )
        except Exception:
            pass

    return total


def calculate(records):
    result = {
        "records": 0,
        "attended": 0,
        "noshow": 0,
        "cancelled": 0,
        "revenue": 0.0,
        "clients": set(),
        "staff": defaultdict(dict),
        "services": defaultdict(
            lambda: {"count": 0, "revenue": 0.0}
        ),
        "sources": defaultdict(int)
    }

    for record in records:
        result["records"] += 1

        attendance = get_attendance(record)

        if record.get("deleted"):
            result["cancelled"] += 1
        elif attendance == 1:
            result["attended"] += 1
        elif attendance == -1:
            result["noshow"] += 1

        client = get_client(record)
        client_id = client.get("id")

        if client_id:
            result["clients"].add(str(client_id))

        revenue = record_revenue(record) if attendance == 1 else 0
        result["revenue"] += revenue

        staff = get_staff(record)
        staff_id = str(
            staff.get("id")
            or record.get("staff_id")
            or staff.get("name")
            or "unknown"
        )
        staff_name = staff.get("name") or "Без мастера"

        master = result["staff"].setdefault(
            staff_id,
            {
                "name": staff_name,
                "records": 0,
                "attended": 0,
                "noshow": 0,
                "revenue": 0.0,
                "clients": set()
            }
        )

        master["records"] += 1
        master["revenue"] += revenue

        if attendance == 1:
            master["attended"] += 1
        elif attendance == -1:
            master["noshow"] += 1

        if client_id:
            master["clients"].add(str(client_id))

        source = (
            record.get("source")
            or record.get("source_title")
            or record.get("from_url")
        )

        if source:
            result["sources"][str(source)] += 1

        if attendance == 1:
            for service in record.get("services") or []:
                title = service.get("title") or "Без названия"

                try:
                    amount = int(service.get("amount", 1) or 1)
                except Exception:
                    amount = 1

                result["services"][title]["count"] += amount

                try:
                    result["services"][title]["revenue"] += float(
                        service.get(
                            "cost_to_pay",
                            service.get("cost", 0)
                        ) or 0
                    )
                except Exception:
                    pass

    result["clients_count"] = len(result["clients"])

    result["avg_check"] = (
        result["revenue"] / result["attended"]
        if result["attended"] else 0
    )

    return result


def get_period(start, end):
    return calculate(
        get_records(
            start.isoformat(),
            end.isoformat()
        )
    )


# -------------------- Отчёты --------------------

def report_today():
    date = datetime.now(TZ).date()

    current = get_period(date, date)
    previous = get_period(
        date - timedelta(days=1),
        date - timedelta(days=1)
    )

    return (
        f"📊 СЕГОДНЯ\n"
        f"🏪 {BRANCH_NAME}\n\n"
        f"💰 Выручка: {money(current['revenue'])}\n"
        f"🛍 Выручка по товарам: {money(current.get('product_revenue', 0))}\n"
        f"👥 Клиенты: {number(current['clients_count'])}\n"
        f"🧾 Средний чек: {money(current['avg_check'])}\n"
        f"📅 Записи: {number(current['records'])}\n"
        f"✅ Состоявшиеся: {number(current['attended'])}\n"
        f"❌ Не пришел: {number(current['noshow'])}\n"
        f"🚫 Отмены: {number(current['cancelled'])}"
    )


def report_masters():
    date = datetime.now(TZ).date()
    metrics = get_period(
        date - timedelta(days=29),
        date
    )

    ranking = []

    for master in metrics["staff"].values():
        score = (
            master["revenue"]
            + master["attended"] * 500
            - master["noshow"] * 300
        )
        ranking.append((score, master))

    ranking.sort(key=lambda x: x[0], reverse=True)

    lines = [
        f"👨‍💼 МАСТЕРА — 30 ДНЕЙ",
        f"🏪 {BRANCH_NAME}",
        ""
    ]

    if not ranking:
        return "\n".join(lines + ["Нет данных по мастерам."])

    for index, (_, master) in enumerate(ranking, 1):
        lines.append(
            f"{index}. {master['name']}\n"
            f"   💰 {money(master['revenue'])} | "
            f"👥 {len(master['clients'])} | "
            f"✅ {master['attended']} | "
            f"❌ {master['noshow']}"
        )

    return "\n".join(lines)


def report_dynamics():
    date = datetime.now(TZ).date()

    current_week = get_period(
        date - timedelta(days=6),
        date
    )

    previous_week = get_period(
        date - timedelta(days=13),
        date - timedelta(days=7)
    )

    current_month = get_period(
        date.replace(day=1),
        date
    )

    previous_month_end = date.replace(day=1) - timedelta(days=1)
    previous_month = get_period(
        previous_month_end.replace(day=1),
        previous_month_end
    )

    def metric_line(label, current, previous, formatter=number):
        return (
            f"{label}:\n"
            f"  текущий период — {formatter(current)}\n"
            f"  предыдущий период — {formatter(previous)}"
        )

    return (
        f"📈 ДИНАМИКА\n"
        f"🏪 {BRANCH_NAME}\n\n"
        f"7 дней: текущий период vs предыдущие 7 дней\n"
        f"{metric_line('💰 Выручка', current_week['revenue'], previous_week['revenue'], money)}\n"
        f"{metric_line('👥 Клиенты', current_week['clients_count'], previous_week['clients_count'])}\n"
        f"{metric_line('🧾 Средний чек', current_week['avg_check'], previous_week['avg_check'], money)}\n\n"
        f"Месяц: текущий месяц vs прошлый месяц\n"
        f"{metric_line('💰 Выручка', current_month['revenue'], previous_month['revenue'], money)}\n"
        f"{metric_line('👥 Клиенты', current_month['clients_count'], previous_month['clients_count'])}\n"
        f"{metric_line('🧾 Средний чек', current_month['avg_check'], previous_month['avg_check'], money)}"
    )


def report_analysis():
    date = datetime.now(TZ).date()

    current = get_period(date, date)
    previous = get_period(
        date - timedelta(days=1),
        date - timedelta(days=1)
    )

    observations = []

    if current["revenue"] > previous["revenue"]:
        observations.append("🟢 Выручка выше вчера.")
    elif current["revenue"] < previous["revenue"]:
        observations.append("🔴 Выручка ниже вчера.")

    if current["avg_check"] < previous["avg_check"]:
        observations.append("⚠️ Средний чек снизился.")

    if current["noshow"] > previous["noshow"]:
        observations.append("🔴 Не пришли больше, чем вчера.")

    if current["attended"] > previous["attended"]:
        observations.append("🟢 Состоявшихся визитов больше.")

    if not observations:
        observations.append(
            "⚪ Существенных изменений относительно вчера не обнаружено."
        )

    return (
        f"🧠 АНАЛИЗ ФИЛИАЛА\n"
        f"🏪 {BRANCH_NAME}\n\n"
        f"Сегодня: {money(current['revenue'])}, "
        f"{current['attended']} визитов, "
        f"средний чек {money(current['avg_check'])}.\n"
        f"Вчера: {money(previous['revenue'])}, "
        f"{previous['attended']} визитов, "
        f"средний чек {money(previous['avg_check'])}.\n\n"
        + "\n".join(observations)
        + "\n\n"
        "💡 Для поиска причины используй "
        "«Мастера», «Услуги», «Клиенты» и «Динамика»."
    )


def report_clients():
    client_list = get_clients()
    today = datetime.now(TZ).date()
    month_start = today.replace(day=1)

    total = len(client_list)

    # Потерянные и уходящие считаем только по текущему календарному месяцу.
    leaving = []
    lost = []
    for client in client_list:
        last_visit = client.get("last_visit")
        if not last_visit:
            continue
        try:
            last_date = datetime.fromisoformat(str(last_visit).replace("Z", "+00:00")).date()
        except Exception:
            continue
        if last_date < month_start:
            lost.append(client)
        elif last_date < today - timedelta(days=21):
            leaving.append(client)

    return (
        f"👥 КЛИЕНТЫ\n"
        f"🏪 {BRANCH_NAME}\n\n"
        f"Всего в базе: {number(total)}\n"
        f"🟡 Уходящие за текущий месяц: {number(len(leaving))}\n"
        f"🔴 Потерянные за текущий месяц: {number(len(lost))}"
    )

def report_return():
    client_list = get_clients()

    today = datetime.now(TZ).date()

    leaving = []
    lost = []

    for client in client_list:
        if not int(client.get("visits", 0) or 0):
            continue

        last_visit = (
            parse_date(client.get("last_change_date"))
            or parse_date(client.get("last_visit_date"))
        )

        if not last_visit:
            continue

        days = (today - last_visit.date()).days

        if 45 <= days < 90:
            leaving.append(client)
        elif days >= 90:
            lost.append(client)

    lines = [
        f"🔄 КОГО НУЖНО ВЕРНУТЬ",
        f"🏪 {BRANCH_NAME}",
        "",
        f"🟡 Уходящие: {len(leaving)}",
        f"🔴 Потерянные: {len(lost)}",
        ""
    ]

    for client in (leaving + lost)[:40]:
        name = (
            client.get("display_name")
            or client.get("name")
            or "Клиент"
        )
        phone = client.get("phone") or ""
        lines.append(f"• {name} {phone}")

    return "\n".join(lines)


def report_finances():
    date = datetime.now(TZ).date()

    metrics = get_period(
        date.replace(day=1),
        date
    )

    return (
        f"💰 ФИНАНСЫ\n"
        f"🏪 {BRANCH_NAME}\n\n"
        f"Выручка: {money(metrics['revenue'])}\n"
        f"Состоявшиеся визиты: {metrics['attended']}\n"
        f"Средний чек: {money(metrics['avg_check'])}\n"
        f"Записи: {metrics['records']}\n"
        f"Не пришел: {metrics['noshow']}\n"
        f"Отмены: {metrics['cancelled']}"
    )


def report_services():
    date = datetime.now(TZ).date()

    metrics = get_period(
        date - timedelta(days=29),
        date
    )

    services = sorted(
        metrics["services"].items(),
        key=lambda item: item[1]["revenue"],
        reverse=True
    )

    lines = [
        f"💈 УСЛУГИ — 30 ДНЕЙ",
        f"🏪 {BRANCH_NAME}",
        ""
    ]

    if not services:
        return "\n".join(lines + ["Нет данных по услугам."])

    for index, (name, service) in enumerate(services[:20], 1):
        lines.append(
            f"{index}. {name} — "
            f"{service['count']} | "
            f"{money(service['revenue'])}"
        )

    return "\n".join(lines)


def report_marketing():
    date = datetime.now(TZ).date()

    metrics = get_period(
        date - timedelta(days=29),
        date
    )

    sources = sorted(
        metrics["sources"].items(),
        key=lambda item: item[1],
        reverse=True
    )

    lines = [
        f"📣 МАРКЕТИНГ — 30 ДНЕЙ",
        f"🏪 {BRANCH_NAME}",
        ""
    ]

    if not sources:
        lines.append(
            "Источники не заполнены в доступных данных YCLIENTS."
        )
        return "\n".join(lines)

    for source, count in sources:
        lines.append(f"• {source}: {count}")

    return "\n".join(lines)


def report_plan():
    return (
        f"🎯 ПЛАН / ФАКТ\n"
        f"🏪 {BRANCH_NAME}\n\n"
        "План филиала пока не задан.\n\n"
        "После задания месячного плана бот будет считать:\n"
        "• факт;\n"
        "• % выполнения;\n"
        "• остаток;\n"
        "• необходимую дневную выручку."
    )


def ask_groq(question):
    """Ответ аналитика Groq. Цифры не выдумываются: в контекст передаются только доступные данные."""
    if not GROQ_API_KEY:
        return "⚠️ GROQ_API_KEY не задан в переменных окружения."

    try:
        from groq import Groq
        client = Groq(api_key=GROQ_API_KEY)

        today = datetime.now(TZ).date()
        metrics = get_period(today.replace(day=1), today)
        context = (
            f"Филиал: {BRANCH_NAME}\n"
            f"Период: {today.replace(day=1)} — {today}\n"
            f"Выручка: {metrics.get('revenue', 0)}\n"
            f"Клиенты: {metrics.get('clients_count', 0)}\n"
            f"Средний чек: {metrics.get('avg_check', 0)}\n"
            f"Записи: {metrics.get('records', 0)}\n"
            f"Состоявшиеся: {metrics.get('attended', 0)}\n"
            f"Не пришел: {metrics.get('noshow', 0)}\n"
            f"Отмены: {metrics.get('cancelled', 0)}\n"
        )

        response = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ты аналитик управляющего барбершопа МЕТРО. "
                        "Отвечай по существу, используй только переданные данные. "
                        "Не придумывай отсутствующие цифры. Если данных недостаточно, "
                        "прямо скажи об этом и дай полезный способ проверить вопрос."
                    )
                },
                {
                    "role": "user",
                    "content": f"Данные YCLIENTS:\n{context}\n\nВопрос управляющего: {question}"
                }
            ],
            temperature=0.2,
        )
        return response.choices[0].message.content.strip()
    except Exception as exc:
        print("Groq error:", repr(exc))
        return "⚠️ Не удалось получить ответ аналитика. Проверь GROQ_API_KEY и подключение Groq."

# -------------------- Меню --------------------

def main_menu():
    return None


def instruction():
    return (
        "ℹ️ ИНСТРУКЦИЯ\n\n"
        "🤖 МЕТРО — ANALYTICS\n"
        f"🏪 {BRANCH_NAME}\n\n"
        "Кнопки запускают готовые отчёты.\n\n"
        "Можно также писать запрос обычным языком:\n\n"
        "• Почему упала выручка?\n"
        "• Кто из мастеров хуже за последние 2 недели?\n"
        "• Почему у мастера низкая загрузка?\n"
        "• Сравни текущий месяц с прошлым.\n\n"
        "Команды:\n"
        "/сегодня\n"
        "/мастера\n"
        "/динамика\n"
        "/анализ\n"
        "/клиенты\n"
        "/услуги\n"
        "/маркетинг\n"
        "/план\n"
        "/утро\n"
        "/вечер"
    )


# -------------------- Обработка запросов --------------------

def route_message(text):
    text = (text or "").lower().strip()

    if text in ("/start", "start"):
        return (
            "🤖 МЕТРО — ANALYTICS\n\n"
            f"🏪 {BRANCH_NAME}\n\n"
            "Выбирай нужный отчёт кнопками ниже "
            "или напиши вопрос обычным языком.",
            main_menu()
        )

    if text in ("/menu", "меню", "главное меню"):
        return (
            "🤖 Главное меню\n\nВыбирай нужный раздел:",
            main_menu()
        )

    if text in ("/help", "инструкция", "ℹ️ инструкция"):
        return instruction(), None

    if text in ("/сегодня", "сегодня", "📊 сегодня"):
        return report_today(), None

    if text in ("/мастера", "мастера", "👨‍💼 мастера"):
        return report_masters(), None

    if text in ("/динамика", "динамика", "📈 динамика"):
        return report_dynamics(), None

    if text in ("/анализ", "анализ", "🧠 анализ"):
        return report_analysis(), None

    if text in ("/клиенты", "клиенты", "👥 клиенты"):
        return report_clients(), None

    if text in ("/услуги", "услуги", "💈 услуги"):
        return report_services(), None

    if text in ("/маркетинг", "маркетинг", "📣 маркетинг"):
        return report_marketing(), None

    if text in ("/план", "план / факт", "🎯 план / факт"):
        return report_plan(), None

    if text in ("/утро", "утро", "☀️ утро"):
        return "☀️ УТРЕННИЙ ОТЧЁТ\n\n" + report_today(), None

    if text in ("/вечер", "вечер", "🌙 вечер"):
        return "🌙 ВЕЧЕРНИЙ ОТЧЁТ\n\n" + report_today(), None

    # Естественные запросы
    if (
        "мастер" in text
        or "рейтинг" in text
        or "кто лучше" in text
        or "кто хуже" in text
    ):
        return report_masters(), None

    if "почему" in text or "что происходит" in text:
        return report_analysis(), None

    if (
        "вернут" in text
        or "потерян" in text
        or "уходящ" in text
    ):
        return report_return(), None

    if "услуг" in text:
        return report_services(), None

    if "маркет" in text or "источник" in text:
        return report_marketing(), None

    if "сравни" in text or "динамик" in text:
        return report_dynamics(), None

    if "клиент" in text:
        return report_clients(), None

    return (
        "🤖 Я могу показать аналитику МЕТРО.\n\n"
        "Например:\n"
        "«Почему упала выручка?»\n"
        "«Кто из мастеров хуже за 2 недели?»\n"
        "«Кого нужно вернуть?»\n"
        "«Сравни месяц с прошлым»",
        None
    )


# -------------------- Telegram webhook --------------------

@app.post("/telegram/webhook")
def telegram_webhook():
    update = request.get_json(silent=True) or {}

    message = (
        update.get("message")
        or update.get("edited_message")
        or {}
    )

    chat = message.get("chat") or {}
    chat_id = chat.get("id")

    remember_chat(chat_id)

    text = (message.get("text") or "").strip()

    if chat_id and text:
        try:
            answer, keyboard = route_message(text)
            tg_send(chat_id, answer, keyboard)
        except Exception as exc:
            print("BOT ERROR:", repr(exc))
            tg_send(
                chat_id,
                "❌ Не удалось сформировать отчёт.\n"
                f"Ошибка: {exc}",
                None
            )

    return jsonify({"ok": True})


# -------------------- YCLIENTS webhook --------------------

@app.route("/webhook", methods=["GET", "POST"])
def yclients_webhook():
    if request.method == "GET":
        return jsonify({
            "status": "ok",
            "service": "metro-analytics",
            "branch": BRANCH_NAME
        })

    payload = request.get_json(silent=True) or {}

    con = sqlite3.connect(DB)
    con.execute(
        "INSERT INTO webhook_events(received_at, payload) VALUES(?, ?)",
        (
            datetime.now(TZ).isoformat(),
            str(payload)
        )
    )
    con.commit()
    con.close()

    return jsonify({"ok": True}), 200


# -------------------- Health --------------------

@app.get("/")
def index():
    return jsonify({
        "service": "metro-analytics",
        "status": "online",
        "branch": BRANCH_NAME,
        "company_id": COMPANY_ID
    })


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "metro-analytics",
        "branch": BRANCH_NAME,
        "company_id": COMPANY_ID,
        "telegram": bool(TELEGRAM_BOT_TOKEN),
        "yclients_user_token": bool(YCLIENTS_USER_TOKEN),
        "chats": len(chats)
    })


# -------------------- Автоматические отчёты --------------------

def scheduler():
    morning_sent = None
    evening_sent = None

    while True:
        try:
            now = datetime.now(TZ)
            day_key = now.strftime("%Y-%m-%d")

            if now.hour == 9 and now.minute < 5:
                if morning_sent != day_key:
                    morning_sent = day_key
                    broadcast(
                        "☀️ ДОБРОЕ УТРО\n\n" + report_today()
                    )

            if now.hour == 21 and now.minute < 5:
                if evening_sent != day_key:
                    evening_sent = day_key
                    broadcast(
                        "🌙 ВЕЧЕРНИЙ ОТЧЁТ\n\n" + report_today()
                    )

        except Exception as exc:
            print("Scheduler error:", repr(exc))

        time.sleep(60)


# -------------------- Запуск --------------------

init_db()

def configure_telegram():
    if not TG or not PUBLIC_URL:
        print("Telegram webhook not configured: missing token or PUBLIC_URL")
        return

    try:
        webhook_url = f"{PUBLIC_URL}/telegram/webhook"

        response = requests.post(
            f"{TG}/setWebhook",
            json={
                "url": webhook_url,
                "drop_pending_updates": False
            },
            timeout=20
        )

        print(
            "Telegram webhook:",
            response.status_code,
            response.text[:500]
        )

        # Меню Telegram открывается только по кнопке в поле ввода.
        # Reply-клавиатура с отчётами намеренно не используется.
        commands = [
            {"command": "сегодня", "description": "Отчёт за сегодня"},
            {"command": "мастера", "description": "Рейтинг мастеров"},
            {"command": "динамика", "description": "Динамика"},
            {"command": "анализ", "description": "Анализ"},
            {"command": "клиенты", "description": "Клиенты"},
            {"command": "услуги", "description": "Услуги"},
            {"command": "маркетинг", "description": "Маркетинг"},
            {"command": "план", "description": "План / факт"},
            {"command": "утро", "description": "Утренний отчёт"},
            {"command": "вечер", "description": "Вечерний отчёт"},
            {"command": "help", "description": "Инструкция"},
        ]

        commands_response = requests.post(
            f"{TG}/setMyCommands",
            json={"commands": commands},
            timeout=20
        )

        print(
            "Telegram commands:",
            commands_response.status_code,
            commands_response.text[:500]
        )

        menu_response = requests.post(
            f"{TG}/setChatMenuButton",
            json={
                "menu_button": {
                    "type": "commands"
                }
            },
            timeout=20
        )

        print(
            "Telegram menu button:",
            menu_response.status_code,
            menu_response.text[:500]
        )

    except Exception as exc:
        print("Telegram setup error:", repr(exc))


configure_telegram()
threading.Thread(
    target=scheduler,
    daemon=True
).start()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(
        host="0.0.0.0",
        port=port
    )
