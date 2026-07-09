#!/usr/bin/env python3
"""
Bot Telegram — surveillance des nouvelles paires $VIRTUAL sur Base.
"""

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

POLL_INTERVAL_MINUTES = float(os.getenv("POLL_INTERVAL_MINUTES", "5"))
VOLUME_THRESHOLD_USD = float(os.getenv("VOLUME_THRESHOLD_USD", "5000"))
PAGES_TO_SCAN = int(os.getenv("PAGES_TO_SCAN", "2"))
STATE_FILE = Path(os.getenv("STATE_FILE", "seen_tokens.json"))
DESC_MAX_LEN = 300

VIRTUAL_TOKEN_ADDRESS = "0x0b3e328455c4059eeb9e3f84b5543f74e24e7e1b"

GECKOTERMINAL_NEW_POOLS_URL = "https://api.geckoterminal.com/api/v2/networks/base/new_pools"
VIRTUALS_LOOKUP_URL = "https://api.virtuals.io/api/virtuals"
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
            "User-Agent": "virtuals-monitor-bot/2.0",
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

def fetch_new_pools_page(page):
    params = {"include": "base_token,quote_token", "page": page}
    resp = SESSION.get(GECKOTERMINAL_NEW_POOLS_URL, params=params, timeout=20)
    resp.raise_for_status()

    payload = resp.json()
    pools = payload.get("data")
    if not isinstance(pools, list):
        raise ValueError(f"Réponse GeckoTerminal inattendue (page {page})")

    included = payload.get("included") or []
    token_lookup = {
        item["id"]: item.get("attributes", {})
        for item in included
        if isinstance(item, dict) and item.get("type") == "token" and "id" in item
    }
    return [p for p in pools if isinstance(p, dict)], token_lookup

def extract_agent_side(pool, token_lookup):
    rels = pool.get("relationships", {})
    base_id = (rels.get("base_token") or {}).get("data", {}).get("id")
    quote_id = (rels.get("quote_token") or {}).get("data", {}).get("id")

    base_attrs = token_lookup.get(base_id, {})
    quote_attrs = token_lookup.get(quote_id, {})

    base_symbol = safe_str(base_attrs.get("symbol")).upper()
    quote_symbol = safe_str(quote_attrs.get("symbol")).upper()

    log.info(f"DEBUG: base={base_symbol}, quote={quote_symbol}")

    if base_symbol == "VIRTUAL":
        agent_attrs = quote_attrs
    elif quote_symbol == "VIRTUAL":
        agent_attrs = base_attrs
    else:
        return None
    addr = safe_str(agent_attrs.get("address"))
    if not addr:
        return None

    attrs = pool.get("attributes", {})
    volume = safe_float((attrs.get("volume_usd") or {}).get("h24"))
    mcap_raw = attrs.get("market_cap_usd")
    mcap = safe_float(mcap_raw) if mcap_raw is not None else attrs.get("fdv_usd")
    mcap = safe_float(mcap) if mcap is not None else None

    return {
        "tokenAddress": addr,
        "name": safe_str(agent_attrs.get("name")) or safe_str(attrs.get("name")),
        "symbol": safe_str(agent_attrs.get("symbol")),
        "volume24h": volume,
        "mcapUsd": mcap,
        "createdAt": safe_str(attrs.get("pool_created_at")),
        "poolAddress": safe_str(attrs.get("address")),
    }
def fetch_new_virtual_pairs():
    seen_this_scan = set()
    results = []
    for page in range(1, PAGES_TO_SCAN + 1):
        try:
            pools, token_lookup = fetch_new_pools_page(page)
        except Exception as exc:
            log.error("Échec récupération new_pools page %d : %s", page, exc)
            break
        for pool in pools:
            agent = extract_agent_side(pool, token_lookup)
            if agent and agent["tokenAddress"].lower() not in seen_this_scan:
                seen_this_scan.add(agent["tokenAddress"].lower())
                results.append(agent)
        if len(pools) < 20:
            break
        time.sleep(REQUEST_PAUSE_S)
    return results

