import logging
import time
import requests
from config import COINGECKO_PRICE_URL

logger = logging.getLogger(__name__)

_cache = {"price": None, "ts": 0}
CACHE_TTL = 300  # 5 min, pas besoin de re-fetch à chaque cycle de 60s


def get_virtual_usd_price() -> float:
    """Prix $VIRTUAL en USD, avec cache 5 min. Fallback sur dernier prix connu si erreur."""
    now = time.time()
    if _cache["price"] and (now - _cache["ts"]) < CACHE_TTL:
        return _cache["price"]

    try:
        response = requests.get(COINGECKO_PRICE_URL, timeout=10)
        response.raise_for_status()
        price = response.json()["virtual-protocol"]["usd"]
        _cache["price"] = price
        _cache["ts"] = now
        return price
    except Exception as e:
        logger.warning(f"Erreur prix VIRTUAL (fallback dernier prix connu): {e}")
        return _cache["price"] or 0
