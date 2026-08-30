# Fase 7 — Limpieza cruzada y sweep final

> Parte de [plan de migración a `FINAL_ARCHITECTURE.md`](README.md). Requiere
> [`FASE_1.md`](FASE_1.md)–[`FASE_6.md`](FASE_6.md) terminadas — en particular
> [`FASE_6.md`](FASE_6.md) (`Inventory`/`Site`/`DeviceGroup`), sin la cual varios de
> los archivos de esta lista todavía tienen caller real y no se pueden borrar.

## Objetivo

Borrar los archivos que quedaron sin ningún caller real después de las Fases 1-6,
y hacer un sweep final de referencias.

## Estado previo esperado

- Fases 1-6 completas.

---

## Línea A y Línea B (conjunto — esta fase es mayormente sweep, no separada por
línea de forma estricta; cada ítem lista a quién le toca por si hace falta
coordinarse)

### 1 — Borrar archivos sin caller real

Correr `grep -rn "import <módulo>"` sobre `app/` para cada uno antes de borrar —
esta lista es la esperada según lo que cada fase anterior ya dejó documentado,
pero confirmar contra el código real en el momento, no asumir que nada cambió
entre fases:

| Archivo | Reemplazado por | Fase que lo dejó sin caller |
|---|---|---|
| ~~`app/services/vlan_service.py`~~ **Borrado** | `VLAN` + `Orquestador` | Fase 5 (A6) |
| ~~`app/services/port_service.py`~~ **Borrado** | `Puerto` + `Orquestador` | Fase 5 (A7) |
| ~~`app/services/vlan_execution_service.py`~~ **Borrado** | `GroupOperationRunner` + `app/tasks.py` para VLAN/Puerto (Fase 5); `Orquestador.ejecutar_comando()` + `app.tasks.guardar_config_task` para `save_device_config()` (Fase 7, ver abajo) | Fase 5 (A4/A5) para VLAN/Puerto; Fase 7 para `save_device_config()` — bloqueaba el borrado hasta migrar ese camino, ver la corrección de esta sección abajo |
| ~~`app/services/port_execution_service.py`~~ **Borrado** | ídem (VLAN/Puerto) | Fase 5 |
| ~~`app/services/port_config_service.py`~~ **Borrado** | ídem (VLAN/Puerto) | Fase 5 |
| ~~`app/services/group_job_service.py`~~ **Borrado** | `JobRepository.resumen_de_grupo()` (Fase 4 A3) | Fase 7 — bloqueado por `enqueue_save_job()` hasta la migración de esta sección |
| ~~`app/models/group_job.py`~~ (`GroupJob`, `DeviceExecution`) **Borrado** | ídem | Fase 7, misma nota |
| ~~`app/services/job_service.py`~~ **Borrado** | `Job` + `JobRepository` | Fase 5 (A8/A9) para `api/jobs.py`/`main.py`; Fase 7 para `save_device_config()` |
| ~~`app/services/retry_policy.py`~~ **Borrado** | `Orquestador._clasificar_error()`/`_ejecutar_con_retry()` | Fase 7 — **corrección real: NO estaba realmente sin caller en Fase 5 como se documentó entonces.** `vlan_execution_service.py` lo importaba a nivel de módulo (línea 7, `from app.services.retry_policy import RetryDecision, classify_error`) — como ese archivo seguía vivo por `enqueue_save_job()`, el import corría igual aunque ninguna función de `retry_policy` se llamara desde el único camino vivo. El grep de Fase 5 chequeó "¿alguien llama a sus funciones?", no "¿alguien lo importa a nivel de módulo?" — quedó bloqueado hasta ahora sin que nadie lo notara |
| ~~`app/services/orchestration_runner.py`~~ **Borrado** | `Orquestador.ejecutar()` | Fase 5 (A3) — este sí era seguro: sus imports en `vlan_execution_service.py` eran locales/lazy (dentro de funciones muertas), nunca corrían al importar el módulo |
| ~~`app/services/cleanup_service.py`~~ **Borrado** | `CleanupScheduler` + `LoginAttemptRepository.purgar_antiguos()` | Fase 5 (B1) |
| ~~`app/services/audit_service.py`~~ **Borrado** | `AuditListener` + `AuditRepository` | Fase 5 (A3/A8/A9/B1) para `Orquestador`/`api/jobs.py`/`main.py`; Fase 7 (§1.5/1.6 abajo) para `role_assignment_service.py`/`api/users.py`/`api/auth.py`/`api/audit.py`/`save_device_config()` |
| ~~`app/services/device_service.py`~~ **Borrado** | `Device` + `DeviceRepository` + `Inventory` | Fase 6 |
| ~~`app/services/site_service.py`~~ **Borrado** | `Site` + `SiteRepository` + `Inventory` | Fase 6 |
| ~~`app/services/device_group_service.py`~~ **Borrado** | `DeviceGroup` + `Repository[DeviceGroup]` + `Inventory` | Fase 6 |
| ~~`app/validators/vlan_validator.py`~~ **Borrado** | `VLAN` (funciones privadas de `models/vlan.py`, copiadas en Fase 2 A1) | Fase 5 (A6) |
| ~~`app/validators/port_validator.py`~~ **Borrado** | `Puerto` (funciones privadas de `models/port.py`, copiadas en Fase 2 A2) | Fase 5 (A7) |

