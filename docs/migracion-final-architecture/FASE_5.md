# Fase 5 — `RecursoGestionable` + `Orquestador` + `GroupOperationRunner` (Línea A) · `CleanupScheduler` (Línea B)

> Parte de [plan de migración a `FINAL_ARCHITECTURE.md`](README.md). Requiere
> [`FASE_1.md`](FASE_1.md)–[`FASE_4.md`](FASE_4.md) terminadas — Línea A necesita
> `RedisCoordinator` (Fase 1); el sync point de `JobRepository.query()` (Fase 4)
> tiene que estar cerrado.

## Objetivo

**Línea A**: la fase más grande — `RecursoGestionable` formal (protocolo de 4
métodos), `Orquestador` (reemplaza `orchestration_runner.py`, absorbe
`retry_policy.py` como métodos privados), `GroupOperationRunner` (reemplaza el
fan-out real de `vlan_execution_service.py`/`port_execution_service.py`/
`port_config_service.py`). Rewirea `api/vlans.py`, `api/ports.py`, `api/jobs.py`,
`main.py`, `worker.py` para usar las clases nuevas.

**Cambio de comportamiento real, decidido explícitamente, no solo migración
estructural**: el código real despacha **un solo Celery task por grupo**
(`vlan_execution_service.py: enqueue_create_jobs()` → `_group_create_task.delay(...)`
único, que corre los N devices **secuencialmente** dentro del mismo worker,
`run_group_create_job()`, líneas 591-602). Eso contradice lo que
`FINAL_ARCHITECTURE.md` viene describiendo hace muchas rondas (un task de Celery
**por device**, en paralelo — `GroupOperationRunner`/Saga, §2.9, y el `.puml`) y
además significa que el código real **no cumple RNF-PERF-01** tal como está
escrito ("operaciones concurrentes en al menos 50 dispositivos") — 50 devices
secuenciales no son concurrentes. **Decisión tomada**: esta fase construye el
despacho **paralelo real** (1 task por device), cerrando ese hueco — no es una
migración 1:1 del comportamiento actual en este punto puntual, es una mejora
consciente. Ver "Riesgos" para el resto de las implicancias.

**Línea B**: `CleanupScheduler` (reemplaza `cleanup_service.py`) — sin
dependencias de Línea A, se puede hacer en paralelo desde el arranque de esta
fase.

## Estado previo esperado

- Fases 1-4 completas. `JobRepository` (con `query(scope)` ya agregado por Línea B
  en Fase 4) es la referencia — no se vuelve a tocar ese archivo en esta fase salvo
  para inyectarlo como colaborador de `Orquestador`/`GroupOperationRunner`.
- Nadie tocó todavía: `app/services/orchestration_runner.py`,
  `app/services/retry_policy.py`, `app/api/vlans.py`, `app/api/ports.py`,
  `app/api/jobs.py`, `app/main.py`, `app/worker.py`, `app/services/cleanup_service.py`.

---

## Línea A

### A1 — `app/models/recurso_gestionable.py` (archivo nuevo): `RecursoGestionable`

Protocolo formal — hasta ahora `VLAN`/`Puerto` ya implementan estos 4 métodos
(Fase 2) sin una interfaz explícita que lo declare. `Protocol` de `typing` (no
`ABC`) porque no hace falta herencia — `VLAN`/`Puerto` ya cumplen el contrato
estructuralmente, declarar el `Protocol` es documentación con chequeo de tipos, no
un cambio de comportamiento:

```python
from typing import Protocol


class RecursoGestionable(Protocol):
    def validar(self) -> None: ...
    def reconciliar(self, device: "Device") -> dict: ...
    def aplicar(self, device: "Device") -> dict: ...
    def repositorio(self) -> str: ...
```

No requiere cambiar `VLAN`/`Puerto` — un `Protocol` no se hereda, se satisface por
forma. Confirmar con el linter de tipos que ambas clases lo satisfacen (si usa
`mypy`/`pyright`, un `def f(r: RecursoGestionable)` en `Orquestador` ya alcanza
para validarlo estáticamente).

### A2 — `app/core/exceptions.py`: agregar `DeviceExecutionError` si no existe

Usado por `Orquestador.ejecutar()` (A3) para convertir un `resultado.rc != 0` en
excepción — confirmar si ya existe (`app/core/exceptions.py`, usado hoy en
`vlan_service.py`/`port_service.py` según lo visto en fases anteriores) antes de
crear uno nuevo. Si existe, no tocar; si no, agregar con el mismo criterio de
mapeo HTTP que el resto del archivo (500, dado que es una falla de ejecución real
sobre el device, no un error de request del cliente).

### A3 — `app/services/orquestador.py` (archivo nuevo): `Orquestador`

