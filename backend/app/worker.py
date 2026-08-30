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
    include=["app.tasks"],
)


@worker_process_init.connect
def _init_db_for_worker(**kwargs):
    from app.core.rls_context import install_worker_system_context
    from app.db.session import init_db
    init_db(settings.DATABASE_URL)
    # MSP: Phase 6 — Celery tasks have no JWT; pin the worker process to
    # system context so its DB access satisfies the RLS deny-default.
    install_worker_system_context()
