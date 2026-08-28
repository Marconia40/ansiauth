# Plan: del código actual al modelo de dominio (`diagrama_clases_dominio.drawio`)

## Contexto

El diagrama de clases (`diagrama_clases_dominio.drawio`) define un modelo de dominio
orientado a objetos — `Device` rico con drivers compuestos, un `Orquestador` genérico,
y dos dominios funcionales nuevos (Interfaces Virtuales, Configuración Global) exigidos
por `Requerimientos.pdf` pero no implementados hoy. Este documento traza, clase por
clase, qué del código real de `ansiauth` **sirve tal cual**, qué **hay que adaptar**, y
qué **hay que construir de cero** — y en qué orden conviene hacerlo para no romper lo
que ya funciona.

> **Actualización — rama base**: el resto del documento estaba escrito contra
> `refactor/observability`. `main` ahora está **8 commits por delante** de esa rama
> (la absorbió entera vía merge, más un cambio nuevo) — pasa a ser la referencia real.
> El cambio nuevo es sustancial: `d712868 "Replace BackgroundTasks with Celery + Redis.
> move locks and rate limit to Redis"`. Nada de esto rompe el diseño del diagrama — al
> contrario, valida una de sus premisas (ver sección 2) — pero cambia varios detalles
> de implementación de las fases 0/1. `refactor/observability` queda superada, no se
> usa más como referencia.

No es un plan de una sola tanda: son 5 fases, cada una verificable de forma
independiente antes de pasar a la siguiente.

> **Actualización — MSP y objetivo OOP completo (2026-08-23)**: la sección 1 y la
> sección 7 de este documento describen `Usuario`/`authz.py` como estaban antes de
> `refactor/MSP-phase-0-and-1`..`refactor/MSP-phase-6`. MSP **eliminó `core/authz.py`
> por completo** y lo reemplazó por `role_assignments`/`effective_role()` — la
> sección 7 de este documento (permisos multi-site vía `device_sites` +
> `user_allowed_devices`) queda **superseded**, MSP resuelve el mismo problema con
> un modelo distinto (ver `docs/MSP_IMPLEMENTATION_PLAN.md` D0 y
> `docs/DEVICE_IMPLEMENTATION_PLAN.md` §1 para el detalle de la reconciliación). Se
> mantiene sin reescribir el texto histórico de la sección 7 — no la vuelvas a
> implementar. Además, el objetivo declarado ahora es OOP completo en todo
> `services/` (no solo las 25 clases del diagrama original) — ver la sección 11,
> nueva, que reemplaza el criterio "estético" de la sección 9 para `Orquestador`/
> `AuditService` y agrega el resto de `services/*.py` que el diagrama original no
> cubría.

---

## 1. Mapa completo — clase del diagrama → código real → estado

| Clase | Código actual equivalente | Estado |
|---|---|---|
| `Inventory` | `services/device_service.py`, `services/device_group_service.py`, `services/site_service.py` (funciones sueltas) | 🟡 Adaptar |
| `Device` | `models/device.py: Device` (dataclass sin métodos) + 5 funciones en `services/vlan_service.py` + equivalentes en `services/port_service.py` | 🔴 Reescribir |
| `DeviceGroup` | `services/device_group_service.py` + `db/models.py: DeviceGroupModel` | 🟡 Adaptar |
| `Site` | `services/site_service.py` + `db/models.py: SiteModel` | 🟢 Sirve tal cual |
| `Usuario` | dict `{"username","role"}` (payload JWT) + `core/authz.py` | 🟡 Adaptar |
| `VLAN` | `models/vlan.py: VLAN` (renombrada de `VLANInfo`, 2026-08-23) | 🟢 Hecha — ya no es solo DTO, valida `vlan_id` en `__post_init__` + `validate_name()` explícito (ver `DEVICE_IMPLEMENTATION_PLAN.md` D7) |
| `Puerto` | `models/port.py: PortInfo` | 🟡 Adaptar (+2 campos) |
| `InterfazVirtual` | — no existe | 🔴 Construir |
| `ConfiguracionGlobal` | — no existe | 🔴 Construir |
| `BaseVlanDriver` | `services/vendors/base.py: BaseVendorDriver` | 🟢 Sirve tal cual |
| `BasePuertoDriver` | `services/vendors/port_driver_base.py: BasePortDriver` | 🟡 Adaptar (+2 métodos) |
| `BaseInterfazDriver` | — no existe | 🔴 Construir |
| `BaseConfigDriver` | — no existe | 🔴 Construir |
| `Huawei/CiscoVlanDriver` | `services/vendors/{huawei,cisco}/vlan_driver.py` | 🟢 Sirven tal cual |
| `Huawei/CiscoPuertoDriver` | `services/vendors/{huawei,cisco}/port_driver.py` | 🟡 Adaptar (+2 métodos c/u) |
| `Huawei/CiscoInterfazDriver` | — no existen | 🔴 Construir (+ playbooks) |
| `Huawei/CiscoConfigDriver` | — no existen | 🔴 Construir (+ playbooks) |
| `Orquestador` | `services/orchestration_runner.py: run_operation` | 🟢 Sirve casi tal cual |
| `Job` | `models/job.py: Job` + `db/models.py: JobModel` | 🟢 Sirve tal cual |
| `GroupJob` | `models/group_job.py: GroupJob`, `DeviceExecution` + `db/models.py: GroupJobModel` | 🟢 Sirve tal cual |
| `AuditService` | `services/audit_service.py` | 🟢 Sirve tal cual |

