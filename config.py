import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Endpoint confirmé via DevTools le 15/07/2026
VIRTUALS_LIST_URL = os.getenv(
    "VIRTUALS_LIST_URL",
    "https://api2.virtuals.io/api/virtuals"
    "?filters[status]=5"
    "&sort[0]=age%3Adesc&sort[1]=createdAt%3Adesc"
    "&populate[0]=image"
    "&pagination[page]=1&pagination[pageSize]=100"
)

COINGECKO_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price?ids=virtual-protocol&vs_currencies=usd"

POLLING_INTERVAL = int(os.getenv("POLLING_INTERVAL", 60))
# Vu le rythme de lancement (~25 tokens/30min observé), une fenêtre de 72h
# avec seulement 100 résultats/page ne couvrirait pas tout. On réduit la
# fenêtre à 6h: au-delà, un token "nouveau" n'a plus vraiment d'intérêt sniping.
TRACKING_WINDOW_HOURS = int(os.getenv("TRACKING_WINDOW_HOURS", 6))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
