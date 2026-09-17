import os
from dataclasses import dataclass, field
import re
from dotenv import load_dotenv


@dataclass
class Settings:
    bot_token: str
    database_url: str
    admin_ids: tuple
    shop_name: str = "Магазин техники"
    catalog_url: str = ""
    sync_api_key: str = field(default="", repr=False)
    port: int = 8080
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
            database_url=(os.getenv("DATABASE_URL", "").strip() or os.getenv("RETAIL_DATABASE_URL", "").strip()),
            admin_ids=admins,
            shop_name=os.getenv("SHOP_NAME", "Магазин техники").strip(),
            catalog_url=os.getenv("CATALOG_URL", "").strip(),
            sync_api_key=os.getenv("SYNC_API_KEY", "").strip(),
            port=int(os.getenv("PORT", "8080")),
            archive_days=int(os.getenv("ARCHIVE_DAYS", "7")),
            draft_hours=int(os.getenv("DRAFT_HOURS", "24")),
            fresh_seconds=int(os.getenv("CATALOG_FRESH_SECONDS", "1800")),
        )
        if not value.bot_token or not value.database_url:
            raise ValueError("Заполни BOT_TOKEN и DATABASE_URL (база этого проекта)")
        if not 1 <= value.archive_days <= 365 or not 1 <= value.draft_hours <= 24 or value.fresh_seconds < 30:
            raise ValueError("Недопустимый срок архива, черновика или свежести прайса")
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", value.sync_api_key):
            raise ValueError("SYNC_API_KEY: одинаковый секрет из 32–128 латинских букв, цифр, _ или - в обоих проектах")
        if not 1 <= value.port <= 65535:
            raise ValueError("PORT: нужен номер порта от 1 до 65535")
        for url in (value.catalog_url,):
            if url and not url.startswith("https://"):
                raise ValueError("CATALOG_URL должен начинаться с https://")
        return value