**Resumen**: de 25 clases, **9 sirven tal cual**, **7 necesitan adaptación acotada**,
**9 hay que construirlas de cero** (2 dataclasses de dominio + 6 clases de driver +
todos los schemas/endpoints que las acompañan — ya no playbooks, ver sección 5).

**Nota**: este documento agrega una **clase 26** que todavía no está en
`diagrama_clases_dominio.drawio` — `PlantillaComando` (sección 6) — y modifica una
relación existente (`Site↔Device`, sección 7). Ver el punto 29 del plan de fases.

---

## 2. Lo que sirve tal cual (bajo riesgo, no tocar la lógica interna)

- **`orchestration_runner.run_operation`** — ya es agnóstico de dominio (no sabe qué es
  una VLAN ni un puerto). Es literalmente lo que permite que `Device.crear_vlan()` y
  `Device.configurar_puerto()` deleguen en el mismo motor sin duplicar retry/lock/rollback.
  No hace falta reescribirlo, solo llamarlo desde `Device` en vez de desde
  `vlan_execution_service.run_create_job`/`port_execution_service.*`.
- **`VLANInfo`, `Job`, `GroupJob`, `DeviceExecution`** — dataclasses ya normalizadas,
  exactamente lo que el diagrama pide para `VLAN`/`Job`/`GroupJob`.
- **`BaseVendorDriver` + `HuaweiVlanDriver` + `CiscoVlanDriver`** — la jerarquía completa
  ya funciona (playbooks incluidos) y no necesita ningún cambio.
- **`site_service.py`** — ya modela exactamente lo que `Site` necesita.
- **`audit_service.py`** — todas las funciones que `AuditService` necesita ya existen
  (`log_action`, `append_audit_event`, consulta con filtros).
- **`device_locks.py` / `rate_limiter.py`** — en `main` pasaron a ser Redis-backed
  (lock/rate-limit distribuido, con fallback automático a `threading.Lock`/memoria si
  Redis no responde), **sin cambiar la firma pública** (`acquire()`, `get_device_lock()`,
  `wait_for_slot()`). Esto es una buena noticia para el diagrama, no solo neutral: la
  limitación de escalado horizontal que teníamos anotada como "no resuelta" en
  `arquitectura-modelo-dominio.md` §2 ya no aplica en `main` — y como `Orquestador`
  (`run_operation`) llama a estas funciones por su interfaz pública, no necesita ni un
  cambio de línea para heredar el beneficio.
- **`backend/app/worker.py`** — entrypoint nuevo de Celery (`celery_app`), separado del
  proceso FastAPI. No es una clase del diagrama, es infraestructura — pero cualquier
  método de `Device` que hoy encolaría trabajo (`crear_vlan`, `configurar_puerto`, etc.)
  va a despachar a través de acá, no de `BackgroundTasks`.

---

## 3. Lo que hay que adaptar (riesgo medio, cambia estructura pero no reglas de negocio)

### `Device` — el cambio más grande
Hoy **no existe** una clase `Device` con comportamiento. La lógica de VLAN/Puerto vive
repartida en `vlan_service.py` (5 funciones casi idénticas: `create_vlan_on_device`,
`delete_vlan`, `update_vlan_description`, `get_vlans`, `save_config_on_device`) y su
equivalente en `port_service.py`, cada una repitiendo el mismo baile
`_resolve_device → decrypt_password → _get_driver → driver.metodo(...)`.

Migrar significa: mover esas funciones a ser métodos de `Device`, resolviendo **los
drivers una sola vez** (en `Inventory.get_device()`, al construir el objeto) en vez de
en cada llamada individual.

**Detalle que cambió con Celery** (ver nota de la sección "Contexto"): en
`refactor/observability`, `enqueue_create_jobs`/`enqueue_delete_jobs`/etc. recibían un
`background_tasks: BackgroundTasks` de FastAPI y hacían `background_tasks.add_task(...)`.
En `main` eso ya no existe — encolan directo con `_group_create_task.delay(...)` sobre
un `@celery_app.task` definido en el mismo módulo. Para el diseño de `Device`, esto es
una simplificación: sus métodos (`crear_vlan`, `configurar_puerto`, etc.) **no
necesitan recibir ni pasar `background_tasks` a ningún lado** — el despacho a Celery
queda encapsulado adentro de la función que hoy ya se llama `enqueue_*`, sin que el
método de `Device` tenga que saber que existe Celery.

### `Inventory` / `DeviceGroup`
`device_service.py` y `device_group_service.py` ya tienen toda la lógica (queries
SQLAlchemy, validaciones) — envolverlos en una clase es mecánico, no cambia una sola
regla de negocio. `DeviceGroup` necesita además los 2 métodos nuevos que hoy no existen
en ningún lado (`crear_vlan_en_grupo`, `configurar_puerto_en_grupo` — orquestar sobre
la lista de devices del grupo).

### `Usuario`
Hoy es un dict plano que sale de `get_current_user` (`core/dependencies.py`). Pasar a
una dataclass real con `puede_acceder(device)` es un wrapper liviano sobre
`authz.ensure_device_allowed` — la lógica de RBAC/site-scoping no cambia.

### `BasePuertoDriver` + `Huawei/CiscoPuertoDriver`
Agregar 2 métodos abstractos nuevos (`set_poe`, `set_storm_control`, cubren
`RF-PUERTO-09` y `RF-PUERTO-07`), implementarlos en las 2 concretas, y escribir
2 playbooks nuevos por vendor (4 en total). El resto de la interfaz no cambia.

