import os
from dataclasses import dataclass
from dotenv import load_dotenv


@dataclass
class Settings:
    bot_token: str
    database_url: str
    admin_ids: tuple
    shop_name: str = "Магазин техники"
    catalog_url: str = ""
    privacy_url: str = ""
    terms_url: str = ""
    seller_info: str = ""
    pickup_address: str = ""
    archive_days: int = 7
    draft_hours: int = 24
    fresh_seconds: int = 1800

    @classmethod
    def from_env(cls):
        load_dotenv()
        admins = tuple(int(x.strip()) for x in os.getenv("ADMIN_IDS", os.getenv("ADMIN_ID", "")).split(",") if x.strip())
        if not admins or any(i <= 0 for i in admins):
            raise ValueError("Укажи ADMIN_IDS: Telegram ID администраторов через запятую")
        value = cls(
            bot_token=os.getenv("BOT_TOKEN", "").strip(),
            database_url=os.getenv("RETAIL_DATABASE_URL", "").strip(),
            admin_ids=admins,
            shop_name=os.getenv("SHOP_NAME", "Магазин техники").strip(),
            catalog_url=os.getenv("CATALOG_URL", "").strip(),
            privacy_url=os.getenv("PRIVACY_URL", "").strip(),
            terms_url=os.getenv("TERMS_URL", "").strip(),
            seller_info=os.getenv("SELLER_INFO", "").strip(),
            pickup_address=os.getenv("PICKUP_ADDRESS", "").strip(),
            archive_days=int(os.getenv("ARCHIVE_DAYS", "7")),
            draft_hours=int(os.getenv("DRAFT_HOURS", "24")),
            fresh_seconds=int(os.getenv("CATALOG_FRESH_SECONDS", "1800")),
        )
        if not value.bot_token or not value.database_url:
            raise ValueError("Заполни BOT_TOKEN и RETAIL_DATABASE_URL")
        if not 1 <= value.archive_days <= 365 or not 1 <= value.draft_hours <= 24 or value.fresh_seconds < 30:
            raise ValueError("Недопустимый срок архива, черновика или свежести прайса")
        for url in (value.catalog_url, value.privacy_url, value.terms_url):
            if url and not url.startswith("https://"):
                raise ValueError("Ссылки CATALOG_URL, PRIVACY_URL и TERMS_URL должны начинаться с https://")
        return value

    def checkout_ready(self):
        return bool(self.privacy_url and self.terms_url and self.seller_info)
