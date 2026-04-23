from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.config import get_settings
from app.database import get_db
from app.models import User
from app.services.auth import create_access_token, get_current_user, oauth

settings = get_settings()
router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/login")
async def login(request: Request):
    # Build the callback URL from BACKEND_PUBLIC_URL so it is always the
    # correct public-facing HTTPS URL regardless of how the request arrives
    # (e.g. behind a reverse proxy / load balancer).
    redirect_uri = f"{settings.backend_public_url}/auth/callback"
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/callback", name="auth_callback")
async def auth_callback(request: Request, db: Session = Depends(get_db)):
    try:
        # Pass redirect_uri explicitly so Authlib doesn't reconstruct it from
        # request.url (which may be HTTP behind Cloudflare/proxy).
        redirect_uri = f"{settings.backend_public_url}/auth/callback"
        token = await oauth.google.authorize_access_token(request, redirect_uri=redirect_uri)
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"OAuth authentication failed: {e}")

    userinfo = token.get("userinfo")
    if not userinfo or not userinfo.get("email"):
        raise HTTPException(status_code=401, detail="Could not retrieve email from Google")

    email = userinfo["email"]

    # Find or create user
    user = db.query(User).filter(User.email == email).first()
    if not user:
        user = User(email=email, subscription_tier="free", quota_seconds_remaining=1800)
        db.add(user)
        db.commit()
        db.refresh(user)

    # Issue JWT and redirect to frontend with token
    access_token = create_access_token(str(user.id))
    frontend_url = settings.frontend_url
    return RedirectResponse(url=f"{frontend_url}?token={access_token}")


@router.get("/me")
def get_me(current_user: User = Depends(get_current_user)):
    return {
        "id": str(current_user.id),
        "email": current_user.email,
        "subscription_tier": current_user.subscription_tier,
        "quota_seconds_remaining": current_user.quota_seconds_remaining,
    }