**`app/services/inventory_service.py` — corrección: nunca estuvo en esta
tabla, no es un archivo a borrar.** Una versión anterior de esta sección
tenía una fila "`inventory_service.py` → `Inventory` (nueva ubicación,
Fase 6)" que daba a entender que el archivo quedaba muerto — falso, la
reescritura de Fase 6 (A5) puso la nueva clase `Inventory` **en el mismo
archivo**, no lo movió. `README.md` (Convención de directorios) ya lo tenía
bien: *"existente, Inventory reescrita in situ (Fase 6) — mismo archivo, no
se mueve"*.

**Los 5 archivos que esta sección marcaba "NO borrar todavía" (`vlan_execution_service.py`,
`group_job_service.py`, `models/group_job.py`, `job_service.py`, y transitivamente
`retry_policy.py`) están todos borrados ahora — resueltos en Fase 7 migrando
`save_device_config()` (`POST /devices/{name}/save`) fuera de
`vlan_execution_service.enqueue_save_job()`.** De paso se encontró que ese
endpoint ya estaba roto en producción, sin relación con esta fase:
`enqueue_save_job()` despachaba el task Celery `ansiauth.vlan.save`
(`_save_task`, definido en el propio `vlan_execution_service.py`), pero
`worker.py: include=["app.tasks"]` (Fase 5, A5) dejó de importar ese módulo
— ningún worker real tenía el task registrado, así que el endpoint
respondía 200 con un job_id que nunca avanzaba de "pending". Fix: nuevo
`Orquestador.ejecutar_comando()` (variante de `ejecutar()` para comandos que
no son un `RecursoGestionable` — no hay `Repository` al que persistir "guardé
la config") + `app.tasks.guardar_config_task` (mismo módulo que ya incluye
`worker.py`, sin tocar ese include). Ver `FASE_6.md` "Correcciones reales",
punto 8, para el hallazgo original.

**No son archivos, son clases dentro de un archivo que sobrevive — Borradas.**
`PortInfo`/`PortConfigRequest` quitadas de `app/models/port.py` (confirmado
con `grep -rn "PortInfo\|PortConfigRequest" app/` → 0 resultados fuera de
comentarios/docstrings históricos que no se tocan).

