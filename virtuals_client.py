import logging
import requests
from config import VIRTUALS_LIST_URL

logger = logging.getLogger(__name__)


def fetch_new_agents():
    """
    Retourne une liste de dicts:
    {address, name, symbol, chain, virtuals_url, volume24h_virtual, mcap_virtual}
    Montants encore en $VIRTUAL - conversion USD faite dans main.py
    """
    try:
        response = requests.get(VIRTUALS_LIST_URL, timeout=15)
        response.raise_for_status()
        data = response.json()

        items = data.get("data", [])
        tokens = []

        for item in items:
            address = item.get("preToken") or item.get("tokenAddress")
            if not address:
                continue

            token_id = item.get("id")
            status = item.get("status", "")
            # UNDERGRAD = bonding curve en cours -> page "prototypes"
            # (à ajuster si un jour on voit un token gradué "SENTIENT" dans le flux)
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
                "created_at": item.get("createdAt"),  # vraie date de lancement, format ISO UTC
            })

        logger.info(f"Virtuals: {len(tokens)} tokens récupérés")
        return tokens

    except requests.exceptions.RequestException as e:
        logger.error(f"Erreur requête Virtuals: {e}")
        return []
    except Exception as e:
        logger.error(f"Erreur parsing Virtuals: {e}")
        return []
