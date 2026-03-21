from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import JWTError, jwt

from config import settings

# In-memory store of valid refresh tokens (cleared on server restart — acceptable for dev)
_valid_refresh_tokens: set[str] = set()


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode["exp"] = expire
    return jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_refresh_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode["exp"] = expire
    to_encode["type"] = "refresh"
    token = jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    _valid_refresh_tokens.add(token)
    return token


def decode_token(token: str) -> dict:
    """Decode and validate a JWT. Raises JWTError on failure."""
    return jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])


def rotate_refresh_token(old_token: str, user_id: str) -> str:
    """Remove old refresh token and issue a new one (token rotation)."""
    _valid_refresh_tokens.discard(old_token)
    return create_refresh_token({"sub": user_id})


def invalidate_refresh_token(token: str) -> None:
    _valid_refresh_tokens.discard(token)


def is_valid_refresh_token(token: str) -> bool:
    return token in _valid_refresh_tokens
