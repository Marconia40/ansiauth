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
| `app/services/vlan_service.py` | `VLAN` + `Orquestador` | Fase 5 (A6) |
| `app/services/port_service.py` | `Puerto` + `Orquestador` | Fase 5 (A7) |
| `app/services/vlan_execution_service.py` — **NO borrar todavía, ver nota abajo** | `GroupOperationRunner` + `app/tasks.py` para VLAN/Puerto (Fase 5); `enqueue_save_job()` sigue sin reemplazo | Ningún caller de VLAN/Puerto le queda tras Fase 5 (A4/A5), pero `api/devices.py: save_device_config()` (`POST /devices/{name}/save`, fuera de alcance de Fase 5/6) sigue llamando `enqueue_save_job()` — encontrado en Fase 6, ver `FASE_6.md` "Correcciones reales", punto 8 |
| `app/services/port_execution_service.py` | ídem (VLAN/Puerto) | Fase 5 |
| `app/services/port_config_service.py` | ídem (VLAN/Puerto) | Fase 5 |
| `app/services/group_job_service.py` — **NO borrar todavía, misma razón que `vlan_execution_service.py`** | `JobRepository.resumen_de_grupo()` (Fase 4 A3) para VLAN/Puerto | `enqueue_save_job()` también llama `group_job_service.create_group_job()` — mismo caller real que la fila de arriba |
| `app/models/group_job.py` (`GroupJob`, `DeviceExecution`) | ídem — su único caller real hoy es `group_job_service.py`, que a su vez sigue vivo por `enqueue_save_job()` | Misma nota que las 2 filas de arriba |
| `app/services/job_service.py` — **NO borrar todavía, misma razón** | `Job` + `JobRepository` para todo excepto `save_device_config()` | Fase 5 (A8/A9) migró `api/jobs.py`/`main.py`, pero `enqueue_save_job()` sigue llamando `job_service.create_job()` |
| `app/services/retry_policy.py` | `Orquestador._clasificar_error()`/`_ejecutar_con_retry()` | Fase 5 (A3) |
| `app/services/orchestration_runner.py` | `Orquestador.ejecutar()` | Fase 5 (A3) |
| `app/services/cleanup_service.py` | `CleanupScheduler` + `LoginAttemptRepository.purgar_antiguos()` | Fase 5 (B1) |
| `app/services/audit_service.py` — **solo parcial, ver §1.5/1.6 abajo, no entra en el sweep mecánico de esta sección** | `AuditListener` + `AuditRepository` para el camino que Fase 5 migra (`Orquestador`, `api/jobs.py`, `main.py`) | Fase 5 (A3/A8/A9/B1) para esas 3 rutas — `role_assignment_service.py`/`api/users.py`/`api/auth.py`/`api/audit.py` siguen con caller real, quedan para esta fase (§1.5/1.6) |
| `app/services/device_service.py` | `Device` + `DeviceRepository` + `Inventory` | Fase 6 |
| `app/services/inventory_service.py` | `Inventory` (nueva ubicación, Fase 6) | Fase 6 |
| `app/services/site_service.py` | `Site` + `SiteRepository` + `Inventory` | Fase 6 |
| `app/services/device_group_service.py` | `DeviceGroup` + `Repository[DeviceGroup]` + `Inventory` | Fase 6 |
| `app/validators/vlan_validator.py` | `VLAN` (funciones privadas de `models/vlan.py`, copiadas en Fase 2 A1) | Fase 5 (A6) — hasta entonces `api/vlans.py` lo llama directo, no borrar antes |
| `app/validators/port_validator.py` | `Puerto` (funciones privadas de `models/port.py`, copiadas en Fase 2 A2) | Fase 5 (A7) — hasta entonces `api/ports.py` lo llama directo, no borrar antes |

