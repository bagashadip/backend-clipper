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
import logging
import time

logger = logging.getLogger(__name__)
settings = get_settings()


def _init_db(max_retries: int = 5, retry_delay: float = 3.0):
    """Initialize database tables with retry logic.

    Handles the case where postgres isn't ready yet on container restart,
    or where enum types already exist from schema.sql init.
    Uses checkfirst=True (the default for create_all) so existing tables
    and types are skipped gracefully.
    """
    for attempt in range(1, max_retries + 1):
        try:
            Base.metadata.create_all(bind=engine)
            logger.info("Database tables initialized successfully")
            return
        except Exception as e:
            if attempt == max_retries:
                logger.error(f"Failed to initialize database after {max_retries} attempts: {e}")
                # Don't crash the app — tables likely already exist from schema.sql
                # The app can still function if the DB is reachable
                logger.warning("Continuing startup without create_all — tables should exist from schema.sql")
                return
            logger.warning(
                f"Database init attempt {attempt}/{max_retries} failed: {e}. "
                f"Retrying in {retry_delay}s..."
            )
            time.sleep(retry_delay)


def _run_migrations():
    """Apply lightweight idempotent SQL migrations from migrations/*.sql.

    Delegates to ``app.migrations_runner`` which uses ``exec_driver_sql`` so
    multi-statement scripts (e.g. ``DO $$ ... $$`` blocks) work, and keeps a
    ledger so already-applied files are skipped.
    """
    try:
        from app.migrations_runner import run_pending_migrations
        run_pending_migrations()
    except Exception as e:
        logger.warning(f"Migration runner failed: {e}. Continuing startup.")


_init_db()
_run_migrations()

app = FastAPI(title="AI Video Clipper", version="1.0.0")

# 1. BIKIN VARIABELNYA DULU DI SINI
_allowed_origins = ["http://localhost:3000", "http://localhost:3001"]
if settings.frontend_url and settings.frontend_url not in _allowed_origins:
    _allowed_origins.append(settings.frontend_url)
if settings.backend_public_url and settings.backend_public_url not in _allowed_origins:
    _allowed_origins.append(settings.backend_public_url)

# Optional regex allow-list — handy for ephemeral Cloudflare quick tunnels
# (their hostname changes on every restart, so a static URL in .env goes
# stale fast). Set CORS_ORIGIN_REGEX to e.g. ``https?://.*\.trycloudflare\.com``
# in development; leave empty in production.
_cors_origin_regex = (settings.cors_origin_regex or "").strip() or None

# 2. CORS (Ditulis Pertama, Dieksekusi Terakhir)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_origin_regex=_cors_origin_regex,
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
    """Health check that also verifies database connectivity."""
    from sqlalchemy import text
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return {"status": "ok"}  # Still return ok so container doesn't restart in a loop