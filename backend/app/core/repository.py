from __future__ import annotations

from typing import Any, Callable, Generic, Optional, TypeVar

from app.db.session import get_session

T = TypeVar("T")


class Repository(Generic[T]):
    """Puerto genérico de persistencia — get/add/list/remove sobre cualquier
    entidad, configurado por instancia (no por subclase). Ver FINAL_ARCHITECTURE.md
    §2.2 y §2.2.1 para el criterio de cuándo SÍ hace falta subclase.

    ``add()`` es upsert (``session.merge()`` por ``pk_field``), no insert puro —
    ver FINAL_ARCHITECTURE.md §2.2 "add() es upsert, no solo insert" para la
    justificación completa (varias entidades reales dependen de esto: Site.renombrar(),
    Job.marcar_completado(), VLAN.aplicar() idempotente). EXCEPCIÓN conocida:
    AuditRecord NO debe pasar por acá — AuditRepository.append() usa
    session.add() directo, ver Fase 3. Cualquier entidad nueva append-only/
    inmutable debe seguir el mismo criterio.
    """

    def __init__(
        self,
        orm_model: type,
        to_domain: Callable[[Any], T],
        to_orm: Callable[[T], Any],
        pk_field: "str | tuple[str, ...]" = "id",
    ):
        self._orm_model = orm_model
        self._to_domain = to_domain
        self._to_orm = to_orm
        self._pk_field: tuple[str, ...] = (
            pk_field if isinstance(pk_field, tuple) else (pk_field,)
        )

    def _pk_filtro(self, pk: Any) -> dict:
        """*pk* es un valor solo para PK simple, o una tupla en el mismo orden
        que ``pk_field`` para PK compuesta (ej. VLAN: ``(vlan_id, device)``)."""
        valores = pk if isinstance(pk, tuple) else (pk,)
        if len(valores) != len(self._pk_field):
            raise ValueError(
                f"pk esperaba {len(self._pk_field)} valor(es) {self._pk_field}, "
                f"recibió {len(valores)}"
            )
        return dict(zip(self._pk_field, valores))

    def get(self, pk: Any) -> Optional[T]:
        with get_session() as session:
            row = (
                session.query(self._orm_model)
                .filter_by(**self._pk_filtro(pk))
                .first()
            )
            return self._to_domain(row) if row is not None else None

    def list(self, **filtros: Any) -> list[T]:
        with get_session() as session:
            q = session.query(self._orm_model)
            if filtros:
                q = q.filter_by(**filtros)
            return [self._to_domain(row) for row in q.all()]

    def add(self, entidad: T) -> T:
        with get_session() as session:
            row = self._to_orm(entidad)
            merged = session.merge(row)
            session.flush()
            return self._to_domain(merged)

    def remove(self, pk: Any) -> None:
        with get_session() as session:
            session.query(self._orm_model).filter_by(**self._pk_filtro(pk)).delete()