Copia **exacta** del código canónico de `FINAL_ARCHITECTURE.md` §2.4 (ya
corregido en las revisiones previas de este plan — notas (5) a (11) del propio
documento explican cada decisión, leerlas antes de escribir el código):

```python
class Orquestador:
    def __init__(self, device_repo, repos: dict, jobs, eventos, coordinador):
        self._device_repo = device_repo    # Repository[Device] o DeviceRepository, Fase 1/3
        self._repos = repos                # dict[str, Repository] -- "vlan": vlan_repository, "puerto": puerto_repository (Fase 2)
        self._jobs = jobs                  # JobRepository, Fase 4
        self._eventos = eventos            # EventDispatcher, Fase 3
        self._coordinador = coordinador    # RedisCoordinator, Fase 1 -- ver nota abajo, gap encontrado en esta revisión

    def ejecutar(self, recurso: "RecursoGestionable", device_name: str, actor: str, job: "Job") -> None:
        if job.esta_en_estado_terminal():
            return
        device = self._device_repo.get(device_name)
        if device is None:
            raise NotFoundError(device_name)

        pre_state = None
        try:
            job.marcar_iniciado()
            self._jobs.add(job)
            with self._coordinador.bloquear(device_name):
                recurso.validar()
                pre_state = recurso.reconciliar(device)
                resultado, retry_count = self._ejecutar_con_retry(
                    lambda: recurso.aplicar(device), job.job_id, device_name,
                )
                if resultado.get("rc", 0) != 0:
                    raise DeviceExecutionError(resultado.get("stderr") or resultado.get("stdout") or "Execution failed")
        except Exception as error:
            rb_performed, rb_success = (False, None)
            if pre_state is not None:
                with self._coordinador.bloquear(device_name):
                    rb_performed, rb_success = self._rollback(recurso, pre_state, device)
            job.marcar_fallido(str(error), rb_performed, rb_success)
            self._jobs.add(job)
            self._eventos.despachar([DomainEvent(
                "recurso_fallido", recurso, device, actor,
                {"error": str(error), "rollback_performed": rb_performed, "rollback_success": rb_success},
                exitoso=False,
            )])
            raise
        else:
            self._repos[recurso.repositorio()].add(recurso)
            job.marcar_completado(resultado)
            self._jobs.add(job)
            self._eventos.despachar([DomainEvent("recurso_aplicado", recurso, device, actor, resultado)])
        finally:
            if not job.esta_en_estado_terminal():
                job.asegurar_estado_final()
                self._jobs.add(job)
```

**`coordinador: RedisCoordinator` — gap real encontrado en esta revisión, no
estaba en ninguna versión anterior de esta fase.** `reconciliar()`/`aplicar()`
son llamadas de red reales (SSH/Ansible, varios segundos, más lo que sumen los
reintentos) — sin lock, dos ejecuciones concurrentes sobre **el mismo** device
(dos operaciones de grupo distintas, o un reintento superpuesto con otro job)
podían pisarse contra el switch real durante esa ventana. El código real
(`orchestration_runner.py:154`, `with device_locks.acquire(device):`) protege
exactamente esto, envolviendo desde el update a `"running"` hasta el rollback
inclusive — y `RedisCoordinator` (Fase 1) ya existe como reemplazo, pero
versiones anteriores de esta fase lo inyectaban en `GroupOperationRunner` (que
solo encola, no ejecuta nada sobre el device) y nunca en `Orquestador` (donde
sí ocurre la llamada real) — quedaba diseñado y sin usar en ningún lado. Fix:
`coordinador` pasa de `GroupOperationRunner` (A4) a `Orquestador`;
`validar()`/`reconciliar()`/`aplicar()` corren dentro del `with`. El rollback
re-adquiere el lock en un segundo `with` en vez de compartir el primero — así
la protección alcanza también a la compensación (el rollback también llama a
`device.driver`) sin anidar un segundo `try/except` dentro del `with`, y sin
romper el único punto de manejo de falla que ya estableció esta fase. Detalle
aceptado como trade-off: entre soltar el lock de la operación fallida y
volver a tomarlo para el rollback hay una ventana breve donde otra ejecución
podría colarse primero — mucho más chica que no tener lock en absoluto, y el
precio de mantener la estructura de excepción simple. Ver `FINAL_ARCHITECTURE.md`
§2.4 nota (12) para el razonamiento completo. **Esto no es lo mismo que
staleness de `Device`** — `device` se lee una sola vez al principio y nunca se
reescribe en este flujo; el riesgo es sobre el device físico, no sobre la fila
de la DB.