### `Puerto` (`PortInfo`)
Agregar `poe_habilitado: bool` y `storm_control: str` al dataclass, y a los parsers
(`services/parsers/port_parser.py`, `cisco_port_parser.py`) que leen la salida real
del device.

---

## 4. Lo que falta construir de cero (mismo patrón, aplicado por 3ra y 4ta vez)

### Interfaces Virtuales — `RF-INTERV-01` a `09`
Nada existe hoy: ni `InterfazVirtual`, ni `BaseInterfazDriver`, ni los 2 drivers
concretos, ni playbooks, ni schema Pydantic, ni router. Es exactamente el mismo molde
ya usado para VLAN y Puerto — no hay nada conceptualmente nuevo que resolver, es
volumen de trabajo repetitivo.

### Configuración Global — `RF-GLOBAL-01` a `12`
Mismo caso, con un dataclass más grande (compone `SnmpConfig`, `RutaEstatica`, listas
de NTP/DNS) y más operaciones (`set_hostname`, `configurar_snmp`,
`agregar_ruta_estatica`, `respaldar_config`, `restaurar_config`, `validar_config`).

### Volumen de playbooks nuevos
- Interfaces Virtuales: ~9 playbooks (crear, eliminar, IP, ACL, DHCP-relay, listar × 2 vendors)
- Configuración Global: ~12 playbooks (SNMP, rutas, hostname, NTP/DNS, backup, restore × 2 vendors)

Cada uno individual es chico (10-15 líneas YAML, mismo patrón que
`ansible/project/vendors/huawei/create_vlan.yml`) — el esfuerzo está en la cantidad,
no en la dificultad de cada uno.

### Endpoints REST nuevos
`/api/v1/interfaces-virtuales/*` y `/api/v1/config/*`, siguiendo el molde exacto de
`api/vlans.py`/`api/ports.py` (RBAC, validación, `authz.ensure_devices_allowed`,
delegación al `Orquestador`).

### Testing de lo nuevo
Todo el código nuevo se testea con **inyección de dependencias por constructor**
(`Device(..., interfaz_driver=FakeInterfazDriver())`), no con el patrón viejo de
`monkeypatch.setattr("app.api.ports._capture_pre_state_...")`. No hace falta migrar
los tests viejos para que esto funcione — conviven las dos formas hasta que se
migre cada endpoint.

---

## 5. Playbooks genéricos por marca, no por acción

Decisión nueva (reemplaza el punto "~9/~12 playbooks nuevos" de la sección 4): en vez
de un archivo `.yml` por acción×vendor, **dos playbooks genéricos**, parametrizados
por los comandos a ejecutar. Revisando los playbooks reales existen dos familias, no
una sola:

- **Huawei** ya es 100% secuencia de comandos crudos
  (`ansible.netcommon.cli_command` con un bloque `command:` multilínea).
- **Cisco** usa `cisco.ios.ios_config` con `parents:`/`lines:` — **declarativo**, no
  una lista de comandos. Este módulo chequea la config actual del device y solo aplica
  lo que falta (idempotencia real, gratis). Pasar a comandos crudos para Cisco
  perdería ese chequeo — por eso van **dos** playbooks genéricos, no uno.

```yaml
# backend/ansible/project/generic/run_commands.yml — reemplaza los playbooks estilo Huawei
- name: Ejecutar secuencia de comandos
  hosts: all
  gather_facts: no
  tasks:
    - name: Comandos
      ansible.netcommon.cli_command:
        command: "{{ comandos | join('\n') }}"
```

```yaml
# backend/ansible/project/generic/configure_lines.yml — reemplaza los playbooks estilo Cisco
- name: Aplicar configuración declarativa
  hosts: all
  gather_facts: no
  tasks:
    - name: Config
      cisco.ios.ios_config:
        parents: "{{ parents | default(omit) }}"
        lines: "{{ lines }}"
    - name: Guardar
      cisco.ios.ios_config:
        save_when: always
      when: guardar | default(false)
```

Cada driver concreto (`HuaweiVlanDriver`, `CiscoPuertoDriver`, etc.) deja de apuntar a
un archivo `_PLAYBOOK_CREATE = "vendors/huawei/create_vlan.yml"` propio — en su lugar,
arma la lista de comandos (o `parents`/`lines`) y llama siempre al mismo playbook
genérico de su familia. **Impacto directo en la sección 4**: los ~21 playbooks
proyectados para Interfaces Virtuales + Configuración Global pasan a ser **0 archivos
YAML nuevos** — solo lógica Python nueva en los drivers.

---

## 6. Comandos de configuración en base de datos

Pedido nuevo: los comandos (hoy, si se implementara el punto 5 tal cual, vivirían
hardcodeados en cada método de driver) se guardan en una tabla, no en código Python.
Esto agrega **una clase nueva que todavía no está en `diagrama_clases_dominio.drawio`**
— hay que sumarla ahí como follow-up:

```
PlantillaComando  «nuevo»
  + id: int (PK)
  + vendor: string          — huawei | cisco
  + accion: string          — create_vlan, set_poe, configurar_snmp, ...
  + tipo: string            — raw_commands | declarative
  + comandos: json          — lista de strings con placeholders Jinja (raw_commands)
  + parents: string         — solo si tipo=declarative
  + lines: json             — solo si tipo=declarative
  + version: int
  + activo: bool
  + get_para(vendor, accion): PlantillaComando   GET
  + renderizar(variables): dict                   (interno)
```

