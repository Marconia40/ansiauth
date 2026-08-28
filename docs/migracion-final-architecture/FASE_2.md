# Fase 2 — `VLAN`/`Puerto` completos + `Repository[VLAN]`/`Repository[Puerto]` (Línea A) · `VisibilityScope`/`RoleAssignmentRepository` (Línea B)

> Parte de [plan de migración a `FINAL_ARCHITECTURE.md`](README.md). Requiere
> [`FASE_1.md`](FASE_1.md) terminada (Línea A necesita `Repository[T]`/`Device.driver`;
> Línea B necesita `Repository[T]`).

## Objetivo

**Línea A**: `VLAN` y `Puerto` pasan a implementar el contrato completo que
`RecursoGestionable` va a formalizar en Fase 5 (`validar`/`reconciliar`/`aplicar`/
`repositorio`), hablando con `device.driver` directo (ya existe desde Fase 1). `Puerto`
deja de ser 3 clases (`PortInfo`+`PortConfigRequest`+`PortConfigResult`) y pasa a ser
una — con el problema real de "qué campo cambió" resuelto vía `mutation_fields`
(que **ya existe** en `PortConfigRequest` hoy, se hereda, no se inventa de cero).

**Línea B**: `VisibilityScope` (VO) + `RoleAssignmentRepository.scope_de()`
reemplazan `effective_role()` (18 call sites reales, confirmados por grep — no "15+"
aproximado) y los 2 de las 3 funciones "qué puede ver este usuario" que se resuelven
en esta fase (`site_service.list_sites_for_user()`,
`device_group_service.list_groups_for_user()` — la 3ra, `inventory_service.
_visible_device_names()`, y la 4ta, `audit_service._apply_msp_audit_scoping()`,
quedan para Fase 3, dependen de `DeviceRepository`).

## Estado previo esperado

- Fase 1 completa: `Repository[T]` existe (`app/core/repository.py`), `Device.driver`/
  `Device.password` existen, `PluginRegistry`+`VendorDriver` fusionado existen,
  `RedisCoordinator`/`SecretVault` consolidados.
- Nadie tocó todavía: `app/models/vlan.py`, `app/models/port.py`,
  `app/validators/vlan_validator.py`, `app/validators/port_validator.py`,
  `app/core/scope.py`, `app/core/dependencies.py`, `app/services/effective_role.py`,
  `app/db/models.py` (para las tablas nuevas de esta fase).

---

## Línea A

### A1 — `app/models/vlan.py`: completar `VLAN`

**Estado actual real** (ya leído completo, 96 líneas): `__post_init__` valida
`vlan_id` (rango + no-reservado) pero **a propósito no valida `name`** —
`validate_name()` es un método separado, llamado explícito solo por quien vaya a
escribir (hoy `Device.create_vlan()`/`update_vlan_description()`). El motivo, real,
documentado en el propio código (`vlan.py:29-38`): `VLAN` también se construye desde
parsers que leen el device real, y esos nombres pueden no cumplir la regex estricta de
`validate_vlan_name()` — auto-validar en `__post_init__` rompería la lectura de
estado real. **`FINAL_ARCHITECTURE.md` no es preciso en este punto** (dice que
`validar()` "absorbe `validators/vlan_validator.py` completo") — la matización real
es la de arriba, mantenerla:

- `__post_init__` sigue llamando solo `validate_vlan_id_range()` +
  `validate_vlan_not_reserved()` (sin cambios respecto a hoy).
- `validate_name()` se mantiene como método separado — pero pasa a ser el `validar()`
  que pide el contrato `RecursoGestionable`, llamado por `Orquestador` **antes** de
  `aplicar()` (Fase 5). No se renombra a `validar()` todavía en esta fase si eso
  generara fricción — alcanza con que exista y haga lo mismo; el renombre es
  cosmético y se puede hacer en Fase 5 al mismo tiempo que se cablea `Orquestador`.

**Agregar el campo `device`** (nuevo, no existe hoy):

```python
    device: str = ""
```

Ubicarlo después de `status` en la firma del dataclass. Motivo (ya documentado en
`FINAL_ARCHITECTURE.md` §2.4 nota 4): la identidad real de una VLAN persistida es
`(vlan_id, device)`, no `vlan_id` solo — switch-A y switch-B pueden tener cada uno su
propia VLAN 100. Default `""` porque `VLAN` se construye antes de saber a qué device
va a aplicarse (se lo asigna quien la reparte — hoy no existe ese código, se agrega en
Fase 5 con `GroupOperationRunner`); no puede ser un campo requerido sin romper todos
los `VLAN(vlan_id=..., name=...)` que ya existen en el código real y en esta misma
fase (parsers, tests).

**Agregar `reconciliar(device)` y `aplicar(device)`**:

