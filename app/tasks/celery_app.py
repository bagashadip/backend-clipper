from celery import Celery
from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "clipper",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    broker_connection_retry_on_startup=True,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    worker_concurrency=2,
    worker_max_memory_per_child=512000,
    task_soft_time_limit=3600,
    task_time_limit=4200,
)

celery_app.conf.update(
    include=["app.tasks.process_video"],
)
