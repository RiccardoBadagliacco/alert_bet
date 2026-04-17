#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import logging
import urllib.request
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
# SOFASCORE LIVE DATA
# ==========================
_SOFA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": "https://www.sofascore.com/",
}

def _sofa_get(url: str) -> dict | None:
    try:
        req = urllib.request.Request(url, headers=_SOFA_HEADERS)
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())
    except Exception as e:
        logger.warning("Sofascore fetch error %s: %s", url, e)
        return None

def fetch_live_context(match_id: int) -> dict:
    """
    Ritorna dict con:
      home_score, away_score, home_red, away_red,
      home_shots_on, away_shots_on, home_xg, away_xg,
      should_alert (bool), skip_reason (str|None)
    """
    result = {
        "home_score": None, "away_score": None,
        "home_red": 0, "away_red": 0,
        "home_shots_on": None, "away_shots_on": None,
        "home_xg": None, "away_xg": None,
        "should_alert": True, "skip_reason": None,
    }

    # Score live via event endpoint
    event_data = _sofa_get(f"https://api.sofascore.com/api/v1/event/{match_id}")
    if event_data:
        ev = event_data.get("event", {})
        hs = ev.get("homeScore", {})
        aws = ev.get("awayScore", {})
        result["home_score"] = hs.get("current")
        result["away_score"] = aws.get("current")

        # Se già segnato → skip (over 0.5 già centrato, quota crollata)
        home_cur = result["home_score"] or 0
        away_cur = result["away_score"] or 0
        if home_cur + away_cur > 0:
            result["should_alert"] = False
            result["skip_reason"] = f"già {home_cur}-{away_cur}"
            return result

    # Statistics (shots on target, xG, red cards)
    stats_data = _sofa_get(f"https://api.sofascore.com/api/v1/event/{match_id}/statistics")
    if stats_data:
        for period_block in stats_data.get("statistics", []):
            if period_block.get("period") != "ALL":
                continue
            for group in period_block.get("groups", []):
                for item in group.get("statisticsItems", []):
                    name = item.get("name", "")
                    if name == "Shots on target":
                        result["home_shots_on"] = item.get("home")
                        result["away_shots_on"] = item.get("away")
                    elif name == "Expected goals":
                        result["home_xg"] = item.get("home")
                        result["away_xg"] = item.get("away")
                    elif name == "Yellow cards":
                        pass  # non ci serve
            break

    # Incidents: conta cartellini rossi
    incidents_data = _sofa_get(f"https://api.sofascore.com/api/v1/event/{match_id}/incidents")
    if incidents_data:
        for inc in incidents_data.get("incidents", []):
            if inc.get("incidentType") == "card" and inc.get("incidentClass") == "red":
                if inc.get("isHome"):
                    result["home_red"] += 1
                else:
                    result["away_red"] += 1

    return result


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
        sent_any = False
        for ko, f, time_known in to_send_v4:
            mid = f["match_id"]
            ko_time = ko.strftime("%H:%M")
            ko_label = ko_time if time_known else "orario n.d."
            time_note = f"`{ko_time}`" if time_known else "_orario n.d._"

            # ── Fetch live context da Sofascore ───────────────────────────────
            ctx = fetch_live_context(mid)

            if not ctx["should_alert"]:
                logger.info("⏭️  v4 skip %s (%s): %s", mid, f.get("home_team"), ctx["skip_reason"])
                # Marca sent lo stesso — partita già segnata, non ha senso inviare dopo
                sent_alerts[f"v4_{mid}"]["sent"] = True
                continue

            league = f.get("league_name", "")
            prob = f.get("prob_cal", "")
            conf = (f.get("confidence") or "").upper()
            prob_str = f" · prob {prob:.0%}" if isinstance(prob, float) else ""

            # Score line
            hs = ctx["home_score"]
            aws = ctx["away_score"]
            score_str = f"`{hs}-{aws}`" if hs is not None and aws is not None else "_score n.d._"

            # Stats line
            stats_parts = []
            h_sot = ctx["home_shots_on"]
            a_sot = ctx["away_shots_on"]
            if h_sot is not None:
                stats_parts.append(f"Tiri in porta: {h_sot}-{a_sot}")
            h_xg = ctx["home_xg"]
            a_xg = ctx["away_xg"]
            if h_xg is not None:
                try:
                    stats_parts.append(f"xG: {float(h_xg):.2f}-{float(a_xg):.2f}")
                except Exception:
                    pass
            stats_line = "  |  ".join(stats_parts) if stats_parts else ""

            # Red cards
            red_parts = []
            if ctx["home_red"]:
                red_parts.append(f"🟥 {f['home_team']} ({ctx['home_red']})")
            if ctx["away_red"]:
                red_parts.append(f"🟥 {f['away_team']} ({ctx['away_red']})")
            red_line = "\n" + "  ".join(red_parts) if red_parts else ""

            block = (
                f"⚽ *OVER 0.5 — Profeta v4*\n{'─' * 30}\n"
                f"🏟 *{f['home_team']}* vs *{f['away_team']}*\n"
                f"⏰ {time_note}  ·  {score_str}  [{conf}{prob_str}]\n"
                f"🏆 _{league}_"
            )
            if stats_line:
                block += f"\n📊 _{stats_line}_"
            if red_line:
                block += red_line
            footer = f"\n{'─' * 30}\n_+30min dal kick-off {ko_label} UTC_"

            try:
                send_telegram_message(block + footer)
                sent_alerts[f"v4_{mid}"]["sent"] = True
                save_sent_alerts(sent_alerts)
                sent_any = True
                logger.info("📤 v4 inviato match_id=%s (%s vs %s)", mid, f["home_team"], f["away_team"])
            except Exception:
                logger.exception("❌ Telegram fallito match_id=%s — riprovo al prossimo ciclo", mid)

        if not sent_any:
            logger.info("📭 v4: tutti skippati (già segnato)")
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

@app.get("/test-sofa/{match_id}")
async def test_sofa(match_id: int):
    ctx = fetch_live_context(match_id)
    return ctx
