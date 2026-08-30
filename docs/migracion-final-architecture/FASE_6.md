# Fase 6 — `Inventory` + `Site` + `DeviceGroup` (Línea A) · integración con `DeviceRepository`/`RoleAssignmentRepository` (Línea B, liviana)

> Parte de [plan de migración a `FINAL_ARCHITECTURE.md`](README.md). Requiere
> [`FASE_1.md`](FASE_1.md)–[`FASE_5.md`](FASE_5.md) terminadas — en particular
> `DeviceRepository`/`DeviceGroupRepository` (Fase 3).

## Objetivo

**Alcance nuevo, no cubierto por ninguna fase anterior** (encontrado armando la
fase de limpieza original — ver nota en `FASE_7.md`): `Site`/`DeviceGroup` pasan
a ser entidades reales (hoy son solo filas + schemas Pydantic de respuesta), y
`Inventory` (ya existe en el código real, Facade real con comportamiento) se
realinea a los patrones del resto del catálogo — `DomainEvent` con `actor`
correcto (ya arreglado en `FINAL_ARCHITECTURE.md`), `SecretVault` inyectada
(Fase 1), visibilidad vía `DeviceRepository.nombres_visibles(scope)` (Fase 3) en
vez de una copia propia de la misma consulta.

**Corrección real encontrada armando esta fase**: `FINAL_ARCHITECTURE.md` decía
que `DeviceGroup.tiene_devices()` bloquea el borrado si tiene devices —
**falso**, comparado contra el código real. Ya corregido en el documento (ver
el catálogo de `DeviceGroup`, §1) — leer esa corrección antes de escribir
`DeviceGroup` acá.

## Estado previo esperado

- Fases 1-5 completas. `DeviceRepository`/`DeviceGroupRepository` (Fase 3) y
  `RoleAssignmentRepository`/`VisibilityScope` (Fase 2) ya existen.
- Nadie tocó todavía: `app/models/site.py` (no existe), `app/models/device_group.py`
  (no existe), `app/services/inventory_service.py`, `app/api/devices.py`,
  `app/api/sites.py`, `app/api/device_groups.py`.

---

## Línea A

### A1 — `app/models/site.py` (archivo nuevo): `Site`

Leído completo `site_service.py` (277 líneas) para esto. **`Site.tiene_devices()`
sí es real y correcto** (a diferencia de `DeviceGroup`) — `delete_site()` real
bloquea con `SiteHasDevicesError` si `device_count > 0`, confirmado.

```python
@dataclass
class Site:
    id: int
    name: str
    description: str | None = None
    kind: str = "REGULAR"  # "REGULAR" | "BASE_INFRASTRUCTURE" (D14)
    default_group_id: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def renombrar(self, nuevo_nombre: str) -> None:
        self.name = nuevo_nombre

    def actualizar_descripcion(self, desc: str) -> None:
        self.description = desc

    def es_base_infraestructura(self) -> bool:
        return self.kind == "BASE_INFRASTRUCTURE"
```

**`created_at`/`updated_at` agregados — encontrado comparando contra
`frontend/src/types/site.ts: Site` real, no estaban en ninguna versión
anterior de esta fase.** Son columnas reales de `SiteModel`
(`db/models.py:234-244`, `updated_at` con `onupdate` automático) que la
primera versión de esta entidad no cargaba — sin esto, `SiteRead` (schema
real, sin tocar en este plan) no tiene de dónde sacarlos al construir la
respuesta de `GET /sites`.

`tiene_devices()`/`device_count` **no viven en la entidad** — necesitan una
consulta (contar devices vía JOIN con `DeviceGroup`), no son campos que `Site`
cargue en memoria. Viven en `SiteRepository` (A3, abajo):

```python
    def contar_devices(self, site_id: int) -> int:
        with get_session() as session:
            return (
                session.query(DeviceModel)
                .join(DeviceGroupModel, DeviceModel.device_group_id == DeviceGroupModel.id)
                .filter(DeviceGroupModel.site_id == site_id)
                .count()
            )

    def tiene_devices(self, site_id: int) -> bool:
        return self.contar_devices(site_id) > 0
```

