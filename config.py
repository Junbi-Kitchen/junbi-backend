from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    FIREBASE_PROJECT_ID: str
    FIREBASE_SERVICE_ACCOUNT_KEY: str | None = None
    DATABASE_URL: str
    ANTHROPIC_API_KEY: str | None = None
    YOUTUBE_API_KEY: str | None = None
    GCV_API_KEY: str | None = None
    INSTAGRAM_ACCESS_TOKEN: str | None = None  # format: app_id|app_secret
    INSTACART_API_KEY: str | None = None
    INSTACART_SERVICE_URL: str = "http://localhost:3001"
    INSTACART_SERVICE_KEY: str | None = None

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
