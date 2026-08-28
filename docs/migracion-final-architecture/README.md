# Plan de migración → `FINAL_ARCHITECTURE.md`

## Qué es esto

Plan de migración del código real de `ansiauth` (rama `refactor/device-rich-domain`)
hacia la arquitectura descripta en [`docs/FINAL_ARCHITECTURE.md`](../FINAL_ARCHITECTURE.md).
Cada fase tiene su propio archivo (`FASE_1.md`, `FASE_2.md`, ...) en esta misma carpeta,
escrito para que **otra sesión de Claude sin contexto previo** pueda ejecutarlo leyendo
solo ese archivo + el código real + `FINAL_ARCHITECTURE.md`.

**No confundir con `docs/plan-migracion-modelo-dominio.md`** — ese es un plan distinto,
ya ejecutado en gran parte (Fases 0/0.5/1/1.5 de ESE documento, ver su propio texto),
que apuntaba a `diagrama_clases_dominio.drawio`. Se deja intacto, sin tocar. Este plan
nuevo tiene su propia numeración (Fase 1, 2, 3... en esta carpeta) y apunta
específicamente a `FINAL_ARCHITECTURE.md`, el documento más reciente y autoritativo.

## Alcance

- **Sí**: migrar todo lo que hoy tiene código real (`VLAN`, `Puerto`, `Device`, `Site`,
  `DeviceGroup`, `Inventory`, `Job`, `Audit`, RBAC/`VisibilityScope`, `Repository[T]`,
  `Orquestador`, etc.) a la forma que describe `FINAL_ARCHITECTURE.md`.
- **No**: `InterfazVirtual`/`ConfiguracionGlobal` (RF-INTERV/RF-GLOBAL) — son features
  nuevas sin código real hoy, quedan fuera de este plan.
- **No**: `AutenticacionService`/`RefreshToken` (`auth_service.py`/
  `refresh_token_service.py`) — decisión explícita, no asignados a ninguna fase. Si se
  quiere cubrirlos, es una fase nueva a agregar, no una extensión de las 7 actuales.
- **No**: migraciones de Alembic / schema de DB, ni actualizar los 78 archivos de test
  existentes — decisión explícita: tests y base de datos se reconstruyen aparte, desde
  cero, una vez que el código llegue a la forma objetivo. Ninguna fase de este plan
  necesita dejar los tests en verde.

## Advertencia importante — leer antes de ejecutar cualquier fase

**La app puede quedar en un estado no ejecutable entre fases.** El criterio de "hecho"
de cada fase es la **forma final de las clases** que le tocan (nombres, métodos,
colaboradores inyectados, tal como los describe `FINAL_ARCHITECTURE.md`), no que
`uvicorn app.main:app` levante sin errores en cada commit intermedio. Cuando una fase
borra o cambia la firma de algo que otro archivo real todavía llama, esa fase lo dice
explícitamente en su sección **"Callers reales que quedan rotos hasta la Fase N"** — no
es un bug, es esperado, y se arregla en la fase indicada. Si al ejecutar una fase
aparece un `ImportError`/`AttributeError` en un archivo **no mencionado** en esa
sección, ahí sí hay que pararse a investigar, puede ser un caso real que este plan no
previó.

**Hay un frontend real (`frontend/src/`) que consume estas APIs — sus tipos
TypeScript son un contrato a verificar, no solo un detalle.** `frontend/src/types/`
(`job.ts`, `audit.ts`, `vlan.ts`, `port.ts`, `device.ts`, `site.ts`) documentan la
forma exacta de cada respuesta real, con más precisión a veces que el propio código
Python (ej. `GroupJobDeviceResult.duration_ms`, un campo real que una versión
anterior de `FASE_4.md` no tenía). Este plan **no** migra el frontend — pero
cualquier fase que rediseñe la forma de una respuesta HTTP (`FASE_4.md`:
`resumen_de_grupo()`; `FASE_7.md`: `api/audit.py`; `FASE_6.md`: `api/devices.py`/
`api/sites.py`/`api/device_groups.py`) tiene que compararla contra el `.ts`
correspondiente antes de darse por cerrada — no asumir que "misma lógica en Python"
implica "misma forma en JSON".

## Convención de directorios y nombres de archivo — obligatoria, todas las fases

Esto no es una sugerencia por fase — es la estructura final que **todas** las fases
tienen que respetar, para que el resultado sea consistente sin importar en qué orden
o con qué separación temporal se ejecuten. Si una fase ya escrita dice algo distinto
a esto, este documento gana — corregir la fase, no el código.