`contar_devices()` reemplaza el `tiene_devices()` booleano que había en la
primera versión de A3 (abajo) — mismo query, `tiene_devices()` pasa a ser un
wrapper de 1 línea sobre este. El router de `GET /sites` (A7) llama
`site_repository.contar_devices(site.id)` para completar `SiteRead.device_count`
— el real `_to_read()` (`site_service.py:34-48`) hace exactamente este JOIN
hoy, por cada site listado.

### A2 — `app/models/device_group.py` (archivo nuevo): `DeviceGroup`

Leído completo `device_group_service.py` (228 líneas). Ver la corrección ya
aplicada a `FINAL_ARCHITECTURE.md` — el campo real que bloquea es
`is_default`/D7, no la cantidad de devices.

```python
@dataclass
class DeviceGroup:
    id: int
    name: str
    description: str | None = None
    site_id: int | None = None
    es_default: bool = False

    def renombrar(self, nuevo_nombre: str) -> None:
        if self.es_default:
            raise DefaultGroupImmutableError(
                f"El grupo {self.id} es el Default del site y no se puede renombrar (D7)"
            )
        self.name = nuevo_nombre
```

**`DefaultGroupImmutableError` — resuelto, mismo criterio que `SiteHasDevicesError`
(A1): se mueve a `app/core/exceptions.py`, no queda en `models/device_group.py`.**
Encontrado con el mismo grep por nombre de excepción: `api/device_groups.py` la
importa y la atrapa explícito (`from app.services.device_group_service import
DefaultGroupImmutableError`, `except DefaultGroupImmutableError as exc:`,
líneas 11 y 117 reales) — mismo problema que `SiteHasDevicesError`, mismo fix.
`api/device_groups.py` pasa a `from app.core.exceptions import
DefaultGroupImmutableError`.

**`GroupHasDevicesError` — no se lleva.** Existe como clase en el código real
(`device_group_service.py:20-23`) pero su propio docstring admite que
**nunca se usa** en el camino real de borrado (auto-move, D19, ver la
corrección ya aplicada a `FINAL_ARCHITECTURE.md`) — código muerto hoy. No
tiene sentido darle una casa nueva a una excepción que nadie lanza; se
confirma con grep (`grep -rn "raise GroupHasDevicesError" app/` — debería dar
0 resultados) y no se migra.

### A3 — `app/repositories/site_repository.py` (archivo nuevo): `SiteRepository`

**7ma subclase de `Repository[T]` — justificación distinta a las otras 6, dejarlo
explícito.** Las 6 anteriores existen por una consulta/JOIN que `filter_by()` no
puede expresar. Esta existe por algo distinto: `create_site()` real
(`site_service.py:51-85`) **no es un alta de una sola entidad** — crea el `Site`
**y** su `DeviceGroup` Default **y** actualiza `Site.default_group_id`, las 3
cosas atómicas en una sola transacción (si el `DeviceGroup` no se pudiera crear,
el `Site` tampoco debería quedar creado). `Repository[Site].add()` genérico
(Fase 1) solo sabe hacer upsert de **una** fila — no alcanza. Mismo criterio de
fondo que motivó las otras 6 (`Repository[T]` genérico no cubre esto), aplicado
a una operación transaccional en vez de a una consulta.

```python
class SiteRepository(Repository):
    def __init__(self):
        super().__init__(SiteModel, _to_domain, _to_orm)

    def crear_con_grupo_default(self, name: str, description: str | None = None, *, kind: str = "REGULAR") -> Site:
        """Reemplaza create_site() (crea Site+DeviceGroup Default atómico) Y
        ensure_base_infrastructure() (idempotente -- si ya existe un Site de
        kind='BASE_INFRASTRUCTURE', no crea uno nuevo, solo asegura que tenga
        su Default group). Un solo método para las 2 necesidades reales."""
        with get_session() as session:
            if kind == "BASE_INFRASTRUCTURE":
                existente = session.query(SiteModel).filter_by(kind=kind).first()
                if existente is not None:
                    return self._asegurar_grupo_default(session, existente)
            elif session.query(SiteModel).filter_by(name=name).first():
                raise ValueError(f"Site '{name}' already exists")
            row = SiteModel(name=name, description=description, kind=kind)
            session.add(row)
            session.flush()
            grupo = DeviceGroupModel(
                name="Default", site_id=row.id, is_default=True,
                description=f"Default group for site '{name}'.",
            )
            session.add(grupo)
            session.flush()
            row.default_group_id = grupo.id
            session.flush()
            return _to_domain(row)

    # contar_devices()/tiene_devices() -- ver A1, mismo query, no repetido acá

    def eliminar(self, site_id: int) -> bool:
        """Copia delete_site() (líneas 184-219) tal cual -- el orden de
        operaciones (null default_group_id -> borrar grupos -> borrar site)
        importa por el RESTRICT FK real, no reordenar. Sigue lanzando
        SiteHasDevicesError si tiene devices -- ver nota abajo, cambia de
        dónde se importa, no de comportamiento."""
        if self.tiene_devices(site_id):
            device_count = ...  # mismo count que tiene_devices(), reusar
            raise SiteHasDevicesError(site_id, device_count)
        ...
```

