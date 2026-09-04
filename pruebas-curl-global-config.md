# Curls para probar Configuración Global — cisco01 / huawei01

Todas las escrituras son asíncronas (devuelven `202` + `group_job_id`). Después de
cada una podés pollear el resultado con la sección "Polling de jobs" al final
(mismo mecanismo que ports/svis).

Cada endpoint de esta guía tiene un curl para `cisco01` **y** uno para `huawei01`.
Los valores de prueba (hostname, community, servers, ruta) fueron los mismos
usados para verificar esto en vivo esta sesión — quedan libres/no pisan nada
real, y cada bloque de escritura tiene su cleanup inmediatamente debajo.

> ⚠️ A diferencia de ports/svis, acá no hay identidad de sub-recurso (no hay
> `interface`/`vlan_id` en el body) — 1 fila de config por device. Los campos
> de solo-lectura con nombre `snmp_version`/`ntp_server`/`dns_server`/
> `log_server`/`log_level` casi siempre viajan en `null` en el `GET` aunque
> los hayas configurado: la lectura de esos campos no está implementada
> todavía, solo la escritura (mismo gap documentado en el código). Community
> SNMP en Huawei tampoco se puede leer de vuelta (VRP la guarda cifrada).
> `running_config` sí es 100% real — dump completo de `show running-config`/
> `display current-configuration`.

## 0. Setup

```bash
BASE="http://localhost:8000/api/v1"

TOKEN=$(curl -sS -X POST "$BASE/auth/login" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=admin&password=dev_bootstrap_password_123" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

AUTH=(-H "Authorization: Bearer $TOKEN")

CISCO=f3r9s1
HUAWEI=f3r9s2
```

Repetí el bloque de login cuando el access token expire (dura poco).

> Escrituras de Configuración Global piden rol `admin` o superior (más
> estricto que `operator`, que alcanza para ports/vlans/svis) — el usuario
> `admin` del bootstrap ya lo cumple. Las lecturas (`GET /`, `/arp`, `/mac`)
> piden `observer`.

---

## 1. Lectura (RF-GLOBAL-01/02/03/04)

### 1.1 Ver config global (GET, cache-first)

```bash
curl -sS "${AUTH[@]}" "$BASE/devices/$CISCO/global-config/" | python3 -m json.tool
curl -sS "${AUTH[@]}" "$BASE/devices/$HUAWEI/global-config/" | python3 -m json.tool
```

`running_config` en la respuesta es el dump completo de `show
running-config`/`display current-configuration` — esto es lo que RF-GLOBAL-01
pedía ("consultar configuración general").

### 1.2 Refrescar cache (POST, async → 202)

```bash
curl -sS "${AUTH[@]}" -X POST "$BASE/devices/$CISCO/global-config/refresh"
curl -sS "${AUTH[@]}" -X POST "$BASE/devices/$HUAWEI/global-config/refresh"
```

---

## 2. Hostname (RF-GLOBAL-08)

> Repetir el mismo `PATCH` con el hostname ya vigente es no-op
> (`{"changed":false,"noop":true}`).

```bash
curl -sS "${AUTH[@]}" -X PATCH "$BASE/devices/$CISCO/global-config/hostname" \
  -H "Content-Type: application/json" \
  -d '{"hostname":"cisco01-test"}'

curl -sS "${AUTH[@]}" -X PATCH "$BASE/devices/$HUAWEI/global-config/hostname" \
  -H "Content-Type: application/json" \
  -d '{"hostname":"huawei01-test"}'
```

```bash
# volver al original
curl -sS "${AUTH[@]}" -X PATCH "$BASE/devices/$CISCO/global-config/hostname" \
  -H "Content-Type: application/json" \
  -d '{"hostname":"Switch"}'

curl -sS "${AUTH[@]}" -X PATCH "$BASE/devices/$HUAWEI/global-config/hostname" \
  -H "Content-Type: application/json" \
  -d '{"hostname":"HUAWEI"}'
```

