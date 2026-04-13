from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    FIREBASE_PROJECT_ID: str
    FIREBASE_SERVICE_ACCOUNT_KEY: str | None = None
    DATABASE_URL: str
    ANTHROPIC_API_KEY: str | None = None
    KROGER_CLIENT_ID: str | None = None
    KROGER_CLIENT_SECRET: str | None = None

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
