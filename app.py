import os
import re
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

# ============================================================
# МЕТРО — Аналитик
# Филиал: Транссибирская 1
#
# Не используются администраторы.
# Существующие Render ENV не меняем.
# ============================================================

PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
YCLIENTS_COMPANY_ID = os.getenv("YCLIENTS_COMPANY_ID", "").strip()
YCLIENTS_USER_TOKEN = os.getenv("YCLIENTS_USER_TOKEN", "").strip()

# Оставляем поддержку уже существующей авторизации.
YCLIENTS_PARTNER_TOKEN = (
    os.getenv("YCLIENTS_PARTNER_TOKEN", "").strip()
    or os.getenv("YCLIENTS_PARTNER_ID", "").strip()
    or os.getenv("YCLIENTS_TOKEN_PARTNER", "").strip()
)

COMPANY_ID = int(YCLIENTS_COMPANY_ID or "0")
BRANCH_NAME = "МЕТРО — Транссибирская 1"
TZ = ZoneInfo("Asia/Omsk")

TG = (
    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    if TELEGRAM_BOT_TOKEN else ""
)
YC = "https://api.yclients.com/api/v1"

DB = "/tmp/metro_analytics.sqlite3"

chats = set()
chat_periods = {}
pending_custom_period = set()
lock = threading.Lock()


# ============================================================
# БАЗА
# ============================================================

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
        chats.add(str(row[0]))
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
                return datetime.strptime(
                    str(value)[:19], fmt
                ).replace(tzinfo=TZ)
            except Exception:
                pass
    return None