def try_enrich_from_virtuals_api(token_address):
    try:
        resp = SESSION.get(
            VIRTUALS_LOOKUP_URL,
            params={"filters[tokenAddress][$eq]": token_address},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json().get("data")
        if not isinstance(data, list):
            return None
        for entry in data:
            if safe_str(entry.get("tokenAddress")).lower() == token_address.lower():
                return entry
    except Exception as exc:
        log.debug("Enrichissement Virtuals API échoué pour %s : %s", token_address, exc)
    return None

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

def build_message(agent, virtuals_data):
    name = escape_markdown(safe_str(agent.get("name"), "?"))
    ticker = escape_markdown(safe_str(agent.get("symbol"), "?"))
    volume = agent.get("volume24h", 0.0)
    mcap = agent.get("mcapUsd")
    ca = agent.get("tokenAddress", "N/A")

    if virtuals_data:
        desc = safe_str(virtuals_data.get("description")).strip().replace("\n", " ")
        if len(desc) > DESC_MAX_LEN:
            desc = desc[: DESC_MAX_LEN - 1].rstrip() + "…"
        desc = escape_markdown(desc) or "_(pas de description)_"
        vid = virtuals_data.get("id")
        link = (
            f"https://app.virtuals.io/virtuals/{vid}"
            if vid else f"https://www.geckoterminal.com/base/pools/{agent.get('poolAddress', '')}"
        )
        name = escape_markdown(safe_str(virtuals_data.get("name")) or agent.get("name", "?"))
        ticker = escape_markdown(safe_str(virtuals_data.get("symbol")) or agent.get("symbol", "?"))
    else:
        desc = "_Description indisponible (agent non trouvé sur l'API Virtuals)_"
        link = f"https://www.geckoterminal.com/base/pools/{agent.get('poolAddress', '')}"

    return (
        f"🆕 *{name}* (${ticker})\n"
        f"💧 Volume 24h : {format_usd(volume)}$\n"
        f"📊 Market cap : {format_usd(mcap)}$\n"
        f"🧠 {desc}\n"
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
    agents = fetch_new_virtual_pairs()
    if not agents:
        log.warning("Aucune nouvelle pool $VIRTUAL récupérée ce cycle.")
        return

    seen = state["seen"]

    if not state.get("initialized"):
        for agent in agents:
            seen[agent["tokenAddress"].lower()] = agent.get("createdAt") or time.strftime("%Y-%m-%dT%H:%M:%SZ")
        state["initialized"] = True
        save_state(state)
        log.info("Initialisation : %d paires marquées comme déjà vues.", len(seen))
        return

    alerts = 0
    for agent in agents:
        key = agent["tokenAddress"].lower()
        if key in seen:
            continue

        volume = agent.get("volume24h", 0.0)
        if volume > VOLUME_THRESHOLD_USD:
            virtuals_data = try_enrich_from_virtuals_api(agent["tokenAddress"])
            message = build_message(agent, virtuals_data)
            if send_telegram(message):
                seen[key] = agent.get("createdAt") or time.strftime("%Y-%m-%dT%H:%M:%SZ")
                alerts += 1
                log.info("Alerte envoyée : %s ($%s) vol=%.0f$", agent.get("name"), agent.get("symbol"), volume)
                save_state(state)
                time.sleep(TELEGRAM_PAUSE_S)
            else:
                log.error("Alerte NON envoyée pour %s — retentera au prochain cycle.", agent.get("name"))

    if alerts == 0:
        log.info("Cycle terminé : %d paires scannées.", len(agents))

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
    interval_s = max(60.0, POLL_INTERVAL_MINUTES * 60)
    log.info(
        "Démarrage — polling toutes les %.0f min, seuil %.0f$.",
        interval_s / 60, VOLUME_THRESHOLD_USD,
    )

    while True:
        try:
            run_cycle(state)
        except Exception:
            log.exception("Erreur inattendue pendant le cycle.")
        time.sleep(interval_s)

if __name__ == "__main__":
    main()
