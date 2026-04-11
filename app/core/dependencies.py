import logging

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import firebase_admin.auth as fb_auth

from app.db import get_db

logger = logging.getLogger(__name__)

security = HTTPBearer()


def _build_user(cur, uid: str) -> dict:
    """Assemble the full user dict from DB rows."""
    cur.execute("""
        SELECT up.id, up.email, up.display_name, up.avatar_url, up.created_at,
               upref.dietary_tags, upref.allergies, upref.cuisines, upref.household_size
        FROM user_profiles up
        LEFT JOIN user_preferences upref ON upref.user_id = up.id
        WHERE up.id = %s
    """, (uid,))
    row = cur.fetchone()
    if row is None:
        return None

    cur.execute("""
        SELECT id, label, street, city, state, zip, is_default
        FROM user_addresses WHERE user_id = %s ORDER BY created_at
    """, (uid,))
    addresses = [
        {
            "id": str(a["id"]),
            "label": a["label"],
            "street": a["street"],
            "city": a["city"],
            "state": a["state"],
            "zip": a["zip"],
            "isDefault": a["is_default"],
        }
        for a in cur.fetchall()
    ]

    cur.execute("""
        SELECT provider, handle FROM connected_accounts WHERE user_id = %s
    """, (uid,))
    connected = {r["provider"]: {"handle": r["handle"]} for r in cur.fetchall()}

    default_addr = next((a["id"] for a in addresses if a["isDefault"]), None)

    return {
        "id": row["id"],
        "name": row["display_name"] or "",
        "email": row["email"],
        "avatar": row["avatar_url"],
        "preferences": {
            "dietaryTags": list(row["dietary_tags"] or []),
            "allergies": list(row["allergies"] or []),
            "cuisines": list(row["cuisines"] or []),
            "householdSize": row["household_size"] or 1,
        },
        "connectedAccounts": connected,
        "addresses": addresses,
        "defaultAddressId": default_addr,
        "createdAt": row["created_at"].isoformat().replace("+00:00", "Z"),
    }


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db=Depends(get_db),
) -> dict:
    try:
        decoded = fb_auth.verify_id_token(credentials.credentials)
    except Exception as e:
        logger.warning("verify_id_token failed: %s: %s", type(e).__name__, e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    uid: str = decoded["uid"]
    cur = db.cursor()

    user = _build_user(cur, uid)
    if user is None:
        # Auto-provision on first authenticated request
        email: str = decoded.get("email", "")
        name: str = decoded.get("name") or email.split("@")[0]
        cur.execute("""
            INSERT INTO user_profiles (id, email, display_name)
            VALUES (%s, %s, %s)
            ON CONFLICT (id) DO NOTHING
        """, (uid, email, name))
        cur.execute("""
            INSERT INTO user_preferences (user_id)
            VALUES (%s)
            ON CONFLICT DO NOTHING
        """, (uid,))
        user = _build_user(cur, uid)

    return user