def date_from_ru(value):
    value = value.strip()
    for fmt in ("%d.%m.%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except Exception:
            pass
    return None


def period_label(start, end):
    if start == end:
        return start.strftime("%d.%m.%Y")
    return f"{start.strftime('%d.%m.%Y')} — {end.strftime('%d.%m.%Y')}"


def set_period(chat_id, start, end, label=None):
    if start > end:
        start, end = end, start
    chat_periods[str(chat_id)] = (
        start,
        end,
        label or period_label(start, end),
    )


def get_selected_period(chat_id):
    value = chat_periods.get(str(chat_id))
    if value:
        return value
    today = datetime.now(TZ).date()
    return today, today, "Сегодня"


def preset_period(chat_id, kind):
    today = datetime.now(TZ).date()

    if kind == "today":
        set_period(chat_id, today, today, "Сегодня")
    elif kind == "yesterday":
        d = today - timedelta(days=1)
        set_period(chat_id, d, d, "Вчера")
    elif kind == "7":
        set_period(
            chat_id,
            today - timedelta(days=6),
            today,
            "Последние 7 дней",
        )
    elif kind == "30":
        set_period(
            chat_id,
            today - timedelta(days=29),
            today,
            "Последние 30 дней",
        )
    elif kind == "month":
        set_period(
            chat_id,
            today.replace(day=1),
            today,
            "Текущий месяц",
        )


# ============================================================
# TELEGRAM
# ============================================================

def tg_send(chat_id, text, keyboard=None):
    if not TG:
        return False

    payload = {
        "chat_id": chat_id,
        "text": text,
    }
    if keyboard:
        payload["reply_markup"] = keyboard

    try:
        response = requests.post(
            f"{TG}/sendMessage",
            json=payload,
            timeout=30,
        )
        print("Telegram:", response.status_code, response.text[:500])
        return response.ok
    except Exception as exc:
        print("Telegram error:", repr(exc))
        return False


def broadcast(text):
    for chat_id in list(chats):
        tg_send(chat_id, text, main_menu())


# ============================================================
# YCLIENTS
# ============================================================

def yclients_headers():
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
        print(
            "YCLIENTS: missing COMPANY_ID, USER_TOKEN or PARTNER_TOKEN"
        )
        return None

    try:
        response = requests.get(
            f"{YC}/{path.lstrip('/')}",
            headers=yclients_headers(),
            params=params or {},
            timeout=30,
        )

        if not response.ok:
            print(
                "YCLIENTS:",
                response.status_code,
                response.text[:1000],
            )
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
    # YCLIENTS accepts date/time strings. End date is sent with the end
    # of the selected day so the whole requested period is included.
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
    return yclients_list(
        f"clients/{COMPANY_ID}",
        {"count": 100},
        100,
    )


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
            total += float(
                service.get(
                    "cost_to_pay",
                    service.get("cost", 0),
                ) or 0
            )
        except Exception:
            pass

    for good in record.get("goods_transactions") or []:
        try:
            total += float(
                good.get(
                    "cost",
                    good.get("amount", 0),
                ) or 0
            )
        except Exception:
            pass

    return total


def record_duration_minutes(record):
    for key in (
        "seance_length",
        "duration",
        "service_duration",
        "length",
    ):
        try:
            value = int(record.get(key) or 0)
            if value > 0:
                return value // 60 if value > 60 else value
        except Exception:
            pass

    total = 0
    for service in record.get("services") or []:
        for key in ("seance_length", "duration", "length"):
            try:
                value = int(service.get(key) or 0)
                if value > 0:
                    total += value // 60 if value > 60 else value
                    break
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
                            service.get("cost", 0),
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
    return calculate(get_records(start, end))


# ============================================================
# ОТЧЁТЫ
# ============================================================

def report_today():
    today = datetime.now(TZ).date()
    current = get_period(today, today)
    previous = get_period(
        today - timedelta(days=1),
        today - timedelta(days=1),
    )

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


def report_period(chat_id):
    start, end, label = get_selected_period(chat_id)
    metrics = get_period(start, end)

    return (
        "📅 АНАЛИТИКА ЗА ПЕРИОД\n"
        f"🏪 {BRANCH_NAME}\n"
        f"🗓 {label}\n\n"
        f"💰 Выручка: {money(metrics['revenue'])}\n"
        f"👥 Клиенты: {number(metrics['clients_count'])}\n"
        f"🧾 Средний чек: {money(metrics['avg_check'])}\n"
        f"📅 Записи: {number(metrics['records'])}\n"
        f"✅ Состоявшиеся: {number(metrics['attended'])}\n"
        f"❌ Неявки: {number(metrics['noshow'])}\n"
        f"🚫 Отмены: {number(metrics['cancelled'])}"
    )


def report_masters(chat_id):
    start, end, label = get_selected_period(chat_id)
    metrics = get_period(start, end)

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
        "👨‍💼 МАСТЕРА",
        f"🏪 {BRANCH_NAME}",
        f"🗓 {label}",
        "",
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


def report_master_load(chat_id, question=""):
    start, end, label = get_selected_period(chat_id)
    metrics = get_period(start, end)

    masters = [
        m for m in metrics["staff"].values()
        if m["name"] != "Без мастера"
    ]

    if not masters:
        return (
            "🧠 АНАЛИЗ ЗАГРУЗКИ МАСТЕРОВ\n"
            f"🏪 {BRANCH_NAME}\n"
            f"🗓 {label}\n\n"
            "Нет данных по мастерам за выбранный период."
        )

    # Для запроса без конкретного имени показываем причины по каждому
    # мастеру относительно среднего по филиалу.
    avg_attended = sum(m["attended"] for m in masters) / len(masters)
    avg_records = sum(m["records"] for m in masters) / len(masters)
    avg_noshow = sum(m["noshow"] for m in masters) / len(masters)
    avg_revenue = sum(m["revenue"] for m in masters) / len(masters)

    # Если в вопросе есть имя мастера — сначала пытаемся найти его.
    q = question.lower()
    selected = [
        m for m in masters
        if m["name"].lower() in q
    ]

    target = selected[0] if selected else None

    def one_master(master):
        reasons = []
        attended = master["attended"]
        records = master["records"]
        noshow = master["noshow"]

        if attended < avg_attended:
            reasons.append(
                f"состоявшихся визитов меньше среднего "
                f"({attended} против {avg_attended:.1f})"
            )
        if records < avg_records:
            reasons.append(
                f"записей меньше среднего "
                f"({records} против {avg_records:.1f})"
            )
        if noshow > avg_noshow and noshow > 0:
            reasons.append(
                f"много неявок ({noshow}, среднее {avg_noshow:.1f})"
            )
        if master["revenue"] < avg_revenue:
            reasons.append(
                f"выручка ниже среднего "
                f"({money(master['revenue'])} против "
                f"{money(avg_revenue)})"
            )

        conversion = (
            attended / records * 100 if records else 0
        )

        if not reasons:
            reasons.append(
                "по доступным данным явной причины низкой загрузки "
                "не видно"
            )

        return (
            f"💈 {master['name']}\n"
            f"💰 {money(master['revenue'])}\n"
            f"📅 Записи: {records}\n"
            f"✅ Визиты: {attended}\n"
            f"❌ Неявки: {noshow}\n"
            f"📊 Доля состоявшихся записей: {percent(conversion)}\n"
            "⚠️ Причины: "
            + "; ".join(reasons)
        )

    if target:
        return (
            "🧠 ПОЧЕМУ НИЗКАЯ ЗАГРУЗКА\n"
            f"🏪 {BRANCH_NAME}\n"
            f"🗓 {label}\n\n"
            + one_master(target)
            + "\n\n"
            "💡 Для более точного ответа можно спросить:\n"
            "«Почему низкая загрузка у Дмитрия?»"
        )

    # Сортируем от самой низкой фактической загрузки к высокой.
    masters.sort(key=lambda m: (m["attended"], m["revenue"]))

    lines = [
        "🧠 ПОЧЕМУ У МАСТЕРОВ НИЗКАЯ ЗАГРУЗКА",
        f"🏪 {BRANCH_NAME}",
        f"🗓 {label}",
        "",
        "Сравниваю мастеров между собой за выбранный период.",
        "",
    ]

    for master in masters:
        lines.append(one_master(master))
        lines.append("")

    lines.append(
        "💡 Главные причины определяются по доступным данным "
        "YCLIENTS: количество записей, состоявшиеся визиты, "
        "неявки и выручка."
    )
    return "\n".join(lines)


def report_dynamics(chat_id):
    start, end, label = get_selected_period(chat_id)
    current = get_period(start, end)

    days = (end - start).days + 1
    previous_end = start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=days - 1)
    previous = get_period(previous_start, previous_end)

    return (
        "📈 ДИНАМИКА\n"
        f"🏪 {BRANCH_NAME}\n"
        f"🗓 Текущий период: {label}\n"
        f"🗓 Предыдущий: {period_label(previous_start, previous_end)}\n\n"
        f"💰 Выручка: {change(current['revenue'], previous['revenue'])}\n"
        f"👥 Клиенты: {change(current['clients_count'], previous['clients_count'])}\n"
        f"🧾 Средний чек: {change(current['avg_check'], previous['avg_check'])}\n"
        f"📅 Записи: {change(current['records'], previous['records'])}\n"
        f"✅ Визиты: {change(current['attended'], previous['attended'])}\n"
        f"❌ Неявки: {change(current['noshow'], previous['noshow'])}"
    )


