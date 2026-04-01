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

# Due file separati generati da score Profeta
HT_FILE = Path("./score_ht_alerts.json")   # Over 0.5 HT — over_25 cs>=750
T2_FILE = Path("./score_2t_alerts.json")   # Over 0.5 2T — over_25 cs>=750 OR over_15 cs>=960 quota<=1.60
SENT_ALERTS_FILE = Path("./sent_alerts.json")

CHECK_EVERY_SECONDS = 300      # 5 minuti
TIME_TOLERANCE_MINUTES = 3     # ±3 minuti

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

HT_OFFSET_MINUTES = 15         # kickoff +15min → Over 0.5 HT
T2_OFFSET_MINUTES = 50         # kickoff +50min → Over 0.5 2T

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
# sent_alerts = {
#   match_id: {"ht_sent": bool, "2t_sent": bool}
# }
# ==========================
def load_sent_alerts() -> dict:
    if not SENT_ALERTS_FILE.exists():
        return {}
    raw = json.loads(SENT_ALERTS_FILE.read_text())
    # Migrazione da formati precedenti
    if isinstance(raw, list):
        return {mid: {"ht_sent": True, "2t_sent": True} for mid in raw}
    migrated = {}
    for mid, v in raw.items():
        if isinstance(v, dict) and "ht_sent" in v:
            migrated[mid] = v
        else:
            migrated[mid] = {
                "ht_sent": v.get("kickoff", v.get("ht_sent", True)),
                "2t_sent": v.get("halftime", v.get("2t_sent", True)),
            }
    return migrated

def save_sent_alerts(sent: dict):
    SENT_ALERTS_FILE.write_text(json.dumps(sent, indent=2))

def _load_fixtures(path: Path) -> list:
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    return data.get("alerts", data) if isinstance(data, dict) else data

# ==========================
# CORE LOGIC
# ==========================
def check_and_send_alerts():
    now = datetime.now()
    tolerance = timedelta(minutes=TIME_TOLERANCE_MINUTES)

    sent_alerts = load_sent_alerts()

    ht_fixtures = _load_fixtures(HT_FILE)
    t2_fixtures = _load_fixtures(T2_FILE)
    logger.info("🔍 HT fixtures: %d | 2T fixtures: %d", len(ht_fixtures), len(t2_fixtures))

    ht_to_send = []   # (kickoff_dt, fixture)
    t2_to_send = []   # (kickoff_dt, fixture)

    def _kickoff(f) -> datetime | None:
        match_date = f.get("match_date", "")
        match_time = f.get("match_time", "") or "00:00"
        dt_str = f"{match_date}T{match_time}:00" if match_date else None
        return parser.isoparse(dt_str) if dt_str else None

    # ── Over 0.5 HT (+15min) ──
    for f in ht_fixtures:
        mid = f["match_id"]
        state = sent_alerts.setdefault(mid, {"ht_sent": False, "2t_sent": False})
        if state["ht_sent"]:
            continue
        ko = _kickoff(f)
        if ko and abs(now - (ko + timedelta(minutes=HT_OFFSET_MINUTES))) <= tolerance:
            ht_to_send.append((ko, f))
            state["ht_sent"] = True

    # ── Over 0.5 2T (+50min) ──
    for f in t2_fixtures:
        mid = f["match_id"]
        state = sent_alerts.setdefault(mid, {"ht_sent": False, "2t_sent": False})
        if state["2t_sent"]:
            continue
        ko = _kickoff(f)
        if ko and abs(now - (ko + timedelta(minutes=T2_OFFSET_MINUTES))) <= tolerance:
            t2_to_send.append((ko, f))
            state["2t_sent"] = True

    # ── Invia Over 0.5 HT ──
    if ht_to_send:
        from collections import defaultdict
        groups: dict = defaultdict(list)
        for ko, f in ht_to_send:
            groups[ko.strftime("%H:%M")].append(f)
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
                    f"👉 Controlla il punteggio → se *0-0* entra su *Over 0.5 1T*"
                )
            footer = f"\n{'─' * 30}\n_Kick-off {ko_time} · +15min · modello Over 2.5 Ultra Aggressivo_"
            send_telegram_message(header + "\n\n".join(blocks) + footer)
            logger.info("📤 [HT +15min] Inviato %s (%d partite)", ko_time, len(group))

    # ── Invia Over 0.5 2T ──
    if t2_to_send:
        from collections import defaultdict
        groups2: dict = defaultdict(list)
        for ko, f in t2_to_send:
            groups2[ko.strftime("%H:%M")].append(f)
        for ko_time, group in sorted(groups2.items()):
            header = f"⏱ *SECONDO TEMPO — Over 0.5 2° Tempo*\n{'─' * 30}\n"
            blocks = []
            for f in group:
                league = f.get("league_name") or f.get("league_id", "")
                a25 = next((x for x in f.get("alerts", []) if x["market"] == "over_25"), None)
                a15 = next((x for x in f.get("alerts", []) if x["market"] == "over_15"), None)
                if a25:
                    sig_line = f"📊 Over 2.5 `{a25['prob_cal']*100:.0f}%` · `cs {a25['confidence_score']:.0f}`"
                else:
                    sig_line = f"📊 Over 1.5 `cs {a15['confidence_score']:.0f}` · quota bassa ✓" if a15 else "📊 Segnale Over 1.5"
                blocks.append(
                    f"🏟 *{f['home_team']}* vs *{f['away_team']}*\n"
                    f"🏆 _{league}_\n"
                    f"{sig_line}\n"
                    f"👉 Se è *0-0* → entra su *Over 0.5 2T*"
                )
            footer = f"\n{'─' * 30}\n_Kick-off {ko_time} · +50min · modello Profeta v2_"
            send_telegram_message(header + "\n\n".join(blocks) + footer)
            logger.info("📤 [2T +50min] Inviato %s (%d partite)", ko_time, len(group))

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
