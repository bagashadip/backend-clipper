import os
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.requests import Request

# MATIKAN SIFAT REWEL AUTHLIB
os.environ["AUTHLIB_INSECURE_TRANSPORT"] = "1"

from app.config import get_settings
from app.database import get_db
from app.models import User
from app.services.auth import create_access_token, get_current_user, oauth

settings = get_settings()
router = APIRouter(prefix="/auth", tags=["auth"])

@router.get("/login")
async def login(request: Request):
    base_url = settings.backend_public_url.rstrip('/')
    redirect_uri = f"{base_url}/auth/callback"
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/callback", name="auth_callback")
async def auth_callback(request: Request, db: Session = Depends(get_db)):
    try:
        base_url = settings.backend_public_url.rstrip('/')
        redirect_uri = f"{base_url}/auth/callback"
        
        # INI PERUBAHAN KRUSIALNYA
        # Kita ambil token tanpa ngecek state session
        token = await oauth.google.fetch_access_token(
            redirect_uri=redirect_uri,
            request=request
        )
    except Exception as e:
        import traceback
        print("🔥" * 20)
        print(f"ERROR TYPE: {type(e)}")
        print(f"ERROR DETAILS: {repr(e)}")
        traceback.print_exc()
        print("🔥" * 20)
        raise HTTPException(status_code=401, detail=f"OAuth authentication failed: {repr(e)}")

    userinfo = token.get("userinfo")
    if not userinfo or not userinfo.get("email"):
        # Coba ambil userinfo manual kalau di token nggak ada
        try:
            resp = await oauth.google.get('https://www.googleapis.com/oauth2/v3/userinfo', token=token)
            userinfo = resp.json()
        except Exception as e:
             raise HTTPException(status_code=401, detail="Could not retrieve email from Google API")

    email = userinfo.get("email")
    if not email:
        raise HTTPException(status_code=401, detail="Email is missing from userinfo")

    user = db.query(User).filter(User.email == email).first()
    if not user:
        user = User(email=email, subscription_tier="free", quota_seconds_remaining=1800)
        db.add(user)
        db.commit()
        db.refresh(user)

    access_token = create_access_token(str(user.id))
    frontend_url = settings.frontend_url.rstrip('/')
    return RedirectResponse(url=f"{frontend_url}?token={access_token}")

@router.get("/me")
def get_me(current_user: User = Depends(get_current_user)):
    return {
        "id": str(current_user.id),
        "email": current_user.email,
        "subscription_tier": current_user.subscription_tier,
        "quota_seconds_remaining": current_user.quota_seconds_remaining,
    }