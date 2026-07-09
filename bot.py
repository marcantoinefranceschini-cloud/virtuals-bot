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

POLL_INTERVAL_MINUTES = float(os.getenv("POLL_INTERVAL_MINUTES", "2"))
VOLUME_THRESHOLD_USD = float(os.getenv("VOLUME_THRESHOLD_USD", "1000"))
STATE_FILE = Path(os.getenv("STATE_FILE", "seen_tokens_dex.json"))

VIRTUAL_TOKEN_ADDRESS = "0x0b3e328455c4059eeb9e3f84b5543f74e24e7e1b"
DEXSCREENER_URL = f"https://api.dexscreener.com/token-pairs/v1/base/{VIRTUAL_TOKEN_ADDRESS}"
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

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
            "User-Agent": "virtuals-monitor-bot/3.0",
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

def fetch_virtual_pairs():
    try:
        resp = SESSION.get(DEXSCREENER_URL, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.error("Échec récupération DexScreener : %s", exc)
        return []

    if not isinstance(data, list):
        log.error("Réponse DexScreener inattendue.")
        return []

    results = []
    for pair in data:
        if not isinstance(pair, dict):
            continue

        base = pair.get("baseToken", {}) or {}
        quote = pair.get("quoteToken", {}) or {}

        base_symbol = safe_str(base.get("symbol")).upper()
        quote_symbol = safe_str(quote.get("symbol")).upper()

        if base_symbol == "VIRTUAL":
            agent = quote
        elif quote_symbol == "VIRTUAL":
            agent = base
        else:
            continue

        addr = safe_str(agent.get("address"))
        if not addr:
            continue

        volume = safe_float((pair.get("volume") or {}).get("h24"))
        mcap = pair.get("marketCap")
        mcap = safe_float(mcap) if mcap is not None else pair.get("fdv")
        mcap = safe_float(mcap) if mcap is not None else None

        results.append(
            {
                "tokenAddress": addr,
                "name": safe_str(agent.get("name")),
                "symbol": safe_str(agent.get("symbol")),
                "volume24h": volume,
                "mcapUsd": mcap,
                "createdAt": pair.get("pairCreatedAt"),
                "url": safe_str(pair.get("url")),
            }
        )

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

def format_usd(value):
    if value is None:
        return "N/A"
    return f"{value:,.0f}".replace(",", " ")

def build_message(agent):
    name = escape_markdown(safe_str(agent.get("name"), "?"))
    ticker = escape_markdown(safe_str(agent.get("symbol"), "?"))
    volume = agent.get("volume24h", 0.0)
    mcap = agent.get("mcapUsd")
    ca = agent.get("tokenAddress", "N/A")
    link = agent.get("url") or f"https://dexscreener.com/base/{ca}"

    return (
        f"🆕 *{name}* (${ticker})\n"
        f"💧 Volume 24h : {format_usd(volume)}$\n"
        f"📊 Market cap : {format_usd(mcap)}$\n"
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
    agents = fetch_virtual_pairs()
    if not agents:
        log.warning("Aucune paire $VIRTUAL récupérée ce cycle.")
        return

    seen = state["seen"]

    if not state.get("initialized"):
        for agent in agents:
            seen[agent["tokenAddress"].lower()] = str(agent.get("createdAt") or time.time())
        state["initialized"] = True
        save_state(state)
        log.info("Initialisation : %d paires $VIRTUAL marquées comme déjà vues.", len(seen))
        return

    alerts = 0
    for agent in agents:
        key = agent["tokenAddress"].lower()
        if key in seen:
            continue

        volume = agent.get("volume24h", 0.0)
        log.info(f"DEBUG: {agent.get('symbol')} vol={volume}")
        if volume >= VOLUME_THRESHOLD_USD:
            message = build_message(agent)
            if send_telegram(message):
                seen[key] = str(agent.get("createdAt") or time.time())
                alerts += 1
                log.info("Alerte envoyée : %s ($%s) vol=%.0f$", agent.get("name"), agent.get("symbol"), volume)
                save_state(state)
                time.sleep(TELEGRAM_PAUSE_S)
            else:
                log.error("Alerte NON envoyée pour %s", agent.get("name"))
        else:
            seen[key] = str(agent.get("createdAt") or time.time())

    if alerts == 0:
        log.info("Cycle terminé : %d paires $VIRTUAL scannées.", len(agents))
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
    log.info("Démarrage (DexScreener) — polling toutes les %.0f sec, seuil %.0f$.", interval_s, VOLUME_THRESHOLD_USD)

    while True:
        try:
            run_cycle(state)
        except Exception:
            log.exception("Erreur inattendue pendant le cycle.")
        time.sleep(interval_s)

if __name__ == "__main__":
    main()
