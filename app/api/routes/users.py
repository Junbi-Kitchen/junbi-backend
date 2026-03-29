from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional

from app.core.dependencies import get_current_user, _build_user
from app.db import get_db

router = APIRouter(prefix="/users", tags=["users"])


class UpdateProfileRequest(BaseModel):
    name: Optional[str] = None
    avatar: Optional[str] = None


class UpdatePreferencesRequest(BaseModel):
    dietaryTags: Optional[list[str]] = None
    allergies: Optional[list[str]] = None
    cuisines: Optional[list[str]] = None
    householdSize: Optional[int] = None


class AddressBody(BaseModel):
    label: str
    street: str
    city: str
    state: str
    zip: str
    isDefault: bool = False


class ConnectedAccountBody(BaseModel):
    platform: str
    handle: str


@router.get("/me")
def get_me(current_user: dict = Depends(get_current_user)) -> dict:
    return current_user


@router.patch("/me")
def update_profile(
    body: UpdateProfileRequest,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    cur = db.cursor()
    if body.name is not None:
        cur.execute(
            "UPDATE user_profiles SET display_name = %s, updated_at = now() WHERE id = %s",
            (body.name, current_user["id"]),
        )
    if body.avatar is not None:
        cur.execute(
            "UPDATE user_profiles SET avatar_url = %s, updated_at = now() WHERE id = %s",
            (body.avatar, current_user["id"]),
        )
    return _build_user(cur, current_user["id"])


@router.patch("/me/preferences")
def update_preferences(
    body: UpdatePreferencesRequest,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    cur = db.cursor()
    existing = current_user["preferences"]
    dietary = body.dietaryTags if body.dietaryTags is not None else existing["dietaryTags"]
    allergies = body.allergies if body.allergies is not None else existing["allergies"]
    cuisines = body.cuisines if body.cuisines is not None else existing["cuisines"]
    size = body.householdSize if body.householdSize is not None else existing["householdSize"]
    cur.execute("""
        UPDATE user_preferences
        SET dietary_tags = %s, allergies = %s, cuisines = %s,
            household_size = %s, updated_at = now()
        WHERE user_id = %s
    """, (dietary, allergies, cuisines, size, current_user["id"]))
    return _build_user(cur, current_user["id"])


@router.post("/me/addresses", status_code=201)
def add_address(
    body: AddressBody,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    cur = db.cursor()
    uid = current_user["id"]
    # If first address or isDefault requested, clear existing defaults first
    if body.isDefault or not current_user["addresses"]:
        cur.execute("UPDATE user_addresses SET is_default = false WHERE user_id = %s", (uid,))
        is_default = True
    else:
        is_default = False
    cur.execute("""
        INSERT INTO user_addresses (user_id, label, street, city, state, zip, is_default)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (uid, body.label, body.street, body.city, body.state, body.zip, is_default))
    return _build_user(cur, uid)


@router.patch("/me/addresses/{address_id}")
def update_address(
    address_id: str,
    body: AddressBody,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    cur = db.cursor()
    uid = current_user["id"]
    cur.execute(
        "SELECT id FROM user_addresses WHERE id = %s AND user_id = %s",
        (address_id, uid),
    )
    if not cur.fetchone():
        raise HTTPException(status_code=404, detail="Address not found")
    if body.isDefault:
        cur.execute("UPDATE user_addresses SET is_default = false WHERE user_id = %s", (uid,))
    cur.execute("""
        UPDATE user_addresses
        SET label = %s, street = %s, city = %s, state = %s, zip = %s, is_default = %s
        WHERE id = %s AND user_id = %s
    """, (body.label, body.street, body.city, body.state, body.zip, body.isDefault, address_id, uid))
    return _build_user(cur, uid)


@router.delete("/me/addresses/{address_id}")
def delete_address(
    address_id: str,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    cur = db.cursor()
    uid = current_user["id"]
    cur.execute(
        "DELETE FROM user_addresses WHERE id = %s AND user_id = %s RETURNING is_default",
        (address_id, uid),
    )
    deleted = cur.fetchone()
    if not deleted:
        raise HTTPException(status_code=404, detail="Address not found")
    # If we deleted the default, promote the next address
    if deleted["is_default"]:
        cur.execute(
            "UPDATE user_addresses SET is_default = true WHERE user_id = %s ORDER BY created_at LIMIT 1",
            (uid,),
        )
    return _build_user(cur, uid)


@router.patch("/me/addresses/{address_id}/default")
def set_default_address(
    address_id: str,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    cur = db.cursor()
    uid = current_user["id"]
    cur.execute(
        "SELECT id FROM user_addresses WHERE id = %s AND user_id = %s",
        (address_id, uid),
    )
    if not cur.fetchone():
        raise HTTPException(status_code=404, detail="Address not found")
    cur.execute("UPDATE user_addresses SET is_default = false WHERE user_id = %s", (uid,))
    cur.execute(
        "UPDATE user_addresses SET is_default = true WHERE id = %s AND user_id = %s",
        (address_id, uid),
    )
    return _build_user(cur, uid)


@router.post("/me/connected-accounts")
def connect_account(
    body: ConnectedAccountBody,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    cur = db.cursor()
    cur.execute("""
        INSERT INTO connected_accounts (user_id, provider, handle)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id, provider) DO UPDATE SET handle = EXCLUDED.handle
    """, (current_user["id"], body.platform, body.handle))
    return _build_user(cur, current_user["id"])


@router.delete("/me/connected-accounts/{platform}")
def disconnect_account(
    platform: str,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    cur = db.cursor()
    cur.execute(
        "DELETE FROM connected_accounts WHERE user_id = %s AND provider = %s",
        (current_user["id"], platform),
    )
    return _build_user(cur, current_user["id"])
