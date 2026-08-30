# Fase 4 — `Job` rico + `JobRepository` (Línea A, con un aporte de Línea B) · elimina `GroupJob`/`DeviceExecution`

> Parte de [plan de migración a `FINAL_ARCHITECTURE.md`](README.md). Requiere
> [`FASE_1.md`](FASE_1.md), [`FASE_2.md`](FASE_2.md) y [`FASE_3.md`](FASE_3.md)
> terminadas — en particular, Línea B necesita `VisibilityScope`/`DeviceRepository`/
> `DeviceGroupRepository` (Fase 2/3) para su único aporte de esta fase.

## Objetivo

**Línea A**: `Job` gana comportamiento real (`_transicionar()`, `marcar_iniciado()`/
`marcar_completado()`/`marcar_fallido()`/`cancelar()`/`registrar_reintento()`/
`asegurar_estado_final()`, `esta_en_estado_terminal()`) sobre una tabla de
transiciones explícita — hoy esa lógica está implícita y sin validar, repartida
entre `update_job()`/`cancel_job()`. `JobRepository` (esqueleto: CRUD + los métodos
de dominio que no dependen de RBAC). Se elimina `GroupJob`/`DeviceExecution`
(`models/group_job.py`) y `group_job_service.py` — reemplazados por
`Job.group_job_id` (ya existe) + `JobRepository.resumen_de_grupo()`, calculado al
vuelo, sin estado duplicado.

**Línea B**: agrega `JobRepository.query(scope)` — **al mismo archivo que Línea A
crea en esta fase**, el único sync point estricto de todo el plan (ver
"Dependencias cruzadas").

## Estado previo esperado

- Fases 1-3 completas.
- Nadie tocó todavía: `app/models/job.py`, `app/models/group_job.py`,
  `app/services/group_job_service.py`, `app/api/group_jobs.py`,
  `app/services/job_service.py`.

---

## Línea A

### A1 — `app/models/job.py`: completar `Job`

**Estado actual real** (ya leído completo, 27 líneas): dataclass plano, sin ningún
método — todas las transiciones de estado viven en `job_service.py: update_job()`,
que **no valida nada**, simplemente asigna `row.status = status` con lo que le
pasen. `cancel_job()` sí tiene una regla real: solo cancela si `row.status in
("pending", "running")` — la única validación de transición que existe hoy, en
cualquier lado.

**Tabla de transiciones — derivada de la lógica real, no inventada**:

```python
_TRANSICIONES_VALIDAS: dict[str, set[str]] = {
    "pending":   {"running", "cancelled"},
    "running":   {"completed", "failed", "cancelled"},
    "completed": set(),  # terminal
    "failed":    set(),  # terminal
    "cancelled": set(),  # terminal
}
```

`pending→cancelled` y `running→cancelled` cubren exactamente lo que ya permite
`cancel_job()` real (`job_service.py:209`, `if row.status in ("pending", "running")`).
`pending→running`, `running→completed`, `running→failed` cubren lo que
`update_job()` ya hace en la práctica (nunca se ve `pending→completed` directo en
ningún caller real — siempre pasa por `running` primero).

**Campo nuevo, decisión real no anticipada en `FINAL_ARCHITECTURE.md`**: agregar
`operation: Optional[str] = None`. Motivo: `GroupJob.operation` (ej. `"create_vlan"`)
es un dato real que `GET /group-jobs/{id}` expone hoy (`api/group_jobs.py:17`,
`_format_group_job()`) — pero `Job` **no tiene** ningún campo equivalente. Sin
agregarlo, `JobRepository.resumen_de_grupo()` (A3, abajo) no tiene de dónde sacar
ese dato al eliminar `GroupJob`, y la respuesta de `GET /group-jobs/{id}` pierde el
campo `operation` en silencio. `GroupOperationRunner.encolar()` (Fase 5, ya conoce
la operación al construir cada `Job`) lo setea en cada `Job` del grupo.

