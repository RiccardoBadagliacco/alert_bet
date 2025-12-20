#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import time
import requests
from datetime import datetime, timedelta
from dateutil import parser
from pathlib import Path

# ==========================
# CONFIG STATICA
# ==========================
BOT_TOKEN = "8168882419:AAGJAutgGoERpvNV6x45DY3J1CjzUyYsiZI"
CHAT_ID = 28388796   # 🔒 CHAT ID FISSA

FIXTURES_FILE = Path("./fixtures_alert_over25.json")
SENT_ALERTS_FILE = Path("./sent_alerts.json")

CHECK_EVERY_SECONDS = 300      # 5 minuti
TIME_TOLERANCE_MINUTES = 2     # ±2 minuti

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

# ==========================
# TELEGRAM
# ==========================
def send_telegram_message(text: str):
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    }
    r = requests.post(TELEGRAM_API, json=payload, timeout=10)
    r.raise_for_status()

# ==========================
# STATE
# ==========================
def load_sent_alerts():
    if SENT_ALERTS_FILE.exists():
        return set(json.loads(SENT_ALERTS_FILE.read_text()))
    return set()

def save_sent_alerts(sent_ids):
    SENT_ALERTS_FILE.write_text(json.dumps(list(sent_ids), indent=2))

# ==========================
# CORE
# ==========================
def check_and_send_alerts():
    now = datetime.now()
    tolerance = timedelta(minutes=TIME_TOLERANCE_MINUTES)

    sent_alerts = load_sent_alerts()
    fixtures = json.loads(FIXTURES_FILE.read_text())

    for f in fixtures:
        match_id = f["match_id"]
        if match_id in sent_alerts:
            continue

        alert_time = parser.isoparse(f["alert_datetime"])

        if abs(now - alert_time) <= tolerance:
            message = (
                f"🔥 *ALERT OVER 2.5*\n\n"
                f"*{f['home_team']}* vs *{f['away_team']}*\n"
                f"{f['league_name']}\n\n"
                f"⏱ {alert_time.strftime('%H:%M')}"
            )

            send_telegram_message(message)
            sent_alerts.add(match_id)

    save_sent_alerts(sent_alerts)

# ==========================
# LOOP
# ==========================
def run():
    print("🚀 Telegram Alert Server attivo (chat_id fissa)")
    while True:
        print("🕓 Nuova iterazione cron:", datetime.now().isoformat())
        try:
            check_and_send_alerts()
        except Exception as e:
            print("❌ Errore:", e)
        time.sleep(CHECK_EVERY_SECONDS)

if __name__ == "__main__":
    run()
