from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 30
    GOOGLE_CLIENT_ID: str = ""

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