**`SiteHasDevicesError` — encontrado con un tercer pase de grep (por nombre de
excepción, no por import de módulo): `api/sites.py` la importa y la atrapa
explícito** (`from app.services.site_service import ... SiteHasDevicesError`,
`except SiteHasDevicesError as e:`, líneas 15 y 176 reales). Borrar
`site_service.py` sin mover esta clase a algún lado rompe ese `except` — deja
de existir el nombre que importa. **Resuelto, no queda abierto**: se mueve a
`app/core/exceptions.py`, mismo archivo que ya tiene `TransicionInvalidaError`
(Fase 4)/`DeviceExecutionError` (Fase 5) — un solo lugar para excepciones de
dominio en todo el catálogo, en vez de una por servicio viejo. `api/sites.py`
pasa a `from app.core.exceptions import SiteHasDevicesError`.

**`_default_group_name_for_site()`** (real, `site_service.py:88-106` — maneja
la colisión rara de un grupo ya llamado "Default" preexistente) — copiar tal
cual como función privada del módulo, llamada desde `crear_con_grupo_default()`
en vez del literal `"Default"` que puse arriba de forma simplificada — no
perder ese caso real al escribir el código final.

### A4 — `Repository[DeviceGroup]` — instancia genérica, sin subclase nueva

`device_group_service.py: create_group()`/`get_group()`/`rename_group()` son
`filter_by()` simples — `Repository[DeviceGroup]` genérico (Fase 1) alcanza. La
única pieza real que falta es un chequeo de unicidad (`name`+`site_id`,
D6) — agregar `existe(**criterio)` a `Repository[T]` genérico (mencionado como
pendiente desde `FINAL_ARCHITECTURE.md` §2.2.1, nunca se había necesitado hasta
ahora):

```python
    # agregar a app/core/repository.py — Repository[T] genérico
    def existe(self, **criterio) -> bool:
        with get_session() as session:
            return session.query(self._orm_model).filter_by(**criterio).first() is not None
```

**`delete_group()`'s auto-move real (D19) — no vive en `Repository[DeviceGroup]`
ni en `Inventory` (cap de 5 métodos, ver A5).** Agregar a
`DeviceGroupRepository` (Fase 3, archivo ya existente,
`app/repositories/device_group_repository.py`) un método nuevo:

```python
    def eliminar_con_auto_move(self, group_id: int, actor: dict, inventory: "Inventory") -> dict:
        """Copia delete_group() (device_group_service.py:165-227) tal cual:
        bloquea si es_default (D7), si no, mueve cada device miembro al
        Default del site vía inventory.move(enforce_authz=False), después
        borra el grupo. Recibe Inventory inyectado -- no lo importa directo,
        mismo criterio de inyección por constructor que el resto del catálogo."""
        ...
```

### A5 — `app/services/inventory_service.py`: reescribir `Inventory`

**El cap real de 5 métodos se respeta — no se agregan `update`/`crear_grupo`/etc.**
El propio código real (`inventory_service.py:13-14`) lo dice explícito: *"Do NOT
add non-Device operations here — service growth is capped at the five methods
below by the phase-3 acceptance criteria"*. Es una decisión ya tomada, de una
fase MSP anterior, real — no se reabre acá.