**`_ejecutar_con_retry`/`_clasificar_error` — absorben `retry_policy.py` completo,
como métodos privados + constantes de módulo.** Copiar `_PERMANENT_PATTERNS`/
`_TRANSIENT_PATTERNS` (`retry_policy.py:15-61`) tal cual como constantes de
`app/services/orquestador.py`. `_clasificar_error()` copia
`vlan_execution_service.py: _classify_result()` (líneas 21-33 — el chequeo de
`rc ∈ {4,6,255}` primero, después delega a las tablas de patrones) — no
`retry_policy.py: classify_error()` sola, que solo hace la mitad (string, no rc).
`_ejecutar_con_retry()` copia `_execute_with_retry()` (líneas 76-114) tal cual,
método por método:

```python
    _PATRONES_PERMANENTES = (...)   # retry_policy.py:15-31, verbatim
    _PATRONES_TRANSITORIOS = (...)  # retry_policy.py:33-61, verbatim
    _RC_TRANSITORIOS = frozenset({4, 6, 255})  # vlan_execution_service.py:15
    _MAX_RETRY_DELAY = 5.0

    def _clasificar_error(self, resultado: dict) -> "RetryDecision":
        if resultado.get("rc") in self._RC_TRANSITORIOS:
            return RetryDecision(should_retry=True, classification="transient", reason=f"ansible rc={resultado.get('rc')}")
        combinado = (resultado.get("stderr") or "") + " " + (resultado.get("stdout") or "")
        lowered = combinado.lower()
        for patron in self._PATRONES_PERMANENTES:
            if patron in lowered:
                return RetryDecision(should_retry=False, classification="permanent", reason=patron)
        for patron in self._PATRONES_TRANSITORIOS:
            if patron in lowered:
                return RetryDecision(should_retry=True, classification="transient", reason=patron)
        return RetryDecision(should_retry=False, classification="permanent", reason="unknown error")

    def _ejecutar_con_retry(self, fn, job_id: str, device: str, max_retries: int = 3, retry_base_delay: float = 1.0) -> tuple[dict, int]:
        resultado = {"rc": 1, "stdout": "", "stderr": ""}
        retry_count = 0
        for intento in range(max_retries + 1):
            try:
                resultado = fn()
            except Exception as exc:
                resultado = {"rc": 1, "stdout": "", "stderr": str(exc)}
            if resultado["rc"] == 0:
                break
            decision = self._clasificar_error(resultado)
            if not decision.should_retry or intento >= max_retries:
                break
            delay = min(retry_base_delay * (2 ** intento), self._MAX_RETRY_DELAY)
            retry_count += 1
            time.sleep(delay)
        return resultado, retry_count
```

**`RetryDecision`** (VO) — mueve de `retry_policy.py` a `app/models/retry_decision.py`.
No es una decisión menor abierta — la convención de directorios del `README.md` ya la
resuelve: `app/models/` es exclusivamente para VOs/entidades/Protocols sin acceso a
DB, una clase por archivo, mismo criterio que `VisibilityScope`/`DomainEvent`.

**`_rollback(recurso, pre_state, device)`** — copia el patrón real de
`vlan_execution_service.py: _rollback_create/_rollback_delete/_rollback_update`
(y los 7 equivalentes de puerto) **unificado**: llama a
`device.driver.<operación inversa>` según lo que `pre_state`/`recurso` indiquen,
**nunca propaga** (cada rama con su propio `try/except`, retorna
`(rollback_performed, rollback_success)`), verifica el resultado contra el device
después de intentar revertir — mismo contrato ya documentado en
`FINAL_ARCHITECTURE.md` §2.4 nota (9). Como VLAN/Puerto ya saben "cómo se
aplican" (Fase 2), `_rollback()` necesita saber "cómo se revierte" — que **no**
está en `RecursoGestionable` (revertir no es parte del contrato de aplicación, es
responsabilidad de la Saga, no del recurso) — mismo criterio de separación que
ya usa el documento para no mezclar "aplicar" con "compensar".

**Borrar `orchestration_runner.py` y `retry_policy.py`** una vez que `Orquestador`
los reemplace y ningún caller real los importe más.

### A4 — `app/services/group_operation_runner.py` (archivo nuevo): `GroupOperationRunner`

**Dispatch paralelo real (decisión de esta fase, ver el aviso al principio del
documento)** — un Celery task **por device**, no uno por grupo.

**`coordinador` — no vive acá.** Una versión anterior de esta fase lo
inyectaba en `GroupOperationRunner`, pero `encolar()` es síncrono y solo
encola tareas — no ejecuta nada sobre el device, así que sostener el lock acá
no protege nada real (la ejecución ocurre después, en otro proceso). Movido a
`Orquestador` (A3), la única clase que efectivamente llama a
`device.driver`. Ver la nota en A3 y `FINAL_ARCHITECTURE.md` §2.4 nota (12).