```python
    def reconciliar(self, device: "Device") -> dict:
        """Estado actual de esta VLAN en *device*, leído en vivo.
        Reemplaza vlan_execution_service.py: _capture_pre_state_vlan()."""
        vlans_actuales = device.driver.get_vlans(device, device.password)
        existente = next((v for v in vlans_actuales if v.vlan_id == self.vlan_id), None)
        return {
            "existed": existente is not None,
            "name": existente.name if existente is not None else None,
        }

    def aplicar(self, device: "Device") -> dict:
        """Aplica esta VLAN contra *device* — decide sola si es create, update,
        delete o no-op. Reemplaza vlan_execution_service.py: create_vlan_on_device()/
        delete_vlan()/update_vlan_description()/save_config_on_device() (las 3
        primeras, fusionadas: qué hacer lo decide el estado de `self`, no 3
        funciones separadas — save_config_on_device() no entra acá, ver nota).

        El dict devuelto siempre incluye "accion" — no lo pone el driver (que
        solo sabe de rc/stdout/stderr), lo agrega este método antes de retornar.
        Fase 3 (AuditListener) lo necesita para no perder la granularidad real
        de RF-AUD-02 (filtrar audit log por tipo de acción) detrás del evento
        genérico "recurso_aplicado" que despacha Orquestador."""
        pre_state = self.reconciliar(device)
        if self.eliminar:
            if not pre_state["existed"]:
                return {"rc": 0, "success": True, "changed": False, "noop": True, "accion": "eliminar_vlan"}
            resultado = device.driver.delete_vlan(self.vlan_id, device, device.password)
            return {**resultado, "accion": "eliminar_vlan"}
        if pre_state["existed"] and pre_state["name"] == self.name:
            return {"rc": 0, "success": True, "changed": False, "noop": True, "accion": "crear_vlan"}
        if pre_state["existed"] and pre_state["name"] != self.name:
            self.validate_name()
            resultado = device.driver.update_vlan(self.vlan_id, self.name, device, device.password)
            return {**resultado, "accion": "actualizar_vlan"}
        self.validate_name()
        resultado = device.driver.create_vlan(self.vlan_id, self.name, device, device.password)
        return {**resultado, "accion": "crear_vlan"}

    def repositorio(self) -> str:
        return "vlan"
```

**Decisión de contrato, resuelta — no queda abierta**: `RecursoGestionable` se queda
en 4 métodos para **todos** los implementers, `eliminar()` **no** se agrega como
método del contrato. Motivo: la necesidad de delete es genuinamente asimétrica entre
recursos — `VLAN` sí tiene delete real (RF-VLAN-02), `Puerto` no (un puerto físico
nunca "se borra", se apaga/reconfigura — por eso `Puerto.aplicar()`, arriba, nunca
tuvo esta pregunta). Si `eliminar()` fuera parte del contrato, `Puerto` (y más
adelante `ConfiguracionGlobal`, que tampoco tiene delete — es 1:1 con el device) lo
heredarían sin necesitarlo — superficie muerta, el mismo tipo de cosa que este
documento ya evita en otros lados (`Device.aplicar(tipo, payload)` genérico,
rechazado por el mismo motivo). En cambio, `VLAN` agrega **un campo propio**:

```python
    eliminar: bool = False
```

Lo setea quien construya la `VLAN` con intención de borrado (el router, en el
`DELETE /vlans/{id}` — hoy `vlan_service.py: delete_vlan()`; recableado en Fase 5).
`aplicar()` mira `self.eliminar` **adentro**, mismo criterio que ya usa `Puerto` con
`mutation_fields` — la ambigüedad se resuelve en el recurso que la tiene, no en el
contrato compartido. Cuando se construya `InterfazVirtual` (fuera de este plan, pero
probablemente sí tenga delete real, a diferencia de `Puerto`/`ConfiguracionGlobal`),
sigue el mismo patrón — campo propio, no método nuevo en `RecursoGestionable`.

**`save_config_on_device()` queda fuera de `VLAN.aplicar()` a propósito** — no es
create/update/delete de una VLAN, es "grabar running-config a startup-config" a nivel
device, una operación distinta que no encaja en el ciclo de vida de una VLAN puntual.
No se resuelve en esta fase (no hay ningún RF de este plan que la reclame todavía) —
queda marcado como huérfano, sin caller nuevo, hasta que se decida a qué clase
pertenece (candidato: algo de `ConfiguracionGlobal`, RF-GLOBAL-10, cuando se
construya — fuera de alcance de este plan).

**Repository[VLAN]** — necesita una tabla ORM nueva que hoy no existe
(`grep -n "class.*Vlan" app/db/models.py` no devuelve nada). Agregar a
`app/db/models.py`:

**Resuelto — PK compuesta real, sin `id` autoincrement separado** (ver
`FASE_1.md`, `Repository[T]`, actualizado para aceptar `pk_field` como tupla — leerlo
antes de esta sección si no se hizo ya). `(vlan_id, device)` **es** la primary key de
la tabla, no un `UniqueConstraint` sobre un `id` aparte:

```python
class DeviceVlanModel(Base):
    __tablename__ = "device_vlans"
    vlan_id = Column(Integer, primary_key=True)
    device = Column(String, primary_key=True)
    name = Column(String, nullable=False)
```

