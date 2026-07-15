import logging
from datetime import datetime, timedelta, timezone
from supabase import create_client
from config import SUPABASE_URL, SUPABASE_KEY, TRACKING_WINDOW_HOURS

logger = logging.getLogger(__name__)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def upsert_user(chat_id: int):
    existing = supabase.table("users").select("chat_id").eq("chat_id", chat_id).execute()
    if not existing.data:
        supabase.table("users").insert({
            "chat_id": chat_id, "active": True, "volume_threshold": 1000,
        }).execute()
        logger.info(f"Nouvel utilisateur: {chat_id}")


def set_user_active(chat_id: int, active: bool):
    supabase.table("users").update({"active": active}).eq("chat_id", chat_id).execute()


def update_threshold(chat_id: int, volume: float):
    supabase.table("users").update({"volume_threshold": volume}).eq("chat_id", chat_id).execute()


def get_threshold(chat_id: int) -> float:
    result = supabase.table("users").select("volume_threshold").eq("chat_id", chat_id).execute()
    return result.data[0]["volume_threshold"] if result.data else 1000


def get_active_users():
    result = supabase.table("users").select("chat_id, volume_threshold").eq("active", True).execute()
    return result.data or []


def get_users_for_new_listings():
    """Users actifs qui veulent les alertes instantanées de nouveaux tokens"""
    result = supabase.table("users").select("chat_id").eq(
        "active", True
    ).eq("notify_new_listings", True).execute()
    return result.data or []


def set_new_listings_pref(chat_id: int, enabled: bool):
    supabase.table("users").update({"notify_new_listings": enabled}).eq("chat_id", chat_id).execute()


def upsert_token(token: dict) -> bool:
    """True si nouveau token"""
    existing = supabase.table("tokens").select("token_address").eq(
        "token_address", token["address"]
    ).execute()
    if not existing.data:
        insert_data = {
            "token_address": token["address"],
            "name": token.get("name"),
            "symbol": token.get("symbol"),
            "chain": token.get("chain"),
            "virtuals_url": token.get("virtuals_url"),
            "last_volume_24h": 0,
        }
        # Vraie date de lancement du token si dispo, sinon Supabase mettra NOW() par défaut
        if token.get("created_at"):
            insert_data["first_seen_at"] = token["created_at"]
        supabase.table("tokens").insert(insert_data).execute()
        return True
    return False


def update_token_stats(token_address: str, volume_24h_usd: float, marketcap_usd: float):
    supabase.table("tokens").update({
        "last_volume_24h": volume_24h_usd,
        "last_marketcap": marketcap_usd,
        "last_checked_at": datetime.now(timezone.utc).isoformat(),
    }).eq("token_address", token_address).execute()


def get_tracked_tokens():
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=TRACKING_WINDOW_HOURS)).isoformat()
    result = supabase.table("tokens").select("*").gte("first_seen_at", cutoff).execute()
    return result.data or []


def already_alerted(chat_id: int, token_address: str, alert_type: str = "threshold") -> bool:
    result = supabase.table("user_alerts").select("id").eq(
        "chat_id", chat_id
    ).eq("token_address", token_address).eq("alert_type", alert_type).execute()
    return len(result.data) > 0


def mark_alerted(chat_id: int, token_address: str, volume_24h: float, alert_type: str = "threshold"):
    try:
        supabase.table("user_alerts").insert({
            "chat_id": chat_id, "token_address": token_address,
            "volume_24h_at_alert": volume_24h, "alert_type": alert_type,
        }).execute()
    except Exception as e:
        logger.warning(f"mark_alerted skip: {e}")
