import os
import sqlite3
import threading
import time
import calendar
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

# ============================================================
# МЕТРО — Аналитик
# Филиал: МЕТРО — Транссибирская 1
# Администраторы НЕ используются.
#
# Render ENV: только 4 переменные, которые уже настроены:
# PUBLIC_URL
# TELEGRAM_BOT_TOKEN
# YCLIENTS_COMPANY_ID
# YCLIENTS_USER_TOKEN
# ============================================================

PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
YCLIENTS_COMPANY_ID = os.getenv("YCLIENTS_COMPANY_ID", "").strip()
YCLIENTS_USER_TOKEN = os.getenv("YCLIENTS_USER_TOKEN", "").strip()

# ВАЖНО: API YCLIENTS для обычных защищённых методов требует
# Bearer partner_token + User user_token. Если partner token уже
# есть в окружении под одним из старых имён — используем его.
# Новых обязательных переменных не добавляем.
YCLIENTS_PARTNER_TOKEN = (
    os.getenv("YCLIENTS_PARTNER_TOKEN", "").strip()
    or os.getenv("YCLIENTS_PARTNER_ID", "").strip()
    or os.getenv("YCLIENTS_TOKEN_PARTNER", "").strip()
)

try:
    COMPANY_ID = int(YCLIENTS_COMPANY_ID or "0")
except Exception:
    COMPANY_ID = 0

BRANCH_NAME = "МЕТРО — Транссибирская 1"
TZ = ZoneInfo("Asia/Omsk")
TG = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else ""
YC = "https://api.yclients.com/api/v1"
DB = "/tmp/metro_analytics.sqlite3"

chats = set()
lock = threading.Lock()

# Состояние календаря динамики: chat_id -> {stage, start, month}
dynamics_state = {}
# Состояние задания плана: chat_id -> True
plan_state = set()


# ============================================================
# БАЗА
# ============================================================

def db_conn():
    return sqlite3.connect(DB)


