#!/usr/bin/env python3
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

POLL_INTERVAL_MINUTES = float(os.getenv("POLL_INTERVAL_MINUTES", "1"))
VOLUME_THRESHOLD_USD = float(os.getenv("VOLUME_THRESHOLD_USD", "1000"))
PAGES_TO_SCAN = int(os.getenv("PAGES_TO_SCAN", "2"))
STATE_FILE = Path(os.getenv("STATE_FILE", "seen_virtuals_api.json"))

VIRTUALS_API = "https://api2.virtuals.io/api/virtuals"
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

REQUEST_PAUSE_S = 1.0
TELEGRAM_PAUSE_S = 1.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("virtuals-bot")

def build_session():
    session = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (compatible; virtuals-monitor/4.0)",
            "Accept": "application/json",
        }
    )
    return session

SESSION = build_session()

def safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default

def safe_str(value, default=""):
    return value if isinstance(value, str) else default

def fetch_page(page):
    params = {
        "filters[status]": 5,
        "sort[0]": "createdAt:desc",
        "sort[1]": "volume24h:desc",
        "populate[0]": "image",
        "pagination[page]": page,
        "pagination[pageSize]": 25,
    }

    resp = SESSION.get(VIRTUALS_API, params=params, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    data = payload.get("data")
    if not isinstance(data, list):
        raise ValueError(f"Réponse Virtuals inattendue (page {page})")
    return data

def extract_agent(item):
    if not isinstance(item, dict):
        return None

    addr = safe_str(item.get("tokenAddress")) or safe_str(item.get("preToken"))
    if not addr:
        return None

    volume = safe_float(item.get("volume24h"))
    mcap = item.get("mcapInVirtual")
    mcap = safe_float(mcap) if mcap is not None else None

    return {
        "id": item.get("id"),
        "tokenAddress": addr,
        "name": safe_str(item.get("name")),
        "symbol": safe_str(item.get("symbol")),
        "volume24h": volume,
        "mcapVirtual": mcap,
        "chain": safe_str(item.get("chain")),
        "createdAt": safe_str(item.get("createdAt")),
    }

def fetch_new_agents():
    results = []
    seen_ids = set()
    for page in range(1, PAGES_TO_SCAN + 1):
        try:
            items = fetch_page(page)
        except Exception as exc:
            log.error("Échec récupération Virtuals page %d : %s", page, exc)
            break
        for item in items:
            agent = extract_agent(item)
            if agent and agent["tokenAddress"].lower() not in seen_ids:
                seen_ids.add(agent["tokenAddress"].lower())
                results.append(agent)
        if len(items) < 25:
            break
        time.sleep(REQUEST_PAUSE_S)
    return results

def load_state():
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(state, dict) and isinstance(state.get("seen"), dict):
                return state
        except (json.JSONDecodeError, OSError) as exc:
            log.error("Fichier d'état corrompu (%s) — réinitialisation.", exc)
    return {"initialized": False, "seen": {}}

def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except OSError as exc:
        log.error("Impossible d'écrire l'état : %s", exc)

def escape_markdown(text):
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text

def format_num(value):
    if value is None:
        return "N/A"
    return f"{value:,.0f}".replace(",", " ")

def build_message(agent):
    name = escape_markdown(safe_str(agent.get("name"), "?"))
    ticker = escape_markdown(safe_str(agent.get("symbol"), "?"))
    volume = agent.get("volume24h", 0.0)
    mcap = agent.get("mcapVirtual")
    chain = escape_markdown(safe_str(agent.get("chain"), "?"))
    ca = agent.get("tokenAddress", "N/A")
    agent_id = agent.get("id")
    link = f"https://app.virtuals.io/virtuals/{agent_id}" if agent_id else f"https://app.virtuals.io"

    mcap_line = f"📊 Market cap : {format_num(mcap)} $VIRTUAL\n" if mcap is not None else ""

    return (
        f"🆕 *{name}* (${ticker})\n"
        f"⛓ Chain : {chain}\n"
        f"💧 Volume 24h : {format_num(volume)}$\n"
        f"{mcap_line}"
        f"🔗 CA : `{ca}`\n"
        f"👉 {link}"
    )

def send_telegram(text):
    url = TELEGRAM_API.format(token=BOT_TOKEN, method="sendMessage")
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        resp = SESSION.post(url, json=payload, timeout=20)
        if resp.status_code == 200:
            return True
        log.warning("Telegram %d : nouvel essai sans Markdown", resp.status_code)
        payload.pop("parse_mode", None)
        resp = SESSION.post(url, json=payload, timeout=20)
        return resp.status_code == 200
    except requests.RequestException as exc:
        log.error("Envoi Telegram échoué : %s", exc)
        return False

def run_cycle(state):
    agents = fetch_new_agents()
    if not agents:
        log.warning("Aucun agent récupéré ce cycle.")
        return

    seen = state["seen"]

    if not state.get("initialized"):
        for agent in agents:
            seen[agent["tokenAddress"].lower()] = str(agent.get("createdAt") or time.time())
        state["initialized"] = True
        save_state(state)
        log.info("Initialisation : %d agents marqués comme déjà vus.", len(seen))
        return

    alerts = 0
    for agent in agents:
        key = agent["tokenAddress"].lower()
        if key in seen:
            continue

        volume = agent.get("volume24h", 0.0)
        if volume >= VOLUME_THRESHOLD_USD:
            message = build_message(agent)
            if send_telegram(message):
                seen[key] = str(agent.get("createdAt") or time.time())
                alerts += 1
                log.info("Alerte : %s ($%s) vol=%.0f$ chain=%s", agent.get("name"), agent.get("symbol"), volume, agent.get("chain"))
                save_state(state)
                time.sleep(TELEGRAM_PAUSE_S)
            else:
                log.error("Alerte NON envoyée pour %s", agent.get("name"))
        else:
            seen[key] = str(agent.get("createdAt") or time.time())

    if alerts == 0:
        log.info("Cycle terminé : %d agents scannés, aucun nouveau > seuil.", len(agents))
    save_state(state)

def main():
    if not BOT_TOKEN or not CHAT_ID:
        log.critical("BOT_TOKEN et CHAT_ID doivent être définis. Arrêt.")
        sys.exit(1)

    def _shutdown(signum, _frame):
        log.info("Signal %s reçu — arrêt propre.", signum)
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    state = load_state()
    interval_s = max(30.0, POLL_INTERVAL_MINUTES * 60)
    log.info("Démarrage (API Virtuals) — polling toutes les %.0f sec, seuil %.0f$.", interval_s, VOLUME_THRESHOLD_USD)

    while True:
        try:
            run_cycle(state)
        except Exception:
            log.exception("Erreur inattendue pendant le cycle.")
        time.sleep(interval_s)

if __name__ == "__main__":
    main()