Tabla nueva `command_templates` (migración Alembic), y un servicio
`command_template_service.py` con `get_commands(vendor, accion) -> PlantillaComando`,
que cada driver llama en vez de tener la lista hardcodeada:

```python
class HuaweiVlanDriver(BaseVlanDriver):
    def create_vlan(self, vlan_id, nombre, device, password):
        plantilla = command_template_service.get_commands("huawei", "create_vlan")
        comandos = plantilla.renderizar(vlan_id=vlan_id, vlan_name=nombre)
        return ansible_service.run_playbook(
            playbook="generic/run_commands.yml",
            extravars={"comandos": comandos}, ...)
```

**Por qué vale la pena**: los comandos quedan editables sin deploy (útil si un
firmware nuevo cambia la sintaxis de un comando puntual), y se pueden auditar/versionar
como cualquier otra entidad ABMC del sistema (mismo patrón que `Device`/`Usuario`).

**Riesgo a vigilar**: cada operación ahora hace una consulta a DB extra antes de tocar
el device — vale la pena cachear en memoria (read-through, invalidado por `version`)
para no agregar latencia por cada VLAN/puerto que se configura. No bloquea la
implementación inicial, es una optimización de la Fase 5.

---

## 7. Modelo de permisos: un device en múltiples Sites

Cambio de modelo de datos, más allá de lo que capturaba `authz.py` hasta ahora.

**Hoy**: `DeviceModel.site_id` es una FK **simple** nullable — un device pertenece a
**un solo** site (o ninguno). `authz.allowed_device_names_for(user)` filtra por
`site_id IN allowed_sites`.

**Pedido nuevo**: un device puede pertenecer a **varios** sites (y ya pertenece a
varios device groups — eso ya es many-to-many hoy, vía `DeviceGroupMemberModel`, no
cambia). Los permisos se setean tanto a nivel site como a nivel device individual.

**Política de conflicto, decidida**: si un device está en el Site A (usuario con
acceso) y el Site B (usuario sin acceso), el usuario **puede** operarlo — alcanza con
acceso a **cualquiera** de los sites del device (unión, no intersección).

### Cambios de esquema

```
devices                          — se elimina la columna site_id
device_sites          (nuevo)    — device_name FK, site_id FK   (many-to-many)
user_allowed_devices   (nuevo)    — user_id FK, device_name FK   (grant directo, sin pasar por site)
```
Mismo patrón que la tabla `user_allowed_sites` que ya existe — no es un concepto nuevo
en el proyecto, es la misma idea aplicada un nivel más abajo.

### Cambio en `authz.py`

```python
def allowed_device_names_for(user) -> set[str] | None:
    if is_unrestricted(user):
        return None
    site_ids = allowed_site_ids_for(user)
    via_sites = devices_in_any_site(site_ids)          # antes: site_id IN (...)
    via_directo = devices_allowed_directly(user.id)     # nuevo: user_allowed_devices
    sin_site = devices_sin_ningun_site()                # fallback migration-safe, igual que hoy
    return via_sites | via_directo | (sin_site if not site_ids else set())
```
La unión (`|`) es la que implementa "más permisivo gana". El fallback de devices sin
ningún site asignado se mantiene igual que hoy (comportamiento migration-safe ya
documentado en `authz.py`).

### Impacto en el diagrama de clases
`Site "1" --> "0..*" Device` pasa a ser `Site "0..*" --> "0..*" Device`. `Usuario` suma
una relación nueva `Usuario ···› Device` ("acceso directo") además de la existente vía
`Site`. Ambos cambios quedan pendientes de reflejar en
`diagrama_clases_dominio.drawio` — no los apliqué todavía, solo quedaron documentados
acá.

---

## 8. Plan de migración por fases

### Fase 0 — Fundamentos (bajo riesgo, no cambia comportamiento observable)
1. `Usuario` e `Inventory` como clases finas envolviendo `device_service.py` /
   `device_group_service.py` / `site_service.py` tal cual están.
2. `Orquestador` como clase fina envolviendo `orchestration_runner.run_operation`
   (o dejarlo función — no bloquea nada, es preferencia de estilo).
3. Verificación: `pytest backend/tests/` completo sigue en verde — nada de esta fase
   toca lógica de negocio, solo agrega wrappers.

### Fase 0.5 — Modelo de permisos multi-site (sección 7)
4. Migración Alembic: nuevas tablas `device_sites`, `user_allowed_devices`; drop de
   `devices.site_id`; backfill de `device_sites` con el `site_id` que cada device
   tenía antes (para no perder asignaciones existentes).
5. Reescribir `authz.allowed_device_names_for` con la lógica de unión (site + directo).
6. Tests nuevos: device en 2 sites con permisos distintos por usuario, confirmando la
   política "más permisivo gana"; caso de grant directo sin ningún site en común.
7. Esta fase se hace **antes** que la Fase 1 porque toca `authz.py`, que usan
   *todos* los endpoints — mejor aislarla temprano que mezclarla con el rewrite de
   `Device`.

### Fase 1 — `Device` rico sobre lo que ya existe (VLAN + Puerto)
8. Métodos VLAN de `Device` (`crear_vlan`, `eliminar_vlan`, `editar_vlan`,
   `listar_vlans`), cableados directo a `orchestration_runner.run_operation`, usando
   **los playbooks actuales tal cual** (todavía sin generic playbooks — un cambio por
   vez). El despacho a background sigue el patrón que ya está en `main`
   (`_group_create_task.delay(...)` vía `app.worker.celery_app`) — no hay que decidir
   nada nuevo acá, solo llamarlo desde el método de `Device` en vez de desde el router.