**No son archivos, son clases dentro de un archivo que sobrevive — no entran en
la tabla de arriba, pero es el mismo tipo de limpieza pendiente.** `PortInfo` y
`PortConfigRequest` (`app/models/port.py`) quedaron sin borrar en Fase 2 A2 por
el mismo motivo que los 2 validadores de arriba — `port_service.py`/
`port_config_service.py` las importaban a nivel de módulo. Una vez que Fase 5
borre esos 2 archivos (fila de arriba), las 2 clases quedan sin ningún caller
real — confirmar con `grep -rn "PortInfo\|PortConfigRequest" app/` y borrarlas
de `models/port.py` en esta misma sección (no queda ningún archivo aparte que
borrar, solo las 2 clases). `Puerto`/`PortConfigResult`/`PortListResponse` son
las que se quedan.

Si alguno de estos **todavía tiene un caller real** al llegar a esta fase (el
grep lo va a mostrar), no borrarlo — es señal de que alguna fase anterior quedó
incompleta, hay que volver y cerrarla antes de seguir acá.

**No se borran** (fuera de alcance de todo este plan, confirmado explícitamente):
`role_assignment_service.py` (ya está bien hecha, `FINAL_ARCHITECTURE.md` lo dice
expreso), `refresh_token_service.py`, `auth_service.py` (dependen de
`AutenticacionService`, nunca asignada a ninguna fase — si en el futuro se decide
migrarlos, es una fase nueva, no una extensión de esta).

### 1.5 — `role_assignment_service.py` necesita un cambio puntual antes de borrar `audit_service.py`

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

### 1.6 — `audit_service.py` tiene más callers reales de los que cualquier fase cubrió

**Encontrado con el mismo método que 1.5 (grep de quién importa cada módulo que
se borra) — sistemático, no solo el caso de `role_assignment_service.py`.**
Ninguna fase anterior tocó estos 4 archivos, y los 4 llaman a `audit_service`
directo:

| Archivo | Llamadas reales | Qué hacer |
|---|---|---|
| `app/main.py` | ~~2 (`_bootstrap_admin()` línea 111, `request_validation_error_handler()` línea 319)~~ **Ya hecho en Fase 5, A9** (`FASE_5.md`) — no queda ninguna llamada a `audit_service`/`job_service`/`cleanup_service` en `main.py`, confirmar con `grep` antes de tocar de nuevo | ~~`audit_service.log_action(...)` → `audit_repository.append(AuditRecord(...))`~~ |
| `app/api/users.py` | 3 (líneas 40, 125, 154) | ídem — `user_service.py` (el resto del archivo) sigue fuera de alcance, **solo** estas 3 llamadas cambian |
| `app/api/auth.py` | 6 (líneas 48, 59, 72, 85, 123, 163 — login éxito/fallo, logout, etc.) | ídem — `auth_service.py`/`refresh_token_service.py` (el resto) siguen fuera de alcance, **solo** estas 6 llamadas cambian |
| `app/api/audit.py` | 3, pero **distintas** — no son `log_action()`, son las funciones de lectura/purga del router de auditoría mismo | Ver abajo, no es un swap mecánico |
| `app/services/vlan_execution_service.py: enqueue_save_job()` — **5to caller, encontrado en Fase 6, no en esta revisión de Fase 7** | 1 (`audit=audit_service.log_action(..., status="pending", job_id=job.job_id, device=device_name)`, el `audit.id` se pasa al Celery task) | Llamado real desde `api/devices.py: POST /devices/{name}/save` — ninguna fase (5 ni 6) migra este camino todavía, ver `FASE_6.md` "Correcciones reales", punto 8. No es un swap mecánico de 1 línea como los de arriba: el `audit.id` devuelto se propaga a `_save_task` para actualizarlo después — hay que resolver eso también, no solo el `.log_action()` inicial |

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

### 4 — Verificación final contra `FINAL_ARCHITECTURE.md`

