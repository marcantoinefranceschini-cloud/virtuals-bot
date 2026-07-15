import logging
import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from config import TELEGRAM_BOT_TOKEN, POLLING_INTERVAL, LOG_LEVEL
import db
import virtuals_client as vc
import price_client as pc

logging.basicConfig(
    level=LOG_LEVEL,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = None


# ===== COMMANDES =====

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db.upsert_user(chat_id)
    db.set_user_active(chat_id, True)
    await update.message.reply_text(
        "🚀 Virtuals Sniper Bot activé!\n\n"
        "📋 Commandes:\n"
        "/setseuil <volume_usd> - Seuil de volume 24h en $ (ex: /setseuil 5000)\n"
        "/getseuil - Voir ton seuil actuel\n"
        "/stop - Désactiver les alertes\n\n"
        "💡 Seuil par défaut: $1000\n"
        "Alerte dès qu'un token franchit ton seuil, même après sa découverte."
    )


async def set_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) != 1:
        await update.message.reply_text("❌ Usage: /setseuil <volume>\nEx: /setseuil 5000")
        return
    try:
        volume = float(context.args[0])
        if volume < 0:
            raise ValueError
        chat_id = update.effective_chat.id
        db.update_threshold(chat_id, volume)
        await update.message.reply_text(f"✅ Seuil défini à ${volume:,.2f}")
    except ValueError:
        await update.message.reply_text("❌ Entrer un nombre positif valide")


async def get_threshold_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    threshold = db.get_threshold(chat_id)
    await update.message.reply_text(f"📊 Ton seuil actuel: ${threshold:,.2f}")


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db.set_user_active(chat_id, False)
    await update.message.reply_text("🛑 Alertes désactivées. Réactiver: /start")


# ===== FORMAT MESSAGE =====

def format_alert(token: dict, volume_24h_usd: float, marketcap_usd: float) -> str:
    return (
        f"🎯 <b>SEUIL FRANCHI</b>\n\n"
        f"📛 Nom: <b>{token['name']}</b> ({token.get('symbol', '')})\n"
        f"⛓ Chain: <b>{token.get('chain', 'BASE')}</b>\n"
        f"💰 Volume 24h: <b>${volume_24h_usd:,.2f}</b>\n"
        f"📊 Marketcap: <b>${marketcap_usd:,.2f}</b>\n"
        f"📍 CA: <code>{token['token_address']}</code>\n"
        f"🔗 <a href=\"{token['virtuals_url']}\">Voir sur Virtuals</a>\n\n"
        f"⚠️ DYOR - Pas de conseil financier"
    )


# ===== BOUCLE PRINCIPALE =====

async def scan_loop():
    logger.info("Scan loop démarré")
    while True:
        try:
            virtual_price = pc.get_virtual_usd_price()
            if not virtual_price:
                logger.warning("Prix VIRTUAL indisponible, on saute ce cycle")
                await asyncio.sleep(POLLING_INTERVAL)
                continue

            # 1. Découvrir les nouveaux tokens (endpoint donne déjà volume+mcap)
            new_agents = vc.fetch_new_agents()
            new_count = 0
            for agent in new_agents:
                if db.upsert_token(agent):
                    new_count += 1
            if new_count:
                logger.info(f"{new_count} nouveaux tokens découverts")

            # 2. Mettre à jour les stats de TOUS les tokens reçus dans ce fetch
            agents_by_address = {a["address"]: a for a in new_agents}

            tracked = db.get_tracked_tokens()
            active_users = db.get_active_users()

            for token in tracked:
                agent = agents_by_address.get(token["token_address"])
                if agent is None:
                    continue  # pas dans ce batch, on le recroisera au prochain cycle

                volume_24h_usd = agent["volume24h_virtual"] * virtual_price
                marketcap_usd = agent["mcap_virtual"] * virtual_price

                db.update_token_stats(token["token_address"], volume_24h_usd, marketcap_usd)

                for user in active_users:
                    if volume_24h_usd < user["volume_threshold"]:
                        continue
                    if db.already_alerted(user["chat_id"], token["token_address"]):
                        continue

                    msg = format_alert(token, volume_24h_usd, marketcap_usd)
                    try:
                        await app.bot.send_message(
                            user["chat_id"], msg,
                            parse_mode="HTML", disable_web_page_preview=True
                        )
                        db.mark_alerted(user["chat_id"], token["token_address"], volume_24h_usd)
                        logger.info(f"Alerte: {token['name']} -> {user['chat_id']}")
                    except Exception as e:
                        logger.error(f"Erreur envoi à {user['chat_id']}: {e}")

            await asyncio.sleep(POLLING_INTERVAL)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Erreur scan_loop: {e}")
            await asyncio.sleep(POLLING_INTERVAL)


async def main():
    global app
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setseuil", set_threshold))
    app.add_handler(CommandHandler("getseuil", get_threshold_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))

    asyncio.create_task(scan_loop())
    logger.info("Bot polling démarré")
    await app.run_polling()


if __name__ == "__main__":
    asyncio.run(main())
