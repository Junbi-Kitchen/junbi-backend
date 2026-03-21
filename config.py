from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    FIREBASE_PROJECT_ID: str

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
