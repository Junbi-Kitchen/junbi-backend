from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, EmailStr

from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    rotate_refresh_token,
    invalidate_refresh_token,
    is_valid_refresh_token,
)
from app.services.user_service import UserService
from app.services import data_store

from jose import JWTError

router = APIRouter(prefix="/auth", tags=["auth"])
_user_service = UserService()


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RegisterRequest(BaseModel):
    name: str
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


def _auth_response(user: dict, user_id: str) -> dict:
    access_token = create_access_token({"sub": user_id})
    refresh_token = create_refresh_token({"sub": user_id})
    return {"user": user, "access_token": access_token, "refresh_token": refresh_token}


@router.post("/register")
def register(body: RegisterRequest) -> dict:
    if len(body.name.strip()) < 2:
        raise HTTPException(status_code=400, detail="Name must be at least 2 characters")
    if len(body.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    if _user_service.email_exists(body.email):
        raise HTTPException(status_code=409, detail="Email already registered")

    user = _user_service.create_user(body.name.strip(), body.email, body.password)
    data_store.seed_user_data(user["id"])
    return _auth_response(user, user["id"])


@router.post("/login")
def login(body: LoginRequest) -> dict:
    internal = _user_service.get_user_with_hash(body.email)
    # Use same error for "not found" and "wrong password" to avoid leaking which
    invalid = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid email or password",
    )
    if not internal:
        raise invalid
    if not _user_service.verify_password(body.password, internal["password_hash"]):
        raise invalid

    user = {k: v for k, v in internal.items() if k != "password_hash"}
    return _auth_response(user, user["id"])


@router.post("/refresh")
def refresh_tokens(body: RefreshRequest) -> dict:
    invalid = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired refresh token",
    )
    if not is_valid_refresh_token(body.refresh_token):
        raise invalid
    try:
        payload = decode_token(body.refresh_token)
    except JWTError:
        invalidate_refresh_token(body.refresh_token)
        raise invalid

    if payload.get("type") != "refresh":
        raise invalid

    user_id: str = payload.get("sub")
    if not user_id:
        raise invalid

    new_refresh = rotate_refresh_token(body.refresh_token, user_id)
    new_access = create_access_token({"sub": user_id})
    return {"access_token": new_access, "refresh_token": new_refresh}


@router.post("/logout", status_code=200)
def logout(body: LogoutRequest) -> dict:
    invalidate_refresh_token(body.refresh_token)
    return {"detail": "Logged out"}
