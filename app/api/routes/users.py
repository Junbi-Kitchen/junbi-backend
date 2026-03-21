from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional

from app.core.dependencies import get_current_user
from app.services.user_service import UserService

router = APIRouter(prefix="/users", tags=["users"])
_user_service = UserService()


class UpdateProfileRequest(BaseModel):
    name: Optional[str] = None
    avatar: Optional[str] = None


class UpdatePreferencesRequest(BaseModel):
    dietaryTags: Optional[list[str]] = None
    allergies: Optional[list[str]] = None
    cuisines: Optional[list[str]] = None
    householdSize: Optional[int] = None


class AddressBody(BaseModel):
    id: Optional[str] = None
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
) -> dict:
    updates = body.model_dump(exclude_none=True)
    user = _user_service.update_user(current_user["id"], updates)
    return user


@router.patch("/me/preferences")
def update_preferences(
    body: UpdatePreferencesRequest,
    current_user: dict = Depends(get_current_user),
) -> dict:
    prefs = {**current_user.get("preferences", {}), **body.model_dump(exclude_none=True)}
    user = _user_service.update_user(current_user["id"], {"preferences": prefs})
    return user


@router.post("/me/addresses")
def add_address(
    body: AddressBody,
    current_user: dict = Depends(get_current_user),
) -> dict:
    import uuid
    addresses = list(current_user.get("addresses", []))
    new_addr = body.model_dump()
    new_addr["id"] = new_addr.get("id") or str(uuid.uuid4())

    # If first address or isDefault requested, make it default
    if body.isDefault or not addresses:
        for a in addresses:
            a["isDefault"] = False
        new_addr["isDefault"] = True
        default_id = new_addr["id"]
    else:
        default_id = current_user.get("defaultAddressId")

    addresses.append(new_addr)
    user = _user_service.update_user(
        current_user["id"],
        {"addresses": addresses, "defaultAddressId": default_id},
    )
    return user


@router.patch("/me/addresses/{address_id}")
def update_address(
    address_id: str,
    body: dict,
    current_user: dict = Depends(get_current_user),
) -> dict:
    addresses = list(current_user.get("addresses", []))
    for i, addr in enumerate(addresses):
        if addr["id"] == address_id:
            addresses[i] = {**addr, **{k: v for k, v in body.items() if k != "id"}}
            break
    else:
        raise HTTPException(status_code=404, detail="Address not found")
    user = _user_service.update_user(current_user["id"], {"addresses": addresses})
    return user


@router.delete("/me/addresses/{address_id}")
def delete_address(
    address_id: str,
    current_user: dict = Depends(get_current_user),
) -> dict:
    addresses = [a for a in current_user.get("addresses", []) if a["id"] != address_id]
    default_id = current_user.get("defaultAddressId")
    if default_id == address_id:
        default_id = addresses[0]["id"] if addresses else None
    user = _user_service.update_user(
        current_user["id"],
        {"addresses": addresses, "defaultAddressId": default_id},
    )
    return user


@router.patch("/me/addresses/{address_id}/default")
def set_default_address(
    address_id: str,
    current_user: dict = Depends(get_current_user),
) -> dict:
    addresses = list(current_user.get("addresses", []))
    found = False
    for addr in addresses:
        addr["isDefault"] = addr["id"] == address_id
        if addr["id"] == address_id:
            found = True
    if not found:
        raise HTTPException(status_code=404, detail="Address not found")
    user = _user_service.update_user(
        current_user["id"],
        {"addresses": addresses, "defaultAddressId": address_id},
    )
    return user


@router.post("/me/connected-accounts")
def connect_account(
    body: ConnectedAccountBody,
    current_user: dict = Depends(get_current_user),
) -> dict:
    accounts = {**current_user.get("connectedAccounts", {}), body.platform: body.handle}
    user = _user_service.update_user(current_user["id"], {"connectedAccounts": accounts})
    return user


@router.delete("/me/connected-accounts/{platform}")
def disconnect_account(
    platform: str,
    current_user: dict = Depends(get_current_user),
) -> dict:
    accounts = {k: v for k, v in current_user.get("connectedAccounts", {}).items() if k != platform}
    user = _user_service.update_user(current_user["id"], {"connectedAccounts": accounts})
    return user