```python
class GroupOperationRunner:
    def __init__(self, orquestador, job_queue, jobs):
        self._orquestador = orquestador
        self._job_queue = job_queue      # ver A5 -- wrapper sobre Celery
        self._jobs = jobs                # JobRepository

    def encolar(self, recurso: "RecursoGestionable", devices: list[str], actor: str) -> tuple[str, list[dict]]:
        import uuid
        from dataclasses import asdict
        group_job_id = str(uuid.uuid4())  # sin fila, sin Repository[GroupJob] -- §2.9
        parametros = asdict(recurso)  # ver nota abajo -- Job.parameters, no estaba poblado
        job_entries = []
        for device_name in devices:
            job = Job(
                operation=recurso.repositorio(), device=device_name,
                group_job_id=group_job_id, parameters=parametros,
            )
            self._jobs.add(job)
            self._job_queue.dispatch("orquestador.ejecutar", recurso, device_name, actor, job.job_id)
            job_entries.append({"device": device_name, "job_id": job.job_id, "status": job.status})
        return group_job_id, job_entries
```

**`Job.parameters` — encontrado revisando `frontend/src/components/JobDetailModal.tsx`
real, no una versión anterior de esta fase.** El modal deriva el título de un
grupo desde `groupJob.parameters?.vlan_id` (línea real: `const vlanId =
params?.vlan_id`) — sin este campo poblado, el título cae a un genérico
("Create vlan" en vez de "Create vlan 100"), degradado pero no roto (el
componente ya maneja `parameters == null` con gracia). Igual es información
real que el sistema actual muestra y este diseño perdía en silencio.
`asdict(recurso)` es genérico a propósito — `GroupOperationRunner` no sabe qué
es una VLAN (mismo principio que el resto del catálogo), así que no arma un
dict curado por tipo (`{"vlan_id":..., "name":...}`, como hace el real
`job_service.create_job()`) — vuelca todos los campos del `recurso`, que ya
incluye `vlan_id` para `VLAN` e `interface` para `Puerto`, suficiente para lo
que el frontend lee hoy.

**Firma corregida — encontrado comparando contra el frontend real
(`frontend/src/types/vlan.ts: VlanOperationResult`, `port.ts:
PortOperationResult`), no estaba en ninguna versión anterior de esta fase.**
Una versión anterior de `encolar()` devolvía solo `group_job_id: str` — pero
tanto `POST /vlans` como los `POST`/`PATCH` de puerto devuelven hoy
`{"group_job_id": ..., "jobs": [{"device":..., "job_id":...}]}` (real,
`vlan_execution_service.py: enqueue_create_jobs()` retorna `(job_entries,
group_job_id)`, `api/vlans.py: create_vlan()` arma la respuesta con los 2). El
frontend depende de ese array `jobs` para trackear cada job individual apenas
se encola, antes de que el grupo termine — perderlo rompe esa pantalla, no es
un detalle cosmético. Fix: `encolar()` devuelve la tupla completa, no solo el id.

**`recurso` cruza la cola serializado** (mismo motivo que `FINAL_ARCHITECTURE.md`
§2.4 nota (2)) — Celery serializa a JSON (`task_serializer="json"`, ya configurado
en `worker.py:13`), así que `VLAN`/`Puerto` necesitan ser serializables a dict/JSON
de forma directa (ya lo son, son dataclasses planas — confirmar que ningún campo
nuevo de Fase 2 rompe esto, ej. `Puerto` no debería tener nada no serializable).

### A5 — Celery: `app/tasks.py` (archivo nuevo) + `worker.py`

**El Celery task por device, un solo nombre genérico para cualquier
`RecursoGestionable`** — no uno por tipo de operación como hoy. Contados por
grep, son **11** nombres reales, no una aproximación:
`ansiauth.vlan.group_create`/`group_delete`/`group_update`/`save`
(`vlan_execution_service.py:635-652`) +
`ansiauth.port.update_description`/`set_admin_state`/`set_access_vlan`/
`set_trunk_allowed_vlans` (`port_execution_service.py:365-390`) +
`ansiauth.port.configure_port`/`shutdown_port`/`enable_port`
(`port_config_service.py:910-928`). Ninguno se referencia por nombre-string
desde otro lado (`send_task()`/`apply_async()` sueltos — verificado con grep,
0 resultados), así que no hay ruptura externa al sacarlos — pasan a ser **uno
solo** (`Orquestador` no sabe qué VLAN/Puerto es, tampoco debería hacer falta
un task de Celery por tipo):

```python
from app.worker import celery_app


@celery_app.task(name="ansiauth.orquestador.ejecutar")
def ejecutar_task(recurso_dict: dict, tipo_recurso: str, device_name: str, actor: str, job_id: str) -> None:
    from app.composition import orquestador, job_repository
    from app.models.vlan import VLAN
    from app.models.port import Puerto

    _TIPOS = {"vlan": VLAN, "puerto": Puerto}
    recurso = _TIPOS[tipo_recurso](**recurso_dict)
    job = job_repository.get(job_id)
    orquestador.ejecutar(recurso, device_name, actor, job)
```

