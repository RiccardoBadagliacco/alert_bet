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

# Un solo file: partite da monitorare per Over 0.5 2T
T2_FILE = Path("./score_2t_alerts.json")   # over_25 cs >= 600
SENT_ALERTS_FILE = Path("./sent_alerts.json")

CHECK_EVERY_SECONDS = 300      # 5 minuti
TIME_TOLERANCE_MINUTES = 3     # ±3 minuti

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

# Alert inviato 30 minuti PRIMA del kick-off come reminder da monitorare
PRE_MATCH_OFFSET_MINUTES = -30   # kickoff - 30min → "tienila d'occhio"

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
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
    r = requests.post(TELEGRAM_API, json=payload, timeout=10)
    r.raise_for_status()

# ==========================
# STATE
# ==========================
def load_sent_alerts() -> dict:
    if not SENT_ALERTS_FILE.exists():
        return {}
    raw = json.loads(SENT_ALERTS_FILE.read_text())
    if isinstance(raw, list):
        return {mid: {"sent": True} for mid in raw}
    migrated = {}
    for mid, v in raw.items():
        if isinstance(v, dict) and "sent" in v:
            migrated[mid] = v
        else:
            # migrazione da formato vecchio ht_sent/2t_sent
            migrated[mid] = {"sent": True}
    return migrated

def save_sent_alerts(sent: dict):
    SENT_ALERTS_FILE.write_text(json.dumps(sent, indent=2))

def _load_fixtures(path: Path) -> list:
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    return data.get("alerts", data) if isinstance(data, dict) else data

def _kickoff(f) -> datetime | None:
    match_date = f.get("match_date", "")
    match_time = f.get("match_time", "") or ""
    if not match_date:
        return None
    if not match_time or match_time == "00:00":
        # orario sconosciuto — salta, non mandare alert a orari sbagliati
        return None
    dt_str = f"{match_date}T{match_time}:00"
    try:
        return parser.isoparse(dt_str)
    except Exception:
        return None

# ==========================
# CORE LOGIC
# ==========================
def check_and_send_alerts():
    now = datetime.now()
    tolerance = timedelta(minutes=TIME_TOLERANCE_MINUTES)

    sent_alerts = load_sent_alerts()
    t2_fixtures = _load_fixtures(T2_FILE)
    logger.info("🔍 2T fixtures: %d", len(t2_fixtures))

    to_send = []

    for f in t2_fixtures:
        mid = f["match_id"]
        state = sent_alerts.setdefault(mid, {"sent": False})
        if state["sent"]:
            continue
        ko = _kickoff(f)
        if ko is None:
            logger.warning("⚠️ Orario sconosciuto per %s vs %s — alert saltato",
                           f.get("home_team", "?"), f.get("away_team", "?"))
            continue
        alert_time = ko + timedelta(minutes=PRE_MATCH_OFFSET_MINUTES)
        if abs(now - alert_time) <= tolerance:
            to_send.append((ko, f))
            state["sent"] = True

    if to_send:
        from collections import defaultdict
        groups: dict = defaultdict(list)
        for ko, f in to_send:
            groups[ko.strftime("%H:%M")].append(f)

        for ko_time, group in sorted(groups.items()):
            header = f"👀 *DA MONITORARE — Over 0.5 2T*\n{'─' * 30}\n"
            blocks = []
            for f in group:
                league = f.get("league_name") or f.get("league_id", "")
                a = next((x for x in f.get("alerts", []) if x["market"] == "over_25"), None)
                cs = f"`cs {a['confidence_score']:.0f}`" if a else ""
                prob = f"`Over 2.5 {a['prob_cal']*100:.0f}%`" if a else ""
                blocks.append(
                    f"🏟 *{f['home_team']}* vs *{f['away_team']}*\n"
                    f"🏆 _{league}_\n"
                    f"📊 {prob} · {cs}\n"
                    f"👉 All'intervallo: se *0-0* e *SOT ≥ 10* → entra su *Over 0.5 2T*"
                )
            footer = f"\n{'─' * 30}\n_Kick-off {ko_time} · Profeta v2 Over 2.5_"
            send_telegram_message(header + "\n\n".join(blocks) + footer)
            logger.info("📤 [pre-match -30min] Inviato %s (%d partite)", ko_time, len(group))
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

@app.head("/")
async def root_head():
    return Response(status_code=200)

@app.get("/")
async def root():
    return {"status": "ok"}
