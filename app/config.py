import os
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    app_name: str = "ProxyListHelper"
    secret_key: str = os.getenv("SECRET_KEY", "proxy-list-helper-secret-key-32chars-minimum-hash")
    admin_password: str = os.getenv("ADMIN_PASSWORD", "admin123")
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./proxy_helper.db")
    
    # Default pricing (in Euro cents)
    price_per_card_cents: int = int(os.getenv("PRICE_PER_CARD_CENTS", "13"))
    price_per_list_cents: int = int(os.getenv("PRICE_PER_LIST_CENTS", "26"))
    order_fee_cents: int = int(os.getenv("ORDER_FEE_CENTS", "0"))
    
    # Scryfall rate limiting & cache
    scryfall_cache_dir: str = os.getenv("SCRYFALL_CACHE_DIR", "./scryfall_cache")
    debug: bool = os.getenv("DEBUG", "false").lower() in ("true", "1", "yes")

    model_config = SettingsConfigDict(env_file=".env", extra="allow")

settings = Settings()
