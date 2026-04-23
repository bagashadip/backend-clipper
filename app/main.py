from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
from app.api.routes import router
from app.api.auth_routes import router as auth_router
from app.config import get_settings
from app.database import engine, Base

settings = get_settings()
Base.metadata.create_all(bind=engine)

app = FastAPI(title="AI Video Clipper", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Session middleware required by Authlib for OAuth state parameter
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    same_site="lax",
    https_only=True
)

# Trust proxy headers from Cloudflare / load balancers so that
# request.url.scheme is 'https' and request.client.host is the real IP.
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

# Build allowed origins dynamically from config
_allowed_origins = ["http://localhost:3000", "http://localhost:3001"]
if settings.frontend_url and settings.frontend_url not in _allowed_origins:
    _allowed_origins.append(settings.frontend_url)
if settings.backend_public_url and settings.backend_public_url not in _allowed_origins:
    _allowed_origins.append(settings.backend_public_url)

app.include_router(router)
app.include_router(auth_router)

@app.get("/health")
def health_check():
    return {"status": "ok"}