---

## 3. SNMP (RF-GLOBAL-07)

> `community` siempre se fija de solo lectura — ya no hay parámetro
> `permission`. `trap_source`/`trap_host` son la interfaz de administración y
> el destino de traps respectivamente (config real: `snmp-server trap-source
> Vlan156` + `snmp-server host 172.19.19.46 version 2c unyberzydadez`).
> `trap_host` necesita una community resuelta — si no la mandás en la misma
> request, usa la que ya esté configurada en el device.
>
> **Huawei**: `trap_source` confirmado (`snmp-agent trap source {interfaz}`).
> `trap_host` **todavía no está soportado** — el comando real
> (`snmp-agent target-host host-name ... trap address udp-domain ... params
> securityname ... v2c`) supera los ~100 caracteres y se corrompe en tránsito
> sobre esta sesión SSH (confirmado en vivo). El endpoint acepta el campo
> pero el job termina en `failed` con el motivo — no manda un comando roto.

```bash
# community (siempre RO)
curl -sS "${AUTH[@]}" -X PATCH "$BASE/devices/$CISCO/global-config/snmp" \
  -H "Content-Type: application/json" \
  -d '{"community":"testpub123"}'

curl -sS "${AUTH[@]}" -X PATCH "$BASE/devices/$HUAWEI/global-config/snmp" \
  -H "Content-Type: application/json" \
  -d '{"version":"v2c","community":"testpub123"}'
```

```bash
# trap_source (idempotente en cisco01 si ya coincide con la interfaz real)
curl -sS "${AUTH[@]}" -X PATCH "$BASE/devices/$CISCO/global-config/snmp" \
  -H "Content-Type: application/json" \
  -d '{"trap_source":"Vlan10"}'

curl -sS "${AUTH[@]}" -X PATCH "$BASE/devices/$HUAWEI/global-config/snmp" \
  -H "Content-Type: application/json" \
  -d '{"trap_source":"Vlanif10"}'
```

```bash
# trap_host (Cisco only por ahora — usa la community ya configurada si no la mandás)
curl -sS "${AUTH[@]}" -X PATCH "$BASE/devices/$CISCO/global-config/snmp" \
  -H "Content-Type: application/json" \
  -d '{"trap_host":"172.16.61.230","trap_version":"2c"}'
```

```bash
# cleanup
docker compose exec -T backend python3 -c "
from app.core.config import DATABASE_URL
from app.db.session import init_db
init_db(DATABASE_URL)
from app.core.scope import require_device
d = require_device('cisco01')
d.driver._aplicar({'lines': ['no snmp-server community testpub123 RO', 'no snmp-server trap-source Vlan10', 'no snmp-server host 172.16.61.230 version 2c testpub123']}, d, d.password, op_label='cleanup')
"

docker compose exec -T backend python3 -c "
from app.core.config import DATABASE_URL
from app.db.session import init_db
init_db(DATABASE_URL)
from app.core.scope import require_device
d = require_device('huawei01')
d.driver._aplicar({'command_block': 'system-view\nundo snmp-agent trap source\nundo snmp-agent\ncommit\nquit'}, d, d.password, op_label='cleanup')
"
```

---

## 4. NTP (RF-GLOBAL-09, endpoint propio)

> `prefer` es opcional, solo tiene efecto confirmado en Cisco (`ntp server
> {ip} prefer`). Sin no-op detection (no hay lectura de NTP servers
> implementada) — el comando se manda siempre.

```bash
# agregar
curl -sS "${AUTH[@]}" -X POST "$BASE/devices/$CISCO/global-config/ntp" \
  -H "Content-Type: application/json" \
  -d '{"server":"10.10.10.99","prefer":true}'

curl -sS "${AUTH[@]}" -X POST "$BASE/devices/$HUAWEI/global-config/ntp" \
  -H "Content-Type: application/json" \
  -d '{"server":"10.10.10.99"}'

# sacar
curl -sS "${AUTH[@]}" -X DELETE "$BASE/devices/$CISCO/global-config/ntp" \
  -H "Content-Type: application/json" \
  -d '{"server":"10.10.10.99"}'

curl -sS "${AUTH[@]}" -X DELETE "$BASE/devices/$HUAWEI/global-config/ntp" \
  -H "Content-Type: application/json" \
  -d '{"server":"10.10.10.99"}'
```

