import logging
import time
import requests
from config import VIRTUALS_LIST_URL

logger = logging.getLogger(__name__)

MAX_RETRIES = 2
REQUEST_TIMEOUT = 20


def fetch_new_agents():
    """
    Retourne une liste de dicts:
    {address, name, symbol, chain, virtuals_url, volume24h_virtual, mcap_virtual}
    Montants encore en $VIRTUAL - conversion USD faite dans main.py

    Retry automatique en cas de timeout/erreur réseau, pour éviter de sauter
    un cycle entier de vérification des seuils (impact direct sur le délai
    de détection ressenti).
    """
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(VIRTUALS_LIST_URL, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            data = response.json()

            items = data.get("data", [])
            tokens = []

            for item in items:
                address = item.get("preToken") or item.get("tokenAddress")
                if not address:
                    continue

                status = item.get("status", "")
                virtuals_url = f"https://app.virtuals.io/prototypes/{address}"

                tokens.append({
                    "address": address,
                    "name": item.get("name", "Unknown"),
                    "symbol": item.get("symbol", ""),
                    "chain": item.get("chain", "BASE"),
                    "virtuals_url": virtuals_url,
                    "volume24h_virtual": item.get("volume24h", 0) or 0,
                    "mcap_virtual": item.get("mcapInVirtual", 0) or 0,
                    "status": status,
                    "created_at": item.get("createdAt"),
                })

            logger.info(f"Virtuals: {len(tokens)} tokens récupérés (tentative {attempt})")
            return tokens

        except requests.exceptions.RequestException as e:
            last_error = e
            logger.warning(f"Tentative {attempt}/{MAX_RETRIES} échouée: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(1.5)
        except Exception as e:
            logger.error(f"Erreur parsing Virtuals: {e}")
            return []

    logger.error(f"Erreur requête Virtuals après {MAX_RETRIES} tentatives: {last_error}")
    return []
