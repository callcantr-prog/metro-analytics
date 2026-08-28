
def ask_groq(question):
    if not GROQ_API_KEY:
        return "⚠️ Не задан GROQ_API_KEY."
    try:
        from groq import Groq
        client = Groq(api_key=GROQ_API_KEY)
        response = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ты аналитик управляющего барбершопа МЕТРО. "
                        "Отвечай на вопрос по данным, которые передал бот. "
                        "Не придумывай цифры и прямо указывай, если данных недостаточно."
                    )
                },
                {"role": "user", "content": question}
            ],
            temperature=0.2,
        )
        return response.choices[0].message.content.strip()
    except Exception as exc:
        print("Groq error:", repr(exc))
        return "⚠️ Не удалось получить ответ ИИ. Проверь GROQ_API_KEY и подключение Groq."
import calendar
import json
import logging
import os
import re
import threading
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("metro-analytics")

# ============================================================
# МЕТРО — Аналитик
# Филиал: МЕТРО — Транссибирская 1
# Администраторы НЕ используются.
#
# Render ENV (4 переменные):
# PUBLIC_URL
# TELEGRAM_BOT_TOKEN
# YCLIENTS_COMPANY_ID
# YCLIENTS_USER_TOKEN
#
# Partner token используется внутри приложения для авторизации
# YCLIENTS API. Пользовательский токен берётся из ENV.
# ============================================================

