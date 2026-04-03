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

# Alert inviato al kick-off (0 min) come reminder da monitorare durante il 1° tempo
PRE_MATCH_OFFSET_MINUTES = 0   # esattamente al kick-off

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

def _kickoff(f) -> tuple[datetime | None, bool]:
    """Ritorna (datetime, orario_noto). Se orario ignoto usa le 08:00 del giorno della partita."""
    match_date = f.get("match_date", "")
    match_time = f.get("match_time", "") or ""
    if not match_date:
        return None, False
    if not match_time or match_time == "00:00":
        # Orario sconosciuto: schedula alert alle 08:00 del giorno della partita
        try:
            dt = parser.isoparse(f"{match_date}T08:00:00")
            return dt, False
        except Exception:
            return None, False
    dt_str = f"{match_date}T{match_time}:00"
    try:
        return parser.isoparse(dt_str), True
    except Exception:
        return None, False

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
        ko, time_known = _kickoff(f)
        if ko is None:
            continue
        # Per partite con orario noto: alert 30min prima del kick-off
        # Per partite senza orario: alert alle 08:00 del giorno (offset = 0)
        offset = timedelta(minutes=PRE_MATCH_OFFSET_MINUTES) if time_known else timedelta(0)
        alert_time = ko + offset
        if abs(now - alert_time) <= tolerance:
            to_send.append((ko, f, time_known))
            state["sent"] = True

    if to_send:
        from collections import defaultdict
        groups: dict = defaultdict(list)
        for ko, f, time_known in to_send:
            groups[(ko.strftime("%H:%M"), time_known)].append(f)

        for (ko_time, time_known), group in sorted(groups.items()):
            header = f"👀 *DA MONITORARE — Over 0.5 2T*\n{'─' * 30}\n"
            blocks = []
            for f in group:
                time_note = f"`{ko_time}`" if time_known else "_orario n.d._"
                league = f.get("league_name") or f.get("league_id", "")
                country = f.get("country", "")
                meta = f"{country} · {league}" if country else league
                blocks.append(
                    f"🏟 *{f['home_team']}* vs *{f['away_team']}*\n"
                    f"📅 {f.get('match_date', '')} ⏰ {time_note}\n"
                    f"🏆 _{meta}_"
                )
            ko_label = ko_time if time_known else "orario n.d."
            footer = f"\n{'─' * 30}\n_Kick-off {ko_label} · Profeta v2 Over 2.5_"
            send_telegram_message(header + "\n\n".join(blocks) + footer)
            logger.info("📤 Inviato %s (%d partite, orario_noto=%s)", ko_label, len(group), time_known)
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
