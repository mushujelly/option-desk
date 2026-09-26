from pathlib import Path
import os
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(os.environ.get("OD_PROJECT_ROOT", Path.cwd())).resolve()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OD_", env_file=ROOT / ".env", extra="ignore"
    )
    database_url: str = (
        "postgresql+psycopg://optiondesk:optiondesk@127.0.0.1:55432/optiondesk"
    )
    mode: str = "offline"
    port: int = 8765
    host: str = "127.0.0.1"
    max_contracts: int = 100
    refresh_seconds: int = 60
    providers: str = ""
    schwab_app_key: str = ""
    schwab_app_secret: str = ""
    schwab_token_path: str = ".state/schwab-token.json"
    opend_host: str = "127.0.0.1"
    opend_port: int = 11111
    polygon_api_key: str = ""
    polygon_base_url: str = "https://api.massive.com"
    source_lookback_days: int = 30
    source_ingestion_enabled: bool = False
    discord_token: str = ""
    discord_channels: str = ""
    mongo_uri: str = ""
    mongo_db: str = "wfreedom"
    mongo_collection: str = "tweets"
    mongo_channels: str = ""
