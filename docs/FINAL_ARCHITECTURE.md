# Arquitectura Final — Clases, Patrones, Mapeo de Código, Flujos

> Síntesis de A (Hexagonal/DDD) + B (ciclo de vida uniforme, sin herencia profunda)
> + C (eventos de dominio) + E (plugins de vendor), con reducción de clases por
> configuración en vez de subclasificación. Reemplaza/consolida
> `TARGET_ARCHITECTURE.md`.

---

## 1. Catálogo completo de clases (~52, tras la 2da poda + tabla de transiciones/Saga + hallazgos de revisión — ver §1.5, §2.8/2.9 y el final del documento)

### Dominio — Entidades (tienen identidad: se buscan por ID, siguen "siendo la
misma" aunque cambien de valor — todas implementan `RecursoGestionable` salvo
`Device`/`Site`/`DeviceGroup`/`Usuario`/`Job`/`RoleAssignment`, que no se
"aplican" a un device, se persisten y ya)

**Corrección**: `GroupJob` (entidad) y `DeviceExecution` (VO) se eliminan del
catálogo — ver `Job` abajo y `JobRepository.resumen_de_grupo()` en
Infraestructura. No hacía falta persistir un "grupo" aparte.

| Clase | Identidad | Reemplaza |
|---|---|---|
| `Device` | nombre | ya existe |
| `Site` | id | `site_service.py` |
| `DeviceGroup` | id | `device_group_service.py` |
| `Usuario` | id/username | `user_service.py` |
| `Job` | id | `job_service.py` |
| `RoleAssignment` | id | `role_assignment_service.py` (parte dominio) |
| `VLAN` | vlan_id + device | ya existe — **corregido**: tiene identidad (vlan_id), RF-VLAN-03 le edita el nombre y sigue siendo la misma VLAN. No es Value Object, aunque se autovalide igual que uno |
| `Puerto` | interface + device | `models/port.py` (`PortInfo`+`PortConfigRequest`+`PortConfigResult`) — mismo error de clasificación que VLAN, corregido |
| `InterfazVirtual` | id + device | RF-INTERV — nueva |
| `ConfiguracionGlobal` | device (1 a 1) | RF-GLOBAL — nueva, singleton por device |

### Validación de transiciones de `Job` (tabla, §2.8 — corregido de State pattern a esto)

`_TRANSICIONES_VALIDAS` (dict de módulo) + `Job._transicionar()` — reemplaza
los `if status in (...)` ad-hoc de `job_service.py`, centralizando qué
transiciones son válidas en un solo lugar. Sin código actual equivalente
completo — hoy es lógica implícita repartida en `update_job()`/`cancel_job()`.

### Dominio — Value Objects (sin identidad propia, viven *dentro* de una Entidad)

| Clase | Vive dentro de | Reemplaza |
|---|---|---|
| `AuditRecord` | — (write-once, nunca se edita) | ya existe |
| `VisibilityScope` | — (dato de retorno, no vive dentro de nadie) | `site_service.py`/`device_group_service.py`/`inventory_service.py`/`audit_service.py` **+ `effective_role.py`**, ver §1.6/§6.1 — nueva, unifica 5 lugares que preguntaban "qué puede ver/hacer este usuario" con su propia query cada uno |
| `RetryDecision` | — (dato de retorno de `Orquestador._clasificar_error()`) | `retry_policy.py` — **ya existe** como `@dataclass`, nunca estuvo en este catálogo a pesar de ser exactamente el mismo tipo de VO que `AuditRecord` |

`ConfiguracionIP`/`AclRule`/`DhcpRelayConfig` (antes VOs de `InterfazVirtual`) y
`SnmpConfig`/`RutaEstatica`/`ServidoresConfig` (antes VOs de `ConfiguracionGlobal`) —
**podados (§1.5)**: pasan a ser campos propios de `InterfazVirtual`/
`ConfiguracionGlobal`, validados en el `validar()` de la entidad dueña. No tenían
validación propia compleja que justificara una clase aparte.
`AccessToken`/`RefreshToken` — **podados**: sin comportamiento propio (ni un
método), se vuelven un `dict`/tupla simple que devuelve `JwtTokenIssuer`.

### Dominio — Eventos

`DomainEvent(tipo: str, recurso, device, actor: str, payload: dict, exitoso: bool = True)`
— **1 sola clase**
(podado de 3: `RecursoAplicado`/`AplicacionFallida`/`JobCancelado` eran la misma
forma con distinto `tipo`, mismo criterio que `Repository[T]`). **Corrección real,
encontrada armando el plan de migración**: la firma original de esta sección no
tenía `actor` — pero `AuditListener` (§1.6) necesita saber quién hizo la acción para
completar `AuditRecord.user` (campo obligatorio, no opcional), y los 4 llamados
reales de `Inventory` (`register()`/`move()`×2/`deregister()`, más abajo) ya
intentaban pasarlo, **incorrectamente, en el lugar del parámetro `device`**
(`DomainEvent("device_registrado", device, actor, {...})` — `actor` terminaba en el
slot de `device`, y ningún `device` real se pasaba). Se agrega `actor` como
parámetro propio; los 7 call sites reales de este documento (`Inventory`×4,
`Orquestador`×2, además de los ejemplos de §4/§5) se corrigen para pasarlo
explícito, ver cada uno abajo. **Segunda corrección, mismo motivo**: se agrega
`exitoso: bool = True` — sin esto, `AuditListener` tendría que inferir éxito/falla
comparando `tipo.endswith("_fallido")` o buscando una clave `"error"` en `payload`,
un heurístico de string frágil para algo que `Orquestador` ya sabe con certeza en el
momento de despachar. Default `True` porque la mayoría de los eventos reales son de
éxito (`"recurso_aplicado"`, los 3 de `Inventory`) — el único que lo pasa explícito
es `"recurso_fallido"` (`exitoso=False`).

### Dominio — Interfaces (ports, sin implementación)

`Repository[T]` (genérico), `AutomationEngine`, `JobQueue`, `EventListener`,
`RecursoGestionable`, **`VendorDriver`** (podado de 4: `BaseVendorDriver`+
`BasePortDriver`+`BaseInterfazDriver`+`BaseConfigDriver` se funden en una — ningún
vendor implementa "solo una", `HuaweiVendor` siempre las 4 juntas).

### Aplicación — orquestación

| Clase | Reemplaza | Colaboradores inyectados |
|---|---|---|
| `Orquestador` | `orchestration_runner.py` | `Repository[Device]`, `dict[str, Repository]` (por recurso), `JobRepository` (nuevo — **corrección de esta revisión**, ver §2.4: sin esto, `job.marcar_completado()`/`marcar_fallido()` mutan el `Job` en memoria pero nadie lo persiste), `EventDispatcher`, `RedisCoordinator` (nuevo — **corrección de esta revisión**, ver §2.4 nota (12): sin esto, nada impide que 2 ejecuciones concurrentes sobre el mismo device pisen sus llamadas al driver durante los varios segundos que dura `reconciliar()`/`aplicar()`) — no `AutomationEngine`/`PluginRegistry`, eso lo resuelve `Device` internamente |
| `GroupOperationRunner` | fan-out de `vlan_execution_service.py`/`port_execution_service.py` | `Orquestador`, `JobQueue`, `JobRepository` — **no** `RedisCoordinator` (movido a `Orquestador`, ver §2.4 nota (12): `encolar()` solo encola, el lock tiene que sostenerse durante la ejecución real, que pasa en otro proceso), **no** `Repository[GroupJob]`, no existe (ver Job/§2.9) |
| `PluginRegistry` | `vendors/dispatcher.py` | — (registro mutable) |
| `EventDispatcher` | llamadas directas a `audit_service` hoy | lista de `EventListener` |
| `AutenticacionService` | `auth_service.py` | `JwtTokenIssuer`, `PasswordHasher`, `LoginAttemptRepository`, `Repository[RefreshToken]` |
| `Inventory` | **ya existe** (MSP Fase 3) — coordina `Site`/`DeviceGroup`/`Device`, `list()` ya filtra por rol del usuario sobre el Site. Ver §1.6 cómo quedaría alineada a los patrones del resto | `Repository[Device]`, `Repository[Site]`, `Repository[DeviceGroup]`, `RoleAssignmentRepository`, `JobRepository`, `EventDispatcher` |
| `RoleAssignmentService` | **ya existe** (`role_assignment_service.py`, MSP Fase 3) — única dueña de `role_assignments` y `users.is_system_admin`. El catálogo original (más abajo, entidad `RoleAssignment`) decía por error que esto se disolvía en `RoleAssignmentRepository`+`AutenticacionService` — se revierte, ver §1.6 | `RoleAssignmentRepository`, `Repository[Usuario]` |

**`effective_role(...)` se absorbe en `VisibilityScope`** — corrección más
grande que un cambio de firma, y **más grande de lo que se documentó
inicialmente**: `effective_role()` no se llama solo desde `require_scope()` —
se llama **directo, inline, en 15+ lugares de 8 archivos** (`api/jobs.py`,
`api/sites.py` ×2, `api/device_groups.py` ×3, `api/vlans.py`, `api/ports.py`,
`api/devices.py` ×4, `core/scope.py` ×3 — verificado por grep sobre el código
real), cada uno haciendo su propia query a `RoleAssignmentModel` cada vez que
se llama. Por separado, `scope_de()` hacía otra query para la visibilidad en
lote — para la misma pregunta de fondo ("¿qué puede hacer este usuario?").

Se unifican: la `VisibilityScope` se calcula **una sola vez por request**
(`scope: VisibilityScope = Depends(obtener_scope)`, agregado a la firma de
cada uno de esos 8 archivos, no solo a `require_scope()`) y carga los grants
ya resueltos; el matching "más específico gana" que hacía `effective_role()`
pasa a ser `scope.rol_para(site_id, group_id)`, en memoria, sin tocar la DB de
nuevo. Los 15+ call sites pasan de `effective_role(session, user, tipo, id)` a
`scope.rol_para(...)` — mismo resultado, sin la query repetida.

`_resolve_scope()` (resolución de "a qué site/group pertenece este recurso
puntual" — depende de *qué* recurso se pide en cada request, no se puede
precalcular) **sí sigue como función en `effective_role.py`**, a diferencia de
lo que pasó con `retry_policy.py` — ahí tenía sentido absorberla en
`Orquestador` porque un solo caller la usaba; acá la van a llamar los mismos
8 archivos de arriba, cada uno necesita resolver su propio recurso antes de
consultar el `VisibilityScope` — es reuso real entre módulos que no se
conocen entre sí, no una excusa para no tocar una clase.

### Infraestructura — persistencia

| Clase | Reemplaza |
|---|---|
| `Repository[T]` (una sola, genérica — `orm_model`/`to_domain` por constructor) | los `_to_domain`+sesión+query repetidos en los 8+ servicios CRUD, y los "12 Repository" que B proponía como subclases |
| `AuditRepository` | subclase — `query()` con filtros más ricos que CRUD genérico |
| `RoleAssignmentRepository` | subclase — `scope_de(user_id)`, ahora también reemplaza a `effective_role()`, ver §1.6/§6.1 |
| `LoginAttemptRepository` | subclase — `esta_bloqueado`/`ip_bloqueada` necesitan contar intentos en una ventana de tiempo, `Repository[T]` genérico no alcanza |
| `JobRepository` | subclase — `query()` con los mismos filtros combinados que `query_jobs()` ya tiene hoy, ver §1.6 |
| `DeviceRepository` | subclase — `nombres_visibles(scope)`, el JOIN device↔group↔site que `AuditRepository`/`JobRepository` necesitan y `VisibilityScope` sola no puede responder (encontrado revisando mi propio rediseño de esas dos) |

**¿Cuántas más van a aparecer?** Ver §2.2.1 — son 5 subclases sobre 12 entidades, cada
una con un criterio explícito, no una tendencia abierta.

### Infraestructura — vendors (consolidado, sin clase Plugin separada ni parsers aparte)

`HuaweiVendor`, `CiscoVendor`, `MockVendor` — cada una implementa `VendorDriver`
(la interfaz única, podada de 4) + parseo como métodos privados. **Trabajo real
pendiente, no ya hecho** — ver nota en §1.6: hoy son 6 clases separadas
(`HuaweiVlanDriver`+`HuaweiPortDriver`, etc.), no 3.

### Infraestructura — seguridad

`JwtTokenIssuer`, `PasswordHasher`, `SecretVault` (ya existe), **`RedisCoordinator`**
(podado: fusiona `RateLimiter`+`DeviceLockManager` — mismo mecanismo Redis+fallback
en memoria para 2 políticas distintas, `limitar(device_id)` y `bloquear(device_id)`
como métodos separados de la misma clase), `CleanupScheduler` (`cleanup_service.py`).

`RefreshTokenStore` — **no es una clase**, es 1 instancia de `Repository[T]` (la
rotación con detección de replay vive en `AutenticacionService.refrescar()`, ver
§1.6). `LoginAttemptTracker` — **tampoco es una clase nueva**, es `Repository
[LoginAttempt]` extendida (arriba) — no una instancia genérica como se decía antes.

### Infraestructura — otros adapters

`AnsibleAutomationEngine` (`ansible_service.py`), `CeleryJobQueue`, `AuditListener` (Observer → `AuditRepository`). **`GroupJobListener` se elimina** — no hay `GroupJob` que actualizar, el resumen de grupo se calcula al vuelo (ver `Job`/§2.9).

### Excepciones de dominio

**Corrección real, encontrada en esta revisión**: esto no es diseño nuevo — RNF-ERR-01/02
**ya está resuelto hoy**. `main.py` tiene handlers reales y wireados
(`ValidationError`→400, `NotFoundError`→404, `ConflictError`→409,
`DeviceExecutionError`→500, `UnsupportedVendorError`→501, catchall→500), usados
de verdad en `api/vlans.py`/`api/ports.py`/`api/devices.py`/`api/sites.py`/
`api/users.py`/`api/device_groups.py`/`api/jobs.py`/`api/group_jobs.py`. No es
que "cualquier función podría lanzar un `ValueError` genérico" — eso ya no pasa.
Lo que sigue faltando es **granularidad**: 5 tipos hoy, ninguno distingue
conexión/autenticación de ejecución/validación genérica.

```
DomainError                                      (catch-all, nunca se lanza directo)
 ├── ValidationError            → 400   (validación)   — ya existe, VLAN.__post_init__ se suma
 ├── NotFoundError              → 404   (—)            — ya existe. NO se angosta a "Device" —
 │                                                        se usa hoy para User/Site/DeviceGroup/
 │                                                        Job/GroupJob, angostarlo perdería cobertura
 ├── DeviceExecutionError       → 500   (ejecución)     — ya existe (`core/exceptions.py`), 12+ call
 │                                                        sites reales — el catch-all de "algo falló
 │                                                        ejecutando en el device", se mantiene
 ├── DeviceUnreachableError     → 502/504 (conexión)   — nuevo, especializa DeviceExecutionError
 │                                                        para fallas de conexión específicamente
 ├── UnsupportedVendorError     → 501   (ejecución)    — ya existe, `vendors/dispatcher.py:146`
 ├── AuthorizationError         → 403   (autenticación)— nuevo, hoy son `HTTPException(403)` sueltas
 │                                                        en `role_assignment_service.py`/`core/scope.py`
 ├── ResourceConflictError      → 409   (ejecución)    — ya existe como `ConflictError`, renombrada
 └── TransicionInvalidaError    → 409   (ejecución)    — nuevo, `Job._transicionar()` (§2.8)
```

**5 de 8 ya existen y funcionan** (`ValidationError`, `NotFoundError`,
`DeviceExecutionError`, `UnsupportedVendorError`, `ResourceConflictError`/
`ConflictError`) — el trabajo real es agregar 3 (`DeviceUnreachableError`,
`AuthorizationError`, `TransicionInvalidaError`), no construir la jerarquía
desde cero.

### Presentación

Sin clases nuevas — middleware JWT/RBAC/RateLimit sigue como `Depends()` de FastAPI,
**salvo `RLSSessionMiddleware`** (MSP Fase 6, real, con migración y tests propios),
que sí es una clase — es middleware ASGI (`BaseHTTPMiddleware`), no un `Depends()`,
y hoy no estaba ni mencionada en este documento (ver Infraestructura — seguridad,
§1.6). **Corrección**: "ya funciona así hoy" no era del todo cierto — hoy hay
**3 implementaciones** del mismo decode-JWT-con-fallback (`core/scope.py`,
`core/dependencies.py`, `core/rls_middleware.py`), no 1. `core/dependencies.py` se
borra; las otras 2 pasan a llamar a `JwtTokenIssuer.decodificar()` en vez de
reescribir el cuerpo — ver §1.6/§6. No agregar clases sigue siendo la decisión
correcta para el 99% de esto; lo que sí hace falta es dejar **un solo** núcleo de
decodificación, no tres.

---

## 1.5 Segunda poda — ~59 → ~45

Mismo criterio que la primera poda (configuración en vez de subclase): ¿tiene
comportamiento propio real, o es solo datos que podrían ser un campo de otra clase?

| Se funden/eliminan | En/como | Ahorro |
|---|---|---|
| `ConfiguracionIP`, `AclRule`, `DhcpRelayConfig` | campos de `InterfazVirtual` | -3 |
| `SnmpConfig`, `RutaEstatica`, `ServidoresConfig` | campos de `ConfiguracionGlobal` | -3 |
| `AccessToken`, `RefreshToken` | `dict`/tupla simple, sin clase | -2 |
| `RecursoAplicado`, `AplicacionFallida`, `JobCancelado` | 1 clase `DomainEvent(tipo, ...)` | -2 |
| `BaseVendorDriver`+`BasePortDriver`+`BaseInterfazDriver`+`BaseConfigDriver` | 1 interfaz `VendorDriver` | -3 |
| `RateLimiter`+`DeviceLockManager` | 1 clase `RedisCoordinator` | -1 |

**Total: -14, de ~59 a ~45** (el diagrama `.drawio` generado tiene el conteo exacto por clase).

Lo que **no** se tocó y por qué: las 11 Entidades (cada una responde a un RF
concreto del SRS, no hay forma de fusionarlas sin perder cobertura), `Orquestador`/
`GroupOperationRunner`/`PluginRegistry`/`EventDispatcher` (cada una con
colaboradores/estado real), `Repository[T]`+`AuditRepository` (ya está en su forma
mínima desde la 1ra poda), los 3 vendors (Strategy real, agregar uno = 1 clase),
`JwtTokenIssuer`/`PasswordHasher`/`SecretVault` (algoritmos genuinamente distintos:
JWT stateless, `pbkdf2_sha256` one-way, Fernet reversible — no hay mecanismo compartido que
fusionar como sí lo había en `RateLimiter`/`DeviceLockManager`), y las 7 excepciones
(costo casi nulo cada una, alto valor para RNF-ERR-02).

---

## 1.6 Catálogo detallado — cada clase, sus atributos, sus métodos, y de qué función/archivo actual sale cada uno

Formato: `método() ← archivo.py: función_actual()`. Donde dice "nuevo" no hay
código actual equivalente (funcionalidad que no existe hoy, ej. RF-INTERV/RF-GLOBAL).

### Aplicación

**`Orquestador`** — el motor genérico: ejecuta un `RecursoGestionable` sobre un
device, con lock, retry, verificación y rollback automático si algo falla. No sabe
qué es una VLAN ni un puerto — solo sabe correr el ciclo completo y avisar cuando
termina. **No habla con `AutomationEngine`/`PluginRegistry` directo** — eso ya lo
resuelve `Device` internamente (Phase 1 de esta sesión); `Orquestador` solo le pasa
el `Device` resuelto a `recurso.aplicar(device)`/`recurso.reconciliar(device)` y deja
que cada uno llame al método de `Device` que le corresponde.
**Simplificado, sin clase nueva** — `_ejecutar_con_retry`/`_clasificar_error`
quedan como métodos privados de `Orquestador`, no se extraen a ningún lado —
solo los usa ella, no hay reuso ni necesidad de testearlos aislados que
justifique una clase o un módulo aparte. Lo único que sí se muda es
`recuperar_huerfanos`, a `JobRepository` — es una operación masiva de arranque
sobre la tabla `jobs` (RNF-RES, jobs que quedaron "running" sin worker vivo
tras un crash), no ejecuta nada sobre ningún device y no necesita nada de
`Orquestador` — encaja con las demás consultas combinadas que ya vive en ese
repositorio (§2.2.1), no con el ciclo de ejecución.
- Atributos: `_device_repo: Repository[Device]`, `_eventos: EventDispatcher`,
  `_repos: dict[str, Repository]` (uno por tipo de `RecursoGestionable` — `"vlan"` →
  `Repository[VLAN]`, `"puerto"` → `Repository[Puerto]`, etc. — ver §2.4 nota (5))
- `ejecutar(recurso, device_name, actor, job)` ← `orchestration_runner.py: run_operation()` (los 366 líneas completos: lock, pre-state, validate, execute-with-retry, verify, rollback, audit)
- `_ejecutar_con_retry(fn)` ← `vlan_execution_service.py: _execute_with_retry()`
- `_clasificar_error(resultado) -> RetryDecision` (RNF-RES-01: manejar timeouts en la ejecución de tareas sobre dispositivos — los patrones `"timeout"`/`"ssh timeout"`/`"connection timeout"` son exactamente esto, nunca citado) ← **absorbe** `retry_policy.py`
  completo, no lo llama — `retry_policy.py` deja de existir como archivo
  aparte, no tiene sentido un módulo con una sola función que solo usa
  `Orquestador`. Las tablas `_PATRONES_PERMANENTES`/`_PATRONES_TRANSITORIOS`
  (15+14 strings, `retry_policy.py: _PERMANENT_PATTERNS`/`_TRANSIENT_PATTERNS`
  reales) pasan a ser constantes privadas del módulo de `Orquestador`. 2
  niveles, ambos se preservan: primero el chequeo de `rc` de Ansible
  (`rc ∈ {4,6,255}` → siempre transitorio, código "host inalcanzable",
  `vlan_execution_service.py: _classify_result()`), y solo si no matchea,
  clasifica por patrones de texto contra esas tablas (lo que hacía
  `retry_policy.py: classify_error()`) — todo dentro del mismo método
- `_rollback(recurso, pre_state, device) -> tuple[bool, bool | None]` ← unifica `vlan_execution_service.py: _rollback_create()/_rollback_delete()/_rollback_update()`, `port_execution_service.py: _rollback_admin_state()/_rollback_access_vlan()/_rollback_trunk_vlans()/_rollback_description()`, `port_config_service.py: _rollback_configure_port()/_rollback_shutdown_port()/_rollback_enable_port()`. **El tri-estado real no es opcional, es el contrato** (`vlan_execution_service.py:117-174`, las 10 funciones que unifica siguen el mismo patrón): `(False, None)` sin rollback disparado, `(True, False)` rollback intentado y falló (o no se pudo verificar), `(True, True)` rollback confirmado contra el device — y **nunca propaga una excepción**, cada una de las 10 funciones reales tiene su propio `try/except Exception` interno que atrapa incluso una falla de la *verificación* posterior al rollback (`vlan_execution_service.py:136-146,159-167`) — ver nota (9)

**`GroupOperationRunner`** (RNF-PERF-01: 50 devices concurrentes sin degradar
tiempo de respuesta — el fan-out async vía `JobQueue` es lo que lo cumple,
nunca citado) — reparte una misma operación (una VLAN, un puerto) entre
varios devices: genera un `group_job_id` (UUID, en memoria — **no crea una fila
de `GroupJob`**, esa entidad se elimina, ver `Job`/§2.9), un `Job` por device
con ese mismo `group_job_id` estampado, y encola cada uno para que
`Orquestador` lo ejecute en paralelo. Es la pieza que responde a "aplicá esto en N
switches", `Orquestador` responde a "aplicá esto en 1 switch". Es el coordinador de
una **Saga** (§2.9) — cada device es un paso independiente con su propia
compensación (`Orquestador._rollback()`) si falla.
- Atributos: `_orquestador: Orquestador`, `_job_queue: JobQueue`, `_coordinador: RedisCoordinator`
- `encolar(recurso, devices, actor)` ← unifica `vlan_execution_service.py: enqueue_create_jobs()/enqueue_delete_jobs()/enqueue_update_jobs()/enqueue_save_job()` + `run_group_create_job()/run_group_delete_job()/run_group_update_job()`, y los `enqueue_*_job()` equivalentes de `port_execution_service.py`/`port_config_service.py`

