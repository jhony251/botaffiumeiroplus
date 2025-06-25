import logging
import re
import threading
import time
import hashlib
from urllib.parse import urlparse

import requests
from telegram import Message, Update
from telegram.ext import (
    Application,
    CallbackContext,
    CommandHandler,
    Defaults,
    MessageHandler,
    filters,
)

from config import ConfigurationManager
from amazon.paapi import AmazonAPI

logger = logging.getLogger(__name__)

# Inicializamos config
config_manager = ConfigurationManager()

# =======================
# AliExpress API Handler
# =======================

class AliexpressAPIHandler:
    BASE_URL = "https://api.alibaba.com/openapi/param2/2/portals.open/api.getPromotionProductDetail/"

    def __init__(self, config):
        self.config = config
        self.api_key = config.aliexpress_app_key
        self.secret = config.aliexpress_app_secret

    def _generate_signature(self, api_path: str, params: dict) -> str:
        sorted_params = "".join(f"{k}{v}" for k, v in sorted(params.items()))
        sign_string = f"{self.secret}{api_path}{sorted_params}{self.secret}"
        return hashlib.md5(sign_string.encode("utf-8")).hexdigest()

    def get_product_info(self, url: str) -> dict | None:
        product_id = self.extract_product_id(url)
        if not product_id:
            return None

        api_path = "/openapi/param2/2/portals.open/api.getPromotionProductDetail/"
        params = {
            "app_key": self.api_key,
            "productId": product_id,
            "timestamp": str(int(time.time() * 1000)),
        }
        params["sign"] = self._generate_signature(api_path, params)

        try:
            response = requests.get(self.BASE_URL, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()

            if "result" not in data:
                return None

            result = data["result"]
            return {
                "title": result.get("productTitle"),
                "image_url": result.get("productMainImageUrl"),
                "price": result.get("salePrice"),
                "old_price": result.get("originalPrice"),
                "description": result.get("productDescription") or "Producto en AliExpress",
            }

        except Exception as e:
            logger.exception("Error al obtener info del producto AliExpress: %s", e)
            return None

    def extract_product_id(self, url: str) -> str | None:
        match = re.search(r"/item/(\d+)\.html", url)
        return match.group(1) if match else None

    def create_affiliate_link(self, url: str) -> str:
        return f"{url}?aff_id={self.config.aliexpress_aff_id}"

    def can_handle(self, url: str) -> bool:
        return "aliexpress.com" in url

# ================
# Amazon API Handler
# ================

class AmazonAPIHandler:
    def __init__(self, config):
        self.config = config
        self.api = AmazonAPI(
            access_key=config.amazon_access_key,
            secret_key=config.amazon_secret_key,
            partner_tag=config.amazon_affiliate_tag,
            country=config.amazon_country
        )

    def can_handle(self, url: str) -> bool:
        return "amazon." in url

    def get_product_info(self, url: str) -> dict | None:
        try:
            asin = self.extract_asin(url)
            if not asin:
                return None

            result = self.api.get_items([asin])
            item = result.items_result.items[0]

            return {
                "title": item.item_info.title.display_value,
                "image_url": item.images.primary.large.url,
                "price": item.offers.listings[0].price.display_amount,
                "old_price": item.offers.listings[0].price.savings_basis if item.offers.listings[0].price.savings_basis else None,
                "description": item.item_info.features.display_values[0] if item.item_info.features else "Producto de Amazon",
            }

        except Exception as e:
            logger.exception("Error al obtener info de Amazon: %s", e)
            return None

    def extract_asin(self, url: str) -> str | None:
        match = re.search(r"/([A-Z0-9]{10})(?:[/?]|$)", url)
        return match.group(1) if match else None

    def create_affiliate_link(self, url: str) -> str:
        asin = self.extract_asin(url)
        if asin:
            return f"https://www.amazon.{self.config.amazon_country}/dp/{asin}?tag={self.config.amazon_affiliate_tag}"
        return url

# ===============
# Utilidades
# ===============

def is_user_excluded(user) -> bool:
    user_id = user.id
    username = user.username
    excluded_users = config_manager.excluded_users
    return user_id in excluded_users or (username and username in excluded_users)

def shorten_url(url: str) -> str:
    try:
        response = requests.get(f"https://tinyurl.com/api-create.php?url={url}")
        return response.text if response.status_code == 200 else url
    except Exception:
        return url

def prepare_message(message: Message) -> str:
    return message.text if message and message.text else ""

# ====================
# Procesamiento Mensaje
# ====================

async def process_link_handlers(message: Message) -> None:
    text = prepare_message(message)
    urls = re.findall(r"https?://[\w./?=&%-]+", text)

    handlers = [AliexpressAPIHandler(config_manager), AmazonAPIHandler(config_manager)]

    for url in urls:
        for handler in handlers:
            if handler.can_handle(url):
                product = handler.get_product_info(url)
                if product:
                    affiliate_link = handler.create_affiliate_link(url)
                    short_link = shorten_url(affiliate_link)

                    caption = f"<b>{product['title']}</b>\n"
                    caption += f"💸 Precio: {product['price']}\n"
                    if product.get("old_price"):
                        caption += f"💰 Antes: {product['old_price']}\n"
                    if product.get("description"):
                        caption += f"📝 {product['description']}\n"
                    caption += f"🔗 {short_link}"

                    if product.get("image_url"):
                        await message.reply_photo(photo=product['image_url'], caption=caption, parse_mode="HTML")
                    else:
                        await message.reply_text(caption, parse_mode="HTML")
                    break
                else:
                    await message.reply_text(f"🔗 {url}")

# =========================
# Comando /descuentos
# =========================

async def handle_discount_command(update: Update, context: CallbackContext) -> None:
    await update.message.reply_text("🧾 No hay descuentos activos todavía")

# =========================
# Modificar Enlaces (Handler)
# =========================

async def modify_link(update: Update, _: CallbackContext) -> None:
    if not update.message or not update.message.text:
        return
    if not update.effective_user:
        return
    if is_user_excluded(update.effective_user):
        return

    await process_link_handlers(update.message)

# =========================
# Carga periódica de config
# =========================

def reload_config_periodically(interval: int) -> None:
    config_manager.load_configuration()
    threading.Timer(interval, reload_config_periodically, [interval]).start()

# =========================
# Registro de comandos
# =========================

def register_discount_handlers(application: Application) -> None:
    for keyword in config_manager.discount_keywords:
        application.add_handler(CommandHandler(keyword, handle_discount_command))

# =========================
# MAIN
# =========================

def main() -> None:
    config_manager.load_configuration()

    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=config_manager.log_level,
    )
    logger.info("Bot iniciado")

    reload_thread = threading.Thread(
        target=reload_config_periodically, args=(24 * 60 * 60,), daemon=True
    )
    reload_thread.start()

    defaults = Defaults(parse_mode="HTML")
    application = (
        Application.builder().token(config_manager.bot_token).defaults(defaults).build()
    )

    register_discount_handlers(application)
    application.add_handler(
        MessageHandler(filters.ALL & filters.ChatType.GROUPS, modify_link)
    )

    logger.info("Ejecutando el bot...")
    application.run_polling()

if __name__ == "__main__":
    main()
