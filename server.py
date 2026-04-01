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
TIME_TOLERANCE_MINUTES = 3     # ±3 minuti

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

HALFTIME_OFFSET_MINUTES = 50   # kickoff + 50min → alert 2° tempo

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
# sent_alerts = { match_id: {"kickoff": bool, "halftime": bool} }
# ==========================
def load_sent_alerts() -> dict:
    if SENT_ALERTS_FILE.exists():
        raw = json.loads(SENT_ALERTS_FILE.read_text())
        # Migrazione da vecchio formato (set/list di match_id)
        if isinstance(raw, list):
            return {mid: {"kickoff": True, "halftime": True} for mid in raw}
        return raw
    return {}

def save_sent_alerts(sent: dict):
    SENT_ALERTS_FILE.write_text(json.dumps(sent, indent=2))

# ==========================
# CORE LOGIC
# ==========================
def check_and_send_alerts():
    now = datetime.now()
    tolerance = timedelta(minutes=TIME_TOLERANCE_MINUTES)

    sent_alerts = load_sent_alerts()
    data = json.loads(FIXTURES_FILE.read_text())
    fixtures = data.get("alerts", data) if isinstance(data, dict) else data
    logger.info("🔍 Controllo alert per %d fixtures", len(fixtures))

    kickoff_to_send = []   # (kickoff_dt, fixture)
    halftime_to_send = []  # (kickoff_dt, fixture)

    for f in fixtures:
        match_id = f["match_id"]
        state = sent_alerts.get(match_id, {"kickoff": False, "halftime": False})

        match_date = f.get("match_date", "")
        match_time = f.get("match_time", "") or "00:00"
        alert_dt_str = f"{match_date}T{match_time}:00" if match_date else None
        if not alert_dt_str:
            continue
        kickoff = parser.isoparse(alert_dt_str)

        # Alert 1 — kickoff
        if not state["kickoff"] and abs(now - kickoff) <= tolerance:
            kickoff_to_send.append((kickoff, f))
            state["kickoff"] = True

        # Alert 2 — inizio 2° tempo
        halftime_time = kickoff + timedelta(minutes=HALFTIME_OFFSET_MINUTES)
        if not state["halftime"] and abs(now - halftime_time) <= tolerance:
            halftime_to_send.append((kickoff, f))
            state["halftime"] = True

        sent_alerts[match_id] = state

    # ── Invia alert kickoff ──
    if kickoff_to_send:
        from collections import defaultdict
        groups: dict = defaultdict(list)
        for kickoff, f in kickoff_to_send:
            groups[kickoff.strftime("%H:%M")].append(f)

        for ko_time, fixtures_group in sorted(groups.items()):
            header = f"🟢 *PARTITA INIZIATA*\n{'─' * 28}\n"
            match_blocks = []
            for f in fixtures_group:
                league = f.get("league_name") or f.get("league_id", "")
                over25 = next((a for a in f.get("alerts", []) if a["market"] == "over_25"), None)
                cs = f"`cs {over25['confidence_score']:.0f}`" if over25 else ""
                prob = f"`{over25['prob_cal']*100:.0f}%`" if over25 else ""
                match_blocks.append(
                    f"⚽ *{f['home_team']}* vs *{f['away_team']}*\n"
                    f"🏆 _{league}_\n"
                    f"📊 Over 2.5 {prob} · {cs}\n"
                    f"👁 Tienila d'occhio — alert 2° tempo tra ~50min"
                )
            footer = f"\n{'─' * 28}\n_Kick-off {ko_time} · modello Over 2.5 Ultra Aggressivo_"
            message = header + "\n\n".join(match_blocks) + footer
            send_telegram_message(message)
            logger.info("📤 [KICKOFF] Inviato blocco %s (%d partite)", ko_time, len(fixtures_group))

    # ── Invia alert 2° tempo ──
    if halftime_to_send:
        from collections import defaultdict
        groups2: dict = defaultdict(list)
        for kickoff, f in halftime_to_send:
            groups2[kickoff.strftime("%H:%M")].append(f)

        for ko_time, fixtures_group in sorted(groups2.items()):
            header = f"⏱ *SECONDO TEMPO — Controlla il punteggio!*\n{'─' * 28}\n"
            match_blocks = []
            for f in fixtures_group:
                league = f.get("league_name") or f.get("league_id", "")
                over25 = next((a for a in f.get("alerts", []) if a["market"] == "over_25"), None)
                prob25 = f"`{over25['prob_cal']*100:.0f}%`" if over25 else ""
                match_blocks.append(
                    f"⚽ *{f['home_team']}* vs *{f['away_team']}*\n"
                    f"🏆 _{league}_\n"
                    f"👉 Se è *0-0* → entra su *Over 0.5 2T*   🎯 {prob25}"
                )
            footer = f"\n{'─' * 28}\n_Kick-off {ko_time} · modello Over 2.5 Ultra Aggressivo_"
            message = header + "\n\n".join(match_blocks) + footer
            send_telegram_message(message)
            logger.info("📤 [HALFTIME] Inviato blocco %s (%d partite)", ko_time, len(fixtures_group))

    if not kickoff_to_send and not halftime_to_send:
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
