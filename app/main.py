from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
from app.api.routes import router
from app.api.auth_routes import router as auth_router
from app.api.endpoints.webhooks import router as webhooks_router
from app.api.endpoints.payment import router as payment_router
from app.config import get_settings
from app.database import engine, Base

settings = get_settings()
Base.metadata.create_all(bind=engine)

app = FastAPI(title="AI Video Clipper", version="1.0.0")

# 1. BIKIN VARIABELNYA DULU DI SINI
_allowed_origins = ["http://localhost:3000", "http://localhost:3001"]
if settings.frontend_url and settings.frontend_url not in _allowed_origins:
    _allowed_origins.append(settings.frontend_url)
if settings.backend_public_url and settings.backend_public_url not in _allowed_origins:
    _allowed_origins.append(settings.backend_public_url)

# 2. CORS (Ditulis Pertama, Dieksekusi Terakhir)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins, # Sekarang variabelnya udah ada
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 3. SESSION (Ditulis Kedua, Dieksekusi di Tengah)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    same_site="none",
    https_only=True
)

# 4. PROXY HEADERS (Ditulis Terakhir, Dieksekusi Pertama saat request masuk)
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

app.include_router(router)
app.include_router(auth_router)
app.include_router(webhooks_router, prefix="/api")
app.include_router(payment_router, prefix="/api/payment")

@app.get("/health")
def health_check():
    return {"status": "ok"}