9. Mismo para los métodos de Puerto.
10. Tests nuevos para estas rutas con inyección de driver — deben cubrir los mismos
    casos que `test_vlan_semantics.py`/`test_rollback_hardened.py`/`test_port_*.py`.
    Mismo truco que ya usa `test_celery_dispatch.py`: con
    `CELERY_TASK_ALWAYS_EAGER=True` (seteado en `conftest.py`), `.delay()` corre
    sincrónico en el test — no hace falta un worker ni Redis reales para testear
    `Device`.
11. Recién con los tests nuevos en verde: deprecar `vlan_service.py`/`port_service.py`
    (las funciones sueltas quedan reemplazadas por métodos de `Device`).

### Fase 1.5 — Playbooks genéricos + comandos en DB (secciones 5 y 6)
12. Migración Alembic: tabla `command_templates`. Cargar ahí los comandos que hoy
    están hardcodeados en los 23 playbooks existentes de VLAN/Puerto.
13. Playbooks genéricos `generic/run_commands.yml` (Huawei) y
    `generic/configure_lines.yml` (Cisco).
14. `command_template_service.py` con caché en memoria invalidada por `version`.
15. Migrar `HuaweiVlanDriver`/`CiscoVlanDriver`/`Huawei-CiscoPuertoDriver` (4 clases)
    para que usen `command_template_service` + playbooks genéricos en vez de sus
    `_PLAYBOOK_*` propios.
16. Borrar los 23 playbooks viejos de `vendors/huawei/`/`vendors/cisco/` recién
    cuando los tests de la fase 1 sigan en verde contra el driver migrado.

### Fase 2 — Puerto extendido (PoE, Storm-Control)
17. 2 métodos nuevos en `BasePuertoDriver` + 2 concretas — **sin playbooks nuevos**,
    solo 2 filas nuevas en `command_templates` (gracias a la Fase 1.5).
18. Campos nuevos en `Puerto` + endpoint REST correspondiente.

### Fase 3 — Interfaces Virtuales (nuevo)
19. `InterfazVirtual` (dataclass) + `BaseInterfazDriver` (ABC) + 2 drivers concretos —
    usan los playbooks genéricos desde el día uno, **cero playbooks nuevos**.
20. ~9 filas nuevas en `command_templates` (antes: ~9 playbooks).
21. Schema Pydantic + router `api/interfaces_virtuales.py`.
22. Métodos correspondientes en `Device`.

### Fase 4 — Configuración Global (nuevo)
23. `ConfiguracionGlobal` (+ sub-tipos `SnmpConfig`, `RutaEstatica`) + `BaseConfigDriver`
    + 2 drivers concretos — mismo criterio, cero playbooks nuevos.
24. ~12 filas nuevas en `command_templates` (antes: ~12 playbooks).
25. Schema Pydantic + router `api/config.py`.
26. Métodos correspondientes en `Device`, incluyendo `respaldar_config`/`restaurar_config`
    (`RF-GLOBAL-10`/`11`) y `validar_config` como precheck (`RF-GLOBAL-12`).

### Fase 5 — Cierre
27. `DeviceGroup.crear_vlan_en_grupo` / `configurar_puerto_en_grupo`.
28. Optimización de caché de `command_template_service` si el smoke test muestra
    latencia agregada perceptible.
29. Actualizar `diagrama_clases_dominio.drawio`: `Site↔Device` many-to-many,
    `Usuario ···› Device` directo, y sumar `PlantillaComando`.
30. Auditoría final de cobertura: recorrer `Requerimientos.pdf` RF por RF y confirmar
    que cada uno mapea a un método concreto de alguna clase.

---

## 9. Decisiones abiertas a confirmar antes de arrancar

- **¿Se reescriben los tests viejos (monkeypatch) de una?** Recomendación: no —
  migrarlos en el mismo commit que migra el endpoint correspondiente (fase 1, paso 10),
  no como un esfuerzo separado.
- **¿`InterfazVirtual`/`ConfiguracionGlobal` se persisten en DB?** Recomendación: no —
  igual que `VLAN`/`Puerto` hoy, se consultan en vivo contra el device. (Distinto de
  los *comandos* para generarlas, que sí se persisten — sección 6.)
- **¿`Orquestador` y `AuditService` quedan como clases o siguen siendo módulos?**
  **RESUELTO (2026-08-23): pasan a ser clases.** Dejó de ser una decisión estética —
  ver la política general en la sección 11, nueva.
- **Resuelto**: conflicto de permisos multi-site → más permisivo gana (sección 7).
- **`user_allowed_devices` como grant directo, ¿lo administra quién?** Falta definir
  si el ABMC de este grant va colgado del endpoint de `Usuario` existente o es un
  endpoint nuevo — no bloquea el diseño de datos, sí falta antes de la Fase 0.5.

## 10. Verificación por fase

Cada fase se da por cerrada solo cuando:
1. `pytest backend/tests/` completo en verde.
2. Un smoke test manual en `EXECUTION_MODE=mock` de la operación nueva/migrada,
   contra `GET /docs` (Swagger) para confirmar que el endpoint quedó bien documentado.
3. Para fases 3 y 4: el RF correspondiente de `Requerimientos.pdf` queda marcado como
   cubierto en la auditoría de la fase 5.
