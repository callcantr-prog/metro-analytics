import os
import re
import json
import time
import sqlite3
import logging
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from collections import Counter, defaultdict

import requests
from flask import Flask, jsonify, request

# МЕТРО — Аналитик
# ВАЖНО: во всех URL проекта используется metro-analytics (с 'a' после anal).

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("metro-analytics")

TZ = ZoneInfo(os.getenv("APP_TIMEZONE", "Asia/Omsk"))
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
YCLIENTS_COMPANY_ID = os.getenv("YCLIENTS_COMPANY_ID", "").strip()
YCLIENTS_COMPANY_IDS = [x.strip() for x in os.getenv("YCLIENTS_COMPANY_IDS", YCLIENTS_COMPANY_ID).split(",") if x.strip()]
YCLIENTS_USER_TOKEN = os.getenv("YCLIENTS_USER_TOKEN", "").strip()
YCLIENTS_PARTNER_TOKEN = os.getenv("YCLIENTS_PARTNER_TOKEN", "").strip()
YCLIENTS_AUTHORIZATION = os.getenv("YCLIENTS_AUTHORIZATION", "").strip()
YCLIENTS_WEBHOOK_SECRET = os.getenv("YCLIENTS_WEBHOOK_SECRET", "").strip()
MORNING_REPORT_TIME = os.getenv("MORNING_REPORT_TIME", "09:00")
EVENING_REPORT_TIME = os.getenv("EVENING_REPORT_TIME", "21:00")
DB_PATH = os.getenv("DB_PATH", "metro_analytics.sqlite3")

API_BASE = "https://api.yclients.com/api/v1"
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else ""
_known_chats = set()
_lock = threading.Lock()


def now():
    return datetime.now(TZ)


def now_text():
    return now().strftime("%d.%m.%Y %H:%M:%S")


