from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from app.config import get_settings

settings = get_settings()

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,          # Test connections before using them (handles postgres restarts)
    pool_recycle=300,            # Recycle connections every 5 minutes (prevents stale connections)
    pool_size=5,                 # Base pool size
    max_overflow=10,             # Allow up to 10 extra connections under load
    connect_args={
        "connect_timeout": 10,   # Don't hang forever if postgres is unreachable
    },
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
