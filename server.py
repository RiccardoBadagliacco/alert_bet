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

# Timing alert
HT_OFFSET_MINUTES = 15     # kickoff +15min → alert Over 0.5 HT (segnale A)
HALFTIME_OFFSET_MINUTES = 50   # kickoff +50min → alert Over 0.5 2T (segnali A+B)

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
# sent_alerts = {
#   match_id: {
#     "ht_sent": bool,    # alert +15min Over 0.5 HT
#     "2t_sent": bool,    # alert +50min Over 0.5 2T
#   }
# }
# ==========================
def load_sent_alerts() -> dict:
    if SENT_ALERTS_FILE.exists():
        raw = json.loads(SENT_ALERTS_FILE.read_text())
        # Migrazione da vecchi formati
        if isinstance(raw, list):
            return {mid: {"ht_sent": True, "2t_sent": True} for mid in raw}
        # Migrazione da formato {kickoff, halftime}
        migrated = {}
        for mid, v in raw.items():
            if isinstance(v, dict) and "ht_sent" in v:
                migrated[mid] = v
            else:
                migrated[mid] = {"ht_sent": v.get("kickoff", True), "2t_sent": v.get("halftime", True)}
        return migrated
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

    ht_to_send = []   # (kickoff_dt, fixture)  — Over 0.5 HT, +15min
    t2_to_send = []   # (kickoff_dt, fixture)  — Over 0.5 2T, +50min

    for f in fixtures:
        match_id = f["match_id"]
        signal_type = f.get("signal_type", "ht_and_2t")  # default retrocompatibile
        state = sent_alerts.get(match_id, {"ht_sent": False, "2t_sent": False})

        match_date = f.get("match_date", "")
        match_time = f.get("match_time", "") or "00:00"
        alert_dt_str = f"{match_date}T{match_time}:00" if match_date else None
        if not alert_dt_str:
            continue
        kickoff = parser.isoparse(alert_dt_str)

        # Alert Over 0.5 HT — solo segnale A (ht_and_2t), +15min
        if signal_type == "ht_and_2t" and not state["ht_sent"]:
            ht_time = kickoff + timedelta(minutes=HT_OFFSET_MINUTES)
            if abs(now - ht_time) <= tolerance:
                ht_to_send.append((kickoff, f))
                state["ht_sent"] = True

        # Alert Over 0.5 2T — tutti i segnali, +50min
        if not state["2t_sent"]:
            t2_time = kickoff + timedelta(minutes=HALFTIME_OFFSET_MINUTES)
            if abs(now - t2_time) <= tolerance:
                t2_to_send.append((kickoff, f))
                state["2t_sent"] = True

        sent_alerts[match_id] = state

    # ── Invia alert Over 0.5 HT (+15min) ──
    if ht_to_send:
        from collections import defaultdict
        groups: dict = defaultdict(list)
        for kickoff, f in ht_to_send:
            groups[kickoff.strftime("%H:%M")].append(f)

        for ko_time, group in sorted(groups.items()):
            header = f"⚽ *PARTITA IN CORSO — Over 0.5 1° Tempo*\n{'─' * 30}\n"
            blocks = []
            for f in group:
                league = f.get("league_name") or f.get("league_id", "")
                a = next((x for x in f.get("alerts", []) if x["market"] == "over_25"), None)
                cs = f"`cs {a['confidence_score']:.0f}`" if a else ""
                prob = f"`{a['prob_cal']*100:.0f}%`" if a else ""
                blocks.append(
                    f"🏟 *{f['home_team']}* vs *{f['away_team']}*\n"
                    f"🏆 _{league}_\n"
                    f"📊 Over 2.5 {prob} · {cs}\n"
                    f"👉 Controlla il punteggio → se ancora *0-0* entra su *Over 0.5 1T*"
                )
            footer = f"\n{'─' * 30}\n_Kick-off {ko_time} · +15min · modello Over 2.5 Ultra Aggressivo_"
            send_telegram_message(header + "\n\n".join(blocks) + footer)
            logger.info("📤 [HT +15min] Inviato blocco %s (%d partite)", ko_time, len(group))

    # ── Invia alert Over 0.5 2T (+50min) ──
    if t2_to_send:
        from collections import defaultdict
        groups2: dict = defaultdict(list)
        for kickoff, f in t2_to_send:
            groups2[kickoff.strftime("%H:%M")].append(f)

        for ko_time, group in sorted(groups2.items()):
            header = f"⏱ *SECONDO TEMPO — Over 0.5 2° Tempo*\n{'─' * 30}\n"
            blocks = []
            for f in group:
                league = f.get("league_name") or f.get("league_id", "")
                signal_type = f.get("signal_type", "ht_and_2t")
                a25 = next((x for x in f.get("alerts", []) if x["market"] == "over_25"), None)
                a15 = next((x for x in f.get("alerts", []) if x["market"] == "over_15"), None)
                if signal_type == "ht_and_2t" and a25:
                    sig_line = f"📊 Over 2.5 `{a25['prob_cal']*100:.0f}%` · `cs {a25['confidence_score']:.0f}`"
                else:
                    cs_val = a15['confidence_score'] if a15 else 0
                    sig_line = f"📊 Over 1.5 `cs {cs_val:.0f}` · quota bassa ✓"
                blocks.append(
                    f"🏟 *{f['home_team']}* vs *{f['away_team']}*\n"
                    f"🏆 _{league}_\n"
                    f"{sig_line}\n"
                    f"👉 Se è *0-0* → entra su *Over 0.5 2T*"
                )
            footer = f"\n{'─' * 30}\n_Kick-off {ko_time} · +50min · modello Profeta v2_"
            send_telegram_message(header + "\n\n".join(blocks) + footer)
            logger.info("📤 [2T +50min] Inviato blocco %s (%d partite)", ko_time, len(group))

    if not ht_to_send and not t2_to_send:
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
