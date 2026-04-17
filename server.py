#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import logging
import requests
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from dateutil import parser
from pathlib import Path
from threading import Event, Thread

from fastapi import FastAPI, Response

# ==========================
# CONFIG (ENV VARS - RENDER SAFE)
# ==========================
BOT_TOKEN = os.environ["BOT_TOKEN"]           # 🔐 DA RENDER
CHAT_ID = int(os.environ["CHAT_ID"])          # 🔐 DA RENDER

# File v4 (Over 0.5 — Profeta v4, alert +30min dal kick-off)
V4_FILE = Path("./score_v4_alerts.json")

SENT_ALERTS_FILE = Path("./sent_alerts.json")

CHECK_EVERY_SECONDS = 300      # 5 minuti
TIME_TOLERANCE_MINUTES = 3     # ±3 minuti

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

# v4: alert 30 minuti DOPO il kick-off
V4_POST_KICKOFF_MINUTES = 30

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
    """Ritorna (datetime UTC-aware, orario_noto). Se orario ignoto usa le 08:00 UTC del giorno."""
    match_date = f.get("match_date", "")
    match_time = f.get("match_time", "") or ""
    if not match_date:
        return None, False
    if not match_time or match_time == "00:00":
        try:
            dt = parser.isoparse(f"{match_date}T08:00:00+00:00")
            return dt, False
        except Exception:
            return None, False
    try:
        return parser.isoparse(f"{match_date}T{match_time}:00+00:00"), True
    except Exception:
        return None, False


# ==========================
# CORE LOGIC
# ==========================
def check_and_send_alerts():
    now = datetime.now(timezone.utc)
    tolerance = timedelta(minutes=TIME_TOLERANCE_MINUTES)

    sent_alerts = load_sent_alerts()

    # ── v4: Over 0.5 superalert — alert +30min dal kick-off ──────────────────
    v4_fixtures = _load_fixtures(V4_FILE)
    logger.info("🔍 v4 fixtures: %d", len(v4_fixtures))

    to_send_v4 = []
    for f in v4_fixtures:
        mid = f["match_id"]
        state = sent_alerts.setdefault(f"v4_{mid}", {"sent": False})
        if state["sent"]:
            continue
        ko, time_known = _kickoff(f)
        if ko is None:
            continue
        alert_time = ko + timedelta(minutes=V4_POST_KICKOFF_MINUTES)
        if abs(now - alert_time) <= tolerance:
            to_send_v4.append((ko, f, time_known))

    if to_send_v4:
        from collections import defaultdict
        groups_v4: dict = defaultdict(list)
        for ko, f, time_known in to_send_v4:
            groups_v4[(ko.strftime("%H:%M"), time_known)].append((f, time_known))

        for (ko_time, time_known), items in sorted(groups_v4.items()):
            group = [f for f, _ in items]
            header = f"⚽ *OVER 0.5 — Profeta v4*\n{'─' * 30}\n"
            blocks = []
            for f in group:
                time_note = f"`{ko_time}`" if time_known else "_orario n.d._"
                league = f.get("league_name", "")
                prob = f.get("prob_cal", "")
                conf = (f.get("confidence") or "").upper()
                prob_str = f" · prob {prob:.0%}" if isinstance(prob, float) else ""
                blocks.append(
                    f"🏟 *{f['home_team']}* vs *{f['away_team']}*\n"
                    f"⏰ {time_note}  [{conf}{prob_str}]\n"
                    f"🏆 _{league}_"
                )
            ko_label = ko_time if time_known else "orario n.d."
            footer = f"\n{'─' * 30}\n_+30min dal kick-off {ko_label} UTC_"
            try:
                send_telegram_message(header + "\n\n".join(blocks) + footer)
                for f in group:
                    sent_alerts[f"v4_{f['match_id']}"]["sent"] = True
                save_sent_alerts(sent_alerts)
                logger.info("📤 v4 inviato %s (%d partite)", ko_label, len(group))
            except Exception:
                logger.exception("❌ Telegram fallito per gruppo %s — riprovo al prossimo ciclo", ko_label)
    else:
        logger.info("📭 v4: nessuna partita da inviare")

# ==========================
# CRON LOOP
# ==========================
def _cron_loop():
    logger.info("🚀 Cron loop attivo")
    while not stop_event.is_set():
        logger.info("🕓 Nuova iterazione cron: %s", datetime.now(timezone.utc).isoformat())
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