**Corrección real, encontrada después de cerrar esta fase — `PortListResponse`
también estaba sin ningún caller real, y esta sección la había dado por
buena sin confirmar.** El texto original de este punto decía "`Puerto`/
`PortConfigResult`/`PortListResponse` son las que se quedan" — copiado de
`FINAL_ARCHITECTURE.md` sin correr el mismo grep que sí se corrió para
`PortInfo`/`PortConfigRequest`. `grep -rn "PortListResponse" app/` → 0
resultados fuera de su propia definición: `VendorDriver.list_ports()`
devuelve `list[Puerto]` directo (`vendors/base.py:217`), nunca un
`PortListResponse` — el propio docstring de la clase ("Envelope returned by
VendorDriver.list_ports") describía algo que nunca se implementó así.
Borrada de `app/models/port.py`.

**Segunda corrección, mismo hallazgo llevado un paso más — `PortConfigResult`
tampoco tenía motivo real para seguir siendo una clase aparte.** Al revisar
por qué "se queda" (a diferencia de `PortListResponse`), el único uso real
era `Puerto.aplicar()`'s rama compuesta destructurándola de vuelta a un dict
una línea después de recibirla (`resultado.to_dict()` + `"rc": 0 if
resultado.success else 1`) — nada más en el código leía sus atributos como
objeto. Contraste real con el resto del catálogo: `create_vlan()`/
`update_port_description()`/`set_port_admin_state()`/etc — **todos** los
demás métodos de mutación de `VendorDriver` ya devuelven `dict`
(`{"rc", "stdout", "stderr", "success"}`); `configure_port()` era la única
excepción, sin que hubiera una razón real para serlo. Fix: `VendorDriver.
configure_port()` pasa a devolver `dict`, mismo contrato que sus 8
hermanos — `PortConfigResult` borrada de `app/models/port.py`, las 3
implementaciones reales (`mock.py`/`huawei/driver.py`/`cisco/driver.py`)
y `Orquestador._rollback_puerto()` (que accedía `resultado.success` como
atributo, ahora `resultado.get("rc", 1) == 0`) actualizadas. Efecto
colateral real, en la dirección buena: `HuaweiVendor.configure_port()`
descartaba el `rc`/`stdout`/`stderr` reales del playbook para armar
`PortConfigResult` a mano con un `success` derivado — al volver a `dict`,
esos 3 campos reales sobreviven en la respuesta en vez de perderse.
`Puerto`/`PortConfigResult` en la lista de "las que se quedan" arriba
también quedó desactualizada por esta segunda corrección — solo `Puerto`
se queda.

Si alguno de estos **todavía tiene un caller real** al llegar a esta fase (el
grep lo va a mostrar), no borrarlo — es señal de que alguna fase anterior quedó
incompleta, hay que volver y cerrarla antes de seguir acá.

**No se borran** (fuera de alcance de todo este plan, confirmado explícitamente):
`role_assignment_service.py` (ya está bien hecha, `FINAL_ARCHITECTURE.md` lo dice
expreso), `refresh_token_service.py`, `auth_service.py` (dependen de
`AutenticacionService`, nunca asignada a ninguna fase — si en el futuro se decide
migrarlos, es una fase nueva, no una extensión de esta).

### 1.5 — `role_assignment_service.py` necesita un cambio puntual antes de borrar `audit_service.py` — Hecho

**Encontrado real, no hipotético — "no tocar" no alcanzaba tal como estaba escrito
hasta esta revisión.** `role_assignment_service.py` (que se queda, sin migrar, ver
arriba) importa `audit_service` y llama `audit_service.log_action()` en 4 lugares
reales (`grant()`/`revoke()`, líneas 123, 144, 181, 271) — verificado que es la
**única** de las 4 funciones fuera de alcance (`role_assignment_service.py`,
`refresh_token_service.py`, `auth_service.py`, `user_service.py`) con una
dependencia real hacia algo que este plan borra; las otras 3 solo importan de
`app.db`/`app.core.config`/`app.schemas`. Sin arreglar esto, borrar
`audit_service.py` en la sección 1 rompe `role_assignment_service.py` — que
íbamos a dejar "sin tocar".

**Fix, acotado a las 4 llamadas, no una reescritura del archivo** — cada
`audit_service.log_action(...)` pasa a `audit_repository.append(AuditRecord(...))`.
No es un swap 1 a 1 de argumentos (`log_action()` es una función con
keyword-args; `append()` recibe un objeto ya armado) — con los defaults que
`AuditRecord` gana en Fase 3 (`id`/`timestamp`/`status`, ver esa fase), el
cuerpo queda casi igual de corto:

```python
# antes
audit_service.log_action(
    user=(actor.get("username") if actor else None) or "system",
    action="grant_role_assignment", resource="role_assignment",
    resource_id=str(record.id), details={...},
)
# después
audit_repository.append(AuditRecord(
    user=(actor.get("username") if actor else None) or "system",
    action="grant_role_assignment", resource="role_assignment",
    resource_id=str(record.id), details={...},
))
```

Hacer este cambio **antes** de borrar `audit_service.py` en la sección 1 (orden
real, no solo lógico — si se borra primero, `role_assignment_service.py` queda
con un import roto en el medio de esta misma fase).

**De paso, aplicar acá un hallazgo real que quedó pendiente desde la revisión de
`FINAL_ARCHITECTURE.md`** (tabla de hallazgos, §6: *"Mismo patrón, 3ra vez:
`RoleAssignmentService.grant()` no audita un re-grant del mismo rol"*) — nunca se
aplicó porque este archivo estaba marcado "no tocar" en todas las fases
anteriores. Ya que esta fase lo toca de todos modos (por el punto de arriba), es
el momento de cerrarlo: sacar el `if existing.role != role:` que hoy guarda toda
la llamada a `log_action()` (línea 120 real) — auditar siempre, con
`"noop": True` en el payload cuando el rol no cambió, mismo criterio ya aplicado
a `Inventory.move()` (nota (6) de `FINAL_ARCHITECTURE.md`).

### 1.6 — `audit_service.py` tiene más callers reales de los que cualquier fase cubrió — Hecho

**Encontrado con el mismo método que 1.5 (grep de quién importa cada módulo que
se borra) — sistemático, no solo el caso de `role_assignment_service.py`.**
Ninguna fase anterior tocó estos 4 archivos, y los 4 llaman a `audit_service`
directo:

| Archivo | Llamadas reales | Resolución |
|---|---|---|
| `app/main.py` | 2 (`_bootstrap_admin()`, `request_validation_error_handler()`) | Hecho en Fase 5, A9 |
| `app/api/users.py` | 3 (`create_user`/`update_user`/`deactivate_user`) | Hecho — las 3 migradas a `audit_repository.append(AuditRecord(...))` |
| `app/api/auth.py` | 6 (login × 4 variantes, `token_refresh`, `unlock_account`) | Hecho — las 6 migradas |
| `app/api/audit.py` | 3, pero **distintas** — no son `log_action()`, son las funciones de lectura/purga del router de auditoría mismo | Hecho — reescrito contra `AuditRepository.query()`/`.purge_old()`, ver snippet abajo |
| `app/services/vlan_execution_service.py: enqueue_save_job()` — 5to caller, encontrado en Fase 6 | 1 (`audit_service.log_action(..., status="pending", ...)`) | Hecho — todo el camino de `save_device_config()` migrado en Fase 7 §1 (`Orquestador.ejecutar_comando()`/`app.tasks.guardar_config_task`), no solo esta llamada. `vlan_execution_service.py` completo está borrado |

**Hallazgo adicional, no resuelto — confirmado con un test end-to-end, no
solo leído.** `AuditRepository._aplicar_scope()` (Fase 3) no tiene ninguna
condición para `resource in ("role_assignment", "user")` — sus 4 condiciones
OR cubren `device`/`resource=="auth"`/`resource=="device_group"`/
`resource=="site"` solamente. Un site-admin que otorga/revoca un grant en su
propio site, o cuyos propios datos de usuario cambian, **no ve esas filas**
en `GET /audit` — solo un system-admin las ve (bypassea el filtro de scope
entero). No es un problema nuevo de esta fase (el filtro es de Fase 3, antes
de que `role_assignment_service.py`/`api/users.py` tuvieran ningún caller
real hacia `AuditRepository`), pero recién ahora que estas rutas están
conectadas se vuelve observable. Arreglarlo requiere una decisión de diseño
real (¿un site-admin ve `role_assignment` audit rows de qué sites? — el
`resource_id` de esas filas es el id del grant, no el site_id, así que un
`IN` directo no alcanza, haría falta mirar `details->site_id`) — queda
documentado, no resuelto, para quien lo tome.

**`api/audit.py` es el caso más importante de encontrar acá — es el router que
expone lo que `AuditRepository` (Fase 3) existe para servir, y nadie lo había
conectado.** Reescribir sus 2 endpoints reales:

```python
@router.post("/purge", ...)
def purge_audit_log(retention_days: Optional[int] = Query(default=None, ge=1), current_user=Depends(require_system_admin)):
    days = retention_days if retention_days is not None else AUDIT_RETENTION_DAYS
    deleted = audit_repository.purge_old(days, triggered_by=current_user["username"])
    return {"success": True, "data": {"deleted": deleted, "retention_days": days}}


@router.get("/", ...)
def get_audit_log(response: Response, user=None, action=None, resource=None, status=None,
                   from_date=None, to_date=None, device_id=None, site_id=None,
                   skip: int = 0, limit: int = 100, page=None, page_size=None,
                   current_user=Depends(require_authenticated),
                   scope: "VisibilityScope" = Depends(obtener_scope)):
    # paginación real acepta 2 formatos (skip/limit y page/page_size) -- mismo
    # cálculo real de api/audit.py:68-72, no perderlo
    if page is not None or page_size is not None:
        effective_page_size = page_size if page_size is not None else limit
        effective_page = page if page is not None else 1
    else:
        effective_page = (skip // limit) + 1 if limit else 1
        effective_page_size = limit
    records, total = audit_repository.query(
        user=user, action=action, resource=resource, status=status,
        from_date=from_date, to_date=to_date, device_id=device_id, site_id=site_id,
        scope=scope, page=effective_page, page_size=effective_page_size,
    )
    response.headers["X-Total-Count"] = str(total)
    response.headers["Access-Control-Expose-Headers"] = "X-Total-Count"
    return records
```

**Nota real sobre el `count()` separado que hace el endpoint real hoy** (2
queries: una para `total`, otra para los datos) — `AuditRepository.query()`
(Fase 3, ya corregida arriba) devuelve `(records, total)` en **una sola**
llamada — el endpoint nuevo no necesita 2 queries donde el real usa 2, es una
simplificación real que cae sola al usar el método ya diseñado. `count()`
(el método aparte de `AuditRepository`) queda para quien necesite **solo** el
total sin las filas — no hace falta acá.

### 2 — Sweep de referencias muertas en documentación/comentarios

- `docs/DEVICE_IMPLEMENTATION_PLAN.md`/`docs/plan-migracion-modelo-dominio.md` —
  **no tocar** (decisión ya tomada al principio de este plan, quedan como
  historia).
- Cualquier comentario real en código que quede citando un archivo borrado (ej.
  `"ver vlan_execution_service.py:340-372"` dentro de un docstring nuevo de
  `VLAN.aplicar()`, Fase 2) — actualizar la cita a la ubicación nueva o borrarla
  si ya no aporta, caso por caso.

### 3 — Confirmar que `app/composition.py` está completo

Checklist de todos los singletons que las 6 fases anteriores fueron agregando —
confirmar que existen y ninguno quedó a mitad de camino. Ver la lista completa
en el `README.md` de esta carpeta, sección "Convención de directorios" — no
repetirla acá para no tener 2 listas que se puedan desincronizar.

Si en algún punto `app/composition.py` se sintió demasiado plano para seguir
agregándole cosas (ya se había marcado como riesgo posible en su momento),
este es el momento de decidir si se parte en `app/composition/` (paquete, un
archivo por área) — no antes, para no reorganizar algo que todavía se estaba
construyendo.

### 4 — Verificación final contra `FINAL_ARCHITECTURE.md` — Hecho

Recorrido completo de §1 (~52 clases) y §1.6 (catálogo detallado), cada una
contra el código real. ~40 de ~50 existen con el nombre y ubicación exactos.
Hallazgos, agrupados por qué hacer con cada uno:

**Clases que el documento nombra y no existen — todas fuera de alcance del
plan, ninguna es un gap de esta migración:**
- `Usuario`, `PasswordHasher`, `AutomationEngine`/`AnsibleAutomationEngine`,
  `TransportSecurityMiddleware`, `InterfazVirtual`/`ConfiguracionGlobal` —
  todas dependen de `AutenticacionService`/RF-INTERV/RF-GLOBAL, explícitamente
  fuera de alcance (`README.md`, sección "Alcance"). `ansible_service.py:
  run_playbook()`/`user_service.py`/`core/security.py`'s `CryptContext`
  duplicado/`core/tls_middleware.py` (HSTS + redirect en 2 clases, no 1)
  siguen como están hoy — correcto, ninguna fase los tocaba.
- `DomainError` (base de la jerarquía de excepciones), `DeviceUnreachableError`,
  `AuthorizationError`, `ConflictError`→`ResourceConflictError` (rename nunca
  aplicado) — el propio `README.md` enumera exactamente qué excepciones
  agrega cada fase y ninguna de estas 4 está ahí — el documento de
  arquitectura es el lado desactualizado, no el código.

**Único hallazgo real, no cubierto por ninguna fase — duplicación
preexistente, no introducida por esta migración:** `parsers/_common.py`
(módulo compartido para `_ANSI_ESCAPE`/`limpiar_lineas()`, propuesto en
§1.6 vendors) nunca se creó — el regex ANSI y la función de limpieza siguen
triplicados en `parsers/port_parser.py`, `parsers/vlan_parser.py`,
`parsers/cisco_port_parser.py`. Ninguna fase de este plan lo asignó
(`FINAL_ARCHITECTURE.md` lo menciona, pero ninguna `FASE_N.md` lo tiene en
su alcance) — queda documentado como la limpieza más barata que sigue
pendiente, para quien la tome, no resuelta acá (fuera del alcance que se
definió para esta fase, que es sweep de lo que las 6 fases anteriores sí
tocaron).

**Nombre/ubicación distinta al documento, código correcto — el documento es
el lado desactualizado en los 5 casos:**
- `JobQueue` (no `CeleryJobQueue`) — `FASE_5.md` ya documentó esta decisión
  explícita (fusiona el port y la implementación Celery en una clase).
- `RoleAssignment` vive en `app/repositories/role_assignment_repository.py`,
  no en `app/models/` — mismo criterio que otras entidades sin
  comportamiento propio real todavía.
- `Site.tiene_devices()` vive en `SiteRepository`, no en la entidad `Site`
  (ya documentado en A1/A3 de `FASE_6.md` — necesita una query).
- `LoginAttemptRepository` no hereda de `Repository[T]` — el propio
  `README.md` ya lo aclara ("NO hereda").
- `EventDispatcher` usa una lista plana + `suscribir(listener)`, no un
  `dict[type, ...]` — suficiente para el único listener real (`AuditListener`).

Ningún constructor/colaboradores inyectados salió mal diseñado —
`Orquestador`/`GroupOperationRunner` coinciden con §2.4/§2.9 incluida la
corrección de `RedisCoordinator` (ver `FASE_5.md`).

### 5 — Smoke-check liviano, sin test suite

No reemplaza tests reales (fuera de alcance, ya decidido) — es gratis y detecta
roturas obvias de import/wiring que un `grep` no ve (ej. un ciclo de imports
nuevo en `composition.py`). Al cerrar **cada fase** de este plan (no solo esta),
correr:

```
python -c "import app.composition"
python -c "import app.main"
```

Si cualquiera de los 2 lanza `ImportError`/`AttributeError`, hay algo roto que
el checklist de esa fase no capturó — pararse ahí, no seguir a la fase
siguiente. Agregar este chequeo como último paso del criterio de finalización
de **todas** las fases (1 a 7), no solo esta — se documenta acá porque es Fase
7 la que revisó todo el plan de punta a punta y lo notó, pero aplica desde
Fase 1.

### 6 — Trazabilidad RF/RNF final — Hecho

Las ~35 citas RF-/RNF- de `FINAL_ARCHITECTURE.md` recorridas una por una
contra el código real (no solo contra el documento). **26 PASS, 3 FAIL/gap
real, 2 PARTIAL, 6 N/A por alcance ya excluido explícitamente** (`RF-INTERV-*`/
`RF-GLOBAL-*`/`RNF-DISP-01`, el propio documento ya los marca "no
implementado hoy" — no son gaps de esta migración).

**RF-AUD-01/02 — PASS.** `api/audit.py` (§1.6) registra y permite consultar
por `user`/`action`/`resource`/`status`/`device_id`/`site_id`/fecha, tal
como pide el SRS. La granularidad por operación (`"accion"` en el payload)
sigue viva. El gap de visibilidad `role_assignment`/`user` para no-admins
(§1.6 arriba) no afecta esta cita — RF-AUD-02 pide que la consulta exista y
filtre por esos campos, no que todo usuario vea todo.

**RNF-API-05 (idempotencia) — 3 hallazgos reales:**
1. **Corregido en el momento**: `Orquestador.ejecutar_comando()` (agregada
   en esta misma fase, arriba) no tenía el guard
   `job.esta_en_estado_terminal()` que `ejecutar()` sí tiene desde Fase 4/5
   — una reentrega de Celery sobre un job de `guardar_config` ya completado
   crasheaba con `TransicionInvalidaError` en vez de no-op. Un `if
   job.esta_en_estado_terminal(): return` al principio del método, mismo
   lugar que `ejecutar()`. Confirmado con test end-to-end (reentrega
   simulada sobre un job `completed`, ya no lanza).
2. **`VLAN.aplicar()` es idempotente de verdad** (`noop: True` si ya existe
   con el mismo nombre / ya no existe al borrar) — confirmado, sin cambios.
3. **`Puerto.aplicar()` no tenía ninguna rama no-op — preexistente de Fase 2,
   no introducido por esta fase. Corregido en esta fase.** Las 4 ramas de
   campo único (`description`/`admin_up`/`access_vlan`/`allowed_vlans`)
   llamaban al driver directo sin comparar contra el estado reconciliado
   primero — un `PATCH /ports/description` repetido con el mismo valor
   volvía a mandar el comando al device cada vez, en vez de detectar que ya
   estaba aplicado. Contraste real con `VLAN.aplicar()`, que sí lo hace desde
   Fase 2. Fix: `Puerto._noop_resultado(accion)` (mismo shape que el no-op
   de `VLAN.aplicar()` — `rc`/`success`/`changed`/`noop`/`accion`) + un
   chequeo "¿el valor pedido ya coincide con lo reconciliado?" al principio
   de cada una de las 4 ramas — `description`/`admin_up`/`access_vlan`
   comparan directo; `allowed_vlans` compara el resultado YA calculado
   (`deseados`, después de aplicar `allowed_vlan_operation`) contra la lista
   actual, no `self.allowed_vlans` crudo contra la actual (un "add" de VLANs
   ya presentes es un no-op real aunque la lista pedida no sea idéntica a la
   actual). Confirmado con test end-to-end, las 4 ramas.

   **La rama compuesta (`configure_port()`) queda deliberadamente sin
   rama no-op — alcance distinto, no un caso olvidado.** Comparar "ya está
   aplicado" contra varios campos a la vez (incluida `allowed_vlan_operation`
   sobre una lista, dentro de un conjunto de campos que puede incluir
   `mode`) es una pregunta combinatoria distinta a las 4 ramas de campo
   único — documentado en el propio código (`Puerto.aplicar()`), no resuelto
   acá.

**RNF-ESCAL-01 (lock/rate-limit consistente entre instancias) — 2
hallazgos:**
1. **El lock (`bloquear()`) está bien** — confirmado inyectado y usado en
   los 4 lugares reales documentados (`Orquestador.ejecutar()`/
   `ejecutar_comando()`, `api/vlans.py`/`_leer_vlans_en_vivo()`,
   `api/ports.py` × 2). Cero imports reales de `device_locks`/`rate_limiter`
   quedan (solo menciones en comentarios/docstrings, ya esperado).
2. **`RedisCoordinator.limitar()`/`.resetear()` (rate limit por device) —
   código muerto, nadie los llama. Corregido en esta fase.** Diseñados en
   Fase 1 (fusionan `rate_limiter.py`), pero `Orquestador.ejecutar()`/
   `ejecutar_comando()` solo llamaban `.bloquear()`, nunca `.limitar()` — el
   límite de requests/seg por device que el documento describe
   (`RedisCoordinator` §2, "limita cuántos requests por segundo entran por
   device") no se aplicaba en ningún camino real. Mismo tipo de hueco que el
   de `bloquear()` que Fase 5 ya encontró y cerró (ver A3 de `FASE_5.md`).
   Fix: `self._coordinador.limitar(device_name)` agregado dentro del `with
   ...bloquear(device_name):` de ambos métodos, antes de tocar el device.

   **Efecto colateral real, encontrado al conectarlo — no al diseñarlo.**
   Con los valores originales (`MAX_JOBS_PER_WINDOW=5`, `WINDOW_SECONDS=60`,
   nunca ejercitados hasta ahora), un test end-to-end realista (varias
   operaciones seguidas de escritura sobre el mismo device — el mismo patrón
   que un batch legítimo de "editar 6+ puertos de un switch") colgó 2+
   minutos: la 6ta operación en la ventana de 60s bloqueaba genuinamente
   hasta que se liberaba un slot. `MAX_JOBS_PER_WINDOW` nunca se había
   ejercitado desde que se escribió (Fase 1) — el valor `5` no estaba
   calibrado contra ningún caso de uso real. Subido a `30` (mismo
   `WINDOW_SECONDS=60`) — sigue protegiendo contra un loop descontrolado
   real sin frenar un batch de tamaño normal. Ver
   `app/services/redis_coordinator.py` para el comentario completo.

**Hallazgos menores, ninguno bloqueante, todos preexistentes fuera de
alcance de las 7 fases de este plan:**
- RNF-ERR-02 (excepciones distinguen conexión/auth/validación/ejecución) —
  FAIL real, pero el propio `FINAL_ARCHITECTURE.md` se contradice y en otro
  punto admite que esto no está implementado — el código coincide con la
  admisión, no con la claim optimista. `core/exceptions.py` no tiene tipos
  de conexión/auth separados; todo cae en `DeviceExecutionError`.
- RNF-MANT-01 (`AutomationEngine` como interfaz explícita) — la
  modularidad real existe vía `VendorDriver`/`PluginRegistry`, pero no la
  indirección `AutomationEngine.run(...)` que el documento cita como
  "cumplida por estructura" — ningún caller pasa por una interfaz así,
  los 2 drivers llaman `ansible_service.run_playbook()` directo. Nunca
  asignado a ninguna fase.
- RNF-PERF-01 (50 devices concurrentes, &lt;5s) — solo el habilitador
  estructural (1 Celery task por device) existe; sin benchmark ni bound de
  concurrencia en código — no medible con una revisión de código sola,
  necesita una prueba de carga real, fuera de alcance de este plan.
- `doc:1042-1063` (limitación de `_redis_checked` permanente) — el
  documento describe una limitación que el código **ya no tiene**
  (`RedisCoordinator._get_redis()` re-prueba cada 30s desde Fase 1) —
  documento desactualizado, código correcto, sin acción.

## Criterio de finalización

- [x] `role_assignment_service.py`: las 4 llamadas a `audit_service.log_action()`
      migradas a `audit_repository.append()` (sección 1.5), **antes** de borrar
      `audit_service.py`. El re-grant del mismo rol audita con `"noop": True` —
      confirmado con test end-to-end (`grant()` real, misma rol dos veces).
- [x] `main.py`/`api/users.py`/`api/auth.py`: las 11 llamadas a
      `audit_service.log_action()` migradas a `audit_repository.append()`
      (sección 1.6) — `main.py` ya venía hecho de Fase 5/A9, `users.py` (3) y
      `auth.py` (6) migradas acá, confirmado con test end-to-end.
- [x] `api/audit.py` reescrito contra `AuditRepository.query()`/`.purge_old()`
      (sección 1.6) — con `device_id`/`site_id`/paginación mixta (`skip`/`limit`
      y `page`/`page_size`) preservados, confirmado con test end-to-end.
- [x] `python -c "import app.composition"` y `python -c "import app.main"`
      corren sin error (sección 5) — confirmado tras cada tanda de borrados,
      no solo al final.
- [x] Trazabilidad RF/RNF de `FINAL_ARCHITECTURE.md` §6 confirmada contra el
      código final (sección 6) — ~35 citas, 26 PASS, 3 FAIL/gap real
      (encontrados), 2 PARTIAL, 6 N/A por alcance ya excluido. Los 3 FAIL
      reales, los 3 corregidos en esta fase: `Orquestador.ejecutar_comando()`
      sin el guard de estado terminal (bug de esta misma fase),
      `RedisCoordinator.limitar()` nunca conectado a `Orquestador` (gap de
      Fase 1/5, cerrado acá — con el ajuste de `MAX_JOBS_PER_WINDOW` 5→30
      encontrado al conectarlo), y `Puerto.aplicar()` sin rama no-op (gap de
      Fase 2, cerrado acá — ver detalle abajo).
- [x] Los archivos de la tabla de la sección 1 confirmados sin caller real y
      borrados — 17 archivos + `models/group_job.py` (18 en total, la sección
      1 original subestimaba el número — 5 quedaban bloqueados por
      `enqueue_save_job()`, resuelto migrando `save_device_config()`).
- [x] `PortInfo`/`PortConfigRequest` borradas de `models/port.py` (no son
      archivo aparte, ver nota bajo la tabla de la sección 1).
- [x] `app/composition.py` completo, contra la lista del `README.md` —
      verificado uno por uno (7 repositories, todos los services/singletons),
      sin faltantes.
- [x] Catálogo de `FINAL_ARCHITECTURE.md` §1/§1.6 verificado contra el código
      real (sección 4) — ~40/50 clases exactas; el resto son fuera de alcance
      del plan o el documento quedó desactualizado (código correcto). Único
      hallazgo real sin dueño: `parsers/_common.py` (ANSI escape cleanup)
      nunca se creó, sigue triplicado — ninguna fase lo tenía asignado.

## Riesgos / cosas a validar

- Esta fase resultó bastante menos mecánica de lo previsto — el hallazgo de
  `save_device_config()`/`enqueue_save_job()` (bug real preexistente en
  producción, no introducido por esta fase: el Celery task que despachaba
  quedó huérfano desde que Fase 5/A5 cambió `worker.py: include=`) obligó a
  diseñar y migrar un camino nuevo (`Orquestador.ejecutar_comando()` +
  `app.tasks.guardar_config_task`) antes de poder borrar 5 de los archivos
  de la sección 1. También se encontró que `retry_policy.py` NO estaba
  realmente sin caller como documentó Fase 5 — un import a nivel de módulo
  en `vlan_execution_service.py` lo mantenía vivo aunque ninguna función
  real lo llamara; el grep de Fase 5 chequeó "¿lo llaman?" pero no "¿alguien
  lo importa a nivel de módulo?". Lección para fases/proyectos futuros: al
  confirmar que un archivo "no tiene caller real", correr el grep sobre
  imports (`from X import`/`import X`) además de sobre llamadas a sus
  funciones — un import muerto a nivel de módulo todavía bloquea el borrado.
- Hallazgo adicional sin resolver, documentado en la sección 1.6: un
  site-admin no ve sus propios audit rows de `role_assignment`/`user` vía
  `GET /audit` — `AuditRepository._aplicar_scope()` no tiene regla de
  visibilidad para esos 2 `resource`. Preexistente a esta fase (el filtro es
  de Fase 3), recién observable ahora que estas rutas tienen caller real
  hacia `AuditRepository`. Requiere una decisión de diseño, no un fix
  mecánico — queda para quien lo tome.
