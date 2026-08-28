# Fase 3 — `EventDispatcher`/`DomainEvent`/`AuditListener` (Línea A) · `AuditRepository`/`LoginAttemptRepository`/`DeviceRepository` (Línea B)

> Parte de [plan de migración a `FINAL_ARCHITECTURE.md`](README.md). Requiere
> [`FASE_1.md`](FASE_1.md) y [`FASE_2.md`](FASE_2.md) terminadas.

## Objetivo

**Línea A**: patrón Observer — `DomainEvent` (con el campo `actor` agregado en esta
misma revisión, ver nota abajo), `EventListener` (interfaz), `EventDispatcher`
(despacha a los listeners suscriptos), `AuditListener` (el único listener real hoy,
traduce un `DomainEvent` a un `AuditRecord`). **Nada de esto tiene caller real
todavía** — mismo patrón que `PluginRegistry` en Fase 1: se construye completo, se
cablea recién en Fase 5 cuando `Orquestador` exista.

**Línea B**: `AuditRepository` (subclase — `append()` con insert estricto, no
upsert; `query()` unificando los 5 casos reales de `_apply_msp_audit_scoping()`),
`LoginAttemptRepository` (subclase), `DeviceRepository` (subclase nueva, un solo
método — `nombres_visibles(scope)`, la pieza que le falta a `AuditRepository.query()`
para resolver "qué nombres de device puede ver este usuario").

## Estado previo esperado

- Fase 1 y Fase 2 completas — en particular, `RoleAssignmentRepository`/
  `VisibilityScope` (Fase 2, Línea B) ya existen, `DeviceRepository` (esta fase) los
  necesita.
- **Corrección importante hecha en `FINAL_ARCHITECTURE.md` al preparar esta fase,
  antes de escribir nada de código**: `DomainEvent` no tenía campo `actor` — los 4
  call sites reales de `Inventory` (`register()`/`move()`×2/`deregister()`) ya
  intentaban pasarlo, pero en el lugar del parámetro `device` (`DomainEvent("...",
  device, actor, {...})` — `actor` terminaba pisando el slot de `device`, y ningún
  device real se pasaba). Se corrigió la firma a `DomainEvent(tipo, recurso, device,
  actor, payload)` y los 7 call sites reales del documento. Si al llegar a esta fase
  `FINAL_ARCHITECTURE.md` todavía muestra la firma vieja (sin `actor`), es una
  regresión — avisar antes de seguir, no reintroducir el bug.

---

## Línea A

### A1 — `app/models/domain_event.py` (archivo nuevo): `DomainEvent`

```python
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DomainEvent:
    tipo: str
    recurso: Any
    device: Any
    actor: str
    payload: dict = field(default_factory=dict)
    exitoso: bool = True
```

`recurso`/`device` quedan tipados `Any` a propósito — `DomainEvent` es genérico, lo
mismo vale para una `VLAN`, un `Puerto` o un `Device` (los eventos de `Inventory`, ver
`FINAL_ARCHITECTURE.md` §1, `Inventory.register()`/`move()`/`deregister()`, donde
`recurso` y `device` terminan siendo el mismo objeto `Device`).

### A2 — `app/services/event_listener.py` (archivo nuevo): interfaz `EventListener`

```python
from abc import ABC, abstractmethod
from app.models.domain_event import DomainEvent


class EventListener(ABC):
    @abstractmethod
    def on_event(self, evento: DomainEvent) -> None: ...
```

### A3 — `app/services/event_dispatcher.py` (archivo nuevo): `EventDispatcher`

```python
from app.models.domain_event import DomainEvent
from app.services.event_listener import EventListener


class EventDispatcher:
    def __init__(self):
        self._listeners: list[EventListener] = []

    def suscribir(self, listener: EventListener) -> None:
        self._listeners.append(listener)

    def despachar(self, eventos: list[DomainEvent]) -> None:
        for evento in eventos:
            for listener in self._listeners:
                listener.on_event(evento)
```