---

## 5. DNS (RF-GLOBAL-09, endpoint propio)

> `POST` es sparse: exactamente 1 de `server` (agrega, incremental) o
> `domain_name` (setea, reemplaza el anterior) — 422 si vienen los 2 o
> ninguno. `DELETE` solo saca servers (no hay "clear domain_name" todavía).

```bash
# agregar server
curl -sS "${AUTH[@]}" -X POST "$BASE/devices/$CISCO/global-config/dns" \
  -H "Content-Type: application/json" \
  -d '{"server":"10.10.10.98"}'

curl -sS "${AUTH[@]}" -X POST "$BASE/devices/$HUAWEI/global-config/dns" \
  -H "Content-Type: application/json" \
  -d '{"server":"10.10.10.98"}'

# setear domain-name
curl -sS "${AUTH[@]}" -X POST "$BASE/devices/$CISCO/global-config/dns" \
  -H "Content-Type: application/json" \
  -d '{"domain_name":"lab.ansiauth.local"}'

curl -sS "${AUTH[@]}" -X POST "$BASE/devices/$HUAWEI/global-config/dns" \
  -H "Content-Type: application/json" \
  -d '{"domain_name":"lab.ansiauth.local"}'

# sacar server
curl -sS "${AUTH[@]}" -X DELETE "$BASE/devices/$CISCO/global-config/dns" \
  -H "Content-Type: application/json" \
  -d '{"server":"10.10.10.98"}'

curl -sS "${AUTH[@]}" -X DELETE "$BASE/devices/$HUAWEI/global-config/dns" \
  -H "Content-Type: application/json" \
  -d '{"server":"10.10.10.98"}'
```

```bash
# cleanup domain-name (Cisco normaliza "ip domain-name" a "ip domain name")
docker compose exec -T backend python3 -c "
from app.core.config import DATABASE_URL
from app.db.session import init_db
init_db(DATABASE_URL)
from app.core.scope import require_device
d = require_device('cisco01')
d.driver._aplicar({'lines': ['no ip domain name lab.ansiauth.local']}, d, d.password, op_label='cleanup')
"

docker compose exec -T backend python3 -c "
from app.core.config import DATABASE_URL
from app.db.session import init_db
init_db(DATABASE_URL)
from app.core.scope import require_device
d = require_device('huawei01')
d.driver._aplicar({'command_block': 'system-view\nundo dns domain lab.ansiauth.local\ncommit\nquit'}, d, d.password, op_label='cleanup')
"
```

---

## 6. Log servers (RF-GLOBAL-09, endpoint propio)

> `level` es opcional y es un ajuste **global** del device en los 2 vendors
> (no por-host) — viaja en la misma request por conveniencia. En Huawei el
> nivel va por un mecanismo separado de canales VRP (`info-center source
> default channel 2 log level {level}`, canal 2 = "loghost", nombre fijo
> estándar — confirmado con `display channel`), no en la misma línea del
> loghost (esa forma existe en algunos VRP pero otros la rechazan). Sin no-op
> detection — no está claro si el device soporta más de 1 loghost, se deja
> que el error real del device aparezca en el job si lo hay.