```python
class Inventory:
    def __init__(self, devices, sites, device_groups, role_assignments, jobs, auditor, vault):
        self._devices = devices              # DeviceRepository, Fase 3
        self._sites = sites                  # SiteRepository, A3
        self._device_groups = device_groups  # Repository[DeviceGroup], A4
        self._role_assignments = role_assignments  # RoleAssignmentRepository, Fase 2
        self._jobs = jobs                    # JobRepository, Fase 4
        self._auditor = auditor              # EventDispatcher, Fase 3
        self._vault = vault                  # SecretVault, Fase 1

    def get(self, name: str) -> "Device | None":
        return self._devices.get(name)

    def list(self, scope: "VisibilityScope", *, site_id=None, device_group_id=None) -> list["Device"]:
        nombres = self._devices.nombres_visibles(scope)
        if nombres is None:
            devices = self._devices.list()
        elif not nombres:
            return []
        else:
            devices = [d for d in self._devices.list() if d.name in nombres]
        if site_id is not None:
            devices = [d for d in devices if d.site_id == site_id]
        if device_group_id is not None:
            devices = [d for d in devices if d.device_group_id == device_group_id]
        return devices

    def register(self, *, name, host, vendor, platform, username, password, site_id, device_group_id, actor) -> "Device":
        site = self._sites.get(site_id)
        if site is None:
            raise ValidationError(f"site {site_id} no existe")
        group = self._device_groups.get(device_group_id) if device_group_id else self._sites.grupo_default(site)
        encrypted = self._vault.encrypt(password)
        device = Device.nuevo(name, host, vendor, platform, username, encrypted, group.id)
        self._devices.add(device)
        self._auditor.despachar([DomainEvent("device_registrado", device, device, actor["username"], {"site_id": site_id})])
        return device

    def move(self, name, target_group_id, actor, *, reason=None, enforce_authz=True) -> "Device":
        # copiar la lógica real de inventory_service.py: move() (líneas 159-298)
        # tal cual -- D_active_job guard (self._jobs.activo_para), D8 (target
        # None -> Default del site actual), no-op idempotente auditado
        ...

    def deregister(self, name, actor) -> None:
        # copiar deregister() (líneas 300-330) tal cual
        ...
```

**Cambio real respecto al código de hoy, no solo forma**: `list()`
(`inventory_service.py: _visible_device_names()`, líneas 334-383) tenía su
**propia** copia del JOIN `Device`↔`DeviceGroup` para resolver visibilidad —
exactamente la 5ta duplicación que `DeviceRepository.nombres_visibles()` (Fase
3) ya centralizó. Esta fase es la que finalmente conecta esa pieza — `Inventory`
deja de tener su propia versión del JOIN, usa la de `DeviceRepository`.