**`tipo_recurso` — dato real que hace falta y que el diseño no había cerrado.**
`RecursoGestionable` es un protocolo, no una clase — al deserializar del lado del
worker no hay forma de saber si `recurso_dict` es un `VLAN` o un `Puerto` sin que
alguien lo diga explícito. `GroupOperationRunner.encolar()` (A4) tiene que pasar
`recurso.repositorio()` (ya existe, "vlan"/"puerto") junto con los datos —
`_job_queue.dispatch()` (el wrapper sobre `.delay()`) arma la llamada real:

```python
class JobQueue:  # wrapper delgado sobre Celery -- FINAL_ARCHITECTURE.md ya lo lista como interfaz
    def dispatch(self, nombre_task: str, recurso, device_name, actor, job_id) -> None:
        from dataclasses import asdict
        from app.tasks import ejecutar_task
        ejecutar_task.delay(asdict(recurso), recurso.repositorio(), device_name, actor, job_id)
```

`worker.py`: cambiar `include=[...]` de los 3 módulos viejos
(`vlan_execution_service`, `port_execution_service`, `port_config_service`) a
`["app.tasks"]` — un solo módulo con un solo task, en vez de 3 módulos con ~7
tasks distintos entre `group_create`/`group_delete`/`group_update`/`save` (VLAN) +
los equivalentes de puerto.

### A6 — Rewirear `api/vlans.py`

```python
@router.post("/", ...)
def create_vlan(vlan: VLANCreate, current_user: dict = Depends(require_scope("write_device_config"))):
    from app.composition import group_operation_runner
    entidad = VLAN(vlan_id=vlan.vlan_id, name=vlan.name)  # __post_init__ valida vlan_id
    group_job_id, jobs = group_operation_runner.encolar(entidad, vlan.devices, current_user["username"])
    return {"success": True, "group_job_id": group_job_id, "jobs": jobs}  # "jobs" -- ver A4, VlanOperationResult real
```

**`vlan_validator.validate_vlan_id_range()`/`validate_vlan_not_reserved()` a mano
en el router (real, `api/vlans.py:115-119`) se borran** — `VLAN.__post_init__` ya
las corre (Fase 2), duplicarlas acá es exactamente la duplicación que motivó
absorber el validator dentro de `VLAN` en primer lugar. `device_service.get_device()`
loop de existencia (`api/vlans.py:120-122`) — evaluar si se mantiene (falla rápido,
antes de encolar nada) o se deja que `Orquestador.ejecutar()` lo resuelva por
device vía `NotFoundError` (A3, ya lo hace) — si se mantiene, usar
`device_repository.get(name)` (Fase 3), no `device_service` (que ya no es la
fuente de verdad). Mismo patrón para `delete_vlan()`/`update_vlan()` —
`VLAN(vlan_id=..., name=..., eliminar=True)` para el delete (Fase 2, A1).

`get_vlans()` (lectura en vivo, GET) — pasa a `device.driver.get_vlans(device,
device.password)` directo (`FINAL_ARCHITECTURE.md` §4, ya documentado), inyectando
`RedisCoordinator` en vez de `device_locks.acquire()` directo (4 lugares reales
documentados: `api/vlans.py:63,84`, `api/ports.py:99-111,209`).

### A7 — Rewirear `api/ports.py`, mismo patrón que A6

`Puerto(interface=..., device=X, <campos según el endpoint>)` — cada uno de los 7
endpoints reales sigue existiendo como ruta HTTP distinta (RF-PUERTO-01..10 no
cambia), pero construye un `Puerto` con **solo el campo que le corresponde
seteado** (el resto `None`) — `mutation_fields` (Fase 2) hace el resto. El
endpoint "Configure port (composite)" sí puede setear varios campos a la vez.
Los 7 endpoints de escritura devuelven `{"group_job_id":..., "jobs":...}` —
mismo fix que A6 (`encolar()` ahora devuelve la tupla).

**`_require_port_driver_with()` (líneas 124-153) — encontrado implementando
Fase 1/A2, ver `FASE_1.md` nota en "Callers reales que quedan rotos".** Gatea
los 7 endpoints de escritura con un chequeo de introspección contra
`BasePortDriver` (`getattr(type(driver), method_name) is
getattr(BasePortDriver, method_name)`, para devolver 501
`VENDOR_NOT_SUPPORTED` si el driver no sobreescribió el método). `BasePortDriver`
ya no existe desde Fase 1 (A2, fusionada en `VendorDriver`) — el `getattr()`
tiene que compararse contra `VendorDriver`, no solo actualizar el import de
`dispatcher.get_port_driver` por `PluginRegistry.obtener(...)`. Si se migra
uno sin el otro, el chequeo compara contra un símbolo que no existe más.