**Decisión real, no en `FINAL_ARCHITECTURE.md`**: el documento cita
`suscribir(tipo_evento, listener)` (con un parámetro extra `tipo_evento`) pero
`DomainEvent` es 1 sola clase para todo — no hay "tipos de evento" como clases
distintas a las que suscribirse selectivamente. Con un solo listener real hoy
(`AuditListener`, que quiere **todos** los eventos), filtrar por tipo en
`EventDispatcher` sería una feature sin caller — se deja `suscribir(listener)` sin el
parámetro de tipo. Si en el futuro aparece un 2do listener que solo quiere un subset,
ahí se decide cómo filtrar (adentro del propio listener, ej. `if evento.tipo not in
{...}: return` al principio de `on_event()` — no antes, no en `EventDispatcher`).

### A4 — `app/services/audit_listener.py` (archivo nuevo): `AuditListener`

**Prerrequisito, tocar `app/models/audit.py` antes de escribir `AuditListener`**:
`AuditRecord` (real, Pydantic, `models/audit.py:7-19`) exige `id`/`timestamp` como
campos obligatorios sin default. `AuditRepository.append()` (B1, más abajo) los
**descarta** igual (el `id` real lo asigna la DB al insertar; usa el `timestamp`
del caller, pero podría no tenerlo) — así que cualquier caller de `append()`
(no solo `AuditListener` acá, también los ~15 call sites reales que hoy llaman
`audit_service.log_action()` directo — `role_assignment_service.py`, `main.py`,
`api/users.py`, `api/auth.py`, cerrados recién en `FASE_7.md`) tendría que
inventar un `id`/`timestamp` a mano solo para satisfacer Pydantic. Agregar
default a los 2, más a `status` (el real `log_action()` también lo default-ea a
`"success"`, `audit_service.py:42`):

```python
from pydantic import BaseModel, Field
import uuid
from datetime import datetime, timezone


class AuditRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    user: str
    action: str
    resource: str
    resource_id: Optional[str] = None
    details: dict
    status: str = "success"
    job_id: Optional[str] = None
    device: Optional[str] = None
    request_id: Optional[str] = None
    parent_audit_id: Optional[str] = None
```

Con esto, construir un `AuditRecord` para el caso común queda tan corto como
`AuditRecord(user=X, action=Y, resource=Z, details={...})` — casi igual de
ergonómico que `log_action()` hoy, sin necesitar un 2do método de conveniencia
en `AuditRepository` (`append(record)` solo, consistente con el resto del
catálogo — `Repository[T].add(entidad)` tampoco tiene una variante por
keyword-args).

```python
from app.models.domain_event import DomainEvent
from app.services.event_listener import EventListener


class AuditListener(EventListener):
    def __init__(self, audit_repo):  # AuditRepository, Línea B — ver Dependencias cruzadas
        self._audit_repo = audit_repo

    def on_event(self, evento: DomainEvent) -> None:
        self._audit_repo.append(AuditRecord.desde(evento))
```

**`AuditRecord.desde(evento)` — classmethod nuevo, agregar a `app/models/audit.py`**
(no es un archivo que Línea B esté editando en esta fase — `AuditRecord` es el VO,
`AuditRepository` es la clase nueva de Línea B, viven en archivos distintos). Acá
está el problema real que resuelve, encontrado armando esta fase, no estaba resuelto
en el diseño: `DomainEvent.tipo` es genérico a propósito
(`"recurso_aplicado"`/`"recurso_fallido"`, `Orquestador` no sabe qué VLAN/Puerto es)
— pero `AuditRecord.action` necesita ser específico (`"crear_vlan"`,
`"actualizar_descripcion_puerto"`, etc., RF-AUD-02 pide poder filtrar por tipo de
acción, y el código real de hoy sí guarda ese nivel de detalle). La resolución: `VLAN.aplicar()`/
`Puerto.aplicar()` (`FASE_2.md`) ya fueron actualizados para que su `resultado`
incluya una clave `"accion"` — `AuditRecord.desde()` la lee de ahí, con
`evento.tipo` como fallback para los eventos de `Inventory` (que no pasan por
`aplicar()`, ej. `"device_registrado"` ya es suficientemente específico). El
éxito/falla **no** se infiere del `payload`/`tipo` — `DomainEvent.exitoso` (`bool`,
agregado a la definición de `FINAL_ARCHITECTURE.md`, ver A1 arriba) lo carga
explícito, seteado por quien despacha el evento (`Orquestador` ya lo sabe con
certeza en ese momento — no hace falta que `AuditListener` lo adivine comparando
strings):