Recorrer el catálogo completo de §1 (~52+ clases, confirmar el número final con
las clases que agregó Fase 6 — `SiteRepository`, `DeviceGroup` como entidad, etc.)
y confirmar, una por una, que cada clase mencionada existe en el código real con
el nombre y la ubicación que este plan terminó usando — algunos nombres de
archivo se decidieron en el camino (`app/repositories/`, `app/composition.py`)
sin que `FINAL_ARCHITECTURE.md` los dictara, así que esta verificación es la
oportunidad de detectar si algo quedó con un nombre distinto entre lo que dice
el documento de arquitectura y lo que terminó en el código — y decidir cuál de
los 2 se corrige.

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

### 6 — Trazabilidad RF/RNF final

`FINAL_ARCHITECTURE.md` acumuló, a lo largo de toda su revisión, citas RF-/RNF-
verificadas contra `Requerimientos.pdf` (`grep -oE "RF-[A-Z]+-[0-9]+"` /
`"RNF-[A-Z]+-[0-9]+"` sobre el documento, ya usado varias veces durante esa
revisión). Esta fase es la última oportunidad de confirmar que esas citas
siguen siendo ciertas **contra el código real migrado**, no solo contra el
documento de arquitectura:

- RF-AUD-01/02 → confirmar que `api/audit.py` (sección 1.6, recién migrado)
  realmente registra y permite consultar por los campos que el SRS pide.
- RNF-API-05 (idempotencia) → confirmar que `VLAN.aplicar()`/`Puerto.aplicar()`
  (Fase 2) y el guard de `Job.esta_en_estado_terminal()` (Fase 4/5) siguen
  comportándose así en el código final, no solo en el pseudocódigo del plan.
- RNF-ESCAL-01 → confirmar que `RedisCoordinator` (Fase 1) quedó realmente
  inyectado en los lugares que antes llamaban `device_locks`/`rate_limiter`
  directo (4 lugares reales documentados en Fase 1, más los de Fase 5).
- El resto de las citas de `FINAL_ARCHITECTURE.md` §6 — recorrerlas todas, no
  solo estas 3, es la única forma de que esta verificación valga lo que costó
  construir esas citas en primer lugar.

## Criterio de finalización

- [ ] `role_assignment_service.py`: las 4 llamadas a `audit_service.log_action()`
      migradas a `audit_repository.append()` (sección 1.5), **antes** de borrar
      `audit_service.py`. El re-grant del mismo rol audita con `"noop": True`.
- [ ] `main.py`/`api/users.py`/`api/auth.py`: las 11 llamadas a
      `audit_service.log_action()` migradas a `audit_repository.append()`
      (sección 1.6) — **antes** de borrar `audit_service.py`.
- [ ] `api/audit.py` reescrito contra `AuditRepository.query()`/`.purge_old()`
      (sección 1.6) — con `device_id`/`site_id`/paginación mixta preservados.
- [ ] `python -c "import app.composition"` y `python -c "import app.main"`
      corren sin error (sección 5).
- [ ] Trazabilidad RF/RNF de `FINAL_ARCHITECTURE.md` §6 confirmada contra el
      código final (sección 6) — no solo contra el documento.
- [ ] Los 15 archivos de la tabla de la sección 1 confirmados sin caller real y
      borrados.
- [ ] `PortInfo`/`PortConfigRequest` borradas de `models/port.py` (no son
      archivo aparte, ver nota bajo la tabla de la sección 1).
- [ ] `app/composition.py` completo, contra la lista del `README.md`.
- [ ] Catálogo de `FINAL_ARCHITECTURE.md` §1 verificado contra el código real,
      diferencias de nombre/ubicación resueltas (documento actualizado o código
      renombrado, lo que corresponda).

## Riesgos / cosas a validar

- Esta fase es mecánica (grep, confirmar, borrar) — bajo riesgo si las Fases 1-6
  se ejecutaron tal como quedaron documentadas. El único riesgo real es dar por
  buena la tabla de la sección 1 sin correr el grep — no asumir, confirmar.