```
backend/app/
├── core/
│   ├── repository.py            # Repository[T] genérico (Fase 1)
│   ├── exceptions.py            # existente — +TransicionInvalidaError (F4), +DeviceExecutionError si faltaba (F5), +DefaultGroupImmutableError+SiteHasDevicesError (F6, movidas — api/sites.py y api/device_groups.py las importan por nombre, ver FASE_6.md)
│   ├── scope.py                  # existente, reescrito (Fase 2)
│   └── dependencies.py           # BORRADO (Fase 2)
│
├── models/                       # entidades + Value Objects + protocolos — SIN comportamiento de persistencia
│   ├── device.py                  # existente, reescrito (Fase 1: driver/password; Fase 6: actualizar())
│   ├── vlan.py                    # existente, reescrito (Fase 2)
│   ├── port.py                    # existente, reescrito (Fase 2) — PortConfigResult se mantiene
│   ├── job.py                     # existente, reescrito (Fase 4)
│   ├── group_job.py               # BORRADO (Fase 4)
│   ├── audit.py                   # existente, +AuditRecord.desde() (Fase 3)
│   ├── site.py                    # nuevo (Fase 6)
│   ├── device_group.py            # nuevo (Fase 6)
│   ├── visibility_scope.py        # nuevo (Fase 2)
│   ├── domain_event.py            # nuevo (Fase 3)
│   ├── recurso_gestionable.py     # nuevo (Fase 5) — Protocol
│   └── retry_decision.py          # nuevo (Fase 5) — VO, movido de retry_policy.py
│
├── repositories/                 # paquete nuevo — las subclases de Repository[T] (y las que no heredan, ver abajo)
│   ├── role_assignment_repository.py   # Fase 2
│   ├── audit_repository.py             # Fase 3
│   ├── login_attempt_repository.py     # Fase 3 — NO hereda de Repository[T]
│   ├── device_repository.py            # Fase 3
│   ├── device_group_repository.py      # Fase 3, +eliminar_con_auto_move() (Fase 6)
│   ├── job_repository.py               # Fase 4 (esqueleto) + Fase 4 Línea B (query(scope))
│   └── site_repository.py              # nuevo (Fase 6) — 7ma subclase, justificación transaccional no de consulta
│
├── services/
│   ├── plugin_registry.py         # nuevo (Fase 1)
│   ├── redis_coordinator.py       # nuevo (Fase 1)
│   ├── secret_service.py          # existente, shim eliminado (Fase 1)
│   ├── event_listener.py          # nuevo (Fase 3)
│   ├── event_dispatcher.py        # nuevo (Fase 3)
│   ├── audit_listener.py          # nuevo (Fase 3)
│   ├── orquestador.py             # nuevo (Fase 5) — absorbe retry_policy.py
│   ├── group_operation_runner.py  # nuevo (Fase 5)
│   ├── cleanup_scheduler.py       # nuevo (Fase 5)
│   ├── inventory_service.py       # existente, Inventory reescrita in situ (Fase 6) — mismo archivo, no se mueve
│   ├── vendors/
│   │   ├── base.py                 # existente, reescrito (Fase 1) — VendorDriver, absorbe port_driver_base.py
│   │   ├── mock.py                 # existente, reescrito (Fase 1) — MockVendor
│   │   ├── dispatcher.py           # BORRADO (Fase 1)
│   │   ├── port_driver_base.py     # BORRADO (Fase 1)
│   │   ├── huawei/driver.py        # nuevo (Fase 1)
│   │   └── cisco/driver.py         # nuevo (Fase 1)
│   │
│   ├── device_locks.py, rate_limiter.py            # BORRADOS (Fase 1)
│   ├── effective_role.py                            # BORRADO (Fase 2)
│   ├── group_job_service.py                         # BORRADO (Fase 4)
│   ├── orchestration_runner.py, retry_policy.py     # BORRADOS (Fase 5)
│   ├── vlan_service.py, port_service.py,
│   │   vlan_execution_service.py, port_execution_service.py,
│   │   port_config_service.py, job_service.py, audit_service.py,
│   │   device_service.py, site_service.py, device_group_service.py  # BORRADOS (Fase 7)
│   │
│   └── role_assignment_service.py, refresh_token_service.py,
│       auth_service.py                              # SIN TOCAR — fuera de alcance (ver "Alcance" arriba)
│
├── validators/                    # BORRADO el paquete completo (Fase 2) — funciones absorbidas como privadas de vlan.py/port.py
│
├── tasks.py                       # nuevo (Fase 5) — un solo Celery task genérico
├── composition.py                  # nuevo (Fase 1 en adelante) — wiring, ver lista completa abajo
└── api/
    ├── vlans.py, ports.py, jobs.py            # reescritos (Fase 5)
    ├── group_jobs.py                           # reescrito (Fase 2 + Fase 4)
    └── devices.py, sites.py, device_groups.py  # reescritos (Fase 6)
```

**Reglas de nombres, no solo de ubicación**:
- Clases nuevas: **una clase por archivo**, nombre de archivo en `snake_case` del
  nombre de la clase (`PluginRegistry` → `plugin_registry.py`). Excepción: los
  Value Objects/entidades chicas que viven junto a la entidad que las usa (ej.
  `DefaultGroupImmutableError` junto a `DeviceGroup`) — evaluar caso a caso, no
  crear un archivo de 5 líneas por cada excepción.
- `app/composition.py` es la única excepción a "una clase por archivo" — no es una
  clase, es el punto de wiring. Si crece demasiado, se parte en `app/composition/`
  (paquete, un archivo por área — persistencia/seguridad/vendors), no antes de que
  eso pase de verdad.
