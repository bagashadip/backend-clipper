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
    redirect_uri = str(request.url_for("auth_callback"))
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/callback", name="auth_callback")
async def auth_callback(request: Request, db: Session = Depends(get_db)):
    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception:
        raise HTTPException(status_code=401, detail="OAuth authentication failed")

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