**`GET /ports?device=X` necesita traducir `interface` → `name` — no es
automático, encontrado comparando contra el schema real.**
`app/schemas/port.py: PortRead` (real, sin tocar en este plan) declara
`name: str` para el campo que `Puerto` (Fase 2) llama `interface` — la
unificación de Fase 2 tomó el nombre del lado de escritura a propósito (los 7
schemas de request reales, `PortDescriptionUpdateRequest` y equivalentes,
**todos** usan `interface` — confirmado, la decisión fue correcta para
escritura), pero eso deja un mismatch real con `PortRead` del lado de lectura.
El router arma la respuesta explícito, no delega en un `to_dict()` genérico:

```python
@router.get("/", ...)
def get_ports(device: str, current_user=Depends(require_authenticated)):
    puertos = device.driver.list_ports(device, device.password)  # list[Puerto], Fase 2
    return {"success": True, "data": {
        "device": device.name, "vendor": device.vendor, "count": len(puertos),
        "ports": [
            PortRead(
                name=p.interface, description=p.description, admin_up=p.admin_up,
                operational_up=p.operational_up, mode=p.mode, access_vlan=p.access_vlan,
                allowed_vlans=p.allowed_vlans, poe_enabled=p.poe_enabled,
                speed=p.speed, duplex=p.duplex,
            ).model_dump()
            for p in puertos
        ],
    }}
```

### A8 — Rewirear `api/jobs.py`

`cancel_job()` — el real no lanza si el job ya terminó (no-op silencioso). Con
`Job.cancelar()` lanzando `TransicionInvalidaError` (Fase 4), el endpoint tiene que
atraparla:

```python
@router.post("/{job_id}/cancel", ...)
def cancel_job(job_id: str, current_user=Depends(require_authenticated)):
    from app.composition import job_repository
    job = job_repository.get(job_id)
    if job is None:
        raise NotFoundError(f"Job '{job_id}' not found")
    try:
        job.cancelar()
    except TransicionInvalidaError:
        raise HTTPException(status_code=409, detail=f"Cannot cancel job with status '{job.status}'")
    job_repository.add(job)
    return {"success": True, "data": {"job_id": job.job_id, "status": job.status}}
```

`list_jobs()` — pasa a `job_repository.query()` (Fase 4, ya corregida con
`device`/`site_id` como filtros propios) en vez de `job_service.query_jobs()` +
`Inventory()._visible_device_names()` armado a mano (real, `api/jobs.py:88-93`):

```python
@router.get("/", ...)
def list_jobs(current_user=Depends(require_authenticated), scope=Depends(obtener_scope),
               status=None, device_id=None, site_id=None, from_date=None, to_date=None,
               page: int = 1, page_size: int = 50):
    from app.composition import job_repository
    jobs, total = job_repository.query(
        status=status, device=device_id, site_id=site_id,
        from_date=from_date, to_date=to_date, scope=scope, page=page, page_size=page_size,
    )
    return {"success": True, "total": total, "page": page, "page_size": page_size,
            "items": [_format_job(j) for j in jobs]}
```

**Forma de la respuesta — verificada contra el cliente real, no asumida.**
`frontend/src/services/api.ts: getJobs()` solo lee `.items`/`.total` del body
(`client.get<{ items: Job[]; total: number }>(...)`) — los campos `success`/
`page`/`page_size` que el real también manda no le importan al cliente, pero
tampoco molestan con estar (TS estructural, extra keys se ignoran) — se
mantienen igual que hoy por continuidad con el resto de los endpoints, no
porque el frontend los necesite.

### A9 — `main.py`/composición final

- `job_service.mark_orphaned_jobs_failed()` (línea 71 real) → `job_repository.recuperar_huerfanos()`.
- `app.composition.plugin_registry`/etc. — confirmar que `build_plugin_registry()`
  (Fase 1) se llama en la importación de `composition.py`, no hace falta
  llamarla de nuevo acá.
- Agregar a `app/composition.py`: `orquestador = Orquestador(device_repository,
  {"vlan": vlan_repository, "puerto": puerto_repository}, job_repository,
  event_dispatcher, redis_coordinator)`, `event_dispatcher = EventDispatcher()` +
  `event_dispatcher.suscribir(AuditListener(audit_repository))`,
  `group_operation_runner = GroupOperationRunner(orquestador, JobQueue(), job_repository)`
  — `redis_coordinator` va a `Orquestador`, no a `GroupOperationRunner` (ver A3/A4).

### Callers reales que quedan rotos/muertos al terminar esta fase (Línea A)

