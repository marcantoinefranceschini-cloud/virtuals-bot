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
from supabase import create_client
 
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
POLL_INTERVAL_SECONDS = float(os.getenv("POLL_INTERVAL_SECONDS", "30"))
VOLUME_THRESHOLD_USD = float(os.getenv("VOLUME_THRESHOLD_USD", "1000"))
PAGES_TO_SCAN = int(os.getenv("PAGES_TO_SCAN", "4"))
STATE_FILE = Path(os.getenv("STATE_FILE", "seen_virtuals_api.json"))

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

VIRTUALS_API = "https://api2.virtuals.io/api/virtuals"
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

REQUEST_PAUSE_S = 1.0
TELEGRAM_PAUSE_S = 0.1

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
            "User-Agent": "Mozilla/5.0 (compatible; virtuals-monitor/5.0)",
            "Accept": "application/json",
        }
    )
    return session

SESSION = build_session()

# ============= SUPABASE FUNCTIONS =============

def get_active_users():
    """Get list of active user chat_ids from Supabase"""
    if not supabase:
        return []
    try:
        resp = supabase.table('users').select('chat_id').eq('active', True).execute()
        return [row['chat_id'] for row in resp.data] if resp.data else []
    except Exception as e:
        log.error(f"Error getting users: {e}")
        return []

def add_user(chat_id):
    """Add user to Supabase"""
    if not supabase:
        return False
    try:
        # Check if exists
        resp = supabase.table('users').select('chat_id').eq('chat_id', chat_id).execute()
        if resp.data:
            log.info(f"✓ User {chat_id} already exists")
            return False
        
        # Add new user
        supabase.table('users').insert({'chat_id': chat_id, 'active': True}).execute()
        log.info(f"✓ User {chat_id} added")
        return True
    except Exception as e:
        log.error(f"Error adding user: {e}")
        return False

def remove_user(chat_id):
    """Remove user from Supabase"""
    if not supabase:
        return False
    try:
        supabase.table('users').update({'active': False}).eq('chat_id', chat_id).execute()
        log.info(f"✓ User {chat_id} removed")
        return True
    except Exception as e:
        log.error(f"Error removing user: {e}")
        return False

def record_alert(token_name, token_symbol):
    """Record an alert in Supabase"""
    if not supabase:
        return
    try:
        supabase.table('alerts').insert({
            'token_name': token_name,
            'token_symbol': token_symbol
        }).execute()
    except Exception as e:
        log.error(f"Error recording alert: {e}")

def get_stats():
    """Get bot statistics from Supabase"""
    if not supabase:
        return {"today": 0, "total": 0, "users": 0}
    try:
        # Total alerts
        resp_total = supabase.table('alerts').select('id', count='exact').execute()
        total = resp_total.count or 0
        
        # Alerts today (simple approach - get all and filter in Python)
        resp_alerts = supabase.table('alerts').select('created_at').execute()
        today_count = 0
        if resp_alerts.data:
            from datetime import datetime, date
            today = date.today()
            for alert in resp_alerts.data:
                alert_date = alert.get('created_at')
                if alert_date:
                    alert_date_obj = datetime.fromisoformat(alert_date.replace('Z', '+00:00')).date()
                    if alert_date_obj == today:
                        today_count += 1
        
        # Active users
        users_count = len(get_active_users())
        
        return {"today": today_count, "total": total, "users": users_count}
    except Exception as e:
        log.error(f"Error getting stats: {e}")
        return {"today": 0, "total": 0, "users": 0}


# ============= UTILITY FUNCTIONS =============

def safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default

def safe_str(value, default=""):
    return value if isinstance(value, str) else default

def escape_markdown(text):
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text

def format_num(value):
    if value is None:
        return "N/A"
    return f"{value:,.0f}".replace(",", " ")

# ============= API FUNCTIONS =============