**`PluginRegistry`** — un diccionario con superpoderes: dado el vendor de un device
("huawei_vrp"), devuelve la clase que sabe hablarle a ese fabricante. Agregar un
vendor nuevo es registrarlo acá, no editar un `if/elif`. **También** es donde se
decide, una sola vez al arrancar la app, si el sistema corre en modo mock o real —
ver nota abajo.
- Atributos: `_vendors: dict[str, VendorDriver]`
- `registrar(vendor, driver)` ← nuevo (hoy no existe — `dispatcher.py` tiene el vendor hardcodeado en `if/elif`)
- `obtener(vendor)` ← `vendors/dispatcher.py: get_driver()/get_vendor_driver()/get_port_driver()/get_port_vendor_driver()` (unificadas en un solo método)

**Alternativa real considerada y descartada: sistema de plugins real
(`entry_points` de `setuptools`/`importlib.metadata`, como usa `pytest` para
sus plugins)**, en vez de `.registrar()` manual al arrancar. Permitiría
vendors como paquetes pip instalables por separado
(`pip install ansiauth-huawei-driver`), de terceros, auto-descubiertos. Se
descarta — el SRS no pide un ecosistema de plugins de terceros: son
exactamente 3 vendors, mantenidos en el mismo repo, por el mismo equipo.
Agregar `entry_points`/packaging resolvería un problema (extensibilidad para
terceros) que este proyecto no tiene — registro manual explícito es lo
correcto mientras eso siga siendo cierto.

**`EXECUTION_MODE` deja de chequearse en cada llamada — se resuelve una vez, acá.**
Hoy `EXECUTION_MODE == "mock"` se repite verificado en **10+ lugares** de 3 capas
distintas: dominio (`models/device.py:44,55,66` — una entidad de dominio leyendo una
env var), aplicación (`vlan_service.py:65`, `port_service.py` ×9) y hasta
presentación (`api/vlans.py:76`, `api/ports.py:195`) — es una violación de DIP: el
"cómo sé si estoy en mock" queda esparcido en vez de resuelto en un solo lugar.
Con `PluginRegistry`, la composición al arrancar decide una vez:
```python
if settings.EXECUTION_MODE == "mock":
    registry.registrar("huawei_vrp", MockVendor())
    registry.registrar("cisco_ios", MockVendor())
else:
    registry.registrar("huawei_vrp", HuaweiVendor())
    registry.registrar("cisco_ios", CiscoVendor())
```
`Device`/`vlan_service`/los endpoints nunca vuelven a preguntar `EXECUTION_MODE` —
solo llaman `registry.obtener(self.vendor)` y reciben lo que corresponda. Ver nota
en `Device` más abajo.

**Corrección sobre esta misma sección**: los "10+ lugares" no son un solo
problema con una sola solución — son 3 problemas distintos, y `PluginRegistry`
resuelve solo el primero:
1. **Selección de driver** (`device.py:44,55`) — resuelto por `PluginRegistry`,
   tal como está descripto arriba.
2. **`device.py:66` (`_get_password()`), no resuelto por esto** — salta el
   `vault.decrypt()` en modo mock, un chequeo de `EXECUTION_MODE` que
   `PluginRegistry` no toca porque no tiene nada que ver con qué driver se
   usa. Corrección real: es código muerto, no una necesidad — `MockVlanDriver`/
   `MockPortDriver` reciben `password` y nunca lo leen (`vendors/mock.py`), y
   los devices mock ya se siembran con contraseña Fernet real
   (`device_service.py: seed_defaults()`, `encrypt_password("admin")`), no un
   valor inválido que rompería un `decrypt()` incondicional. El chequeo se
   borra directo — desencriptar siempre, vía el `SecretVault` inyectado (nota
   (5) más abajo) — es lo que hace que la frase "`Device` no vuelve a saber
   que 'modo mock' existe" (más abajo) sea cierta, no solo declarada.
3. **`vlan_service.py:65`/`api/vlans.py:76` y sus equivalentes en `port_service.py`/
   `api/ports.py` — no es un problema de driver, es la API misma**: `device=None`
   solo es válido en modo mock, y en ese caso devuelve una lista fija en
   memoria en vez de leer un device real — no hay device del que sacar un
   vendor, entonces `PluginRegistry` no tiene nada que resolver acá.
   Es un atajo de test/desarrollo filtrado al contrato público de la API
   (`GET /vlans` sin `device` se comporta distinto según config del server, no
   según lo que pide el cliente) — no lo resuelve ninguna clase de este
   catálogo, queda documentado como fuera de alcance, igual que los 8 RNF de
   §6.
4. **El resto de `port_service.py` (list/update/admin-state) no lo arregla
   `PluginRegistry` tampoco** — hoy, en modo mock, esas funciones saltean
   `_resolve_device()` por completo, sin validar que el device exista. Lo que
   realmente lo arregla es absorber `port_service.py` en `Puerto`/`Device`
   (mismo camino que ya se hizo con `vlan_service.py` en Phase 2): el
   `Orquestador.ejecutar()` canónico (§2.4) siempre hace `RepoDev.get(device)`
   primero, sea cual sea el modo — la validación de existencia deja de ser
   opcional por config.

**Alternativa real considerada y descartada: que "mock" sea un valor de
`vendor` por device (`registry.registrar("mock", MockVendor())`, un device
con `vendor="mock"` conviviendo en la misma base con devices reales), en vez
de un flag global de proceso.** Permitiría un solo deployment con devices
reales y simulados a la vez (demos, onboarding incremental de un cliente
nuevo, smoke-test contra un device falso sin tocar red real). Se descarta —
el SRS no pide convivencia mock/real (RF-INV no distingue "tipos" de
ambiente, es infraestructura de prueba, no un requerimiento funcional), y
mezclar acciones simuladas con acciones reales en el mismo audit log
(RF-AUD-01, que debe ser confiable) es más riesgo que beneficio: un
`AuditRecord` que dice "VLAN creada" sin que quede claro si fue contra un
switch real o un mock es peor que dos ambientes separados. Mismo criterio
YAGNI que ya descartó `entry_points`/Protocol-split en `PluginRegistry`
arriba: resolver un problema (convivencia mock/real) que este proyecto no
tiene todavía.

**`EventDispatcher`** — el cartero del patrón Observer: recibe "esto pasó" de
`Orquestador` y se lo reparte a quien esté escuchando (hoy, solo auditoría —
`GroupJobListener` se eliminó, ver `Job`/§2.9), sin que `Orquestador` sepa
quiénes son esos "quien".
- Atributos: `_listeners: dict[type, list[EventListener]]`
- `suscribir(tipo_evento, listener)` ← nuevo
- `despachar(eventos)` ← reemplaza las llamadas directas a `audit_service.append_audit_event()`/`log_action()` esparcidas en `vlan_execution_service.py`, `port_execution_service.py`, `port_config_service.py`

**`AutenticacionService`** — la fachada de login: junta verificación de contraseña,
bloqueo por intentos fallidos, y emisión/renovación de tokens en un solo lugar en
vez de tener esas 3 responsabilidades sueltas por distintos archivos.
- Atributos: `_jwt_issuer: JwtTokenIssuer`, `_hasher: PasswordHasher`, `_login_attempts: LoginAttemptRepository`, `_refresh_tokens: Repository[RefreshToken]`
- `login(username, password)` ← `auth_service.py: authenticate_user()`, `user_service.py: authenticate()/verify_password()`, `login_attempt_service.py: is_username_locked()/is_ip_blocked()/record_attempt()` — los dos primeros ahora son `self._login_attempts.esta_bloqueado(username)`/`.ip_bloqueada(ip)` (ver `LoginAttemptRepository` en Infraestructura)
- `refrescar(raw_token)` ← `refresh_token_service.py: validate_and_rotate()`
- `revocar(raw_token)` ← `refresh_token_service.py: revoke()`

**`Inventory`** (RF-INV-01: ABMC de equipos en inventario, RF-INV-02: ABMC de
equipos dentro de grupo — ninguna versión anterior de este documento citaba estos
2 requisitos a pesar de dedicarle 2 secciones enteras) — el coordinador de
`Site`/`DeviceGroup`/`Device`: única puerta para
crear/mover/dar de baja un device, y para listar qué devices puede ver un usuario
según sus roles. **Ya existe** (MSP Fase 3), mismo contrato de 5 métodos — pero hoy
no sigue los patrones del resto de este catálogo (habla SQLAlchemy directo, lanza
`HTTPException`/`ValueError`, audita a mano). Abajo, cómo quedaría alineada —
firmas idénticas a las de hoy, cambia solo con qué habla adentro.
- Atributos: `_devices: Repository[Device]`, `_sites: Repository[Site]`,
  `_device_groups: Repository[DeviceGroup]`,
  `_jobs: JobRepository`, `_auditor: EventDispatcher`, `_vault: SecretVault`

```python
class Inventory:
    def get(self, name) -> Device | None:
        return self._devices.get(name)

    def list(self, scope: VisibilityScope, *, site_id=None, device_group_id=None) -> list[Device]:
        if scope.site_ids is None:
            devices = self._devices.todos()                             # system-admin
        else:
            devices = [d for d in self._devices.todos()
                       if d.site_id in scope.site_ids or d.device_group_id in scope.device_group_ids]
        if site_id is not None:
            devices = [d for d in devices if d.site_id == site_id]
        if device_group_id is not None:
            devices = [d for d in devices if d.device_group_id == device_group_id]
        return devices

    def register(self, *, name, host, vendor, platform, username, password,
                 site_id, device_group_id, actor) -> Device:
        site = self._sites.get(site_id)
        if site is None:
            raise ValidationError(f"site {site_id} no existe")           # (2)
        group = self._device_groups.get(device_group_id) if device_group_id else site.grupo_default()
        encrypted = self._vault.encrypt(password)                        # (5)
        device = Device.nuevo(name, host, vendor, platform, username, encrypted, group.id)
        self._devices.add(device)
        self._auditor.despachar([DomainEvent("device_registrado", device, device, actor, {"site_id": site_id})])  # (4)
        return device

    def move(self, name, target_group_id, actor, *, reason=None) -> Device:
        device = self._devices.get(name)
        if device is None:
            raise NotFoundError(name)                              # (2)
        if self._jobs.activo_para(name):
            raise ResourceConflictError(f"job en curso para {name}")     # (1)(2)
        target = self._device_groups.get(target_group_id) if target_group_id else self._sites.get(device.site_id).grupo_default()
        if device.device_group_id == target.id:
            self._auditor.despachar([DomainEvent("device_movido", device, device, actor,
                {"reason": reason, "to": target.id, "noop": True})])     # (6)
            return device                                                # no-op idempotente, pero SÍ auditado
        device.mover_a(target)                                           # (3)
        self._devices.add(device)
        self._auditor.despachar([DomainEvent("device_movido", device, device, actor, {"reason": reason, "to": target.id})])
        return device

    def deregister(self, name, actor) -> None:
        device = self._devices.get(name)
        if device is None:
            raise NotFoundError(name)
        if self._jobs.activo_para(name):
            raise ResourceConflictError(f"job en curso para {name}")
        self._devices.eliminar(device)
        self._auditor.despachar([DomainEvent("device_dado_de_baja", device, device, actor, {})])
```

**(1)** `JobRepository.activo_para(device)` es una consulta de filtro combinado
que un `Repository[T]` genérico no cubre — misma extensión ya aceptada para
`AuditRepository`, no una clase nueva. `list()` ya no llama a `scope_de()`
ella misma — recibe el `VisibilityScope` ya calculado como parámetro (ver
bloque de `RoleAssignmentRepository` más abajo y §6.1 para el detalle de la
unificación con `effective_role()`).

**(2)** `ValueError`/`HTTPException(404)`/`HTTPException(409)` → `ValidationError`/
`NotFoundError`/`ResourceConflictError` de la jerarquía de §1 — el
`ErrorMappingMiddleware` los traduce a HTTP, `Inventory` no vuelve a saber qué es
un código de status.

**(3)** El guard "sin job activo" y la invariante "un solo grupo, resolver Default
si el target es `None`" dejan de ser un `SELECT` con `JOIN` inline en `Inventory` y
pasan a vivir en `Device.mover_a()` — mismo criterio que en `move()` de hoy, pero
ahora es la entidad la que no permite un estado inconsistente, no una función que
hay que acordarse de llamar bien. `Device.nuevo(...)` valida el vendor al
construirse (mismo `__post_init__` que ya usa `VLAN`) en vez de un
`if vendor not in _VALID_VENDORS` suelto en `register()`.

**(4)** `audit_service.log_action(...)` a mano en cada método → `EventDispatcher.despachar(...)`
— mismo problema (y misma solución) que ya se corrigió para VLAN/Puerto en §2.5/§6.

**(5)** `Inventory` encripta **antes** de llamar a `Device.nuevo()` — inyecta
`SecretVault` por constructor (mismo criterio que el resto del catálogo, §2.7)
en vez de que `Device` importe un singleton de módulo. `Device.nuevo()` recibe
`encrypted_password` ya listo, nunca sabe que existe `SecretVault` — sigue
siendo un dataclass plano, sin colaboradores propios. Corrige una
inconsistencia real: hoy `device_service.py`/`inventory_service.py`/
`port_service.py` llaman a `secret_service.encrypt_password()`/
`decrypt_password()` (funciones-shim de módulo), mientras que `models/device.py`
importa `vault` (el singleton) directo y llama a sus métodos — 2 formas
distintas de usar el mismo objeto, ninguna inyectada. El shim de funciones se
elimina, `SecretVault` se inyecta donde haga falta (acá y en `Device`
internamente para desencriptar) — **corrección**: "que ya lo hacía bien" no es
del todo cierto, `Device._get_password()` hoy salta el `decrypt()` entero en
modo mock (`device.py:66`), un tercer chequeo de `EXECUTION_MODE` que no tiene
que ver con inyección sino con lógica muerta — se elimina el `if`, se
desencripta siempre (ver nota en `PluginRegistry` arriba).