```python
@dataclass
class Job:
    job_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: str = "pending"
    operation: Optional[str] = None  # nuevo
    playbook: Optional[str] = None
    device: Optional[str] = None
    parameters: Optional[dict] = None
    result: Optional[dict] = None
    error: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    retry_count: int = 0
    max_retries: int = 3
    rollback_performed: bool = False
    rollback_success: Optional[bool] = None
    pre_state: Optional[dict] = None
    last_error: Optional[str] = None
    current_step: Optional[str] = None
    group_job_id: Optional[str] = None

    def _transicionar(self, nuevo_status: str) -> None:
        validas = _TRANSICIONES_VALIDAS.get(self.status, set())
        if nuevo_status not in validas:
            raise TransicionInvalidaError(
                f"Job {self.job_id}: transición inválida {self.status!r} -> {nuevo_status!r}"
            )
        self.status = nuevo_status

    def esta_en_estado_terminal(self) -> bool:
        return not _TRANSICIONES_VALIDAS.get(self.status, {"__nunca__"})  # set vacío = terminal

    def marcar_iniciado(self) -> None:
        self._transicionar("running")
        self.started_at = datetime.now(timezone.utc)
        self.current_step = "executing"

    def marcar_completado(self, resultado: dict) -> None:
        self._transicionar("completed")
        self.result = resultado
        self.finished_at = datetime.now(timezone.utc)
        self.current_step = "completed"

    def marcar_fallido(self, error: str, rollback_performed: bool = False, rollback_success: Optional[bool] = None) -> None:
        self._transicionar("failed")
        self.error = error
        self.rollback_performed = rollback_performed
        self.rollback_success = rollback_success
        self.finished_at = datetime.now(timezone.utc)
        self.current_step = "failed"

    def cancelar(self) -> None:
        self._transicionar("cancelled")
        self.finished_at = datetime.now(timezone.utc)

    def registrar_reintento(self, delay: float) -> None:
        self.retry_count += 1
        self.last_error = self.error
        self.current_step = "retrying"

    def asegurar_estado_final(self) -> None:
        """Única excepción deliberada -- NO llama a _transicionar(). Recuperación
        ante crash (server restart con jobs en 'running'), no una transición de
        negocio -- forzar 'running'->'failed' violaría la tabla de arriba a
        propósito, es exactamente el caso que esta excepción cubre."""
        if not self.esta_en_estado_terminal():
            self.status = "failed"
            if not self.error:
                self.error = "Unexpected termination"
            self.finished_at = datetime.now(timezone.utc)
```

**`TransicionInvalidaError`** — excepción nueva, agregar a `app/core/exceptions.py`
(mapea a 409, mismo criterio que el resto de excepciones de ese archivo — revisar
cómo están registradas las demás y seguir el mismo patrón, no inventar uno nuevo).