def fetch_page(page):
    params = {
        "filters[status]": 1, 
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

def calculate_risk_score(agent):
    score = 0
    # Red flag: si concentration extrême
    if agent.get("top10HolderPercentage") is not None and agent.get("top10HolderPercentage") >= 95:
        return 1  

    # 1. Top 10 Holder % (max 2 pts)
    top10 = agent.get("top10HolderPercentage") or 0
    if top10 < 50:
        score += 2
    elif top10 < 90:
        score += 1
    
    # 2. Holder Count (max 2 pts)
    holders = agent.get("holderCount") or 0
    if holders > 10:
        score += 2
    elif holders > 5:
        score += 1
    
    # 3. Liquidity USD (max 2 pts)
    liquidity = agent.get("liquidityUsd") or 0
    if liquidity > 10000:
        score += 2
    elif liquidity > 5000:
        score += 1
    
    # 4. Dev Holding % (max 2 pts)
    dev_hold = agent.get("devHoldingPercentage") or 0
    if dev_hold == 0:
        score += 2
    elif dev_hold < 10:
        score += 1.5
    elif dev_hold < 30:
        score += 1
    
    # 5. Token Age (max 2 pts)
    launched = agent.get("launchedAt")
    if launched:
        from datetime import datetime
        try:
            launch_time = datetime.fromisoformat(launched.replace('Z', '+00:00'))
            age_hours = (datetime.now(launch_time.tzinfo) - launch_time).total_seconds() / 3600
            if age_hours > 24:
                score += 2
            elif age_hours > 12:
                score += 1.5
            elif age_hours > 6:
                score += 1
        except:
            pass
    
    # 6. isVerified (bonus 1 pt)
    if agent.get("isVerified"):
        score += 1
    
    return min(10, round(score, 1))


def get_risk_emoji(score):
    """Get emoji based on risk score"""
    if score >= 9:
        return "🟢"
    elif score >= 7:
        return "✅"
    elif score >= 5:
        return "⚠️"
    else:
        return "🚩"

def extract_agent(item):
    if not isinstance(item, dict):
        return None

    addr = safe_str(item.get("tokenAddress")) or safe_str(item.get("preToken"))
    if not addr:
        return None

    volume = safe_float(item.get("volume24h"))
    mcap = item.get("mcapVirtual")
    mcap = safe_float(mcap) if mcap is not None else None
    
    risk_score = calculate_risk_score(item)

    return {
        "id": item.get("id"),
        "tokenAddress": addr,
        "name": safe_str(item.get("name")),
        "symbol": safe_str(item.get("symbol")),
        "volume24h": volume,
        "mcapVirtual": mcap,
        "chain": safe_str(item.get("chain")),
        "createdAt": safe_str(item.get("createdAt")),
        "risk_score": risk_score,
        "holderCount": item.get("holderCount"),
        "top10HolderPercentage": item.get("top10HolderPercentage"),
        "liquidityUsd": item.get("liquidityUsd"),
        "devHoldingPercentage": item.get("devHoldingPercentage"),
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

# ============= MESSAGE FUNCTIONS =============

def build_message(agent):
    name = agent.get("name", "?")
    ticker = agent.get("symbol", "?")
    chain = agent.get("chain", "?")
    volume = agent.get("volume24h", 0)
    ca = agent.get("tokenAddress", "N/A")
    agent_id = agent.get("id")
    link = f"https://app.virtuals.io/virtuals/{agent_id}" if agent_id else "https://app.virtuals.io"
    
    return (
        f"🆕 {name} (${ticker})\n"
        f"⛓️ Chain : {chain}\n"
        f"💧 Volume 24h : ${format_num(volume)}\n"
        f"🔗 CA : `{ca}`\n"
        f"\n{link}"
    )

def send_telegram(chat_id, text):
    url = TELEGRAM_API.format(token=BOT_TOKEN, method="sendMessage")
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        resp = SESSION.post(url, json=payload, timeout=20)
        if resp.status_code == 200:
            return True
        log.warning("Telegram %d pour chat_id %s", resp.status_code, chat_id)
        return False
    except requests.RequestException as exc:
        log.error("Envoi Telegram échoué (chat_id %s) : %s", chat_id, exc)
        return False

# ============= TELEGRAM HANDLER =============

def get_user_threshold(chat_id):
    """Get user's volume threshold from Supabase"""
    if not supabase:
        return VOLUME_THRESHOLD_USD
    try:
        resp = supabase.table('users').select('volume_threshold').eq('chat_id', chat_id).execute()
        if resp.data and resp.data[0].get('volume_threshold'):
            return resp.data[0]['volume_threshold']
    except Exception as e:
        log.error(f"Error getting user threshold: {e}")
    return VOLUME_THRESHOLD_USD

def set_user_threshold(chat_id, threshold):
    """Set user's volume threshold in Supabase"""
    if not supabase:
        return False
    try:
        supabase.table('users').update({'volume_threshold': threshold}).eq('chat_id', chat_id).execute()
        log.info(f"✓ User {chat_id} threshold set to {threshold}")
        return True
    except Exception as e:
        log.error(f"Error setting threshold: {e}")
        return False

def handle_telegram_update(update):
    message = update.get("message", {})
    text = safe_str(message.get("text", "")).strip()
    user_id = message.get("from", {}).get("id")
    chat_id = message.get("chat", {}).get("id")

    if not user_id or not chat_id:
        return

    # Commands
    if text in ["/start", "/stop", "/help", "/status", "/stats", "/threshold"]:
        if text == "/start":
            if add_user(chat_id):
                send_telegram(user_id, "✅ Inscrit ! Tu recevras les alertes crypto > seuil.")
            else:
                send_telegram(user_id, "ℹ️ Déjà inscrit !")

        elif text == "/stop":
            remove_user(chat_id)
            send_telegram(user_id, "❌ Désinscrit.")

        elif text == "/help":
            help_text = (
                "/start — M'inscrire\n"
                "/stop — Me désinscrire\n"
                "/status — État du bot\n"
                "/threshold — Voir mon seuil\n"
                "/setthreshold 500 — Changer mon seuil\n"
                "/stats — Statistiques\n"
                "/help — Cette aide"
            )
            send_telegram(user_id, help_text)

        elif text == "/status":
            active_count = len(get_active_users())
            user_threshold = get_user_threshold(chat_id)
            status_text = f"👥 Users inscrits: {active_count}\n✅ Actifs: {active_count}\n💰 Ton seuil: {user_threshold}$"
            send_telegram(user_id, status_text)

        elif text == "/threshold":
            user_threshold = get_user_threshold(chat_id)
            threshold_text = f"💰 Ton seuil actuel : {user_threshold}$\n\nUtilise /setthreshold MONTANT pour le changer."
            send_telegram(user_id, threshold_text)

        elif text == "/stats":
            stats = get_stats()
            stats_text = f"""📊 Bot Statistics

🔔 Alerts Today: {stats['today']}
📈 Total Alerts: {stats['total']}
👥 Active Users: {stats['users']}"""
            send_telegram(user_id, stats_text)

    # /setthreshold command
    elif text.startswith("/setthreshold "):
        try:
            amount_str = text.replace("/setthreshold ", "").strip()
            amount = float(amount_str)
            if amount < 0:
                send_telegram(user_id, "❌ Le seuil doit être positif !")
            elif set_user_threshold(chat_id, amount):
                send_telegram(user_id, f"✅ Seuil changé à {amount}$ !")
            else:
                send_telegram(user_id, "❌ Erreur lors du changement du seuil.")
        except ValueError:
            send_telegram(user_id, "❌ Format invalide. Utilise : /setthreshold 500")


def process_telegram_updates():
    offset = 0
    while True:
        try:
            url = TELEGRAM_API.format(token=BOT_TOKEN, method="getUpdates")
            resp = SESSION.get(url, params={"offset": offset, "timeout": 30}, timeout=35)
            data = resp.json()
            if data.get("ok"):
                for update in data.get("result", []):
                    handle_telegram_update(update)
                    offset = update.get("update_id", 0) + 1
        except Exception as exc:
            log.error("Erreur polling Telegram : %s", exc)
        time.sleep(1)

# ============= MAIN CYCLE =============

def run_cycle(state):
    agents = fetch_new_agents()
    log.info(f"📊 Tokens trouvés: {len(agents)}")
    for agent in agents[:10]:  # Affiche les 10 premiers
        log.info(f"Token: {agent.get('name')} - Volume: ${agent.get('volume24h')} - Chain: {agent.get('chain')}")
    if not agents:
        log.warning("Aucun agent récupéré ce cycle.")
        return

    seen = state["seen"]

    if not state.get("initialized"):
        for agent in agents:
            volume = agent.get("volume24h", 0.0)
            # On initialise avec le seuil global
            if volume >= VOLUME_THRESHOLD_USD:
                seen[agent["tokenAddress"].lower()] = str(agent.get("createdAt") or time.time())
        state["initialized"] = True
        save_state(state)
        log.info("Initialisation : %d agents > seuil marqués comme vus.", len(seen))
        return

    alerts = 0
    active_users = get_active_users()
    
    for agent in agents:
        key = agent["tokenAddress"].lower()
        if key in seen:
            continue
    
        volume = agent.get("volume24h", 0)
        alert_sent = False
    
    # Envoyer à chaque user selon SON seuil
    for chat_id in active_users:
        user_threshold = get_user_threshold(chat_id)
        if volume >= user_threshold:
            message = build_message(agent)
            record_alert(agent.get("name"), agent.get("symbol"))
            
            if send_telegram(chat_id, message):
                alerts += 1
                alert_sent = True
                log.info(f"Alerte envoyée à {chat_id} : {agent.get('name')} (${agent.get('symbol')})")
            
            time.sleep(TELEGRAM_PAUSE_S)
    
    # SEULEMENT si une alerte a été envoyée, on marque comme vu
    if alert_sent:
        seen[key] = str(agent.get("createdAt") or time.time())
        save_state(state)

    log.info("Cycle : %d alertes envoyées à %d users.", alerts, len(active_users))


def main():
    if not BOT_TOKEN:
        log.critical("BOT_TOKEN doit être défini. Arrêt.")
        sys.exit(1)

    if not supabase:
        log.critical("SUPABASE_URL et SUPABASE_KEY doivent être définis. Arrêt.")
        sys.exit(1)

    def _shutdown(signum, _frame):
        log.info("Signal %s reçu — arrêt propre.", signum)
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    state = load_state()
    interval_s = min(120.0, max(10.0, POLL_INTERVAL_SECONDS))
    
    log.info("Démarrage (API Virtuals Multi-User) — polling toutes les %.0f sec, seuil %.0f$.", interval_s, VOLUME_THRESHOLD_USD)
    log.info("Users actuels : %d", len(get_active_users()))

    import threading
    tg_thread = threading.Thread(target=process_telegram_updates, daemon=True)
    tg_thread.start()

    while True:
        try:
            run_cycle(state)
        except Exception:
            log.exception("Erreur inattendue pendant le cycle.")
        time.sleep(interval_s)

if __name__ == "__main__":
    main()
