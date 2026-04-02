from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # API Keys
    anthropic_api_key: str = ""
    xai_api_key: str = ""
    alpha_vantage_api_key: str = ""
    eia_api_key: str = ""
    quandl_api_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Infrastructure
    postgres_url: str = "postgresql://trading:trading@localhost:5432/trading"
    redis_url: str = "redis://localhost:6379"

    # Optional - Shipping
    datalastic_api_key: str = ""
    vessel_finder_api_key: str = ""
    signal_ocean_api_key: str = ""


settings = Settings()
