from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # API Keys
    anthropic_api_key: str = ""
    xai_api_key: str = ""
    alpha_vantage_api_key: str = ""
    eia_api_key: str = ""
    fred_api_key: str = ""
    quandl_api_key: str = ""
    finnhub_api_key: str = ""
    twelve_api_key: str = ""
    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_symbol: str = "CLUSDT"  # TRADIFI perpetual tracking NYMEX WTI front-month
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    # Comma-separated list of additional chat_ids that RECEIVE notifications
    # but cannot interact with the bot (no chat, no commands). Used for
    # read-only observers. Only telegram_chat_id can send messages.
    telegram_notify_chat_ids: str = ""

    # Infrastructure
    postgres_url: str = "postgresql://trading:trading@localhost:5432/trading"
    redis_url: str = "redis://localhost:6379"

    # Dashboard API key — when set, every mutating endpoint and /api/chat
    # requires the X-API-Key header to match. Leave empty for no-auth local
    # dev; MUST be set if you ever expose the dashboard outside localhost.
    dashboard_api_key: str = ""

    # Optional - Shipping
    datalastic_api_key: str = ""
    vessel_finder_api_key: str = ""
    signal_ocean_api_key: str = ""


settings = Settings()
