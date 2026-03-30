#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
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
# CONFIG (ENV VARS - RENDER SAFE)
# ==========================
BOT_TOKEN = os.environ["BOT_TOKEN"]           # 🔐 DA RENDER
CHAT_ID = int(os.environ["CHAT_ID"])          # 🔐 DA RENDER

FIXTURES_FILE = Path("./score_over_05_alerts.json")
SENT_ALERTS_FILE = Path("./sent_alerts.json")

CHECK_EVERY_SECONDS = 300      # 5 minuti
TIME_TOLERANCE_MINUTES = 2     # ±2 minuti

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

# ==========================
# LOGGING
# ==========================
logger = logging.getLogger("alert_bet")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# ==========================
# FASTAPI LIFESPAN
# ==========================
stop_event = Event()
cron_thread: Thread | None = None

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
# CORE LOGIC
# ==========================
HALFTIME_OFFSET_MINUTES = 50   # alert al ~2° tempo (kick-off + 50 min)

def check_and_send_alerts():
    now = datetime.now()
    tolerance = timedelta(minutes=TIME_TOLERANCE_MINUTES)

    sent_alerts = load_sent_alerts()
    data = json.loads(FIXTURES_FILE.read_text())
    fixtures = data.get("alerts", data) if isinstance(data, dict) else data
    logger.info("🔍 Controllo alert per %d fixtures", len(fixtures))

    to_send = []  # lista di (alert_time, fixture) da inviare in questo ciclo
    for f in fixtures:
        match_id = f["match_id"]
        if match_id in sent_alerts:
            continue

        match_date = f.get("match_date", "")
        match_time = f.get("match_time", "") or "00:00"
        alert_dt_str = f"{match_date}T{match_time}:00" if match_date else None
        if not alert_dt_str:
            continue
        kickoff = parser.isoparse(alert_dt_str)
        alert_time = kickoff + timedelta(minutes=HALFTIME_OFFSET_MINUTES)

        if abs(now - alert_time) <= tolerance:
            to_send.append((kickoff, f))
            sent_alerts.add(match_id)

    if to_send:
        # Raggruppa per orario di kick-off (stessa ora → stesso messaggio)
        from collections import defaultdict
        groups: dict = defaultdict(list)
        for kickoff, f in to_send:
            groups[kickoff.strftime("%H:%M")].append(f)

        for ko_time, fixtures_group in sorted(groups.items()):
            lines = [f"⚽ *ALERT OVER 0.5 — 2° Tempo*\n"]
            for f in fixtures_group:
                league = f.get("league_name") or f.get("league_id", "")
                over15 = next((a for a in f.get("alerts", []) if a["market"] == "over_15"), None)
                over25 = next((a for a in f.get("alerts", []) if a["market"] == "over_25"), None)
                badge_15 = f"O1.5 {over15['prob_cal']*100:.0f}%" if over15 else ""
                badge_25 = f"O2.5 {over25['prob_cal']*100:.0f}%" if over25 else ""
                badges = "  ".join(b for b in [badge_15, badge_25] if b)
                lines.append(
                    f"*{f['home_team']}* vs *{f['away_team']}*\n"
                    f"_{league}_  ⏱ {ko_time}\n"
                    f"{badges}"
                )
            message = "\n\n".join(lines)
            send_telegram_message(message)
            logger.info("📤 Inviato blocco %s (%d partite)", ko_time, len(fixtures_group))
    else:
        logger.info("📭 Nessuna partita da inviare in questa iterazione")

    save_sent_alerts(sent_alerts)

# ==========================
# CRON LOOP
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

# ==========================
# HEALTHCHECK (RENDER)
# ==========================
@app.head("/health")
async def health_check():
    return Response(status_code=200)

@app.get("/health")
async def health_check_get():
    return {"status": "ok"}

@app.get("/")
async def root():
    return {"status": "ok"}