```python
    @classmethod
    def desde(cls, evento: "DomainEvent") -> "AuditRecord":
        from datetime import datetime, timezone

        accion = evento.payload.get("accion", evento.tipo)
        return cls(
            # id -- NO se pasa acá, usa el default_factory del propio campo
            # (arriba). Mismo resultado que pasar id=str(uuid.uuid4()) a mano
            # -- provisorio, AuditRepository.append() lo reemplaza al
            # persistir, ver B1 -- sin repetir el uuid.uuid4() que el campo
            # ya hace solo. Corrección real sobre una versión anterior de esta
            # fase, que sí lo pasaba explícito.
            timestamp=datetime.now(timezone.utc),
            user=evento.actor,
            action=accion,
            resource=evento.recurso.repositorio() if hasattr(evento.recurso, "repositorio") else type(evento.recurso).__name__.lower(),
            details=evento.payload,
            status="success" if evento.exitoso else "failure",
            device=getattr(evento.device, "name", None),
        )
```

**Nota sobre el `id` provisorio**: `AuditRecord.id` es `str` (`models/audit.py:8`),
pero la fila real (`AuditLogModel.id`) es un `Integer` autoincrement — mismo tipo de
mismatch que ya se corrigió para `Device` en Fase 1 (dataclass con id propio vs. PK
autoincrement real). La solución acá es la misma que ahí: el `id` que pone
`AuditRecord.desde()` es descartado — `AuditRepository.append()` (B1) inserta sin
`id` (lo genera la DB) y arma el `AuditRecord` final desde la fila recién insertada,
con el `id` real. No pasar el `AuditRecord` de `desde()` directo a upsert por su
propio `id` — no tendría sentido, `append()` nunca actualiza una fila existente (ver
B1, es insert estricto).

**Qué pasa con `append_audit_event()`/`ensure_audit_final_state()` (código real,
`audit_service.py:309-354`) — no se migran, quedan sin reemplazo.** Son un mecanismo
de cadena padre-hijo (una fila "pending"/"running" que después gana una fila
"completed"/"failed" enlazada por `parent_audit_id`) que la orquestación vieja usa
para trackear progreso intermedio. El `Orquestador` canónico de `FINAL_ARCHITECTURE.md`
(§2.4, Fase 5) despacha **un solo** `DomainEvent` por `ejecutar()` — nunca un evento
de "arranqué" seguido de otro de "terminé". No hay ningún caller real en el diseño
nuevo que necesite la cadena padre-hijo. Si en el futuro hace falta trackear
progreso intermedio real, es una feature nueva, no parte de esta migración — no
inventar un uso para `append_audit_event()` solo para no "perderlo".

### Callers reales que quedan rotos/sin dueño hasta Fase 5 (Línea A)

Nada de lo que existía antes se rompe en esta fase — `EventDispatcher`/`AuditListener`
son 100% código nuevo, sin caller. `audit_service.py` (real, 419 líneas) **sigue
existiendo y sigue siendo la ruta real de auditoría** hasta que `Orquestador` (Fase 5)
empiece a llamar `event_dispatcher.despachar(...)` en vez de que cada función de
`vlan_execution_service.py`/`port_*.py` llame `audit_service.log_action()` directo.

---

## Línea B

### B1 — `app/repositories/audit_repository.py` (archivo nuevo): `AuditRepository`