4. Para la fase 0.5 en particular: un test que confirme que un usuario con acceso a
   Site A pero no a Site B **sí** puede operar un device que está en ambos.

---

## 11. Política general: qué pasa a clase y qué no (2026-08-23)

Objetivo declarado por el usuario: *"pasar completamente a OOP ... quiero que cada
clase tenga sus métodos y objetos con responsabilidades claras y dejar de estar
llamando a funciones en cualquier lado."* Esto es más amplio que las 25 clases del
diagrama original (sección 1) — cubre **todo** `backend/app/services/*.py`, la
mayoría de lo cual son módulos de funciones sueltas que el diagrama nunca contempló
porque no son "clases del dominio de red" (`device_locks.py`, `rate_limiter.py`,
`retry_policy.py`, `ansible_service.py`, `secret_service.py`,
`vendors/dispatcher.py`, `login_attempt_service.py`, `refresh_token_service.py`,
`auth_service.py`, `job_service.py`, `group_job_service.py`, `effective_role.py`).

### Criterio: no todo módulo se convierte 1-a-1 en una clase

La ventaja real de OOP acá no es "cada `services/*.py` pasa a ser una clase" — es que
el código quede organizado por **sustantivo de dominio** (`Device`, `Job`,
`Orquestador`) en vez de por **verbo/feature** disperso en varios archivos
(`vlan_service.py` + `vlan_execution_service.py` para lo mismo). Señales de que algo
**sí** debería ser clase:

- Necesita cachear/recordar algo entre llamadas (estado) — ej. `Device` cacheando su
  driver resuelto, algo que una función no puede hacer sola.
- 3+ funciones hoy dispersas en 2-3 archivos distintos operan sobre el mismo
  concepto/dato — señal de que están mal repartidas, no de que falte una clase por
  archivo.

Señal de que **no** hace falta clase: una función pura (mismos inputs → mismo output,
sin efectos secundarios, sin estado que cachear) que no le "pertenece" a ningún
objeto concreto del dominio. `effective_role(session, user, resource_type,
resource_id)` (MSP) es el ejemplo — envolverla en una `AuthorizationService` solo
para tener una clase sería OOP cosmético, el mismo error ya identificado y corregido
en `DEVICE_IMPLEMENTATION_PLAN.md` D2 al intentar justificar un "Repository" sin
precedente real en el código.

### Mapa completo — `services/*.py` restante → clase destino

| Módulo(s) actual(es) | Clase destino | Motivo |
|---|---|---|
| `vendors/dispatcher.py` | `VendorDriverFactory` **(nueva)** | Reemplaza `get_driver`/`get_port_driver`; hoy `Device` los llama como funciones sueltas — gap real detectado al revisar `Device` (ver `DEVICE_IMPLEMENTATION_PLAN.md` D5) |
| `secret_service.py` | `SecretVault` **(nueva)** | Reemplaza `decrypt_password`; mismo gap, ver D6 |
| `ansible_service.py` | `AnsibleRunner` **(nueva)** | Encapsula config (timeout, paths) que hoy se relee de `core.config` en cada llamada — instancia con estado real, no cosmético |
| `audit_service.py` | `AuditService` **(nueva)** | Ya estaba en el diagrama original; deja de ser opcional (sección 9) |
| `orchestration_runner.py` | `Orquestador` **(nueva)** | Ídem — deja de ser opcional |
| `device_locks.py` + `rate_limiter.py` + `retry_policy.py` | Colaboradores internos de `Orquestador` (composición) | Solo el orquestador los usa hoy — no necesitan ser clases públicas de primer nivel, alcanza con que `Orquestador` los tenga como atributos propios |
| `job_service.py` + `group_job_service.py` | Métodos de `Job` / `GroupJob` (ya son dataclasses, sección 1) | No es "un servicio de Jobs" — es lo que un Job hace consigo mismo (`job.mark_failed()`, `job.retry()`) |
| `login_attempt_service.py` + `refresh_token_service.py` + `auth_service.py` | Candidatos a fusionar en una sola `AuthenticationService`, o repartirse entre `Usuario`/una futura `Session` — **a confirmar antes de tocarlos** | Los 3 giran sobre el estado de autenticación de un usuario; fusionarlos evita 3 clases finas que se llaman entre sí sin necesidad |
| `device_service.py` | `Inventory` (ya existe, MSP Fase 3) | No es clase nueva — es mover métodos a la clase que ya reclama esa responsabilidad (`DEVICE_IMPLEMENTATION_PLAN.md` §9.3) |
| `vlan_service.py` (+ `port_service.py`, pendiente su turno) | `Device` | **Hecha para VLAN (2026-08-23)** — pero no como se planeaba: `vlan_service.py` mismo pasó a ser 5 delegados de una línea a `Device`, no se tocó ningún caller. Ver `DEVICE_IMPLEMENTATION_PLAN.md` Phase 2 / D3 revisada |
| `vlan_execution_service.py` (+ `port_execution_service.py`/`port_config_service.py`, equivalente) | `VlanJobRunner` **(nueva, corrige clasificación previa)** | **No es `Device` ni `VLAN`** — es orquestación de job (rollback, retry, pre-state, notificar GroupJob), una capa distinta a propósito (`DEVICE_IMPLEMENTATION_PLAN.md` §7: "No orchestration inside Device"). Mismo criterio que `Orquestador`: hoy importa 5 colaboradores como módulos sueltos; inyectarlos por constructor es la ganancia real. Usa `Orquestador` (motor genérico) internamente, no lo reemplaza |
| `parsers/*.py` | Métodos privados de cada driver concreto (`HuaweiVlanDriver._parse(...)`, etc.) | No necesitan ser clases de primer nivel — son detalle de implementación de cada driver |
| `effective_role.py` | **Se queda función** — no se convierte | Computación pura sin estado (decisión deliberada de MSP); forzarla a método no encapsula nada nuevo |

