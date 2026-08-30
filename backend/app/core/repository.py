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
            pk_reales = [c.name for c in self._orm_model.__mapper__.primary_key]
            if list(self._pk_field) != pk_reales:
                # pk_field (identidad de negocio) no coincide con la PK real
                # mapeada en SQLAlchemy (ej. Job: pk_field="job_id", PK real
                # autoincrement "id") -- session.merge() identifica filas por
                # la PK real, no por pk_field, así que sobre una fila nueva
                # con "id"=None siempre haría INSERT, nunca encontraría la
                # fila existente por job_id/name. Buscarla primero por
                # pk_field y copiar su PK real antes de mergear, para que
                # merge() sí la reconozca como update. No hace falta para
                # entidades cuya PK real YA es pk_field (VLAN, Puerto -- PK
                # compuesta sin id separado, ver FASE_1.md/FASE_2.md).
                valores_pk = tuple(getattr(entidad, campo) for campo in self._pk_field)
                existente = (
                    session.query(self._orm_model)
                    .filter_by(**self._pk_filtro(valores_pk))
                    .first()
                )
                if existente is not None:
                    for campo in pk_reales:
                        setattr(row, campo, getattr(existente, campo))
            merged = session.merge(row)
            session.flush()
            return self._to_domain(merged)

    def remove(self, pk: Any) -> None:
        with get_session() as session:
            session.query(self._orm_model).filter_by(**self._pk_filtro(pk)).delete()

    def existe(self, **criterio: Any) -> bool:
        """Chequeo de unicidad genérico -- agregado en Fase 6 (A4) para
        DeviceGroup (name+site_id, D6), mencionado como pendiente desde
        FINAL_ARCHITECTURE.md §2.2.1, nunca se había necesitado hasta esa
        fase."""
        with get_session() as session:
            return session.query(self._orm_model).filter_by(**criterio).first() is not None