```python
from app.core.repository import Repository
from app.db.models import AuditLogModel
from app.db.session import get_session
from app.models.audit import AuditRecord
from app.models.visibility_scope import VisibilityScope


def _to_domain(row: AuditLogModel) -> AuditRecord:
    # idéntico a audit_service.py: _to_record() real, copiar tal cual
    return AuditRecord(
        id=str(row.id), timestamp=row.timestamp, user=row.user, action=row.action,
        resource=row.resource, resource_id=row.resource_id,
        details=row.details if row.details else {}, status=row.status,
        job_id=row.job_id, device=row.device, request_id=row.request_id,
        parent_audit_id=str(row.parent_audit_id) if row.parent_audit_id is not None else None,
    )


class AuditRepository(Repository):
    def __init__(self):
        super().__init__(AuditLogModel, _to_domain, to_orm=None)  # to_orm no se usa -- append() no pasa por Repository.add()

    def append(self, record: AuditRecord) -> AuditRecord:
        """Insert estricto -- NO session.merge(). Ver FASE_1.md, excepción
        documentada para AuditRecord (RNF-AUD/SRS §6.5, "a prueba de
        manipulaciones"): un id repetido tiene que fallar ruidoso, no
        sobreescribir en silencio un registro histórico.

        Confirmación real, no solo teórica -- encontrada con un 4to pase de
        revisión: `app/db/audit_guard.py` (real, instalado una sola vez en
        `init_db()`) es un listener `before_flush` de SQLAlchemy que bloquea
        cualquier UPDATE sobre `AuditLogModel` a nivel de sesión, sea cual sea
        el código que lo dispare -- ni siquiera hace falta este comentario
        para que el sistema se proteja solo. Si `append()` hubiera heredado
        `self.add()` (que sí hace `session.merge()`), un choque de `id` real
        habría disparado `AuditImmutabilityError` ahí mismo, no una
        sobreescritura silenciosa -- la excepción documentada arriba ya era
        necesaria por diseño, y además hay un guard real e independiente que
        la refuerza."""
        with get_session() as session:
            row = AuditLogModel(
                timestamp=record.timestamp, user=record.user, action=record.action,
                resource=record.resource, resource_id=record.resource_id,
                details=record.details, status=record.status, job_id=record.job_id,
                device=record.device, request_id=record.request_id,
            )
            session.add(row)  # insert puro, no merge
            session.flush()
            return _to_domain(row)

    def query(
        self, *, user=None, action=None, resource=None, status=None,
        from_date=None, to_date=None, device_id=None, site_id=None,
        scope: VisibilityScope, page=1, page_size=100,
    ) -> tuple[list[AuditRecord], int]:
        # copiar _apply_filters() (audit_service.py:68-129) tal cual para los
        # filtros simples (user/action/resource/status/from_date/to_date) --
        # sin cambios de lógica, solo se reemplaza el bloque `if viewer is not
        # None: q = _apply_msp_audit_scoping(...)` por lo de abajo.
        # device_id/site_id -- CORRECCIÓN real, faltaban en una versión anterior
        # de esta firma: son filtros que el CALLER pide explícito (ej. "solo
        # las filas del device X"), *además* del scope de visibilidad -- no son
        # lo mismo que "qué puede ver el usuario" (eso ya lo resuelve `scope`).
        # site_id acá también copia _apply_filters() (audit_service.py:99-114):
        # filas de devices de ese site, MÁS las filas sin device asociado.
        with get_session() as session:
            q = session.query(AuditLogModel).order_by(AuditLogModel.timestamp.desc())
            # ... filtros simples, copiados de _apply_filters() ...
            if device_id is not None:
                q = q.filter(AuditLogModel.device == device_id)
            if site_id is not None:
                from app.db.models import DeviceGroupModel, DeviceModel
                from sqlalchemy import or_
                nombres_site = [
                    r[0] for r in session.query(DeviceModel.name)
                    .join(DeviceGroupModel, DeviceModel.device_group_id == DeviceGroupModel.id)
                    .filter(DeviceGroupModel.site_id == site_id).all()
                ]
                if nombres_site:
                    q = q.filter(or_(AuditLogModel.device.is_(None), AuditLogModel.device.in_(nombres_site)))
                else:
                    q = q.filter(AuditLogModel.device.is_(None))
            q = self._aplicar_scope(q, session, scope)
            total = q.count()
            rows = q.offset((page - 1) * page_size).limit(page_size).all()
            return [_to_domain(r) for r in rows], total

    def _aplicar_scope(self, q, session, scope: VisibilityScope):
        if scope.es_system_admin:
            return q
        from sqlalchemy import and_, or_
        from app.db.models import AuditLogModel
        from app.composition import device_repository, device_group_repository  # Línea B, esta misma fase

        nombres = device_repository.nombres_visibles(scope)  # B3 -- None (no debería llegar acá) o un set
        grupos = device_group_repository.grupos_visibles(scope)  # B4 -- set de ids
        conds = []
        if nombres:
            conds.append(AuditLogModel.device.in_(nombres))
        conds.append(and_(AuditLogModel.device.is_(None), AuditLogModel.resource == "auth"))
        if grupos:
            conds.append(and_(
                AuditLogModel.resource == "device_group",
                AuditLogModel.resource_id.in_({str(g) for g in grupos}),
            ))
        conds.append(and_(AuditLogModel.resource == "site", AuditLogModel.resource_id.in_([str(s) for s in (scope.site_ids or set())])))
        return q.filter(or_(*conds))