- `app/repositories/` es exclusivamente para subclases de `Repository[T]` (hereden o
  no la implementación genérica, ver Fase 3) — nunca para lógica de aplicación
  (`Orquestador`, `Inventory`, etc., que van en `app/services/`).
- `app/models/` es exclusivamente para dataclasses/Protocols sin acceso a DB — si
  algo en ese archivo abre `get_session()`, está en el paquete equivocado.

**`app/composition.py` — lista completa de singletons al terminar las 7 fases**
(usar esto como checklist, no reconstruirla por fase):

`plugin_registry`, `secret_vault`, `redis_coordinator` (Fase 1) ·
`vlan_repository`, `puerto_repository`, `role_assignment_repository` (Fase 2) ·
`device_repository`, `device_group_repository`, `audit_repository`,
`login_attempt_repository` (Fase 3) · `job_repository` (Fase 4) ·
`orquestador`, `event_dispatcher`, `group_operation_runner`, `cleanup_scheduler`
(Fase 5) · `site_repository`, `inventory` (Fase 6).

## Las 2 líneas de trabajo

- **Línea A — Dominio/Orquestación/Topología**: `Device`, `VLAN`, `Puerto`, `Site`,
  `DeviceGroup`, `Inventory`, `RecursoGestionable`, `Orquestador`,
  `GroupOperationRunner`, `Job`, `JobRepository` (esqueleto), `Repository[T]`
  genérico, `PluginRegistry`, `VendorDriver`, `EventDispatcher`/`DomainEvent`/
  `AuditListener`, `SiteRepository`.
- **Línea B — Identidad/Seguridad/Infra**: `VisibilityScope`, `RoleAssignmentRepository`,
  `AuditRepository`, `LoginAttemptRepository`, `DeviceRepository`,
  `DeviceGroupRepository`, `SecretVault` (consolidación), `RedisCoordinator`,
  `CleanupScheduler`.

Ninguna fase requiere que A y B avancen al mismo ritmo — una línea puede estar 2 fases
adelante de la otra. Lo único que hay que respetar son los **sync points explícitos**
marcados en cada `FASE_N.md` (archivos que ambas líneas tocan, en momentos distintos,
nunca en simultáneo) — el único estricto de todo el plan es `JobRepository` en Fase 4.

## Convención: `Repository[T]` y el campo de identidad

`Repository[T].get(pk)`/`.remove(pk)` no asumen que la PK sea siempre `.id`, y aceptan
tanto un valor simple como una tupla (PK compuesta — `VLAN`/`Puerto`, Fase 2). Para
`Device` la clave natural real es `.name`; `DeviceModel.id` (autoincrement) y
`Device.id` (dataclass, `uuid4()`) son tipos incompatibles, no usar `.id` como PK de
upsert ahí. Mismo patrón para `Job` (`.job_id`, no `.id`). Cada instancia de
`Repository[T]` declara su `pk_field` real — ver `FASE_1.md` para el diseño completo.

## Tabla de fases

| Fase | Línea A | Línea B | Sync point |
|---|---|---|---|
| 1 | `Repository[T]` genérico · fusión de drivers (6→3) · `PluginRegistry` · `Device` limpio (`driver`/`password`) | `SecretVault` (saca el shim) · `RedisCoordinator` (fusiona locks+rate limit, arregla el fallback permanente) | Ninguno |
| 2 | `VLAN` completo · `Puerto` completo (unifica `PortInfo`+`PortConfigRequest`, `mutation_fields`) — validators absorbidos como privados | `VisibilityScope` + `RoleAssignmentRepository.scope_de()` · reescribe `require_scope()` · migra 18 call sites de `effective_role()` | B espera a que A-Fase1 (`Repository[T]`) esté terminado |
| 3 | `EventDispatcher`+`DomainEvent`(con `actor`/`exitoso`)+`AuditListener` | `AuditRepository` (insert estricto) · `LoginAttemptRepository` (no hereda) · `DeviceRepository`/`DeviceGroupRepository` (nombres/grupos visibles) | Contrato de nombre de método acordado, no bloqueante |
| 4 | `Job` rico (+`operation`) · `JobRepository` (esqueleto) · elimina `GroupJob`/`group_job_service.py` | Agrega `JobRepository.query(scope)` al archivo que A crea | **Estricto** — único sync point real del plan |
| 5 | `RecursoGestionable` · `Orquestador` (+retry real) · `GroupOperationRunner` (dispatch paralelo real, 1 task/device — mejora consciente sobre el código actual) | `CleanupScheduler` | A necesita `RedisCoordinator` (Fase 1) |
| 6 | `Site`/`DeviceGroup` (entidades) · `SiteRepository` (7ma subclase, transaccional) · `Inventory` reescrita (cap real de 5 métodos) · `Device.actualizar()` | Integración liviana — confirmar que `Inventory` usa `DeviceRepository.nombres_visibles()` | Ninguno nuevo |
| 7 | Limpieza cruzada final — borrar los 11 archivos sin caller real, sweep, verificación contra `FINAL_ARCHITECTURE.md` §1 | ídem | Coordinar qué se borra |