**Total real**: 9 clases nuevas (`VendorDriverFactory`, `SecretVault`, `AnsibleRunner`,
`AuditService`, `Orquestador`, `Site`, `DeviceGroup`, `Usuario`,
`AuthenticationService` a confirmar) + 3 que ya existen (`Device`, `Inventory`,
`RoleAssignmentService`) + 1 función que se queda función a propósito
(`effective_role`). Bastante menos que "un archivo de `services/`, una clase".

### Fuerza real de justificación por clase, con contrafáctico "si no se convierte" (revisado 2026-08-23)

Al revisar el código real (no solo el conteo de funciones), la justificación
"tiene estado que cachear" resultó **no ser el criterio que este código ya usa** —
ni `Inventory` ni `RoleAssignmentService` (las 2 clases que ya existen, construidas
por MSP) tienen estado real: ambas tienen `self._db = db` en el constructor, sin
usar, comentado literalmente `# session-per-call; kept for plan compatibility`. El
criterio que de verdad justifica una clase en este código es el otro: **unificar
funciones dispersas en torno a un concepto con identidad propia**, no "tener
estado".

**Ya existen — no hay contrafáctico, ya está hecho:**

| Clase | Reemplazó | Por qué ya se justificó |
|---|---|---|
| `Device` | `models/device.py` + `vlan_service.py`/Port equivalente | Estado real (drivers cacheados) + unifica 2 archivos. Hecha (Phase 1 de `DEVICE_IMPLEMENTATION_PLAN.md`) |
| `Inventory` | `device_service.py` + `device_group_service.py` + `site_service.py` | Unifica 3 archivos de CRUD/queries de device (MSP Fase 3) |
| `RoleAssignmentService` | lógica de `role_assignments` que antes no existía | "Sole owner" de grants/revoke/admin-flag — **confirmado que NO se fusiona con `Inventory`**, ver subsección siguiente |
| `BaseVendorDriver`/`BasePortDriver` + concretas | siempre existieron así | Polimorfismo real |

**Confirmado (2026-08-23): `RoleAssignmentService` no se fusiona con `Inventory`.**
Se evaluó explícitamente y se descartó. Motivos, verificados contra el código:
1. `RoleAssignmentService` tiene 4 métodos (`grant`, `revoke`, `list_for_user`,
   `set_system_admin`) — "¿quién puede hacer qué sobre qué recurso?", un concepto
   ortogonal a "¿qué devices existen y dónde viven?" (`Inventory`).
2. `effective_role()` (lo que `RoleAssignmentService` alimenta) se llama desde **6
   routers distintos** — `jobs.py`, `devices.py`, `device_groups.py`, `vlans.py`,
   `sites.py`, `ports.py`. Acoplar autorización a `Inventory` forzaría a `sites.py`
   (que no tiene nada que ver con devices) a depender de él.
3. `Inventory.list(user, ...)` ya **consume** `role_assignments` para filtrar
   visibilidad — es cliente del dato, no dueño. Que el consumidor absorba al que
   administra el dato invertiría la dependencia sin necesidad.