```

**Resuelto — `DeviceGroupRepository` nueva, 6ta subclase de `Repository[T]`, no un
método más en `DeviceRepository`.** El problema real: `_apply_msp_audit_scoping()`
(código real, `audit_service.py:176-197`) calcula `visible_group_ids` como la unión
de "grupos con grant directo" (`scope.device_group_ids`, ya cubierto) **y** "grupos
cuyo site tiene grant site-wide" (`DeviceGroupModel.site_id IN scope.site_ids` — un
JOIN que ningún método de `VisibilityScope` resuelve). La fila de auditoría de un
`device_group` guarda `resource_id = str(group_id)` directo — no hay ningún device
de por medio — así que lo único que hace falta es el **set de IDs de grupo**
visibles, para comparar `AuditLogModel.resource_id.in_({...})`. Como este método no
toca `DeviceModel` para nada (solo `DeviceGroupModel`/`SiteModel`), forzarlo adentro
de `DeviceRepository` sería confuso — pasa a su propia clase, ver B4.

- `count(**filtros)` ← copiar `audit_service.py: count_audit_log()` (con el mismo
  fix de `scope` que `query()`).
- `purge_old(retention_days, triggered_by)` ← copiar `audit_service.py:
  purge_old_records()` tal cual (no toca `VisibilityScope`, es una operación de
  sistema, no de un usuario puntual). El `log_action(...)` final adentro de esta
  función (línea 396-406 real) pasa a ser `self.append(AuditRecord(...))` —
  mismo patrón, generar el `AuditRecord` a mano igual que hacía `log_action()`.

### B2 — `app/repositories/login_attempt_repository.py` (archivo nuevo): `LoginAttemptRepository`

```python
from app.core.repository import Repository
from app.db.models import LoginAttemptModel
from app.db.session import get_session
from datetime import datetime, timedelta, timezone

USERNAME_FAIL_LIMIT = 5
USERNAME_LOCKOUT_MINUTES = 15
IP_FAIL_LIMIT = 20
IP_LOCKOUT_HOURS = 1


class LoginAttemptRepository:
    """No hereda de Repository[T] -- ver nota abajo. Todas sus consultas son
    agregaciones/counts/deletes por ventana de tiempo, ninguna lee/escribe una
    fila individual como objeto de dominio -- get()/add()/list()/remove()
    genéricos no tendrían ningún caller real acá."""

    def esta_bloqueado(self, username: str) -> bool:
        # copiar is_username_locked() (login_attempt_service.py:12-24) tal cual
        ...

    def ip_bloqueada(self, ip: str) -> bool:
        # copiar is_ip_blocked() (líneas 27-39) tal cual
        ...

    def registrar_intento(self, username: str, ip: str, exitoso: bool) -> None:
        # copiar record_attempt() (líneas 42-51) tal cual
        ...

    def resetear(self, username: str) -> int:
        # unifica reset_username_failures() (54-59) + unlock_username() (62-68)
        # -- eran casi idénticas, una devolvía el count borrado y la otra no;
        # queda la que sí devuelve
        ...
