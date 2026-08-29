import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, date as date_cls
import calendar
from zoneinfo import ZoneInfo
from collections import defaultdict

import requests
import sys
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
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
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
analytics_sessions = {}
dynamic_states = {}
plan_states = {}
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
    con.execute("""
        CREATE TABLE IF NOT EXISTS monthly_plans(
            plan_key TEXT PRIMARY KEY,
            month TEXT NOT NULL,
            amount REAL NOT NULL,
            updated_at TEXT NOT NULL
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

    if keyboard is None:
        keyboard = {
            "inline_keyboard": [
                [{"text": "☰ Главное меню", "callback_data": "main_menu"}]
            ]
        }

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


def _compact_metric(label, previous, current, formatter=number):
    return f"{label}: {formatter(previous)} | {formatter(current)}"


def shift_month_same_day(d, months=-1):
    """Сдвигает дату на указанное число месяцев, сохраняя номер дня.
    Если в целевом месяце такого дня нет — берёт последний день месяца.
    """
    month_index = d.year * 12 + (d.month - 1) + months
    year = month_index // 12
    month = month_index % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date_cls(year, month, min(d.day, last_day))


def report_dynamics(selected_start=None, selected_end=None):
    today = datetime.now(TZ).date()

    # 7 дней: текущие 7 календарных дней сравниваются с теми же
    # числами предыдущего месяца.
    current_week_start = today - timedelta(days=6)
    current_week_end = today
    previous_month_week_start = shift_month_same_day(current_week_start, -1)
    previous_month_week_end = shift_month_same_day(current_week_end, -1)

    current_week = get_period(current_week_start, current_week_end)
    previous_week = get_period(
        previous_month_week_start,
        previous_month_week_end
    )

    current_month = get_period(today.replace(day=1), today)

    previous_month_end = today.replace(day=1) - timedelta(days=1)
    previous_month = get_period(
        previous_month_end.replace(day=1),
        previous_month_end
    )

    lines = [
        "📈 ДИНАМИКА",
        f"🏪 {BRANCH_NAME}",
        "",
        (
            f"7 дней: "
            f"{previous_month_week_start.strftime('%d.%m')}–"
            f"{previous_month_week_end.strftime('%d.%m')} | "
            f"{current_week_start.strftime('%d.%m')}–"
            f"{current_week_end.strftime('%d.%m')}"
        ),
        _compact_metric("💰 Выручка", previous_week["revenue"], current_week["revenue"], money),
        _compact_metric("👥 Клиенты", previous_week["clients_count"], current_week["clients_count"]),
        _compact_metric("🧾 Средний чек", previous_week["avg_check"], current_week["avg_check"], money),
        "",
        "Месяц: Прошлый | Настоящий",
        _compact_metric("💰 Выручка", previous_month["revenue"], current_month["revenue"], money),
        _compact_metric("👥 Клиенты", previous_month["clients_count"], current_month["clients_count"]),
        _compact_metric("🧾 Средний чек", previous_month["avg_check"], current_month["avg_check"], money),
    ]

    if selected_start and selected_end:
        # Например: 01.08–07.08 → 01.07–07.07.
        previous_start = shift_month_same_day(selected_start, -1)
        previous_end = shift_month_same_day(selected_end, -1)

        selected = get_period(selected_start, selected_end)
        selected_previous = get_period(previous_start, previous_end)

        lines.extend([
            "",
            f"📅 Период: {selected_start.strftime('%d.%m.%Y')}–{selected_end.strftime('%d.%m.%Y')}",
            f"Сравнение: {previous_start.strftime('%d.%m.%Y')}–{previous_end.strftime('%d.%m.%Y')}",
            _compact_metric("💰 Выручка", selected_previous["revenue"], selected["revenue"], money),
            _compact_metric("👥 Клиенты", selected_previous["clients_count"], selected["clients_count"]),
            _compact_metric("🧾 Средний чек", selected_previous["avg_check"], selected["avg_check"], money),
        ])

    return "\n".join(lines)


MONTH_NAMES = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"
]


def dynamic_period_keyboard(view_date=None, start=None, end=None, mode="calendar"):
    """Inline-календарь для выбора произвольного периода."""
    view_date = view_date or datetime.now(TZ).date()

    if mode == "months":
        rows = [
            [{"text": f"📅 {view_date.year}", "callback_data": f"dyncal|years|{view_date.year}|{view_date.month}"}]
        ]
        for row_start in range(0, 12, 3):
            row = []
            for month in range(row_start + 1, row_start + 4):
                label = MONTH_NAMES[month - 1]
                if month == view_date.month:
                    label = f"· {label} ·"
                row.append({
                    "text": label,
                    "callback_data": f"dyncal|month|{view_date.year}|{month}"
                })
            rows.append(row)
        rows.append([{"text": "⬅️ Назад к календарю", "callback_data": f"dyncal|calendar|{view_date.year}|{view_date.month}"}])
        return {"inline_keyboard": rows}

    if mode == "years":
        start_year = view_date.year - 4
        rows = []
        for row_start in range(start_year, start_year + 10, 2):
            rows.append([
                {"text": str(row_start), "callback_data": f"dyncal|month|{row_start}|1"},
                {"text": str(row_start + 1), "callback_data": f"dyncal|month|{row_start + 1}|1"},
            ])
        rows.append([{"text": "⬅️ Назад к календарю", "callback_data": f"dyncal|calendar|{view_date.year}|{view_date.month}"}])
        return {"inline_keyboard": rows}

    first_weekday, days_in_month = calendar.monthrange(view_date.year, view_date.month)
    # Telegram calendar: Monday = 0.
    offset = first_weekday
    rows = [
        [
            {"text": "‹", "callback_data": f"dyncal|nav|{(view_date.replace(day=1) - timedelta(days=1)).year}|{(view_date.replace(day=1) - timedelta(days=1)).month}"},
            {"text": f"{MONTH_NAMES[view_date.month - 1]} {view_date.year}", "callback_data": f"dyncal|months|{view_date.year}|{view_date.month}"},
            {"text": "›", "callback_data": f"dyncal|nav|{(view_date.replace(day=days_in_month) + timedelta(days=1)).year}|{(view_date.replace(day=days_in_month) + timedelta(days=1)).month}"},
        ],
        [{"text": "Пн", "callback_data": "noop"}, {"text": "Вт", "callback_data": "noop"},
         {"text": "Ср", "callback_data": "noop"}, {"text": "Чт", "callback_data": "noop"},
         {"text": "Пт", "callback_data": "noop"}, {"text": "Сб", "callback_data": "noop"},
         {"text": "Вс", "callback_data": "noop"}]
    ]

    week = []
    for _ in range(offset):
        week.append({"text": " ", "callback_data": "noop"})

    for day in range(1, days_in_month + 1):
        d = date_cls(view_date.year, view_date.month, day)
        if start and d == start and end and d == end:
            label = f"🟢{day:02d}"
        elif start and d == start:
            label = f"🟢{day:02d}"
        elif end and d == end:
            label = f"🔵{day:02d}"
        elif start and end and start < d < end:
            label = f"·{day:02d}·"
        else:
            label = f"{day:02d}"
        week.append({"text": label, "callback_data": f"dyncal|day|{d.isoformat()}|{view_date.year}|{view_date.month}"})
        if len(week) == 7:
            rows.append(week)
            week = []

    if week:
        while len(week) < 7:
            week.append({"text": " ", "callback_data": "noop"})
        rows.append(week)

    status = []
    if start:
        status.append(f"От: {start.strftime('%d.%m.%Y')}")
    if end:
        status.append(f"До: {end.strftime('%d.%m.%Y')}")
    if status:
        rows.append([{"text": " | ".join(status), "callback_data": "noop"}])

    if start and end:
        rows.append([{"text": "💾 Сохранить период", "callback_data": "dyncal|save"}])
    else:
        rows.append([{"text": "Выберите дату начала и дату окончания", "callback_data": "noop"}])

    rows.append([{"text": "✖️ Отмена", "callback_data": "dyncal|cancel"}])
    return {"inline_keyboard": rows}


def dynamic_period_prompt(start=None, end=None):
    text = (
        "📅 ВЫБОР ПЕРИОДА\n\n"
        "Выберите дату начала и дату окончания.\n"
        "Сначала нажмите первое число, затем второе.\n\n"
        "Можно переключать месяц и год кнопками сверху.\n"
        "Сравнение будет с теми же числами предыдущего месяца."
    )
    if start:
        text += f"\n\n🟢 Начало: {start.strftime('%d.%m.%Y')}"
    if end:
        text += f"\n🔵 Конец: {end.strftime('%d.%m.%Y')}"
    return text


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

    total = len(client_list)

    # Используем реальные поля YCLIENTS с датой последнего визита.
    # Уходящие: последний визит больше 30 дней, но меньше 90 дней назад.
    # Потерянные: последний визит 90 дней и более назад.
    leaving = []
    lost = []

    for client in client_list:
        try:
            visits = int(client.get("visits", 0) or 0)
        except Exception:
            visits = 0

        if not visits:
            continue

        last_visit = (
            parse_date(client.get("last_visit_date"))
            or parse_date(client.get("last_change_date"))
        )

        if not last_visit:
            continue

        days = (today - last_visit.date()).days

        if 30 < days < 90:
            leaving.append(client)
        elif days >= 90:
            lost.append(client)

    return (
        f"👥 КЛИЕНТЫ\n"
        f"🏪 {BRANCH_NAME}\n\n"
        f"Всего в базе: {number(total)}\n"
        f"🟡 Уходящие: {number(len(leaving))}\n"
        f"🔴 Потерянные: {number(len(lost))}"
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


def get_monthly_plan(plan_key, month_key):
    con = sqlite3.connect(DB)
    row = con.execute(
        "SELECT amount FROM monthly_plans WHERE plan_key=? AND month=?",
        (str(plan_key), month_key)
    ).fetchone()
    con.close()
    return float(row[0]) if row else None


def set_monthly_plan(plan_key, month_key, amount):
    con = sqlite3.connect(DB)
    con.execute(
        """INSERT INTO monthly_plans(plan_key, month, amount, updated_at)
           VALUES(?,?,?,?)
           ON CONFLICT(plan_key) DO UPDATE SET
             month=excluded.month,
             amount=excluded.amount,
             updated_at=excluded.updated_at""",
        (str(plan_key), month_key, float(amount), datetime.now(TZ).isoformat())
    )
    con.commit()
    con.close()


def reset_monthly_plan(plan_key, month_key):
    con = sqlite3.connect(DB)
    con.execute(
        "DELETE FROM monthly_plans WHERE plan_key=? AND month=?",
        (str(plan_key), month_key)
    )
    con.commit()
    con.close()


def plan_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "➕ Задать план", "callback_data": "plan:set"},
                {"text": "✏️ Изменить план", "callback_data": "plan:edit"},
            ],
            [{"text": "🗑 Сбросить план", "callback_data": "plan:reset"}],
            [{"text": "☰ Главное меню", "callback_data": "main_menu"}],
        ]
    }


def report_plan(plan_key="default"):
    today = datetime.now(TZ).date()
    month_start = today.replace(day=1)
    month_end = today.replace(day=calendar.monthrange(today.year, today.month)[1])
    month_key = today.strftime("%Y-%m")
    plan = get_monthly_plan(plan_key, month_key)

    if plan is None:
        return (
            f"🎯 ПЛАН / ФАКТ\n"
            f"🏪 {BRANCH_NAME}\n"
            f"📅 {month_start.strftime('%d.%m.%Y')} — {month_end.strftime('%d.%m.%Y')}\n\n"
            "План на текущий месяц не задан."
        )

    metrics = get_period(month_start, today)
    fact = float(metrics.get("revenue", 0) or 0)
    days_total = (month_end - month_start).days + 1
    days_passed = (today - month_start).days + 1
    remaining = max(plan - fact, 0)
    percent = (fact / plan * 100) if plan else 0
    days_left = max(days_total - days_passed, 0)
    per_day = (remaining / days_left) if days_left else 0

    return (
        f"🎯 ПЛАН / ФАКТ\n"
        f"🏪 {BRANCH_NAME}\n"
        f"📅 {month_start.strftime('%d.%m.%Y')} — {month_end.strftime('%d.%m.%Y')}\n\n"
        f"План: {money(plan)}\n"
        f"Факт: {money(fact)}\n"
        f"Выполнено: {percent:.1f}%\n"
        f"Осталось: {money(remaining)}\n"
        f"Прошло дней: {days_passed}\n"
        f"Нужно делать в день: {money(per_day)}"
    )


def build_analytics_context():
    """Собирает актуальный контекст из YCLIENTS для диалога аналитика."""
    today = datetime.now(TZ).date()
    month_start = today.replace(day=1)

    current = get_period(month_start, today)
    prev_end = month_start - timedelta(days=1)
    prev_start = prev_end.replace(day=1)
    previous = get_period(prev_start, prev_end)

    last30_start = today - timedelta(days=29)
    last30 = get_period(last30_start, today)

    def staff_lines(metrics):
        rows = []
        for master in metrics.get("staff", {}).values():
            rows.append(
                f"{master.get('name', 'Без мастера')}: "
                f"выручка={master.get('revenue', 0):.0f} ₽, "
                f"визиты={master.get('attended', 0)}, "
                f"неявки={master.get('noshow', 0)}, "
                f"клиенты={len(master.get('clients', set()))}"
            )
        return rows

    def service_lines(metrics):
        rows = []
        for name, item in sorted(
            metrics.get("services", {}).items(),
            key=lambda x: x[1].get("revenue", 0),
            reverse=True
        )[:20]:
            rows.append(
                f"{name}: количество={item.get('count', 0)}, "
                f"выручка={item.get('revenue', 0):.0f} ₽"
            )
        return rows

    context = [
        f"Филиал: {BRANCH_NAME}",
        f"Дата формирования: {today.isoformat()}",
        "",
        "ТЕКУЩИЙ МЕСЯЦ:",
        f"Период: {month_start} — {today}",
        f"Выручка: {current.get('revenue', 0):.0f} ₽",
        f"Клиенты: {current.get('clients_count', 0)}",
        f"Средний чек: {current.get('avg_check', 0):.0f} ₽",
        f"Записи: {current.get('records', 0)}",
        f"Состоявшиеся: {current.get('attended', 0)}",
        f"Неявки: {current.get('noshow', 0)}",
        f"Отмены: {current.get('cancelled', 0)}",
        "",
        "ПРОШЛЫЙ МЕСЯЦ:",
        f"Период: {prev_start} — {prev_end}",
        f"Выручка: {previous.get('revenue', 0):.0f} ₽",
        f"Клиенты: {previous.get('clients_count', 0)}",
        f"Средний чек: {previous.get('avg_check', 0):.0f} ₽",
        f"Записи: {previous.get('records', 0)}",
        f"Состоявшиеся: {previous.get('attended', 0)}",
        f"Неявки: {previous.get('noshow', 0)}",
        f"Отмены: {previous.get('cancelled', 0)}",
        "",
        "ПОСЛЕДНИЕ 30 ДНЕЙ:",
        f"Период: {last30_start} — {today}",
        f"Выручка: {last30.get('revenue', 0):.0f} ₽",
        f"Клиенты: {last30.get('clients_count', 0)}",
        f"Средний чек: {last30.get('avg_check', 0):.0f} ₽",
        f"Записи: {last30.get('records', 0)}",
        f"Состоявшиеся: {last30.get('attended', 0)}",
        f"Неявки: {last30.get('noshow', 0)}",
        f"Отмены: {last30.get('cancelled', 0)}",
        "",
        "МАСТЕРА — ПОСЛЕДНИЕ 30 ДНЕЙ:",
        *(staff_lines(last30) or ["Нет данных"]),
        "",
        "УСЛУГИ — ТЕКУЩИЙ МЕСЯЦ:",
        *(service_lines(current) or ["Нет данных"]),
    ]
    return "\n".join(context)



def clean_analyst_answer(answer):
    """Приводит ответ ИИ к читаемому обычному тексту для Telegram."""
    if not answer:
        return "Не удалось получить ответ."

    s = str(answer)

    # Убираем LaTeX-обёртки.
    s = s.replace(r"\[", "").replace(r"\]", "")
    s = s.replace(r"\(", "").replace(r"\)", "")
    s = s.replace("$$", "").replace("$", "")

    # Частые LaTeX-конструкции превращаем в обычные записи.
    def frac_repl(m):
        numerator = m.group(1).strip()
        denominator = m.group(2).strip()
        return f"{numerator} / {denominator}"

    # Несколько проходов для вложенных простых дробей.
    for _ in range(3):
        s = re.sub(r"\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}", frac_repl, s)

    s = re.sub(r"\\text\s*\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"\\mathrm\s*\{([^{}]*)\}", r"\1", s)
    s = s.replace(r"\times", "×")
    s = s.replace(r"\cdot", "·")
    s = s.replace(r"\approx", "≈")
    s = s.replace(r"\geq", "≥").replace(r"\leq", "≤")
    s = s.replace(r"\%", "%")
    s = s.replace(r"\,", " ")
    s = s.replace(r"\;", " ")
    s = s.replace(r"\rightarrow", "→")
    s = s.replace(r"\to", "→")
    s = s.replace(r"\pm", "±")
    s = s.replace(r"\div", "÷")

    # Убираем оставшиеся одиночные обратные слэши перед буквами,
    # чтобы Telegram не показывал служебный LaTeX.
    s = re.sub(r"\\([A-Za-z]+)", r"\1", s)

    # Markdown-звёздочки не нужны: Telegram отправляет обычный текст.
    s = re.sub(r"\*\*(.*?)\*\*", r"\1", s)
    s = re.sub(r"__(.*?)__", r"\1", s)
    s = re.sub(r"`([^`]*)`", r"\1", s)

    # Убираем LaTeX-остатки и лишние пустые строки.
    s = re.sub(r"^\s*[-=]{3,}\s*$", "", s, flags=re.M)
    s = re.sub(r"\n{3,}", "\n\n", s)
    s = re.sub(r"[ \t]{2,}", " ", s)

    # Приводим списки к аккуратному виду.
    s = re.sub(r"(?m)^\s*[-•]\s*", "• ", s)

    return s.strip()



def ai_log(message):
    """Надёжный лог для Render: сразу выводим в stdout/stderr."""
    print(f"[AI-ANALYTICS] {message}", file=sys.stderr, flush=True)
    try:
        app.logger.error("[AI-ANALYTICS] %s", message)
    except Exception:
        pass


def ask_groq(question, history=None):
    """Ответ аналитика через Groq API с контекстом YCLIENTS и историей диалога.
    Использует обычный Chat Completions API с коротким таймаутом и fallback-моделью.
    """
    if not GROQ_API_KEY:
        ai_log("GROQ_API_KEY отсутствует")
        return "⚠️ GROQ_API_KEY не задан в переменных окружения."

    # Получаем аналитику отдельно, чтобы ошибка YCLIENTS не маскировалась
    # под ошибку обращения к ИИ.
    try:
        context = build_analytics_context()
    except Exception as exc:
        ai_log(f"Analytics context error: {exc!r}")
        context = "Данные аналитики временно недоступны. Не выдумывай цифры."

    system_prompt = (
        "Ты — ИИ-аналитик управляющего барбершопа МЕТРО. "
        "Ты ведёшь полноценный диалог с управляющим. "
        "Отвечай непосредственно на каждый заданный вопрос, а не выдавай общий отчёт. "
        "Используй только актуальные данные YCLIENTS из контекста. "
        "Не выдумывай цифры, имена мастеров или причины. "
        "Если данных недостаточно, честно скажи об этом. "
        "Учитывай предыдущие сообщения диалога и отвечай на уточнения по смыслу. "
        "Отвечай по-русски, понятно и конкретно. "
        "Если вопрос требует сравнения, показывай конкретные цифры и процент изменения. "
        "Формат для Telegram: короткие абзацы и понятные списки, без таблиц. "
        "НИКОГДА не используй LaTeX и математические команды. "
        "Не пиши \\frac, \\text, \\[, \\], \\(, \\) или $...$. "
        "Формулы пиши обычным текстом, например: 359 400 ₽ / 429 = 837,5 ₽. "
        "Не начинай ответ с повторения всей аналитики. "
        "Сначала дай прямой ответ на вопрос, затем кратко объясни вывод "
        "и предложи конкретные действия."
    )

    messages = [
        {"role": "system", "content": system_prompt}
    ]

    for item in (history or [])[-8:]:
        if item.get("role") in ("user", "assistant") and item.get("content"):
            messages.append({
                "role": item["role"],
                "content": str(item["content"])[:5000]
            })

    messages.append({
        "role": "user",
        "content": (
            "АКТУАЛЬНАЯ АНАЛИТИКА YCLIENTS:\n"
            f"{context}\n\n"
            f"ВОПРОС УПРАВЛЯЮЩЕГО:\n{question}"
        )
    })

    # GPT-OSS требует reasoning и может дольше отвечать. Для Telegram-диалога
    # сначала используем более быструю стабильную модель, а GPT-OSS оставляем
    # резервом. Это предотвращает зависание webhook на долгом reasoning-запросе.
    models = [
        ("llama-3.3-70b-versatile", False),
        ("openai/gpt-oss-120b", True),
    ]

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    last_error = None

    for model, is_reasoning in models:
        payload = {
            "model": model,
            "messages": messages,
            "temperature": 0.2,
            "max_completion_tokens": 1400,
            "stream": False,
        }

        if is_reasoning:
            payload["reasoning_effort"] = "low"
            payload["include_reasoning"] = False

        ai_log(
            f"Groq: отправляю запрос model={model}, "
            f"question={question[:160]!r}"
        )

        try:
            # Раздельные таймауты: соединение не может зависнуть надолго,
            # а ответу даём достаточно времени.
            response = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=(8, 25),
            )
        except requests.Timeout as exc:
            last_error = f"timeout {model}: {exc!r}"
            ai_log(last_error)
            continue
        except requests.RequestException as exc:
            last_error = f"request error {model}: {exc!r}"
            ai_log(last_error)
            continue

        ai_log(f"Groq: HTTP {response.status_code}, model={model}")

        if response.status_code != 200:
            body = response.text[:2000]
            last_error = f"HTTP {response.status_code} {model}: {body}"
            ai_log(last_error)

            # Ошибку авторизации/лимита бессмысленно повторять другой моделью.
            if response.status_code in (401, 403, 429):
                break
            continue

        try:
            data = response.json()
        except ValueError as exc:
            last_error = f"invalid JSON from Groq: {exc!r}"
            ai_log(last_error)
            continue

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        answer = message.get("content") or ""

        if not answer:
            # GPT-OSS иногда отдаёт reasoning отдельно. При include_reasoning=False
            # это не должно происходить, но оставляем безопасную диагностику.
            ai_log(f"Groq empty content: {str(data)[:2000]}")
            last_error = f"empty content from {model}"
            continue

        ai_log(f"Groq: ответ получен, chars={len(answer)}")
        return clean_analyst_answer(answer)

    ai_log(f"Groq final error: {last_error!r}")
    if last_error and "HTTP 401" in last_error:
        return "⚠️ Groq отклонил API-ключ (401). Проверь GROQ_API_KEY в Render."
    if last_error and "HTTP 403" in last_error:
        return "⚠️ Groq запретил доступ для этого API-ключа (403)."
    if last_error and "HTTP 429" in last_error:
        return "⚠️ Groq сообщил о лимите запросов (429). Попробуйте немного позже."
    if last_error and "timeout" in last_error.lower():
        return "⚠️ Groq не успел ответить. Попробуйте ещё раз."
    return "⚠️ ИИ временно не смог ответить. Попробуйте ещё раз."

def analyst_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "🛑 Закончить диалог", "callback_data": "end_analyst"}]
        ]
    }


def analyst_start_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "🛑 Закончить диалог", "callback_data": "end_analyst"}]
        ]
    }


# -------------------- Меню --------------------

def main_menu():
    return {
        "inline_keyboard": [
            [{"text": "📊 Сегодня", "callback_data": "menu:сегодня"}],
            [{"text": "👨‍💼 Мастера", "callback_data": "menu:мастера"},
             {"text": "📈 Динамика", "callback_data": "menu:динамика"}],
            [{"text": "🧠 Анализ", "callback_data": "menu:анализ"},
             {"text": "👥 Клиенты", "callback_data": "menu:клиенты"}],
            [{"text": "💈 Услуги", "callback_data": "menu:услуги"},
             {"text": "📣 Маркетинг", "callback_data": "menu:маркетинг"}],
            [{"text": "🎯 План / факт", "callback_data": "menu:план"}],
            [{"text": "🤖 Спроси аналитика", "callback_data": "ask_analyst"}],
            [{"text": "ℹ️ Инструкция", "callback_data": "menu:help"}]
        ]
    }


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

    if text in ("/analitik", "спроси аналитика", "🤖 спроси аналитика"):
        return (
            "🤖 Режим аналитика включён.\n\n"
            "Задавайте вопросы — я буду вести диалог и отвечать "
            "на основе актуальной аналитики МЕТРО из YCLIENTS.",
            analyst_start_keyboard()
        )

    if text in ("/сегодня", "сегодня", "📊 сегодня"):
        return report_today(), None

    if text in ("/мастера", "мастера", "👨‍💼 мастера"):
        return report_masters(), None

    if text in ("/динамика", "динамика", "📈 динамика"):
        return (
            report_dynamics(),
            {
                "inline_keyboard": [
                    [{"text": "📅 Выбрать период", "callback_data": "dynamic_period"}],
                    [{"text": "☰ Главное меню", "callback_data": "main_menu"}],
                ]
            }
        )

    if text in ("/анализ", "анализ", "🧠 анализ"):
        return report_analysis(), None

    if text in ("/клиенты", "клиенты", "👥 клиенты"):
        return report_clients(), None

    if text in ("/услуги", "услуги", "💈 услуги"):
        return report_services(), None

    if text in ("/маркетинг", "маркетинг", "📣 маркетинг"):
        return report_marketing(), None

    if text in ("/план", "план / факт", "🎯 план / факт"):
        return report_plan(), plan_keyboard()

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

    # Inline-кнопка «☰ Главное меню» и кнопки меню
    callback = update.get("callback_query") or {}
    if callback:
        callback_id = callback.get("id")
        callback_message = callback.get("message") or {}
        callback_chat = callback_message.get("chat") or {}
        callback_chat_id = callback_chat.get("id")
        data = callback.get("data") or ""

        try:
            if callback_id and TG:
                requests.post(
                    f"{TG}/answerCallbackQuery",
                    json={"callback_query_id": callback_id},
                    timeout=10
                )

            if callback_chat_id and data == "main_menu":
                tg_send(callback_chat_id, "🤖 Главное меню\n\nВыбирай нужный раздел:", main_menu())
                return jsonify({"ok": True})

            if callback_chat_id and data == "ask_analyst":
                callback_user = callback.get("from") or {}
                callback_user_id = callback_user.get("id")
                session_key = f"{callback_chat_id}:{callback_user_id}"
                analytics_sessions[session_key] = []
                tg_send(
                    callback_chat_id,
                    "🤖 Режим аналитика включён.\n\n"
                    "Теперь все ваши сообщения идут только ИИ-аналитику. "
                    "Задавайте вопросы, уточняйте ответы и продолжайте диалог.\n\n"
                    "Чтобы выйти из диалога, нажмите «🛑 Закончить диалог».",
                    analyst_start_keyboard()
                )
                return jsonify({"ok": True})

            if callback_chat_id and data == "end_analyst":
                callback_user = callback.get("from") or {}
                callback_user_id = callback_user.get("id")
                session_key = f"{callback_chat_id}:{callback_user_id}"
                analytics_sessions.pop(session_key, None)
                tg_send(
                    callback_chat_id,
                    "🤖 Диалог с аналитиком завершён.\n\nВозвращаемся к обычному режиму.",
                    main_menu()
                )
                return jsonify({"ok": True})

            if callback_chat_id and data.startswith("plan:"):
                callback_user = callback.get("from") or {}
                callback_user_id = callback_user.get("id")
                plan_key = f"{callback_chat_id}:{callback_user_id}"
                action = data.split(":", 1)[1]
                today = datetime.now(TZ).date()
                month_key = today.strftime("%Y-%m")
                current_plan = get_monthly_plan(plan_key, month_key)

                if action in ("set", "edit"):
                    plan_states[plan_key] = True
                    tg_send(
                        callback_chat_id,
                        "🎯 Введите месячный план в рублях.\n\n"
                        "Например: 500000",
                        None
                    )
                    return jsonify({"ok": True})

                if action == "reset":
                    reset_monthly_plan(plan_key, month_key)
                    tg_send(
                        callback_chat_id,
                        "🗑 План на текущий месяц сброшен.",
                        plan_keyboard()
                    )
                    return jsonify({"ok": True})

            if callback_chat_id and data == "dynamic_period":
                callback_user = callback.get("from") or {}
                callback_user_id = callback_user.get("id")
                state_key = f"{callback_chat_id}:{callback_user_id}"
                state = dynamic_states.get(state_key, {})
                view = state.get("view") or datetime.now(TZ).date()
                start_date = state.get("start")
                end_date = state.get("end")
                tg_send(
                    callback_chat_id,
                    dynamic_period_prompt(start_date, end_date),
                    dynamic_period_keyboard(view, start_date, end_date)
                )
                return jsonify({"ok": True})

            if callback_chat_id and data.startswith("dyncal|"):
                callback_user = callback.get("from") or {}
                callback_user_id = callback_user.get("id")
                state_key = f"{callback_chat_id}:{callback_user_id}"
                parts = data.split("|")
                action = parts[1] if len(parts) > 1 else ""

                if action == "cancel":
                    dynamic_states.pop(state_key, None)
                    tg_send(
                        callback_chat_id,
                        report_dynamics(),
                        {
                            "inline_keyboard": [
                                [{"text": "📅 Выбрать период", "callback_data": "dynamic_period"}],
                                [{"text": "☰ Главное меню", "callback_data": "main_menu"}],
                            ]
                        }
                    )
                    return jsonify({"ok": True})

                state = dynamic_states.setdefault(
                    state_key,
                    {"view": datetime.now(TZ).date(), "start": None, "end": None}
                )

                if action == "save":
                    if not state.get("start") or not state.get("end"):
                        tg_send(
                            callback_chat_id,
                            "⚠️ Сначала выберите дату начала и дату окончания.",
                            dynamic_period_keyboard(state["view"], state.get("start"), state.get("end"))
                        )
                        return jsonify({"ok": True})

                    start_date = state["start"]
                    end_date = state["end"]
                    dynamic_states.pop(state_key, None)
                    tg_send(
                        callback_chat_id,
                        report_dynamics(start_date, end_date),
                        {
                            "inline_keyboard": [
                                [{"text": "📅 Изменить период", "callback_data": "dynamic_period"}],
                                [{"text": "☰ Главное меню", "callback_data": "main_menu"}],
                            ]
                        }
                    )
                    return jsonify({"ok": True})

                if action == "day":
                    chosen = datetime.strptime(parts[2], "%Y-%m-%d").date()
                    if not state.get("start") or state.get("end"):
                        state["start"] = chosen
                        state["end"] = None
                    else:
                        first = state["start"]
                        if chosen < first:
                            state["start"], state["end"] = chosen, first
                        else:
                            state["end"] = chosen
                    state["view"] = chosen
                    tg_send(
                        callback_chat_id,
                        dynamic_period_prompt(state.get("start"), state.get("end")),
                        dynamic_period_keyboard(state["view"], state.get("start"), state.get("end"))
                    )
                    return jsonify({"ok": True})

                if action in ("nav", "month"):
                    year = int(parts[2])
                    month = int(parts[3])
                    state["view"] = date_cls(year, month, 1)
                    tg_send(
                        callback_chat_id,
                        dynamic_period_prompt(state.get("start"), state.get("end")),
                        dynamic_period_keyboard(state["view"], state.get("start"), state.get("end"))
                    )
                    return jsonify({"ok": True})

                if action == "months":
                    year = int(parts[2])
                    month = int(parts[3])
                    state["view"] = date_cls(year, month, 1)
                    tg_send(
                        callback_chat_id,
                        dynamic_period_prompt(state.get("start"), state.get("end")),
                        dynamic_period_keyboard(state["view"], state.get("start"), state.get("end"), "months")
                    )
                    return jsonify({"ok": True})

                if action == "years":
                    year = int(parts[2])
                    month = int(parts[3])
                    state["view"] = date_cls(year, month, 1)
                    tg_send(
                        callback_chat_id,
                        dynamic_period_prompt(state.get("start"), state.get("end")),
                        dynamic_period_keyboard(state["view"], state.get("start"), state.get("end"), "years")
                    )
                    return jsonify({"ok": True})

            if data == "noop":
                return jsonify({"ok": True})

            if callback_chat_id and data.startswith("menu:"):
                menu_command = data.split(":", 1)[1]
                if menu_command == "динамика":
                    tg_send(
                        callback_chat_id,
                        report_dynamics(),
                        {
                            "inline_keyboard": [
                                [{"text": "📅 Выбрать период", "callback_data": "dynamic_period"}],
                                [{"text": "☰ Главное меню", "callback_data": "main_menu"}],
                            ]
                        }
                    )
                else:
                    if menu_command == "план":
                        answer, keyboard = report_plan(), plan_keyboard()
                    else:
                        answer, keyboard = route_message(menu_command)
                    tg_send(callback_chat_id, answer, keyboard)
                return jsonify({"ok": True})
        except Exception as exc:
            print("CALLBACK ERROR:", repr(exc))
        return jsonify({"ok": True})

    text = (message.get("text") or "").strip()

    if chat_id and text:
        try:
            user = message.get("from") or {}
            user_id = user.get("id")
            cid = f"{chat_id}:{user_id}"
            if cid in plan_states:
                plan_states.pop(cid, None)
                raw = text.replace(" ", "").replace(",", ".").replace("₽", "")
                try:
                    amount = float(raw)
                    if amount <= 0:
                        raise ValueError
                    set_monthly_plan(cid, datetime.now(TZ).date().strftime("%Y-%m"), amount)
                    tg_send(chat_id, report_plan(cid), plan_keyboard())
                except ValueError:
                    plan_states[cid] = True
                    tg_send(chat_id, "⚠️ Введите сумму плана числом, например: 500000", None)
                return jsonify({"ok": True})

            if cid in analytics_sessions:
                history = analytics_sessions[cid]
                answer = ask_groq(text, history)

                # Сохраняем историю текущего разговора, чтобы ИИ понимал уточнения.
                history.append({"role": "user", "content": text})
                history.append({"role": "assistant", "content": answer})
                analytics_sessions[cid] = history[-12:]

                tg_send(chat_id, answer, analyst_keyboard())
                return jsonify({"ok": True})

            if text.lower() in ("/analitik", "спроси аналитика", "🤖 спроси аналитика"):
                analytics_sessions[cid] = []
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
