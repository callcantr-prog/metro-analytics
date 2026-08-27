import os
import json
import sqlite3
from datetime import datetime, timezone

from flask import Flask, request, jsonify

app = Flask(__name__)

DB_PATH = os.getenv("DB_PATH", "analytics.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at TEXT NOT NULL,
            event_type TEXT,
            payload TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


# Инициализация БД выполняется и при запуске через Gunicorn/Render.
init_db()


@app.get("/")
def index():
    return jsonify({
        "service": "МЕТРО — Аналитик",
        "status": "online"
    })


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.post("/webhook")
def webhook():
    data = request.get_json(silent=True)

    if data is None:
        data = {"raw_body": request.get_data(as_text=True)}

    event_type = None
    if isinstance(data, dict):
        event_type = (
            request.headers.get("X-Event")
            or request.headers.get("X-Event-Type")
            or data.get("event")
        )

    received_at = datetime.now(timezone.utc).isoformat()

    conn = get_db()
    conn.execute(
        """
        INSERT INTO events (received_at, event_type, payload)
        VALUES (?, ?, ?)
        """,
        (
            received_at,
            event_type,
            json.dumps(data, ensure_ascii=False)
        )
    )
    conn.commit()
    conn.close()

    print("=" * 60)
    print("YCLIENTS WEBHOOK")
    print("Время:", received_at)
    print("Событие:", event_type)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    print("=" * 60)

    return jsonify({"status": "ok"}), 200


@app.post("/callback")
def callback():
    data = request.get_json(silent=True)

    if data is None:
        data = {"raw_body": request.get_data(as_text=True)}

    print("=" * 60)
    print("YCLIENTS CALLBACK")
    print(json.dumps(data, ensure_ascii=False, indent=2))
    print("=" * 60)

    return jsonify({"status": "ok"}), 200


@app.get("/events")
def events():
    conn = get_db()
    rows = conn.execute(
        """
        SELECT id, received_at, event_type, payload
        FROM events
        ORDER BY id DESC
        LIMIT 100
        """
    ).fetchall()
    conn.close()

    result = []
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except Exception:
            payload = row["payload"]

        result.append({
            "id": row["id"],
            "received_at": row["received_at"],
            "event_type": row["event_type"],
            "payload": payload
        })

    return jsonify(result)


if __name__ == "__main__":
    init_db()
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