```

**Decisión tomada en esta revisión, corrige una versión anterior de esta fase**: no
hereda de `Repository[T]`. La primera versión de este documento la hacía heredar con
`to_domain=None, to_orm=None` — funcionaba (nunca se llaman los métodos heredados),
pero heredar un contrato de 4 métodos para no usar ninguno es ruido, no ayuda.

**Criterio para decidir esto de forma consistente en el resto del plan** (no es "¿esta
clase puntual llama a `get`/`add`/`list`/`remove`?" — ese test da mal hasta para
`AuditRepository`, que tampoco los usa hoy): ¿el concepto de fondo **es o va a ser**
una entidad con identidad individual (algo que en algún momento tiene sentido pedir
"tráeme el #47", agregar uno, borrar uno), aunque esta clase puntual todavía no
ejerza esa parte? Si sí, hereda — aunque hoy solo use el método nuevo (`AuditRecord`,
`RoleAssignment`, `Device` son entidades reales, con identidad, aunque
`AuditRepository`/`RoleAssignmentRepository` en este plan solo usen `query()`/
`scope_de()` por ahora). Si no — si el concepto **nunca** tiene sentido como "una
fila individual" (un intento de login no se "busca por id", solo se cuenta en
ventanas o se borra en bloque) — no hereda. `LoginAttemptRepository` cae del lado
que no hereda. `DeviceGroup` (B4, abajo) sí es una entidad real con identidad —
**se revierte la decisión de B4 más abajo, sí hereda de `Repository[T]`**, aunque
`to_domain`/`to_orm` queden en `None` por ahora (misma razón que `AuditRepository`:
nadie construyó la entidad `DeviceGroup` todavía en este plan, no porque el concepto
no la tenga).

### B3 — `app/repositories/device_repository.py` (archivo nuevo): `DeviceRepository`

**Primera vez que se instancia `Repository[Device]` en este plan** — Fase 1 solo
limpió la clase `Device`, no instanció su repository. Adaptar `_to_domain()`
real (`device_service.py:16-40`, ya leída para Fase 1) tal cual — el mapeo
`DeviceModel` → `Device` no cambió en ninguna fase anterior.

```python
from app.core.repository import Repository
from app.db.models import DeviceGroupModel, DeviceModel
from app.db.session import get_session
from app.models.device import Device
from app.models.visibility_scope import VisibilityScope


def _to_domain(row: DeviceModel) -> Device:
    # copiar device_service.py: _to_domain() (líneas 16-40) tal cual
    ...


def _to_orm(d: Device) -> DeviceModel:
    return DeviceModel(
        name=d.name, host=d.host, vendor=d.vendor, platform=d.platform,
        username=d.username, encrypted_password=d.encrypted_password,
        device_group_id=d.device_group_id,
    )


class DeviceRepository(Repository):
    def __init__(self):
        super().__init__(DeviceModel, _to_domain, _to_orm, pk_field="name")  # Fase 1: Device usa .name, no .id

    def nombres_visibles(self, scope: VisibilityScope) -> "set[str] | None":
        """None = sin restricción (system-admin, el caller se salta el filtro
        IN). Copia el JOIN real de inventory_service.py: _visible_device_names()
        (líneas 334-380, ya leída), pero recibiendo VisibilityScope en vez de
        recalcular sus propios grants."""
        if scope.es_system_admin:
            return None
        if not scope.site_ids and not scope.device_group_ids:
            return set()
        from sqlalchemy import or_
        with get_session() as session:
            q = session.query(DeviceModel.name).join(
                DeviceGroupModel, DeviceModel.device_group_id == DeviceGroupModel.id
            )
            conds = []
            if scope.site_ids:
                conds.append(DeviceGroupModel.site_id.in_(scope.site_ids))
            if scope.device_group_ids:
                conds.append(DeviceModel.device_group_id.in_(scope.device_group_ids))
            return {r[0] for r in q.filter(or_(*conds)).all()}