**(6)** Corrección real, no de código viejo — inconsistencia encontrada entre 2
partes de este mismo catálogo. `VLAN.aplicar()`/`Puerto.aplicar()` (código real,
`check_noop` en `run_operation()`) auditan igual cuando la operación es un
no-op, con un `reason` tipo `"vlan_already_exists_no_op"` en el payload — queda
registro de que se pidió la acción, aunque no haya tocado el device. El código
real de `inventory_service.py: move()` hace lo contrario a propósito (comentario
original: *"keeps the endpoint idempotent without emitting a spurious audit
row"*) — 2 respuestas distintas a la misma pregunta, nunca resueltas. RF-AUD-01
("registrar cada acción ejecutada a través de la API") es más consistente con
lo que ya hace `VLAN`/`Puerto` — se alinea `Inventory.move()` a ese criterio:
un no-op también se audita, con `"noop": True` en el payload.

**Alcance real**: esto es el diseño de "si `Inventory` se reescribe con los mismos
patrones que el resto de este catálogo". `docs/DEVICE_IMPLEMENTATION_PLAN.md` — el
plan que sí se ejecutó sobre el código real — decidió explícitamente no tocarla
(tope de 5 métodos de la Fase 3 de MSP, `get()` se queda como delegate de una
línea). Hoy en `inventory_service.py` sigue tal cual estaba; esto queda como el
target si en algún momento se hace, no como trabajo pendiente real de esta sesión.

**`RoleAssignmentService`** (RF-USR-02: asignación de roles, RF-ACC-02: RBAC —
tampoco citados hasta ahora) — única dueña de la tabla `role_assignments` y de
`users.is_system_admin`. **Ya existe** (`role_assignment_service.py`, MSP Fase 3) —
y a diferencia de `Inventory`, esta sección del documento tenía un error real: la
entidad `RoleAssignment` (más abajo) decía que `grant`/`revoke`/`list_for_user`/
`set_system_admin` "van a `RoleAssignmentRepository` + `AutenticacionService`, no
a la entidad" — eso hubiera **borrado** lógica real al reescribir: la regla D25
(un group-admin no puede delegar roles, solo site-admin/system-admin) y el
invariante "no se puede degradar al último system-admin activo" no son CRUD, son
reglas de autorización con estado que hoy viven encapsuladas acá. Se revierte esa
frase y se documenta la clase tal cual es. **Una corrección puntual sí hace
falta** (ver `grant()` abajo) — el resto queda intacto.
- Atributos: `_role_assignments: RoleAssignmentRepository`, `_usuarios: Repository[Usuario]`
- `grant(target_user_id, site_id, device_group_id, role, actor)` → `RoleAssignmentRead` ← `role_assignment_service.py: RoleAssignmentService.grant()` (valida D25 vía `_authorize_grant_or_revoke`, es upsert — si ya existe el grant, actualiza el rol). **Corrección**: el código real solo audita cuando `existing.role != role` — un re-grant del **mismo** rol que ya tenía queda sin auditar, cero registro. Misma inconsistencia que ya corregimos en `Inventory.move()` (nota (6) más arriba) — se alinea: audita siempre, con `"noop": True` cuando el rol no cambió
- `revoke(grant_id, actor)` ← `RoleAssignmentService.revoke()`, misma autorización que `grant`
- `list_for_user(user_id, viewer)` → `list[RoleAssignmentRead]` ← `RoleAssignmentService.list_for_user()` — filtra por lo que el *viewer* puede ver, no por lo que tiene el *target* (es un caso más de "quién puede ver qué" — candidato a `VisibilityScope` si esta clase se llega a tocar, no se fuerza acá)
- `set_system_admin(target_user_id, is_system_admin, actor)` ← `RoleAssignmentService.set_system_admin()` — guarda el invariante "al menos un system-admin activo"
- `_authorize_grant_or_revoke(actor, site_id, device_group_id, role)` ← `RoleAssignmentService._authorize_grant_or_revoke()` — implementa D25

**Alcance real**: a diferencia de `Inventory` (que se deja como está a propósito),
acá el hallazgo es documental — la clase ya está bien diseñada, el error era de
este documento, no del código. No hace falta ninguna reescritura.

### Dominio — Entidades

**`Device`** — un equipo de red administrado: identidad (nombre, host, vendor,
site/grupo) y credenciales. Resuelve su driver de vendor y su password solo la
primera vez que hace falta, cacheados. **Corrección sobre este mismo
documento** (encontrada cuestionando el diseño, no en código viejo): versiones
anteriores mantenían acá los métodos públicos
`create_vlan()`/`delete_vlan()`/`update_vlan_description()`/`list_vlans()`/`save_config()`/`configure_port()`/`set_port_admin_state()`/`set_port_access_vlan()`/`set_trunk_allowed_vlans()`/`update_port_description()`/`list_ports()`
(los 11 que ya existen en `models/device.py`, Phase 1) sin cambios — se
sacan. Ver justificación abajo.

**Por qué se sacan — contradice su propio bounded context**: §7 dice de
"Topología de Red" (el contexto donde vive `Device`) que *"no sabe que existen
VLANs"* — pero `create_vlan(self, vlan: "VLAN")` toma un `VLAN` tipado como
parámetro, y lo mismo para `Puerto`. Con `InterfazVirtual`/`ConfiguracionGlobal`
(RF-INTERV/RF-GLOBAL) `Device` sumaría 2 tandas más de métodos — crece sin
límite con cada tipo de recurso nuevo, exactamente lo que este documento evita
en todos los demás lugares (`PluginRegistry` en vez de `if/elif` por vendor,
`repositorio()` en vez de `if/elif` por tipo en `Orquestador`). Es, en los
hechos, una copia 1 a 1 de la superficie completa de `VendorDriver` (que ya
fusiona los 4 drivers viejos en 1) reexpuesta como métodos propios de
`Device` — sin agregar comportamiento real más que resolver driver+password,
2 cosas, no 11.

**Fix**: `Device` expone 2 propiedades estables en vez de 1 método por
operación — `driver` (el `VendorDriver` ya resuelto y cacheado) y `password`
(ya desencriptada, cacheada). Cada `Recurso.aplicar(device)` llama a su driver
directo: `VLAN.aplicar()` pasa de `device.create_vlan(self)` a
`device.driver.create_vlan(self.vlan_id, self.name, device, device.password)`
(mismo cambio en `reconciliar()`/`Puerto`, ver sus notas más abajo). `Device`
deja de crecer con cada RF nuevo — 2 propiedades cubren cualquier recurso
futuro, no una lista abierta de métodos.

**Efecto secundario, encontrado al revisar esto**: `device.create_vlan()`
real (`device.py:72-74`) llama `vlan.validate_name()` **adentro**, antes de
delegar al driver — pero el `Orquestador.ejecutar()` canónico de este
documento (§2.4) ya llama `recurso.validar()` como paso explícito, antes de
`aplicar()`. Con `Device` reexponiendo `create_vlan()`, esa validación corría
2 veces sin que nadie lo hubiera decidido a propósito — vestigio de cuando
`Device` era el único lugar que validaba, antes de que `RecursoGestionable`
existiera. Con el fix de arriba desaparece sola: el driver nunca valida
("ejecuta, no valida", ya establecido para los vendors), y `validar()` corre
una sola vez, donde el contrato dice que corre.

```python
@property
def driver(self) -> "VendorDriver":
    if self._driver is None:
        self._driver = plugin_registry.obtener(self.vendor)
    return self._driver

@property
def password(self) -> str:
    if self._password is None:
        self._password = secret_vault.decrypt(self.encrypted_password)
    return self._password
```
De paso resuelve el hallazgo de `_get_vlan_driver()`/`_get_port_driver()`
(2 métodos casi idénticos, cada uno con su propio `if EXECUTION_MODE ==
"mock"`, `device.py:41-61`) — colapsan en la única propiedad `driver` de
arriba, y `password` deja de saltear el `decrypt()` en modo mock (nota en
`PluginRegistry` más arriba) — `Device` deja de importar `EXECUTION_MODE` del
todo, ninguna de las 2 propiedades vuelve a preguntar el modo.

**Alternativa considerada y descartada (se mantiene, ahora aplica a
`VendorDriver` y no a `Device`): un solo método genérico
`driver.aplicar(tipo: str, payload: dict)` en vez de un método explícito por
operación** (`create_vlan`/`configure_port`/etc.) — menos métodos que agregar
cuando lleguen RF-INTERV/RF-GLOBAL. Se descarta por el mismo motivo de
siempre en este documento: el universo de tipos acá es **cerrado y conocido**
(los 4 exactos que pide el SRS, no una lista abierta), y ahí el polimorfismo
explícito gana — un dispatch genérico por string tiene sentido cuando los
tipos crecen sin límite, no cuando están fijados de antemano. Perdés tipado
(typos en `tipo` sin que nada los detecte) sin ganar nada, porque el vendor
real igual necesita distinguir el comando por dentro — el `if/elif` que
evitamos con polimorfismo reaparecería adentro del driver.

**`Site`** — una sede/cliente del MSP; agrupa `DeviceGroup`s. Sabe renombrarse y
decir si tiene devices adentro (para bloquear un borrado inseguro).
- Atributos: `id`, `name`, `description`
- `renombrar(nuevo_nombre)` ← `site_service.py: update_site()` (parte del nombre)
- `actualizar_descripcion(desc)` ← `site_service.py: update_site()` (parte de la descripción)
- `tiene_devices()` ← `site_service.py: delete_site()` (el chequeo previo que hoy lanza `SiteHasDevicesError`)
- *(`create_site`/`get_site`/`list_sites`/`list_sites_for_user`/`ensure_base_infrastructure` van a `Repository[Site]`, no son comportamiento de la entidad — son consultas/altas)*

**`DeviceGroup`** — un grupo de devices dentro de un `Site` (ej. "switches de
planta baja"); un device pertenece a exactamente un grupo. **Corrección real,
encontrada armando el plan de migración** — el comportamiento NO es "mismo que
`Site`, un nivel más abajo": `Site.tiene_devices()`/`SiteHasDevicesError` sí es
real (`site_service.py: delete_site()` **bloquea** el borrado si
`device_count > 0`) — pero `device_group_service.py: delete_group()` real
**nunca** bloquea por cantidad de devices. `GroupHasDevicesError` existe como
clase pero su propio docstring dice *"Not used by the normal delete path —
devices are auto-moved to the Site's Default group by delete_group (D19)"* — el
borrado real **mueve automáticamente** los devices al grupo Default del site
(vía `Inventory.move()`, `enforce_authz=False`) y recién después borra el grupo.
La guarda real que sí bloquea es otra completamente distinta: `is_default`
(D7) — un grupo Default (`DeviceGroupModel.is_default`) no se puede renombrar
ni borrar, sin importar cuántos devices tenga.
- Atributos: `id`, `name`, `description`, `site_id`, `es_default: bool`
- `renombrar(nuevo_nombre)` ← `device_group_service.py: rename_group()` — lanza si `self.es_default` (D7), no tiene nada que ver con devices
- *(`tiene_devices()` se elimina del catálogo — no hay ningún caller real que la necesite; el borrado real es "mover devices al Default + borrar", no "bloquear si tiene devices", ver §1.6 `Inventory`/coordinador de borrado, Fase 7 del plan de migración)*

**`Usuario`** (RF-USR-01: ABMC de usuarios) — quien opera el sistema; sabe
verificar y cambiar su propia contraseña, y desactivarse. No sabe nada de
roles/permisos — eso es `RoleAssignment`.
- Atributos: `id`, `username`, `hashed_password`, `is_active`, `is_system_admin`
- `verificar_password(plano)` ← `user_service.py: verify_password()`, `core/security.py: verify_password()`
- `actualizar_password(nuevo)` ← `user_service.py: update_password()`, `core/security.py: hash_password()`
- `desactivar()` ← `user_service.py: deactivate_user()`

**`Job`** — el registro de una operación async sobre un device: su estado
(pending/running/completed/failed), reintentos, y si hubo rollback. RF-JOB-01..04
son básicamente "consultar/cancelar un Job". Ya existe como dataclass
(`models/job.py`), gana métodos — cada uno valida la transición contra una
**tabla** en vez de mutar `status` directo (ver §2.8 — corregido de State
pattern a tabla de transiciones, mismo resultado con 1 clase menos):
- `marcar_iniciado()` ← reemplaza `job_service.py: update_job(status="running")`
- `marcar_completado(resultado)` ← reemplaza `job_service.py: update_job(status="completed", result=...)`
- `marcar_fallido(error, rollback_performed=False, rollback_success=None)` ← reemplaza `job_service.py: update_job(status="failed", error=..., rollback_performed=..., rollback_success=...)` — **corrección (nota (9), §2.4)**: el ejemplo canónico de `Orquestador.ejecutar()` llamaba a `marcar_fallido(str(error))` sin los otros 2 parámetros, perdiendo el tri-estado de rollback que `api/jobs.py: _format_job()` ya expone hoy en `execution_summary`
- `cancelar()` ← reemplaza `job_service.py: cancel_job()`, pero ahora lanza `TransicionInvalidaError` en vez de no-op silencioso si el Job ya terminó
- `registrar_reintento(delay)` ← `job_service.py: update_job(retry_count=..., last_error=..., current_step="retrying")` (dentro de `_execute_with_retry`)
- `asegurar_estado_final()` ← `job_service.py: ensure_final_state()` — **única
  excepción deliberada**, no llama a `_transicionar()` (§2.8 nota nueva):
  recuperación ante un crash del worker, no una transición de negocio
- `_transicionar(nuevo_status)` (privado) ← valida contra `_TRANSICIONES_VALIDAS`, el resto de los métodos de arriba lo llaman en vez de asignar `status` directo
- `esta_en_estado_terminal() -> bool` ← nuevo — `self.status in ("completed", "failed", "cancelled")`, mismo criterio que ya usa `_TRANSICIONES_VALIDAS` (sets vacíos = terminal). Lo usa `Orquestador.ejecutar()` como guard de idempotencia contra reentregas de Celery (§2.4 nota nueva) — no es una regla de negocio nueva, es exponer una pregunta que la tabla de transiciones ya podía responder
- Atributo `group_job_id: Optional[str]` ← ya existe (`models/job.py`) — el
  UUID que agrupa todos los `Job` de una misma acción disparada sobre N
  devices (ej. "crear VLAN 100 en 5 switches"). Lo estampa
  `GroupOperationRunner.encolar()` (§2.9), no una fila de `GroupJob`

**`GroupJob`/`DeviceExecution`/`GroupJobListener` se eliminan del catálogo** —
`Job.group_job_id` ya alcanza para agrupar. `total_devices`/`execution_summary()`
(lo que hacía `GroupJob`) y `device_results` (lo que era `DeviceExecution`) se
recalculan al vuelo desde `Repository[Job].list(group_job_id=X)` — ver
`JobRepository.resumen_de_grupo()` en Infraestructura y §2.9. No hay una
segunda copia del estado que mantener sincronizada, ni un Observer
(`GroupJobListener`) que se pueda desincronizar de los `Job` reales.

**`RoleAssignment`** (RF-ACC-02: RBAC) — un permiso otorgado: "este usuario tiene
este rol en este Site/DeviceGroup". Entidad de datos — el matching "más
específico gana" que antes se pensó como método de instancia (`es_valido_para`)
se corrigió: vive en `VisibilityScope.rol_para()` (arriba, Infraestructura —
persistencia), porque opera sobre **todos** los grants de un usuario juntos en
memoria, no sobre un grant a la vez.
- Atributos: `id`, `user_id`, `site_id`, `device_group_id`, `role`, `created_at`, `created_by_user_id`
- *(`grant`/`revoke`/`list_for_user`/`set_system_admin` de `role_assignment_service.py` **no se tocan** — son `RoleAssignmentService`, clase de Aplicación ya existente y bien hecha, ver más arriba. Versión anterior de este documento decía por error que se disolvían acá)*

**`VLAN`** (RF-VLAN-01: crear, RF-VLAN-02: eliminar, RF-VLAN-03: editar
descripción, RF-VLAN-04: consultar — antes solo RF-VLAN-03 estaba citado) — una
VLAN configurada (o a configurar) en un device: se autovalida al
construirse, y sabe aplicarse a sí misma contra el driver del vendor que le pasen.
Ya existe (`models/vlan.py`), **con un campo nuevo que faltaba** (ver nota abajo):
- Atributos: `vlan_id`, `name`, `status`, **`device: str`** (nuevo — el nombre del device al que pertenece; sin esto `Repository[VLAN].add()` no tiene con qué armar la fila de `device_vlans`, ver nota)
- `validar()` (`__post_init__`) ← **absorbe** `validators/vlan_validator.py`
  completo (29 líneas: `validate_vlan_id_range()`/`validate_vlan_not_reserved()`/
  `validate_vlan_name()`) — el archivo no sobrevive. Único lugar donde se
  valida una VLAN; `api/vlans.py` deja de llamar al validator por su cuenta
  (§2.1 — esa duplicación era justo el problema que motivó el Value Object)
- `reconciliar(device)` ← llama `device.driver.get_vlans(device, device.password)` y busca la propia (mismo dato que `vlan_execution_service.py: _capture_pre_state_vlan()`) — **corrección**: versiones anteriores decían "vía `device.list_vlans()`"; con el fix de `Device` (arriba, ya no reexpone métodos por operación), `VLAN` habla con `device.driver` directo
- `aplicar(device)` ← llama `device.driver.create_vlan(...)`/`delete_vlan(...)`/`update_vlan(...)`/`save_config(...)` (con `device`/`device.password` como argumentos, mismas firmas que `VendorDriver` ya tiene hoy) — reemplaza a `vlan_service.py: create_vlan_on_device()/delete_vlan()/update_vlan_description()/save_config_on_device()`, que hoy delegan a `Device` (Phase 2 de esta sesión) — pasan a delegar a `device.driver` en vez de a métodos de `Device` (ver corrección en `Device`, arriba). Internamente decide (§2.4 nota (6)): si ya existe una VLAN con este `vlan_id` pero **otro** nombre, falla (`vlan_execution_service.py:340-361`); si ya existe con el **mismo** nombre, no toca el device (`vlan_execution_service.py:363-372`); en operaciones destructivas, confirma el resultado contra lo esperado (`vlan_execution_service.py:476`) — las 3 son parte de "cómo se aplica una VLAN", no fases separadas del contrato
- `repositorio()` → `"vlan"` ← nuevo, 4to método de `RecursoGestionable` (§2.4 nota (5)) — le dice a `Orquestador` a qué `Repository` pertenece sin que `Orquestador` necesite preguntarle el tipo

**`Puerto`** — un puerto físico de un device (interfaz, modo access/trunk, VLANs
asignadas, PoE, etc.); mismo rol que `VLAN` pero para RF-PUERTO-01..10.
- Atributos: `interface`, `device: str` (nuevo, mismo motivo que `VLAN`) — el resto,
  **`Optional`, default `None`**: `description`, `admin_up`, `mode`, `access_vlan`,
  `allowed_vlans`, `poe_enabled`. `operational_up`/`speed`/`duplex` son
  **solo de lectura** (los reporta el device, `aplicar()` nunca los toca — ver
  nota de `mutation_fields` abajo). **Corrección de esta revisión**: versiones
  anteriores los tenían todos como campos "planos" (siempre presentes) — ver
  por qué cambia, nota siguiente
- `validar()` (`__post_init__`) ← **absorbe** `validators/port_validator.py`'s
  funciones de validación (`validate_interface_name()`/`validate_access_vlan_id()`/
  `validate_trunk_vlan_id()`/`validate_trunk_vlan_list()`/`validate_description()`
  — hoy llamadas solo desde `api/ports.py`, nunca desde `models/port.py`,
  mismo problema que tenía `VLAN` antes de §2.1). Único lugar donde se valida
  un puerto — `api/ports.py` deja de llamar al validator por su cuenta.
  **`compress_vlans_cisco()`/`compress_vlans_huawei()`** (mismo archivo, pero
  no son validación — formatean una lista de VLANs a la sintaxis CLI de cada
  vendor) se quedan del lado de ejecución, no de `Puerto`: pasan a ser métodos
  privados de `CiscoVendor`/`HuaweiVendor` respectivamente (§1.6 Infraestructura
  — vendors) — los drivers ejecutan, `Puerto` valida, ninguno hace el trabajo
  del otro. `validators/port_validator.py` no sobrevive como archivo
- `reconciliar(device)` ← llama `device.driver.list_ports(device, device.password)` y busca el propio (mismo dato que unifica `port_execution_service.py: _capture_pre_state_admin()/_capture_pre_state_access_vlan()/_capture_pre_state_trunk_vlans()/_capture_pre_state_description()`, `port_config_service.py: _capture_pre_state_configure()/_capture_pre_state_shutdown()/_capture_pre_state_enable()`) — **corrección**, mismo motivo que `VLAN` arriba: vía `device.driver`, no vía un método propio de `Device`
- `aplicar(device)` — **hallazgo real, resuelto en esta revisión**: a diferencia de `VLAN`
  (1 sola pregunta: ¿existe, con qué nombre?), `Puerto` cubre **6 operaciones de
  vendor genuinamente independientes** (`update_port_description`/`set_port_admin_state`/
  `set_port_access_vlan`/`set_trunk_allowed_vlans`/`configure_port`/`shutdown_port`+`enable_port`
  — 7 endpoints reales en `api/ports.py`, cada uno con su propio `run_*_job()`).
  Con todos los campos siempre presentes (como estaban antes de esta revisión),
  `aplicar()` no puede distinguir "el caller quiere `admin_up=True`" de
  "`admin_up=True` ya era el valor actual, copiado sin querer decir nada" — el
  código real ya resuelve exactamente esto con `PortConfigRequest.mutation_fields`
  (`port_config_service.py:420`), que este catálogo no había absorbido. **Fix**:
  con los campos opcionales de arriba, `mutation_fields` no es un campo
  guardado — es una `@property` (`{f for f in campos_mutables if getattr(self, f) is not None}`)
  y `aplicar()` despacha por presencia: 1 campo no-`None` → el método puntual del
  driver (`device.driver.set_port_access_vlan(...)`, etc.); 2+ campos no-`None`
  → `device.driver.configure_port(self)`, el **composite real** (`port_driver_base.py:261-300`,
  ya documentado como "aplica todos los campos no-`None` en una sola llamada al
  device, el driver decide el orden interno" — sigue siendo **una sola llamada,
  un solo `resultado`**, el Template Method de `Orquestador` no necesita saber
  que existe esta distinción. Reemplaza a `port_service.py:
  update_port_description_on_device()/set_port_admin_state_on_device()/set_port_access_vlan_on_device()/set_trunk_allowed_vlans_on_device()/configure_port_on_device()/shutdown_port_on_device()/enable_port_on_device()`,
  que pasan a delegar a `device.driver` (ver corrección en `Device`, §1).
  Internamente decide idempotencia (`_is_configure_noop()`) y validación contra
  estado, mismo criterio que `VLAN` (§2.4 nota (6)) — sobre el subconjunto de
  campos que `mutation_fields` marca, no sobre todos
- `repositorio()` → `"puerto"` ← nuevo, mismo motivo que en `VLAN`

**Nota para cuando se construyan `InterfazVirtual`/`ConfiguracionGlobal`**: las
dos van a tener el mismo problema que `Puerto` — varios campos independientes,
no una sola pregunta de existencia como `VLAN`. Mismo patrón: campos
`Optional`, `mutation_fields` como propiedad calculada, sin volver a inventar
la solución por separado para cada una.

**`InterfazVirtual`** (RF-INTERV) — una interfaz lógica (SVI) de un device: IP,
ACL, DHCP relay, asociada a una VLAN. Mismo rol que `VLAN`/`Puerto`, cubre
RF-INTERV-01..09. Atributos y métodos (`validar`/`reconciliar`/`aplicar`)
**nuevos**, no hay código actual (RF-INTERV-01..09 no implementado hoy).

**Limitación real conocida, no resuelta todavía**: `InterfazVirtual` está
*asociada a una VLAN* — a diferencia de `VLAN`/`Puerto`, que son
independientes entre sí, acá hay una dependencia real: la SVI necesita que su
VLAN ya exista en el device. `GroupOperationRunner` (§2.9) asume que los N
pasos de una Saga son **independientes**, despachados en paralelo sin orden
garantizado — correcto para "VLAN 100 en switch-A y switch-B" (2 devices sin
relación), pero si un mismo request pidiera "crear VLAN 100 y su interfaz
virtual" en el mismo grupo, esos 2 pasos **sí** tienen una dependencia de
orden que el modelo actual no expresa. No es un bug hoy — `InterfazVirtual`
no está construida — pero es un límite real del modelo de Saga tal como está
(pasos independientes) que no cubre pasos dependientes entre sí, y este
documento no dice todavía qué hacer cuando eso aparezca (opciones a evaluar
cuando se construya: encolar la SVI en un `Job` separado que dependa del
`Job` de la VLAN, o resolver la dependencia en la capa de aplicación antes de
llamar a `encolar()`).

**`ConfiguracionGlobal`** (RF-GLOBAL) — la configuración del device que no es
VLAN/Puerto/Interfaz: hostname, SNMP, rutas, NTP/DNS, backup/restore. Un solo
objeto por device (no una lista). Ídem, **nueva**, sin código actual (RF-GLOBAL-01..12 no implementado hoy).

**2 notas para cuando se construya, leyendo los 12 sub-requisitos completos**:

1. **RF-GLOBAL-04 (ACLs) es de 2 niveles, esta entidad solo cubre uno.**
   `AclRule` ya se podó (§1.5) como campo de `InterfazVirtual` — eso es la
   **aplicación** de una ACL a una interfaz puntual ("ACL 100 entrante en
   Gi0/1"). RF-GLOBAL-04 pide "consultar ACLs configuradas" como requisito
   global, separado — eso es la **definición** del catálogo de ACLs del
   device (mismo patrón que en equipos reales: se define una vez a nivel
   device, se aplica en N interfaces). Falta el primer nivel acá.
2. **RF-GLOBAL-10/11 ("Respaldar"/"Restaurar — Rollback") no es
   `Orquestador._rollback()`.** Mismo nombre, cosa distinta: `_rollback()`
   (Saga, §2.9) es automático, disparado por una falla de ejecución, compensa
   un paso puntual. RF-GLOBAL-11 es un backup/restore explícito pedido por el
   usuario ("volver a la config de ayer") — no reusa `_rollback()`, es una
   operación de `ConfiguracionGlobal` en sí misma (`respaldar()`/`restaurar()`).

### Dominio — Value Objects / Eventos

**`AuditRecord`** (RNF-AUD-01: trazabilidad completa, **inmutabilidad e
integridad** de los registros de auditoría — coincide palabra por palabra con
lo que sigue, nunca citado hasta ahora) — una entrada del log de auditoría:
quién hizo qué, cuándo, con
qué resultado. Write-once, nunca se edita. Ya existe (`models/audit.py`), sin cambios.
"Write-once" no es solo una convención documentada — **ya está forzado por código**:
`db/audit_guard.py: AuditImmutabilityError` + un listener `before_flush` de
SQLAlchemy rechazan cualquier `UPDATE` sobre una fila de `audit_logs` antes de
que llegue a ejecutarse el SQL, portable entre SQLite/Postgres/MySQL (reemplaza
un trigger de DB que no lo era). `DELETE` sigue permitido (retención/tests).

**`RetryDecision`** — el resultado de clasificar un error: ¿se reintenta o no?
Ya existe hoy en `retry_policy.py` como `@dataclass` — mismo criterio de VO
que `AuditRecord`, nunca estuvo en el catálogo. Se muda junto con todo lo
demás de `retry_policy.py` a vivir junto a `Orquestador` (§2.4) — el archivo
`retry_policy.py` no sobrevive solo.
- Atributos: `should_retry: bool`, `classification: "transient" | "permanent"`, `reason: str`
- Sin método propio — lo produce/consume `Orquestador._clasificar_error()`/`_ejecutar_con_retry()` (§2.4), métodos privados, sin módulo aparte

**`DomainEvent`** — el mensaje que viaja de `Orquestador` a `EventDispatcher`:
"pasó esto, con este recurso, en este device, con este resultado".
- Atributos: `tipo: str`, `recurso`, `device`, `payload: dict`, `timestamp`
- Sin método propio — es el mensaje que `EventDispatcher.despachar()` reparte. Nuevo.

### Dominio — Interfaces (ports)

Ninguna de estas se instancia — son el "contrato" que la infraestructura cumple, para que el dominio nunca importe SQLAlchemy/Ansible/Celery directo.

- **`Repository[T]`** — "cómo se persiste cualquier entidad", sin decir con qué tecnología. Nuevo puerto formal; formaliza el patrón que hoy repite cada `_to_domain(row)` + `get_session()` en `device_service.py:16`, `job_service.py:19`, `group_job_service.py:21`, `site_service.py:34`, `device_group_service.py:26`, `user_service.py:19`, `role_assignment_service.py:29`.
- **`AutomationEngine`** — "cómo se ejecuta un playbook", sin decir que es Ansible. Nuevo puerto; refleja `ansible_service.py: run_playbook()`.
- **`JobQueue`** — "cómo se despacha trabajo async", sin decir que es Celery. Nuevo puerto; refleja `worker.py` (`celery_app`) y los `.delay(...)` que hoy se llaman directo en `vlan_execution_service.py`/`port_execution_service.py`.
- **`EventListener`** — "cómo reacciona algo a un evento" — lo implementa `AuditListener` (único listener real hoy; `GroupJobListener` se eliminó, ver `Job`/§2.9). Nuevo.
- **`RecursoGestionable`** — "qué debe saber hacer cualquier cosa que `Orquestador` pueda ejecutar" — 4 métodos: `validar()`/`reconciliar(device)`/`aplicar(device)` + `repositorio()` (nota (5) de §2.4). `orchestration_runner.py` real (§2.4 nota (6)) tiene además fases de "¿ya está aplicado, no hago nada?" y "verificar que el cambio prendió" — **decisión de diseño**: en vez de 2 métodos más en el contrato, esas fases quedan **adentro** de `aplicar()` de cada recurso concreto (`VLAN`/`Puerto` deciden ellas mismas si hace falta aplicar algo y cómo confirmarlo) — mismo comportamiento, contrato más chico, cada recurso dueño completo de "cómo se aplica a sí mismo" en vez de repartido en varios puntos de entrada. Nuevo.
- **`VendorDriver`** — "qué debe saber hacer cualquier vendor" — fusiona `vendors/base.py: BaseVendorDriver` (7 métodos: `create_vlan`/`delete_vlan`/`update_vlan`/`save_config`/`get_vlans`/`list_vlans`/`get_vlan`) + `vendors/port_driver_base.py: BasePortDriver` (10 métodos) + 2 interfaces nuevas para `InterfazVirtual`/`ConfiguracionGlobal` (RF-INTERV/RF-GLOBAL, no existen hoy). **Una sola interfaz, sin segregar por capacidad** — los 3 vendors reales (`Huawei`/`Cisco`/`Mock`) implementan todo junto hoy, y el SRS no menciona ningún vendor de capacidad parcial. Separarla por `Protocol`/ISP se evaluó y se descartó — resolvería un problema que no existe todavía; el día que aparezca un vendor real de capacidad limitada, ahí se separa, no antes.

### Infraestructura

**`Repository[T]` (impl SQLAlchemy)** — la única implementación real de
`Repository[T]`; una instancia por entidad (`Repository[Device]`,
`Repository[Site]`, etc.), configurada con qué modelo ORM y cómo mapearlo, no
subclasificada.
- Atributos: `_session_factory`, `_orm_model`, `_to_domain: Callable`, `_to_orm: Callable`
- `get(id)`/`list(**filtros)`/`add(entidad)`/`remove(id)` ← el esqueleto `with get_session() as session: session.query(Model).filter_by(...)` repetido ~50 veces en los 8 servicios CRUD

**`AuditRepository`** (RF-AUD-01: registrar cada acción; RF-AUD-02: consultar
registros históricos filtrando por usuario/fecha/dispositivo/acción — nunca
citado hasta ahora, a pesar de que `query()` ya hace exactamente eso) — la
primera de 5 subclases de `Repository[T]` (ver las
otras 4 abajo, y el criterio unificado en §2.2.1): además del CRUD genérico, sabe
hacer consultas con filtros combinados (usuario+device+fecha+acción) que un
`Repository[T]` genérico no cubre.
- `append(record)` ← `audit_service.py: log_action()/append_audit_event()`. **Corrección de esta
  revisión**: no delega a `self.add()` — `Repository[T].add()` es upsert (`session.merge()` por
  PK, §2.2), correcto para `Job`/`VLAN`/`Site`/etc., pero un `merge()` sobre un `AuditRecord` con
  un `id` repetido (bug, reintento, o manipulación deliberada) **sobreescribiría en silencio** una
  fila existente — exactamente lo que RNF-AUD/§6.5 del SRS prohíbe ("registros inmutables...
  a prueba de manipulaciones"). `append()` usa `session.add()` (insert estricto) directo: una
  colisión de `id` debe fallar ruidosamente (`IntegrityError`), no pisar el registro anterior.
  No hace falta subclase nueva para esto — `AuditRepository` ya es subclase por otro motivo
  (§2.2.1), esto es una particularidad más de una clase que ya existe
- `query(user, date_range, action, scope: VisibilityScope)` ← `audit_service.py: get_audit_log()/_apply_filters()`. `_apply_msp_audit_scoping()` (114 líneas, `audit_service.py:132-198`) **ya no vive acá**, pero **corrección sobre una revisión anterior de este documento**: no alcanza con recibir el `VisibilityScope` tal cual — es una unión de 5 condiciones por `resource`, y solo 2 de ellas usan IDs que `VisibilityScope` ya expone directo (`resource='site'` → `scope.site_ids`, `resource='device_group'` → `scope.device_group_ids`). Las otras 3 necesitan nombres de device, que `VisibilityScope` no tiene: `resource='device'` y el resto (job/vlan/port/…, vía la columna `device`) llaman `DeviceRepository.nombres_visibles(scope)` (abajo); `resource='user'` es un caso aparte — fila visible si el usuario auditado tiene un grant en `scope.site_ids`, **o** si `AuditLogModel.user == viewer.username` (uno siempre ve sus propias acciones, tenga o no grants)
- `count(**filtros)` ← `audit_service.py: count_audit_log()`
- `purge_old(retention_days)` (RNF-AUD-02: retención configurable, default 30 días — `AUDIT_RETENTION_DAYS`, nunca citado) ← `audit_service.py: purge_old_records()`

**`RoleAssignmentRepository`** — segunda de 5 subclases de
`Repository[T]`, mismo criterio que `AuditRepository`: cierra la duplicación real
de "¿qué puede ver/hacer este usuario?" reescrita en 5 lugares distintos hoy
(`site_service.py: list_sites_for_user()` 34 líneas, `device_group_service.py:
list_groups_for_user()` 47 líneas, `inventory_service.py: _visible_device_names()`
50 líneas, `audit_service.py: _apply_msp_audit_scoping()` 114 líneas, **más**
`effective_role.py: effective_role()` — antes se contaba aparte, ahora se ve
que es la misma pregunta con otra forma — **245+ líneas medidas**, mismo query
de fondo repetido con SQL levemente distinto cada vez).
- `scope_de(user_id) -> VisibilityScope` ← trae los grants del usuario **una
  sola vez**, calculado como dependencia de FastAPI al principio de cada
  request (`Depends(obtener_scope)`), no en cada punto de chequeo — unifica
  las 5 funciones de arriba, incluida `effective_role()`

```python
@dataclass(frozen=True)
class VisibilityScope:
    es_system_admin: bool
    grants: tuple[tuple[int, int | None, str], ...]   # (site_id, device_group_id|None, role)

    def rol_para(self, site_id: int, device_group_id: int | None = None) -> str | None:
        if self.es_system_admin:
            return "super-admin"
        if device_group_id is not None:                 # más específico gana (D11)
            for sid, gid, role in self.grants:
                if sid == site_id and gid == device_group_id:
                    return role
        for sid, gid, role in self.grants:
            if sid == site_id and gid is None:
                return role
        return None

    @property
    def site_ids(self) -> set[int] | None:               # SOLO grants site-wide (6)
        return None if self.es_system_admin else {sid for sid, gid, _ in self.grants if gid is None}
    @property
    def device_group_ids(self) -> set[int]:
        return set() if self.es_system_admin else {gid for _, gid, _ in self.grants if gid is not None}
```

**(6) Bug real corregido en esta revisión**: `site_ids` tomaba el `site_id` de
**cualquier** grant, incluidos los específicos de un grupo — un usuario con
**solo** un grant de grupo (sin grant a nivel site) terminaba viendo **todos**
los devices del site entero vía `Inventory.list()`'s `d.site_id in
scope.site_ids`, fuga de visibilidad a grupos hermanos nunca otorgados.
`site_ids` ahora solo cuenta grants site-wide (`gid is None`) — los grants de
grupo específico ya los cubre `device_group_ids` por separado, sin
superposición.

`VisibilityScope` reemplaza tanto lo que hacía `effective_role()` (`rol_para()`
— chequeo puntual, en memoria, sin query nueva) como lo que hacía la versión
anterior de `scope_de()` (`site_ids`/`device_group_ids` — visibilidad en lote,
mismos datos ya en memoria). `Inventory.list()`, `site_service`,
`device_group_service`, `AuditRepository.query()`, `JobRepository.query()` y
`require_scope()` (Presentación, abajo) reciben **el mismo objeto**, calculado
una vez — el query de "grants del usuario" se escribe una vez, se ejecuta una
vez por request, no 5.

**`LoginAttemptRepository`** — tercera de 5 subclases. El catálogo
original de este documento decía que `LoginAttemptTracker` "no es una clase, es una
instancia de `Repository[T]`" — genérico no alcanza: `is_username_locked()`/
`is_ip_blocked()` cuentan intentos fallidos **dentro de una ventana de tiempo** y
comparan contra un umbral (5 fallos/15 min por usuario, 20 fallos/1h por IP,
`login_attempt_service.py:6-9`) — no es un `get(id)`/`add(entidad)`.
- `esta_bloqueado(username) -> bool` ← `login_attempt_service.py: is_username_locked()`
- `ip_bloqueada(ip) -> bool` ← `login_attempt_service.py: is_ip_blocked()`
- `registrar_intento(username, ip, exitoso)` ← `login_attempt_service.py: record_attempt()`
- `resetear(username)` ← `login_attempt_service.py: reset_username_failures()`/`unlock_username()` (unificadas — hoy son 2 funciones casi iguales, una cuenta las filas borradas y la otra no, sin otra diferencia)

**`JobRepository`** — cuarta de 5 subclases. `query_jobs()` (`job_service.py:81-131`) ya
filtra por status/device/rango de fechas/paginación — y además arma su **propio**
JOIN `DeviceModel`↔`DeviceGroupModel` para resolver "devices de este site", una
quinta variante del mismo problema que `scope_de()` ya resuelve. No se reinventa:
recibe el `VisibilityScope` en vez de reconstruir su propio JOIN.
- `query(status, date_range, scope: VisibilityScope, page, page_size) -> tuple[list[Job], int]` ← `job_service.py: query_jobs()` — `JobModel.device` también es un string, no un FK a site/group (mismo problema que `AuditRepository.query()` arriba, ver **(7)**): `query()` llama primero `DeviceRepository.nombres_visibles(scope)` (abajo) y recién con ese `set[str]` arma el `JobModel.device.in_(...)` — el `site_id` suelto y el `allowed_devices` que hoy son dos parámetros separados (`job_service.py:87,100`) se unifican en esa única llamada
- `activo_para(device) -> bool` ← ya documentado en `Inventory` (§1.6 arriba)
- `recuperar_huerfanos() -> int` ← `job_service.py: mark_orphaned_jobs_failed()` — mudada acá desde `Orquestador` (§2.4): operación masiva de arranque sobre la tabla `jobs`, no necesita nada de la ejecución sobre un device
- `resumen_de_grupo(group_job_id) -> dict` (RNF-LOG-06: "registrar fallos
  parciales en operaciones sobre múltiples dispositivos" — `partial_success`
  es exactamente esto, nunca citado) ← **nuevo** — reemplaza a
  `GroupJob`/`DeviceExecution`/`GroupJobListener` completos (§1, entidad `Job`):
  `SELECT * FROM jobs WHERE group_job_id = X`, agregado en memoria —
  `total_devices=len(jobs)`, `completed`/`failed`/`partial_success` contando
  por `status`, `device_results` es la lista de esos mismos `Job` formateados.
  Se calcula fresco en cada consulta — no hay una segunda copia de estado que
  pueda quedar desactualizada

**`DeviceRepository`** — quinta subclase, encontrada revisando mi propio
rediseño de `AuditRepository`/`JobRepository` arriba, no un requerimiento
nuevo. `VisibilityScope` expone `site_ids`/`device_group_ids` (IDs), pero
tanto `AuditLogModel.device` como `JobModel.device` son columnas `String` —
un nombre de device, no un FK. Ninguna de las dos puede filtrar
`.device.in_(scope.site_ids)` directamente; hace falta bajar el scope a
nombres concretos primero, con el mismo JOIN que hoy hace
`inventory_service.py: _visible_device_names()` (`DeviceModel` ↔
`DeviceGroupModel`, `site_id ∈ scope.site_ids OR device_group_id ∈
scope.device_group_ids`) — un JOIN real de 2 tablas que `Repository[Device]`
genérico no cubre, el mismo criterio de §2.2.1 que ya separó a las otras 4.
- `nombres_visibles(scope: VisibilityScope) -> set[str]` ← `inventory_service.py: _visible_device_names()` (mudada acá desde `Inventory`, que la llamaba como método "propio" pero en realidad hace un query de `Device`, no de `Inventory` — ver **(7)**)

**(7) Corrección sobre este mismo documento**: versiones anteriores de esta
sección daban por buena la fórmula "`AuditRepository`/`JobRepository` reciben
`VisibilityScope` y ya está" — cierto para el chequeo en memoria
(`rol_para()`), falso para estas dos consultas: ambas necesitan una consulta
SQL adicional (nombres de device) que `VisibilityScope` por sí sola no
resuelve. `device_service.py` pasa de "cero `.filter()` extra" (como se lo
había catalogado) a dueño de esa consulta — de ahí esta quinta subclase.

**`HuaweiVendor` / `CiscoVendor` / `MockVendor`** — "cómo se le habla realmente a
un Huawei/Cisco/al mock" — cada una sabe traducir `create_vlan`/`configure_port`/etc. a los comandos concretos de ese fabricante (o simularlos, en el caso de Mock).
**Corrección**: versiones anteriores de este documento decían "ya existen,
construidos Phase 1 de esta sesión" — **no es así**. Hoy, en el código real, son
**6 clases separadas**, no 3: `HuaweiVlanDriver(BaseVendorDriver)` +
`HuaweiPortDriver(BasePortDriver)`, `CiscoVlanDriver`+`CiscoPortDriver`,
`MockVlanDriver`+`MockPortDriver` — 1.827 líneas repartidas en los 6 archivos.
Fusionarlas en 3 (`HuaweiVendor`, etc.) es trabajo real pendiente, no algo ya hecho
por Phase 1 (esa fase construyó `Device`, no tocó los drivers).
- Implementan `VendorDriver` ← fusiona `vendors/huawei/vlan_driver.py: HuaweiVlanDriver`+`vendors/huawei/port_driver.py: HuaweiPortDriver` (y equivalentes Cisco/Mock) en una sola clase por vendor
- `_construir_inventario(device, password)` (privado) ← unifica `_build_inventory()`, hoy duplicada **literal, 4 veces** (`huawei/vlan_driver.py`, `huawei/port_driver.py`, `cisco/vlan_driver.py`, `cisco/port_driver.py` — mismo cuerpo de 5 líneas, solo cambian las constantes `_NETWORK_OS`/`_CONNECTION` de módulo, que ya son por-vendor)
- `_parsear_vlans()`/`_parsear_puertos()` (privados) ← `parsers/vlan_parser.py`/`parsers/port_parser.py` (Huawei), equivalentes Cisco. Ambos, y `cisco_port_parser.py`, llaman a `limpiar_lineas()` — función de módulo (no método, no depende de vendor) en un `parsers/_common.py` nuevo, que unifica `_clean()`+`_ANSI_ESCAPE` — hoy duplicados **literal, mismo docstring incluido**, en `port_parser.py` y `cisco_port_parser.py` (`_ANSI_ESCAPE` además en `vlan_parser.py`, 3 copias). **No confundir con `_is_physical_port()`** — mismo nombre en los 3 parsers pero lógica genuinamente distinta por vendor (prefijos IOS vs VRP), esa sí queda privada de cada parser, no se unifica
- `_formatear_vlans(lista)` (privado) ← `validators/port_validator.py: compress_vlans_huawei()`/`compress_vlans_cisco()` — **no es validación**, es formatear una lista de VLANs a la sintaxis CLI de cada vendor (ej. "10,20,30-35"), parte de cómo cada vendor ejecuta, no de si `Puerto` es válido. Antes vivía en un módulo de validators compartido; cada vendor tiene su propio formato, así que cada uno tiene su propia versión de este método — no se unifican entre sí porque el formato real difiere

**`JwtTokenIssuer`** — emite y valida los tokens de sesión (JWT, RF-ACC-01).
- `emitir(usuario)` ← `core/security.py: create_access_token()`
- `validar(token)` ← `core/security.py: verify_token()`
- `decodificar(token) -> dict` ← nuevo — unifica el núcleo que hoy está
  **triplicado**: `core/scope.py: get_current_user()`, `core/dependencies.py:
  get_current_user()` (se borra, ver Presentación) y `core/rls_middleware.py:
  _decode_authenticated_user()` (que en su propio comentario admite ser
  "same fallback as core.scope.get_current_user" — duplicado a sabiendas,
  porque middleware ASGI y `Depends()` de FastAPI no comparten código fácil).
  Decodifica el JWT, extrae `sub` → username, resuelve `is_system_admin` con
  el mismo fallback al claim legacy `role` en los 3 lugares. Cada llamador
  (`require_authenticated()`, `RLSSessionMiddleware`) sigue haciendo su propio
  wrapping (header manual vs `ContextVar`), pero el cuerpo deja de reescribirse

**`PasswordHasher`** — hashea/verifica contraseñas de **usuarios**
(`pbkdf2_sha256` vía `passlib`, one-way — **no bcrypt**, corregido en esta
revisión) — no confundir con `SecretVault`, que es para passwords de **devices**.
**Corrección**: `core/security.py: hash_password()`/`verify_password()` son
**código muerto** — grep confirma que nadie las importa. El hasheo real vive
inline en `user_service.py`, que define su **propio** `CryptContext` (mismo
scheme, instancia duplicada) y lo usa directo en `verify_password()`/
`update_password()`/`authenticate()`. No es un problema de seguridad (mismo
algoritmo en las dos, hashes compatibles) — es una `CryptContext` duplicada,
una de las dos sin uso.
- `hash(plano)` ← `user_service.py: update_password()` (uso inline de `_pwd_context.hash()`)
- `verificar(plano, hash)` ← `user_service.py: verify_password()`/`authenticate()` (uso inline de `_pwd_context.verify()`)

**`SecretVault`** — cifra/descifra passwords de **devices** (Fernet, reversible —
hace falta poder recuperarlo para conectarse al equipo). Ya existe (construida
esta sesión, `secret_service.py`): `encrypt()`/`decrypt()`. **Corrección**: el
shim de módulo `encrypt_password()`/`decrypt_password()` (llamado hoy desde
`device_service.py`/`inventory_service.py`/`port_service.py`) se elimina —
esos 3 pasan a recibir `SecretVault` inyectada, igual que `models/device.py`
ya hace bien hoy con el singleton `vault` (ver `Inventory` nota (5)).

**`RedisCoordinator`** (RNF-ESCAL-01: múltiples instancias del backend sin
cambiar la lógica de negocio, manteniendo consistencia — el estado vive en
Redis, no en memoria de un proceso, por eso N instancias pueden correr sin
pisarse; nunca citado) — evita que dos operaciones choquen: limita cuántos
requests por segundo entran por device, y bloquea un device mientras se lo está
configurando (con fallback a memoria local si Redis no responde). **No confundir
con `RateLimitMiddleware`** (Presentación, abajo) — son 2 rate-limiters reales y
distintos: este limita operaciones *por device* (Redis+fallback), el otro limita
*requests HTTP* por IP/usuario/login (en memoria, sin Redis) — mismo nombre
conceptual, tecnología y capa completamente distintas.

**Límite real heredado, no resuelto por este documento**: el fallback a
memoria local (`device_locks.py`/`rate_limiter.py`, mismo patrón en los dos)
resuelve Redis **una sola vez por proceso** (`_redis_checked`, cacheado) — si
Redis no responde en ese primer chequeo, el proceso queda fijado a
`threading.Lock`/contadores en memoria **para el resto de su vida**, sin
reintentar nunca. Eso rompe justo la garantía que `RedisCoordinator` dice
sostener: `RNF-ESCAL-01` pide "múltiples instancias del backend...
manteniendo consistencia", pero un lock en memoria de un proceso no lo ve
ninguna otra instancia — con 2+ instancias corriendo y una con Redis caído en
el arranque, esa instancia puede aplicar Ansible sobre el mismo device que
otra está tocando en simultáneo, la corrupción de configuración que el lock
existe para evitar, justo cuando Redis está degradado. `RedisCoordinator`
hereda este comportamiento tal cual de `device_locks.py`/`rate_limiter.py` —
este catálogo no lo corrige, lo deja como limitación conocida. Alternativas
reales, no implementadas: reintentar la conexión periódicamente en vez de
cachear `_redis_checked` para siempre; o fail-closed (rechazar con 503 en vez
de degradar a un lock que no protege entre instancias) — más conservador,
pero honesto con `RNF-ESCAL-01`. Requiere HA real de Redis (Sentinel/Cluster)
para resolverse de fondo, fuera del alcance de este documento.
- `limitar(device_id)` ← `rate_limiter.py: wait_for_slot()`
- `resetear(device_id=None)` ← `rate_limiter.py: reset()`
- `bloquear(device_id, timeout=None)` ← `device_locks.py: acquire()`. **Corrección
  ampliada** (la primera pasada solo había visto 2 de 4 usos reales): hoy no
  es la única puerta — `device_locks.acquire()`/`is_device_busy()` se llaman
  directo, sin pasar por acá, en **4 lugares** de `api/vlans.py`/`api/ports.py`:
  `api/vlans.py:63,84` (`acquire()`, lecturas en vivo de `GET /vlans`),
  `api/ports.py:209` (`acquire()`, mover VLANs de un trunk), y
  `api/ports.py:99-111` (`is_device_busy()` dentro de `_check_device_not_locked()`
  — un probe no-bloqueante *antes* de encolar, reusado por **3 endpoints**
  de escritura — configure/shutdown/enable port — para fallar rápido con 409
  en vez de encolar un job condenado). Los 4 pasan a recibir `RedisCoordinator`
  inyectado — un solo punto de entrada al lock, sea para leer en vivo, mover
  un puerto, o el probe previo a encolar
- `esta_ocupado(device_id)` ← `device_locks.py: is_device_busy()`

**`CleanupScheduler`** (Facade autocontenida, §2.11) — el barrendero: borra
artefactos viejos, refresh tokens vencidos e intentos de login antiguos, en un
job periódico.
- `purgar_artefactos(dias)` ← `cleanup_service.py: purge_old_artifacts()`
- `limpiar_refresh_tokens()` ← `cleanup_service.py: sweep_expired_refresh_tokens()`
- `limpiar_intentos_login(dias)` ← `cleanup_service.py: sweep_old_login_attempts()`
- `ejecutar_todo()` ← `cleanup_service.py: run_all()`

**`AnsibleAutomationEngine`** — la única clase que sabe que existe
`ansible_runner` — implementa `AutomationEngine` de verdad. `run(playbook, extravars, inventory)` ← `ansible_service.py: run_playbook()` (incluye `_mask_inventory()` como privado). **Corrección**: `validate_inventory()` — citada antes como "privada" — en realidad es pública (sin guión bajo) y **no se llama desde ningún lado del código actual**, código muerto, no se migra

**`CeleryJobQueue`** — la única clase que sabe que existe Celery — implementa
`JobQueue` de verdad. `dispatch(tarea, *args)` ← `worker.py` (`celery_app`) + los `_group_create_task.delay(...)` etc. de `vlan_execution_service.py`

**Por qué Celery y no un motor de orquestación más pesado** — Celery entrega
"al menos una vez", no "exactamente una vez"; el guard `job.esta_en_estado_terminal()`
(§2.4 nota (7)) existe **porque** elegimos Celery — un motor tipo Temporal.io/
AWS Step Functions da idempotencia de ejecución gratis, sin construirla a
mano. Evaluado y descartado: agregar un motor de orquestación es un servicio
más para correr/operar/monitorear, con curva de aprendizaje propia, para un
problema (N devices por operación, reintentos acotados) que Celery+Redis ya
resuelve con una pieza de código chica. Correcto para el tamaño de este
proyecto — pero el guard de idempotencia es el precio real y medible de esa
elección, no gratis.

**Otra alternativa real, en la otra dirección: sacar Celery/Redis del todo,
usar `asyncio` nativo de FastAPI** (`async def` + `BackgroundTasks`/
`asyncio.create_task()`) — menos infraestructura, un solo proceso, sin broker
separado. Se descarta sin dudar, no es una decisión cerrada: una tarea
`asyncio` vive **en memoria del proceso de la API** — si el servidor se
reinicia (deploy, crash) a mitad de una operación sobre un device, la tarea
se pierde **sin dejar rastro**, ni siquiera un `Job` en `"failed"`. Celery,
proceso separado con cola persistente en Redis, sobrevive a un restart del
servidor API — es justo lo que hace posible que `recuperar_huerfanos()`
(§2.4) tenga sentido: sin una cola durable, no habría "jobs huérfanos" que
recuperar, habrían desaparecido sin dejar Job alguno. RF-JOB pide que los
jobs sean consultables después de encolados, RNF-RES pide resiliencia —
Celery no es la opción más simple porque sí, es la única de las dos que no
pierde trabajo en un restart del servidor.

**Pero esa durabilidad depende de que Redis esté arriba** — y es el **mismo**
Redis (`settings.REDIS_URL`, `worker.py:8-9`) que usa `RedisCoordinator` para
locks/rate-limit (nota de límite real, arriba). No son 2 riesgos separados,
son 1 solo: si ese Redis cae, no solo el locking entre instancias se degrada
de forma insegura (arriba) — Celery tampoco puede encolar ni entregar jobs, la
propia garantía de "no perder trabajo en un restart" que se acaba de usar
para justificar Celery sobre `asyncio` deja de sostenerse. Un solo punto de
falla concentra 3 garantías distintas (durabilidad de jobs, locking
entre-instancias, rate-limit) — no es un problema nuevo de este diseño (el
código real ya comparte el mismo Redis), pero tampoco está nombrado en
ninguna parte de este documento como el riesgo único que en verdad es. HA de
Redis (Sentinel/Cluster) deja de ser "nice to have" y pasa a ser condición
para que 3 RNF distintos (`RNF-RES-01`, `RNF-ESCAL-01`, `RNF-SEG-03`) sigan
cumpliéndose bajo falla — fuera del alcance de este documento (es
infraestructura/despliegue), pero vale dejarlo dicho una sola vez en lugar de
3 veces repartido.

**`AuditListener`** — escucha cualquier `DomainEvent` y lo persiste como
`AuditRecord` — la implementación concreta de "auditar" en el patrón Observer.
`on_event(evento)` ← nuevo, envuelve lo que hoy es una llamada directa a `audit_service.append_audit_event()`

### Presentación (sin clases — referencia de dónde vive hoy cada middleware)

`core/scope.py: require_authenticated()/require_scope()/_enforce()` (RNF-SEG-02:
"toda operación realizada por una entidad autenticada y debidamente
autorizada" — es exactamente esta cadena, nunca citada) (RBAC) —
queda como `Depends()` de FastAPI, no se convierte en clase (§ Presentación
arriba). `require_scope(op)` deja de llamar a `effective_role()` por cada
recurso puntual — recibe `scope: VisibilityScope = Depends(obtener_scope)` (ya
calculado una vez para el request, ver `RoleAssignmentRepository.scope_de()`
en Infraestructura), resuelve `(site_id, group_id)` del recurso puntual con
`_resolve_scope()` (sin cambios), y llama `scope.rol_para(site_id, group_id)`
— en memoria, sin query nueva — antes de comparar contra `OP_MIN_ROLE` en
`_enforce()`. Esa descripción alcanza para 15+ de los 16 ops de `OP_MIN_ROLE`
— **`move_device` es la excepción, ver la 4ta corrección abajo**. **4
correcciones más encontradas en revisiones sucesivas, ninguna necesita clase
nueva:**

- `core/dependencies.py: get_current_user()` **se elimina** — era una segunda
  implementación del mismo JWT-decode que `core/scope.py: get_current_user()`
  ya hace, solo que sin el enriquecimiento de `require_authenticated()` (que sí
  revalida `is_active`/`is_system_admin` contra la DB). La usaba un único
  archivo, `api/group_jobs.py` — pasa a usar `Depends(require_authenticated)`
  como los otros 8. Hoy, por esta duplicación, un usuario desactivado con un JWT
  todavía vigente conserva acceso de lectura a `GET /group-jobs/{id}` — el único
  endpoint del sistema donde eso pasa.
- `core/scope.py: _lookup_device_scope()` **se elimina** — es la misma query,
  case por case idéntica, que `effective_role.py: _resolve_scope()` ya resuelve
  para `resource_type="device"` (JOIN `DeviceModel`↔`DeviceGroupModel`, filtro
  por nombre). `require_scope()` pasa a llamar `_resolve_scope()` directo en vez
  de reimplementarla.
- `core/rls_middleware.py: _decode_authenticated_user()` — **tercera**
  implementación del mismo decode-JWT-con-fallback (su propio comentario admite
  ser "same fallback as core.scope.get_current_user"). No se puede simplemente
  borrar como las anteriores — corre en un middleware ASGI (`BaseHTTPMiddleware`),
  no en un `Depends()` de ruta, así que sigue siendo un lugar distinto de
  llamarla. Pasa a llamar `JwtTokenIssuer.decodificar(token)` (§1.6, Infraestructura
  — seguridad) para el núcleo compartido, y mantiene su propio wrapping
  (`_lookup_user_id()`, la resolución a `users.id` para el GUC de Postgres).
- **`move_device` — dispatch dual perdido en el rediseño de `require_scope()`
  de arriba, encontrado en esta revisión.** El código real (`core/scope.py:
  180-246`) no trata `move_device` como un `rol_para(site_id, group_id)`
  puntual: es un op sintético que primero decide si el movimiento es
  same-site o cross-site (comparando el site del device origen contra el
  site del `device_group_id` destino del body) y **recién ahí** aplica uno de
  dos umbrales distintos —
  `move_device_same_site` (`OP_MIN_ROLE`: `operator`, un solo
  `scope.rol_para(site_id_origen, None)`) vs.
  `move_device_cross_site` (`OP_MIN_ROLE`: `admin`, **dos** chequeos —
  `scope.rol_para(site_id_origen)` **y** `scope.rol_para(site_id_destino)`,
  D16: toma el mínimo de los dos). La versión de `require_scope()` descripta
  arriba pierde esto — un solo `scope.rol_para()` no alcanza para decidir
  cuál de los dos umbrales aplica, porque el umbral depende de comparar
  origen contra destino, no de un único recurso. `require_scope("move_device")`
  sigue necesitando su propia rama (como hoy `_authorize_move_device()`),
  simplemente reemplaza sus llamadas a `effective_role()` por
  `scope.rol_para()` — sin clase nueva, es lógica de dispatch, no estado.

### Infraestructura — seguridad (agregado en esta revisión)

**`RLSSessionMiddleware`** (`core/rls_middleware.py`) — MSP Fase 6, **ya existe**,
con migración real (`migrations/versions/f6msp5_msp_rls.py`) y test dedicado
(`test_msp_rls.py`); no estaba en ningún lado de este documento. Es una segunda
capa de control de acceso, a nivel de base de datos, en paralelo a todo el
trabajo de `effective_role`/`VisibilityScope` (aplicación) catalogado arriba —
**no lo reemplaza, es defensa en profundidad**: si algún día un endpoint nuevo
se olvida de aplicar `VisibilityScope` correctamente, las policies RLS de
Postgres (deny-by-default) igual filtran las filas en la DB.

**Alternativa real considerada y descartada: que RLS haga todo el trabajo de
autorización, sacando `VisibilityScope`/`effective_role` de la capa de
aplicación.** Si RLS ya filtra filas deny-by-default, ¿para qué mantener
lógica de autorización en 2 capas? No funciona, por una razón concreta, no
solo preferencia: RLS filtra **filas que ya se están leyendo de la DB** —
pero varias decisiones de este sistema son sobre **acciones**, no sobre
filas. La regla D25 (`RoleAssignmentService`, "un group-admin no puede
delegar roles") es sobre *quién puede escribir qué*, no expresable como
predicado de fila — no hay tabla que filtrar, es una decisión antes de que
exista ningún row. Y sin capa de aplicación, un 403 claro con mensaje se
convierte en "0 filas, sin explicación" — peor API (RNF-API implícito de
usabilidad). RLS se queda como red de seguridad, no como mecanismo primario.
- Middleware ASGI (`BaseHTTPMiddleware`), no `Depends()` — corre antes de rate
  limiting/CORS (`app.add_middleware(RLSSessionMiddleware)` en `main.py`)
- `dispatch(request, call_next)` — decodifica el JWT, guarda `{id, is_system_admin}`
  en un `ContextVar` (`current_user_ctx`) por la duración del request
- Un hook `after_begin` de SQLAlchemy (`install_session_rls_hook`, instalado una
  vez desde `init_db`) lee ese `ContextVar` y ejecuta `SET LOCAL app.user_id`/
  `app.is_system_admin` en **cada transacción** de Postgres — las policies RLS
  leen esas 2 GUCs. `SET LOCAL` es transaccional, no se filtra entre requests
  aunque la conexión vuelva al pool
- `system_context()` — context manager para código interno confiable (bootstrap,
  workers de Celery sin JWT) que fuerza `is_system_admin=true` en el hook
- Default sin contexto seteado: `user_id=0`, `is_system_admin=false` — toda
  policy deniega. SQLite no tiene RLS; el hook detecta el dialecto y es no-op
  ahí, así que dev/tests siguen funcionando sin Postgres

**`RateLimitMiddleware`** (`core/rate_limit_middleware.py`) — **ya existe**, no
estaba documentada. RNF-SEG-03: "mecanismos de protección para prevenir
abusos de uso y sobrecarga de peticiones por usuario/IP" — es literalmente
esto, nunca citado. Rate-limit a nivel HTTP (sliding window en memoria, no
Redis) — distinto de `RedisCoordinator.limitar()` (ver nota ahí arriba).
- Middleware ASGI, corre después de `RLSSessionMiddleware` en el stack real de
  `main.py` (`RLSSessionMiddleware` → `RateLimitMiddleware` →
  `TransportSecurityMiddleware` (condicional) → `CORSMiddleware`)
- Clave distinta según el request: `login:{ip}` en `/api/v1/auth/login` (protege
  contra brute-force, complementa a `LoginAttemptRepository` — ese cuenta
  fallos por usuario/IP para bloquear login, este limita el *rate* de intentos
  independientemente de si fallan o no), `user:{username}` si hay JWT válido,
  `ip:{ip}` si no — 429 con header `Retry-After` cuando excede el límite

**`TransportSecurityMiddleware`** (RNF-SEG-01: confidencialidad de datos en
tránsito — nunca citado) — hoy son 2 clases (`HSTSMiddleware`/
`HTTPSRedirectMiddleware`, `core/tls_middleware.py`), ambas triviales (~15-20
líneas), siempre desplegadas juntas, mismo propósito (forzar TLS). Se fusionan
en una — agregar cabecera HSTS y redirigir HTTP→HTTPS (con detección de
`X-Forwarded-Proto` detrás de proxy) son 2 pasos del mismo `dispatch()`, no 2
razones distintas para cambiar la clase. Sin motivo real para mantenerlas
separadas a este tamaño.

---

## 2. Patrones — desarrollo con ejemplo antes/después

### 2.1 Value Object autovalidado

**Antes** (validación separada del objeto, se puede olvidar):
```python
# api/vlans.py
vlan_validator.validate_vlan_id_range(vlan.vlan_id)
vlan_validator.validate_vlan_not_reserved(vlan.vlan_id)
vlan_validator.validate_vlan_name(vlan.name)
# si CUALQUIER otro código construye una VLAN sin pasar por acá, no se valida nada
```

**Después**:
```python
class VLAN:
    def __post_init__(self):
        validate_vlan_id_range(self.vlan_id)
        validate_vlan_not_reserved(self.vlan_id)
```

**Por qué ayuda de verdad**: no es "más prolijo" — es que ahora es **imposible** que exista una instancia de `VLAN` con `vlan_id` fuera de rango en cualquier parte del sistema (rollback, tests, un endpoint nuevo que alguien agregue el año que viene). Antes, alcanzaba con que un solo caller se olvidara de llamar al validator.

---

### 2.2 Repository genérico (por configuración, no por subclase)

**Antes** — este bloque se repite ~50 veces en `device_service.py`, `job_service.py`, `site_service.py`, etc.:
```python
def get_device(name: str) -> Device | None:
    with get_session() as session:
        row = session.query(DeviceModel).filter_by(name=name).first()
        if not row:
            return None
        return _to_domain(row)

def get_job(id: str) -> Job | None:
    with get_session() as session:
        row = session.query(JobModel).filter_by(id=id).first()
        if not row:
            return None
        return _to_job(row)
# ...×8 servicios, mismo esqueleto, solo cambia el modelo y el mapper
```

**Después** — una sola clase, parametrizada por lo único que cambia:
```python
class Repository(Generic[T]):
    def __init__(self, orm_model, to_domain: Callable):
        self._orm_model = orm_model
        self._to_domain = to_domain
    def get(self, id) -> T | None:
        with get_session() as session:
            row = session.query(self._orm_model).filter_by(id=id).first()
            return self._to_domain(row) if row else None
    def add(self, entidad: T) -> T:
        with get_session() as session:
            session.merge(self._to_orm(entidad))   # upsert por PK -- ver nota abajo
            session.flush()
            return entidad

device_repo = Repository[Device](DeviceModel, _device_to_domain)
job_repo    = JobRepository(JobModel, _job_to_domain)
```

**`add()` es upsert, no solo insert — corrección real, no obvia por el nombre.**
El contrato de `Repository[T]` (§2.2.1) es 4 métodos: `get`/`add`/`list`/`remove`
— **no hay `update()`**. Eso significa que cualquier mutación sobre una
entidad ya persistida (`Site.renombrar()`, `DeviceGroup.renombrar()`, la
propia `job.marcar_completado()` de arriba, o `VLAN.aplicar()` idempotente
volviendo a llamar `repo_vlan.add(vlan)` sobre una VLAN que ya existía)
**tiene que pasar por `add()` de nuevo** sobre una fila que ya está en la DB.
Con `session.add()` (insert puro), la 2da llamada sobre la misma PK
rompería con un duplicate-key — justo en el caso que RNF-API-05
(idempotencia) más necesita que funcione sin error. `session.merge()`
(insert-si-nueva, update-si-existe-por-PK) es la primitiva genérica de
SQLAlchemy para esto — no hace falta lógica por entidad, ni una subclase
(no es un JOIN ni un filtro combinado, el criterio de §2.2.1 no aplica
acá): es el mismo cambio para 12 de las 13 instancias. Este comportamiento
estaba **implícito** en todo el documento (varias entidades dependen de que
`add()` actualice, no solo cree) pero nunca se había hecho explícito hasta
esta revisión. **Excepción real, no hipotética**: `AuditRecord` — un
`merge()` sobre un `id` repetido sobreescribiría en silencio un registro de
auditoría, justo lo que RNF-AUD/§6.5 del SRS prohíbe ("a prueba de
manipulaciones"). `AuditRepository.append()` no delega a este `add()` — usa
insert estricto (ver §1.6, `AuditRepository`).

**Por qué ayuda de verdad**: el bug de "me olvidé de cerrar la sesión" o "el filtro está mal" se arregla **una vez**, no 8 veces. Hoy, si `device_service.get_device` tiene un bug de sesión, `job_service.get_job` puede tener ese mismo bug arreglado de forma distinta (o sin arreglar).

**Alternativa real considerada y descartada: Active Record.** En vez de
`Device` (dataclass) + `DeviceModel` (SQLAlchemy) + `_to_domain()` mapeando
entre las dos, una sola clase — `DeviceModel.create_vlan()` directo sobre el
modelo ORM, sin capa de dominio separada. Es lo que hacen Rails/Django por
default — menos código, menos capas, y para el tamaño de este proyecto
probablemente alcanzaría funcionalmente. Se descarta, con evidencia concreta
ya probada en esta sesión: los 18 tests de `Device` (Phase 1) inyectan un
driver falso y corren **sin tocar la DB para nada**, porque `Device` no
hereda de nada de SQLAlchemy. Con Active Record, testear `create_vlan()`
necesitaría una sesión de DB real o mockear SQLAlchemy — mucho más difícil de
aislar. Para un documento que apunta explícitamente a "todas las mejores
prácticas de OOP", la separación hexagonal es más defendible que Active
Record aunque sea objetivamente más código — el costo (una clase más por
entidad, un mapper) es real, no gratis, y vale la pena decirlo así.

---

### 2.2.1 ¿Cuántas subclases de `Repository[T]` van a terminar habiendo?

Pregunta válida — van 5 (`AuditRepository`, `RoleAssignmentRepository`,
`LoginAttemptRepository`, `JobRepository`, `DeviceRepository`), encontradas una
por una en revisiones sucesivas de este documento. Antes de que parezca una
tendencia sin freno, dos cosas que hay que separar:

**"Por configuración, no por subclase" (título de §2.2) sigue siendo cierto para
el 99% del catálogo** — es la regla para el CRUD básico: agregar una entidad nueva
(`Site`, `Usuario`, `VLAN`, `Puerto`, `InterfazVirtual`,
`ConfiguracionGlobal`, `RefreshToken`...) es **una línea**,
`Repository[X](XModel, _x_to_domain)`, cero clases nuevas. Eso es lo que evita el
problema real que tenía B (12 `Repository` como subclases, una por entidad, sin
motivo).

**La subclase aparece solo cuando el genérico `get/add/list/remove` no alcanza** —
y el criterio es verificable, no una decisión de gusto: ¿esta entidad necesita un
JOIN entre tablas, o un filtro que agrupe/cuente/compare contra un umbral, que un
`filter_by(**kwargs)` de una sola tabla no puede expresar? Las 5 que sí lo
necesitan tienen, cada una, código real y medido que lo prueba (§6.1): filtros
combinados de auditoría (114 líneas), visibilidad por rol (245 líneas en 4
archivos), ventana de tiempo para bloqueo de login, filtros+paginación de jobs, y
el JOIN device↔group↔site que `nombres_visibles()` (`DeviceRepository`) resuelve
— esta última no vino de una función vieja, vino de revisar que `VisibilityScope`
por sí sola no alcanzaba para lo que `AuditRepository`/`JobRepository`
prometían hacer con ella. No son decisiones arbitrarias — son 5 casos donde
ya existía (o hacía falta) una consulta que un `filter_by()` de una tabla no
puede expresar.

**Verificación de que no sigue creciendo**: se revisaron los servicios CRUD
restantes — `user_service.py`, `group_job_service.py` tienen
cero `.filter()` más allá de `filter_by(id=...)` — y `site_service.py`/
`device_group_service.py`, que sí tienen `.filter()` extra, pero son *chequeos de
unicidad* de un solo campo ("¿ya existe un site con este nombre?"), no consultas
combinadas — encajan en un método genérico más (`Repository[T].existe(**criterio)`,
un `EXISTS` de una tabla) sin necesitar subclase. **`device_service.py` se
corrige** — se había marcado como "cero `.filter()` extra" en una revisión
anterior, pero `nombres_visibles()` (arriba) sí necesita un JOIN real que
`Repository[Device]` genérico no cubre — pasa a `DeviceRepository`, la 5ta
subclase.

Contando todo lo que este catálogo respalda con un `Repository[T]` — las 10
entidades de §1 (`GroupJob` ya no es una, §2.9) + `AuditRecord` (VO) +
`LoginAttempt`/`RefreshToken` (podadas a dato simple, pero igual persistidas)
= **13 instancias en total. 5 son subclase, 8 quedan en `Repository[T]`
genérico sin una línea propia** (`Site`, `DeviceGroup`, `Usuario`,
`VLAN`, `Puerto`, `InterfazVirtual`, `ConfiguracionGlobal`, `RefreshToken`).
No es una tendencia — es un techo real, ya verificado contra el código que
queda, no una proyección.

---

### 2.3 Strategy (vendor drivers) — ya existe, es el ejemplo de que el patrón funciona

**Hoy, sin el patrón**, sería:
```python
def create_vlan(vlan_id, name, device):
    if device.vendor == "huawei_vrp":
        # comandos VRP...
    elif device.vendor == "cisco_ios":
        # comandos IOS...
    elif device.vendor == "juniper":   # agregar un vendor = tocar ESTA función
        ...
```

**Con el patrón** (lo que ya hay en `vendors/{huawei,cisco}/vlan_driver.py`):
```python
class HuaweiVendor(VendorDriver):
    def create_vlan(self, vlan_id, name, device, password): ...

# agregar Juniper = una clase nueva, CERO líneas tocadas en código existente
class JuniperVendor(VendorDriver): ...
```

**Por qué ayuda de verdad**: es el único lugar del código actual donde ya se ve esto — agregamos vendors nuevos sin locura de `if/elif` creciendo para siempre. Sirve de prueba de que el resto de los patrones, bien aplicados, dan el mismo resultado.

---

### 2.4 `RecursoGestionable` + `Orquestador` (Template Method por composición, no herencia)

**Antes** — la lógica de rollback se repite (con variaciones) en 3 funciones distintas de `vlan_execution_service.py`: `_rollback_create`, `_rollback_delete`, `_rollback_update` — cada una ~40 líneas casi iguales (log → `job_service.update_job(current_step="rollback_started")` → ejecutar → verificar → log resultado).

**Después**:
```python
class Orquestador:
    def ejecutar(self, recurso: RecursoGestionable, device_name, actor, job):
        if job.esta_en_estado_terminal():                # (7) reentrega de Celery,
            return                                          # no un error -- no-op
        device = self._device_repo.get(device_name)     # el único lugar donde
                                                            Orquestador toca algo
                                                            que no sea el contrato
                                                            RecursoGestionable
        if device is None:
            raise NotFoundError(device_name)

        pre_state = None
        try:
            job.marcar_iniciado()                          # (10)
            self._jobs.add(job)                            # (10) upsert -- ver §2.2
            with self._coordinador.bloquear(device_name):  # (12)
                recurso.validar()
                pre_state = recurso.reconciliar(device)       # → device.driver.get_vlans(...) adentro
                resultado, retry_count = self._ejecutar_con_retry(     # (11)
                    lambda: recurso.aplicar(device), job.job_id, device_name,
                )
                if resultado.get("rc", 0) != 0:
                    raise DeviceExecutionError(resultado.get("stderr") or resultado.get("stdout") or "Execution failed")  # (11)
        except Exception as error:
            rb_performed, rb_success = (False, None)
            if pre_state is not None:
                with self._coordinador.bloquear(device_name):      # (12) -- se re-adquiere para el rollback
                    rb_performed, rb_success = self._rollback(recurso, pre_state, device)  # (11)
            job.marcar_fallido(str(error), rb_performed, rb_success)              # (8)(9)
            self._jobs.add(job)                                                    # (10)
            self._eventos.despachar([DomainEvent("recurso_fallido", recurso, device, actor, {"error": str(error), "rollback_performed": rb_performed, "rollback_success": rb_success}, exitoso=False)])
            raise
        else:
            self._repos[recurso.repositorio()].add(recurso)   # (5)
            job.marcar_completado(resultado)                  # (8)
            self._jobs.add(job)                                # (10)
            self._eventos.despachar([DomainEvent("recurso_aplicado", recurso, device, actor, resultado)])
        finally:
            if not job.esta_en_estado_terminal():              # (11) red de seguridad final
                job.asegurar_estado_final()
                self._jobs.add(job)
```

Notá que `Orquestador` ya no recibe `PluginRegistry`/`AutomationEngine` — no le
hacen falta, `Device` ya resuelve su propio driver (Phase 1 de esta sesión).
`Orquestador` conoce `Repository[Device]`, `dict[str, Repository]`,
`RecursoGestionable`, `JobRepository`, `EventDispatcher` y `RedisCoordinator`
(este último, nota (12)).

**(6) Por qué `aplicar()` alcanza, sin agregar 2 métodos más al contrato**
(RNF-API-05: "las operaciones deben ser idempotentes cuando corresponda" —
esta nota, `Job.esta_en_estado_terminal()` (nota 7) e `Inventory.move()`'s
no-op auditado son, sin haberlo buscado a propósito, exactamente este
requisito — nunca citado hasta ahora en ninguno de los 3 lugares) —
`orchestration_runner.py` real (366 líneas) tiene, además de validar/capturar
estado/ejecutar, dos fases más: verificar contra el `pre_state` si la operación
ya está aplicada (idempotencia — ejemplo real, `vlan_execution_service.py:
340-372`: crear una VLAN que ya existe con el mismo nombre no debería tocar el
device) y verificar después de ejecutar que el cambio prendió. Dos formas de
resolverlo: (a) 2 métodos más en `RecursoGestionable` con default heredado, o
(b) que `VLAN.aplicar(device)` internamente decida si hace falta aplicar algo y
cómo confirmarlo, sin exponerlo como fase separada del contrato. Se eligió (b) —
mismo comportamiento, contrato más chico (3 métodos + `repositorio()`, no 6),
cada recurso dueño completo de "cómo se aplica a sí mismo" en vez de repartido
en varios puntos de entrada que `InterfazVirtual`/`ConfiguracionGlobal` (que no
necesitan estas fases) tendrían que aceptar igual, aunque sea con default.

**(7) Guard de idempotencia contra reentregas de Celery** — Celery entrega "al
menos una vez": en un fallo raro (worker crashea después de terminar pero
antes de confirmar al broker), `orquestador.ejecutar()` puede correr 2 veces
para el mismo `job`. En la 2da entrega, `recurso.aplicar(device)` ya está
protegido por la idempotencia de (6) (no vuelve a tocar el device real), pero
sin este guard, la actualización final del `Job` (`marcar_completado()`) se
ejecutaría sobre un Job que **ya está terminal** — `_TRANSICIONES_VALIDAS`
(§2.8) lo rechaza con `TransicionInvalidaError`, pensada para detectar bugs de
negocio (cancelar un Job ya terminado), no reentregas de infraestructura. Sin
distinguir los dos casos, esa excepción se propaga, crashea la tarea de
Celery, y según la política de reintentos puede volver a entregarse — mismo
problema, en loop. El guard corta el caso ANTES de hacer cualquier trabajo
(ni siquiera vuelve a consultar el device en vivo): `job.esta_en_estado_terminal()`
usa el mismo criterio que ya tiene `_TRANSICIONES_VALIDAS` (estados con
transiciones vacías = terminal), no es una regla nueva.

**(8) El camino de falla estaba incompleto** — versiones anteriores de este
ejemplo re-lanzaban después de `_rollback()` sin marcar el `Job` ni auditar
la falla. Dos problemas reales: el `Job` quedaba `"running"` para siempre
(hasta el próximo restart, único momento en que corre `recuperar_huerfanos()`
— RF-JOB-03 roto), y ninguna falla real dejaba rastro vía
`EventDispatcher`/`AuditListener` (solo el éxito despachaba — RF-AUD-01 roto
para el caso que más importa auditar). Mismo `DomainEvent` genérico, otro
`tipo` (`"recurso_fallido"`) — no hace falta clase nueva, `AuditListener` ya
escucha cualquier tipo de evento.

**(9) El propio arreglo de la nota (8) reintroducía una versión más chica del
mismo problema, encontrado revisando el ejemplo canónico contra el código
real que dice unificar.** `self._rollback(recurso, pre_state, device)` se
llamaba como sentencia suelta, con el resultado descartado — pero el código
real que `_rollback()` unifica (`vlan_execution_service.py: _rollback_create()`
y las 9 funciones equivalentes) no devuelve `None`: devuelve
`(rollback_performed, rollback_success)`, un tri-estado que
`job_service.update_job()` persiste y que `api/jobs.py: _format_job()` ya
expone hoy en `execution_summary`. Con el resultado descartado, `Job.rollback_performed`/
`rollback_success` quedan siempre en su default (`False`/`None`) sin importar
qué haya pasado realmente en el device — un cliente que pregunta "¿se
revirtió el cambio fallido?" recibe siempre la misma respuesta falsa. La
buena noticia, verificada contra las 10 funciones reales: ninguna de ellas
deja escapar una excepción — cada una tiene su propio `try/except Exception`
interno, incluso alrededor de la verificación posterior al rollback
(`vlan_execution_service.py:159-167`) — así que `_rollback()` nunca puede
tirar abajo a `job.marcar_fallido()` como si pudiera pasar sin querer; el
problema no era robustez, era simplemente no leer el valor de retorno.
Arreglado: `_rollback()` documentado con su tipo de retorno real, y
`marcar_fallido()` recibe los 3 datos.

**(10) Hueco más grande, encontrado cuestionando el diseño de clases, no en
código viejo: `Orquestador` mutaba `Job` sin tener con qué persistirlo.**
`Orquestador` (§1, tabla de colaboradores) no tenía `JobRepository`/
`Repository[Job]` inyectado — solo `Repository[Device]`, `dict[str,
Repository]` por recurso, y `EventDispatcher`. Pero `job.marcar_completado()`/
`marcar_fallido()` estaban documentados como reemplazo directo de
`job_service.py: update_job(...)` (línea ~1894, más abajo) — una escritura
real a la DB. `Job` es un dataclass plano, sin sesión propia (mismo criterio
que `Device` — Active Record se descartó explícitamente en §2.2), así que sin
un repositorio inyectado esas llamadas solo mutaban el objeto en memoria: el
cambio se perdía al terminar la función, y la fila real en la DB quedaba
como estaba. Esto deshacía en silencio el arreglo de las notas (8)/(9) — el
`Job` real seguía en `"running"` para siempre, exactamente el RF-JOB-03 que
creíamos haber cerrado. Segundo problema relacionado: `marcar_iniciado()`
(§1, ya catalogado) no se llamaba **en ningún lado** del flujo canónico — el
código real sí transiciona a `"running"` al arrancar (`orchestration_runner.py:159`),
la versión anterior de este ejemplo lo omitía. Arreglado arriba: `Orquestador`
recibe `_jobs: JobRepository` (mismo colaborador que ya tiene
`GroupOperationRunner`), llama `marcar_iniciado()` al principio, y persiste
con `self._jobs.add(job)` después de cada transición — 3 puntos, no 1, porque
`Job` puede terminar en 3 lugares distintos del método (arranque, falla,
éxito).

**(11) Dos huecos más, encontrados comparando este ejemplo contra
`orchestration_runner.py: run_operation()` real (367 líneas) al armar el plan de
migración — no en código viejo que "todavía no se implementó", en la propia
lógica que este ejemplo dice reemplazar.**

**Primero: no había reintentos.** `Orquestador._ejecutar_con_retry()`/
`_clasificar_error()` están catalogados (§1.6) y citados como "absorbiendo
`retry_policy.py`" — pero el ejemplo canónico nunca los llamaba, hacía
`resultado = recurso.aplicar(device)` una sola vez. El real
`_execute_with_retry()` (`vlan_execution_service.py:76-114`) llama a la función
de ejecución **hasta `max_retries+1` veces**, con backoff exponencial, si
`_clasificar_error()` dice que el error es transitorio (`rc` de Ansible ∈
{4,6,255}, o un patrón de texto tipo "timeout"/"connection reset") — y **nunca
relanza**: atrapa cualquier excepción de la función que ejecuta y la normaliza a
un `{"rc": 1, "stderr": str(exc)}`, devolviendo siempre `(resultado, retry_count)`
al que llama. Fix: `recurso.aplicar(device)` se llama envuelto en
`self._ejecutar_con_retry(lambda: recurso.aplicar(device), job.job_id,
device_name)` — con esto, un error transitorio (switch momentáneamente
inalcanzable) se reintenta solo, en vez de fallar al primer intento.

**Segundo, relacionado: como `_ejecutar_con_retry()` nunca relanza, hace falta
convertir `rc != 0` en una excepción a mano** (`raise
DeviceExecutionError(...)`) para que seguir usando `except Exception` como único
punto de manejo de falla siga funcionando — **esto también resuelve, sin
buscarlo, una inconsistencia real entre el `.puml` y este mismo código Python que
quedó sin cerrar en una revisión anterior**: el `.puml` mostraba `VLAN.aplicar()`
retornando limpio y `Orquestador` chequeando `alt resultado.rc != 0` **después**
del retorno — el código Python de acá mostraba un `try/except` alrededor de
`aplicar()`, como si lanzara. Los 2 tenían razón a medias: `aplicar()` (vía el
driver) **sí** puede devolver un `resultado` con `rc != 0` sin lanzar (eso es lo
que hace `_ejecutar_con_retry()` que la envuelve) — pero `Orquestador` lo
convierte en excepción **acá mismo**, un solo lugar, para no duplicar el manejo
de falla entre "aplicar() lanzó" (ej. `validate_name()` fallando) y "aplicar()
devolvió rc≠0" (ej. el device rechazó el comando). El `.puml` queda correcto tal
como está — no hace falta tocarlo de nuevo.

**Tercero: sin un `finally` con red de seguridad, una falla fuera de la llamada a
`aplicar()` (ej. `recurso.validar()`, o la propia `reconciliar()` — una lectura
en vivo real, que puede fallar) dejaba el `Job` pegado en `"running"` sin que
`marcar_fallido()` corriera nunca** — el `except` de arriba solo cubría lo que
pasara **dentro** del `try`, y antes de este arreglo el `try` envolvía solo
`aplicar()`. El real `run_operation()` tiene un `finally:
job_service.ensure_final_state(job_id)` envolviendo **toda** la función,
exactamente para este caso. Fix: el `try` ahora empieza en `marcar_iniciado()`
(cubre `validar()`/`reconciliar()` también), y un `finally` con
`job.asegurar_estado_final()` (§2.8, la única excepción deliberada a
`_transicionar()`) queda como red de seguridad final — solo actúa si, por lo que
sea, el `Job` no llegó a un estado terminal ni por el camino de éxito ni por el
de falla.

**(12) Cuarto hueco, encontrado preguntando qué pasa si la llamada al driver
tarda varios segundos — no en código viejo, en el propio razonamiento sobre
este ejemplo.** `recurso.reconciliar(device)`/`recurso.aplicar(device)` son
llamadas de red reales (SSH/Ansible contra el switch), de varios segundos cada
una, más lo que sume `_ejecutar_con_retry()` con backoff si hay reintentos. El
ejemplo canónico, hasta esta revisión, no tenía ningún mecanismo que impidiera
que **dos ejecuciones concurrentes sobre el mismo device** (dos operaciones de
grupo distintas, o un reintento superpuesto con otro job) corrieran esa
ventana en paralelo — nada análogo a lo que hace el código real
(`orchestration_runner.py:154`, `with device_locks.acquire(device):`,
envolviendo desde el `update_job(..., "running")` hasta el rollback inclusive).
El propio catálogo ya tenía `RedisCoordinator.bloquear()` diseñado (Fase 1 de
la migración, reemplaza a `device_locks.py`) y hasta aparecía listado como
colaborador de `GroupOperationRunner` en la tabla de §1 — pero **nunca se
llamaba en ningún lado**: `GroupOperationRunner.encolar()` solo encola (es
síncrono y rápido, no ejecuta nada sobre el device), así que sostener el lock
ahí no protege nada; la ejecución real ocurre después, en otro proceso
(worker de Celery), dentro de `Orquestador.ejecutar()` — que nunca recibía
`coordinador` como colaborador. Corregido: `RedisCoordinator` se mueve de
`GroupOperationRunner` a `Orquestador` (única clase que efectivamente toca el
device), y `validar()`/`reconciliar()`/`aplicar()` corren dentro de
`self._coordinador.bloquear(device_name)`. El rollback se re-adquiere el lock
en un segundo `with` en vez de compartir el primero (evita anidar un segundo
`try/except` dentro del `with`, manteniendo el único punto de manejo de falla
que ya estableció la nota (11)) — deja una ventana breve, entre soltar el lock
de la operación fallida y volver a tomarlo para revertir, donde en teoría otra
ejecución podría colarse antes que el rollback; el mismo riesgo, acotado a esa
ventana en vez de a los varios segundos completos de `aplicar()`, se acepta
como trade-off documentado en vez de forzar una estructura de excepciones
anidada solo para cerrarlo del todo. Importante: esto **no** es el mismo tipo
de problema que "el `Device` puede estar obsoleto" — `device` se lee una sola
vez al principio de `ejecutar()` y nunca se reescribe en este flujo (ni
`VLAN`/`Puerto` tocan campos de `Device`), así que no hay carrera de
lectura/escritura sobre la fila de `Device` en la DB. El riesgo real es sobre
el **device físico** — dos llamadas Ansible pisándose sobre el mismo switch —,
exactamente lo que `RedisCoordinator` existe para evitar.

**Por qué ayuda de verdad — y por qué NO es herencia**: si en cambio `VLAN`/`Puerto`/`InterfazVirtual`/`ConfiguracionGlobal` heredaran de una clase base con `commit()` (la idea de `ManagedResource`/`DeviceOperation`), tendrías 1 clase base + 1 subclase concreta por cada una de las ~30 operaciones del SRS completo. Con contrato (`RecursoGestionable`, 4 métodos que cada recurso implementa) + motor genérico (`Orquestador`, que no sabe qué es una VLAN), el rollback centralizado se logra igual, pero `Orquestador` es **una sola clase para las 30 operaciones**, no 30 subclases.

**Alternativa real considerada y descartada: Command en vez de Template
Method.** En vez de que `Orquestador` decida la secuencia
(`validar→reconciliar→aplicar`) y cada recurso solo implemente los pasos,
cada operación podría ser un objeto `Comando` (`CrearVlanCommand`) con un
único método `ejecutar(device)` que decide **su propia** secuencia interna —
`Orquestador` se reduciría a "correr este comando con lock/retry/rollback
alrededor", sin saber que existen 3 fases. Se descarta: el Template Method
actual **garantiza estructuralmente** que ningún recurso pueda saltarse
`reconciliar()` (capturar `pre_state`, el Memento) antes de `aplicar()` — si
lo hiciera, no habría con qué hacer rollback. Con Command, esa garantía pasa
a ser disciplina de cada comando — exactamente el problema de origen de este
proyecto ("confiar en que cada función se acuerde de auditar/capturar
estado"). Como el rollback correcto depende de que `pre_state` **siempre**
exista, Template Method es más seguro acá — pero es una decisión real entre
2 patrones válidos, no la única forma posible de resolverlo.

**¿Por qué `vlan.aplicar(device)` no es indirección de sobra?** — es tentador pensar
"si total `VLAN.aplicar()` solo llama a `device.driver.create_vlan(...)`, que
`Orquestador` le pegue directo y ahorramos el paso". El problema es
que `Orquestador` maneja **varios tipos de recurso** (`VLAN`, `Puerto`,
`InterfazVirtual`, `ConfiguracionGlobal`, y lo que se agregue después) — sin
`aplicar()`, alguien tiene que decidir a qué método del driver llamar según el
tipo, y esa decisión termina siendo un `if/elif` en algún lado:

```python
# sin RecursoGestionable, Orquestador necesitaría esto:
if isinstance(recurso, VLAN):
    resultado = device.driver.create_vlan(recurso.vlan_id, recurso.name, device, device.password)
elif isinstance(recurso, Puerto):
    resultado = device.driver.configure_port(recurso, device, device.password)
elif isinstance(recurso, InterfazVirtual):
    resultado = device.driver.crear_interfaz(recurso, device, device.password)
# agregás "Route" el año que viene → volvés a tocar este método
```

**(5)** El mismo `if/elif` que este ejemplo evita para `aplicar()` reaparecía, sin
querer, en la línea `self._repo_de(recurso).add(recurso)` de más arriba —
`_repo_de` también necesita saber si `recurso` es una `VLAN` o un `Puerto` para
devolver el `Repository` correcto. Se corrige con el mismo criterio: `VLAN`/
`Puerto`/`InterfazVirtual`/`ConfiguracionGlobal` implementan `repositorio() -> str`
(4ta parte del contrato `RecursoGestionable`, ej. `VLAN.repositorio() → "vlan"`), y
`Orquestador` guarda un `dict[str, Repository]` en vez de un método con `if`. Es
el mismo mecanismo que ya usa `PluginRegistry` para vendors, aplicado acá a
persistencia en vez de a drivers.

Exactamente el `if/elif` que ya evitamos en `PluginRegistry` (vendors) y en
`ManagedResource` (recursos) — solo que reaparecería acá si sacamos `aplicar()`. Con
`recurso.aplicar(device)`, la decisión de "qué método de `Device` llamar" la toma
polimorfismo (cada clase concreta sabe la suya), no un `if` que necesita conocer
todos los tipos de antemano. La "línea de más" en `VLAN.aplicar()` es el precio de
que `Orquestador` nunca tenga que enterarse de que existe una VLAN — no es
boilerplate, es lo que reemplaza al `if/elif`.

---

### 2.5 Observer (`EventDispatcher`)

**Antes** — `audit_service.append_audit_event(...)` se llama a mano, con distinta forma, en cada una de las funciones de `vlan_execution_service.py` (`run_create_job`, `run_delete_job`, `_rollback_create`, etc. — 8+ call sites distintos):
```python
audit_service.append_audit_event(audit_id, "completed", {...})   # en run_create_job
audit_service.append_audit_event(audit_id, "failed", {...})       # en run_delete_job, con OTRA forma del payload
```

**Después**:
```python
class Orquestador:
    def ejecutar(self, ...):
        ...
        self._eventos.despachar([DomainEvent("recurso_aplicado", recurso, device, actor, resultado)])
        # Orquestador NO sabe que existe auditoría

class AuditListener(EventListener):
    def on_event(self, evento):
        self._audit_repo.append(AuditRecord.desde(evento))
```

**Por qué ayuda de verdad — corregido dos veces**: la razón real no es "algún
día vas a querer Slack" (el SRS no pide notificaciones multi-canal, esa excusa
era especulativa). Tampoco es ya "2 listeners fijos" — `GroupJobListener` se
eliminó (§2.9), queda **1 solo listener real, `AuditListener`**. ¿Sigue
justificado con uno solo, o es el mismo caso que `retry_policy.py`
(indirección sin consumidor real que la use)? No — la diferencia es que acá
la indirección **sí** hace algo hoy: es lo que convierte RF-AUD-01 ("cada
acción se audita") en garantía estructural en vez de disciplina — cualquier
`RecursoGestionable` nuevo (`InterfazVirtual`/`ConfiguracionGlobal`, RF-INTERV/
RF-GLOBAL) queda auditado gratis con que `Orquestador.ejecutar()` llame
`despachar()` una vez, sin que ese recurso nuevo tenga que acordarse de nada.
Sin `EventDispatcher`, `Orquestador` tendría que importar `audit_service`
directo — un módulo que no le incumbe a "ejecutar una operación con reintento
y rollback". Eso importa para testear `Orquestador` aislado (§2.7) sin tener
que fakear auditoría, y para que agregar una notificación nueva no
signifique encontrar los 8 call sites de `audit_service` y agregar una llamada
más al lado de cada uno, como pasa hoy.

---

### 2.6 Adapter (`AnsibleAutomationEngine`, `SecretVault`)

**Antes**: `vlan_service.py`, `port_service.py`, y los drivers importan `ansible_runner`/`cryptography.Fernet` directo — si mañana cambiás de Ansible a otra herramienta, tocás cada archivo que lo importa.

**Después**: el dominio depende de una interfaz (`AutomationEngine.run(playbook, extravars, inventory)`), y solo `AnsibleAutomationEngine` sabe que existe `ansible_runner`. Cambiar de herramienta = una clase nueva, cero cambios en `Orquestador`/drivers/entidades. Es literalmente RNF-MANT-01 del SRS, cumplido por estructura, no por disciplina.

---

### 2.7 Inyección por constructor (todas las clases de arriba)

**Antes** — para testear `run_create_job` hoy hay que hacer `monkeypatch.setattr(vlan_service, "create_vlan_on_device", fake_fn)` — un parche por *nombre de string*, que se rompe silenciosamente si alguien renombra la función.

**Después**:
```python
def test_orquestador_ejecuta_vlan():
    device = Device(name="switch-A", ...)
    device._vlan_driver = FakeDriver()          # inyección directa, ya probada en Phase 1
    fake_device_repo = FakeRepository({device.name: device})
    orquestador = Orquestador(device_repo=fake_device_repo, eventos=FakeEventDispatcher())
    orquestador.ejecutar(VLAN(vlan_id=10, name="TEST"), "switch-A", actor, job)
    assert device._vlan_driver.calls == [...]
```

**Por qué ayuda de verdad**: ya lo probamos en esta sesión — los 18 tests que le escribimos a `Device` (Phase 1) inyectan un driver falso directo al constructor, sin parchear ningún string. Si renombrás una clase, el test falla en la importación (error claro), no en silencio.

---

### 2.8 Tabla de transiciones (`Job` — corregido de State pattern a esto)

**Corrección de criterio**: una versión anterior de este documento resolvía
esto con el patrón State (GoF) — `EstadoJob` (interfaz) + 5 clases concretas
(`Pendiente`/`Corriendo`/`Completado`/`Fallado`/`Cancelado`). Se revisa —
State vale la pena cuando cada estado tiene **comportamiento propio** más allá
de qué transiciones acepta; acá no lo tiene, es puramente "¿esta transición es
legal o no?". Una tabla de transiciones da exactamente la misma garantía con
1 clase menos que 6, y además muestra la máquina de estados completa en un
solo lugar en vez de repartida en 6 clases.

**El problema real, verificado en el código actual**: `job_service.py: update_job()`
acepta **cualquier string** como `status`, sin validar contra el estado actual:

```python
# job_service.py — tal cual está hoy
def update_job(job_id, status=None, ...):
    with get_session() as session:
        row = session.query(JobModel).filter_by(job_id=job_id).first()
        if status is not None:
            row.status = status          # ← sin ningún chequeo de transición válida
```

Nada impide `update_job(job_id, status="completed")` sobre un Job que ya está
`"cancelled"` — se sobreescribe en silencio. (`cancel_job()` sí tiene un guard
puntual — `if row.status in ("pending", "running")` — pero es un chequeo ad-hoc en
esa única función, no una regla que se aplique en todos lados.)

**Con la tabla**, la regla de qué transiciones son válidas vive en un solo lugar:

```python
_TRANSICIONES_VALIDAS: dict[str, set[str]] = {
    "pending":   {"running", "cancelled"},
    "running":   {"completed", "failed", "cancelled"},
    "completed": set(),   # terminal
    "failed":    set(),   # terminal
    "cancelled": set(),   # terminal
}

class Job:
    def _transicionar(self, nuevo_status: str) -> None:
        if nuevo_status not in _TRANSICIONES_VALIDAS[self.status]:
            raise TransicionInvalidaError(self.status, nuevo_status)
        self.status = nuevo_status

    def cancelar(self) -> None:
        self._transicionar("cancelled")   # raise si no es válido, no silencio
```

**Por qué ayuda de verdad**: no es "prevenir un bug que ya existe" (`cancel_job`
igual tenía su guard) — es cerrar el hueco real que sí existe (`update_job` sin
ningún chequeo) y, sobre todo, cambiar "se sobreescribe en silencio" por "explota
con un error claro" — más fácil de detectar en desarrollo que un estado
inconsistente que aparece recién en producción. La tabla es la fuente única de
verdad de la máquina de estados — se lee de arriba a abajo, no hay que saltar
entre 6 clases para reconstruir qué transiciones existen.

**Grieta real encontrada al revisar el propio diseño (no código viejo)**:
`job_service.py: ensure_final_state()` — la red de seguridad que corre en el
`finally` de `run_operation()` para forzar a `"failed"` cualquier Job que quedó
pegado en un estado no-terminal tras un crash del worker — necesita forzar
`"pending" → "failed"`, que **no está** en `_TRANSICIONES_VALIDAS`
(`"pending"` solo permite `{"running", "cancelled"}`). Si `asegurar_estado_final()`
llamara a `_transicionar()` como todo el resto, la propia protección que
acabamos de construir se lo impediría — justo la única vez que hace falta
forzar el estado. **No se agrega `"failed"` a `_TRANSICIONES_VALIDAS["pending"]`**
— eso debilitaría la tabla para el caso normal, donde un Job en `pending` sí
debe pasar por `running` antes de poder fallar. `asegurar_estado_final()` queda
como la única excepción documentada que escribe `status` directo, sin pasar
por `_transicionar()` — es recuperación ante un crash, no una transición de
negocio, y tiene sentido que tenga una regla distinta.

### 2.9 Saga (`GroupOperationRunner`) — y por qué no hace falta un `GroupJob` persistido

`GroupOperationRunner` ya hace esto desde que lo diseñamos: reparte una operación
en N devices, cada uno se ejecuta independiente, y si uno falla se compensa
(rollback) sin afectar a los demás — es la definición exacta de una **Saga**
(Garcia-Molina & Salem, 1987; hoy estándar en sistemas distribuidos/microservicios)
aplicada a N switches en vez de N microservicios. Le da fundamento académico
(sagas en sistemas distribuidos) a un diseño que ya era correcto, y deja claro
que el rollback por device (§2.4) es literalmente la "compensación" de cada
paso de la saga, no una idea aislada.

**Corrección real sobre el diseño** (no sobre código viejo): versiones
anteriores de este documento tenían una entidad `GroupJob` persistida (con su
propio `Repository[GroupJob]`), un VO `DeviceExecution` reflejando el estado
de `Job` por-device, y un `GroupJobListener` (Observer) manteniéndolos
sincronizados cada vez que un device terminaba. Revisado: `Job` ya tiene
`group_job_id` — agrupar N jobs de la misma operación no necesita una fila
aparte, es una consulta:

```python
class GroupOperationRunner:  # = coordinador de Saga
    def encolar(self, recurso, devices, actor) -> str:
        group_job_id = str(uuid.uuid4())             # ni fila ni Repository — un UUID
        for device in devices:                        # cada uno = un paso de la Saga
            job = self._repo_job.add(Job(device, recurso, group_job_id=group_job_id))
            self._job_queue.dispatch("orquestador.ejecutar", recurso, device, actor, job.id)
        return group_job_id

# cada paso, ejecutado por Orquestador, es responsable de su propia compensación:
#   Orquestador._rollback() = la "acción compensatoria" de la Saga para ese paso
# el resumen del grupo no se mantiene sincronizado en ningún lado — se pide:
#   JobRepository.resumen_de_grupo(group_job_id) → agrega los Job reales al vuelo
```

**Por qué ayuda de verdad**: la Saga sigue siendo la misma — nombrarla bien
(GoF no alcanza para justificar `GroupOperationRunner`/rollback por device,
la bibliografía de sagas distribuidas sí) no cambiaba una línea, pero revisar
si hacía falta un `GroupJob` persistido sí es un cambio real: **3 clases
menos** (`GroupJob`, `DeviceExecution`, `GroupJobListener`), y se elimina el
riesgo de que la copia desnormalizada (`GroupJob.device_results`) quede
desincronizada de los `Job` reales si algo falla entre "el device terminó" y
"el listener actualizó la copia" — un problema clásico de vistas
materializadas que simplemente no existe si no hay copia.

### 2.9.1 CQRS (`JobRepository.resumen_de_grupo()`) — ya aplicado, nunca nombrado

Separar el modelo de escritura del modelo de lectura es, literalmente,
**CQRS** (Command Query Responsibility Segregation — Fowler/Young): `Job` es
el modelo de escritura (`Orquestador` lo muta, transición por transición, vía
`_transicionar()`), y `resumen_de_grupo()` es el modelo de lectura — un
agregado calculado al vuelo, nunca mutado directamente, optimizado para
responder "¿cómo va este grupo?" sin tocar el camino de escritura. No hace
falta un Event Store ni proyecciones asíncronas (la versión pesada de CQRS)
para que el patrón aplique — la separación de responsabilidad ya está, con
una consulta agregada en vez de una tabla materializada. Mismo criterio que
Saga/Memento: no cambia una línea de código, pero le da nombre real (con
bibliografía) a una decisión de diseño que ya se tomó por razones concretas
(§2.9), no una elección estética.

---

### 2.10 Memento (`pre_state`)

`pre_state = recurso.reconciliar(device)` (§2.4) es exactamente un **Memento**
(GoF): una foto del estado del recurso en el device, tomada **antes** de
aplicar el cambio, guardada sin exponer los detalles internos de cómo se llegó
a ella, para poder restaurar si algo falla. `Orquestador._rollback(recurso,
pre_state, device)` es el "restore" del Memento. Ya estaba en el diagrama de
secuencia (`post_vlans_secuencia.puml`) marcado como comentario — nunca se
había formalizado como patrón en este catálogo. Mismo caso que Saga: cero
cambio de código, solo nombrarlo bien para la tesis.

---

### 2.11 Facade (`Inventory`, `AutenticacionService`, `CleanupScheduler`)

Las 3 son el mismo patrón, sin haberlo nombrado: una interfaz simple sobre
varios colaboradores más finos, para que el caller no tenga que orquestarlos
él mismo.

- `Inventory` — fachada sobre `Repository[Device]`/`Repository[Site]`/
  `Repository[DeviceGroup]`/`RoleAssignmentRepository` (vía `scope_de()`).
- `AutenticacionService` — fachada sobre `JwtTokenIssuer`/`PasswordHasher`/
  `LoginAttemptRepository`/`Repository[RefreshToken]`.
- `CleanupScheduler` — fachada autocontenida sobre 3 tareas de limpieza
  periódica relacionadas (artefactos, refresh tokens, intentos de login). Se
  evaluó repartir cada purga al `Repository[T]` correspondiente (mismo
  criterio que las 5 especializaciones de §2.2.1) y se descartó — para 3
  tareas chicas y ya cohesivas bajo un mismo propósito ("mantenimiento
  periódico"), separarlas en 3 repos distintos cambia "una clase legible de
  punta a punta" por "una fachada vacía apuntando a lógica desperdigada", sin
  ganancia real de claridad a este tamaño.

**Por qué ayuda de verdad**: sin la fachada, cada endpoint de `api/` tendría
que saber en qué orden llamar a 3-4 colaboradores y qué hacer si uno falla a
mitad de camino — la fachada es la única que conoce ese orden, una vez.

---

### 2.12 Chain of Responsibility (middleware ASGI + cascada de `Depends()`)

El stack de middleware real (`main.py`: `RLSSessionMiddleware` →
`RateLimitMiddleware` → `TransportSecurityMiddleware` →
`CORSMiddleware`) más la cascada `Depends(require_authenticated)` →
`Depends(require_scope(...))` es la definición de manual del patrón: cada
eslabón decide si la request sigue al siguiente o corta acá con una respuesta
(401/403/429/redirect), sin que ningún eslabón sepa qué hace el anterior o el
siguiente. FastAPI/Starlette ya lo implementan así — nunca se había nombrado
el patrón en este catálogo. Cero cambio de código.

---

## 3. Mapeo código actual → clase nueva

| Archivo actual | Va a |
|---|---|
| `device_service.py` | `DeviceRepository` (subclase — `nombres_visibles(scope)`, §1.6) + lógica de negocio a `Device` |
| `job_service.py` | `JobRepository` + métodos en `Job` |
| `group_job_service.py` | absorbido — `Job.group_job_id` + `JobRepository.resumen_de_grupo()`, sin `GroupJob`/`Repository[GroupJob]` (§2.9) |
| `site_service.py` | `Repository[Site]` + `Site` |
| `device_group_service.py` | `Repository[DeviceGroup]` + `DeviceGroup` |
| `role_assignment_service.py` | **no se toca** — `RoleAssignmentService` ya existe y ya está bien hecha (§1.6). Solo `RoleAssignmentRepository` (CRUD base + `scope_de()`) es infraestructura nueva que la clase pasaría a usar en vez de `get_session()` directo |
| `user_service.py` | `Repository[Usuario]` + `Usuario` |
| `audit_service.py` | `AuditRepository` + `AuditListener` |
| `vlan_service.py` | absorbido — `VLAN.aplicar()` llama al driver vía `PluginRegistry` directo |
| `validators/vlan_validator.py` | absorbido completo en `VLAN.validar()` — no sobrevive como archivo |
| `validators/port_validator.py` | validación absorbida en `Puerto.validar()`; `compress_vlans_*()` (formateo, no validación) pasa a método privado de `HuaweiVendor`/`CiscoVendor` — no sobrevive como archivo |
| `vlan_execution_service.py` | `Orquestador` + `GroupOperationRunner` |
| `port_service.py`/`port_execution_service.py`/`port_config_service.py` | mismo molde que VLAN |
| `vendors/dispatcher.py` | `PluginRegistry` |
| `vendors/{huawei,cisco}/*.py` | `HuaweiVendor`/`CiscoVendor` (se funden vlan+port+parser) |
| `secret_service.py` | `SecretVault` (ya existe) — el shim `encrypt_password()`/`decrypt_password()` se elimina, inyectada por constructor donde haga falta |
| `ansible_service.py` | `AnsibleAutomationEngine` |
| `device_locks.py` | `RedisCoordinator.bloquear()` |
| `rate_limiter.py` | `RedisCoordinator.limitar()` |
| `login_attempt_service.py` | `LoginAttemptRepository` extendida (no genérica — ver §1.6) |
| `refresh_token_service.py` | instancia de `Repository[T]` + rotación en `AutenticacionService` |
| `auth_service.py` | `AutenticacionService` + `JwtTokenIssuer` |
| `effective_role.py` | se achica a `_resolve_scope()` (sigue como función — reuso real entre 8 archivos, ver §1) — el matching de rol se absorbe en `VisibilityScope.rol_para()`, tocando los 15+ call sites de `effective_role()` en `api/jobs.py`/`api/sites.py`/`api/device_groups.py`/`api/vlans.py`/`api/ports.py`/`api/devices.py`/`core/scope.py`, no solo `require_scope()` |
| `cleanup_service.py` | `CleanupScheduler` |
| `orchestration_runner.py` | `Orquestador` |
| `retry_policy.py` | absorbido completo en `Orquestador._clasificar_error()` — no sobrevive como archivo aparte |
| `parsers/*.py` | métodos privados de `HuaweiVendor`/`CiscoVendor` |

---

## 4. Flujo — `POST /vlans` (crear VLAN en 2 devices)

```
Cliente → [JWT → RBAC → RateLimit] → router
router:
   vlan = VLAN(vlan_id=100, name="MGMT")              # autovalida, 400 si falla
   group_job_id = group_op_runner.encolar(vlan, ["switch-A","switch-B"], actor)
   return {"group_job_id": group_job_id}              # RF-JOB-02

GroupOperationRunner.encolar()  [síncrono]
   group_job_id = str(uuid.uuid4())                    # sin fila, sin Repository[GroupJob] (§2.9)
   for device in [A,B]:
       job = repo_job.add(Job(device, vlan, group_job_id=group_job_id))  # RF-JOB-01
       job_queue.dispatch("orquestador.ejecutar", vlan, device, actor, job.id)
   return group_job_id

── async, un worker por device ──
Orquestador.ejecutar(vlan, device_name="switch-A", actor, job)
   if job.esta_en_estado_terminal(): return               # (5) reentrega de Celery, no-op
   device = repo_device.get(device_name)                 # recién ACÁ se resuelve el Device real (1)
   if device is None: raise NotFoundError(device_name)
   job.marcar_iniciado(); repo_job.add(job)               # (6) sin esto el Job real nunca sale de "pending"
   vlan.device = device_name                             # FK para persistir (4)
 with coordinador.bloquear("switch-A"):                  # (8) protege el device físico durante los segundos reales
   vlan.validar()                                        # ya corrió al reconstruir vlan (2)
   pre_state = vlan.reconciliar(device)                  # → device.driver.get_vlans(...) adentro (3)
   resultado, retries = orq._ejecutar_con_retry(lambda: vlan.aplicar(device), job.job_id, device_name)  # (7) → device.driver.create_vlan(...) adentro (3)
   if resultado.get("rc", 0) != 0: raise DeviceExecutionError(...)          # (7) falla real, ver §2.4 nota (11)
   repo_vlan.add(vlan)                                   # ya sabe a qué device pertenece -- add() es upsert (§2.2)
   job.marcar_completado(resultado)                      # valida contra la tabla, no un update() crudo (§2.8)
   repo_job.add(job)                                      # (6) persiste la transición -- Job es dataclass plano, no Active Record
   event_dispatcher.despachar([DomainEvent("recurso_aplicado", vlan, device, actor, resultado)])
        → AuditListener   → repo_audit.append(...)        # RF-AUD-01

Lectura posterior — GET /group-jobs/{id} → JobRepository.resumen_de_grupo(id),
agrega los Job del grupo al vuelo (§2.9) — no hay nada que este worker tenga
que actualizar acá, "switch-B" corriendo en paralelo se refleja solo con que
su propio Job exista con el mismo group_job_id.
```

**(1)** `encolar()`/`job_queue.dispatch()` reciben `device_name: str` (nombre), no
`Device`. El objeto real se resuelve recién acá, uno a la vez, justo antes de
usarse — mismo criterio de resolución perezosa que ya usa `Device` para sus propios
drivers. Pasar objetos `Device` completos por la cola de
Celery no es serializable de forma segura (menos uno que ya resolvió su password) y
obligaría al router a leer los N devices de la DB antes de responder el HTTP.

**(2)** `vlan` cruza la cola de Celery serializado (dict), no como objeto vivo — del
lado del worker se reconstruye `VLAN(**data)`, una construcción nueva donde
`__post_init__` corre de nuevo solo. No es que `Orquestador` "vuelva a validar" un
objeto ya validado — es un objeto distinto (reconstruido) que se valida a sí mismo,
como cualquier otro.

**(3)** `vlan.reconciliar()`/`vlan.aplicar()` sí hablan con el `VendorDriver`
directo, vía `device.driver` — **corrección sobre una versión anterior de esta
nota**, que decía "pasan por `Device`" (`device.list_vlans()`/`device.create_vlan(self)`
como métodos propios de `Device`); se sacaron por romper el bounded context de
`Device` (ver corrección en `Device`, §1). Lo que sí se mantiene: `Orquestador`
nunca necesita `PluginRegistry`/`AutomationEngine` como dependencias propias —
`device.driver` sigue resolviéndose adentro de `Device` (cacheado), `Orquestador`
solo ve el contrato `RecursoGestionable`.

**(4)** La identidad real de una VLAN persistida es `(vlan_id, device)`, no
`vlan_id` solo — la VLAN 100 en switch-A y la VLAN 100 en switch-B son dos filas
distintas de `device_vlans`. `VLAN` no sabía a qué device pertenecía hasta que
`Orquestador` se lo asigna acá (recién puede saberlo, porque el mismo `vlan` se
reparte a varios devices vía `GroupOperationRunner` — no tiene un device "propio"
hasta que un worker puntual lo procesa). Alternativa descartada: que
`Repository[VLAN].add(vlan, device)` tomara el device como segundo parámetro —
rompería la firma única `add(entidad)` que `Repository[T]` usa para **todas** las
entidades; mejor que `VLAN` cargue su propio FK como cualquier otro campo.

**(5)** Guard de idempotencia contra reentregas de Celery (§2.4 nota (7)) — si
esta tarea corre 2 veces para el mismo `job` (fallo raro de entrega), la 2da
corta acá, antes de volver a consultar el device en vivo o reaplicar nada.
`job.marcar_completado(resultado)` pasa por `_transicionar()` (§2.8), no
escribe `status` crudo — **corrección**: eso solo muta el objeto en memoria,
`repo_job.add(job)` (nota (6)) es lo que efectivamente reemplaza a
`repo_job.update(job.id, "completed")`, el paso que faltaba.

**(6)** `Job` es un dataclass plano — Active Record se descartó explícitamente
para todo este catálogo (§2.2). Sin repositorio inyectado, `marcar_iniciado()`/
`marcar_completado()` solo mutan el objeto en memoria; el cambio se pierde al
terminar la función si nadie llama `repo_job.add(job)` después (§2.4 nota (10)).
`add()` acá es upsert (`session.merge()` por PK, §2.2) — la fila ya existe
desde `GroupOperationRunner.encolar()`, esto la actualiza, no la duplica.

**(7)** Mismo motivo que §2.4 nota (11): `aplicar()` va envuelto en
`_ejecutar_con_retry()` (reintenta transitorios, nunca lanza — devuelve
`(resultado, retry_count)` siempre) y un `rc != 0` tras agotar reintentos se
convierte acá en excepción, un solo lugar de manejo de falla para ambos casos
("aplicar() lanzó" vs. "aplicar() devolvió `rc != 0`"). Este flujo simplificado
omite el `try/except/finally` completo (ver §2.4 para la versión con manejo de
falla y la red de seguridad final) — se muestra solo el camino feliz.

**(8)** `vlan.reconciliar()`/`vlan.aplicar()` son llamadas de red reales
(varios segundos, más lo que sumen los reintintos) — sin el `coordinador.bloquear()`
de acá, nada impide que "switch-B" corriendo en paralelo (otro worker, mismo
flujo, otro device) o un reintento superpuesto sobre **el mismo** "switch-A"
se pisen contra el device físico durante esa ventana. Ver §2.4 nota (12) para
el detalle completo (por qué se encontró, por qué el lock vive en `Orquestador`
y no en `GroupOperationRunner`, y por qué el rollback re-adquiere el lock en
vez de compartir el de la operación original).

**Corrección** — `GET /vlans?device=switch-A` **no** lee de `Repository[VLAN]`
como decía una versión anterior de esta nota: el código real (`api/vlans.py`)
hace una lectura **en vivo** al device (`vlan_service.get_vlans(device)`,
resuelve el driver y consulta el equipo de verdad, no la DB) — la verdad
"actual" del device, no lo último que quedó persistido. Va envuelta en
`device_locks.acquire(device, timeout=10)` para no chocar con una escritura
en curso, con 503 si el device está ocupado.

Con el diseño de este documento, esa lectura en vivo es exactamente
`device.driver.get_vlans(device, device.password)` — la misma llamada que
`VLAN.reconciliar()` ya usa internamente (§4 nota (3)), no hace falta otra
ruta. El lock, en
cambio, hoy llama a `device_locks.acquire()` **directo** desde `api/vlans.py`/
`api/ports.py`, sin pasar por `RedisCoordinator.bloquear()` — que es la clase
que `Orquestador` sí usa para lo mismo. Mismo problema que ya vimos con
`SecretVault`: 2 formas de llamar al mismo mecanismo de bloqueo, ninguna
inyectada. Se corrige igual — el endpoint recibe `RedisCoordinator` inyectado
(vía `Depends()`) y llama `coordinador.bloquear(device)`, no `device_locks`
directo.

**Entonces ¿para qué persiste `Orquestador.ejecutar()` en `Repository[VLAN]`
si la lectura de 1 device siempre va en vivo?** Pregunta real, no cosmética —
si nada leyera de `Repository[VLAN]`, sería el mismo tipo de estado
duplicado y sin uso que ya eliminamos con `GroupJob`. La diferencia es que
acá sí hay 2 razones reales para que las dos cosas convivan:
1. **Reportes cross-device** — "todas las VLANs de toda la red" no puede
   hacer live-query a N devices por request (lento, satura conexiones
   Ansible/SSH reales) — necesita una copia local para consultas masivas.
2. **Detección de drift** — comparar "lo que creemos que está configurado"
   (`Repository[VLAN]`) contra "lo que realmente está" (lectura en vivo) es
   un caso de uso real de automatización de red; hace falta una última copia
   conocida guardada para poder compararla.

Ninguna de las dos está en un RF explícito del SRS, pero son la única
justificación real para que `Repository[VLAN]` exista cuando el path de
lectura de 1 device ya no lo usa — sin esto dicho en algún lado, es
exactamente el mismo olor que `GroupJob` (persistir algo que nadie lee).
Mismo criterio para `Repository[Puerto]` en §5.

---

## 5. Flujo — `POST /puertos` (configurar puerto trunk en 1 device)

```
Cliente → [JWT → RBAC → RateLimit] → router
router:
   puerto = Puerto(interface="Gi0/0/24", mode="trunk", allowed_vlans=[10,20,30])
   job = orquestador.ejecutar_async(puerto, "switch-A", actor)   # 1 solo device (1)
   return {"job_id": job.id}

── async ──
Orquestador.ejecutar(puerto, device_name="switch-A", actor, job)
   if job.esta_en_estado_terminal(): return               # (4) reentrega de Celery, no-op
   device = repo_device.get(device_name)                 # mismo paso que VLAN
   if device is None: raise NotFoundError(device_name)
   job.marcar_iniciado(); repo_job.add(job)               # (5) ver §4 nota (6)
   puerto.device = device_name                           # FK para persistir (3)
 with coordinador.bloquear("switch-A"):                  # ver §4 nota (8) / §2.4 nota (12)
   puerto.validar()                                      # ¿mode válido? ¿vlans en rango?
   pre_state = puerto.reconciliar(device)                # → device.driver.list_ports(...) adentro (2)
   resultado, retries = orq._ejecutar_con_retry(lambda: puerto.aplicar(device), job.job_id, device_name)  # (2) → device.driver.configure_port(...)
   if resultado.get("rc", 0) != 0: raise DeviceExecutionError(...)          # ver §4 nota (7) / §2.4 nota (11)
   repo_puerto.add(puerto)                               # ya sabe a qué device pertenece -- add() es upsert (§2.2)
   job.marcar_completado(resultado)                      # valida contra la tabla (§2.8)
   repo_job.add(job)                                      # (5) persiste la transición
   event_dispatcher.despachar([DomainEvent("recurso_aplicado", puerto, device, actor, resultado)])
        → AuditListener → repo_audit.append(...)
```

**(1)** Un `POST` de un solo device no necesita `GroupOperationRunner` —
`Orquestador` alcanza directo. `GroupOperationRunner` solo entra cuando el request
trae `devices: list[str]` con más de uno.

**(2)** Igual que en VLAN — `reconciliar()`/`aplicar()` hablan con el
`VendorDriver` directo vía `device.driver` (§4 nota (3)).

**(3)** Mismo motivo que en VLAN — identidad real `(interface, device)`, ver
nota (4) de §4.

**(4)** Mismo guard de idempotencia que VLAN — ver nota (5) de §4 / nota (7) de §2.4.

**(5)** Mismo hueco de persistencia que VLAN — ver §4 nota (6) / §2.4 nota (10).

---

## 6. Ventajas de este plan vs. el estado actual

| Problema real de hoy | Cómo lo resuelve este diseño | Evidencia concreta |
|---|---|---|
| `vlan_service.py`/`port_service.py` repiten "resolver device → desencriptar → despachar driver" 5+ veces casi idénticas | Colapsa en `Device`/`VLAN`/`Puerto` — la lógica vive una vez, se llama desde donde haga falta | Ya lo hicimos: `vlan_service.py` pasó de 144 a 88 líneas, sin lógica propia, en el Phase 2 de esta sesión |
| El mismo `_to_domain(row)` + `get_session()` se repite en 8 servicios CRUD (~50 veces en todo `services/`) | `Repository[T]` genérico — se escribe una vez, se configura por instancia | Contado directo en `device_service.py`, `job_service.py`, `site_service.py`, etc. |
| Tests parchean funciones por nombre de string (`monkeypatch.setattr(vlan_service, "create_vlan_on_device", fake)`) — se rompen en silencio si alguien renombra | Inyección por constructor — fakes tipados, el error de renombrar se ve al importar, no en un test que falla sin explicación | Ya lo probamos: los 18 tests de `Device` (Phase 1) inyectan un driver falso directo, sin parchear strings |
| Dos "bases de datos" mock quedaron desincronizadas (`vlan_service.py` vs `vendors/mock.py`) porque nada obligaba a que hubiera una sola fuente de verdad | `Repository[T]` es la única puerta de escritura — estructuralmente imposible tener dos copias divergentes | Nos pasó **en esta misma sesión**, durante el Phase 2 de VLAN — nos costó una vuelta entera diagnosticarlo y arreglarlo |
| Auditoría se llama a mano (`audit_service.append_audit_event(...)`) en cada función de `vlan_execution_service.py`/`port_execution_service.py` — se puede olvidar en una nueva | `Orquestador` despacha el evento automáticamente al terminar cualquier operación — no depende de que cada función se acuerde | RF-AUD-01 pasa de "disciplina" a "garantía estructural" |
| Rollback implementado por separado para VLAN (`_rollback_create/_delete/_update`) y para Puerto (`_rollback_admin_state/_rollback_access_vlan/...`), casi el mismo código repetido | Un solo `Orquestador._rollback()` para cualquier `RecursoGestionable` — VLAN, Puerto, y lo que se agregue después | Comparación directa: `vlan_execution_service.py` + `port_execution_service.py` + `port_config_service.py` suman ~15 funciones de rollback casi iguales hoy |
| Agregar un vendor nuevo significa tocar el `if/elif` de `vendors/dispatcher.py` | `PluginRegistry.registrar()` — agregar vendor es una clase nueva, cero líneas tocadas en código existente | RNF-MANT-02 del SRS, cumplido por estructura |
| RF-INTERV (9 requisitos) y RF-GLOBAL (12 requisitos) — **21 requisitos sin implementar hoy** — implicarían escribir ~20 funciones nuevas seguramente repitiendo el mismo patrón de duplicación que ya tiene VLAN/Puerto | Se implementan como 2 clases nuevas (`InterfazVirtual`, `ConfiguracionGlobal`) que satisfacen `RecursoGestionable` — el motor (`Orquestador`, retry, rollback, auditoría) ya existe, no se reinventa | Mismo molde probado con VLAN/Puerto, sin arquitectura nueva que inventar |
| Errores hoy son `ValueError`/`Exception` genéricos en su mayoría — no hay forma sistemática de diferenciar conexión/autenticación/validación/ejecución | Jerarquía de excepciones de dominio (§1) — cada categoría de RNF-ERR-02 tiene su tipo, el mapeo a HTTP es automático | RNF-ERR-01/02 del SRS, cumplidos por estructura, no por revisión manual de cada endpoint |
| No hay forma rápida de saber si un requisito del SRS está cubierto — hay que leer 30 archivos de funciones sueltas | Cada clase/método de §1.6 cita explícitamente qué RF cubre o qué función actual reemplaza | Trazabilidad completa RF ↔ clase, algo que hoy no existe en ningún documento del proyecto — verificado contra el SRS completo: faltaban RF-INV-01/02, RF-USR-01/02, RF-ACC-02 y RF-VLAN-01/02/04, ya corregido en §1.6 (encontrado en esta misma revisión, precisamente en las clases más nuevas del catálogo) |

**Cobertura RNF (24 requisitos, §5 del SRS) — verificada completa en esta
revisión, con una distinción explícita**: 13 ya están satisfechos por clases
concretas y ahora citados (`RNF-PERF-01`→`GroupOperationRunner`,
`RNF-ESCAL-01`→`RedisCoordinator`, `RNF-RES-01/02`→`Orquestador`,
`RNF-API-05`→idempotencia (§2.4 nota 6), `RNF-AUD-01`→`AuditRecord`/
`AuditImmutabilityError`, `RNF-AUD-02`→`AuditRepository.purge_old()`,
`RNF-SEG-01`→`TransportSecurityMiddleware`, `RNF-SEG-02`→`require_scope`,
`RNF-SEG-03`→`RateLimitMiddleware`, `RNF-LOG-06`→`resumen_de_grupo()`).
**8 quedan deliberadamente fuera del diseño de clases** — no es un hueco, es
un límite de alcance explícito: `RNF-DISP-01` (99% disponibilidad,
redundancia) es un target de despliegue/infraestructura, habilitado por
`RNF-ESCAL-01` pero no resuelto por ninguna clase; `RNF-API-01/02/03/04`
(OpenAPI, Swagger UI, versionado por URI) vienen gratis de elegir FastAPI +
convención de rutas, no de una clase nueva; `RNF-LOG-01/02` ya los cumple el
`logging` estándar de Python usado en todo el código real, y `RNF-LOG-03/04/05`
(centralización, métricas, integración con Prometheus/Datadog/etc.) son
instrumentación y despliegue, no diseño de dominio — agregar una clase para
esto sería inventar alcance que el documento no se propuso cubrir.
| "¿Qué sites/groups puede ver este usuario?" se reescribe con SQL propio en 4 archivos distintos (`site_service.py`, `device_group_service.py`, `inventory_service.py`, `audit_service.py`) — si cambia la regla de visibilidad, hay que acordarse de tocar los 4 | `RoleAssignmentRepository.scope_de(user_id)` — una sola consulta, un solo `VisibilityScope` que todos consumen | 245 líneas medidas repartidas en 4 funciones (§6.1) — encontrado al hacer esta misma revisión, no estaba en ninguna versión anterior del documento |
| `EXECUTION_MODE == "mock"` se verifica por separado en 10+ lugares de 3 capas — dominio (`models/device.py`, una **entidad** leyendo una env var), aplicación (`vlan_service.py`, `port_service.py` ×9) y hasta presentación (`api/vlans.py`, `api/ports.py`) — es DIP roto: cada capa "sabe" que existe un modo mock | **3 problemas, no 1** (corrección sobre una revisión anterior de esta misma fila): (a) selección de driver → `PluginRegistry.obtener(vendor)`, una vez al arrancar; (b) `Device._get_password()` saltando el decrypt en mock → chequeo eliminado, código muerto (`MockVendor` ignora el password); (c) `device=None` como atajo mock-only en `vlan_service.py`/`port_service.py`/`api/*.py` → no es un problema de driver, queda fuera de alcance de este catálogo, documentado como tal | `grep EXECUTION_MODE` real: 3 ocurrencias en `device.py` (dominio, 2 resueltas por (a), 1 por (b)), 1 en `vlan_service.py` y 9 en `port_service.py` (mezcla de (c) y bypass de `_resolve_device()`, resuelto al absorber esos servicios en `Puerto`/`Device` — §2.4), 2 en `api/*.py` (c) — encontrado en esta misma revisión |
| `query_jobs()` arma su propio JOIN device→group para resolver "devices de este site", y recibe además un `allowed_devices` separado — dos formas distintas de decir "qué puede ver este caller" en la misma función | `JobRepository.query(..., scope: VisibilityScope, ...)` — reusa el mismo `VisibilityScope` que ya usan `Inventory`/`AuditRepository`, no inventa una tercera forma | `job_service.py:81-131`, encontrado en esta revisión |
| `mark_orphaned_jobs_failed()` (recuperación de jobs "running" tras un crash del servidor, RNF-RES) no estaba en ningún lado del catálogo | `JobRepository.recuperar_huerfanos()` — operación masiva de arranque, no pertenece a `Orquestador` (que ejecuta 1 operación sobre 1 device, no toca N jobs sin instancia viva) | `job_service.py:216-231`, encontrado en esta revisión, reubicado al pedido de mantener `Orquestador` chico |
| El mismo decode-JWT-con-fallback implementado **3 veces** (`core/scope.py`, `core/dependencies.py`, `core/rls_middleware.py` — la 3ra con comentario propio admitiendo la duplicación) — la 2da, usada solo por `api/group_jobs.py`, no revalida `is_active` contra la DB | `core/dependencies.py` se borra (`group_jobs.py` pasa a `require_authenticated`); las otras 2 llaman a `JwtTokenIssuer.decodificar()` en vez de reescribir el cuerpo | Hoy, un usuario desactivado conserva lectura en `GET /group-jobs/{id}` — único endpoint con este gap. Encontrado en 2 revisiones sucesivas — la 3ra copia (RLS) apareció recién al revisar `worker.py` |
| `core/scope.py: _lookup_device_scope()` reimplementa la misma query que `effective_role.py: _resolve_scope()` ya tiene para `resource_type="device"` | `require_scope()` llama a `_resolve_scope()` directo, se borra la copia | Mismo JOIN `DeviceModel`↔`DeviceGroupModel`, comparado línea por línea — encontrado en esta revisión |
| Postgres Row-Level Security (MSP Fase 6, `core/rls_context.py`/`core/rls_middleware.py`) — una capa entera de defensa en profundidad a nivel DB, real y funcionando, no estaba en ningún lado del catálogo | Documentada como `RLSSessionMiddleware` en Infraestructura — seguridad (§1.6), explícitamente como complemento de `VisibilityScope`, no reemplazo | Migración real (`f6msp5_msp_rls.py`) + test dedicado (`test_msp_rls.py`) — encontrado al seguir la cita de `worker.py: install_worker_system_context()` |
| `Puerto` tenía el mismo problema que `VLAN` tenía antes de §2.1 — validación duplicada en `api/ports.py` (llama a `port_validator.*` directo) en vez de vivir en `models/port.py`, que hoy no la usa **nunca** | Absorbida en `Puerto.validar()`, único lugar donde se valida un puerto — `compress_vlans_*()` (formateo, no validación) se separa a método privado de cada vendor | `validators/port_validator.py` (164 líneas) — verificado con grep que `models/port.py` nunca importa nada de ahí, solo `api/ports.py` y los drivers (para formateo, no validación) |
| `SecretVault` se llama de 2 formas distintas — shim de funciones de módulo (`device_service.py`/`inventory_service.py`/`port_service.py`) vs. singleton importado directo (`models/device.py`) — ninguna inyectada por constructor | Shim eliminado, `SecretVault` inyectada en `Inventory` (nota (5)) — un solo punto de entrada | Verificado con grep: 3 archivos usan el shim, 1 usa el singleton directo — inconsistencia real, no hipotética |
| Mismo problema con el lock de device: `device_locks.acquire()`/`is_device_busy()` se llaman directo en **4 lugares** de `api/vlans.py`/`api/ports.py` (2 lecturas en vivo, 1 mover puerto, 1 probe reusado por 3 endpoints de escritura), mientras `Orquestador` pasa por `RedisCoordinator.bloquear()` — 2 puertas al mismo mecanismo | `RedisCoordinator` inyectada también en esos 4 lugares — una sola puerta al lock | `api/vlans.py:63,84`, `api/ports.py:99-111,209` — verificado con grep, corregido en 2 pasadas (la primera solo había visto 2 de 4) |
| `GET /vlans?device=X` documentado antes como lectura de `Repository[VLAN]` (DB) — el código real hace una lectura **en vivo** al device (`vlan_service.get_vlans()`) | Corregido: es `device.driver.get_vlans(device, device.password)` — la misma llamada que `VLAN.reconciliar()` ya usa, no una ruta nueva | `api/vlans.py:51-94`, encontrado en esta revisión |
| `Device` reexponía como métodos propios (`create_vlan`/`delete_vlan`/`configure_port`/etc., 11 en total) la superficie completa de `VendorDriver` — crecía sin límite con cada RF nuevo (InterfazVirtual/ConfiguracionGlobal sumarían 2 tandas más), y contradecía el bounded context de §7 ("Topología de Red... no sabe que existen VLANs": `create_vlan(vlan: "VLAN")` toma un `VLAN` tipado) | `Device` expone 2 propiedades estables (`driver`, `password`) en vez de 1 método por operación; cada `Recurso.aplicar(device)` llama a `device.driver` directo — de paso elimina una doble validación real (`device.create_vlan()` llamaba `vlan.validate_name()` adentro, mientras `Orquestador.ejecutar()` ya llama `recurso.validar()` antes) | §1 (`Device`), cuestionando el diseño de clases, no código viejo — `device.py:72-74` para la doble validación |
| `Puerto.aplicar(device)` no podía distinguir "el caller quiere cambiar este campo" de "el campo copiado ya tenía este valor" — con todos los campos siempre presentes, no hay forma de saber cuál de las 7 operaciones reales de vendor (`update_port_description`/`set_port_admin_state`/`set_port_access_vlan`/`set_trunk_allowed_vlans`/`configure_port`/`shutdown`/`enable`) corresponde a una llamada puntual | Campos mutables de `Puerto` pasan a `Optional` (default `None`); `mutation_fields` como `@property` (campos no-`None`), no campo guardado; `aplicar()` despacha por presencia — 1 campo → método puntual del driver, 2+ → `configure_port(self)` (composite real, 1 sola llamada atómica, `port_driver_base.py:261-300`) | El código real ya necesita esto — `PortConfigRequest.mutation_fields` (`port_config_service.py:420`) — el catálogo no lo había absorbido; encontrado cuestionando el diseño de clases, no código viejo |
| `Orquestador` no tenía `JobRepository` inyectado (§1: solo `Repository[Device]`, `dict[str, Repository]`, `EventDispatcher`), pero `job.marcar_completado()`/`marcar_fallido()` estaban documentados como reemplazo de una escritura real a la DB — `Job` es dataclass plano (Active Record descartado en §2.2), sin repositorio inyectado esas llamadas solo mutan el objeto en memoria y el cambio se pierde; deshacía en silencio el arreglo de RF-JOB-03 de las notas (8)/(9). `marcar_iniciado()` tampoco se llamaba en ningún lado del flujo canónico | `Orquestador` recibe `_jobs: JobRepository` inyectado; llama `marcar_iniciado()`+persiste al arrancar, y persiste después de cada `marcar_fallido()`/`marcar_completado()` — 3 puntos. De paso, `Repository[T].add()` se documenta explícitamente como upsert (`session.merge()` por PK, §2.2) — sin eso, persistir la misma entidad 2 veces (cualquier `Job`, o un `VLAN` idempotente) rompería con duplicate-key | §2.4 nota (10), §2.2 — el gap más grande encontrado cuestionando el diseño de clases, no en código viejo: afecta el mecanismo básico de persistencia de todo el catálogo |
| El fix anterior (`add()` = upsert vía `session.merge()`) es correcto para `Job`/`VLAN`/`Site`, pero aplicado sin excepción a `AuditRecord` permitiría que una colisión de `id` (bug, reintento, manipulación) sobreescriba en silencio un registro de auditoría — justo lo que RNF-AUD/§6.5 del SRS prohíbe ("a prueba de manipulaciones") | `AuditRepository.append()` no delega a `self.add()` — usa `session.add()` (insert estricto): una colisión de `id` falla ruidosamente, no pisa el registro anterior. Sin subclase nueva, `AuditRepository` ya es subclase por otro motivo | §1.6 (`AuditRepository`), §2.2 — autocrítica sobre mi propio fix anterior en esta misma revisión, no código viejo |
| Los no-op se auditan en `VLAN`/`Puerto` pero `Inventory.move()` los ignora en silencio — 2 respuestas distintas a la misma pregunta ("¿un no-op es una acción auditable?"), nunca resueltas de forma consciente | `Inventory.move()` alineada a `VLAN`/`Puerto`: despacha `DomainEvent` también en el caso no-op, con `"noop": True` | `inventory_service.py: move()`, comentario real *"without emitting a spurious audit row"* — decisión opuesta a `run_operation()`'s `check_noop`, encontrado revisando el diseño, no código viejo |
| Mismo patrón, 3ra vez: `RoleAssignmentService.grant()` no audita un re-grant del mismo rol (`if existing.role != role:` guarda todo el `log_action(...)`) | Alineado al mismo criterio — audita siempre, `"noop": True` si el rol no cambió | `role_assignment_service.py: grant()`, encontrado revisando el diseño — 3 lugares distintos con la misma pregunta sin resolver de forma consistente |
| Reentrega de Celery ("al menos una vez") sobre un `Job` ya terminal → `job.marcar_completado()` lanzaría `TransicionInvalidaError` sin distinguir "bug de negocio" de "esto ya se procesó" — no controlado, podía crashear la tarea y reintentarse en loop | `job.esta_en_estado_terminal()` como guard al principio de `Orquestador.ejecutar()` — corta antes de repetir ningún trabajo real, mismo criterio que ya tiene `_TRANSICIONES_VALIDAS` | §2.4 nota (7), encontrado revisando semántica de entrega de Celery contra la tabla de transiciones que ya construimos |
| El camino de falla de `Orquestador.ejecutar()` re-lanzaba sin marcar el `Job` ni auditar — quedaba `"running"` para siempre (RF-JOB-03 roto) y sin rastro en el audit log (RF-AUD-01 roto) | `job.marcar_fallido(error)` + `DomainEvent("recurso_fallido", ...)` antes del `raise` — mismo evento genérico, otro `tipo` | §2.4 nota (8) — encontrado en el propio ejemplo canónico del documento, no en código viejo |
| El arreglo de la fila anterior descartaba el resultado de `self._rollback(...)` — `_rollback_create()`/9 funciones equivalentes reales devuelven `(rollback_performed, rollback_success)`, tri-estado que `api/jobs.py: _format_job()` ya expone en `execution_summary`; con el retorno descartado, esos 2 campos quedan siempre en su default pase lo que pase en el device | `_rollback()` documentado con su tipo de retorno real (nunca propaga, las 10 funciones que unifica atrapan su propia excepción) + `job.marcar_fallido(error, rollback_performed, rollback_success)` | §2.4 nota (9) — mismo patrón que la fila anterior, un nivel más adentro, encontrado revisando el propio ejemplo canónico contra `vlan_execution_service.py:117-174` |
| `RedisCoordinator.bloquear()` (reemplazo de `device_locks.acquire()`) estaba diseñado y hasta listado como colaborador de `GroupOperationRunner` en §1, pero nunca se llamaba en ningún flujo real — `encolar()` solo encola (no toca el device), y `Orquestador.ejecutar()` (donde sí ocurre la llamada de varios segundos al driver) no recibía `coordinador` como colaborador. Dos ejecuciones concurrentes sobre el mismo device (2 operaciones de grupo, o un reintento superpuesto) podían pisarse contra el switch real sin que nada lo impidiera | `RedisCoordinator` se mueve de `GroupOperationRunner` a `Orquestador`; `validar()`/`reconciliar()`/`aplicar()` corren dentro de `self._coordinador.bloquear(device_name)`, mismo alcance que `orchestration_runner.py:154`; el rollback re-adquiere el lock en un segundo `with` en vez de compartir el primero | §2.4 nota (12) — encontrado preguntando qué pasa cuando la llamada al driver tarda varios segundos, no en código viejo ni en una revisión de imports |

**El costo, dicho sin vueltas**: son más clases para navegar (~52 vs. ~30 archivos de
funciones hoy) y hay que aprender dónde vive cada cosa la primera vez. Ese costo es
real — no es una arquitectura gratis. La apuesta es que se paga una sola vez al
aprenderla, mientras que la duplicación actual (el primer problema de esta tabla) se
paga **cada vez** que se toca una función más sin darse cuenta de que hay 4
hermanas casi iguales en otro archivo.

### 6.1 Cuantificación — líneas de código repetido que se sacarían

Medido con `grep`/rangos de línea reales sobre el código actual (no estimado a ojo).
Un hallazgo nuevo apareció al medir: **`port_config_service.py` (1121 líneas) no
pasa por `orchestration_runner.run_operation()`** como sí hacen
`vlan_execution_service.py` y `port_execution_service.py` (confirmado — cero
ocurrencias de `run_operation` en ese archivo) — reimplementa el esqueleto completo
de lock/pre-state/rollback/auditoría **por tercera vez**, a mano, para
`configure_port`/`shutdown_port`/`enable_port`.

| Qué se repite hoy | Dónde (líneas medidas) | Total hoy | Colapsa en | Estimado después | Ahorro |
|---|---|---|---|---|---|
| Funciones de rollback (mismo esqueleto: log → marcar rollback_started → ejecutar → verificar → marcar rollback_completed) | `vlan_execution_service.py` (3 funciones, 188 líneas) + `port_execution_service.py` (4 funciones, 349 líneas) + `port_config_service.py` (3 funciones, 241 líneas) | **778 líneas**, 10 funciones | 1 solo `Orquestador._rollback()` | ~30 líneas | **~748 líneas** |
| Captura de pre-state (mismo patrón: leer estado actual, envolver en dict, manejar excepción) | `vlan_execution_service.py` (16) + `port_execution_service.py` (130, 4 pares) + `port_config_service.py` (109, 3 pares) | **255 líneas**, 12 funciones | `VLAN.reconciliar()` + `Puerto.reconciliar()` | ~25 líneas | **~230 líneas** |
| `run_configure_port_job`/`run_shutdown_port_job`/`run_enable_port_job` reimplementando el esqueleto en vez de delegar a `run_operation` (comparado contra las 4 funciones equivalentes de `vlan_execution_service.py`, que sí delegan: 63+84+60+25=232 líneas para 4 operaciones, ~58 líneas/operación) | `port_config_service.py`, 507 líneas para 3 operaciones (~169 líneas/operación — 3x más por operación que la versión que sí delega) | **507 líneas** | Delegando a `Orquestador.ejecutar()` igual que VLAN | ~174 líneas (3 × 58) | **~333 líneas** |
| 5 funciones de `vlan_service.py` repitiendo resolver→desencriptar→despachar (**ya medido — hecho real en esta sesión, Phase 2**) | `vlan_service.py` | 144 líneas | Delegados de 1 línea a `Device` | 88 líneas | **56 líneas (real, ya aplicado)** |
| `_to_domain(row)` + `with get_session()` repetido en cada función CRUD | `device_service.py`(8) + `job_service.py`(8) + `site_service.py`(7) + `device_group_service.py`(7) + `user_service.py`(8) + `role_assignment_service.py`(4) + `group_job_service.py`(3) + `audit_service.py`(7) — 52 ocurrencias, ~6 líneas c/u | **~312 líneas** | `Repository[T]` genérico | ~20 líneas (escrita una vez) | **~292 líneas** |
| "grants del usuario → sites/groups visibles" reescrito con SQL propio en cada consumidor | `site_service.py: list_sites_for_user()` (34) + `device_group_service.py: list_groups_for_user()` (47) + `inventory_service.py: _visible_device_names()` (50) + `audit_service.py: _apply_msp_audit_scoping()` (114) | **245 líneas**, 4 funciones | `RoleAssignmentRepository.scope_de()` | ~20 líneas | **~225 líneas** |
| Adicional (no en el conteo de líneas, cambio de forma no de tamaño): `effective_role()` se llama inline en **15+ lugares de 8 archivos** (`api/jobs.py`/`api/sites.py`/`api/device_groups.py`/`api/vlans.py`/`api/ports.py`/`api/devices.py`/`core/scope.py`), cada uno con **una query nueva** — con `scope_de()` calculado una vez y `VisibilityScope.rol_para()` en memoria, esos 15+ chequeos puntuales pasan a **cero queries adicionales** por request | — | — | `VisibilityScope.rol_para()` | — | menos round-trips a la DB por request, no solo menos líneas — verificado por grep sobre los 8 archivos reales |
| `_build_inventory(device, password)` — mismo cuerpo de 5 líneas, copiado literal | `huawei/vlan_driver.py` + `huawei/port_driver.py` + `cisco/vlan_driver.py` + `cisco/port_driver.py` | **~20 líneas**, 4 funciones | 1 método privado por vendor (`HuaweiVendor._construir_inventario()`) | ~10 líneas (2 vendors) | **~10 líneas** |
| `_clean()`/`_ANSI_ESCAPE` — limpieza de escapes ANSI, mismo cuerpo y docstring, sin depender del vendor | `port_parser.py` + `cisco_port_parser.py` (función completa) + `vlan_parser.py` (solo la constante) — 3 copias | **~9 líneas** | `parsers/_common.py: limpiar_lineas()` — función de módulo, no clase | ~3 líneas | **~6 líneas** |

**Total: ~1.900 líneas de código duplicado eliminadas** (778+255+507+144+312+245+20+6 ≈
2267 líneas hoy → ~370 líneas después, sin contar las 56 ya confirmadas en el Phase 2
real de esta sesión).

Nota de método: el lado "hoy" está medido directo del código (rangos de línea
verificados con `grep`/conteo real). El lado "después" es una estimación razonable
basada en el tamaño real de piezas equivalentes que **ya construimos** esta sesión
(`Orquestador` de referencia, `VLAN.reconciliar()`, y el propio colapso medido de
`vlan_service.py`) — no una clase que todavía no existe e inventada de cero.

---

## 7. Bounded Contexts (DDD estratégico)

Todo lo anterior es DDD **táctico** — Entidades, Value Objects, Repository — dentro
de **un** modelo de dominio único. DDD **estratégico** pregunta algo distinto: ¿es
en realidad un solo modelo, o estamos metiendo varios lenguajes/conceptos
distintos en la misma bolsa? Mirando el catálogo de §1, hay 5 agrupaciones
naturales, cada una con su propio vocabulario:

```
┌────────────────────────┐   ┌───────────────────────┐   ┌──────────────────────────┐
│ Identidad y Acceso      │   │ Topología de Red        │   │ Configuración de Red      │
│ Usuario, RoleAssignment,│──▶│ Site, DeviceGroup,       │──▶│ VLAN, Puerto,              │
│ AccessToken,             │OHS│ Device                   │C/S│ InterfazVirtual,           │
│ AutenticacionService     │   │                          │   │ ConfiguracionGlobal,        │
└────────────────────────┘   └───────────────────────┘   │ RecursoGestionable,          │
         ▲                                                │ VendorDriver, PluginRegistry │
         │ OHS                                             └──────────────────────────┘
         │                                                            │
         │                                                            │ ACL
         │                                                            ▼
┌────────┴───────────────┐                                 ┌──────────────────────────┐
│  (todos los contextos    │                                 │ CLI real de Huawei/Cisco  │
│   consumen "¿puede este  │                                 │ (fuera de nuestro dominio)│
│   usuario hacer X?")     │                                 └──────────────────────────┘
└──────────────────────────┘
         ▲
         │ Customer
┌────────┴───────────────┐   OHS    ┌──────────────────────────┐
│ Orquestación de Jobs     │◀────────│  (Configuración implementa │
│ Job,                       │         │   el puerto RecursoGestio- │
│ Orquestador,              │         │   nable que este contexto  │
│ GroupOperationRunner       │         │   publica)                 │
└────────────────────────┘                                        
         │
         │ eventos (Conformist)
         ▼
┌────────────────────────┐
│ Auditoría                │
│ AuditRecord,              │
│ AuditRepository,           │
│ AuditListener               │
└────────────────────────┘
```

### Los 5 contextos, qué posee cada uno y su lenguaje propio

| Contexto | Clases | Lenguaje ubicuo | Rol |
|---|---|---|---|
| **Identidad y Acceso** | `Usuario`, `RoleAssignment`, `RoleAssignmentService` (ya existe — otorga/revoca roles, D25), `AccessToken`, `RefreshToken`, `AutenticacionService`, `JwtTokenIssuer`, `PasswordHasher` | "usuario", "rol", "sesión", "token" | Publica `RoleAssignmentRepository.scope_de(user_id) -> VisibilityScope` como único servicio — calculado una vez por request, responde tanto "¿puede X puntual?" (`rol_para()`) como "¿qué puede ver en general?" (`site_ids`/`device_group_ids`); nadie más necesita saber cómo se calculan los permisos |
| **Topología de Red** | `Site`, `DeviceGroup`, `Device`, `DeviceRepository` (subclase — `nombres_visibles(scope)`, §1.6), **`Inventory`** (coordinador — ya existe, MSP Fase 3) | "site", "grupo", "device", "vendor" | Define qué *es* un device y de quién es — identidad, pertenencia. No sabe que existen VLANs (afirmación que ahora es cierta — `Device` ya no reexpone `create_vlan()`/etc., ver §1 y §6) |
| **Configuración de Red** | `VLAN`, `Puerto`, `InterfazVirtual`, `ConfiguracionGlobal`, `RecursoGestionable`, `VendorDriver`, `PluginRegistry`, `Huawei/Cisco/MockVendor`, **`SecretVault`** (desencripta el password justo antes de hablarle al vendor — le pertenece a quien lo usa, no a Identidad) | "VLAN", "trunk", "ACL", "SNMP" | El más grande — todo lo que se le puede *hacer* a un device |
| **Orquestación de Jobs** | `Job` (con `_transicionar()`, tabla de transiciones — §2.8, y `group_job_id` — §2.9), `Orquestador`, `GroupOperationRunner`, `RedisCoordinator` | "job", "reintento", "rollback", "saga" | Genérico a propósito — no sabe qué es una VLAN, podría orquestar cualquier cosa que implemente `RecursoGestionable` |
| **Auditoría** | `AuditRecord`, `AuditRepository`, `AuditListener` | "acción", "trazabilidad" | Solo escucha `DomainEvent` — ni siquiera necesita saber qué es una VLAN, recibe datos ya aplanados |

### Las relaciones entre contextos, con el patrón DDD correcto para cada una

- **Identidad y Acceso → todos** — **Open Host Service**: publica **un** servicio
  bien definido que cualquiera puede llamar — `RoleAssignmentRepository
  .scope_de(user_id) -> VisibilityScope`, calculado una vez por request.
  Responde tanto "¿puede este usuario hacer X sobre este recurso puntual?"
  (`scope.rol_para(...)`) como "¿qué sites/groups puede ver en general?"
  (`scope.site_ids`/`.device_group_ids`) — antes eran 2 servicios
  (`effective_role()` + `scope_de()`), cada uno con su propia query; unificados
  en uno solo (§1.6/§6.1). No es Shared Kernel (no comparten código/modelo) —
  cada contexto solo consume la respuesta, nunca importa clases de Identidad.
- **Topología → Configuración** — **Customer/Supplier**: Configuración depende de `Device` (supplier), pero Topología no se entera de que existe VLAN — Configuración se adapta a lo que Topología publica, no al revés.
- **Configuración → Orquestación** — también **Open Host Service**, en dirección inversa a la intuición: Orquestación publica el puerto `RecursoGestionable` (4 métodos), y Configuración es quien lo *implementa* en `VLAN`/`Puerto`. Orquestación nunca importa nada de Configuración — es Configuración la que se ajusta al contrato que Orquestación definió.
- **CLI de Huawei/Cisco → Configuración** — **Anti-Corruption Layer** (ya la nombramos en el catálogo de patrones): los parsers dentro de `HuaweiVendor`/`CiscoVendor` son la barrera que impide que la sintaxis real del fabricante contamine nuestro modelo.
- **Todos → Auditoría** — **Conformist**: Auditoría nunca le pide a nadie que cambie su forma de emitir eventos para su conveniencia — toma lo que le dan (`DomainEvent`) tal cual.

### Qué cambia en concreto — discusión, no estructura a imponer

**Corrección de criterio**: versiones anteriores de esta sección proponían
carpetas físicas por contexto con un linter de imports verificándolo en CI. Se
revisa — para un proyecto mantenido por 1 persona (tesis), a esta escala, esa
disciplina resuelve un problema que no existe todavía (evitar que *otro
equipo* importe donde no debe). Las 5 agrupaciones de arriba valen como
**mapa conceptual** — explican por qué el dominio se corta donde se corta, y
qué clase pertenece a qué vocabulario — sin que haga falta forzarlo en
carpetas ni reglas de import verificadas por herramienta. Si el proyecto
crece lo suficiente (más gente, o de verdad se evalúa partir un contexto en
un servicio separado), ahí se paga el costo de moverlo a carpetas — no antes.
Es la misma lógica que ya se usó para descartar microservicios (opción J):
diseñar para un tamaño de equipo/sistema que este proyecto no tiene todavía.

La correspondencia contexto → clases queda como estaba en la tabla de arriba
(sirve para ubicar cada clase mentalmente); lo que se retira es la carpeta
física y el linter de imports.

---

*Fin. ~52 clases totales (43 del catálogo original — 45 menos `GroupJob` y
`GroupJobListener`, eliminadas en esta ronda, ver §2.9: `Job.group_job_id` +
`JobRepository.resumen_de_grupo()` alcanzan, sin entidad ni Observer aparte
— + 1
excepción nueva (`TransicionInvalidaError`, `Job._transicionar()` — §2.8, ya no
son 6 clases de `EstadoJob`, corregido a tabla de transiciones en 1 método) + `Inventory`, ya existente en el código — MSP Fase 3 — que
faltaba en el catálogo + `VisibilityScope`, Value Object nuevo que cierra una
duplicación real de 245 líneas en 4 archivos, §6.1 + `RoleAssignmentService`, ya
existente y ya bien hecha — el catálogo anterior la mandaba a disolver por error,
§1.6 + `RoleAssignmentRepository`/`LoginAttemptRepository`/`JobRepository`, las 3
subclases de `Repository[T]` que faltaban contar — corrección de esta ronda:
rondas anteriores decían que sus métodos "no sumaban al total" tratándolas como
si fueran la misma clase genérica con métodos de más; son subclases reales, igual
que `AuditRepository` siempre lo fue, y se cuentan igual — ver criterio en
§2.2.1 + `RetryDecision`, Value Object ya existente (`retry_policy.py`) que
faltaba en el catálogo, mismo criterio que `AuditRecord`. `DeviceExecution`
(agregada en una ronda anterior) se elimina en esta — era la misma
duplicación de estado que `GroupJob`, no sobrevive tampoco) + 1
(`DeviceRepository`, 5ta subclase de `Repository[T]` — corrección de esta
ronda sobre mi propio rediseño: `AuditRepository.query()`/`JobRepository.query()`
filtran por nombre de device, no por `site_id`/`device_group_id`, y
`VisibilityScope` sola no resuelve ese JOIN — ver §1.6, nota (7)), cada una
con una razón de existir citada en §1 y §1.6.
`repositorio()` en `RecursoGestionable` y `recuperar_huerfanos()` en
`JobRepository` (mudado desde `Orquestador`, a pedido — ver §2.4) son métodos
nuevos sobre clases que ya estaban contadas, no suman al total. `Orquestador`
mantiene `_ejecutar_con_retry()`/`_clasificar_error()` como métodos privados
propios, con las tablas de patrones de `retry_policy.py` absorbidas como
constantes del módulo — se evaluó extraerlos a una clase nueva
(`PoliticaReintento`) y a un módulo de funciones libres, y se descartaron los
dos: solo los usa `Orquestador`, ni la clase ni el módulo aparte se
justifican, y `retry_policy.py` no sobrevive como archivo propio.
`validate_inventory()` (`ansible_service.py`) es código muerto hoy — no se migra,
no resta del total porque nunca estuvo contada.*
