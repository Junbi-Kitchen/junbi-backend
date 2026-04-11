from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    FIREBASE_PROJECT_ID: str
    FIREBASE_SERVICE_ACCOUNT_KEY: str | None = None
    DATABASE_URL: str

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
