from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from datetime import timedelta

from app.services.user_service import UserService
from app.services.sso_verifier import SSOVerifier
from app.core.security import create_access_token, get_current_user
from app.models.user import UserCreate, UserResponse
from config import settings

router = APIRouter(prefix="/api/auth", tags=["auth"])
user_service = UserService()

# Pydantic schema for what the phone sends us
class MobileSSORequest(BaseModel):
    id_token: str
    provider: str = "google" # Can be "google", "apple", "firebase"

@router.post("/mobile-login", response_model=dict)
async def mobile_sso_login(auth_data: MobileSSORequest):
    # Verify the token based on the provider
    if auth_data.provider == "google":
        user_info = SSOVerifier.verify_google_token(auth_data.id_token)
        email = user_info.get("email")
        name = user_info.get("name", "").replace(" ", "_")
        picture = user_info.get("picture")
    else:
        raise HTTPException(status_code=400, detail="Unsupported provider")

    if not email:
        raise HTTPException(status_code=400, detail="Email not provided by SSO")

    # Find or create the user in your database
    user = user_service.get_user_by_email(email)
    
    if not user:
        # Create a new user seamlessly (no password needed!)
        user_data = UserCreate(
            email=email,
            password="", 
            username=name or None,
            profile_picture_url=picture,
            auth_provider=auth_data.provider
        )
        user = user_service.create_user(user_data)
        
        # Trigger any new-user logic here (like your PromptInitializer)
        print(f"New mobile user created: {user.id}")

    # Generate backend's JWT for session management
    access_token = create_access_token(
        data={"sub": str(user.id)}, 
        expires_delta=timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES)
    )

    # Return standard JSON to the React Native app
    return {
        "user": user,
        "access_token": access_token,
        "token_type": "bearer"
    }

# The phone uses this to verify its token is still valid on app startup
@router.get("/me", response_model=UserResponse)
async def get_current_user_info(current_user = Depends(get_current_user)):
    return current_user