```

Agregar `device_repository = DeviceRepository()` a `app/composition.py` — usado por
`AuditRepository.query()` (B1) y, en Fase 4, por `JobRepository.query()`.

**`device_service.py` no se borra en esta fase** — sigue existiendo, con
`create_device()`/`get_device()`/etc reales, sin tocar. `DeviceRepository` es
**adicional**, no un reemplazo todavía — el reemplazo real de `device_service.py`
pasa por rewirear cada uno de sus callers a `DeviceRepository`/`Device`, que no es
parte de esta fase (no hay ningún caller que hoy necesite `DeviceRepository` — el
único, `AuditRepository.query()`, es nuevo en esta misma fase).

### B4 — `app/repositories/device_group_repository.py` (archivo nuevo): `DeviceGroupRepository`

Sexta subclase de `Repository[T]` — un solo método, mismo criterio de justificación
que las otras 5 (§2.2.1: necesita un JOIN que `filter_by()` no expresa). No toca
`DeviceModel` para nada, a diferencia de `DeviceRepository` — resuelve visibilidad de
**grupos**, no de devices.

```python
from app.core.repository import Repository
from app.db.models import DeviceGroupModel
from app.db.session import get_session
from app.models.visibility_scope import VisibilityScope


class DeviceGroupRepository(Repository):
    def __init__(self):
        # to_domain/to_orm en None -- a diferencia de LoginAttemptRepository
        # (B2), DeviceGroup SÍ es una entidad real con identidad individual
        # (a diferencia de un intento de login, que nunca se busca "por id"),
        # por eso esta clase sí hereda de Repository[T]. Los mappers quedan
        # en None simplemente porque nadie construyó la entidad DeviceGroup
        # todavía en este plan (device_group_service.py sigue trabajando con
        # DeviceGroupRead + filas ORM directo) -- completar cuando/si una
        # fase futura la construya, no acá.
        super().__init__(DeviceGroupModel, to_domain=None, to_orm=None)

    def grupos_visibles(self, scope: VisibilityScope) -> set[int]:
        """IDs de DeviceGroup visibles: grant directo al grupo (scope.device_group_ids)
        UNION grupos cuyo site tiene grant site-wide (scope.site_ids). Resuelve el
        JOIN que _apply_msp_audit_scoping() (audit_service.py:176-197) hace hoy
        para 'visible_group_ids', que VisibilityScope solo no puede responder."""
        if scope.es_system_admin:
            return set()  # caller (AuditRepository) ya cortó antes por es_system_admin -- no debería llegar acá
        directos = set(scope.device_group_ids)
        if not scope.site_ids:
            return directos
        with get_session() as session:
            via_site = {
                r[0] for r in session.query(DeviceGroupModel.id)
                .filter(DeviceGroupModel.site_id.in_(scope.site_ids))
                .all()
            }
        return directos | via_site
