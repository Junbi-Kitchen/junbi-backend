from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import firebase_admin.auth as fb_auth

from app.services.user_service import UserService
from app.services import data_store

security = HTTPBearer()
_user_service = UserService()


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    try:
        decoded = fb_auth.verify_id_token(credentials.credentials)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    firebase_uid: str = decoded["uid"]
    user = _user_service.get_user_by_id(firebase_uid)

    if user is None:
        # Lazy-create the backend user profile on first authenticated request
        email: str = decoded.get("email", "")
        name: str = decoded.get("name") or email.split("@")[0]
        user = _user_service.create_firebase_user(firebase_uid, email, name)
        data_store.seed_user_data(firebase_uid)

    return user
