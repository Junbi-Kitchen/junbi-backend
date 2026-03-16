# app/api/dependencies.py
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from app.services.user_service import UserService
from app.models.user import TokenData
from config import settings

# Initialize the HTTP Bearer security scheme for Swagger UI & header extraction
security = HTTPBearer()
user_service = UserService()

def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> TokenData:
    """Extracts and verifies the JWT from the request header."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    
    try:
        # Decode the token using the secret key
        payload = jwt.decode(
            credentials.credentials, 
            settings.JWT_SECRET_KEY, 
            algorithms=[settings.JWT_ALGORITHM]
        )
        
        user_id: str = payload.get("sub")
        if user_id is None:
            raise credentials_exception
            
        return TokenData(user_id=user_id)
        
    except JWTError:
        raise credentials_exception

def get_current_user(token_data: TokenData = Depends(verify_token)):
    """Fetches the actual user object from the database using the verified token."""
    user = user_service.get_user_by_id(token_data.user_id)
    
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found"
        )
        
    return user