```bash
# agregar (con nivel)
curl -sS "${AUTH[@]}" -X POST "$BASE/devices/$CISCO/global-config/log-servers" \
  -H "Content-Type: application/json" \
  -d '{"server":"10.10.10.97","level":"warnings"}'

curl -sS "${AUTH[@]}" -X POST "$BASE/devices/$HUAWEI/global-config/log-servers" \
  -H "Content-Type: application/json" \
  -d '{"server":"10.10.10.97","level":"warning"}'

# sacar
curl -sS "${AUTH[@]}" -X DELETE "$BASE/devices/$CISCO/global-config/log-servers" \
  -H "Content-Type: application/json" \
  -d '{"server":"10.10.10.97"}'

curl -sS "${AUTH[@]}" -X DELETE "$BASE/devices/$HUAWEI/global-config/log-servers" \
  -H "Content-Type: application/json" \
  -d '{"server":"10.10.10.97"}'
```

```bash
# cleanup (Cisco: logging trap queda en el nivel pedido, volver a informational)
docker compose exec -T backend python3 -c "
from app.core.config import DATABASE_URL
from app.db.session import init_db
init_db(DATABASE_URL)
from app.core.scope import require_device
d = require_device('cisco01')
d.driver._aplicar({'lines': ['logging trap informational']}, d, d.password, op_label='cleanup')
"
```

---

## 7. Rutas estáticas (RF-GLOBAL-06)