`vlan_service.py`, `port_service.py`, `vlan_execution_service.py`,
`port_execution_service.py`, `port_config_service.py`, `job_service.py`,
`audit_service.py` (la parte de escritura, `log_action`/`append_audit_event` —
`get_audit_log`/`count_audit_log` los reemplaza `AuditRepository.query()`/`count()`
de Fase 3) quedan **sin ningún caller real** después de A6-A9 — no se borran en
esta fase (eso es Fase 6), pero ya no los ejecuta ningún camino real del sistema.

---

## Línea B

### B1 — `app/services/cleanup_scheduler.py` (archivo nuevo): `CleanupScheduler`

```python
class CleanupScheduler:
    def __init__(self, login_attempt_repo):
        self._login_attempts = login_attempt_repo  # LoginAttemptRepository, Fase 3

    def purgar_artefactos(self, dias: int = 30) -> int:
        # copiar purge_old_artifacts() (cleanup_service.py:21-51) tal cual --
        # filesystem, sin DB, sin dependencia de ningún Repository
        ...

    def limpiar_refresh_tokens(self) -> int:
        # copiar sweep_expired_refresh_tokens() (líneas 54-69) tal cual --
        # ver nota abajo, no pasa por un Repository[RefreshToken]
        ...

    def limpiar_intentos_login(self, dias: int = 7) -> int:
        return self._login_attempts.purgar_antiguos(dias)  # nuevo método, ver nota

    def ejecutar_todo(self, artifact_retention_days=30, login_attempt_retention_days=7) -> dict:
        # copiar run_all() (líneas 94-115) tal cual -- try/except por rutina
        ...
```

**`limpiar_intentos_login()` delega a `LoginAttemptRepository`, no toca
`LoginAttemptModel` directo** — mismo principio ya discutido en Fase 3: la clase
`*Repository` es la única dueña del acceso a esa tabla, aunque no herede de
`Repository[T]`. Agregar `purgar_antiguos(dias)` a `LoginAttemptRepository`
(Fase 3, archivo ya existente) — copia `sweep_old_login_attempts()`
(`cleanup_service.py:72-91`) tal cual. **Esto es una edición sobre un archivo que
Línea B ya creó en Fase 3** (`login_attempt_repository.py`) — no hay conflicto
con Línea A, pero sí hay que recordar tocar 2 archivos en esta fase, no solo
`cleanup_scheduler.py`.

**`limpiar_refresh_tokens()` no pasa por ningún Repository — riesgo/decisión
menor, documentada, no resuelta a fondo.** No existe `Repository[RefreshToken]`
en ningún punto de este plan (`refresh_token_service.py`/`AutenticacionService`
están fuera de alcance — ninguna fase los construye). Construir un
`Repository[RefreshToken]` completo solo para este método sería alcance nuevo, no
migración de algo que este plan ya cubre. Se deja `limpiar_refresh_tokens()` con
su propio `get_session()` directo, igual que hoy — una excepción puntual y
documentada al principio de "el Repository es el único dueño del acceso a la
DB", aceptada porque construir la pieza que faltaría es trabajo fuera del
alcance ya acordado (RefreshToken/AutenticacionService).

**Borrar `cleanup_service.py`** una vez que `main.py`/`_lifespan()`/`_make_scheduler()`
(los 2 lugares reales que lo llaman, `main.py:131-190`) usen `CleanupScheduler`
en vez de las funciones sueltas.

---

## Dependencias cruzadas

- Ninguna entre A y B en esta fase — `CleanupScheduler` (B) no depende de
  `Orquestador`/`GroupOperationRunner` (A) para nada, puede escribirse en
  paralelo desde el día 1 de esta fase.
- A9 (composición final en `main.py`) es el punto donde **todo** lo construido en
  las 5 fases converge — depende de que A1-A8 estén terminados, pero es trabajo
  interno de Línea A, sin cruce con B (B solo agrega sus propias 2-3 líneas a
  `composition.py` para `cleanup_scheduler`).

## Criterio de finalización

- [ ] `RecursoGestionable` (Protocol) existe. `VLAN`/`Puerto` lo satisfacen sin
      cambios de código (confirmar con el linter de tipos si el proyecto usa uno).
- [ ] `Orquestador` existe con la forma exacta del código canónico de
      `FINAL_ARCHITECTURE.md` §2.4 (try/except/else/finally completo, no solo
      try/except alrededor de `aplicar()`).
- [ ] `_ejecutar_con_retry()`/`_clasificar_error()` absorben `retry_policy.py`
      completo — el archivo no existe más.
- [ ] `GroupOperationRunner.encolar()` despacha **un Celery task por device**
      (confirmado — no uno por grupo, ver el aviso de esta fase).
- [ ] `app/tasks.py` tiene un único task genérico (`ansiauth.orquestador.ejecutar`),
      no los 11 anteriores. `worker.py: include=["app.tasks"]`.
