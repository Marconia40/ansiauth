import os
from celery import Celery
from celery.signals import worker_process_init
from app.core.config import settings

celery_app = Celery(
    "ansiauth",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    task_track_started=True,
    task_always_eager=os.getenv("CELERY_TASK_ALWAYS_EAGER", "false").lower() in ("true", "1"),
    include=[
        "app.services.vlan_execution_service",
        "app.services.port_execution_service",
        "app.services.port_config_service",
    ],
)


@worker_process_init.connect
def _init_db_for_worker(**kwargs):
    from app.db.session import init_db
    init_db(settings.DATABASE_URL)