> `destination` acepta un host dentro de la red (se normaliza a la dirección
> de red antes de comparar/aplicar). Si el destino ya existe con el MISMO
> next-hop, es no-op. Si ya existe con un next-hop DISTINTO, no se
> sobreescribe — devuelve `accion:"ruta_ya_existe"` sin tocar el device (así
> pide el SRS: "Alternative course: Ruta ya existente, se notifica al
> usuario"). `DELETE` usa el mismo body shape que `POST`.
>
> ⚠️ Límite conocido: la detección de "ya existe" lee `show ip route`/
> `display ip routing-table` (la RIB), no el config crudo — una ruta con
> next-hop no alcanzable en la red real queda en el config pero nunca se
> instala en la RIB, así que `POST`/`DELETE` no la ven (reportan no-op
> aunque la línea sí esté en el device). No pasa con next-hops reales.

```bash
curl -sS "${AUTH[@]}" -X POST "$BASE/devices/$CISCO/global-config/routes" \
  -H "Content-Type: application/json" \
  -d '{"destination":"192.168.99.0/24","next_hop":"10.10.10.99"}'

curl -sS "${AUTH[@]}" -X POST "$BASE/devices/$HUAWEI/global-config/routes" \
  -H "Content-Type: application/json" \
  -d '{"destination":"192.168.99.0/24","next_hop":"10.10.10.99"}'
```

```bash
# repetir la misma → no-op
curl -sS "${AUTH[@]}" -X POST "$BASE/devices/$CISCO/global-config/routes" \
  -H "Content-Type: application/json" \
  -d '{"destination":"192.168.99.0/24","next_hop":"10.10.10.99"}'

# mismo destino, next-hop distinto → "ruta_ya_existe", no aplica nada
curl -sS "${AUTH[@]}" -X POST "$BASE/devices/$CISCO/global-config/routes" \
  -H "Content-Type: application/json" \
  -d '{"destination":"192.168.99.0/24","next_hop":"10.10.10.88"}'
```

```bash
# borrar (mismo body que el POST)
curl -sS "${AUTH[@]}" -X DELETE "$BASE/devices/$CISCO/global-config/routes" \
  -H "Content-Type: application/json" \
  -d '{"destination":"192.168.99.0/24","next_hop":"10.10.10.99"}'

curl -sS "${AUTH[@]}" -X DELETE "$BASE/devices/$HUAWEI/global-config/routes" \
  -H "Content-Type: application/json" \
  -d '{"destination":"192.168.99.0/24","next_hop":"10.10.10.99"}'
```

---

## 8. ACLs (RF-GLOBAL-05)

**Pausado por ahora** — no hay endpoint todavía (queda como pendiente
explícito del plan, el SRS no detalla el shape de una regla de ACL y se
decidió no adivinar el alcance).

---

## 9. Tabla ARP y tabla MAC (fuera de RF-GLOBAL-01..09, pedido nuevo)

> Lectura **en vivo, sin cache** (a diferencia de todo el resto de esta
> guía) — la tabla ARP/MAC cambia todo el tiempo, cachearla la volvería
> vieja al instante. `include` es opcional y se pasa tal cual al `| include`
> del propio device — vos sabés qué buscar, la respuesta no se parsea.
> Charset permitido en `include`: alfanumérico + `:./_-`, sin espacios ni
> pipes ni saltos de línea (422 si no cumple — es lo que viaja dentro de una
> línea de comando sobre la sesión SSH, no se puede dejar pasar cualquier
> cosa). Pide rol `observer` (no `admin`, es solo lectura).

```bash
# tabla completa
curl -sS "${AUTH[@]}" "$BASE/devices/$CISCO/global-config/arp"
curl -sS "${AUTH[@]}" "$BASE/devices/$HUAWEI/global-config/arp"

curl -sS "${AUTH[@]}" "$BASE/devices/$CISCO/global-config/mac"
curl -sS "${AUTH[@]}" "$BASE/devices/$HUAWEI/global-config/mac"
```

```bash
# filtrada (ejemplo: por IP/MAC/interfaz — ajustá el valor a algo que exista)
curl -sS "${AUTH[@]}" "$BASE/devices/$CISCO/global-config/arp?include=Vlan156"
curl -sS "${AUTH[@]}" "$BASE/devices/$HUAWEI/global-config/mac?include=GE0/0/48"
```

```bash
# include inválido → 422, nunca llega al device
curl -sS -o /dev/null -w "%{http_code}\n" "${AUTH[@]}" "$BASE/devices/$CISCO/global-config/arp?include=foo%0Ashow%20running-config"
```

---

## 10. Polling de jobs (todas las escrituras son async)

Cada respuesta de escritura tiene esta forma:

```json
{"success":true,"data":{"group_job_id":"...","jobs":[{"job_id":"...","device":"cisco01"}]}}
```

```bash
# job individual
curl -sS "${AUTH[@]}" "$BASE/jobs/JOB_ID" | python3 -m json.tool

# resumen del grupo (todos los devices del mismo job)
curl -sS "${AUTH[@]}" "$BASE/group-jobs/GROUP_JOB_ID" | python3 -m json.tool
```

Ejemplo encadenado (dispara y pollea en un solo bloque):

```bash
RESP=$(curl -sS "${AUTH[@]}" -X PATCH "$BASE/devices/$CISCO/global-config/hostname" \
  -H "Content-Type: application/json" \
  -d '{"hostname":"cisco01-test"}')

JOB_ID=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['jobs'][0]['job_id'])")

sleep 2
curl -sS "${AUTH[@]}" "$BASE/jobs/$JOB_ID" | python3 -m json.tool
```

---

## 11. Equipos reales (f3r9s1 / f3r9s2) — cuidado

Los mismos endpoints funcionan contra `f3r9s1`/`f3r9s2`, pero son equipos
reales en producción, no de lab:

- **No pruebes rutas/SNMP/NTP/DNS/log-servers apuntando a `0.0.0.0/0` o a
  valores que ya estén en uso** (community real, ACL real, etc.) — usá
  siempre un destino/valor descartable que no pise nada existente, y limpiá
  apenas termines.
- `f3r9s2` (Huawei S-series real) no soporta `commit` — el driver ya maneja
  esto con reintento automático, no hace falta nada especial del lado del
  curl. Tampoco acepta el comando de `trap_host` de SNMP (fuera de alcance
  por el problema de corrupción de línea larga, ver sección 3).
- `f3r9s1` ya tiene SNMP/NTP/DNS/logging configurados de antes — para probar
  el camino "ya existe"/idempotente sin tocar nada real, mandá el mismo
  valor que ya está configurado (podés verlo con `GET .../global-config/`,
  el campo `running_config`, o por CLI) en vez de uno inventado.
- ARP/MAC son de solo lectura, sin riesgo — se pueden probar libremente
  contra los 4 devices.
