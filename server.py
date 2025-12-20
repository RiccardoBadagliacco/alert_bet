#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import logging
import requests
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from dateutil import parser
from pathlib import Path
from threading import Event, Thread

from fastapi import FastAPI, Response

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

logger = logging.getLogger("alert_bet")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global cron_thread
    if not cron_thread or not cron_thread.is_alive():
        stop_event.clear()
        cron_thread = Thread(target=_cron_loop, daemon=True)
        cron_thread.start()
        logger.info("🧵 Thread cron avviato")

    try:
        yield
    finally:
        stop_event.set()
        if cron_thread and cron_thread.is_alive():
            cron_thread.join(timeout=CHECK_EVERY_SECONDS)
            logger.info("🧵 Thread cron arrestato")


app = FastAPI(title="Alert Bet API", lifespan=lifespan)
stop_event = Event()
cron_thread: Thread | None = None

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
def _cron_loop():
    logger.info("🚀 Cron loop attivo")
    while not stop_event.is_set():
        logger.info("🕓 Nuova iterazione cron: %s", datetime.now().isoformat())
        try:
            check_and_send_alerts()
        except Exception:
            logger.exception("❌ Errore durante l'invio degli alert")

        if stop_event.wait(CHECK_EVERY_SECONDS):
            break

    logger.info("🛑 Cron loop terminato")


@app.head("/health")
async def health_check():
    return Response(status_code=200)