- [ ] `api/vlans.py`/`api/ports.py` construyen `VLAN`/`Puerto` y llaman
      `GroupOperationRunner.encolar()` — no importan `vlan_service`/`port_service`.
- [ ] `GroupOperationRunner.encolar()` devuelve `(group_job_id, jobs)` — los
      endpoints de escritura de VLAN/Puerto responden con `"jobs"` en el body,
      confirmado contra `frontend/src/types/vlan.ts`/`port.ts`.
- [ ] Cada `Job` que crea `encolar()` tiene `parameters=asdict(recurso)` — no
      queda en `None`, confirmado contra `JobDetailModal.tsx` real
      (`groupJob.parameters?.vlan_id` para el título del modal de grupo).
- [ ] `GET /ports` traduce `Puerto.interface` → `PortRead.name` explícito en el
      router — confirmado contra `app/schemas/port.py: PortRead` real.
- [ ] `api/jobs.py: cancel_job()` atrapa `TransicionInvalidaError` → 409.
      `list_jobs()` usa `JobRepository.query(scope)`.
- [ ] `main.py` usa `job_repository.recuperar_huerfanos()`, no
      `job_service.mark_orphaned_jobs_failed()`.
- [ ] `CleanupScheduler` existe, `LoginAttemptRepository.purgar_antiguos()`
      agregado. `cleanup_service.py` sin caller real.

- [ ] `python -c "import app.composition"` y `python -c "import app.main"` corren sin error (ver `FASE_7.md` sección 5).

## Riesgos / cosas a validar

- **`Job.playbook` queda en `None` con este diseño — degradación aceptada, no
  un bug a arreglar en esta fase.** Hoy cada `Job` guarda un nombre de archivo
  real (`"create_vlan.yml"`) porque `job_service.create_job()` lo recibe
  explícito de cada función de `vlan_execution_service.py`. En el diseño
  nuevo, qué playbook de Ansible corre es una decisión **interna** de
  `HuaweiVendor`/`CiscoVendor` (Fase 1) — `Orquestador`/`GroupOperationRunner`
  nunca lo saben, ni deberían (mismo principio de "no sabe qué es una VLAN").
  `frontend/src/components/JobDetailModal.tsx` ya maneja `job.playbook == null`
  con gracia (`[job.playbook, job.device].filter(Boolean).join(' — ')`, el
  campo `Field` muestra "—") — no rompe, solo se ve menos informativo que hoy.
  Si en algún momento se decide que vale la pena recuperarlo, haría falta que
  `VendorDriver` devuelva el nombre del playbook usado como parte del
  `resultado` (mismo patrón que `"accion"`, Fase 2) — no es parte de esta fase.
- **El cambio a dispatch paralelo (1 task/device) es una mejora real de
  comportamiento, no solo estructura — probarlo bajo carga antes de confiar en
  que resuelve RNF-PERF-01 de verdad.** Corre N tasks de Celery en paralelo real
  ahora depende de cuántos workers Celery haya configurados — con 1 solo worker
  concurrente, N tasks en la misma cola siguen ejecutándose uno por vez (la cola
  los serializa igual, aunque estén "despachados" independientemente). El
  paralelismo real requiere `--concurrency` > 1 en el worker, o varios
  procesos worker — confirmar la config de despliegue real antes de dar por
  cerrado el RNF, encolar más rápido no es lo mismo que ejecutar en paralelo.
- **`RedisCoordinator.limitar()` (rate limit por device) ahora sí importa de
  verdad** — con dispatch secuencial (código actual) nunca había 2 operaciones
  simultáneas sobre el mismo device dentro de un mismo grupo. Con dispatch
  paralelo, si el mismo device aparece en 2 requests de grupo distintas al mismo
  tiempo (2 usuarios, o reintento superpuesto), `RedisCoordinator.bloquear()`
  pasa a ser lo único que evita 2 workers pisándose en el mismo device.
  **Corrección sobre una versión anterior de esta fila**: decía "ya estaba
  diseñado para esto (Fase 1)" dando a entender que ya estaba conectado —
  no era cierto, `bloquear()` estaba inyectado en `GroupOperationRunner`
  (que no ejecuta nada sobre el device) y nunca en `Orquestador` (donde sí
  ocurre la llamada real) — quedaba sin usarse en ningún flujo. Corregido en
  A3/A4 de esta misma fase (ver la nota ahí y `FINAL_ARCHITECTURE.md` §2.4
  nota (12)); esta fila es exactamente el escenario que ese fix cierra.
- **`limpiar_refresh_tokens()` sin `Repository[RefreshToken]`** — aceptado como
  alcance parcial, ver B1. Si una fase futura (fuera de este plan) construye
  `AutenticacionService`/`Repository[RefreshToken]`, revisar este método
  entonces, no antes.