**`register()` no valida el vendor contra `_VALID_VENDORS` acá** — ese chequeo
(`{"cisco_ios", "cisco", "huawei"}`, real en `inventory_service.py:39` **y**
`device_service.py:13`, duplicado en 2 lugares hoy) pasa a `Device.nuevo()`
(ya citado en `FINAL_ARCHITECTURE.md` §1 nota (2): *"`Device.nuevo(...)` valida
el vendor al construirse... en vez de un `if vendor not in _VALID_VENDORS`
suelto"*) — confirmar que `Device.nuevo()` ya tiene esta validación de una fase
anterior; si no quedó escrita en ningún lado todavía, agregarla acá, es el
único lugar que le queda.

### A6 — `Device.actualizar(...)` — nuevo, reemplaza `device_service.update_device()`

**Encontrado real, no cubierto por `Inventory` (cap de 5 métodos)**:
`api/devices.py: PUT /devices/{name}` (real, líneas 107-152) llama
`device_service.update_device(name, host, vendor, platform, username, password)`
— una operación que **no** es create/list/get/move/delete, así que no encaja en
ninguno de los 5 métodos reales de `Inventory`, y el propio código dice que no
se agregan más. Pasa a ser un método de `Device`:

```python
    # app/models/device.py — agregar a Device
    def actualizar(self, *, host=None, vendor=None, platform=None, username=None, password=None, vault=None) -> None:
        if host is not None:
            self.host = host
        if vendor is not None:
            self.vendor = vendor
        if platform is not None:
            self.platform = platform
        if username is not None:
            self.username = username
        if password is not None:
            if vault is None:
                raise ValueError("actualizar(): password nuevo requiere vault")
            self.encrypted_password = vault.encrypt(password)
            self._password = None  # invalida el cache de Device.password (Fase 1)
```

El router pasa a llamar `device_repository.get(name)` → `device.actualizar(...)`
→ `device_repository.add(device)` **directo**, sin pasar por `Inventory` — mismo
patrón que ya usa `Site.renombrar()`/`DeviceGroup.renombrar()` para mutaciones
simples de campo, no para las 5 operaciones que sí necesitan coordinación
cross-entidad.

### A7 — Rewirear `api/devices.py`, `api/sites.py`, `api/device_groups.py`

Cada endpoint pasa de llamar a `device_service`/`site_service`/
`device_group_service` a llamar a `Inventory`/`SiteRepository`/
`Repository[DeviceGroup]`/`DeviceGroupRepository` según corresponda — mismo
patrón 1 a 1 que Fase 5 ya aplicó para `api/vlans.py`/`api/ports.py`. No repetir
acá cada endpoint uno por uno — usar la correspondencia:

| Endpoint real | Pasa a llamar |
|---|---|
| `GET/POST/DELETE /devices` | `Inventory.list()`/`.register()`/`.deregister()` |
| `PUT /devices/{name}` | `device_repository.get()` + `Device.actualizar()` + `device_repository.add()` (A6) |
| `POST /devices/{name}/move` | `Inventory.move()` |
| `POST/GET/PUT/DELETE /sites` | `site_repository.crear_con_grupo_default()`/`.get()`/`Site.renombrar()`+`site_repository.add()`/`.eliminar()` |
| `POST/GET/PUT/DELETE /device-groups` | `device_group_repository` (existe) + `Repository[DeviceGroup]` (A4) + `DeviceGroup.renombrar()` + `.eliminar_con_auto_move()` |

### A8 — `main.py`

`site_service.ensure_base_infrastructure()` (línea 128 real) →
`site_repository.crear_con_grupo_default(BASE_INFRA_SITE_NAME, kind="BASE_INFRASTRUCTURE")`
(A3, ya idempotente). `_bootstrap_admin()` (`Usuario`) **no se toca** — fuera de
alcance de esta fase (depende de `user_service.py`, no asignado a ninguna fase
de este plan).

### Callers reales que quedan rotos hasta el final de esta fase (Línea A)

`device_service.py`/`inventory_service.py`/`site_service.py`/
`device_group_service.py` quedan sin caller real recién cuando A7/A8 terminan —
hasta entonces siguen siendo la ruta real (no se tocan a mitad de la fase). Se
borran en `FASE_7.md`, no acá.

---

## Línea B (liviana esta fase)

### B1 — Confirmar la integración, sin construir nada nuevo

Todo lo que Línea A necesita de Línea B en esta fase ya existe:
`DeviceRepository.nombres_visibles()` (Fase 3), `RoleAssignmentRepository.scope_de()`
(Fase 2). El único trabajo de Línea B acá es **revisar** que `Inventory.list()`
(A5) arma el `VisibilityScope` correcto antes de llamarla — confirmar que el
router (`api/devices.py`) recibe `scope: VisibilityScope = Depends(obtener_scope)`
(mismo patrón que `require_scope`, Fase 2) y se lo pasa a `Inventory.list(scope, ...)`,
no un `user: dict` crudo como hace el código real hoy.

Si al revisar esto aparece algo que `VisibilityScope`/`DeviceRepository` no
cubren (mismo tipo de hueco que ya aparecieron en Fases 2 y 3), documentarlo con
el mismo nivel de detalle que esas 2 fases — no asumir que como "ya está
construido" no puede haber nada nuevo.

**Revisión hecha, 5 puntos verificados — 1 hallazgo real, el resto confirma
que la integración ya está bien:**

1. **El ask explícito de B1 — confirmado.** `api/devices.py: list_devices()`
   recibe `scope: VisibilityScope = Depends(obtener_scope)` (A7) y se lo pasa
   a `Inventory.list(scope, ...)` — no un `user: dict` crudo. `Inventory.list()`
   usa `DeviceRepository.nombres_visibles(scope)` (Fase 3), no su propio JOIN
   (ver corrección de forma en A5, arriba).
2. **Hallazgo real: `Inventory.__init__` recibe `role_assignments`
   (`RoleAssignmentRepository`) pero ningún método de la clase lo usa.**
   Confirmado con `grep -n "self._role_assignments" app/services/
   inventory_service.py` → 0 resultados fuera del `__init__`. No es un bug —
   la autorización de escritura (`register`/`move`/`deregister`) corre
   siempre en el router (`scope.rol_para()`/`require_scope`), nunca dentro
   de `Inventory` — mismo criterio de separación que el resto del catálogo
   desde Fase 2. El parámetro está en la firma canónica de
   `FINAL_ARCHITECTURE.md`/`FASE_6.md` A5 tal cual, así que se mantiene por
   fidelidad al plan, pero queda documentado acá que es vestigial: ningún
   caller real necesita pasarle un `RoleAssignmentRepository` distinto del
   singleton, y ninguna versión futura de `Inventory` debería asumir que
   `self._role_assignments` hace algo hoy.
3. **Los `DomainEvent` que `Inventory` despacha (`device_registrado`/
   `device_movido`/`device_dado_de_baja`) llegan correctamente a
   `AuditRepository._aplicar_scope()` para un viewer no-admin.** Verificado
   leyendo el código: `AuditRecord.desde()` setea `device=evento.device.name`,
   y `_aplicar_scope()` incluye `AuditLogModel.device.in_(nombres)` con
   `nombres = device_repository.nombres_visibles(scope)` — el mismo
   `DeviceRepository` que `Inventory` ya usa. Un operador con acceso al
   device ve su propio evento de auditoría; no hace falta ningún cambio en
   `AuditRepository` para esto.
4. **Los 2 audit rows armados a mano en `api/sites.py: create_site()`**
   (`resource="site"`/`resource="device_group"`, sin `device=`) **también
   matchean correcto contra `_aplicar_scope()`** — vía sus condiciones
   `resource == "site"`/`resource == "device_group"` + `resource_id`. Solo
   relevante en la práctica para system-admins (`create_site` exige
   `is_system_admin`), pero confirmado igual, no asumido.
5. **`require_scope("move_device")`/`_authorize_move_device()`
   (`core/scope.py`, Fase 2) siguen intactos — no dependían de
   `device_service`/`site_service`/`device_group_service`.** Confirmado con
   `grep`: `_lookup_device_scope()` ya usaba `get_session()` directo, no los
   3 servicios que esta fase deja sin caller real. Cero riesgo de import
   roto ahí.

---

## Correcciones reales encontradas implementando esta fase de punta a punta

Mismo método que Fase 5: cada pieza se probó contra un in-memory SQLite real
(sitio/grupo/device/usuario reales, no mocks a nivel de servicio) antes de
darla por terminada. 8 correcciones reales, ninguna prevista en la versión
del plan que existía al empezar a escribir el código:

1. **`DeviceGroup` (A2) le faltaba `created_at` — mismo gap que `Site` ya
   tuvo, no detectado a tiempo para el snippet canónico de A2.**
   `frontend/src/services/api.ts: DeviceGroup` declara `created_at: string`
   no-opcional — sin este campo, cualquier `DeviceGroupRead` real rompe con
   un 500 de validación Pydantic. Agregado a la entidad y a los 2 lugares
   que la construyen (`DeviceGroupRepository._to_domain()`,
   `SiteRepository.grupo_default()`).
2. **`Inventory.register()` (A5) devolvía/auditaba el `device` recién
   construido con `Device.nuevo()`, no el que vuelve de `add()`.**
   `Device.nuevo()` nunca setea `site_id`/`site_name`/`device_group_name`
   (no los recibe) — `DevicePublic` (schema real) los declara **no**
   opcionales, así que el router rompía con un 500 en **cada** alta de
   device. Fix: capturar `device = self._devices.add(device)` (que sí los
   resuelve vía la relación ORM `device_group.site`) antes de auditar/
   devolver.
3. **`Inventory.register()` no rechazaba un nombre ya existente.**
   `create_device()` real rechaza con `IntegrityError` → "already exists";
   `Repository[Device].add()` es upsert (`session.merge()` por
   `pk_field="name"`), así que sin un chequeo explícito, registrar un
   nombre repetido pisaba en silencio el device viejo (host/vendor/etc.)
   en vez de rechazar. Fix: `self._devices.get(name) is not None` antes de
   construir/guardar.
4. **`SiteRepository.grupo_default(site)` no estaba especificado en
   ningún lado con una implementación real.** `FINAL_ARCHITECTURE.md`'s
   snippet de `Inventory.register()`/`move()` usa `site.grupo_default()`
   (método en `Site`, imposible sin acceso a DB desde un dataclass puro) y
   `FASE_6.md`'s propio snippet de A5 usa `self._sites.grupo_default(site)`
   sin que A3 lo defina. Agregado a `SiteRepository` (ya toca
   `DeviceGroupModel` directo en `crear_con_grupo_default()`/
   `contar_devices()`, mismo criterio).
5. **`SiteRepository.visibles(scope)` no estaba especificado — necesario
   para `GET /sites`.** Ninguna sección de A3 cubre el reemplazo de
   `site_service.list_sites_for_user()`. Agregado, usando
   `scope.grants` directo (no `scope.site_ids`, que el propio docstring de
   `VisibilityScope` avisa que excluye grants solo-de-grupo — exactamente
   el caso que `list_sites_for_user()` real sí cuenta).
6. **`DeviceGroupRepository.visibles_para_usuario()`/`en_site()`/
   `contar_miembros()` — mismo tipo de gap que el punto 5, para
   `GET /device-groups`, `GET /sites/{id}/groups`, y el `member_count` de
   cualquier `DeviceGroupRead` real.** Agregados con el mismo criterio que
   `grupos_visibles()` (ya existente, Fase 3) — `site_id`/`device_group_id`
   grants unionados vía `scope.site_ids`/`scope.device_group_ids` (acá sí
   son los correctos — a diferencia del punto 5, esta lectura replica
   exactamente lo que esas 2 properties calculan).
7. **La tabla de A7 dice `POST/GET/PUT/DELETE /device-groups` — el `PUT`
   no existe en el código real.** `api/device_groups.py` real tiene 5
   rutas (`POST /`, `GET /`, `GET /{id}`, `DELETE /{id}`,
   `GET /{id}/devices`) — nunca un `PUT`/rename. `DeviceGroup.renombrar()`
   (A2) existe como método de dominio pero no tiene ningún endpoint HTTP
   real que lo llame — ninguna fase lo expone, y esta fase tampoco lo
   agrega (fuera de alcance, no pedido). Confirmado con
   `grep -n "@router\." app/api/device_groups.py`.
8. **`POST /devices/{name}/save` (`save_device_config()`) es un caller
   real de `vlan_execution_service.py`/`group_job_service.py`/
   `job_service.create_job()`/`audit_service.log_action()` que ni Fase 5
   ni Fase 6 cubren — corrige una claim incorrecta de `FASE_5.md`/
   `FASE_7.md`.** `enqueue_save_job()` (`vlan_execution_service.py:727`)
   llama `group_job_service.create_group_job()` + `job_service.create_job()`
   + `audit_service.log_action()` + despacha un Celery task propio
   (`_save_task`, fuera del `app.tasks` unificado de Fase 5/A5) — ninguno
   de los 3 módulos que Fase 5 marcó "sin caller real después de A6-A9"
   (`vlan_execution_service.py`, en `FASE_5.md`/`FASE_7.md`) ni el que
   Fase 5/7 documentó para `audit_service.py` (§1.5/1.6 de `FASE_7.md`,
   que ya lista 4 archivos con callers reales pero no éste) contaba con
   este quinto. Fix acotado de esta fase: `save_device_config()` pasa de
   `device_service.get_device(name)` a `inventory.get(name)` (satisface el
   criterio "no importa device_service" sin tocar `enqueue_save_job()` en
   sí) — "guardar configuración" no es una operación de VLAN/Puerto (Fase
   5) ni encaja en el cap de 5 métodos de `Inventory` (Fase 6), así que
   migrarla de verdad es alcance nuevo, no de esta fase. Queda como
   corrección pendiente real para quien la tome — `FASE_5.md`/`FASE_7.md`
   corregidos para reflejar este caller.

---

## Dependencias cruzadas

- A5 (`Inventory`) depende de A3 (`SiteRepository`) y A4
  (`Repository[DeviceGroup]`/`DeviceGroupRepository.eliminar_con_auto_move()`)
  — dependencia interna de Línea A, sin cruce con B.
- Ninguna dependencia nueva hacia Línea B más allá de lo que Fases 2/3 ya
  entregaron.

## Criterio de finalización

- [x] `Site`/`DeviceGroup` existen como entidades (`app/models/site.py`,
      `app/models/device_group.py`), con `renombrar()` — el de `DeviceGroup`
      bloqueando por `es_default` (D7), no por cantidad de devices. `Site`
      **y `DeviceGroup`** tienen `created_at` (`Site` también `updated_at`) —
      confirmado contra `SiteModel`/`DeviceGroupModel` reales y
      `frontend/src/types/site.ts`/`frontend/src/services/api.ts: DeviceGroup`
      (`DeviceGroup.created_at` fue un gap real encontrado en esta fase, ver
      corrección 1 arriba).
- [x] `SiteRepository` existe (`crear_con_grupo_default()`, `contar_devices()`,
      `tiene_devices()`, `eliminar()`) — 7ma subclase de `Repository[T]`,
      justificación documentada (transaccional, no de consulta). `GET /sites`
      usa `contar_devices()` para completar `SiteRead.device_count`. También
      `grupo_default()`/`visibles()` (correcciones 4/5 arriba, no estaban en
      ningún snippet del plan original).
- [x] `Repository[T]` genérico tiene `existe(**criterio)`.
- [x] `DeviceGroupRepository` (Fase 3) tiene `eliminar_con_auto_move()`
      agregado. También `visibles_para_usuario()`/`en_site()`/
      `contar_miembros()` (corrección 6 arriba).
- [x] `SiteHasDevicesError`/`DefaultGroupImmutableError` viven en
      `app/core/exceptions.py`. `api/sites.py`/`api/device_groups.py` las
      importan de ahí, no de `site_service`/`device_group_service`.
      `GroupHasDevicesError` no se migra (confirmado sin ningún `raise` real).
- [x] `Inventory` reescrita, respeta el cap de 5 métodos, `list()` usa
      `DeviceRepository.nombres_visibles(scope)` en vez de su propio JOIN.
- [x] `Device.actualizar()` existe, `api/devices.py: PUT /devices/{name}` la usa
      en vez de `device_service.update_device()`.
- [x] `api/devices.py`/`api/sites.py`/`api/device_groups.py` no importan
      `device_service`/`site_service`/`device_group_service` — confirmado con
      `grep`. `api/devices.py: save_device_config()` sigue importando
      `vlan_execution_service` (corrección 8 arriba, fuera de alcance de esta
      fase, no de la lista de 3 módulos de este ítem).
- [x] `main.py` usa `site_repository.crear_con_grupo_default(kind="BASE_INFRASTRUCTURE")`.

- [x] `python -c "import app.composition"` y `python -c "import app.main"` corren sin error (ver `FASE_7.md` sección 5).

## Riesgos / cosas a validar

- **`SiteRepository` es la única subclase de `Repository[T]` justificada por
  transaccionalidad, no por consulta** — si esto genera dudas al escribir el
  código real (¿de verdad necesita heredar de `Repository[T]`, o es mejor una
  clase aparte?), es una discusión legítima — el mismo criterio que ya se usó
  en Fase 3 para decidir si `LoginAttemptRepository`/`DeviceGroupRepository`
  heredaban aplica acá: `Site` sí es una entidad con identidad individual real,
  así que heredar sigue siendo consistente, pero vale confirmarlo antes de
  darlo por sentado.
- **`_default_group_name_for_site()`'s caso raro** (colisión con un grupo
  preexistente literal "Default") — no perderlo al escribir
  `crear_con_grupo_default()`, ver nota en A3.
- **Confirmar si `Device.nuevo()` ya valida el vendor** (nota en A5) — si
  ninguna fase anterior lo dejó escrito, esta fase es la última oportunidad
  antes de que `_VALID_VENDORS` (duplicado hoy en 2 archivos reales) desaparezca
  sin reemplazo.