4. MSP ya lo evaluó bajo el límite más duro de su plan (*"Absolute limit: no other
   new services"* — solo 3 servicios nuevos permitidos en toda la Fase 3) y aun así
   los mantuvo separados. Fusionarlos les habría "ahorrado" un servicio bajo ese
   presupuesto; no lo hicieron.

**Fuerte — conviene hacerlas:**

| Clase | Código actual | Por qué sí | Si NO se convierte (queda como está) |
|---|---|---|---|
| `Site` | `site_service.py` (10 funciones) | Mismo patrón que `Inventory`. Pendiente desde el diagrama original, ni el código actual ni MSP la construyeron | Sigue siendo `site_service.get_site(id)` — sin objeto para pasar entre capas ni lugar para agregar comportamiento sin otra función suelta |
| `DeviceGroup` | `device_group_service.py` (7 funciones) | Ídem. MSP la dejó afuera **a propósito** por límite de alcance, no porque sobrara | Ídem, y bloquea `DeviceGroup.crear_vlan_en_grupo()`/`configurar_puerto_en_grupo()` (plan original, sección 3), que no tiene dónde vivir sin la clase |
| `Usuario`/`User` | `user_service.py` (13 funciones) | Ídem — hoy es un dict plano, ni dataclass | Código sigue leyendo claves de dict (`user["role"]`), sin lugar para métodos como `usuario.puede_acceder(recurso)` |
| `Orquestador` | `orchestration_runner.py` (`run_operation`, 4 funciones) | Importa 4 colaboradores inline hoy — inyectarlos por constructor es ganancia real de testeo | Sigue siendo una función de 200+ líneas con imports inline; testear con otro rate limiter exige `monkeypatch` de módulo entero |
| `Job`/`GroupJob` absorbiendo `job_service.py`/`group_job_service.py` | Ya son dataclasses | Mismo molde que `Device` | `job_service.update_job(job_id, "failed", ...)` sigue siendo la única forma de tocar un Job — no existe `job.marcar_fallido(error)` |
| `VlanJobRunner` (nueva, de `vlan_execution_service.py`) | `orchestration_runner`/`job_service`/`audit_service`/`group_job_service`/`vlan_service` importados como módulos sueltos hoy | Orquesta rollback/retry/pre-state para un job de VLAN — no es `Device` ni `Orquestador`, es el adaptador entre ambos | Sigue funcionando exactamente igual — es la capa que ya está más aislada (no la toca nada del resto del código, ni `api/vlans.py` la importa directo salvo `enqueue_*`) |

**Moderada — vale la pena, sin urgencia:**

| Clase | Código actual | Por qué (matizado) | Si NO se convierte |
|---|---|---|---|
| `SecretVault` | `secret_service.py` (2 funciones) | `_fernet` ya es estado real hoy (global de módulo) — pasar a instancia es ganancia modesta pero real, y cierra el gap directo de `Device` (D6). **Hecha (2026-08-23)** — ver `DEVICE_IMPLEMENTATION_PLAN.md` D6 | `Device._get_password()` sigue llamando una función suelta — funciona igual, solo inconsistente con "todo lo que `Device` llama es un objeto" |
| `AnsibleRunner` | `ansible_service.py` (8 funciones) | Config cacheada una vez al importar (constantes de módulo) — mover a instancia es la misma ganancia modesta que `SecretVault` | Sigue funcionando exactamente igual — el costo es solo en testeo (no inyectable) |
| `AuditService` | `audit_service.py` (10 funciones, un solo archivo) | Sin estado real — la única ganancia es inyectar un fake en `Orquestador` en vez de mockear función por función | Sigue funcionando igual (ya estable) — el costo es solo en testeo de `Orquestador` |

**Débil o incierta:**

| Clase | Código actual | Problema | Si NO se convierte |
|---|---|---|---|
| `VendorDriverFactory` | `vendors/dispatcher.py` (4 funciones, un solo archivo) | Sin estado real — los `frozenset` no cambian en runtime. Solo se justifica como **registro mutable** (`.register_vlan_driver(vendor, driver_cls)`), no como envoltorio fino del `if/elif` actual | Sigue siendo 4 funciones — **funciona exactamente igual**. Único costo: `Device` llama una función suelta en vez de un método (inconsistencia estética, no funcional) |
| `AuthenticationService` | `login_attempt_service.py` (6) + `refresh_token_service.py` (4) + `auth_service.py` (1) | Ambigüedad real: ¿1 responsabilidad o 3? No decidido | Los 3 siguen separados — funciona, pero no hay dónde compartir estado entre "intentos fallidos" y "tokens vigentes" de un mismo usuario si algún día hiciera falta |

**Se queda función a propósito — no son candidatas:**

| Función/módulo | Por qué no conviene convertirla | Consecuencia de dejarla así |
|---|---|---|
| `effective_role(session, user, resource_type, resource_id)` | Computación pura, MSP la diseñó así a propósito. Envolverla sería el mismo error ya corregido con "Repository" | Ninguna — es el caso correcto para quedarse función |
| `parsers/*.py` | No son concepto de dominio propio — detalle de implementación de cada driver | Se integran como métodos privados del driver concreto que los usa |
| `device_locks.py`/`rate_limiter.py`/`retry_policy.py` | No son "servicios" independientes — solo los usa `Orquestador` | Pasan a ser atributos internos de `Orquestador` (composición), no clases públicas sueltas |

### Secuencia sugerida (reordenada — las fuertes primero, no las que aparecieron primero en la conversación)

No hay una fase asignada todavía para este trabajo en la sección 8 (ese plan de
fases es anterior a esta política).

1. **`Site`, `DeviceGroup`, `Usuario`** — la deuda más vieja de las tres (pedidas
   desde el diagrama original, sección 3, nunca construidas), y la de justificación
   más fuerte junto con `Orquestador`/`Job`/`GroupJob`. Mismo molde ya probado con
   `Device`: dataclass rico + métodos, sin tocar `Inventory` ni las tablas.
2. `Orquestador` — compone sus 4 colaboradores por constructor en vez de imports
   inline; sin dependencias nuevas.
3. `Job`/`GroupJob` absorbiendo `job_service.py`/`group_job_service.py`.
3b. `VlanJobRunner` (de `vlan_execution_service.py`) — depende de que `Orquestador`
    (paso 2) exista primero, ya que lo compone. Su equivalente de Port
    (`PortJobRunner`, de `port_execution_service.py`/`port_config_service.py`)
    sigue el mismo molde una vez que este quede probado.
4. `SecretVault` — gap directo de `Device` (`DEVICE_IMPLEMENTATION_PLAN.md` D6),
   justificación moderada pero real, bajo riesgo. **Hecha (2026-08-23)**.
5. `AnsibleRunner` — mismo nivel de justificación que `SecretVault`, sin urgencia
   directa (no es un gap de `Device` hoy).
6. `device_locks`/`rate_limiter`/`retry_policy` como colaboradores de `Orquestador`
   una vez que `Orquestador` exista (paso 2).
7. `VendorDriverFactory` — **replantear el diseño primero** (registro mutable, no
   envoltorio fino) antes de implementar; si no se rediseña, evaluar si vale la
   pena en absoluto.
8. `AuditService` — justificación más débil, sin urgencia.
9. `AuthenticationService` (o el reparto que se confirme) — mayor ambigüedad,
   dejarlo para el final.
