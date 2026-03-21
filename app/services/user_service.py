import uuid
import copy
from datetime import datetime, timezone
from typing import Optional

import bcrypt

from app.data.mock_data import DEMO_USER_BASE, DEMO_USER_EMAIL, DEMO_USER_ID, DEMO_USER_PASSWORD


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# In-memory user store: user_id -> full user dict (including password_hash)
_users: dict[str, dict] = {}

# Email -> user_id index
_email_index: dict[str, str] = {}


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def _seed_demo_user() -> None:
    user = copy.deepcopy(DEMO_USER_BASE)
    user["password_hash"] = _hash_password(DEMO_USER_PASSWORD)
    _users[DEMO_USER_ID] = user
    _email_index[DEMO_USER_EMAIL] = DEMO_USER_ID


_seed_demo_user()


class UserService:
    def get_user_by_id(self, user_id: str) -> Optional[dict]:
        user = _users.get(user_id)
        if not user:
            return None
        return _public(user)

    def get_user_by_email(self, email: str) -> Optional[dict]:
        user_id = _email_index.get(email.lower())
        if not user_id:
            return None
        return _public(_users[user_id])

    def get_user_with_hash(self, email: str) -> Optional[dict]:
        """Returns the full internal record including password_hash."""
        user_id = _email_index.get(email.lower())
        if not user_id:
            return None
        return _users.get(user_id)

    def email_exists(self, email: str) -> bool:
        return email.lower() in _email_index

    def create_user(self, name: str, email: str, password: str) -> dict:
        user_id = str(uuid.uuid4())
        user = {
            "id": user_id,
            "name": name,
            "email": email.lower(),
            "avatar": None,
            "preferences": {
                "dietaryTags": [],
                "allergies": [],
                "cuisines": [],
                "householdSize": 1,
            },
            "connectedAccounts": {},
            "addresses": [],
            "defaultAddressId": None,
            "password_hash": _hash_password(password),
            "createdAt": _iso_now(),
        }
        _users[user_id] = user
        _email_index[email.lower()] = user_id
        return _public(user)

    def update_user(self, user_id: str, updates: dict) -> Optional[dict]:
        user = _users.get(user_id)
        if not user:
            return None
        # Only allow updating safe fields
        allowed = {"name", "avatar", "preferences", "connectedAccounts", "addresses", "defaultAddressId"}
        for key, val in updates.items():
            if key in allowed:
                user[key] = val
        return _public(user)

    def verify_password(self, plain: str, hashed: str) -> bool:
        return _verify_password(plain, hashed)


def _public(user: dict) -> dict:
    """Strip internal fields from user record."""
    return {k: v for k, v in user.items() if k != "password_hash"}