**Por qué no un `id` autoincrement + `UniqueConstraint`, que sería lo más común**:
con `id` separado, `_to_orm()` (abajo) construiría una fila nueva **sin ese `id`**
(el dataclass `VLAN` nunca lo carga) — `session.merge()` no tendría cómo encontrar la
fila existente por `vlan_id`+`device`, e insertaría una fila duplicada en cada
llamada a `aplicar()`, rompiendo la idempotencia que motivó que `add()` fuera upsert
en primer lugar (§2.2 de `FASE_1.md`). Con la PK real compuesta,
`session.merge()` reconoce la fila existente de forma nativa — no hace falta lógica
extra en `Repository[T]`, solo que la tabla esté mapeada así.

**Supuesto que estoy tomando, confirmar**: como las migraciones de Alembic quedaron
fuera de alcance de este plan (decisión ya tomada), esta fase agrega la clase
`DeviceVlanModel` como código Python puro — no genera ni corre ninguna migración. Si
alguien necesita una base real con esta tabla antes de que se reconstruya todo el
schema desde cero (según lo ya decidido), va a hacer falta correr
`Base.metadata.create_all()` manualmente o escribir la migración aparte — fuera de
esta fase.

Instanciar en `app/composition.py` (agregado a lo que ya existe de Fase 1):

```python
def _vlan_to_orm(v: "VLAN"):
    from app.db.models import DeviceVlanModel
    return DeviceVlanModel(vlan_id=v.vlan_id, name=v.name, device=v.device)

def _vlan_to_domain(row) -> "VLAN":
    from app.models.vlan import VLAN
    return VLAN(vlan_id=row.vlan_id, name=row.name, device=row.device)

vlan_repository = Repository(
    DeviceVlanModel, _vlan_to_domain, _vlan_to_orm, pk_field=("vlan_id", "device"),
)
```

`vlan_repository.get((100, "switch-A"))` / `.remove((100, "switch-A"))` — la tupla en
el mismo orden que `pk_field`. `add()` no necesita tupla, `session.merge()` la arma
solo desde los atributos de `DeviceVlanModel` que `_to_orm()` ya pobló.

### A2 — `app/models/port.py`: unificar `PortInfo`+`PortConfigRequest`+`PortConfigResult` → `Puerto`

**Ya existe algo importante que reutilizar, no inventar**: `PortConfigRequest`
(`models/port.py:170-266`) **ya tiene** `mutation_fields` como `@property` (líneas
252-266) — exactamente el mecanismo que este plan (y `FINAL_ARCHITECTURE.md`) propone
para que `Puerto.aplicar()` sepa qué campo cambió. No hay que diseñarlo de cero, hay
que **fusionarlo** dentro de la clase única, junto con la validación cruzada real que
ya tiene `PortConfigRequest.__post_init__` (líneas 218-245: `access_vlan` solo válido
si `mode` es access/trunk, `allowed_vlans` solo válido si `mode == "trunk"`, al menos
un campo de mutación presente).

**2 renames reales, no arbitrarios — resuelven un choque de nombres entre el lado de
lectura y el de escritura que existe hoy**:
- `PortInfo.name` (interfaz, lado lectura) y `PortConfigRequest.interface` (lado
  escritura) son el mismo concepto con 2 nombres. La clase unificada usa
  **`interface`** (gana el nombre del lado de escritura, ya es el que usa
  `FINAL_ARCHITECTURE.md` §1).
- `PortInfo.admin_up` (lectura) y `PortConfigRequest.admin_enabled` (escritura) son
  el mismo concepto. La clase unificada usa **`admin_up`** (gana el nombre del lado
  de lectura, también el que ya usa `FINAL_ARCHITECTURE.md` §1).

**Campo real que `FINAL_ARCHITECTURE.md` no menciona y hay que decidir qué hacer
con él**: `PortConfigRequest.allowed_vlan_operation: str = "add"` (línea 216) — no
está en la lista de atributos de `Puerto` del catálogo (§1). Es real (controla si
`allowed_vlans` se suma o reemplaza el trunk existente). Mantenerlo como atributo de
`Puerto` — omitirlo rompería la semántica real de `set_trunk_allowed_vlans`. Marcado
como corrección al catálogo, no una decisión nueva de este plan.

**Diseño final de `Puerto`**:

```python
@dataclass
class Puerto:
    interface: str
    device: str = ""
    description: str | None = None
    admin_up: bool | None = None
    mode: PortConfigMode | None = None
    access_vlan: int | None = None
    allowed_vlans: list[int] | None = None
    allowed_vlan_operation: str = "add"
    poe_enabled: bool | None = None
    # -- solo lectura, el device las reporta, aplicar() nunca las mira --
    operational_up: bool | None = None
    speed: str | None = None
    duplex: str | None = None

    def __post_init__(self) -> None:
        from app.validators.port_validator import validate_interface_name
        validate_interface_name(self.interface)
        # las 3 reglas cruzadas de PortConfigRequest.__post_init__ (mode required
        # con access_vlan/allowed_vlans, etc.) se copian acá tal cual --
        # ver models/port.py:222-245 real, mismo texto de error

    @property
    def mutation_fields(self) -> set[str]:
        campos = ("description", "admin_up", "mode", "access_vlan",
                  "allowed_vlans", "poe_enabled")
        return {c for c in campos if getattr(self, c) is not None}

    def validar(self) -> None:
        from app.validators.port_validator import (
            validate_access_vlan_id, validate_trunk_vlan_id, validate_trunk_vlan_list,
            validate_description,
        )
        if self.access_vlan is not None:
            validate_access_vlan_id(self.access_vlan)
        if self.allowed_vlans is not None:
            validate_trunk_vlan_list(self.allowed_vlans)
        if self.description is not None:
            validate_description(self.description)

    def reconciliar(self, device: "Device") -> dict:
        puertos = device.driver.list_ports(device, device.password)
        existente = next((p for p in puertos if p.interface == self.interface), None)
        return {"existed": existente is not None, "actual": existente}

    def aplicar(self, device: "Device") -> dict:
        """Mismo criterio que VLAN.aplicar() (arriba): el dict devuelto siempre
        incluye "accion", agregado acá, no por el driver — Fase 3 (AuditListener)
        lo necesita para RF-AUD-02. `configure_port()` devuelve un
        `PortConfigResult` (dataclass), no un dict como los métodos puntuales —
        se normaliza acá con `.to_dict()` (el método ya existe,
        `models/port.py:307-317`) para que el caller de `aplicar()` siempre
        reciba la misma forma (dict), sin importar cuántos campos cambiaron."""
        campos = self.mutation_fields
        if len(campos) > 1:
            resultado = device.driver.configure_port(self, device, device.password)
            return {**resultado.to_dict(), "accion": "configurar_puerto"}
        campo = next(iter(campos))
        if campo == "description":
            resultado = device.driver.update_port_description(self.interface, self.description, device, device.password)
            return {**resultado, "accion": "actualizar_descripcion_puerto"}
        if campo == "admin_up":
            resultado = device.driver.set_port_admin_state(self.interface, self.admin_up, device, device.password)
            return {**resultado, "accion": "activar_puerto" if self.admin_up else "desactivar_puerto"}
        if campo == "access_vlan":
            resultado = device.driver.set_port_access_vlan(self.interface, self.access_vlan, device, device.password)
            return {**resultado, "accion": "asignar_vlan_acceso"}
        if campo == "allowed_vlans":
            resultado = device.driver.set_trunk_allowed_vlans(self.interface, self.allowed_vlans, device, device.password)
            return {**resultado, "accion": "configurar_trunk_vlans"}
        raise ValueError(f"Puerto.aplicar(): no hay driver call para el campo {campo!r}")

    def repositorio(self) -> str:
        return "puerto"
```

**Nota sobre `validar()` vs `__post_init__`, mismo criterio que `VLAN`**: acá SÍ tiene
sentido que `__post_init__` valide `interface` siempre (no hay equivalente al problema
de "un parser construye esto con datos que no pasan validación estricta" — `interface`
viene del propio device igual, formato ya vendor-neutral). Las validaciones de
`access_vlan`/`allowed_vlans`/`description` sí quedan en `validar()` explícito (no en
`__post_init__`) porque **sí** pueden llegar `None` en instancias construidas para
lectura (`reconciliar()` arma un `Puerto` desde `PortInfo`-equivalente sin pasar por
`validar()`).