```

**Nota real**: hoy no existe un dataclass `DeviceGroup` de dominio —
`device_group_service.py` trabaja con `DeviceGroupRead` (schema Pydantic de
respuesta HTTP) y filas ORM directo. Construir esa entidad (con `renombrar()`/
`tiene_devices()`, `FINAL_ARCHITECTURE.md` §1) no es parte de este plan — por eso
`to_domain`/`to_orm` quedan en `None` arriba, no inventarla acá solo para esta
clase.

Agregar `device_group_repository = DeviceGroupRepository()` a `app/composition.py`.

---

## Dependencias cruzadas

- **`AuditListener` (A4) recibe `AuditRepository` (B1) por constructor** — mismo
  patrón que el resto del documento (inyección, no import directo). Como nada cablea
  `AuditListener` todavía (no hay composition real hasta Fase 5), esto no es
  bloqueante — A4 puede escribir `AuditListener.__init__(self, audit_repo)` sin que
  B1 exista todavía, el tipo se resuelve en tiempo de ejecución, no de escritura.
- **`AuditRepository._aplicar_scope()` (B1) llama `device_repository.nombres_visibles()`
  (B3)** — ambas están en Línea B, en la misma fase, sin conflicto de archivo (viven
  en 2 archivos nuevos distintos). Orden sugerido: escribir B3 antes que la parte de
  `query()` de B1, ya que B1 depende de que exista.
- Ninguna dependencia real entre Línea A y Línea B en esta fase más allá de la
  inyección por constructor de `AuditListener` (que no bloquea, ver arriba).

## Criterio de finalización

- [ ] `DomainEvent` tiene `actor`. Confirmado en `FINAL_ARCHITECTURE.md` (los 7 call
      sites reales, no solo la definición).
- [ ] `EventListener`, `EventDispatcher`, `AuditListener` existen, con las firmas de
      este documento. `AuditRecord.desde(evento)` existe en `app/models/audit.py` y
      usa `payload.get("accion", evento.tipo)`.
- [ ] `AuditRecord.id`/`timestamp`/`status` tienen default (`Field(default_factory=...)`/
      `"success"`) — confirmar que `AuditRecord(user=X, action=Y, resource=Z,
      details={...})` alcanza para construir uno válido, sin pasar `id`/`timestamp` a mano.
- [ ] `VLAN.aplicar()`/`Puerto.aplicar()` (Fase 2, revisar si hace falta actualizar
      ahí) incluyen `"accion"` en el dict que devuelven.
- [ ] `AuditRepository.append()` usa `session.add()`, confirmado que **no** llama a
      `self.add()` heredado de `Repository[T]` (que sería `session.merge()`).
- [ ] `AuditRepository.query()`/`count()` reciben `scope: VisibilityScope`, no
      `viewer: dict`. El filtro `resource='device_group'` usa
      `device_group_repository.grupos_visibles(scope)` (B4), no solo
      `scope.device_group_ids`.
- [ ] `LoginAttemptRepository` tiene los 4 métodos (`esta_bloqueado`/`ip_bloqueada`/
      `registrar_intento`/`resetear`), mismo comportamiento que las 5 funciones
      reales que reemplaza.
- [ ] `DeviceRepository.nombres_visibles()` existe, devuelve `None` para
      system-admin, `set()` vacío si no hay grants, el set real si hay — mismo
      comportamiento que `inventory_service._visible_device_names()`.
- [ ] `DeviceGroupRepository.grupos_visibles()` existe, devuelve la unión de
      `scope.device_group_ids` (directos) y los grupos cuyo `site_id` está en
      `scope.site_ids` (vía site-wide) — mismo resultado que `visible_group_ids`
      en `_apply_msp_audit_scoping()` real.

- [ ] `python -c "import app.composition"` y `python -c "import app.main"` corren sin error (ver `FASE_7.md` sección 5).

## Riesgos / cosas a validar
- **`AuditRepository.__init__` pasa `to_orm=None`** — como `append()` no usa
  `Repository.add()` (usa `session.add()` directo, insert estricto), el `to_orm`
  heredado nunca se invoca. Si alguna vez alguien llama `audit_repo.add(...)` por
  error (el método heredado, no `append()`), va a fallar con un error de "`None` no
  es invocable" en vez de silenciosamente hacer upsert sobre un audit record — es el
  comportamiento querido (fail loud), pero vale confirmarlo si `Repository[T]`
  cambia de forma en una fase futura.
- **`LoginAttemptRepository`/`AuditRepository` con `to_domain=None`/`to_orm=None`
  parcial es un poco raro de leer** — si al escribir el código real esto genera
  fricción (linters de tipos quejándose, etc.), la alternativa de no heredar
  `Repository[T]` para estas 2 clases está mencionada en B2 — no es una decisión
  cerrada, evaluar en el momento.