**Ojo con `cancelar()` — hoy es no-op silencioso, pasa a lanzar.** El real
`cancel_job()` simplemente no hace nada si el job ya está en estado terminal (no
lanza, no avisa). Con `_transicionar()`, `cancelar()` sobre un Job ya `completed`
**lanza** `TransicionInvalidaError`. Esto es un cambio de comportamiento real, ya
señalado como intencional en `FINAL_ARCHITECTURE.md` §1 (`cancelar()` "pero ahora
lanza `TransicionInvalidaError` en vez de no-op silencioso") — el caller
(`api/jobs.py: cancel_job()`, Fase 5) necesita atrapar esa excepción y traducirla al
mismo 409 que ya devuelve hoy (`"Cannot cancel job with status '{status}'"`) — no
dejar que se propague como 500.

### A2 — Rewirear `api/group_jobs.py` — NO se borran `GroupJob`/`group_job_service.py` todavía

**Corrección real sobre el diseño original, encontrada implementando esta
sección — mismo patrón que `validators/*.py`/`PortInfo`/`PortConfigRequest`
en fases anteriores.** El texto original decía borrar `app/models/group_job.py`
(`GroupJob`, `DeviceExecution`) y `app/services/group_job_service.py` completos
en esta fase. Falso: `group_job_service` tiene imports **a nivel de módulo**
en `vlan_execution_service.py` (línea 6, `from app.services import
audit_service, group_job_service, job_service, vlan_service`),
`port_execution_service.py` y `port_config_service.py` (ambos con
`group_job_service` dentro de su `from app.services import (...)` top-level) —
y esos 3 archivos son importados a nivel de módulo por `api/vlans.py`/
`api/ports.py`. Borrar `group_job_service.py` acá rompe esos imports →
`api/vlans.py`/`api/ports.py` truenan → `app.main` no arranca — mismo tipo de
problema ya encontrado 2 veces antes en este plan. `app/models/group_job.py`
tiene un solo caller real, el propio `group_job_service.py` — como ese no se
borra, tampoco `models/group_job.py`.

**Los 2 se mantienen sin ningún cambio** hasta que Fase 5 (A4/A5) rewiree
`vlan_execution_service.py`/`port_execution_service.py`/`port_config_service.py`
para que dejen de necesitar `group_job_service` — recién ahí quedan sin caller
real y se borran. Agregados a la tabla de limpieza de `FASE_7.md` (no
estaban — la tabla ya tenía a los 3 archivos de ejecución que los importan,
pero no a `group_job_service.py`/`models/group_job.py` en sí).

`app/db/models.py: GroupJobModel` **se deja** — es schema de DB, fuera de
alcance de este plan (decisión ya tomada, sin migraciones). La tabla real
queda huérfana, sin código que la escriba ni la lea — aceptado, no se dropea
acá.

`app/api/group_jobs.py` — el único endpoint (`GET /group-jobs/{group_job_id}`)
cambia de `group_job_service.get_group_job(id)` a `job_repository.resumen_de_grupo(id)`
(A3, abajo). El archivo ya tenía el import roto desde Fase 2 (`core.dependencies`
eliminado, B5) — en esta fase además cambia de qué servicio depende:

```python
from app.core.scope import require_authenticated
from app.core.exceptions import NotFoundError


@router.get("/{group_job_id}", ...)
def get_group_job(group_job_id: str, current_user: dict = Depends(require_authenticated)):
    resumen = job_repository.resumen_de_grupo(group_job_id)
    if resumen is None:
        raise NotFoundError(f"Group job '{group_job_id}' not found")
    return {"success": True, "data": resumen}
```

`resumen_de_grupo()` (A3) devuelve `None` (no una lista vacía) cuando no existe
**ningún** `Job` con ese `group_job_id` — así el endpoint puede seguir
distinguiendo 404 de "grupo real con 0 resultados" (que no debería pasar nunca,
pero la distinción importa).

### A3 — `app/repositories/job_repository.py` (archivo nuevo): `JobRepository`

**Primera vez que se instancia `Repository[Job]`.** Igual que `Device` (Fase 1),
`Job.job_id` (dataclass) y `JobModel.id` (DB, autoincrement) **no son la misma
columna** — la clave real es `job_id` (`JobModel.job_id`, `unique=True, index=True`).
Mismo `pk_field` que el resto de las entidades con este patrón.

**Esto expuso un bug real en `Repository[T].add()` — ver `FASE_1.md`, nota debajo
del código de `Repository[T]`.** `session.merge()` identifica filas por la PK real
de SQLAlchemy, no por `pk_field` — sin el fix (ya aplicado en `FASE_1.md`), un
segundo `add()` sobre el mismo `job_id` (ej. `marcar_completado()` después de
`marcar_iniciado()`) revienta con `UNIQUE constraint failed` en vez de actualizar.
Corregir `app/core/repository.py` (Fase 1) **antes** de escribir/probar esta
sección — si se llega acá con la versión vieja de `add()`, `JobRepository` va a
fallar exactamente así apenas se pruebe un update real.

Además falta una columna real: `app/db/models.py: JobModel` **no tiene**
`operation` (ver campo nuevo arriba) — agregarla antes de escribir
`_to_orm`/`_to_domain` de abajo, quedaron con `row.operation`/`j.operation` dando
por hecho que ya existe.

```python
from app.core.repository import Repository
from app.db.models import JobModel

```python
from app.core.repository import Repository
from app.db.models import JobModel
from app.db.session import get_session
from app.models.job import Job


def _to_domain(row: JobModel) -> Job:
    # copiar job_service.py: _to_job() (líneas 19-39) tal cual, + operation=row.operation (A1)
    ...


def _to_orm(j: Job) -> JobModel:
    return JobModel(
        job_id=j.job_id, status=j.status, operation=j.operation, playbook=j.playbook,
        device=j.device, parameters=j.parameters, result=j.result, error=j.error,
        created_at=j.created_at, started_at=j.started_at, finished_at=j.finished_at,
        retry_count=j.retry_count, max_retries=j.max_retries,
        rollback_performed=j.rollback_performed, rollback_success=j.rollback_success,
        pre_state=j.pre_state, last_error=j.last_error, current_step=j.current_step,
        group_job_id=j.group_job_id,
    )


class JobRepository(Repository):
    def __init__(self):
        super().__init__(JobModel, _to_domain, _to_orm, pk_field="job_id")

    def activo_para(self, device: str) -> bool:
        """¿Hay un Job pending/running para este device? Usado por
        Inventory.move() (FINAL_ARCHITECTURE.md §1, nota 1/2) para bloquear
        mover un device con trabajo en curso."""
        with get_session() as session:
            existe = (
                session.query(JobModel)
                .filter(JobModel.device == device, JobModel.status.in_(("pending", "running")))
                .first()
            )
            return existe is not None

    def recuperar_huerfanos(self) -> int:
        """Reemplaza job_service.py: mark_orphaned_jobs_failed() (líneas 216-232) --
        copiar la lógica tal cual, usando Job.asegurar_estado_final() en vez de
        mutar status/error/finished_at a mano."""
        with get_session() as session:
            filas = session.query(JobModel).filter(JobModel.status == "running").all()
            n = 0
            for row in filas:
                job = _to_domain(row)
                job.asegurar_estado_final()
                row.status = job.status
                row.error = job.error
                row.finished_at = job.finished_at
                n += 1
            return n

    def resumen_de_grupo(self, group_job_id: str) -> "dict | None":
        """Reemplaza GroupJob completo (A2) -- agrega los Job reales al vuelo,
        sin estado duplicado. Devuelve None si no hay ningún Job con este
        group_job_id (equivalente al 404 que hoy da get_group_job() real).

        Forma exacta verificada contra frontend/src/types/job.ts (GroupJob/
        GroupJobExecutionSummary/GroupJobDeviceResult) -- ver nota abajo, 2
        campos reales que una versión anterior de este método no tenía."""
        jobs = self.list(group_job_id=group_job_id)
        if not jobs:
            return None
        completed = sum(1 for j in jobs if j.status == "completed")
        failed = sum(1 for j in jobs if j.status == "failed")
        rollback_count = sum(1 for j in jobs if j.rollback_performed)
        estados = {j.status for j in jobs}
        todos_terminales = all(j.esta_en_estado_terminal() for j in jobs)
        if estados <= {"pending"}:
            status_grupo = "pending"   # ningún device arrancó todavía
        elif not todos_terminales:
            status_grupo = "running"
        elif failed == 0:
            status_grupo = "completed"
        elif completed == 0:
            status_grupo = "failed"
        else:
            status_grupo = "partial_success"
        primero = jobs[0]
        started = [j.started_at for j in jobs if j.started_at]
        finished = [j.finished_at for j in jobs if j.finished_at]
        duration_ms_grupo = (
            round((max(finished) - min(started)).total_seconds() * 1000)
            if started and finished and todos_terminales else None
        )

        def _duration_ms(j: "Job") -> "int | None":
            if j.started_at and j.finished_at:
                return round((j.finished_at - j.started_at).total_seconds() * 1000)
            return None

        return {
            "group_job_id": group_job_id,
            "status": status_grupo,
            "operation": primero.operation,
            "playbook": primero.playbook,
            "parameters": primero.parameters,
            "created_at": min((j.created_at for j in jobs), default=None),
            "started_at": min(started, default=None),
            "finished_at": max(finished, default=None) if todos_terminales else None,
            "execution_summary": {
                "total_devices": len(jobs),
                "completed": completed,
                "failed": failed,
                "partial_success": completed > 0 and failed > 0,
                "rollback_count": rollback_count,
                "duration_ms": duration_ms_grupo,
            },
            "device_results": [
                {
                    "device": j.device, "job_id": j.job_id, "status": j.status,
                    "current_step": j.current_step, "retry_count": j.retry_count,
                    "rollback_performed": j.rollback_performed,
                    "rollback_success": j.rollback_success, "error": j.error,
                    "duration_ms": _duration_ms(j),
                }
                for j in jobs
            ],
        }
```

**Los 2 campos de arriba (`status="pending"` distinto de `"running"`,
`duration_ms` por device y de grupo) se encontraron comparando este método
contra `frontend/src/types/job.ts` real** (`GroupJobStatus`,
`GroupJobDeviceResult.duration_ms`, `GroupJobExecutionSummary.duration_ms`) —
no estaban en ninguna versión anterior de esta fase. El tipo de frontend
también declara un 6to valor de status, `"partial_failure"` — **no se
implementa**, confirmado con grep sobre `group_job_service.py` real que
`update_device_result()` nunca lo produce (solo `completed`/`partial_success`/
`failed`/`running`, más `"pending"` como default de la fila antes del primer
update) — es un valor muerto ya en el sistema actual, no una omisión de este
plan.

**`self.list(group_job_id=group_job_id)` usa el `list(**filtros)` genérico de
`Repository[T]`** (Fase 1, `filter_by(**filtros)`) — funciona porque es un filtro de
igualdad simple, exactamente el caso que `list()` genérico ya cubre, no hace falta
un método dedicado.

### Callers reales que quedan rotos hasta Fase 5 (Línea A)

`job_service.py` (real, 233 líneas) **sigue existiendo y sigue siendo la ruta real**
— nada la reemplaza todavía (`Orquestador`, que va a llamar a `Job`/`JobRepository`
en vez de `job_service.*`, es Fase 5). `api/jobs.py` sigue llamando
`job_service.get_job()`/`cancel_job()`/`query_jobs()` sin tocar.
`main.py:70-71` (`job_service.mark_orphaned_jobs_failed()`, llamado al arrancar) —
**no se toca en esta fase**, sigue llamando la función vieja; el rewire a
`job_repository.recuperar_huerfanos()` es tarea de Fase 5 (cablear `main.py` con lo
nuevo es parte de terminar la composición, no de construir las piezas).

---

## Línea B

### B1 — Agregar `query(scope)` a `app/repositories/job_repository.py`

**El único sync point estricto de todo el plan.** Línea A crea el archivo completo
(A3, arriba) — Línea B agrega **un método más** a la misma clase, en el mismo
archivo, después. No es "cada línea edita su propio archivo" como en el resto de
las fases — acá **hay que coordinarse en el tiempo**, no solo en el espacio: B no
abre este archivo para editar hasta que A haya terminado y el archivo esté
mergeado/estable.

```python
    def query(
        self, *, status=None, device=None, site_id=None, from_date=None, to_date=None,
        scope: "VisibilityScope", page=1, page_size=50,
    ) -> tuple[list[Job], int]:
        from app.composition import device_repository
        with get_session() as session:
            q = session.query(JobModel)
            if status is not None:
                q = q.filter(JobModel.status == status)
            if device is not None:
                q = q.filter(JobModel.device == device)
            if from_date is not None:
                q = q.filter(JobModel.created_at >= from_date)
            if to_date is not None:
                q = q.filter(JobModel.created_at <= to_date)
            if site_id is not None:
                from app.db.models import DeviceGroupModel, DeviceModel
                nombres_site = [
                    r[0] for r in session.query(DeviceModel.name)
                    .join(DeviceGroupModel, DeviceModel.device_group_id == DeviceGroupModel.id)
                    .filter(DeviceGroupModel.site_id == site_id).all()
                ]
                if not nombres_site:
                    return [], 0
                q = q.filter(JobModel.device.in_(nombres_site))
            if not scope.es_system_admin:
                nombres = device_repository.nombres_visibles(scope)
                if nombres is not None:
                    if not nombres:
                        return [], 0
                    q = q.filter(JobModel.device.in_(nombres))
            total = q.count()
            rows = q.order_by(JobModel.created_at.desc()).offset((page - 1) * page_size).limit(page_size).all()
            return [_to_domain(r) for r in rows], total
```

**Corrección real — `device`/`site_id` SÍ son parámetros propios, no se
unifican en `scope`.** Una versión anterior de esta fase decía lo contrario
("se unifican en este único parámetro `scope`") — error, mismo tipo de
confusión que ya se había corregido para `AuditRepository.query()` (Fase 3):
`scope` resuelve **quién puede ver qué** (autorización); `device`/`site_id`
acá son **filtros que el usuario pide explícito** sobre lo que ya puede ver
("de lo que veo, mostrame solo lo del site X") — 2 preguntas distintas, la
misma distinción que ya se había hecho para `AuditRepository`. Confirmado con
el cliente real del frontend: `frontend/src/services/api.ts: getJobs()` manda
`site_id`/`device` como query params reales, separados de la autorización
(que ni siquiera es un parámetro visible del lado del cliente — la resuelve
el JWT). `api/jobs.py: list_jobs()` (Fase 5, A8) pasa a llamar
`job_repository.query(status=..., device=device_id, site_id=site_id, scope=scope, ...)`.

---

## Dependencias cruzadas

- **B1 depende de que A3 esté terminado y estable antes de tocar
  `app/repositories/job_repository.py`.** Es el único sync point estricto del plan
  completo — todas las demás fases evitan que A y B toquen el mismo archivo; acá no
  se pudo evitar porque `JobRepository` combina métodos de dominio (A, sin RBAC) y
  de consulta con scope (B, con RBAC) en la misma clase. Coordinar el momento real
  (aviso explícito de "A terminó, mergeado" antes de que B empiece) en vez de que
  ambas líneas asuman que pueden trabajar en paralelo acá.
- **B1 depende de `DeviceRepository.nombres_visibles()` (Fase 3, ya terminada)** —
  sin dependencia nueva más allá de lo que Fase 3 ya entregó.
- **A2 (borrar `api/group_jobs.py`'s dependencia vieja) depende de A3
  (`resumen_de_grupo()`)** — dependencia interna de Línea A, mismo archivo de
  trabajo, sin cruce con B.

## Criterio de finalización

- [ ] `Job` tiene `operation`, `_transicionar()`, `esta_en_estado_terminal()`,
      `marcar_iniciado`/`marcar_completado`/`marcar_fallido`/`cancelar`/
      `registrar_reintento`/`asegurar_estado_final`. `_TRANSICIONES_VALIDAS`
      coincide con las reglas reales (`cancel_job()`'s guard, especialmente).
- [ ] `TransicionInvalidaError` existe en `app/core/exceptions.py`, mapea a 409.
- [ ] **`app/models/group_job.py`, `app/services/group_job_service.py` siguen
      existiendo, sin cambios** — corrección real: `vlan_execution_service.py`/
      `port_execution_service.py`/`port_config_service.py` los importan a nivel
      de módulo, se borran en Fase 7 recién cuando Fase 5 los deje sin caller.
      `app/db/models.py: GroupJobModel` sigue existiendo (no se toca, fuera de
      alcance).
- [ ] `app/api/group_jobs.py` usa `job_repository.resumen_de_grupo()`, no
      `group_job_service`. Usa `require_authenticated`, no
      `core.dependencies.get_current_user` (arrastrado de Fase 2, confirmar que no
      quedó sin corregir).
- [ ] `JobRepository` (A3) existe con `pk_field="job_id"`, `activo_para()`,
      `recuperar_huerfanos()`, `resumen_de_grupo()`. `JobModel.operation` existe
      en `app/db/models.py`. `Repository[T].add()` (Fase 1) tiene el fix de
      PK real vs `pk_field` — confirmar marcando un `Job` completado después de
      iniciado (2 `add()` seguidos) sin `UNIQUE constraint failed`.
- [ ] `JobRepository.query()` (B1) existe en el **mismo archivo**, agregado
      después de que A3 se dio por terminado — no en un archivo separado, no
      duplicando la clase. Recibe `device`/`site_id` como filtros propios,
      además de `scope` — no unificados, confirmado contra
      `frontend/src/services/api.ts: getJobs()`.

- [ ] `python -c "import app.composition"` y `python -c "import app.main"` corren sin error (ver `FASE_7.md` sección 5).

## Riesgos / cosas a validar

- **`cancelar()` pasa de no-op silencioso a excepción** — el caller real
  (`api/jobs.py: cancel_job()`, Fase 5) tiene que atraparla y devolver el mismo 409
  que ya da hoy. Si Fase 5 lo olvida, un intento de cancelar un job ya terminado
  pasa de "409 con mensaje claro" a "500 sin explicación" — regresión real de UX,
  marcarlo explícitamente en el checklist de Fase 5 cuando se escriba.
- **El sync point de `JobRepository` es el punto más frágil de todo el plan en
  términos de coordinación humana** (no de diseño) — si las 2 líneas se
  desincronizan y B edita el archivo antes de que A lo termine, van a pisarse en
  git de verdad, a diferencia del resto de las fases donde eso es estructuralmente
  imposible. Vale la pena que quien ejecute esta fase avise explícitamente cuándo
  A3 está listo, en vez de asumir un tiempo fijo.
- **`JobModel.operation` (columna nueva, A1/A3) — corrección real, más grave de
  lo que una primera pasada de este documento decía.** No es "rompe la suite de
  tests" — es que **`app.main` no arranca en absoluto** contra cualquier DB
  migrada con Alembic real (test o producción), no solo la de los tests.
  `main.py:71` (`job_service.mark_orphaned_jobs_failed()`, código viejo,
  todavía la ruta real) hace `SELECT jobs.*` al arrancar — con la columna en
  el modelo Python pero no en el schema real (sin migración), truena con
  `OperationalError: no such column: jobs.operation` **en el import mismo de
  `app.main`**, antes de levantar nada. Verificado 2 veces: reconstruyendo
  `test.db` desde cero, y con una DB nueva migrada con `alembic upgrade head`
  fuera del entorno de tests — mismo error en los 2 casos. Distinto de
  `DeviceVlanModel`/`DevicePortModel` (Fase 2): esas son tablas **nuevas**,
  nada las consulta todavía, quedan dormidas; `jobs` es una tabla existente
  que código viejo sigue usando de verdad.

  **Excepción puntual a la decisión de "sin migraciones" de este plan,
  justificada por la severidad** (no es un test roto, es que la app no
  levanta): se agregó una migración Alembic mínima, de una sola columna
  (`migrations/versions/g1msp6_jobs_operation.py`, `ALTER TABLE jobs ADD
  COLUMN operation`), encadenada después de la última migración real
  (`f6msp5_rls`). Ninguna otra tabla/columna de este plan necesita esto —
  todas las demás son aditivas sobre código que todavía no las consulta.
  Verificado: `import app.main` corre limpio contra una DB recién migrada con
  Alembic real. `Repository[Job]` en sí, probado contra una DB armada con
  `Base.metadata.create_all()`, ya funcionaba correctamente de punta a
  punta — no había ningún bug de diseño, era puramente el desfasaje de schema,
  ahora cerrado.

  El resto de la suite de tests local sigue sin poder correr — pero por un
  motivo **distinto y ya aceptado**: `tests/conftest.py` tiene fixtures
  `autouse=True` (`reset_rate_limiter`, etc.) que importan `rate_limiter`/
  `device_locks`, borrados en esta misma fase (Línea B, fusionados en
  `RedisCoordinator`). Mismo tipo de "test referencia módulo ya migrado" ya
  aceptado como fuera de alcance en todo este plan — no es nuevo, no bloquea
  nada, los tests no se tocan hasta que se decida rehacerlos de cero.