def init_db():
    con = db_conn()
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
    con.execute("""
        CREATE TABLE IF NOT EXISTS monthly_plans(
            month TEXT PRIMARY KEY,
            amount REAL NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    for row in con.execute("SELECT chat_id FROM chats"):
        chats.add(str(row[0]))
    con.commit()
    con.close()


def remember_chat(chat_id):
    if not chat_id:
        return
    cid = str(chat_id)
    with lock:
        chats.add(cid)
    con = db_conn()
    con.execute(
        "INSERT OR IGNORE INTO chats(chat_id, added_at) VALUES(?, ?)",
        (cid, datetime.now(TZ).isoformat()),
    )
    con.commit()
    con.close()


# ============================================================
# УТИЛИТЫ
# ============================================================

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


def percent(value):
    try:
        return f"{float(value):.1f}%"
    except Exception:
        return "0.0%"


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


def month_start(d):
    return d.replace(day=1)


def month_end(d):
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def month_label(d):
    names = [
        "январь", "февраль", "март", "апрель", "май", "июнь",
        "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
    ]
    return f"{names[d.month - 1]} {d.year}"


def period_label(start, end):
    return f"{start.strftime('%d.%m.%Y')} — {end.strftime('%d.%m.%Y')}"


def current_month_period():
    today = datetime.now(TZ).date()
    return month_start(today), month_end(today), month_label(today)


def previous_month_same_days(start, end):
    # Сравнение именно с теми же числами предыдущего месяца.
    prev_month_anchor = (start.replace(day=1) - timedelta(days=1))
    py, pm = prev_month_anchor.year, prev_month_anchor.month
    last_day = calendar.monthrange(py, pm)[1]
    ps_day = min(start.day, last_day)
    pe_day = min(end.day, last_day)
    return date(py, pm, ps_day), date(py, pm, pe_day)


def normalize_amount(text):
    raw = (text or "").replace("₽", "").replace("руб", "").replace("р", "")
    raw = raw.replace(" ", "").replace("_", "").replace(",", ".")
    try:
        value = float(raw)
        return value if value > 0 else None
    except Exception:
        return None


# ============================================================
# TELEGRAM
# ============================================================

def tg_request(method, payload=None):
    if not TG:
        return None
    try:
        r = requests.post(f"{TG}/{method}", json=payload or {}, timeout=30)
        print("Telegram:", method, r.status_code, r.text[:500])
        return r
    except Exception as exc:
        print("Telegram error:", repr(exc))
        return None


def tg_send(chat_id, text, keyboard=None, inline=False):
    payload = {"chat_id": chat_id, "text": text}
    if keyboard:
        payload["reply_markup"] = keyboard
    response = tg_request("sendMessage", payload)
    return bool(response is not None and response.ok)


def tg_edit(chat_id, message_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if keyboard:
        payload["reply_markup"] = keyboard
    return bool(tg_request("editMessageText", payload))


def tg_answer_callback(callback_id):
    tg_request("answerCallbackQuery", {"callback_query_id": callback_id})


def broadcast(text):
    for chat_id in list(chats):
        tg_send(chat_id, text, main_menu())


# ============================================================
# YCLIENTS
# ============================================================

def yclients_headers():
    # Формат авторизации YCLIENTS: Bearer partner_token, User user_token.
    authorization = ""
    if YCLIENTS_PARTNER_TOKEN:
        authorization = f"Bearer {YCLIENTS_PARTNER_TOKEN}"
        if YCLIENTS_USER_TOKEN:
            authorization += f", User {YCLIENTS_USER_TOKEN}"
    elif YCLIENTS_USER_TOKEN:
        # Не скрываем проблему: пробуем User token отдельно, если
        # текущая конфигурация проекта действительно поддерживает его.
        authorization = f"Bearer {YCLIENTS_USER_TOKEN}"

    return {
        "Accept": "application/vnd.yclients.v2+json",
        "Content-Type": "application/json",
        "Authorization": authorization,
    }


def yclients_get(path, params=None):
    if not COMPANY_ID or not YCLIENTS_USER_TOKEN:
        print("YCLIENTS: missing COMPANY_ID or USER_TOKEN")
        return None

    try:
        response = requests.get(
            f"{YC}/{path.lstrip('/')}",
            headers=yclients_headers(),
            params=params or {},
            timeout=30,
        )
        print("YCLIENTS GET:", response.status_code, response.url)
        if not response.ok:
            print("YCLIENTS ERROR:", response.text[:1500])
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
            "start_date": f"{start_date.isoformat()} 00:00:00",
            "end_date": f"{end_date.isoformat()} 23:59:59",
            "count": 100,
        },
        100,
    )


def get_clients():
    return yclients_list(f"clients/{COMPANY_ID}", {"count": 100}, 100)


# ============================================================
# РАЗБОР YCLIENTS
# ============================================================

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
            total += float(service.get("cost_to_pay", service.get("cost", 0)) or 0)
        except Exception:
            pass
    for good in record.get("goods_transactions") or []:
        try:
            total += float(good.get("cost", good.get("amount", 0)) or 0)
        except Exception:
            pass
    return total


def record_duration_minutes(record):
    for key in ("seance_length", "duration", "service_duration", "length"):
        try:
            value = int(record.get(key) or 0)
            if value > 0:
                return value // 60 if value > 60 else value
        except Exception:
            pass
    return 0


def calculate(records):
    result = {
        "records": 0,
        "attended": 0,
        "noshow": 0,
        "cancelled": 0,
        "revenue": 0.0,
        "clients": set(),
        "new_clients": set(),
        "staff": defaultdict(dict),
        "services": defaultdict(lambda: {"count": 0, "revenue": 0.0}),
        "sources": defaultdict(int),
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
        staff_id = str(staff.get("id") or record.get("staff_id") or staff.get("name") or "unknown")
        staff_name = staff.get("name") or "Без мастера"
        master = result["staff"].setdefault(
            staff_id,
            {
                "name": staff_name,
                "records": 0,
                "attended": 0,
                "noshow": 0,
                "revenue": 0.0,
                "clients": set(),
                "duration": 0,
            },
        )
        master["records"] += 1
        master["revenue"] += revenue
        master["duration"] += record_duration_minutes(record)
        if attendance == 1:
            master["attended"] += 1
        elif attendance == -1:
            master["noshow"] += 1
        if client_id:
            master["clients"].add(str(client_id))

        source = record.get("source") or record.get("source_title") or record.get("from_url")
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
                        service.get("cost_to_pay", service.get("cost", 0)) or 0
                    )
                except Exception:
                    pass

    result["clients_count"] = len(result["clients"])
    result["avg_check"] = result["revenue"] / result["attended"] if result["attended"] else 0
    return result


def get_period(start, end):
    return calculate(get_records(start, end))


# ============================================================
# ОТЧЁТЫ: МЕСЯЦ — БАЗОВЫЙ ПЕРИОД ВСЕЙ АНАЛИТИКИ
# ============================================================

def monthly_metrics():
    start, end, label = current_month_period()
    return start, end, label, get_period(start, end)


def report_today():
    today = datetime.now(TZ).date()
    current = get_period(today, today)
    previous = get_period(today - timedelta(days=1), today - timedelta(days=1))
    return (
        "📊 СЕГОДНЯ\n"
        f"🏪 {BRANCH_NAME}\n\n"
        f"💰 Выручка: {money(current['revenue'])}\n"
        f"👥 Клиенты: {number(current['clients_count'])}\n"
        f"🧾 Средний чек: {money(current['avg_check'])}\n"
        f"📅 Записи: {number(current['records'])}\n"
        f"✅ Состоявшиеся: {number(current['attended'])}\n"
        f"❌ Неявки: {number(current['noshow'])}\n"
        f"🚫 Отмены: {number(current['cancelled'])}\n\n"
        "📈 К вчера\n"
        f"Выручка: {change(current['revenue'], previous['revenue'])}\n"
        f"Клиенты: {change(current['clients_count'], previous['clients_count'])}\n"
        f"Средний чек: {change(current['avg_check'], previous['avg_check'])}"
    )


def report_month():
    start, end, label, metrics = monthly_metrics()
    return (
        "📊 АНАЛИТИКА МЕСЯЦА\n"
        f"🏪 {BRANCH_NAME}\n"
        f"🗓 {period_label(start, end)}\n"
        f"📅 {label}\n\n"
        f"💰 Выручка: {money(metrics['revenue'])}\n"
        f"👥 Клиенты: {number(metrics['clients_count'])}\n"
        f"🧾 Средний чек: {money(metrics['avg_check'])}\n"
        f"📅 Записи: {number(metrics['records'])}\n"
        f"✅ Состоявшиеся: {number(metrics['attended'])}\n"
        f"❌ Неявки: {number(metrics['noshow'])}\n"
        f"🚫 Отмены: {number(metrics['cancelled'])}"
    )


def report_masters():
    start, end, label, metrics = monthly_metrics()
    ranking = []
    for master in metrics["staff"].values():
        if master["name"] == "Без мастера":
            continue
        score = master["revenue"] + master["attended"] * 500 - master["noshow"] * 300
        ranking.append((score, master))
    ranking.sort(key=lambda x: x[0], reverse=True)
    lines = [
        "👨‍💼 МАСТЕРА — МЕСЯЦ",
        f"🏪 {BRANCH_NAME}",
        f"🗓 {period_label(start, end)}",
        "",
    ]
    if not ranking:
        return "\n".join(lines + ["Нет данных по мастерам за текущий месяц."])
    for index, (_, master) in enumerate(ranking, 1):
        lines.append(
            f"{index}. {master['name']}\n"
            f"   💰 {money(master['revenue'])} | 👥 {len(master['clients'])} | "
            f"✅ {master['attended']} | ❌ {master['noshow']}"
        )
    return "\n".join(lines)


def report_master_load(question=""):
    start, end, label, metrics = monthly_metrics()
    masters = [m for m in metrics["staff"].values() if m["name"] != "Без мастера"]
    if not masters:
        return (
            "🧠 АНАЛИЗ ЗАГРУЗКИ МАСТЕРОВ\n"
            f"🏪 {BRANCH_NAME}\n"
            f"🗓 {period_label(start, end)}\n\n"
            "Нет данных по мастерам за текущий месяц."
        )

    avg_attended = sum(m["attended"] for m in masters) / len(masters)
    avg_records = sum(m["records"] for m in masters) / len(masters)
    avg_noshow = sum(m["noshow"] for m in masters) / len(masters)
    avg_revenue = sum(m["revenue"] for m in masters) / len(masters)
    q = (question or "").lower()
    selected = [m for m in masters if m["name"].lower() in q]

    def one_master(master):
        reasons = []
        if master["attended"] < avg_attended:
            reasons.append(f"визитов меньше среднего ({master['attended']} против {avg_attended:.1f})")
        if master["records"] < avg_records:
            reasons.append(f"записей меньше среднего ({master['records']} против {avg_records:.1f})")
        if master["noshow"] > avg_noshow and master["noshow"] > 0:
            reasons.append(f"неявок больше среднего ({master['noshow']} против {avg_noshow:.1f})")
        if master["revenue"] < avg_revenue:
            reasons.append(f"выручка ниже среднего ({money(master['revenue'])} против {money(avg_revenue)})")
        if not reasons:
            reasons.append("по доступным данным явной причины ниже средней загрузки не видно")
        conversion = master["attended"] / master["records"] * 100 if master["records"] else 0
        return (
            f"💈 {master['name']}\n"
            f"💰 {money(master['revenue'])}\n"
            f"📅 Записи: {master['records']}\n"
            f"✅ Визиты: {master['attended']}\n"
            f"❌ Неявки: {master['noshow']}\n"
            f"📊 Состоялись: {percent(conversion)}\n"
            "⚠️ Причины: " + "; ".join(reasons)
        )

    if selected:
        return (
            "🧠 ПОЧЕМУ НИЗКАЯ ЗАГРУЗКА\n"
            f"🏪 {BRANCH_NAME}\n"
            f"🗓 {period_label(start, end)}\n\n"
            + one_master(selected[0])
        )

    masters.sort(key=lambda m: (m["attended"], m["revenue"]))
    lines = [
        "🧠 ПОЧЕМУ У МАСТЕРОВ НИЗКАЯ ЗАГРУЗКА",
        f"🏪 {BRANCH_NAME}",
        f"🗓 {period_label(start, end)}",
        "",
    ]
    for master in masters:
        lines.append(one_master(master))
        lines.append("")
    return "\n".join(lines)


def report_analysis():
    start, end, label, current = monthly_metrics()
    prev_end = start - timedelta(days=1)
    prev_start = prev_end.replace(day=1)
    previous = get_period(prev_start, prev_end)
    observations = []
    if current["revenue"] > previous["revenue"]:
        observations.append("🟢 Выручка выше прошлого месяца.")
    elif current["revenue"] < previous["revenue"]:
        observations.append("🔴 Выручка ниже прошлого месяца.")
    if current["avg_check"] < previous["avg_check"]:
        observations.append("⚠️ Средний чек снизился.")
    if current["noshow"] > previous["noshow"]:
        observations.append("🔴 Неявок стало больше.")
    if current["attended"] > previous["attended"]:
        observations.append("🟢 Состоявшихся визитов стало больше.")
    if not observations:
        observations.append("⚪ Существенных изменений не обнаружено.")
    return (
        "🧠 АНАЛИЗ ФИЛИАЛА\n"
        f"🏪 {BRANCH_NAME}\n"
        f"🗓 {period_label(start, end)} ({label})\n\n"
        f"💰 Выручка: {money(current['revenue'])}\n"
        f"👥 Клиенты: {current['clients_count']}\n"
        f"✅ Визиты: {current['attended']}\n"
        f"🧾 Средний чек: {money(current['avg_check'])}\n\n"
        + "\n".join(observations)
    )


def report_clients():
    client_list = get_clients()
    active = [c for c in client_list if int(c.get("visits", 0) or 0) > 0]
    leaving, lost = [], []
    today = datetime.now(TZ).date()
    for client in active:
        last_visit = parse_date(client.get("last_change_date")) or parse_date(client.get("last_visit_date"))
        if not last_visit:
            continue
        days = (today - last_visit.date()).days
        if days >= 90:
            lost.append(client)
        elif days >= 45:
            leaving.append(client)
    return (
        "👥 КЛИЕНТЫ\n"
        f"🏪 {BRANCH_NAME}\n\n"
        f"Всего в базе: {len(client_list)}\n"
        f"С историей визитов: {len(active)}\n"
        f"🟡 Уходящие: {len(leaving)}\n"
        f"🔴 Потерянные: {len(lost)}"
    )


def report_return():
    client_list = get_clients()
    today = datetime.now(TZ).date()
    leaving, lost = [], []
    for client in client_list:
        if not int(client.get("visits", 0) or 0):
            continue
        last_visit = parse_date(client.get("last_change_date")) or parse_date(client.get("last_visit_date"))
        if not last_visit:
            continue
        days = (today - last_visit.date()).days
        if 45 <= days < 90:
            leaving.append(client)
        elif days >= 90:
            lost.append(client)
    lines = [
        "🔄 КОГО НУЖНО ВЕРНУТЬ",
        f"🏪 {BRANCH_NAME}",
        "",
        f"🟡 Уходящие: {len(leaving)}",
        f"🔴 Потерянные: {len(lost)}",
        "",
    ]
    for client in (leaving + lost)[:40]:
        name = client.get("display_name") or client.get("name") or "Клиент"
        phone = client.get("phone") or ""
        lines.append(f"• {name} {phone}".strip())
    return "\n".join(lines)


def report_finances():
    start, end, label, m = monthly_metrics()
    return (
        "💰 ФИНАНСЫ — МЕСЯЦ\n"
        f"🏪 {BRANCH_NAME}\n"
        f"🗓 {period_label(start, end)}\n\n"
        f"Выручка: {money(m['revenue'])}\n"
        f"Состоявшиеся визиты: {m['attended']}\n"
        f"Средний чек: {money(m['avg_check'])}\n"
        f"Записи: {m['records']}\n"
        f"Неявки: {m['noshow']}\n"
        f"Отмены: {m['cancelled']}"
    )


def report_services():
    start, end, label, m = monthly_metrics()
    services = sorted(m["services"].items(), key=lambda item: item[1]["revenue"], reverse=True)
    lines = [
        "💈 УСЛУГИ — МЕСЯЦ",
        f"🏪 {BRANCH_NAME}",
        f"🗓 {period_label(start, end)}",
        "",
    ]
    if not services:
        return "\n".join(lines + ["Нет данных по услугам."])
    for i, (name, service) in enumerate(services[:20], 1):
        lines.append(f"{i}. {name} — {service['count']} | {money(service['revenue'])}")
    return "\n".join(lines)


def report_marketing():
    start, end, label, m = monthly_metrics()
    sources = sorted(m["sources"].items(), key=lambda item: item[1], reverse=True)
    lines = [
        "📣 МАРКЕТИНГ — МЕСЯЦ",
        f"🏪 {BRANCH_NAME}",
        f"🗓 {period_label(start, end)}",
        "",
    ]
    if not sources:
        return "\n".join(lines + ["Источники не заполнены в доступных данных YCLIENTS."])
    for source, count in sources:
        lines.append(f"• {source}: {count}")
    return "\n".join(lines)


# ============================================================
# ПЛАН / ФАКТ — ТОЛЬКО МЕСЯЧНЫЙ ПЛАН
# ============================================================

def get_plan(d=None):
    d = d or datetime.now(TZ).date()
    key = f"{d.year:04d}-{d.month:02d}"
    con = db_conn()
    row = con.execute("SELECT amount FROM monthly_plans WHERE month=?", (key,)).fetchone()
    con.close()
    return float(row[0]) if row else None


def set_plan(d, amount):
    key = f"{d.year:04d}-{d.month:02d}"
    con = db_conn()
    con.execute(
        "INSERT INTO monthly_plans(month, amount, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(month) DO UPDATE SET amount=excluded.amount, updated_at=excluded.updated_at",
        (key, float(amount), datetime.now(TZ).isoformat()),
    )
    con.commit()
    con.close()


def plan_menu():
    return {
        "inline_keyboard": [
            [{"text": "🎯 Задать план", "callback_data": "plan:set"}],
            [{"text": "✏️ Изменить план", "callback_data": "plan:edit"}],
            [{"text": "📊 План / факт", "callback_data": "plan:view"}],
            [{"text": "⬅️ Главное меню", "callback_data": "menu:main"}],
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
        f"📅 {month_label(today)}",
        "",
    ]
    if plan is None:
        lines += [
            "План на этот месяц пока не задан.",
            "",
            "После задания месячного плана бот будет считать:",
            "• факт;",
            "• % выполнения;",
            "• остаток;",
            "• сколько нужно делать в день для выполнения плана.",
        ]
        return "\n".join(lines)

    elapsed_end = min(today, end)
    elapsed_days = max(1, (elapsed_end - start).days + 1)
    remaining_days = max(1, (end - today).days + 1)
    fact = float(fact)
    remain = max(0.0, plan - fact)
    completion = fact / plan * 100 if plan else 0
    daily_needed = remain / remaining_days if remaining_days else remain
    lines += [
        f"🎯 План: {money(plan)}",
        f"💰 Факт: {money(fact)}",
        f"📊 Выполнение: {percent(completion)}",
        f"📉 Осталось: {money(remain)}",
        f"📆 Прошло дней: {elapsed_days} из {(end - start).days + 1}",
        f"🔥 Нужно делать в день: {money(daily_needed)}",
    ]
    return "\n".join(lines)


# ============================================================
# ДИНАМИКА — ЕДИНСТВЕННОЕ МЕСТО, ГДЕ ЕСТЬ ВЫБОР ПЕРИОДА
# ============================================================

def report_dynamics(start, end):
    current = get_period(start, end)
    prev_start, prev_end = previous_month_same_days(start, end)
    previous = get_period(prev_start, prev_end)
    return (
        "📈 ДИНАМИКА\n"
        f"🏪 {BRANCH_NAME}\n\n"
        f"🗓 Текущий период: {period_label(start, end)}\n"
        f"🗓 Сравнение: {period_label(prev_start, prev_end)}\n\n"
        f"💰 Выручка: {change(current['revenue'], previous['revenue'])}\n"
        f"👥 Клиенты: {change(current['clients_count'], previous['clients_count'])}\n"
        f"🧾 Средний чек: {change(current['avg_check'], previous['avg_check'])}\n"
        f"📅 Записи: {change(current['records'], previous['records'])}\n"
        f"✅ Визиты: {change(current['attended'], previous['attended'])}\n"
        f"❌ Неявки: {change(current['noshow'], previous['noshow'])}"
    )


def dynamics_menu():
    return {
        "inline_keyboard": [
            [{"text": "📅 Выбрать период", "callback_data": "dyn:calendar"}],
            [{"text": "📈 Показать последний период", "callback_data": "dyn:last"}],
            [{"text": "⬅️ Главное меню", "callback_data": "menu:main"}],
        ]
    }


def calendar_keyboard(year, month, start_selected=None):
    cal = calendar.monthcalendar(year, month)
    month_names = [
        "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
        "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
    ]
    rows = [
        [
            {"text": "◀️", "callback_data": f"cal:move:{year}:{month}:prev"},
            {"text": f"{month_names[month-1]} {year}", "callback_data": "cal:noop"},
            {"text": "▶️", "callback_data": f"cal:move:{year}:{month}:next"},
        ],
        [{"text": x, "callback_data": "cal:noop"} for x in ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]],
    ]
    for week in cal:
        row = []
        for day in week:
            if day == 0:
                row.append({"text": " ", "callback_data": "cal:noop"})
            else:
                marker = ""
                if start_selected and day == start_selected.day and year == start_selected.year and month == start_selected.month:
                    marker = "🟢"
                row.append({
                    "text": f"{marker}{day}",
                    "callback_data": f"cal:day:{year}:{month}:{day}",
                })
        rows.append(row)
    rows.append([{"text": "❌ Отмена", "callback_data": "menu:main"}])
    return {"inline_keyboard": rows}


def calendar_text(state):
    if state.get("stage") == "start":
        return "📈 ДИНАМИКА\n\nВыбери дату начала периода в календаре.\nПример: 1 августа."
    start = state["start"]
    return (
        "📈 ДИНАМИКА\n\n"
        f"Дата начала: {start.strftime('%d.%m.%Y')}\n\n"
        "Теперь выбери дату окончания периода в этом же месяце."
    )


# ============================================================
# МЕНЮ
# ============================================================

def main_menu():
    return {
        "keyboard": [
            [{"text": "📊 Сегодня"}, {"text": "🧠 Анализ"}],
            [{"text": "👨‍💼 Мастера"}, {"text": "📈 Динамика"}],
            [{"text": "👥 Клиенты"}, {"text": "🔄 Вернуть клиентов"}],
            [{"text": "💰 Финансы"}, {"text": "💈 Услуги"}],
            [{"text": "📣 Маркетинг"}, {"text": "🎯 План / факт"}],
            [{"text": "🤖 Спроси аналитика"}, {"text": "ℹ️ Инструкция"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def instruction():
    return (
        "ℹ️ ИНСТРУКЦИЯ\n\n"
        "🤖 МЕТРО — Аналитик\n"
        f"🏪 {BRANCH_NAME}\n\n"
        "📅 Все основные отчёты работают за текущий календарный месяц.\n"
        "В каждом отчёте бот показывает даты месяца: с 01 числа по последнее число месяца.\n\n"
        "📈 Только в разделе «Динамика» можно выбрать период календарём.\n"
        "Например: 01.08.2026–07.08.2026 → сравнение с 01.07.2026–07.07.2026.\n\n"
        "🎯 В «План / факт» план задаётся только на текущий месяц.\n"
        "Можно задать или изменить сумму плана. Бот считает факт, выполнение, остаток и необходимую дневную выручку.\n\n"
        "🤖 Можно задавать вопросы обычным языком:\n"
        "• Почему у мастеров низкая загрузка?\n"
        "• Почему у Дмитрия низкая загрузка?\n"
        "• Почему упала выручка?\n"
        "• Кого нужно вернуть?\n"
        "• Сравни 1–7 августа с 1–7 июля."
    )


# ============================================================
# ОБРАБОТКА ПЛАНА
# ============================================================

def handle_plan_text(chat_id, text):
    if str(chat_id) not in plan_state:
        return None
    amount = normalize_amount(text)
    if amount is None:
        return "❌ Не понял сумму. Напиши, например: 1500000"
    set_plan(datetime.now(TZ).date(), amount)
    plan_state.discard(str(chat_id))
    return "✅ План сохранён на текущий месяц.\n\n" + report_plan()


# ============================================================
# МАРШРУТИЗАЦИЯ
# ============================================================

def route_message(chat_id, text):
    raw = (text or "").strip()
    lower = raw.lower()

    if str(chat_id) in plan_state:
        result = handle_plan_text(chat_id, raw)
        if result:
            return result, plan_menu()

    if lower in ("/start", "start"):
        remember_chat(chat_id)
        return (
            "🤖 МЕТРО — Аналитик\n\n"
            f"🏪 {BRANCH_NAME}\n\n"
            "Все основные отчёты считаются за текущий месяц.\n"
            "Период выбирается только внутри «Динамика».\n\n"
            "Выбирай нужный раздел кнопками ниже.",
            main_menu(),
        )

    if lower in ("ℹ️ инструкция", "инструкция", "/help", "/инструкция"):
        return instruction(), main_menu()

    if lower in ("📊 сегодня", "сегодня", "/сегодня"):
        return report_today(), main_menu()

    if lower in ("🧠 анализ", "анализ", "/анализ"):
        return report_analysis(), main_menu()

    if lower in ("👨‍💼 мастера", "мастера", "/мастера"):
        return report_masters(), main_menu()

    if lower in ("📈 динамика", "динамика", "/динамика"):
        today = datetime.now(TZ).date()
        start = today - timedelta(days=6)
        return "📈 ДИНАМИКА\n\nНажми «📅 Выбрать период», чтобы задать даты в календаре.\n\n" + report_dynamics(start, today), dynamics_menu()

    if lower in ("👥 клиенты", "клиенты", "/клиенты"):
        return report_clients(), main_menu()

    if lower in ("🔄 вернуть клиентов", "вернуть клиентов", "кого вернуть", "/вернуть"):
        return report_return(), main_menu()

    if lower in ("💰 финансы", "финансы", "/финансы"):
        return report_finances(), main_menu()

    if lower in ("💈 услуги", "услуги", "/услуги"):
        return report_services(), main_menu()

    if lower in ("📣 маркетинг", "маркетинг", "/маркетинг"):
        return report_marketing(), main_menu()

    if lower in ("🎯 план / факт", "план / факт", "/план"):
        return report_plan(), plan_menu()

    if lower in ("🤖 спроси аналитика", "спроси аналитика"):
        return (
            "🤖 СПРОСИ АНАЛИТИКА\n\n"
            "Напиши вопрос обычным языком. Например:\n"
            "• Почему у мастеров низкая загрузка?\n"
            "• Почему у Дмитрия низкая загрузка?\n"
            "• Почему упала выручка?\n"
            "• Кого нужно вернуть?",
            main_menu(),
        )

    # План естественным языком
    if "задать план" in lower or "поставить план" in lower:
        plan_state.add(str(chat_id))
        return "🎯 Введи сумму месячного плана, например: 1500000", plan_menu()
    if "изменить план" in lower:
        plan_state.add(str(chat_id))
        return "✏️ Введи новую сумму плана на текущий месяц, например: 1500000", plan_menu()

    # Вопросы обычным языком. Сначала загрузка, чтобы не сваливалось в общий анализ.
    if any(x in lower for x in ("загрузк", "загружен", "свободн", "мало запис", "мало визит")):
        return report_master_load(raw), main_menu()
    if "мастер" in lower or "рейтинг" in lower or "кто лучше" in lower or "кто хуже" in lower:
        return report_masters(), main_menu()
    if "почему" in lower or "что происходит" in lower or "проблем" in lower:
        return report_analysis(), main_menu()
    if "вернут" in lower or "потерян" in lower or "уходящ" in lower:
        return report_return(), main_menu()
    if "финанс" in lower or "выруч" in lower:
        return report_finances(), main_menu()
    if "услуг" in lower:
        return report_services(), main_menu()
    if "маркет" in lower or "источник" in lower:
        return report_marketing(), main_menu()
    if "динамик" in lower or "сравни" in lower:
        today = datetime.now(TZ).date()
        start = today - timedelta(days=6)
        return report_dynamics(start, today), dynamics_menu()
    if "клиент" in lower:
        return report_clients(), main_menu()

    return (
        "🤖 Не совсем понял запрос.\n\n"
        "Используй кнопки меню или спроси, например:\n"
        "«Почему у мастеров низкая загрузка?»\n"
        "«Почему упала выручка?»\n"
        "«Кого нужно вернуть?»",
        main_menu(),
    )


# ============================================================
# CALLBACK-КНОПКИ: ПЛАН И КАЛЕНДАРЬ ДИНАМИКИ
# ============================================================

def handle_callback(callback):
    callback_id = callback.get("id")
    data = callback.get("data", "")
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    message_id = message.get("message_id")
    remember_chat(chat_id)
    tg_answer_callback(callback_id)

    if data == "menu:main":
        if message_id:
            tg_edit(chat_id, message_id, "Главное меню.")
        tg_send(chat_id, "Главное меню.", main_menu())
        return

    if data == "plan:view":
        tg_send(chat_id, report_plan(), plan_menu())
        return

    if data in ("plan:set", "plan:edit"):
        plan_state.add(str(chat_id))
        tg_send(chat_id, "🎯 Введи сумму месячного плана, например: 1500000", plan_menu())
        return

    if data == "dyn:last":
        today = datetime.now(TZ).date()
        start = today - timedelta(days=6)
        tg_send(chat_id, report_dynamics(start, today), dynamics_menu())
        return

    if data == "dyn:calendar":
        today = datetime.now(TZ).date()
        dynamics_state[str(chat_id)] = {"stage": "start", "month": month_start(today), "start": None}
        tg_send(
            chat_id,
            calendar_text(dynamics_state[str(chat_id)]),
            calendar_keyboard(today.year, today.month),
        )
        return

    if data == "cal:noop":
        return

    if data.startswith("cal:move:"):
        _, _, year_s, month_s, direction = data.split(":")
        y, m = int(year_s), int(month_s)
        anchor = date(y, m, 1)
        if direction == "prev":
            anchor = anchor - timedelta(days=1)
            anchor = date(anchor.year, anchor.month, 1)
        else:
            if m == 12:
                anchor = date(y + 1, 1, 1)
            else:
                anchor = date(y, m + 1, 1)
        state = dynamics_state.get(str(chat_id), {"stage": "start", "start": None})
        state["month"] = anchor
        dynamics_state[str(chat_id)] = state
        if message_id:
            tg_edit(chat_id, message_id, calendar_text(state), calendar_keyboard(anchor.year, anchor.month, state.get("start")))
        return

    if data.startswith("cal:day:"):
        _, _, year_s, month_s, day_s = data.split(":")
        selected = date(int(year_s), int(month_s), int(day_s))
        state = dynamics_state.get(str(chat_id))
        if not state:
            state = {"stage": "start", "start": None, "month": month_start(selected)}

        if state.get("stage") == "start":
            state["start"] = selected
            state["stage"] = "end"
            state["month"] = month_start(selected)
            dynamics_state[str(chat_id)] = state
            tg_send(chat_id, calendar_text(state), calendar_keyboard(selected.year, selected.month, selected))
            return

        start = state.get("start")
        if not start or selected < start:
            tg_send(chat_id, "❌ Дата окончания должна быть не раньше даты начала. Выбери ещё раз.", calendar_keyboard(start.year, start.month, start))
            return
        if selected.month != start.month or selected.year != start.year:
            tg_send(chat_id, "❌ Для корректного сравнения выбери обе даты в одном месяце.", calendar_keyboard(start.year, start.month, start))
            return

        dynamics_state.pop(str(chat_id), None)
        tg_send(chat_id, report_dynamics(start, selected), dynamics_menu())
        return


# ============================================================
# TELEGRAM WEBHOOK
# ============================================================

@app.post("/telegram/webhook")
def telegram_webhook():
    update = request.get_json(silent=True) or {}

    if update.get("callback_query"):
        try:
            handle_callback(update["callback_query"])
        except Exception as exc:
            print("CALLBACK ERROR:", repr(exc))
        return jsonify({"ok": True})

    message = update.get("message") or update.get("edited_message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    remember_chat(chat_id)
    text = (message.get("text") or "").strip()

    if chat_id and text:
        try:
            answer, keyboard = route_message(chat_id, text)
            tg_send(chat_id, answer, keyboard)
        except Exception as exc:
            print("BOT ERROR:", repr(exc))
            tg_send(chat_id, "❌ Не удалось сформировать отчёт.\nОшибка: " + str(exc), main_menu())

    return jsonify({"ok": True})


# ============================================================
# YCLIENTS WEBHOOK
# ============================================================

@app.route("/webhook", methods=["GET", "POST"])
def yclients_webhook():
    if request.method == "GET":
        return jsonify({"status": "ok", "service": "metro-analytics", "branch": BRANCH_NAME})
    payload = request.get_json(silent=True) or {}
    con = db_conn()
    con.execute(
        "INSERT INTO webhook_events(received_at, payload) VALUES(?, ?)",
        (datetime.now(TZ).isoformat(), str(payload)),
    )
    con.commit()
    con.close()
    return jsonify({"ok": True}), 200


# ============================================================
# HEALTH
# ============================================================

@app.get("/")
def index():
    return jsonify({
        "service": "metro-analytics",
        "status": "online",
        "branch": BRANCH_NAME,
        "company_id": COMPANY_ID,
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
        "yclients_partner_token": bool(YCLIENTS_PARTNER_TOKEN),
        "chats": len(chats),
    })


# ============================================================
# АВТОМАТИЧЕСКИЕ ОТЧЁТЫ
# ============================================================

def scheduler():
    morning_sent = None
    evening_sent = None
    while True:
        try:
            now = datetime.now(TZ)
            day_key = now.strftime("%Y-%m-%d")
            if now.hour == 9 and now.minute < 5 and morning_sent != day_key:
                morning_sent = day_key
                broadcast("☀️ ДОБРОЕ УТРО\n\n" + report_today())
            if now.hour == 21 and now.minute < 5 and evening_sent != day_key:
                evening_sent = day_key
                broadcast("🌙 ВЕЧЕРНИЙ ОТЧЁТ\n\n" + report_today())
        except Exception as exc:
            print("Scheduler error:", repr(exc))
        time.sleep(60)


# ============================================================
# ЗАПУСК
# ============================================================

init_db()


def configure_telegram():
    if not TG or not PUBLIC_URL:
        print("Telegram webhook not configured: missing token or PUBLIC_URL")
        return
    try:
        webhook_url = f"{PUBLIC_URL}/telegram/webhook"
        r = tg_request("setWebhook", {"url": webhook_url, "drop_pending_updates": False})
        if r is not None:
            print("Telegram webhook:", r.status_code, r.text[:500])
    except Exception as exc:
        print("Webhook setup error:", repr(exc))


configure_telegram()
threading.Thread(target=scheduler, daemon=True).start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