def db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS webhook_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        received_at TEXT NOT NULL,
        company_id TEXT,
        resource TEXT,
        resource_id TEXT,
        status TEXT,
        payload TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS report_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        report_type TEXT,
        sent_at TEXT,
        body TEXT
    );
    CREATE TABLE IF NOT EXISTS bot_chats (
        chat_id TEXT PRIMARY KEY,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL
    );
    """)
    con.commit()
    con.close()


init_db()


def save_chat(chat_id):
    if not chat_id:
        return
    with _lock:
        _known_chats.add(str(chat_id))
    con = db()
    ts = now_text()
    con.execute("""INSERT INTO bot_chats(chat_id,first_seen,last_seen) VALUES(?,?,?)
                   ON CONFLICT(chat_id) DO UPDATE SET last_seen=excluded.last_seen""", (str(chat_id), ts, ts))
    con.commit(); con.close()


def telegram_send(chat_id, text):
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        return False
    # Telegram has a 4096 character limit.
    chunks = [text[i:i+3900] for i in range(0, len(text), 3900)] or [""]
    try:
        for chunk in chunks:
            r = requests.post(f"{TELEGRAM_API}/sendMessage", json={
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
            }, timeout=25)
            if not r.ok:
                log.error("Telegram error %s: %s", r.status_code, r.text[:500])
                return False
        return True
    except requests.RequestException:
        log.exception("Telegram request failed")
        return False


def notify_telegram(text):
    targets = set()
    if TELEGRAM_CHAT_ID:
        targets.add(TELEGRAM_CHAT_ID)
    with _lock:
        targets.update(_known_chats)
    con = db()
    targets.update(r[0] for r in con.execute("SELECT chat_id FROM bot_chats"))
    con.close()
    return sum(1 for chat in targets if telegram_send(chat, text))


def auth_header():
    if YCLIENTS_AUTHORIZATION:
        return YCLIENTS_AUTHORIZATION
    # Official YCLIENTS API accepts partner token and, where required, user token.
    # We also support the currently configured USER_TOKEN as a fallback.
    partner = YCLIENTS_PARTNER_TOKEN or YCLIENTS_USER_TOKEN
    if not partner:
        return ""
    if YCLIENTS_USER_TOKEN and YCLIENTS_PARTNER_TOKEN:
        return f"Bearer {partner}, User {YCLIENTS_USER_TOKEN}"
    return f"Bearer {partner}"


def yc_headers():
    h = {
        "Accept": "application/vnd.yclients.v2+json",
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
    }
    if auth_header():
        h["Authorization"] = auth_header()
    return h


def yc_request(method, path, params=None, json_body=None, timeout=30):
    url = API_BASE + path
    r = requests.request(method, url, headers=yc_headers(), params=params, json=json_body, timeout=timeout)
    if not r.ok:
        raise RuntimeError(f"YCLIENTS {r.status_code}: {r.text[:800]}")
    try:
        return r.json()
    except Exception:
        return {"success": True, "data": r.text}


def data_part(payload):
    if isinstance(payload, dict):
        d = payload.get("data")
        if isinstance(d, list): return d
        if isinstance(d, dict): return d
    return payload if isinstance(payload, list) else []


def fetch_pages(path, params=None, method="GET", body=None, max_pages=100):
    params = dict(params or {})
    out = []
    for page in range(1, max_pages + 1):
        p = dict(params)
        p.setdefault("page", page)
        p.setdefault("count", 200)
        payload = yc_request(method, path, params=p if method == "GET" else None,
                             json_body=(body if method != "GET" and page == 1 else None))
        rows = data_part(payload)
        if isinstance(rows, dict): rows = [rows]
        if not rows:
            break
        out.extend(rows)
        meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
        total = meta.get("total_count")
        if total is not None and len(out) >= int(total):
            break
        if len(rows) < 200:
            break
    return out


def company_ids():
    if not YCLIENTS_COMPANY_IDS:
        raise RuntimeError("Не задан YCLIENTS_COMPANY_ID")
    return YCLIENTS_COMPANY_IDS


def fetch_records(cid, start, end):
    # YCLIENTS records endpoint supports start_date/end_date filters.
    return fetch_pages(f"/records/{cid}", {"start_date": start, "end_date": end})


def fetch_clients(cid):
    return fetch_pages(f"/clients/{cid}")


def fetch_staff(cid):
    return fetch_pages(f"/staff/{cid}")


def fetch_services(cid):
    return fetch_pages(f"/services/{cid}")


def money(v):
    try: return float(v or 0)
    except Exception: return 0.0


def record_dt(r):
    raw = r.get("datetime") or r.get("date") or r.get("create_date") or ""
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw, tz=TZ)
    s = str(raw).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        return dt.astimezone(TZ) if dt.tzinfo else dt.replace(tzinfo=TZ)
    except Exception:
        return None


def record_amount(r):
    total = 0.0
    for s in r.get("services") or []:
        total += money(s.get("cost") if s.get("cost") is not None else s.get("manual_cost")) * money(s.get("amount", 1))
    for g in r.get("goods_transactions") or []:
        total += money(g.get("cost") or g.get("price") or g.get("amount"))
    # Finance/sale fields can contain the final paid amount.
    for key in ("paid", "paid_amount", "amount", "sum"):
        if r.get(key) is not None and not r.get("services"):
            total = max(total, money(r.get(key)))
    return total


def attended(r):
    return r.get("attendance") == 1 or r.get("visit_attendance") == 1 or r.get("status") in ("attended", "completed")


def cancelled(r):
    status = str(r.get("status", "")).lower()
    return bool(r.get("deleted")) or status in {"cancelled", "canceled", "deleted"}


def noshow(r):
    return r.get("attendance") == -1 or r.get("visit_attendance") == -1


def client_name(r):
    c = r.get("client") or {}
    return c.get("name") or r.get("client_name") or "Без имени"


def staff_name(r):
    s = r.get("staff") or {}
    return s.get("name") or r.get("staff_name") or "Без мастера"


def service_names(r):
    return [str(x.get("title") or x.get("name") or "Услуга") for x in (r.get("services") or [])]


def period_from_text(text):
    t = text.lower()
    today = now().date()
    if "позавчера" in t:
        d = today - timedelta(days=2); return d, d
    if "вчера" in t:
        d = today - timedelta(days=1); return d, d
    if "сегодня" in t or "сейчас" in t:
        return today, today
    if "завтра" in t:
        d = today + timedelta(days=1); return d, d
    if "недел" in t or "7 дн" in t:
        return today - timedelta(days=6), today
    if "месяц" in t:
        first = today.replace(day=1); return first, today
    if "год" in t:
        return today.replace(month=1, day=1), today
    m = re.search(r"за\s+(\d+)\s+дн", t)
    if m:
        n = max(1, int(m.group(1))); return today - timedelta(days=n-1), today
    return today, today


def load_period(text):
    start, end = period_from_text(text)
    all_records = []
    errors = []
    for cid in company_ids():
        try:
            rows = fetch_records(cid, start.isoformat(), (end + timedelta(days=1)).isoformat())
            for r in rows:
                r = dict(r); r["_company_id"] = cid
                all_records.append(r)
        except Exception as e:
            errors.append(f"филиал {cid}: {e}")
    return start, end, all_records, errors


def aggregate(records):
    total = sum(record_amount(r) for r in records if not cancelled(r))
    attended_count = sum(attended(r) for r in records)
    no_show_count = sum(noshow(r) for r in records)
    cancel_count = sum(cancelled(r) for r in records)
    unique_clients = {str((r.get("client") or {}).get("id")) for r in records if (r.get("client") or {}).get("id") is not None}
    new_clients = set()
    staff = defaultdict(lambda: {"visits": 0, "revenue": 0.0})
    services = defaultdict(lambda: {"count": 0, "revenue": 0.0})
    for r in records:
        if cancelled(r): continue
        if attended(r):
            n = staff_name(r); staff[n]["visits"] += 1; staff[n]["revenue"] += record_amount(r)
        for s in r.get("services") or []:
            name = str(s.get("title") or s.get("name") or "Услуга")
            services[name]["count"] += 1
            services[name]["revenue"] += money(s.get("cost") if s.get("cost") is not None else s.get("manual_cost")) * money(s.get("amount", 1))
        c = r.get("client") or {}
        if c.get("id") is not None and (c.get("visits") == 1 or c.get("success_visits_count") == 1):
            new_clients.add(str(c.get("id")))
    repeat = max(0, len(unique_clients) - len(new_clients)) if unique_clients else 0
    return {
        "revenue": total, "records": len(records), "attended": attended_count,
        "no_show": no_show_count, "cancelled": cancel_count,
        "clients": len(unique_clients), "new_clients": len(new_clients),
        "repeat_clients": repeat, "staff": staff, "services": services,
    }


def fmt_report(title, start, end, records, errors=None):
    a = aggregate(records)
    lines = [f"📊 {title}", f"Период: {start.strftime('%d.%m.%Y')} — {end.strftime('%d.%m.%Y')}", "",
             f"💰 Выручка по записям: {a['revenue']:,.0f} ₽".replace(",", " "),
             f"📅 Записей: {a['records']}", f"✅ Пришли: {a['attended']}",
             f"❌ Не пришли: {a['no_show']}", f"🚫 Отменено/удалено: {a['cancelled']}",
             f"👥 Клиентов: {a['clients']}", f"🆕 Новых: {a['new_clients']}", f"🔁 Повторных: {a['repeat_clients']}"]
    if a["staff"]:
        lines += ["", "👤 ТОП МАСТЕРОВ"]
        for name, x in sorted(a["staff"].items(), key=lambda kv: (-kv[1]["revenue"], -kv[1]["visits"]))[:10]:
            lines.append(f"• {name}: {x['visits']} визитов / {x['revenue']:,.0f} ₽".replace(",", " "))
    if a["services"]:
        lines += ["", "✂️ ТОП УСЛУГ"]
        for name, x in sorted(a["services"].items(), key=lambda kv: (-kv[1]["revenue"], -kv[1]["count"]))[:10]:
            lines.append(f"• {name}: {x['count']} / {x['revenue']:,.0f} ₽".replace(",", " "))
    if errors:
        lines += ["", "⚠️ Ошибки API:"] + [f"• {e}" for e in errors]
    return "\n".join(lines)


def help_text():
    return """🤖 МЕТРО — Аналитик\n\nМожно писать обычным текстом или командами.\n\n📊 ОСНОВНЫЕ\n/today — сегодня\n/yesterday — вчера\n/week — последние 7 дней\n/month — текущий месяц\n\n💰 АНАЛИТИКА\n/summary — сводка за период\n/revenue — выручка\n/records — записи\n/clients — клиенты\n/newclients — новые клиенты\n/repeat — повторные клиенты\n/noshow — неявки\n/cancellations — отмены\n/staff — мастера\n/services — услуги\n/top — топ мастеров и услуг\n/comments — комментарии из записей\n\n📋 МОЖНО СПРОСИТЬ ФРАЗОЙ\n«выручка за вчера»\n«сколько клиентов сегодня»\n«кто лучший мастер за неделю»\n«какие услуги самые прибыльные»\n«сколько не пришло вчера»\n«покажи отмены за месяц»\n«новые клиенты за 7 дней»\n«повторные клиенты за месяц»\n«покажи комментарии за сегодня»\n\n🌅 /morning — утренний отчёт\n🌙 /evening — вечерний отчёт\n📡 /status — состояние интеграций\n🧪 /test — тест Telegram"""


def comments_report(text):
    start, end, records, errors = load_period(text)
    rows = []
    for r in records:
        c = str(r.get("comment") or "").strip()
        if c:
            dt = record_dt(r); date = dt.strftime("%d.%m %H:%M") if dt else ""
            rows.append(f"• {date} | {client_name(r)} | {staff_name(r)}\n  {c}")
    if not rows:
        return f"💬 Комментариев за период нет.\n{start:%d.%m.%Y} — {end:%d.%m.%Y}"
    return f"💬 Комментарии ({len(rows)})\nПериод: {start:%d.%m.%Y} — {end:%d.%m.%Y}\n\n" + "\n".join(rows[:40])


def detailed_query(text):
    t = text.lower().strip()
    if t in ("/start", "/help", "помощь", "команды", "что умеешь"):
        return help_text()
    if t.startswith("/comments") or "комментари" in t:
        return comments_report(t)
    if t.startswith("/today") or t == "сегодня": t = "сегодня"
    elif t.startswith("/yesterday"): t = "вчера"
    elif t.startswith("/week"): t = "неделя"
    elif t.startswith("/month"): t = "месяц"
    elif t.startswith("/revenue"): t = "выручка " + t[8:]
    elif t.startswith("/records"): t = "записи " + t[8:]
    elif t.startswith("/clients"): t = "клиенты " + t[8:]
    elif t.startswith("/newclients"): t = "новые клиенты " + t[11:]
    elif t.startswith("/repeat"): t = "повторные клиенты " + t[7:]
    elif t.startswith("/noshow"): t = "неявки " + t[7:]
    elif t.startswith("/cancellations"): t = "отмены " + t[14:]
    elif t.startswith("/staff"): t = "мастера " + t[6:]
    elif t.startswith("/services"): t = "услуги " + t[9:]
    elif t.startswith("/top"): t = "топ " + t[4:]
    elif t.startswith("/summary"): t = "сводка " + t[8:]
    elif t.startswith("/"):
        return "Не знаю такую команду.\n\n" + help_text()

    start, end, records, errors = load_period(t)
    a = aggregate(records)
    is_revenue = any(x in t for x in ("выруч", "деньги", "оборот", "касс"))
    is_clients = "клиент" in t
    is_staff = any(x in t for x in ("мастер", "барбер", "сотрудник"))
    is_services = "услуг" in t or "стриж" in t
    is_new = "нов" in t and "клиент" in t
    is_repeat = "повтор" in t or "вернул" in t or "возврат" in t
    is_noshow = "не приш" in t or "неяв" in t or "no-show" in t
    is_cancel = "отмен" in t or "удал" in t
    is_top = "топ" in t or "лучший" in t

    if is_new or is_repeat:
        target = a["new_clients"] if is_new else a["repeat_clients"]
        label = "новых клиентов" if is_new else "повторных клиентов"
        return f"👥 {label.capitalize()}: {target}\nПериод: {start:%d.%m.%Y} — {end:%d.%m.%Y}\nВсего уникальных клиентов: {a['clients']}"
    if is_noshow:
        return f"❌ Неявки: {a['no_show']}\nПериод: {start:%d.%m.%Y} — {end:%d.%m.%Y}\nВсего записей: {a['records']}"
    if is_cancel:
        return f"🚫 Отменено/удалено: {a['cancelled']}\nПериод: {start:%d.%m.%Y} — {end:%d.%m.%Y}"
    if is_staff or is_top:
        rows = sorted(a["staff"].items(), key=lambda kv: (-kv[1]["revenue"], -kv[1]["visits"]))
        if not rows: return "👤 Данных по мастерам за этот период нет."
        out = [f"👤 Мастера\nПериод: {start:%d.%m.%Y} — {end:%d.%m.%Y}", ""]
        for i,(name,x) in enumerate(rows[:15],1):
            avg = x["revenue"] / x["visits"] if x["visits"] else 0
            out.append(f"{i}. {name} — {x['visits']} визитов, {x['revenue']:,.0f} ₽, средний чек {avg:,.0f} ₽".replace(","," "))
        return "\n".join(out)
    if is_services:
        rows = sorted(a["services"].items(), key=lambda kv: (-kv[1]["revenue"], -kv[1]["count"]))
        if not rows: return "✂️ Данных по услугам за этот период нет."
        out = [f"✂️ Услуги\nПериод: {start:%d.%m.%Y} — {end:%d.%m.%Y}", ""]
        for i,(name,x) in enumerate(rows[:15],1):
            out.append(f"{i}. {name} — {x['count']} шт., {x['revenue']:,.0f} ₽".replace(","," "))
        return "\n".join(out)
    if is_revenue:
        avg = a["revenue"] / a["attended"] if a["attended"] else 0
        return (f"💰 Выручка\nПериод: {start:%d.%m.%Y} — {end:%d.%m.%Y}\n\n"
                f"Выручка: {a['revenue']:,.0f} ₽\nПришли: {a['attended']}\nСредний чек: {avg:,.0f} ₽".replace(","," "))
    if is_clients:
        return (f"👥 Клиенты\nПериод: {start:%d.%m.%Y} — {end:%d.%m.%Y}\n\n"
                f"Уникальных: {a['clients']}\nНовых: {a['new_clients']}\nПовторных: {a['repeat_clients']}\nЗаписей: {a['records']}")
    return fmt_report("Сводка МЕТРО", start, end, records, errors)


def make_scheduled_report(kind):
    if kind == "morning":
        # Morning = yesterday results + today bookings.
        d = now().date() - timedelta(days=1)
        text = "вчера"
        start, end, records, errors = load_period(text)
        body = fmt_report("🌅 Утренний отчёт МЕТРО", start, end, records, errors)
        body += "\n\n📌 Сегодня\n"
        try:
            _, _, today_records, today_errors = load_period("сегодня")
            body += fmt_report("Записи на сегодня", now().date(), now().date(), today_records, today_errors)
        except Exception as e:
            body += f"⚠️ Не удалось получить записи на сегодня: {e}"
    else:
        body = fmt_report("🌙 Вечерний отчёт МЕТРО", *load_period("сегодня")[:2], load_period("сегодня")[2], load_period("сегодня")[3])
    return body


def scheduler_loop():
    sent_keys = set()
    while True:
        try:
            n = now()
            key = n.strftime("%Y-%m-%d")
            if n.strftime("%H:%M") == MORNING_REPORT_TIME and (key,"morning") not in sent_keys:
                body = make_scheduled_report("morning"); notify_telegram(body)
                sent_keys.add((key,"morning"))
            if n.strftime("%H:%M") == EVENING_REPORT_TIME and (key,"evening") not in sent_keys:
                body = make_scheduled_report("evening"); notify_telegram(body)
                sent_keys.add((key,"evening"))
            # prevent unbounded growth
            if len(sent_keys) > 20: sent_keys = set(list(sent_keys)[-10:])
        except Exception:
            log.exception("Scheduler error")
        time.sleep(30)


def configure_telegram_webhook():
    if not TELEGRAM_BOT_TOKEN or not PUBLIC_URL:
        return
    url = f"{PUBLIC_URL}/telegram/webhook"
    try:
        r = requests.post(f"{TELEGRAM_API}/setWebhook", json={"url": url, "drop_pending_updates": False}, timeout=20)
        log.info("Telegram setWebhook: %s %s", r.status_code, r.text[:500])
    except requests.RequestException:
        log.exception("Failed to configure Telegram webhook")


@app.get("/")
def index():
    return jsonify({"service":"МЕТРО — Аналитик","status":"online","time":now_text(),
                    "endpoints":["/health","/webhook","/telegram/webhook","/register","/callback"]})


@app.get("/health")
def health():
    return jsonify({
        "status":"ok",
        "telegram_token_configured":bool(TELEGRAM_BOT_TOKEN),
        "telegram_chat_id_configured":bool(TELEGRAM_CHAT_ID),
        "yclients_company_id_configured":bool(YCLIENTS_COMPANY_IDS),
        "yclients_user_token_configured":bool(YCLIENTS_USER_TOKEN),
        "yclients_partner_token_configured":bool(YCLIENTS_PARTNER_TOKEN),
        "yclients_authorization_configured":bool(YCLIENTS_AUTHORIZATION),
        "yclients_webhook_secret_configured":bool(YCLIENTS_WEBHOOK_SECRET),
        "timezone":str(TZ),"morning_report":MORNING_REPORT_TIME,"evening_report":EVENING_REPORT_TIME,
        "time":now_text()})


@app.route("/webhook", methods=["GET","POST"])
def yclients_webhook():
    if request.method == "GET":
        return jsonify({"status":"ok","message":"YCLIENTS webhook endpoint is available. Send POST JSON here."})
    if YCLIENTS_WEBHOOK_SECRET:
        supplied = (request.headers.get("X-YCLIENTS-SECRET") or request.headers.get("X-Webhook-Secret")
                    or request.args.get("secret") or "")
        if supplied != YCLIENTS_WEBHOOK_SECRET:
            return jsonify({"ok":False,"error":"invalid webhook secret"}),401
    payload = request.get_json(silent=True) or {}
    company_id = payload.get("company_id"); resource = payload.get("resource")
    resource_id = payload.get("resource_id"); status = payload.get("status")
    con = db(); con.execute("INSERT INTO webhook_events(received_at,company_id,resource,resource_id,status,payload) VALUES(?,?,?,?,?,?)",
                            (now_text(),str(company_id or ""),str(resource or ""),str(resource_id or ""),str(status or ""),json.dumps(payload,ensure_ascii=False)))
    con.commit(); con.close()
    log.info("YCLIENTS webhook: company_id=%s resource=%s resource_id=%s status=%s",company_id,resource,resource_id,status)
    # Keep webhook acknowledgement fast; notification is optional and concise.
    if resource in {"record","records","client","staff","sale"}:
        notify_telegram(f"🟢 YCLIENTS\n{resource}: {status}\nID: {resource_id}\nФилиал: {company_id}")
    return jsonify({"ok":True,"received":{"company_id":company_id,"resource":resource,"resource_id":resource_id,"status":status}}),200


@app.route("/telegram/webhook", methods=["POST"])
def telegram_webhook():
    update = request.get_json(silent=True) or {}
    message = update.get("message") or update.get("edited_message")
    if not message: return jsonify({"ok":True})
    chat_id = str((message.get("chat") or {}).get("id", "")).strip()
    text = (message.get("text") or "").strip()
    save_chat(chat_id)
    try:
        if text.startswith("/status"):
            telegram_send(chat_id, f"📡 МЕТРО — Аналитик\n\nTelegram: {'✅' if TELEGRAM_BOT_TOKEN else '❌'}\nYCLIENTS company: {'✅' if YCLIENTS_COMPANY_IDS else '❌'}\nYCLIENTS user token: {'✅' if YCLIENTS_USER_TOKEN else '❌'}\nYCLIENTS partner token: {'✅' if YCLIENTS_PARTNER_TOKEN else '⚠️ не задан (используется USER_TOKEN как fallback)'}\nWebhook secret: {'✅' if YCLIENTS_WEBHOOK_SECRET else '⚪ не задан'}\nВремя: {now_text()}")
        elif text.startswith("/test"):
            telegram_send(chat_id, "🧪 Telegram работает. Бот готов принимать запросы и аналитику YCLIENTS.")
        elif text.startswith("/morning"):
            telegram_send(chat_id, make_scheduled_report("morning"))
        elif text.startswith("/evening"):
            telegram_send(chat_id, make_scheduled_report("evening"))
        else:
            telegram_send(chat_id, detailed_query(text))
    except Exception as e:
        log.exception("Query failed")
        telegram_send(chat_id, f"⚠️ Не удалось получить аналитику.\n\n{e}\n\nПроверь /status и права API YCLIENTS.")
    return jsonify({"ok":True})


@app.get("/register")
def register():
    return jsonify({"service":"МЕТРО — Аналитик","status":"ready","received_params":dict(request.args)})


@app.route("/callback", methods=["GET","POST"])
def callback():
    payload = request.get_json(silent=True)
    log.info("YCLIENTS callback: params=%s payload=%s", dict(request.args), payload)
    return jsonify({"ok":True,"message":"Callback received","params":dict(request.args),"payload":payload})


@app.errorhandler(404)
def not_found(error):
    return jsonify({"ok":False,"error":"not_found","path":request.path,"available_endpoints":["/","/health","/webhook","/telegram/webhook","/register","/callback"]}),404


configure_telegram_webhook()
threading.Thread(target=scheduler_loop, daemon=True, name="metro-report-scheduler").start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","8000")))