def report_analysis(chat_id):
    start, end, label = get_selected_period(chat_id)
    current = get_period(start, end)

    days = (end - start).days + 1
    previous_end = start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=days - 1)
    previous = get_period(previous_start, previous_end)

    observations = []

    if current["revenue"] > previous["revenue"]:
        observations.append("🟢 Выручка выше предыдущего периода.")
    elif current["revenue"] < previous["revenue"]:
        observations.append("🔴 Выручка ниже предыдущего периода.")

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
        f"🗓 {label}\n\n"
        f"💰 Выручка: {money(current['revenue'])}\n"
        f"✅ Визиты: {current['attended']}\n"
        f"🧾 Средний чек: {money(current['avg_check'])}\n\n"
        + "\n".join(observations)
    )


def report_clients():
    client_list = get_clients()
    active = [
        c for c in client_list
        if int(c.get("visits", 0) or 0) > 0
    ]

    leaving = []
    lost = []
    today = datetime.now(TZ).date()

    for client in active:
        last_visit = (
            parse_date(client.get("last_change_date"))
            or parse_date(client.get("last_visit_date"))
        )
        if not last_visit:
            continue

        days = (today - last_visit.date()).days

        if days >= 90:
            lost.append(client)
        elif days >= 45:
            leaving.append(client)

    return (
        f"👥 КЛИЕНТЫ\n"
        f"🏪 {BRANCH_NAME}\n\n"
        f"Всего в базе: {len(client_list)}\n"
        f"С историей визитов: {len(active)}\n"
        f"🟡 Уходящие: {len(leaving)}\n"
        f"🔴 Потерянные: {len(lost)}"
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
        "🔄 КОГО НУЖНО ВЕРНУТЬ",
        f"🏪 {BRANCH_NAME}",
        "",
        f"🟡 Уходящие: {len(leaving)}",
        f"🔴 Потерянные: {len(lost)}",
        "",
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


def report_finances(chat_id):
    start, end, label = get_selected_period(chat_id)
    metrics = get_period(start, end)

    return (
        "💰 ФИНАНСЫ\n"
        f"🏪 {BRANCH_NAME}\n"
        f"🗓 {label}\n\n"
        f"Выручка: {money(metrics['revenue'])}\n"
        f"Состоявшиеся визиты: {metrics['attended']}\n"
        f"Средний чек: {money(metrics['avg_check'])}\n"
        f"Записи: {metrics['records']}\n"
        f"Неявки: {metrics['noshow']}\n"
        f"Отмены: {metrics['cancelled']}"
    )


def report_services(chat_id):
    start, end, label = get_selected_period(chat_id)
    metrics = get_period(start, end)

    services = sorted(
        metrics["services"].items(),
        key=lambda item: item[1]["revenue"],
        reverse=True,
    )

    lines = [
        "💈 УСЛУГИ",
        f"🏪 {BRANCH_NAME}",
        f"🗓 {label}",
        "",
    ]

    if not services:
        return "\n".join(lines + ["Нет данных по услугам."])

    for index, (name, service) in enumerate(services[:20], 1):
        lines.append(
            f"{index}. {name} — "
            f"{service['count']} | {money(service['revenue'])}"
        )

    return "\n".join(lines)


def report_marketing(chat_id):
    start, end, label = get_selected_period(chat_id)
    metrics = get_period(start, end)

    sources = sorted(
        metrics["sources"].items(),
        key=lambda item: item[1],
        reverse=True,
    )

    lines = [
        "📣 МАРКЕТИНГ",
        f"🏪 {BRANCH_NAME}",
        f"🗓 {label}",
        "",
    ]

    if not sources:
        return "\n".join(
            lines + [
                "Источники не заполнены в доступных данных YCLIENTS."
            ]
        )

    for source, count in sources:
        lines.append(f"• {source}: {count}")

    return "\n".join(lines)


def report_plan():
    return (
        "🎯 ПЛАН / ФАКТ\n"
        f"🏪 {BRANCH_NAME}\n\n"
        "План филиала пока не задан.\n\n"
        "После задания месячного плана бот будет считать:\n"
        "• факт;\n"
        "• % выполнения;\n"
        "• остаток;\n"
        "• необходимую дневную выручку."
    )


# ============================================================
# МЕНЮ И ПЕРИОД
# ============================================================

def main_menu():
    return {
        "keyboard": [
            [{"text": "📊 Сегодня"}, {"text": "📅 Период"}],
            [{"text": "🧠 Анализ"}, {"text": "👨‍💼 Мастера"}],
            [{"text": "📈 Динамика"}, {"text": "👥 Клиенты"}],
            [{"text": "🔄 Вернуть клиентов"}, {"text": "💰 Финансы"}],
            [{"text": "💈 Услуги"}, {"text": "📣 Маркетинг"}],
            [{"text": "🎯 План / факт"}, {"text": "🤖 Спроси аналитика"}],
            [{"text": "☀️ Утро"}, {"text": "🌙 Вечер"}],
            [{"text": "ℹ️ Инструкция"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def period_menu():
    return {
        "keyboard": [
            [{"text": "📅 Сегодня"}, {"text": "◀️ Вчера"}],
            [{"text": "7️⃣ Последние 7 дней"}, {"text": "3️⃣0️⃣ Последние 30 дней"}],
            [{"text": "🗓 Текущий месяц"}],
            [{"text": "✏️ Произвольный период"}],
            [{"text": "⬅️ Главное меню"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def instruction():
    return (
        "ℹ️ ИНСТРУКЦИЯ\n\n"
        "🤖 МЕТРО — Аналитик\n"
        f"🏪 {BRANCH_NAME}\n\n"
        "📅 Сначала выбери период кнопкой «📅 Период».\n"
        "После этого «Мастера», «Финансы», «Услуги», "
        "«Динамика» и другие отчёты покажут данные именно "
        "за выбранный период.\n\n"
        "✏️ Для произвольного периода напиши, например:\n"
        "01.08.2026 — 27.08.2026\n\n"
        "🤖 Можно задавать вопросы обычным языком:\n"
        "• Почему у мастеров низкая загрузка?\n"
        "• Почему у Дмитрия низкая загрузка?\n"
        "• Кто из мастеров хуже за 2 недели?\n"
        "• Почему упала выручка?\n"
        "• Кого нужно вернуть?\n"
        "• Сравни выбранный период с предыдущим."
    )


def parse_custom_period(text):
    # Поддерживаем: 01.08.2026 - 27.08.2026
    # и: с 01.08.2026 по 27.08.2026
    matches = re.findall(
        r"\b\d{2}[.\-]\d{2}[.\-]\d{4}\b",
        text,
    )
    if len(matches) >= 2:
        start = date_from_ru(matches[0])
        end = date_from_ru(matches[1])
        if start and end:
            return start, end
    return None


# ============================================================
# ОБРАБОТКА ЗАПРОСОВ
# ============================================================

def route_message(chat_id, text):
    raw = (text or "").strip()
    lower = raw.lower()

    # Ввод произвольного периода.
    if str(chat_id) in pending_custom_period:
        custom = parse_custom_period(raw)
        if custom:
            pending_custom_period.discard(str(chat_id))
            set_period(
                chat_id,
                custom[0],
                custom[1],
                period_label(custom[0], custom[1]),
            )
            return (
                "✅ Период установлен\n\n"
                f"🗓 {period_label(custom[0], custom[1])}\n\n"
                "Теперь любой отчёт из меню будет строиться "
                "за этот период.",
                main_menu(),
            )
        return (
            "✏️ Не понял даты.\n\n"
            "Напиши так:\n"
            "01.08.2026 — 27.08.2026",
            period_menu(),
        )

    if lower in ("/start", "start"):
        today = datetime.now(TZ).date()
        set_period(chat_id, today, today, "Сегодня")
        return (
            "🤖 МЕТРО — Аналитик\n\n"
            f"🏪 {BRANCH_NAME}\n\n"
            "Выбирай отчёт кнопками или задавай вопрос "
            "обычным языком.",
            main_menu(),
        )

    if lower in ("/help", "инструкция", "ℹ️ инструкция"):
        return instruction(), main_menu()

    if lower in ("📅 период", "/период", "период"):
        return (
            "📅 ВЫБЕРИ ПЕРИОД\n\n"
            "После выбора периода все основные отчёты "
            "будут строиться за него.",
            period_menu(),
        )

    if lower in ("📅 сегодня", "сегодня", "/сегодня"):
        preset_period(chat_id, "today")
        return report_period(chat_id), main_menu()

    if lower in ("◀️ вчера", "вчера"):
        preset_period(chat_id, "yesterday")
        return report_period(chat_id), main_menu()

    if lower in ("7️⃣ последние 7 дней", "последние 7 дней", "7 дней"):
        preset_period(chat_id, "7")
        return report_period(chat_id), main_menu()

    if lower in ("3️⃣0️⃣ последние 30 дней", "последние 30 дней", "30 дней"):
        preset_period(chat_id, "30")
        return report_period(chat_id), main_menu()

    if lower in ("🗓 текущий месяц", "текущий месяц"):
        preset_period(chat_id, "month")
        return report_period(chat_id), main_menu()

    if lower in ("✏️ произвольный период", "произвольный период"):
        pending_custom_period.add(str(chat_id))
        return (
            "✏️ Введи начало и конец периода.\n\n"
            "Например:\n"
            "01.08.2026 — 27.08.2026",
            period_menu(),
        )

    if lower in ("⬅️ главное меню", "главное меню"):
        return "Главное меню.", main_menu()

    if lower in ("👨‍💼 мастера", "/мастера", "мастера"):
        return report_masters(chat_id), main_menu()

    if lower in ("📈 динамика", "/динамика", "динамика"):
        return report_dynamics(chat_id), main_menu()

    if lower in ("🧠 анализ", "/анализ", "анализ"):
        return report_analysis(chat_id), main_menu()

    if lower in ("👥 клиенты", "/клиенты", "клиенты"):
        return report_clients(), main_menu()

    if lower in (
        "🔄 вернуть клиентов",
        "/вернуть",
        "вернуть клиентов",
        "кого вернуть",
    ):
        return report_return(), main_menu()

    if lower in ("💰 финансы", "/финансы", "финансы"):
        return report_finances(chat_id), main_menu()

    if lower in ("💈 услуги", "/услуги", "услуги"):
        return report_services(chat_id), main_menu()

    if lower in ("📣 маркетинг", "/маркетинг", "маркетинг"):
        return report_marketing(chat_id), main_menu()

    if lower in ("🎯 план / факт", "/план", "план / факт"):
        return report_plan(), main_menu()

    if lower in ("📊 сегодня",):
        return report_today(), main_menu()

    if lower in ("☀️ утро", "утро", "/утро"):
        return "☀️ ДОБРОЕ УТРО\n\n" + report_today(), main_menu()

    if lower in ("🌙 вечер", "вечер", "/вечер"):
        return "🌙 ВЕЧЕРНИЙ ОТЧЁТ\n\n" + report_today(), main_menu()

    # --------------------------------------------------------
    # Естественный язык.
    # ВАЖНО: запрос про низкую загрузку проверяется ДО
    # общего правила "мастер -> показать рейтинг".
    # --------------------------------------------------------

    load_words = (
        "загрузк",
        "загружен",
        "свободн",
        "мало запис",
        "мало визит",
    )

    if any(word in lower for word in load_words):
        return report_master_load(chat_id, raw), main_menu()

    if (
        "мастер" in lower
        or "рейтинг" in lower
        or "кто лучше" in lower
        or "кто хуже" in lower
    ):
        return report_masters(chat_id), main_menu()

    if (
        "почему" in lower
        or "что происходит" in lower
        or "проблем" in lower
    ):
        return report_analysis(chat_id), main_menu()

    if (
        "вернут" in lower
        or "потерян" in lower
        or "уходящ" in lower
    ):
        return report_return(), main_menu()

    if "финанс" in lower or "выруч" in lower:
        return report_finances(chat_id), main_menu()

    if "услуг" in lower:
        return report_services(chat_id), main_menu()

    if "маркет" in lower or "источник" in lower:
        return report_marketing(chat_id), main_menu()

    if "сравни" in lower or "динамик" in lower:
        return report_dynamics(chat_id), main_menu()

    if "клиент" in lower:
        return report_clients(), main_menu()

    # Если пользователь прямо написал две даты без режима
    # произвольного периода — тоже принимаем их.
    custom = parse_custom_period(raw)
    if custom:
        set_period(
            chat_id,
            custom[0],
            custom[1],
            period_label(custom[0], custom[1]),
        )
        return report_period(chat_id), main_menu()

    return (
        "🤖 Не совсем понял запрос.\n\n"
        "Попробуй:\n"
        "«Почему у мастеров низкая загрузка?»\n"
        "«Почему у Дмитрия низкая загрузка?»\n"
        "«Кто из мастеров хуже за 2 недели?»\n"
        "или выбери «📅 Период».",
        main_menu(),
    )


# ============================================================
# TELEGRAM WEBHOOK
# ============================================================

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
            answer, keyboard = route_message(chat_id, text)
            tg_send(chat_id, answer, keyboard)
        except Exception as exc:
            print("BOT ERROR:", repr(exc))
            tg_send(
                chat_id,
                "❌ Не удалось сформировать отчёт.\n"
                f"Ошибка: {exc}",
                main_menu(),
            )

    return jsonify({"ok": True})


# ============================================================
# YCLIENTS WEBHOOK
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

    con = sqlite3.connect(DB)
    con.execute(
        "INSERT INTO webhook_events(received_at, payload) VALUES(?, ?)",
        (
            datetime.now(TZ).isoformat(),
            str(payload),
        ),
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


# ============================================================
# ЗАПУСК
# ============================================================

init_db()


def configure_telegram():
    if not TG or not PUBLIC_URL:
        print(
            "Telegram webhook not configured: "
            "missing token or PUBLIC_URL"
        )
        return

    try:
        webhook_url = f"{PUBLIC_URL}/telegram/webhook"

        response = requests.post(
            f"{TG}/setWebhook",
            json={
                "url": webhook_url,
                "drop_pending_updates": False,
            },
            timeout=20,
        )

        print(
            "Telegram webhook:",
            response.status_code,
            response.text[:500],
        )
    except Exception as exc:
        print("Webhook setup error:", repr(exc))


configure_telegram()

threading.Thread(
    target=scheduler,
    daemon=True,
).start()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(
        host="0.0.0.0",
        port=port,
    )
