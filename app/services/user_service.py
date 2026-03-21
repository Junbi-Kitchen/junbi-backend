from datetime import datetime, timezone
from typing import Optional


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# In-memory user store: firebase_uid -> user dict
_users: dict[str, dict] = {}

# Email -> firebase_uid index
_email_index: dict[str, str] = {}


class UserService:
    def get_user_by_id(self, user_id: str) -> Optional[dict]:
        return _users.get(user_id)

    def get_user_by_email(self, email: str) -> Optional[dict]:
        user_id = _email_index.get(email.lower())
        if not user_id:
            return None
        return _users.get(user_id)

    def email_exists(self, email: str) -> bool:
        return email.lower() in _email_index

    def create_firebase_user(self, firebase_uid: str, email: str, name: str) -> dict:
        user: dict = {
            "id": firebase_uid,
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
            "createdAt": _iso_now(),
        }
        _users[firebase_uid] = user
        _email_index[email.lower()] = firebase_uid
        return user

    def update_user(self, user_id: str, updates: dict) -> Optional[dict]:
        user = _users.get(user_id)
        if not user:
            return None
        allowed = {"name", "avatar", "preferences", "connectedAccounts", "addresses", "defaultAddressId"}
        for key, val in updates.items():
            if key in allowed:
                user[key] = val
        return user
