from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from app.api.routes import router
from app.api.auth_routes import router as auth_router
from app.config import get_settings
from app.database import engine, Base

settings = get_settings()
Base.metadata.create_all(bind=engine)

app = FastAPI(title="AI Video Clipper", version="1.0.0")

# Session middleware required by Authlib for OAuth state parameter
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
app.include_router(auth_router)


@app.get("/health")
def health_check():
    return {"status": "ok"}
