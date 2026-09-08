import os
from datetime import timedelta

from celery import Celery
from celery.signals import worker_process_init
from app.core.config import settings

celery_app = Celery(
    "ansiauth",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
)

# Cadencia del barrido periódico que dispara ``sync_stale_devices_task``.
# Se lee acá y no adentro de ``tasks.py`` para evitar el ciclo de imports
# (Beat necesita el schedule al construir ``celery_app``, antes de que
# ``tasks`` termine de importarse). Ajustable via ``SYNC_STALE_INTERVAL_MIN``.
_SYNC_STALE_INTERVAL_MIN = int(os.getenv("SYNC_STALE_INTERVAL_MIN", "20"))

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    task_track_started=True,
    task_always_eager=os.getenv("CELERY_TASK_ALWAYS_EAGER", "false").lower() in ("true", "1"),
    include=["app.tasks"],
    # Celery Beat: periodic scheduler. Corre en un proceso ``celery beat``
    # separado del worker (ver docker-compose.yml). Reemplaza el patrón
    # anterior de "el usuario clickea refresh global y encola N×3 tareas
    # simultáneas" -- el scheduler barre el inventory con staggering y
    # coalescing, con carga sostenida en vez de picos.
    beat_schedule={
        "sync-stale-devices": {
            "task": "ansiauth.device.sync_stale",
            "schedule": timedelta(minutes=_SYNC_STALE_INTERVAL_MIN),
        },
    },
)


@worker_process_init.connect
def _init_db_for_worker(**kwargs):
    from app.core.rls_context import install_worker_system_context
    from app.db.session import init_db
    init_db(settings.DATABASE_URL)
    # MSP: Phase 6 — Celery tasks have no JWT; pin the worker process to
    # system context so its DB access satisfies the RLS deny-default.
    install_worker_system_context()