PUBLIC_URL = (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
YCLIENTS_COMPANY_ID = os.getenv("YCLIENTS_COMPANY_ID", "1687248").strip()
YCLIENTS_USER_TOKEN = os.getenv("YCLIENTS_USER_TOKEN", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()

# Партнёрский токен приложения YCLIENTS.
# Не выводится в Telegram и не показывается пользователю.
YCLIENTS_PARTNER_TOKEN = "CJqq523ZxP1vue71jnBd"

try:
    COMPANY_ID = int(YCLIENTS_COMPANY_ID)
except Exception:
    COMPANY_ID = 1687248

BRANCH_NAME = "МЕТРО — Транссибирская 1"
TZ = ZoneInfo("Asia/Omsk")

TG = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else ""
YC = "https://api.yclients.com/api/v1"

# Telegram
_known_chats = set()
_lock = threading.Lock()

# План хранится в SQLite, чтобы не пропадал при перезапуске Render.
DB_PATH = "/tmp/metro_analytics.sqlite3"

# Состояния диалогов
_plan_input = set()
_ai_input = set()
_dynamic = {}


# ============================================================
# DATABASE
# ============================================================

def db():
    import sqlite3
    return sqlite3.connect(DB_PATH)


def init_db():
    con = db()
    con.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            chat_id TEXT PRIMARY KEY,
            added_at TEXT NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS monthly_plans (
            month TEXT PRIMARY KEY,
            amount REAL NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    con.commit()

    for row in con.execute("SELECT chat_id FROM chats"):
        _known_chats.add(str(row[0]))

    con.close()


def remember_chat(chat_id):
    if not chat_id:
        return
    cid = str(chat_id)
    with _lock:
        _known_chats.add(cid)
    con = db()
    con.execute(
        "INSERT OR IGNORE INTO chats(chat_id, added_at) VALUES(?, ?)",
        (cid, datetime.now(TZ).isoformat()),
    )
    con.commit()
    con.close()


# ============================================================
# HELPERS
# ============================================================

def money(v):
    try:
        return f"{float(v):,.0f} ₽".replace(",", " ")
    except Exception:
        return "0 ₽"


def number(v):
    try:
        return f"{float(v):,.0f}".replace(",", " ")
    except Exception:
        return "0"


def percent(v):
    try:
        return f"{float(v):.1f}%"
    except Exception:
        return "0.0%"


def pct_change(current, previous):
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


def month_start(d):
    return d.replace(day=1)


def month_end(d):
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def month_name(d):
    names = [
        "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
        "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
    ]
    return f"{names[d.month - 1]} {d.year}"


def period_label(start, end):
    return f"{start:%d.%m.%Y} — {end:%d.%m.%Y}"


def current_month():
    d = datetime.now(TZ).date()
    return month_start(d), month_end(d)


def parse_date(value):
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(text)
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


def previous_month_same_days(start, end):
    anchor = start.replace(day=1) - timedelta(days=1)
    last = calendar.monthrange(anchor.year, anchor.month)[1]
    ps = min(start.day, last)
    pe = min(end.day, last)
    return date(anchor.year, anchor.month, ps), date(anchor.year, anchor.month, pe)


# ============================================================
# TELEGRAM API
# ============================================================

def tg_call(method, payload=None):
    if not TG:
        return None
    try:
        r = requests.post(f"{TG}/{method}", json=payload or {}, timeout=30)
        if not r.ok:
            log.error("Telegram %s: %s", method, r.text[:800])
        return r
    except requests.RequestException as exc:
        log.error("Telegram error: %s", exc)
        return None


def tg_send(chat_id, text, markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if markup is not None:
        payload["reply_markup"] = markup
    return tg_call("sendMessage", payload)


def tg_edit(chat_id, message_id, text, markup=None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
    }
    if markup is not None:
        payload["reply_markup"] = markup
    return tg_call("editMessageText", payload)


def tg_answer(callback_id):
    tg_call("answerCallbackQuery", {"callback_query_id": callback_id})


def remove_keyboard():
    return {"remove_keyboard": True}


# Меню показывается только по /start, «Главное меню» или кнопке ☰.
def main_menu():
    return {
        "keyboard": [
            [{"text": "📊 Сегодня"}, {"text": "🧠 Анализ"}],
            [{"text": "👨‍💼 Мастера"}, {"text": "📈 Динамика"}],
            [{"text": "👥 Клиенты"}, {"text": "💈 Услуги"}],
            [{"text": "📣 Маркетинг"}, {"text": "🎯 План / факт"}],
            [{"text": "🤖 Спроси аналитика"}, {"text": "ℹ️ Инструкция"}],
        ],
    }


def menu_button():
    return None


def back_button():
    return None


# ============================================================
# YCLIENTS API
# ============================================================

def yclients_headers():
    return {
        "Authorization": f"Bearer {YCLIENTS_PARTNER_TOKEN}, User {YCLIENTS_USER_TOKEN}",
        "Accept": "application/vnd.yclients.v2+json",
        "Content-Type": "application/json",
    }


def yclients_get(path, params=None):
    if not YCLIENTS_USER_TOKEN:
        raise RuntimeError("YCLIENTS_USER_TOKEN не задан в Render.")

    url = f"{YC}/{path.lstrip('/')}"
    response = requests.get(
        url,
        headers=yclients_headers(),
        params=params or {},
        timeout=30,
    )

    try:
        body = response.json()
    except Exception:
        body = {}

    if not response.ok:
        meta = body.get("meta") if isinstance(body, dict) else {}
        msg = ""
        if isinstance(meta, dict):
            msg = str(meta.get("message") or "")
        msg = msg or str(body.get("message") if isinstance(body, dict) else "")
        raise RuntimeError(
            f"YCLIENTS {response.status_code}: {msg or response.text[:500]}"
        )

    if isinstance(body, dict) and "data" in body:
        return body["data"]
    return body


def get_records(start, end):
    result = []
    for page in range(1, 101):
        data = yclients_get(
            f"records/{COMPANY_ID}",
            {
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "page": page,
                "count": 200,
            },
        )

        if isinstance(data, dict):
            rows = data.get("records") or data.get("data") or []
        else:
            rows = data or []

        if not isinstance(rows, list):
            break

        result.extend(x for x in rows if isinstance(x, dict))

        if len(rows) < 200:
            break

    return result


def get_clients():
    result = []
    for page in range(1, 101):
        data = yclients_get(
            f"clients/{COMPANY_ID}",
            {"page": page, "count": 100},
        )

        if isinstance(data, dict):
            rows = data.get("clients") or data.get("data") or []
        else:
            rows = data or []

        if not isinstance(rows, list):
            break

        result.extend(x for x in rows if isinstance(x, dict))

        if len(rows) < 100:
            break

    return result


# ============================================================
# YCLIENTS DATA PARSING
# ============================================================

def attendance(record):
    value = record.get("visit_attendance")
    if value is None:
        value = record.get("attendance")

    try:
        return int(value)
    except Exception:
        if value in (True, "true", "True"):
            return 1
        return 0


def attended(record):
    return not bool(record.get("deleted")) and attendance(record) == 1


def noshow(record):
    if record.get("deleted"):
        return False
    return attendance(record) in (-1, 2)


def cancelled(record):
    return bool(record.get("deleted"))


def client(record):
    value = record.get("client") or {}
    if isinstance(value, list):
        return value[0] if value else {}
    return value if isinstance(value, dict) else {}


def staff(record):
    value = record.get("staff") or record.get("master") or {}
    if isinstance(value, list):
        return value[0] if value else {}
    return value if isinstance(value, dict) else {}


def revenue(record):
    total = 0.0

    for service in record.get("services") or []:
        if not isinstance(service, dict):
            continue
        cost = (
            service.get("cost_to_pay")
            if service.get("cost_to_pay") is not None
            else service.get("cost")
        )
        if cost is None:
            cost = service.get("manual_cost")
        if cost is None:
            cost = service.get("first_cost")

        try:
            amount = float(service.get("amount") or 1)
            total += float(cost or 0) * amount
        except Exception:
            pass

    for good in record.get("goods_transactions") or []:
        if not isinstance(good, dict):
            continue
        try:
            amount = float(good.get("amount") or 1)
            cost = (
                good.get("cost")
                if good.get("cost") is not None
                else good.get("sum")
            )
            if cost is None:
                cost = good.get("price")
            total += float(cost or 0) * amount
        except Exception:
            pass

    if total == 0:
        for key in ("cost_to_pay", "cost", "sum", "paid_amount"):
            try:
                value = float(record.get(key) or 0)
                if value:
                    return value
            except Exception:
                pass

    return total


def client_key(record):
    c = client(record)
    for key in ("id", "phone", "name"):
        if c.get(key) is not None:
            return str(c[key])
    return None


def master_name(record):
    s = staff(record)
    return str(s.get("name") or "Без мастера")


def calculate(records):
    result = {
        "records": len(records),
        "attended": 0,
        "noshow": 0,
        "cancelled": 0,
        "revenue": 0.0,
        "clients": set(),
        "new": set(),
        "repeat": set(),
        "masters": defaultdict(lambda: {
            "name": "",
            "records": 0,
            "attended": 0,
            "noshow": 0,
            "cancelled": 0,
            "revenue": 0.0,
            "clients": set(),
        }),
        "services": defaultdict(lambda: {"count": 0, "revenue": 0.0}),
        "sources": defaultdict(int),
    }

    for record in records:
        ok = attended(record)
        ns = noshow(record)
        cn = cancelled(record)

        if ok:
            result["attended"] += 1
            result["revenue"] += revenue(record)
        if ns:
            result["noshow"] += 1
        if cn:
            result["cancelled"] += 1

        cid = client_key(record)
        if cid and ok:
            result["clients"].add(cid)

            c = client(record)
            try:
                visits_count = int(c.get("success_visits_count") or 0)
            except Exception:
                visits_count = 0

            if visits_count <= 1:
                result["new"].add(cid)
            else:
                result["repeat"].add(cid)

        mn = master_name(record)
        m = result["masters"][mn]
        m["name"] = mn
        m["records"] += 1
        m["revenue"] += revenue(record) if ok else 0
        if ok:
            m["attended"] += 1
        if ns:
            m["noshow"] += 1
        if cn:
            m["cancelled"] += 1
        if cid and ok:
            m["clients"].add(cid)

        if ok:
            for service in record.get("services") or []:
                if not isinstance(service, dict):
                    continue
                title = str(service.get("title") or "Без названия")
                try:
                    amount = int(float(service.get("amount") or 1))
                except Exception:
                    amount = 1

                cost = service.get("cost_to_pay")
                if cost is None:
                    cost = service.get("cost")
                if cost is None:
                    cost = service.get("manual_cost")
                if cost is None:
                    cost = service.get("first_cost")

                try:
                    cost_value = float(cost or 0)
                except Exception:
                    cost_value = 0

                result["services"][title]["count"] += amount
                result["services"][title]["revenue"] += cost_value * amount

        source = (
            record.get("source")
            or record.get("source_title")
            or record.get("from_url")
        )
        if source:
            result["sources"][str(source)] += 1

    result["clients_count"] = len(result["clients"])
    result["new_count"] = len(result["new"])
    result["repeat_count"] = len(result["repeat"])
    result["avg"] = (
        result["revenue"] / result["attended"]
        if result["attended"] else 0
    )
    return result


# ============================================================
# REPORTS
# ============================================================

def get_period(start, end):
    return calculate(get_records(start, end))


def report_today():
    today = datetime.now(TZ).date()
    current = get_period(today, today)
    previous = get_period(today - timedelta(days=1), today - timedelta(days=1))

    return (
        "📊 СЕГОДНЯ\n"
        f"🏪 {BRANCH_NAME}\n\n"
        f"💰 Выручка: {money(current['revenue'])}\n"
        f"👥 Клиенты: {number(current['clients_count'])}\n"
        f"🧾 Средний чек: {money(current['avg'])}\n"
        f"📅 Записи: {number(current['records'])}\n"
        f"✅ Состоявшиеся: {number(current['attended'])}\n"
        f"❌ Не пришел: {number(current['noshow'])}\n"
        f"🚫 Отмены: {number(current['cancelled'])}\n\n"
    )


def report_analysis():
    start, end = current_month()
    current = get_period(start, end)

    if start.month == 1:
        previous_start = date(start.year - 1, 12, 1)
    else:
        previous_start = date(start.year, start.month - 1, 1)
    previous_end = month_end(previous_start)
    previous = get_period(previous_start, previous_end)

    observations = []

    if current["revenue"] > previous["revenue"]:
        observations.append("🟢 Выручка выше прошлого месяца.")
    elif current["revenue"] < previous["revenue"]:
        observations.append("🔴 Выручка ниже прошлого месяца.")

    if current["avg"] < previous["avg"]:
        observations.append("⚠️ Средний чек снизился.")
    elif current["avg"] > previous["avg"]:
        observations.append("🟢 Средний чек вырос.")

    if current["attended"] > previous["attended"]:
        observations.append("🟢 Состоявшихся визитов больше.")
    elif current["attended"] < previous["attended"]:
        observations.append("🔴 Состоявшихся визитов меньше.")

    if current["noshow"] > previous["noshow"]:
        observations.append("🔴 не пришли стало больше.")

    if not observations:
        observations.append("⚪ Существенных изменений не обнаружено.")

    return (
        "🧠 АНАЛИЗ ФИЛИАЛА\n"
        f"🏪 {BRANCH_NAME}\n"
        f"🗓 {period_label(start, end)}\n"
        f"📅 {month_name(start)}\n\n"
        f"💰 Выручка: {money(current['revenue'])}\n"
        f"👥 Клиенты: {number(current['clients_count'])}\n"
        f"✅ Визиты: {number(current['attended'])}\n"
        f"🧾 Средний чек: {money(current['avg'])}\n\n"
        + "\n".join(observations)
    )


def report_masters():
    start, end = current_month()
    m = get_period(start, end)

    rows = []
    for master in m["masters"].values():
        if master["name"] == "Без мастера":
            continue
        score = (
            master["revenue"]
            + master["attended"] * 500
            - master["noshow"] * 300
        )
        rows.append((score, master))

    rows.sort(key=lambda x: x[0], reverse=True)

    lines = [
        "👨‍💼 МАСТЕРА — МЕСЯЦ",
        f"🏪 {BRANCH_NAME}",
        f"🗓 {period_label(start, end)}",
        "",
    ]

    if not rows:
        return "\n".join(lines + ["Нет данных по мастерам за этот месяц."])

    for i, (_, master) in enumerate(rows, 1):
        lines.append(
            f"{i}. {master['name']}\n"
            f"💰 {money(master['revenue'])} | "
            f"👥 {len(master['clients'])} | "
            f"✅ {master['attended']} | "
            f"❌ {master['noshow']}"
        )

    return "\n".join(lines)


def report_master_question(question=""):
    start, end = current_month()
    m = get_period(start, end)

    masters = [
        x for x in m["masters"].values()
        if x["name"] != "Без мастера"
    ]

    if not masters:
        return (
            "🧠 АНАЛИЗ ЗАГРУЗКИ МАСТЕРОВ\n"
            f"🏪 {BRANCH_NAME}\n"
            f"🗓 {period_label(start, end)}\n\n"
            "Нет данных по мастерам."
        )

    avg_visits = sum(x["attended"] for x in masters) / len(masters)
    avg_records = sum(x["records"] for x in masters) / len(masters)
    avg_revenue = sum(x["revenue"] for x in masters) / len(masters)

    q = question.lower()
    selected = [
        x for x in masters
        if x["name"].lower() in q
    ]

    def one(x):
        reasons = []
        if x["attended"] < avg_visits:
            reasons.append(
                f"визитов меньше среднего ({x['attended']} против {avg_visits:.1f})"
            )
        if x["records"] < avg_records:
            reasons.append(
                f"записей меньше среднего ({x['records']} против {avg_records:.1f})"
            )
        if x["revenue"] < avg_revenue:
            reasons.append(
                f"выручка ниже среднего ({money(x['revenue'])} против {money(avg_revenue)})"
            )
        if x["noshow"]:
            reasons.append(f"не пришли: {x['noshow']}")
        if not reasons:
            reasons.append("явной причины по доступным данным не видно")

        conversion = (
            x["attended"] / x["records"] * 100
            if x["records"] else 0
        )

        return (
            f"💈 {x['name']}\n"
            f"💰 {money(x['revenue'])}\n"
            f"📅 Записи: {x['records']}\n"
            f"✅ Визиты: {x['attended']}\n"
            f"❌ Не пришел: {x['noshow']}\n"
            f"📊 Состоялись: {percent(conversion)}\n"
            f"⚠️ Причины: {'; '.join(reasons)}"
        )

    if selected:
        return (
            "🧠 ПОЧЕМУ НИЗКАЯ ЗАГРУЗКА\n"
            f"🏪 {BRANCH_NAME}\n"
            f"🗓 {period_label(start, end)}\n\n"
            + one(selected[0])
        )

    masters.sort(key=lambda x: (x["attended"], x["revenue"]))
    lines = [
        "🧠 ПОЧЕМУ У МАСТЕРОВ НИЗКАЯ ЗАГРУЗКА",
        f"🏪 {BRANCH_NAME}",
        f"🗓 {period_label(start, end)}",
        "",
    ]

    for x in masters:
        lines.append(one(x))
        lines.append("")

    return "\n".join(lines)


def report_clients():
    clients = get_clients()
    today = datetime.now(TZ).date()
    active = []
    leaving = []
    lost = []

    for c in clients:
        try:
            visits = int(c.get("visits") or 0)
        except Exception:
            visits = 0

        if visits > 0:
            active.append(c)

        last = (
            parse_date(c.get("last_visit_date"))
            or parse_date(c.get("last_change_date"))
        )

        if visits > 0 and last:
            days = (today - last.date()).days
            if days >= 90:
                lost.append(c)
            elif days >= 45:
                leaving.append(c)

    return (
        "👥 КЛИЕНТЫ\n"
        f"🏪 {BRANCH_NAME}\n\n"
        f"Всего в базе: {len(clients)}\n"
        f"С историей визитов: {len(active)}\n"
        f"🟡 Уходящие: {len(leaving)}\n"
        f"🔴 Потерянные: {len(lost)}"
    )


def report_return():
    clients = get_clients()
    today = datetime.now(TZ).date()
    leaving = []
    lost = []

    for c in clients:
        try:
            visits = int(c.get("visits") or 0)
        except Exception:
            visits = 0

        if not visits:
            continue

        last = (
            parse_date(c.get("last_visit_date"))
            or parse_date(c.get("last_change_date"))
        )
        if not last:
            continue

        days = (today - last.date()).days

        if 45 <= days < 90:
            leaving.append(c)
        elif days >= 90:
            lost.append(c)

    lines = [
        "🔄 КОГО НУЖНО ВЕРНУТЬ",
        f"🏪 {BRANCH_NAME}",
        "",
        f"🟡 Уходящие: {len(leaving)}",
        f"🔴 Потерянные: {len(lost)}",
        "",
    ]

    for c in (leaving + lost)[:40]:
        name = c.get("display_name") or c.get("name") or "Клиент"
        phone = c.get("phone") or ""
        lines.append(f"• {name} {phone}".strip())

    return "\n".join(lines)


def report_finance():
    start, end = current_month()
    m = get_period(start, end)

    return (
        "💰 ФИНАНСЫ — МЕСЯЦ\n"
        f"🏪 {BRANCH_NAME}\n"
        f"🗓 {period_label(start, end)}\n\n"
        f"💰 Выручка: {money(m['revenue'])}\n"
        f"👥 Клиенты: {m['clients_count']}\n"
        f"✅ Визиты: {m['attended']}\n"
        f"🧾 Средний чек: {money(m['avg'])}\n"
        f"📅 Записи: {m['records']}\n"
        f"❌ Не пришел: {m['noshow']}\n"
        f"🚫 Отмены: {m['cancelled']}"
    )


def report_services():
    start, end = current_month()
    m = get_period(start, end)

    rows = sorted(
        m["services"].items(),
        key=lambda x: x[1]["revenue"],
        reverse=True,
    )

    lines = [
        "💈 УСЛУГИ — МЕСЯЦ",
        f"🏪 {BRANCH_NAME}",
        f"🗓 {period_label(start, end)}",
        "",
    ]

    if not rows:
        return "\n".join(lines + ["Нет данных по услугам."])

    for i, (name, s) in enumerate(rows[:20], 1):
        lines.append(
            f"{i}. {name} — {s['count']} | {money(s['revenue'])}"
        )

    return "\n".join(lines)


def report_marketing():
    start, end = current_month()
    m = get_period(start, end)

    rows = sorted(
        m["sources"].items(),
        key=lambda x: x[1],
        reverse=True,
    )

    lines = [
        "📣 МАРКЕТИНГ — МЕСЯЦ",
        f"🏪 {BRANCH_NAME}",
        f"🗓 {period_label(start, end)}",
        "",
    ]

    if not rows:
        return "\n".join(
            lines + ["Источники не заполнены в доступных данных YCLIENTS."]
        )

    for source, count in rows:
        lines.append(f"• {source}: {count}")

    return "\n".join(lines)


# ============================================================
# PLAN / FACT
# ============================================================

def get_plan(d=None):
    d = d or datetime.now(TZ).date()
    key = f"{d.year:04d}-{d.month:02d}"

    con = db()
    row = con.execute(
        "SELECT amount FROM monthly_plans WHERE month=?",
        (key,),
    ).fetchone()
    con.close()

    return float(row[0]) if row else None


def set_plan(d, amount):
    key = f"{d.year:04d}-{d.month:02d}"

    con = db()
    con.execute(
        """
        INSERT INTO monthly_plans(month, amount, updated_at)
        VALUES(?,?,?)
        ON CONFLICT(month)
        DO UPDATE SET amount=excluded.amount, updated_at=excluded.updated_at
        """,
        (key, float(amount), datetime.now(TZ).isoformat()),
    )
    con.commit()
    con.close()


def plan_menu():
    return {
        "inline_keyboard": [
            [{"text": "🎯 Задать план", "callback_data": "plan:set"}],
            [{"text": "✏️ Изменить план", "callback_data": "plan:edit"}],
            [{"text": "🗑 Сбросить план", "callback_data": "plan:clear"}],
            [{"text": "⬅️ Главное меню", "callback_data": "menu:open"}],
        ]
    }


def report_plan():
    today = datetime.now(TZ).date()
    start, end = month_start(today), month_end(today)
    plan = get_plan(today)
    fact = get_period(start, end)["revenue"]

    lines = [
        "🎯 ПЛАН / ФАКТ",
        f"🏪 {BRANCH_NAME}",
        f"🗓 {period_label(start, end)}",
        f"📅 {month_name(start)}",
        "",
    ]

    if plan is None:
        lines.extend([
            "План на этот месяц пока не задан.",
            "",
            "После задания месячного плана бот считает:",
            "• факт;",
            "• % выполнения;",
            "• остаток;",
            "• сколько нужно делать в день для выполнения плана.",
        ])
        return "\n".join(lines)

    fact = float(fact)
    remain = max(0.0, plan - fact)
    completion = fact / plan * 100 if plan else 0

    days_total = (end - start).days + 1
    remaining_days = max(1, (end - today).days + 1)
    elapsed_days = min(days_total, max(1, (today - start).days + 1))
    daily_needed = remain / remaining_days

    lines.extend([
        f"🎯 План: {money(plan)}",
        f"💰 Факт: {money(fact)}",
        f"📊 Выполнение: {percent(completion)}",
        f"📉 Осталось: {money(remain)}",
        f"📆 Прошло дней: {elapsed_days} из {days_total}",
        f"🔥 Нужно делать в день: {money(daily_needed)}",
    ])

    return "\n".join(lines)


# ============================================================
# DYNAMIC — ONLY PLACE WITH CALENDAR
# ============================================================

def dynamics_menu():
    return {
        "inline_keyboard": [
            [{"text": "📅 Выбрать период", "callback_data": "dyn:calendar"}],
            [{"text": "⬅️ Главное меню", "callback_data": "menu:open"}],
        ]
    }


def calendar_keyboard(year, month, mode, selected=None):
    names = [
        "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
        "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
    ]

    if month == 1:
        py, pm = year - 1, 12
    else:
        py, pm = year, month - 1

    if month == 12:
        ny, nm = year + 1, 1
    else:
        ny, nm = year, month + 1

    rows = [[
        {"text": "◀️", "callback_data": f"cal:move:{mode}:{py}:{pm}"},
        {"text": f"{names[month-1]} {year}", "callback_data": "cal:noop"},
        {"text": "▶️", "callback_data": f"cal:move:{mode}:{ny}:{nm}"},
    ]]

    rows.append([
        {"text": x, "callback_data": "cal:noop"}
        for x in ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    ])

    for week in calendar.monthcalendar(year, month):
        row = []
        for day in week:
            if day == 0:
                row.append({"text": " ", "callback_data": "cal:noop"})
            else:
                mark = ""
                if selected and selected.year == year and selected.month == month and selected.day == day:
                    mark = "🟢"
                row.append({
                    "text": f"{mark}{day}",
                    "callback_data": f"cal:day:{mode}:{year}:{month}:{day}",
                })
        rows.append(row)

    rows.append([{"text": "❌ Отмена", "callback_data": "menu:open"}])

    return {"inline_keyboard": rows}


def report_dynamics(start, end):
    current = get_period(start, end)
    previous_start, previous_end = previous_month_same_days(start, end)
    previous = get_period(previous_start, previous_end)

    return (
        "📈 ДИНАМИКА\n"
        f"🏪 {BRANCH_NAME}\n\n"
        f"📅 Текущий период: {period_label(start, end)}\n"
        f"↔️ Сравнение: {period_label(previous_start, previous_end)}\n\n"
        f"💰 Выручка: {money(current['revenue'])} → {pct_change(current['revenue'], previous['revenue'])}\n"
        f"👥 Клиенты: {number(current['clients_count'])} → {pct_change(current['clients_count'], previous['clients_count'])}\n"
        f"📅 Записи: {number(current['records'])} → {pct_change(current['records'], previous['records'])}\n"
        f"✅ Визиты: {number(current['attended'])} → {pct_change(current['attended'], previous['attended'])}\n"
        f"🧾 Средний чек: {money(current['avg'])} → {pct_change(current['avg'], previous['avg'])}\n"
        f"🆕 Новые: {number(current['new_count'])} → {pct_change(current['new_count'], previous['new_count'])}\n"
        f"🔄 Повторные: {number(current['repeat_count'])} → {pct_change(current['repeat_count'], previous['repeat_count'])}\n"
        f"❌ Не пришел: {number(current['noshow'])} → {pct_change(current['noshow'], previous['noshow'])}"
    )


# ============================================================
# INSTRUCTION
# ============================================================

def instruction():
    return (
        "ℹ️ ИНСТРУКЦИЯ\n\n"
        "🤖 МЕТРО — Аналитик\n"
        f"🏪 {BRANCH_NAME}\n\n"
        "📅 Основная аналитика считается по календарным месяцам: "
        "Январь, Февраль, Март и т.д.\n"
        "В отчёте всегда указаны даты месяца — с 01 числа по последнее число.\n\n"
        "📈 Только в «Динамике» есть выбор периода календарём.\n"
        "Например: 01.08.2026 — 07.08.2026 сравнивается с "
        "01.07.2026 — 07.07.2026.\n\n"
        "🎯 «План / факт» — только месячный план.\n"
        "Можно задать, изменить или сбросить план.\n"
        "Бот считает факт, % выполнения, остаток и нужную дневную выручку.\n\n"
        "🤖 «Спроси аналитика» понимает обычные вопросы."
    )


# ============================================================
# NATURAL LANGUAGE
# ============================================================

def handle_plan_input(chat_id, text):
    amount_text = re.sub(r"[^0-9.,]", "", text or "").replace(",", ".")
    try:
        amount = float(amount_text)
    except Exception:
        return "❌ Не понял сумму. Напиши, например: 1500000"

    if amount <= 0:
        return "❌ Сумма должна быть больше нуля."

    set_plan(datetime.now(TZ).date(), amount)
    _plan_input.discard(str(chat_id))

    return "✅ План сохранён на текущий месяц.\n\n" + report_plan()


def route_message(chat_id, text):
    text = (text or "").strip()
    low = text.lower()

    if str(chat_id) in _plan_input:
        return handle_plan_input(chat_id, text), plan_menu()

    if low.startswith("/start") or low in ("главное меню", "🏠 главное меню", "☰ главное меню"):
        remember_chat(chat_id)
        return (
            "🤖 МЕТРО — Аналитик\n\n"
            f"🏪 {BRANCH_NAME}\n\n"
            "Выберите нужный раздел.",
            main_menu(),
        )

    if low in ("📊 сегодня", "сегодня", "/сегодня"):
        return report_today(), menu_button()

    if low in ("🧠 анализ", "анализ", "/анализ"):
        return report_analysis(), menu_button()

    if low in ("👨‍💼 мастера", "мастера", "/мастера"):
        return report_masters(), menu_button()

    if low in ("👥 клиенты", "клиенты", "/клиенты"):
        return report_clients(), menu_button()

    if low in ("💈 услуги", "услуги", "/услуги"):
        return report_services(), menu_button()

    if low in ("📣 маркетинг", "маркетинг", "/маркетинг"):
        return report_marketing(), menu_button()

    if low in ("🎯 план / факт", "план / факт", "/план"):
        return report_plan(), plan_menu()

    if low in ("ℹ️ инструкция", "инструкция", "/help", "/инструкция"):
        return instruction(), menu_button()

    if low in ("🤖 спроси аналитика", "спроси аналитика"):
        _ai_input.add(str(chat_id))
        return (
            "🤖 СПРОСИ АНАЛИТИКА\n\n"
            "Напиши любой вопрос обычным текстом. Я передам его ИИ."
        ), None

    if str(chat_id) in _ai_input:
        _ai_input.discard(str(chat_id))
        return ask_groq(text), None

    if low in ("📈 динамика", "динамика", "/динамика"):
        today = datetime.now(TZ).date()
        start = today - timedelta(days=6)
        try:
            text_out = report_dynamics(start, today)
        except Exception as exc:
            text_out = (
                "📈 ДИНАМИКА\n\n"
                "Нажми «📅 Выбрать период», чтобы задать даты."
            )
        return text_out, dynamics_menu()

    # Natural language.
    if any(x in low for x in ("загрузк", "загружен", "свободн", "мало запис", "мало визит")):
        return report_master_question(text), menu_button()

    if "мастер" in low or "рейтинг" in low or "кто лучше" in low or "кто хуже" in low:
        return report_masters(), menu_button()

    if "услуг" in low:
        return report_services(), menu_button()

    if "маркет" in low or "источник" in low:
        return report_marketing(), menu_button()

    if "динамик" in low or "сравни" in low:
        today = datetime.now(TZ).date()
        start = today - timedelta(days=6)
        return report_dynamics(start, today), dynamics_menu()

    if "почему" in low or "что происходит" in low or "проблем" in low:
        return report_analysis(), menu_button()

    if "клиент" in low:
        return report_clients(), menu_button()

    return (
        "🤖 Не понял запрос.\n\n"
        "Откройте ☰ Главное меню или задайте вопрос обычным языком.",
        menu_button(),
    )


# ============================================================
# CALLBACKS
# ============================================================

def callback_handler(callback):
    callback_id = callback.get("id")
    data = callback.get("data", "")
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    message_id = message.get("message_id")

    remember_chat(chat_id)
    tg_answer(callback_id)

    if data == "menu:open":
        # Старую клавиатуру/отчёт не плодим: показываем меню отдельным сообщением.
        tg_send(chat_id, "☰ Главное меню", main_menu())
        return

    if data == "plan:set" or data == "plan:edit":
        _plan_input.add(chat_id)
        tg_send(
            chat_id,
            "🎯 Введи сумму месячного плана, например: 1500000",
            back_button(),
        )
        return

    if data == "plan:clear":
        today = datetime.now(TZ).date()
        key = f"{today.year:04d}-{today.month:02d}"
        con = db()
        con.execute("DELETE FROM monthly_plans WHERE month=?", (key,))
        con.commit()
        con.close()
        tg_send(chat_id, "🗑 План текущего месяца сброшен.", plan_menu())
        return

    if data == "dyn:calendar":
        today = datetime.now(TZ).date()
        _dynamic[chat_id] = {
            "stage": "start",
            "start": None,
        }
        tg_send(
            chat_id,
            "📈 ДИНАМИКА\n\nВыберите дату начала периода:",
            calendar_keyboard(today.year, today.month, "start"),
        )
        return

    if data == "cal:noop":
        return

    if data.startswith("cal:move:"):
        _, _, mode, y, m = data.split(":")
        y, m = int(y), int(m)
        state = _dynamic.get(chat_id) or {"stage": "start", "start": None}
        selected = state.get("start")

        if message_id:
            tg_edit(
                chat_id,
                message_id,
                "📈 ДИНАМИКА\n\n"
                + (
                    "Выберите дату начала периода:"
                    if mode == "start"
                    else f"Начало: {selected:%d.%m.%Y}\n\nВыберите дату окончания периода:"
                ),
                calendar_keyboard(y, m, mode, selected),
            )
        return

    if data.startswith("cal:day:"):
        _, _, mode, y, m, d = data.split(":")
        selected = date(int(y), int(m), int(d))

        state = _dynamic.get(chat_id)

        if mode == "start":
            _dynamic[chat_id] = {
                "stage": "end",
                "start": selected,
            }
            tg_send(
                chat_id,
                f"📈 ДИНАМИКА\n\n"
                f"Дата начала: {selected:%d.%m.%Y}\n\n"
                "Теперь выберите дату окончания периода:",
                calendar_keyboard(
                    selected.year,
                    selected.month,
                    "end",
                    selected,
                ),
            )
            return

        if not state or not state.get("start"):
            tg_send(chat_id, "Сначала выберите дату начала.", dynamics_menu())
            return

        start = state["start"]

        if selected < start:
            tg_send(
                chat_id,
                "❌ Дата окончания не может быть раньше начала.",
                calendar_keyboard(start.year, start.month, "end", start),
            )
            return

        # Период может быть только внутри одного календарного месяца.
        if selected.year != start.year or selected.month != start.month:
            tg_send(
                chat_id,
                "❌ Период должен быть внутри одного календарного месяца.",
                calendar_keyboard(start.year, start.month, "end", start),
            )
            return

        _dynamic.pop(chat_id, None)

        try:
            report = report_dynamics(start, selected)
        except Exception as exc:
            report = (
                "❌ Не удалось получить динамику из YCLIENTS.\n\n"
                f"{exc}"
            )

        tg_send(chat_id, report, dynamics_menu())
        return


# ============================================================
# TELEGRAM WEBHOOK
# ============================================================

@app.post("/telegram/webhook")
def telegram_webhook():
    update = request.get_json(silent=True) or {}

    if update.get("callback_query"):
        try:
            callback_handler(update["callback_query"])
        except Exception as exc:
            log.exception("Callback error")
            callback = update["callback_query"]
            msg = callback.get("message") or {}
            cid = str((msg.get("chat") or {}).get("id", ""))
            if cid:
                tg_send(cid, f"❌ Ошибка: {exc}", menu_button())
        return jsonify({"ok": True})

    message = update.get("message") or update.get("edited_message") or {}
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id", "")).strip()
    text = (message.get("text") or "").strip()

    if not chat_id:
        return jsonify({"ok": True})

    remember_chat(chat_id)

    try:
        answer, markup = route_message(chat_id, text)
        tg_send(chat_id, answer, markup)
    except Exception as exc:
        log.exception("Bot error")
        tg_send(
            chat_id,
            "❌ Не удалось получить данные YCLIENTS.\n\n"
            f"{exc}",
            menu_button(),
        )

    return jsonify({"ok": True})


# ============================================================
# YCLIENTS WEBHOOK / HEALTH
# ============================================================

@app.route("/webhook", methods=["GET", "POST"])
def yclients_webhook():
    if request.method == "GET":
        return jsonify({
            "status": "ok",
            "service": "metro-analytics",
            "branch": BRANCH_NAME,
        })

    payload = request.get_json(silent=True) or {}
    log.info("YCLIENTS webhook: %s", payload)

    return jsonify({"ok": True}), 200


@app.route("/register", methods=["GET"])
def register():
    return jsonify({
        "service": "МЕТРО — Аналитик",
        "status": "ready",
        "company_id": COMPANY_ID,
    })


@app.route("/callback", methods=["GET", "POST"])
def callback():
    return jsonify({
        "ok": True,
        "message": "Callback received",
        "params": dict(request.args),
        "payload": request.get_json(silent=True),
    })


@app.get("/")
def index():
    return jsonify({
        "service": "МЕТРО — Аналитик",
        "status": "online",
        "version": "2026-08-28-YC-DATA-MENU-FINAL",
        "branch": BRANCH_NAME,
        "company_id": COMPANY_ID,
    })


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "version": "2026-08-28-YC-DATA-MENU-FINAL",
        "telegram": bool(TELEGRAM_BOT_TOKEN),
        "yclients_company_id": COMPANY_ID,
        "yclients_user_token": bool(YCLIENTS_USER_TOKEN),
        "yclients_partner_token": bool(YCLIENTS_PARTNER_TOKEN),
        "known_chats": len(_known_chats),
    })


# ============================================================
# TELEGRAM WEBHOOK CONFIG
# ============================================================

def configure_telegram_webhook():
    if not TG or not PUBLIC_URL:
        log.warning("Telegram webhook is not configured: missing token or PUBLIC_URL.")
        return

    url = f"{PUBLIC_URL}/telegram/webhook"

    try:
        r = tg_call(
            "setWebhook",
            {
                "url": url,
                "drop_pending_updates": False,
            },
        )
        if r is not None:
            log.info("Telegram webhook: %s %s", r.status_code, r.text[:500])
    except Exception:
        log.exception("Webhook configuration failed")


# ============================================================
# START
# ============================================================

init_db()
configure_telegram_webhook()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