**Borrar `PortInfo`, `PortConfigRequest`, `PortConfigResult`** una vez que `Puerto`
las reemplace — pero **`PortConfigResult` merece una nota**: hoy es el tipo de
**retorno** de `configure_port()` (`success`/`changed`/`interface`/`vendor`/
`execution_time_ms`/`rollback_performed`/`warnings`, ver `models/port.py:269-306`),
no una entidad de dominio — `Puerto.aplicar()` de arriba sigue devolviendo lo que el
driver devuelva (hoy: un `dict` para los métodos puntuales, un `PortConfigResult`
para `configure_port`). **No se unifica con `Puerto`** — son cosas distintas (una es
la entidad, la otra es el resultado de una operación sobre ella), a pesar de que
`FINAL_ARCHITECTURE.md` los agrupa en la misma frase ("unifica `PortInfo`+
`PortConfigRequest`+`PortConfigResult`"). Dejar `PortConfigResult` donde está, sin
tocar — no hace falta para el contrato `RecursoGestionable` (`aplicar()` devuelve lo
que sea, `Orquestador` no le exige un tipo).

**Absorber `validators/port_validator.py` completo — corrección sobre una
versión anterior de esta fase.** La primera versión de este documento dejaba las
5 funciones de validación viviendo en `validators/port_validator.py`, importadas
por `Puerto`. Eso contradice un principio ya establecido en todo este plan (y en
`FINAL_ARCHITECTURE.md`): "no quiero métodos aislados, nada en archivos de
funciones sueltas" — si `Puerto` termina siendo el **único** caller real de esas
5 funciones (confirmar con grep que nadie más las usa, debería dar cierto), es
exactamente el archivo huérfano de un solo caller que ese principio rechaza.
**Se copian las 5 funciones (`validate_interface_name`/`validate_access_vlan_id`/
`validate_trunk_vlan_id`/`validate_trunk_vlan_list`/`validate_description`) como
funciones privadas del módulo `models/port.py`** (`_validate_interface_name()`,
etc. — mismo cuerpo, sin cambiar lógica), llamadas desde `Puerto.__post_init__()`/
`validar()`. **`compress_vlans_cisco()`/`compress_vlans_huawei()`** (mismo
archivo, líneas 106-144) **no son validación** — mover cada una a un método
privado de `CiscoVendor`/`HuaweiVendor` respectivamente (los archivos que A2 de
Fase 1 ya fusionó). Con las 5 funciones de validación copiadas y las 2 de
formato movidas, `validators/port_validator.py` queda vacío — se borra en esta
misma fase, no en Fase 6.

**Mismo criterio para `validators/vlan_validator.py` — corrección equivalente.**
`VLAN.__post_init__()`/`validate_name()` (arriba, A1) hoy están escritos
importando `validate_vlan_id_range()`/`validate_vlan_not_reserved()`/
`validate_vlan_name()` desde ese archivo — mismo problema: si `VLAN` es el único
caller real, se copian las 3 funciones como privadas de `models/vlan.py` (más
`validate_description()`, la 4ta función real del archivo, usada hoy por
`api/vlans.py: update_vlan()` — confirmar en Fase 5, A6, que ese caller pasa a
`Puerto`/`VLAN` también) y `validators/vlan_validator.py` se borra en esta
fase, no en Fase 6.

**`Repository[Puerto]`** — mismo criterio resuelto que `VLAN` (A1): PK compuesta real,
sin `id` autoincrement separado.

```python
class DevicePortModel(Base):
    __tablename__ = "device_ports"
    interface = Column(String, primary_key=True)
    device = Column(String, primary_key=True)
    description = Column(String, nullable=True)
    admin_up = Column(Boolean, nullable=True)
    mode = Column(String, nullable=True)
    access_vlan = Column(Integer, nullable=True)
    allowed_vlans = Column(JSON, nullable=True)  # lista de int
    poe_enabled = Column(Boolean, nullable=True)
```

Mismo supuesto sobre migraciones fuera de alcance que `VLAN`. Instanciar en
`app/composition.py`:

```python
def _puerto_to_orm(p: "Puerto"):
    from app.db.models import DevicePortModel
    return DevicePortModel(
        interface=p.interface, device=p.device, description=p.description,
        admin_up=p.admin_up, mode=p.mode, access_vlan=p.access_vlan,
        allowed_vlans=p.allowed_vlans, poe_enabled=p.poe_enabled,
    )

def _puerto_to_domain(row) -> "Puerto":
    from app.models.port import Puerto
    return Puerto(
        interface=row.interface, device=row.device, description=row.description,
        admin_up=row.admin_up, mode=row.mode, access_vlan=row.access_vlan,
        allowed_vlans=row.allowed_vlans, poe_enabled=row.poe_enabled,
    )

puerto_repository = Repository(
    DevicePortModel, _puerto_to_domain, _puerto_to_orm, pk_field=("interface", "device"),
)
```

**Nota real, no cosmética**: `Puerto` persistido no carga `operational_up`/`speed`/
`duplex` (son de solo lectura, vienen del device en cada `reconciliar()`, no tiene
sentido guardar una copia potencialmente vieja) ni `allowed_vlan_operation` (es una
instrucción de la llamada — "sumar" vs. "reemplazar" — no un atributo persistente del
puerto). El `Repository[Puerto]` guarda el **estado deseado/aplicado**, no cada
detalle de cada llamada que lo produjo — mismo criterio que `VLAN`, que tampoco
persiste `eliminar`.

### Callers reales que quedan rotos hasta Fase 5 (Línea A)

`vlan_service.py`/`port_service.py` (y los 3 archivos de ejecución) **siguen sin
tocarse en esta fase** — ya estaban rotos desde Fase 1 (`vlan_service.py`) o van a
romperse más cuando `vendors/dispatcher.py` deje de existir en la práctica para
`port_service.py`. Nada nuevo que agregar acá — `VLAN`/`Puerto` ganan comportamiento
pero **nadie los llama todavía** (eso es trabajo de `Orquestador`, Fase 5). `api/vlans.py`/
`api/ports.py` tampoco se tocan en esta fase — siguen llamando a los `service.py`
viejos, que siguen rotos desde Fase 1.

---

## Línea B

### B1 — `app/models/visibility_scope.py` (archivo nuevo): `VisibilityScope`

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class VisibilityScope:
    es_system_admin: bool
    grants: tuple[tuple[int, "int | None", str], ...]  # (site_id, device_group_id|None, role)

    def rol_para(self, site_id: int, device_group_id: "int | None" = None) -> "str | None":
        if self.es_system_admin:
            return "super-admin"
        if device_group_id is not None:
            for sid, gid, role in self.grants:
                if sid == site_id and gid == device_group_id:
                    return role
        for sid, gid, role in self.grants:
            if sid == site_id and gid is None:
                return role
        return None

    @property
    def site_ids(self) -> "set[int] | None":
        """Sites con grant SITE-WIDE (gid is None). None = system-admin, sin
        restricción. OJO: esto NO es "todos los sites donde tengo algún grant"
        -- ver nota abajo, site_service.list_sites_for_user() necesita ese
        segundo concepto, distinto, derivado de .grants directo."""
        return None if self.es_system_admin else {sid for sid, gid, _ in self.grants if gid is None}

    @property
    def device_group_ids(self) -> "set[int]":
        return set() if self.es_system_admin else {gid for _, gid, _ in self.grants if gid is not None}
```

**Nota real, encontrada leyendo los 3 usos actuales, no en el documento**:
`site_ids` (arriba) intencionalmente solo cuenta grants site-wide — es el mismo
criterio que `device_group_service.list_groups_for_user()` (real,
`device_group_service.py:93`, `visible_site_ids = {sid for (sid, gid) in grants if
gid is None}`) ya usa hoy, y mapea 1 a 1. Pero `site_service.list_sites_for_user()`
(real, `site_service.py:137-144`) usa un concepto **distinto y más amplio** —
"todos los sites donde tengo *cualquier* grant, sea site-wide o de un grupo
puntual adentro" (porque si tenés acceso a un grupo, tiene sentido poder ver el site
que lo contiene). Ese segundo concepto **no es** `scope.site_ids` — se deriva
directo del campo público `grants`:

```python
sites_con_algun_grant = {sid for sid, _gid, _role in scope.grants}
```

No agregar una 3ra property a `VisibilityScope` para esto — es un one-liner sobre un
campo que ya es público, agregar una property más por cada consumidor puntual iría
en contra del criterio de "cerrado, no crece sin límite" que ya usa este documento en
otros lados. Dejarlo como está, documentado en el docstring de `site_ids` (arriba)
para que quien migre `site_service.py` no reintente inventar `scope.site_ids` para
esto y reintroduzca el bug de fuga de visibilidad que ya se corrigió una vez (ver
`FINAL_ARCHITECTURE.md`, nota (6) sobre `VisibilityScope.site_ids`).

### B2 — `RoleAssignmentRepository` (archivo nuevo: `app/repositories/role_assignment_repository.py`)

```python
from app.core.repository import Repository
from app.db.models import RoleAssignmentModel
from app.db.session import get_session
from app.models.visibility_scope import VisibilityScope


class RoleAssignmentRepository(Repository):
    def __init__(self):
        super().__init__(RoleAssignmentModel, _to_domain, _to_orm)  # ver nota abajo

    def scope_de(self, user: dict) -> VisibilityScope:
        if user.get("is_system_admin"):
            return VisibilityScope(es_system_admin=True, grants=())
        user_id = user.get("id")
        if user_id is None:
            return VisibilityScope(es_system_admin=False, grants=())
        with get_session() as session:
            rows = (
                session.query(
                    RoleAssignmentModel.site_id,
                    RoleAssignmentModel.device_group_id,
                    RoleAssignmentModel.role,
                )
                .filter(RoleAssignmentModel.user_id == user_id)
                .all()
            )
        return VisibilityScope(
            es_system_admin=False,
            grants=tuple((r.site_id, r.device_group_id, r.role) for r in rows),
        )
```

**Nota real sobre por qué esta clase hereda de `Repository[RoleAssignment]` con
`_to_domain`/`_to_orm` como placeholders**: `role_assignment_service.py` (322 líneas)
ya existe y **no se toca en este plan** (`FINAL_ARCHITECTURE.md` lo marca
explícitamente como "ya está bien hecha") — sigue haciendo sus propias queries
directas a `RoleAssignmentModel` para `grant()`/`revoke()`/`list_for_user()`. Esta
fase **no** migra `role_assignment_service.py` a usar `Repository[RoleAssignment]` —
`RoleAssignmentRepository` en esta fase existe **solo** para `scope_de()`. Escribir
`_to_domain`/`_to_orm` triviales (mapeo campo a campo de `RoleAssignmentModel`) para
que la clase sea instanciable, aunque `get()`/`add()`/`list()`/`remove()` genéricos no
se usen todavía en ningún lado real — quedan disponibles para cuando alguna fase
futura decida migrar `role_assignment_service.py` (no es parte de este plan).

### B3 — Reescribir `require_scope()` en `app/core/scope.py`

**Cambio de firma real**: `require_scope(op)` pasaba a `effective_role()` una consulta
nueva por cada chequeo (`core/scope.py:170`, dentro de `with get_session()`). Ahora
recibe `scope: VisibilityScope = Depends(obtener_scope)` (dependencia nueva, ver
abajo) y llama `scope.rol_para(...)` en memoria:

```python
def obtener_scope(
    current: dict = Depends(require_authenticated),
    role_assignment_repo: RoleAssignmentRepository = Depends(get_role_assignment_repo),
) -> VisibilityScope:
    return role_assignment_repo.scope_de(current)
```

`get_role_assignment_repo` — dependency de FastAPI que devuelve el singleton (agregar
`role_assignment_repository = RoleAssignmentRepository()` a `app/composition.py`,
igual que `plugin_registry`/`secret_vault`/`redis_coordinator` de Fase 1, y una
función `get_role_assignment_repo()` que lo devuelva, para poder pisarla en tests con
`app.dependency_overrides` sin pasar por `EXECUTION_MODE` ni nada parecido — mismo
principio de Fase 1 aplicado acá).

Reescribir el cuerpo de `require_scope(op)`:

```python
    async def dep(
        request: Request,
        current: dict = Depends(require_authenticated),
        scope: VisibilityScope = Depends(obtener_scope),
    ) -> dict:
        if op == "move_device":
            await _authorize_move_device(request, current, scope)
            return current

        cfg = OP_MIN_ROLE.get(op)
        if cfg is None:
            raise HTTPException(status_code=500, detail=f"Unknown scope operation: {op}")
        scope_kind, min_role = cfg

        target = await _resolve_target(request, scope_kind)
        if target is None:
            raise HTTPException(status_code=400, detail=(...))  # mismo texto de hoy

        site_id, group_id = _resolver_site_group(target, scope_kind)  # ver nota abajo
        role = scope.rol_para(site_id, group_id)
        _enforce(role, min_role, op, scope_kind, target)
        return current
```

**Problema real que esto expone, no estaba resuelto en `FINAL_ARCHITECTURE.md`**:
`scope.rol_para(site_id, device_group_id)` necesita `(site_id, device_group_id)` —
pero `_resolve_target()` (real, `core/scope.py:251-316`) hoy devuelve un **nombre de
device** (para `scope_kind == "device"`) o un **id de site/group** directo — nunca
resuelve el `(site_id, group_id)` de un device. Eso es exactamente lo que hacía
`effective_role()` internamente vía `_resolve_scope()` (`effective_role.py:97-134`,
el JOIN `DeviceModel`↔`DeviceGroupModel`). Con `effective_role()` reemplazado, ese
JOIN necesita seguir viviendo en algún lado — **`_lookup_device_scope()`** (real,
`core/scope.py:341-359`) ya hace exactamente esto y no se toca (era la duplicación de
`effective_role.py: _resolve_scope()` que `FINAL_ARCHITECTURE.md` marcó para
eliminar — pero eliminar `_resolve_scope()` significa que `_lookup_device_scope()`
pasa a ser el JOIN que **queda**, no el que se borra). Agregar
`_resolver_site_group(target, scope_kind)` como una función chica que:
  - `scope_kind == "site"` → `(target, None)` directo, sin query.
  - `scope_kind == "device_group"` → 1 query a `DeviceGroupModel` por `site_id` dado
    `target` (el `group_id`).
  - `scope_kind == "device"` → llama `_lookup_device_scope(session, target)` (ya
    existe, se mantiene tal cual).

**`_authorize_move_device()`** — recibe `scope` también, reemplaza sus 2 llamadas a
`effective_role()` (`core/scope.py:233,240-241`) por `scope.rol_para(site_id, None)`
sobre el `(site_id, group_id)` que ya resuelve `_lookup_device_scope()` (sin cambios
en esa función). El resto de la lógica de dispatch same-site/cross-site
(`core/scope.py:180-246`) no cambia — sigue siendo la rama especial que
`FINAL_ARCHITECTURE.md` ya documentó como excepción (no entra en `OP_MIN_ROLE`).

**Borrar `effective_role.py`** por completo — no queda ni `effective_role()` ni
`_resolve_scope()` (el JOIN que hacía duplicado ya vive en `_lookup_device_scope()`,
que se queda en `core/scope.py`).

### B4 — Migrar los 18 call sites reales de `effective_role()`

Confirmado por grep (no aproximado), lista completa a migrar en esta fase:

| Archivo | Líneas | Patrón de reemplazo |
|---|---|---|
| `app/api/device_groups.py` | 45, 91, 109, 149 | recibir `scope: VisibilityScope = Depends(obtener_scope)`, resolver `(site_id, group_id)` del recurso (ya lo hacían con `effective_role(session, ...)`, ahora sin `session`/sin query) y llamar `scope.rol_para(...)` |
| `app/api/jobs.py` | 137 (`_check_device_scope`) | mismo patrón — recibe `scope` inyectado en vez de abrir su propia `get_session()` |
| `app/api/ports.py` | 19 | ídem |
| `app/api/sites.py` | 91, 134 | ídem (acá `scope_kind` siempre es `"site"`, sin JOIN) |
| `app/api/vlans.py` | 31 (`_authz_devices`) | ídem — este es un loop sobre varios devices, `scope.rol_para()` en memoria hace que el loop no dispare N queries (hoy si dispara N, una por `effective_role()`) |
| `app/api/devices.py` | 59, 81, 125, 193, 214 | ídem |
| `app/core/scope.py` | 170, 233, 240, 241 | ya cubierto en B3 |

Cada endpoint que hoy abre su propio `with get_session(): role = effective_role(session,
user, ...)` pasa a recibir `scope` por `Depends(obtener_scope)` (mismo patrón que
`require_scope`, B3) y llamar `scope.rol_para(site_id, group_id)` — **sin abrir
sesión propia**, el JOIN device→(site,group) sigue centralizado en
`_lookup_device_scope()`/la función nueva de B3 si el endpoint necesita resolverlo
desde un nombre de device. Revisar cada uno de los 18 call sites individualmente —
varios ya tienen su propio `_check_device_scope`/`_authz_devices` como wrapper local
(`api/jobs.py`, `api/vlans.py`) que hay que actualizar, no solo la línea del
`effective_role()` en sí.

### B5 — Eliminar `app/core/dependencies.py`

Archivo completo (`get_current_user()`, duplicado de `core/scope.py: get_current_user()`
pero sin el enriquecimiento de `require_authenticated()` — no revalida `is_active`
contra la DB). Único caller real: `app/api/group_jobs.py:5`
(`from app.core.dependencies import get_current_user`). Cambiar ese import a
`from app.core.scope import require_authenticated` y usar `Depends(require_authenticated)`
en vez de `Depends(get_current_user)` en las rutas de ese archivo. Borrar
`app/core/dependencies.py`.

---

## Dependencias cruzadas

- Ninguna dependencia cruzada **dentro** de esta fase entre A y B — `VLAN`/`Puerto`
  (A) no tocan RBAC, `VisibilityScope`/`require_scope` (B) no tocan `VLAN`/`Puerto`.
- B depende de que Fase 1 (`Repository[T]`) esté terminada (ya cubierto en "Estado
  previo esperado").
- Ningún archivo se toca por las 2 líneas en esta fase.

## Criterio de finalización

- [ ] `VLAN` tiene `device`, `eliminar: bool = False`, `reconciliar()`, `aplicar()`
      (con la rama de `self.eliminar`), `repositorio()`. `RecursoGestionable` se
      queda en 4 métodos — no se agregó `eliminar()` al contrato.
- [ ] `Puerto` (nueva, en `models/port.py`) reemplaza a `PortInfo`+`PortConfigRequest`.
      `PortConfigResult` se mantiene sin tocar. `mutation_fields` funciona sobre los
      6 campos mutables. `aplicar()` despacha correcto para 1 campo y para 2+
      (composite).
- [ ] `validators/port_validator.py` y `validators/vlan_validator.py` **no
      existen** — sus funciones de validación son privadas de `models/port.py`/
      `models/vlan.py`, `compress_vlans_*` movidas a `HuaweiVendor`/`CiscoVendor`.
- [ ] `app/db/models.py` tiene `DeviceVlanModel`/`DevicePortModel`, cada una con PK
      compuesta real (`primary_key=True` en las 2 columnas de negocio), sin `id`
      autoincrement separado. `vlan_repository`/`puerto_repository` en
      `app/composition.py` instanciados con `pk_field=(...)` tupla.
- [ ] `app/models/visibility_scope.py` existe con `VisibilityScope`, `rol_para()`,
      `site_ids`, `device_group_ids` — con el docstring que distingue `site_ids` de
      "sites con cualquier grant".
- [ ] `RoleAssignmentRepository.scope_de()` existe y funciona para system-admin y
      usuario normal.
- [ ] `require_scope()`/`_authorize_move_device()` usan `scope.rol_para()`, no
      `effective_role()`. `_lookup_device_scope()` sigue existiendo (no se borra).
- [ ] Los 18 call sites de la tabla de B4 están migrados — confirmar con
      `grep -rn "effective_role(" app/` que solo quedan menciones en comentarios/
      docstrings, ninguna llamada real.
- [ ] `app/core/dependencies.py` no existe. `app/services/effective_role.py` no
      existe. `app/api/group_jobs.py` usa `require_authenticated`.

- [ ] `python -c "import app.composition"` y `python -c "import app.main"` corren sin error (ver `FASE_7.md` sección 5).

## Riesgos / cosas a validar

- **PK compuesta — resuelto, ver A1.** `Repository[T]` (actualizado en `FASE_1.md`)
  acepta `pk_field` como tupla; `DeviceVlanModel`/`DevicePortModel` usan PK compuesta
  real, no `id` autoincrement + `UniqueConstraint`. Si al ejecutar esta fase
  `FASE_1.md` ya estaba implementado con la versión vieja de `Repository[T]` (`pk_field:
  str` simple), hay que volver a tocar `app/core/repository.py` — avisar antes de
  seguir si ese es el caso, no reintroducir el bug de `add()` duplicando filas.
- **Decisión ya tomada, dejar registrado para Fase 5**: `VLAN.eliminar: bool` es un
  campo propio de `VLAN`, no un método de `RecursoGestionable`. Cuando se construya
  `InterfazVirtual` (fuera de este plan), si tiene delete real, sigue el mismo
  patrón — campo propio, no tocar el contrato compartido.
- **`site_service.list_sites_for_user()`/`device_group_service.list_groups_for_user()`
  no se migran en esta fase** — quedan usando `RoleAssignmentModel` directo, como
  hoy. Migrarlas a `VisibilityScope`/`RoleAssignmentRepository.scope_de()` no está en
  el alcance de Fase 2 (no lo pidió el usuario explícitamente todavía) — si se
  quiere hacer, es trabajo adicional de Línea B, en esta fase o en una posterior, a
  confirmar.
