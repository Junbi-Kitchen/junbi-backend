from google.oauth2 import id_token
from google.auth.transport import requests
from fastapi import HTTPException, status
from config import settings

class SSOVerifier:
    @staticmethod
    def verify_google_token(token: str) -> dict:
        try:
            # We ONLY need the Client ID here to ensure the token was minted for the app
            idinfo = id_token.verify_oauth2_token(
                token, 
                requests.Request(), 
                settings.GOOGLE_CLIENT_ID
            )
            return idinfo
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, 
                detail="Invalid Google token"
            )