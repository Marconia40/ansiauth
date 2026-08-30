# Fase 1 — `Repository[T]` genérico, fusión de drivers, `Device` limpio (Línea A) · `SecretVault`/`RedisCoordinator` (Línea B)

> Parte de [plan de migración a `FINAL_ARCHITECTURE.md`](README.md). Leé el `README.md`
> de esta carpeta antes de esta fase — explica el alcance general, las 2 líneas de
> trabajo, y por qué la app puede quedar no-ejecutable entre fases.

## Objetivo

**Línea A**: construir la infraestructura de persistencia genérica que el resto del
plan va a usar (`Repository[T]`), fusionar los 6 drivers de vendor reales (3 VLAN + 3
Port) en 3 clases `VendorDriver` únicas, construir `PluginRegistry` para resolverlas
por vendor, y limpiar `Device` para que deje de reexponer la superficie completa de
`VendorDriver` como métodos propios y deje de conocer `EXECUTION_MODE`.

**Línea B**: consolidar `SecretVault` (ya existe — sacar el shim de funciones de
módulo) y construir `RedisCoordinator`, fusionando `device_locks.py` + `rate_limiter.py`
en una sola clase — corrigiendo de paso el bug real de fallback permanente que ya está
documentado en `FINAL_ARCHITECTURE.md` (sección `RedisCoordinator`, "Límite real
heredado, no resuelto por este documento").

## Estado previo esperado

- Código en el estado del commit `4d35d80` de `refactor/device-rich-domain` (o
  posterior, siempre que nadie haya tocado los archivos que esta fase toca).
- Nadie tocó todavía: `app/models/device.py`, `app/services/vendors/**`,
  `app/services/device_locks.py`, `app/services/rate_limiter.py`,
  `app/services/secret_service.py`, ni creó `app/core/repository.py`,
  `app/services/plugin_registry.py`, `app/services/redis_coordinator.py`,
  `app/composition.py` (ninguno de estos 4 archivos existe todavía).
- `FINAL_ARCHITECTURE.md` es la referencia de diseño para todo lo que sigue —
  específicamente las secciones: `PluginRegistry` (§1.6, Infraestructura — vendors),
  `Device` (§1, corrección grande sobre las 11 métodos que se sacan), `Repository[T]`
  genérico (§2.2), `RedisCoordinator` (§1.6, Infraestructura — seguridad).

---

## Línea A

### A1 — `app/core/repository.py` (archivo nuevo): `Repository[T]` genérico

Leé primero `app/services/device_service.py:16-40` (`_to_domain`) y
`app/db/session.py:54-66` (`get_session()`, ya existe, no se toca) para el patrón real
que se está generalizando.

**Decisión de diseño que hay que tomar acá, no estaba resuelta en `FINAL_ARCHITECTURE.md`**:
el generic necesita 2 mappers, no 1 — `to_domain` (fila ORM → dataclass, ya existe el
patrón) y **`to_orm`** (dataclass → fila ORM, para el `add()`/upsert — no existe ningún
precedente real de esto en el código actual, hay que escribirlo de cero para cada
entidad a medida que se instancia `Repository[X]` en fases futuras). Fase 1 solo
construye la clase genérica; las instancias concretas (`Repository[VLAN]`, etc.) se
crean en fases posteriores, cuando la entidad correspondiente esté lista.

**Segunda decisión, real, no en el documento**: la PK no siempre es `.id`. Ver la nota
en el `README.md` de esta carpeta — `Device` usa `.name` como clave real. `Repository[T]`
recibe un `pk_field` configurable, default `"id"`.

Crear `app/core/repository.py`:

```python
from __future__ import annotations

from typing import Any, Callable, Generic, Optional, TypeVar

from app.db.session import get_session

T = TypeVar("T")


class Repository(Generic[T]):
    """Puerto genérico de persistencia — get/add/list/remove sobre cualquier
    entidad, configurado por instancia (no por subclase). Ver FINAL_ARCHITECTURE.md
    §2.2 y §2.2.1 para el criterio de cuándo SÍ hace falta subclase.

    ``add()`` es upsert (``session.merge()`` por ``pk_field``), no insert puro —
    ver FINAL_ARCHITECTURE.md §2.2 "add() es upsert, no solo insert" para la
    justificación completa (varias entidades reales dependen de esto: Site.renombrar(),
    Job.marcar_completado(), VLAN.aplicar() idempotente). EXCEPCIÓN conocida:
    AuditRecord NO debe pasar por acá — AuditRepository.append() usa
    session.add() directo, ver Fase 3. Cualquier entidad nueva append-only/
    inmutable debe seguir el mismo criterio.
    """

    def __init__(
        self,
        orm_model: type,
        to_domain: Callable[[Any], T],
        to_orm: Callable[[T], Any],
        pk_field: "str | tuple[str, ...]" = "id",
    ):
        self._orm_model = orm_model
        self._to_domain = to_domain
        self._to_orm = to_orm
        self._pk_field: tuple[str, ...] = (
            pk_field if isinstance(pk_field, tuple) else (pk_field,)
        )

    def _pk_filtro(self, pk: Any) -> dict:
        """*pk* es un valor solo para PK simple, o una tupla en el mismo orden
        que ``pk_field`` para PK compuesta (ej. VLAN: ``(vlan_id, device)``)."""
        valores = pk if isinstance(pk, tuple) else (pk,)
        if len(valores) != len(self._pk_field):
            raise ValueError(
                f"pk esperaba {len(self._pk_field)} valor(es) {self._pk_field}, "
                f"recibió {len(valores)}"
            )
        return dict(zip(self._pk_field, valores))

    def get(self, pk: Any) -> Optional[T]:
        with get_session() as session:
            row = (
                session.query(self._orm_model)
                .filter_by(**self._pk_filtro(pk))
                .first()
            )
            return self._to_domain(row) if row is not None else None

    def list(self, **filtros: Any) -> list[T]:
        with get_session() as session:
            q = session.query(self._orm_model)
            if filtros:
                q = q.filter_by(**filtros)
            return [self._to_domain(row) for row in q.all()]

    def add(self, entidad: T) -> T:
        with get_session() as session:
            row = self._to_orm(entidad)
            pk_reales = [c.name for c in self._orm_model.__mapper__.primary_key]
            if list(self._pk_field) != pk_reales:
                valores_pk = tuple(getattr(entidad, campo) for campo in self._pk_field)
                existente = (
                    session.query(self._orm_model)
                    .filter_by(**self._pk_filtro(valores_pk))
                    .first()
                )
                if existente is not None:
                    for campo in pk_reales:
                        setattr(row, campo, getattr(existente, campo))
            merged = session.merge(row)
            session.flush()
            return self._to_domain(merged)

    def remove(self, pk: Any) -> None:
        with get_session() as session:
            session.query(self._orm_model).filter_by(**self._pk_filtro(pk)).delete()
```

**Corrección real sobre `add()` — encontrada en Fase 4 (`JobRepository`), no en el
diseño original.** `session.merge()` identifica la fila existente por la **primary
key real mapeada en SQLAlchemy**, no por `pk_field` — para `VLAN`/`Puerto` (Fase 2)
funciona porque su PK real **es** la tupla compuesta de `pk_field` (sin `id`
autoincrement separado, por diseño). Pero `JobModel` (y `DeviceModel`, cuando se
instancie `Repository[Device]`) tiene `id` autoincrement como PK real, con
`job_id`/`name` marcados solo `unique=True` — sobre una fila nueva con `id=None`,
`session.merge()` siempre intenta INSERT, nunca reconoce la fila existente por
`job_id`, y una segunda llamada a `add()` sobre el mismo `job_id` revienta con
`UNIQUE constraint failed` en vez de actualizar. Reproducido real marcando un `Job`
completado después de iniciado (2 llamadas a `add()` sobre el mismo `job_id`).
Fix: cuando `pk_field` no coincide con la PK real de la tabla, `add()` busca
primero la fila existente por `pk_field` y copia su PK real sobre la fila nueva
antes de mergear — así `session.merge()` la reconoce como update. No cambia nada
para `VLAN`/`Puerto` (la rama nueva no se activa, `pk_field` ya es la PK real) —
reverificado con el mismo test de upsert de Fase 2 después del fix, mismo
resultado. Este bug estuvo dormido desde que se escribió `Repository[T]` en esta
misma fase porque hasta Fase 4 nunca se había instanciado un `Repository[X]`
contra una tabla con PK autoincrement real + clave de negocio separada.

**Por qué `pk_field` acepta tupla, no solo string — no es anticipación, es necesario
ya en Fase 2.** `VLAN`/`Puerto` (Fase 2) no tienen una PK de una sola columna en la
base real — su identidad de negocio es compuesta (`vlan_id`+`device`,
`interface`+`device`, ver `FINAL_ARCHITECTURE.md` §2.4 nota 4: "switch-A y switch-B
pueden tener cada uno su propia VLAN 100"). **Esto también resuelve un bug real en
`add()`, no solo en `get()`/`remove()`**: si la tabla tuviera un `id` autoincrement
separado y `_to_orm()` construyera una fila nueva sin ese `id` (porque el dataclass de
dominio nunca lo carga), `session.merge()` nunca encontraría la fila existente por
`vlan_id`+`device` — insertaría una fila duplicada en cada llamada, rompiendo
exactamente la idempotencia que `add()`-como-upsert existe para garantizar. La
solución no es solo que `get()`/`remove()` acepten una tupla — es que la tabla use la
clave de negocio **como PK real** (`PrimaryKeyConstraint`, sin `id` separado, ver Fase
2 A1/A2) para que `session.merge()` la reconozca de forma nativa. `pk_field` como
tupla es lo que le dice a `Repository[T]` cuáles son esas columnas para `get()`/
`remove()` — `merge()` en `add()` ya las toma solo de cómo esté mapeada la clase ORM,
no necesita que se las digamos.

No hace falta un método `existe(**criterio)` todavía (mencionado en
`FINAL_ARCHITECTURE.md` §2.2.1 para `Site`/`DeviceGroup`) — se agrega en la fase que
efectivamente instancie `Repository[Site]`, no en esta.

**No se instancia ningún `Repository[X]` concreto en esta fase.** Eso pasa en Fase 2
(`VLAN`, `Puerto`) en adelante, a medida que cada entidad esté lista.

### A2 — Fusión de drivers de vendor: 6 clases → 3

**Por qué esto va en Fase 1 y no se deja para más adelante**: `Device.driver` (A4,
abajo) es **una sola propiedad** que devuelve **un solo objeto** — no tiene sentido si
todavía existen `HuaweiVlanDriver`/`HuaweiPortDriver` como 2 clases separadas. La
fusión tiene que pasar antes o junto con la limpieza de `Device`, no después.

**Archivos involucrados** (leerlos en este orden antes de tocar nada):
1. `app/services/vendors/base.py` — `BaseVendorDriver(ABC)`, 7 métodos VLAN
   (`create_vlan`, `delete_vlan`, `update_vlan`, `save_config`, `get_vlans` abstracto;
   `list_vlans`, `get_vlan` concretos, implementados en términos de `get_vlans`).
2. `app/services/vendors/port_driver_base.py` — `BasePortDriver(ABC)`, 10 métodos:
   `list_ports` (abstracto) + `update_port_description`/`set_port_admin_state`/
   `set_port_access_vlan`/`set_trunk_pvid_vlan`/`set_trunk_allowed_vlans`/
   `configure_port`/`shutdown_port`/`enable_port`/`get_port` (con implementación
   default que lanza `NotImplementedError` — ver docstrings reales de cada uno,
   `port_driver_base.py:59-403`).
3. `app/services/vendors/huawei/vlan_driver.py` — `HuaweiVlanDriver(BaseVendorDriver)`,
   303 líneas.
4. `app/services/vendors/huawei/port_driver.py` — `HuaweiPortDriver(BasePortDriver)`,
   631 líneas.
5. `app/services/vendors/cisco/vlan_driver.py` — `CiscoVlanDriver(BaseVendorDriver)`,
   250 líneas.
6. `app/services/vendors/cisco/port_driver.py` — `CiscoPortDriver(BasePortDriver)`,
   448 líneas.
7. `app/services/vendors/mock.py` — `MockVlanDriver(BaseVendorDriver)` +
   `MockPortDriver(BasePortDriver)`, 195 líneas, las 2 clases en el mismo archivo.

**Paso 1 — fusionar las 2 ABC en una sola `VendorDriver`.**
Reescribir `app/services/vendors/base.py` para que la clase se llame `VendorDriver(ABC)`
y contenga los 7 métodos que ya tiene `BaseVendorDriver` **más** los 10 métodos de
`BasePortDriver` (mismas firmas, mismos docstrings, sin cambiar lógica — es un
copy-paste de método, no una reescritura). Borrar `app/services/vendors/port_driver_base.py`
una vez que su contenido esté copiado adentro de `base.py`. Actualizar el import en
cualquier archivo que hoy haga `from app.services.vendors.port_driver_base import BasePortDriver`
(hoy: `port_driver_base.py` mismo no importa desde otros lados salvo TYPE_CHECKING en
`device.py`, que A4 va a limpiar de todas formas).

**Paso 2 — fusionar cada par vendor-específico.**
Para Huawei: crear `app/services/vendors/huawei/driver.py` con
`class HuaweiVendor(VendorDriver):` conteniendo **todos** los métodos de
`HuaweiVlanDriver` (`vlan_driver.py`) **y todos** los métodos de `HuaweiPortDriver`
(`port_driver.py`), copiados verbatim (mismo cuerpo, mismos imports que usen, sin
cambiar ninguna línea de lógica de negocio ni de parsing). Borrar
`app/services/vendors/huawei/vlan_driver.py` y `.../huawei/port_driver.py` una vez
copiado. Mismo procedimiento para Cisco: `app/services/vendors/cisco/driver.py`,
`class CiscoVendor(VendorDriver):`, fusiona `CiscoVlanDriver`+`CiscoPortDriver`, borra
los 2 archivos viejos.

Para Mock: **no hace falta archivo nuevo** — `mock.py` ya tiene las 2 clases juntas,
solo hay que fusionarlas en `class MockVendor(VendorDriver):` con los métodos de
`MockVlanDriver` + `MockPortDriver` combinados en una sola clase, en el mismo archivo.
Ojo con el estado compartido: `_mock_vlans`/`reset_mock_vlans()` (nivel de módulo) se
mantienen igual, ambos grupos de métodos ya los usaban desde el mismo módulo.

**Verificación de que la fusión fue mecánica, no una reescritura**: contar métodos
antes y después. `HuaweiVlanDriver` + `HuaweiPortDriver` deberían sumar exactamente los
mismos métodos (mismo nombre, misma firma) que `HuaweiVendor` después de la fusión —
ningún método nuevo, ninguno perdido. Repetir para Cisco y Mock.

### A3 — `app/services/plugin_registry.py` (archivo nuevo): `PluginRegistry`

```python
class PluginRegistry:
    """FINAL_ARCHITECTURE.md §1.6 — un diccionario con superpoderes: dado el
    vendor de un device, devuelve el VendorDriver que sabe hablarle. Reemplaza
    el if/elif de vendors/dispatcher.py (borrado en esta misma fase, ver abajo)."""

    def __init__(self):
        self._vendors: dict[str, "VendorDriver"] = {}

    def registrar(self, vendor: str, driver: "VendorDriver") -> None:
        self._vendors[vendor] = driver

    def obtener(self, vendor: str) -> "VendorDriver":
        if vendor not in self._vendors:
            raise ValueError(f"No hay driver registrado para vendor='{vendor}'")
        return self._vendors[vendor]
```

**Borrar `app/services/vendors/dispatcher.py`** (`get_driver`/`get_vendor_driver`/
`get_port_driver`/`get_port_vendor_driver`, hoy con el `if vendor in _CISCO_VENDORS: ...`
— exactamente el `if/elif` que `PluginRegistry` reemplaza) una vez que `Device` (A4) ya
no lo importe.

**`app/services/vendors/__init__.py` — encontrado con un segundo pase de grep,
nadie lo había mirado.** Real, hoy re-exporta `get_driver`/`get_port_driver`/
`get_port_vendor_driver`/`get_vendor_driver` desde `dispatcher.py` (recién
borrado) y `BasePortDriver`/`BaseVendorDriver` desde `base.py`/
`port_driver_base.py` (el 2do borrado, el 1ro renombrado a `VendorDriver` en
este mismo `A2`). Sin tocarlo, `import app.services.vendors` rompe apenas se
borre `dispatcher.py`, aunque nada más en el proyecto haga ese import hoy
(confirmar con grep — si de verdad no tiene caller real, se simplifica a
re-exportar solo `VendorDriver`; si algo sí lo usa, actualizar esos imports
también). Reescribir a:

```python
from app.services.vendors.base import VendorDriver

__all__ = ["VendorDriver"]
```

**Composición al arrancar — archivo nuevo `app/composition.py`.** No existe hoy ningún
lugar central donde se arme el grafo de dependencias (`main.py` conecta FastAPI
directo). Crear este archivo con una función `build_plugin_registry() -> PluginRegistry`
que, según `EXECUTION_MODE` (importado de `app.core.config`), registra:

```python
def build_plugin_registry() -> PluginRegistry:
    from app.core.config import EXECUTION_MODE
    from app.services.plugin_registry import PluginRegistry
    registry = PluginRegistry()
    if EXECUTION_MODE == "mock":
        from app.services.vendors.mock import MockVendor
        registry.registrar("huawei_vrp", MockVendor())
        registry.registrar("cisco_ios", MockVendor())
    else:
        from app.services.vendors.huawei.driver import HuaweiVendor
        from app.services.vendors.cisco.driver import CiscoVendor
        registry.registrar("huawei_vrp", HuaweiVendor())
        registry.registrar("cisco_ios", CiscoVendor())
    return registry
```

**Corrección — no es el único lugar todavía, y no pasa a serlo hasta más adelante.**
`Device` deja de leer `EXECUTION_MODE` en esta fase (A4). Pero `vlan_service.py:65`
(rama `device_id is None` de `get_vlans()`) y las ~11 ocurrencias de `port_service.py`
(cada función de escritura + `list_ports()`) **siguen leyéndolo** después de esta
fase — no se tocan hasta Fase 2/Fase 5 (ver "Callers reales que quedan rotos" más
abajo). `app/composition.py`/`build_plugin_registry()` es el único lugar que decide
**qué implementación de `VendorDriver` registrar** por vendor — no es, todavía, el
único lugar del código que menciona la env var. Eso se vuelve cierto recién cuando
`port_service.py`/`vlan_service.py` (los archivos, no solo las llamadas a `Device`)
dejen de existir. `app/composition.py` va a ir creciendo en fases futuras (acá va a
vivir también la construcción de `RedisCoordinator`, `Repository[X]` concretos, etc.)
— no hace falta anticipar esas partes ahora, alcanza con `build_plugin_registry()`.

**No cablear todavía `app/main.py` para usar esto** — `Orquestador`/`GroupOperationRunner`
(que van a recibir el `PluginRegistry` indirectamente vía `Device`) no existen hasta
Fase 5. Dejar `build_plugin_registry()` sin llamar desde `main.py` por ahora (se llama
una sola vez en Fase 5, cuando haya algo real que la use). Si esto genera un warning de
"función sin uso" en el linter, es esperado — dejarlo así.

**Nota para la futura suite de tests (fuera de alcance de este plan, pero relevante
para quien la escriba): esto permite que los tests dejen de depender de
`EXECUTION_MODE` por completo.** Hoy, para probar código que toca un `Device`, hay que
correr el proceso entero con `EXECUTION_MODE=mock` — es una variable de entorno
global, todo el proceso queda en un modo o el otro. Con `PluginRegistry`, un test
puede construir su propio registro y pisar el global antes de que cualquier
`Device.driver` lo consulte:

```python
def test_algo(monkeypatch):
    from app.services.plugin_registry import PluginRegistry
    from app.services.vendors.mock import MockVendor

    fake_registry = PluginRegistry()
    fake_registry.registrar("cisco_ios", MockVendor())
    monkeypatch.setattr("app.composition.plugin_registry", fake_registry)
    # a partir de acá, cualquier Device(vendor="cisco_ios", ...).driver
    # devuelve MockVendor() sin haber tocado EXECUTION_MODE para nada
```

Esto **solo funciona si el import adentro de `Device.driver` (A4, abajo) se queda
exactamente como está escrito** — `from app.composition import plugin_registry`
**adentro del cuerpo del método**, no como import de módulo arriba del archivo. Un
import a nivel de módulo se resuelve una sola vez, cuando `device.py` se importa por
primera vez, y el `monkeypatch.setattr` de arriba llegaría tarde. El import local
(dentro de la property) se re-evalúa en cada llamada — lee el valor de
`app.composition.plugin_registry` que sea en ese momento, incluido el que un test haya
pisado. Esto no es un detalle de estilo, es lo que hace posible este patrón — dejarlo
así a propósito, no "optimizarlo" a import de arriba del archivo en ninguna fase futura.
`EXECUTION_MODE` pasa a ser una decisión de **despliegue** (qué corre en producción),
no de **testing** — que es, en el fondo, lo mismo que ya buscaba
`FINAL_ARCHITECTURE.md` al decidir "una sola vez al arrancar", solo que la
consecuencia para tests nunca se había hecho explícita.

### A4 — `app/models/device.py`: limpiar `Device`

**Estado actual real** (`app/models/device.py`, 107 líneas) — 3 métodos privados de
resolución (`_get_vlan_driver`, `_get_port_driver`, `_get_password`, cada uno con su
propio `if EXECUTION_MODE == "mock"`) + 11 métodos públicos pass-through
(`create_vlan`, `delete_vlan`, `update_vlan_description`, `list_vlans`, `save_config`,
`configure_port`, `set_port_admin_state`, `set_port_access_vlan`,
`set_trunk_allowed_vlans`, `update_port_description`, `list_ports`).

**Estado final** — reemplazar los campos `_vlan_driver`/`_port_driver`/`_password` y
los 3 métodos `_get_*` por:

```python
    _driver: "VendorDriver | None" = field(default=None, repr=False, compare=False, init=False)
    _password: "str | None" = field(default=None, repr=False, compare=False, init=False)

    @property
    def driver(self) -> "VendorDriver":
        if self._driver is None:
            from app.composition import plugin_registry  # ver nota abajo
            self._driver = plugin_registry.obtener(self.vendor)
        return self._driver

    @property
    def password(self) -> str:
        if self._password is None:
            from app.composition import secret_vault  # ver nota abajo
            self._password = secret_vault.decrypt(self.encrypted_password)
        return self._password
```

**Nota real sobre el import**: `app/composition.py` (A3) define
`build_plugin_registry()` como función, no como singleton importable — para que
`Device.driver` pueda hacer `from app.composition import plugin_registry` como en el
snippet de arriba, `composition.py` necesita **además** exponer una instancia ya
construida a nivel de módulo:

```python
plugin_registry = build_plugin_registry()
```

Agregar esa línea al final de `app/composition.py` (A3). Mismo criterio para
`secret_vault` — `SecretVault` ya existe (`secret_service.py`), Línea B expone la
instancia en Fase 1 (B1, abajo) — Línea A depende de que **ese nombre exista** en
`app/composition.py`, no depende de ningún archivo que B esté editando directamente.
Ver "Dependencias cruzadas" más abajo — esto es la única conexión real entre A y B en
esta fase, y no es bloqueante en el sentido estricto (A puede escribir el código de
`Device.password` sin que `secret_vault` exista todavía; solo no va a poder ejecutarse
hasta que B termine su parte).

**Borrar los 11 métodos pass-through completos**: `create_vlan`, `delete_vlan`,
`update_vlan_description`, `list_vlans`, `save_config`, `configure_port`,
`set_port_admin_state`, `set_port_access_vlan`, `set_trunk_allowed_vlans`,
`update_port_description`, `list_ports` — ninguno sobrevive. La lógica de cada uno
(incluida la llamada a `vlan.validate_name()` dentro de `create_vlan`/
`update_vlan_description`, líneas 73 y 80 del archivo actual) **no se mueve a ningún
lado en esta fase** — se resuelve en Fase 2, cuando `VLAN.aplicar()` empiece a llamar
`device.driver.create_vlan(...)` directo y decida ahí mismo si valida el nombre antes.

**Actualizar el docstring de la clase** — ya no dice "sabe crear/borrar/listar sus
propias VLANs y puertos", pasa a algo como "identidad de un device administrado (site,
grupo, credenciales) — expone su driver de vendor y su password resueltos y
cacheados, no ejecuta nada por sí mismo".

### Callers reales que quedan rotos hasta fases posteriores (Línea A)

**No son simétricos — `VLAN` y `Puerto` están en estados reales distintos hoy.**
Verificado con grep antes de escribir esto (no asumido):
`grep -n "device\.\(configure_port\|set_port\|update_port_description\|list_ports\)" app/services/port_service.py`
no devuelve **ningún** resultado. `port_service.py` nunca se refactorizó para delegar a
`Device` — cada una de sus funciones tiene su propia resolución de driver
(`_get_driver()` → `vendors.dispatcher.get_port_driver()`, no `Device`) y su propio
`if EXECUTION_MODE == "mock": ...`. `vlan_service.py` sí — su propio comentario
(`vlan_service.py:31-38`) dice explícitamente que sus funciones son "thin delegates"
a `Device` desde una refactorización anterior a este plan.

- **`app/services/vlan_service.py`** — `create_vlan_on_device()`, `delete_vlan()`,
  `update_vlan_description()`, `save_config_on_device()` llaman `device.create_vlan(...)`
  etc. directo — **se rompen en esta misma fase**, en mock y en real (`Device`
  decidía el modo internamente, para las 2). `get_vlans()` es mixto: la rama
  `device_id is not None` llama `device.list_vlans()` (se rompe acá); la rama
  `device_id is None` (línea 65) tiene su **propio** `if EXECUTION_MODE == "mock":
  return _mock_vlans` que no toca `Device` para nada — **sigue funcionando sin
  cambios después de esta fase**. Ese `EXECUTION_MODE` puntual no se saca en Fase 1
  ni en Fase 2 — `FINAL_ARCHITECTURE.md` ya lo marcó como fuera de alcance (es un
  atajo de API para "no especifiqué device", no una decisión de driver).
- **`app/services/port_service.py`** — **no se rompe por A4** (no llama a ningún
  método de `Device`). Se rompe por **A3** (`vendors/dispatcher.py` deja de existir,
  y `port_service.py: _get_driver()` lo importa) — pero **solo en la rama real** de
  cada función; la rama `if EXECUTION_MODE == "mock":` de cada una de sus ~7
  funciones de escritura + `list_ports()` sigue funcionando igual que hoy, sin
  tocar nada de Línea A. Los 11 `EXECUTION_MODE` de este archivo **no se tocan en
  esta fase** — se resuelven cuando `Puerto`/`Orquestador` absorban este archivo
  (Fase 2/Fase 5), no antes.
- `app/services/vlan_execution_service.py`, `port_execution_service.py`,
  `port_config_service.py` — llaman a las funciones de los 2 archivos de arriba;
  heredan la misma asimetría (las llamadas hacia `vlan_service.py` rotas ya en esta
  fase, las llamadas hacia `port_service.py` rotas solo en su rama real).
- Cualquier test bajo `tests/test_device_*.py` que invoque estos métodos directo (no se
  tocan tests en este plan, per acuerdo — pero si alguien corre la suite igual, van a
  fallar acá, es esperado).
- **`app/api/ports.py: _require_port_driver_with()` (líneas 124-153) — encontrado
  implementando A2, no estaba en ningún grep anterior de este plan.** Usado por los 7
  endpoints de escritura de puertos (`update_port_description`/`set_port_admin_state`/
  `set_port_access_vlan`/`set_trunk_allowed_vlans`/`configure_port`/`shutdown_port`/
  `enable_port`). Importa `dispatcher.get_port_driver` (se rompe en A3, ya cubierto por
  el resto de esta lista) **y además** `from app.services.vendors.port_driver_base import
  BasePortDriver` para un chequeo de introspección: `getattr(type(driver), method_name)
  is getattr(BasePortDriver, method_name)` — así detecta "el driver no sobreescribió
  este método, sigue siendo el default `NotImplementedError`" y devuelve un 501
  `VENDOR_NOT_SUPPORTED` controlado en vez de la excepción cruda. Se rompe en **A2**
  (el módulo `port_driver_base` deja de existir), un caller distinto del resto de la
  lista. **Importante para quien rewiree `api/ports.py` en Fase 5**: no alcanza con
  cambiar el import de `dispatcher` — el símbolo del `getattr()` también tiene que
  pasar de `BasePortDriver` a `VendorDriver`, o el chequeo de introspección compara
  contra una clase que ya no existe.

---

## Línea B

### B1 — `SecretVault`: sacar el shim, exponer instancia en `composition.py`

Leer `app/services/secret_service.py` completo (43 líneas) primero. Ya existe
`class SecretVault` con `encrypt()`/`decrypt()`, y un singleton de módulo
`vault = SecretVault()`, más 2 funciones-shim `encrypt_password(plain)`/
`decrypt_password(encrypted)` que delegan a `vault`.

**Borrar las 2 funciones-shim** (`encrypt_password`/`decrypt_password`) — quedan solo
la clase `SecretVault` y el singleton `vault = SecretVault()` (el singleton se
mantiene, es lo que se va a exponer vía `composition.py`, no hace falta que cada caller
construya su propio `SecretVault()`).

**Buscar todos los call sites reales de las 2 funciones-shim** (usar
`grep -rn "secret_service.encrypt_password\|secret_service.decrypt_password\|from app.services.secret_service import encrypt_password\|from app.services.secret_service import decrypt_password"`
sobre `app/`) y reemplazar cada uno por `secret_service.vault.encrypt(...)` /
`secret_service.vault.decrypt(...)` (mismo objeto, solo cambia cómo se llama). Al
momento de escribir este plan, los call sites conocidos son `device_service.py`,
`inventory_service.py`, `port_service.py` — confirmar con el grep real, puede haber
cambiado.

**Agregar a `app/composition.py`** (el archivo que A3 crea en esta misma fase — ver
"Dependencias cruzadas" abajo, es la única vez que A y B tocan el mismo archivo en
Fase 1, y es un agregado no conflictivo, no una edición sobre la misma línea):

```python
from app.services.secret_service import vault as secret_vault  # noqa: F401 — re-exportado
```

### B2 — `app/services/redis_coordinator.py` (archivo nuevo): `RedisCoordinator`

Leer primero `app/services/device_locks.py` (126 líneas) y `app/services/rate_limiter.py`
(100 líneas) completos — son casi simétricos: cada uno tiene su propio
`_get_redis()`/`_redis_client`/`_redis_checked` a nivel de módulo, con el mismo patrón
de "cachear el resultado del primer chequeo de Redis para siempre".

**El bug real que esta fase corrige** (ya documentado en `FINAL_ARCHITECTURE.md`,
sección `RedisCoordinator`): `_redis_checked` se pone en `True` la primera vez que se
llama `_get_redis()`, y nunca se vuelve a intentar — si Redis no respondía en ese
primer chequeo (arranque en frío, blip de red), el proceso queda pegado al fallback en
memoria **para siempre**, sin reintentar, rompiendo la garantía de RNF-ESCAL-01 (locks
entre instancias) justo cuando más hace falta. `RedisCoordinator` no hereda este
comportamiento — reintenta la conexión con un TTL corto en vez de cachear para siempre.

**Diseño**:

```python
import os
import threading
import time
from contextlib import contextmanager

MAX_JOBS_PER_WINDOW: int = 5
WINDOW_SECONDS: float = 60.0
_REDIS_RECHECK_SECONDS: float = 30.0  # nuevo -- reintenta, no cachea para siempre


class RedisCoordinator:
    """FINAL_ARCHITECTURE.md §1.6 -- fusiona device_locks.py + rate_limiter.py.
    limitar()/resetear() = rate_limiter.py; bloquear()/esta_ocupado() =
    device_locks.py. Corrige el fallback permanente de ambos (ver nota arriba)."""

    def __init__(self, redis_url: str | None = None):
        self._redis_url = redis_url or os.getenv("REDIS_URL")
        self._redis_client = None
        self._last_check: float = 0.0
        self._locks: dict[str, threading.Lock] = {}
        self._meta_lock = threading.Lock()
        self._device_timestamps: dict[str, list[float]] = {}

    def _get_redis(self):
        now = time.time()
        if self._redis_client is not None:
            return self._redis_client
        if now - self._last_check < _REDIS_RECHECK_SECONDS:
            return None  # ya falló hace poco, no reintentar todavía
        self._last_check = now
        if not self._redis_url:
            return None
        try:
            import redis as _redis
            r = _redis.from_url(self._redis_url, socket_connect_timeout=2)
            r.ping()
            self._redis_client = r
            return r
        except Exception:
            return None

    def bloquear(self, device_id: str, timeout: float | None = None):
        # cuerpo == device_locks.py: acquire(), mismo comportamiento de
        # locking/timeout, mismos mensajes de TimeoutError -- solo cambia
        # _get_redis() (arriba) en vez del módulo-nivel de device_locks.py
        ...

    def esta_ocupado(self, device_id: str) -> bool:
        # cuerpo == device_locks.py: is_device_busy()
        ...

    def limitar(self, device_id: str) -> None:
        # cuerpo == rate_limiter.py: wait_for_slot()
        ...

    def resetear(self, device_id: str | None = None) -> None:
        # cuerpo == rate_limiter.py: reset()
        ...
```

**Copiar el cuerpo real** de `acquire()`/`is_device_busy()`
(`device_locks.py:44-126`) dentro de `bloquear()`/`esta_ocupado()`, y el de
`wait_for_slot()`/`reset()` (`rate_limiter.py:38-99`) dentro de `limitar()`/
`resetear()` — mismos nombres de variable de estado, pero ahora como atributos de
instancia (`self._locks`, `self._device_timestamps`) en vez de globals de módulo, y
reemplazando cada llamada a `_get_redis()` de módulo por `self._get_redis()` (la nueva
versión de arriba, con reintento). No cambiar la lógica de locking/backoff en sí —
solo el mecanismo de cacheo del cliente Redis.

**Borrar `app/services/device_locks.py` y `app/services/rate_limiter.py`** una vez
copiados. Buscar call sites reales (`device_locks.acquire`, `device_locks.is_device_busy`,
`rate_limiter.wait_for_slot`, `rate_limiter.reset`) — **no migrarlos en esta fase**, son
archivos que Fase 5 (`Orquestador`) y `api/vlans.py`/`api/ports.py` (que hoy llaman
`device_locks.acquire()` directo en 4 lugares, ya documentado en
`FINAL_ARCHITECTURE.md`) van a reescribir para inyectar `RedisCoordinator`. Dejarlos
importando el módulo viejo por ahora es un caller roto más, agregado a la lista de
abajo.

**Agregar a `app/composition.py`**:

```python
from app.services.redis_coordinator import RedisCoordinator
redis_coordinator = RedisCoordinator()
```

### Callers reales que quedan rotos hasta fases posteriores (Línea B)

- `app/api/vlans.py:63,84`, `app/api/ports.py:99-111,209` — llaman
  `device_locks.acquire()`/`is_device_busy()` directo (el módulo ya no existe después
  de esta fase). Se arreglan en Fase 5, cuando estos endpoints reciban
  `RedisCoordinator` inyectado.
- Cualquier archivo que importe `rate_limiter`/`device_locks` a nivel de módulo — buscar
  con grep antes de dar la fase por cerrada, para tener la lista exacta (puede haber
  más de lo que este plan anticipa).

---

## Dependencias cruzadas

- **A3 y B1 escriben en el mismo archivo (`app/composition.py`), pero en líneas
  distintas y sin pisarse**: A crea el archivo con `build_plugin_registry()` +
  `plugin_registry = build_plugin_registry()`; B le agrega la línea de `secret_vault` y
  (B2) `redis_coordinator`. **Orden sugerido, no estricto**: que A cree el archivo
  primero (aunque sea con un placeholder `# TODO: secret_vault, redis_coordinator —
  Línea B`), y B haga un solo commit agregando sus 2 líneas al final — evita que ambas
  líneas creen el archivo en paralelo con contenido incompatible. Si ambas ya
  arrancaron en paralelo, resolver el conflicto de merge es trivial (son líneas
  independientes al final del archivo), no hace falta coordinarse en tiempo real.
- **A4 (`Device.password`) depende de que exista `secret_vault` en `composition.py`
  (B1)** — el código de `Device.password` se puede escribir sin que B haya terminado
  (es solo un `import`), pero no se puede *ejecutar* hasta que B1 esté listo. No es
  bloqueante para escribir el código, sí para probarlo.
- **A4 (`Device.driver`) depende de que exista `plugin_registry` en `composition.py`
  (A3, misma línea)** — dependencia interna de Línea A, no cruza a B.
- Ninguna otra dependencia cruzada en esta fase — A2 (fusión de drivers) y B2
  (`RedisCoordinator`) son completamente independientes entre sí.

## Criterio de finalización

- [ ] `app/core/repository.py` existe, `Repository[T]` tiene los 4 métodos
      (`get`/`list`/`add`/`remove`) con la firma exacta de este documento, `add()` usa
      `session.merge()` — con el fix de PK real vs `pk_field` (nota debajo del
      código, encontrado en Fase 4): busca la fila existente primero cuando
      `pk_field` no coincide con la PK real de la tabla.
- [ ] `app/services/vendors/base.py` define `VendorDriver(ABC)` con los 17 métodos
      fusionados (7 de VLAN + 10 de Port). `port_driver_base.py` no existe más.
- [ ] `HuaweiVendor`, `CiscoVendor`, `MockVendor` existen, cada una implementa
      `VendorDriver` completo. `vlan_driver.py`/`port_driver.py` de huawei y cisco no
      existen más. `mock.py` tiene una sola clase `MockVendor`, no 2.
- [ ] `app/services/plugin_registry.py` existe con `PluginRegistry`.
      `vendors/dispatcher.py` no existe más. `vendors/__init__.py` reescrito,
      sin importar nada de `dispatcher.py`/`port_driver_base.py`.
- [ ] `app/composition.py` existe, expone `plugin_registry`, `secret_vault`,
      `redis_coordinator` a nivel de módulo.
- [ ] `app/models/device.py`: `Device` no tiene ninguno de los 11 métodos
      pass-through ni los métodos `_get_*`. Tiene `driver`/`password` como
      `@property`. No importa `EXECUTION_MODE` en ningún lado del archivo (buscar
      `grep -n EXECUTION_MODE app/models/device.py` — debe devolver 0 resultados).
- [ ] `app/services/secret_service.py`: no tiene `encrypt_password()`/
      `decrypt_password()` como funciones de módulo. Todos los call sites reales
      migrados a `vault.encrypt()`/`vault.decrypt()` (o al `secret_vault` de
      `composition.py`, según corresponda).
- [ ] `app/services/redis_coordinator.py` existe con `RedisCoordinator`, sus 4 métodos
      (`bloquear`/`esta_ocupado`/`limitar`/`resetear`), y el fix del fallback
      permanente (`_last_check`/reintento, no `_redis_checked` fijo). `device_locks.py`
      y `rate_limiter.py` no existen más.
- [ ] Los `grep` de callers rotos (secciones de arriba) están confirmados y
      documentados — no hace falta arreglarlos, sí saber exactamente cuáles son antes
      de pasar a Fase 2.

- [ ] `python -c "import app.composition"` y `python -c "import app.main"` corren sin error (ver `FASE_7.md` sección 5).

## Riesgos / cosas a validar

- **La fusión de drivers es mecánica pero voluminosa** (2400+ líneas entre los 6
  archivos originales) — el riesgo real no es de diseño, es de transcripción: verificar
  método por método que nada quedó afuera. Sugerido: antes de borrar los archivos
  viejos, correr un diff de nombres de método (`grep "def " archivo_viejo.py` vs
  `grep "def " archivo_nuevo.py`) para confirmar que la cuenta cierra.
- **`Device.password` (`_get_password()` original) tenía un comportamiento real**: en
  modo mock, devolvía `None` (nunca desencriptaba) — el fix de este plan (documentado
  en `FINAL_ARCHITECTURE.md`, sección `Device`) es que ahora desencripta **siempre**,
  porque `MockVendor` recibe el parámetro `password` y no lo usa. Confirmar esto al
  fusionar `MockVlanDriver`/`MockPortDriver` (A2) — ningún método de `MockVendor`
  debería leer el parámetro `password` para nada más que ignorarlo. Si alguno lo usa
  de verdad, avisar antes de seguir — cambiaría el diseño de `Device.password`.
- **`app/composition.py` construye `plugin_registry`/`secret_vault`/`redis_coordinator`
  a nivel de import de módulo** (no lazy) — esto significa que importar
  `app.composition` en cualquier lado (incluso un test suelto, aunque no se estén
  actualizando tests en este plan) va a intentar conectarse a Redis y leer
  `EXECUTION_MODE` inmediatamente. Si eso genera ruido/errores molestos durante el
  desarrollo de esta fase, es esperado — no es un bug a arreglar acá.
