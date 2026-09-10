import ipaddress
import logging
import re
import time
from dataclasses import asdict, is_dataclass

from app.core.exceptions import DeviceExecutionError, NotFoundError
from app.models.domain_event import DomainEvent
from app.models.retry_decision import RetryDecision

logger = logging.getLogger(__name__)

# retry_policy.py:15-61, verbatim -- absorbido acá como constantes de módulo.
# Permanent patterns are checked FIRST -- a device that says "authentication
# failure: connection timeout" should never be retried.
#
# Cada patrón va acompañado de una `categoria` corta -- no cambia la lógica
# de reintento (sigue siendo exactamente la misma tabla, mismo orden, mismos
# strings), solo etiqueta QUÉ TIPO de problema es cada uno. Usado por
# Orquestador._resumir_error() para traducir un RetryDecision a una frase
# legible en Job.error_summary -- ver plan de esta sesión ("Clasificar y
# explicar mejor los errores de device"). Al mergear ``fix/batch-rollback``:
# esa rama tenía la misma tabla pero como tuplas de solo texto (sin
# categoria, calculaba su propio resumen aparte) -- se adoptó la forma
# tupla-de-tuplas (usada por el resto de este módulo) y se le sumaron sus
# 6 patrones de "business errors" nuevos, cada uno con su categoria.
_PATRONES_PERMANENTES: tuple[tuple[str, str], ...] = (
    ("invalid vlan id", "syntax"),
    ("incomplete command", "syntax"),
    ("syntax error", "syntax"),
    ("vlan already exists", "conflict"),
    # Rechazo de IP/subred en conflicto con otra VLAN -- bug real
    # encontrado en vivo: "% 192.168.121.0 overlaps with Vlan222" (Cisco)
    # no matcheaba NINGÚN patrón permanente, así que cuando el mismo texto
    # combinado también traía "closed by remote host" (la sesión se cierra
    # después del rechazo, mismo patrón ya visto con storm-control), caía
    # en _PATRONES_TRANSITORIOS y se clasificaba "connectivity" -- un
    # conflicto de IP real y permanente, mostrado como si fuera un
    # problema de red transitorio. "is assigned to" cubre la otra frase
    # ya vista en vivo esta sesión (Cisco también, forma distinta del
    # mismo rechazo). "conflicts with" cubre una 3ra variante encontrada
    # en vivo en Huawei (SVI): "Error: The specified address conflicts
    # with another address." -- mismo tipo de rechazo (IP ya asignada a
    # otra interfaz), texto totalmente distinto de las otras 2 formas.
    ("overlaps with", "conflict"),
    ("is assigned to", "conflict"),
    ("conflicts with", "conflict"),
    ("permission denied", "auth"),
    ("authentication failure", "auth"),
    ("authentication failed", "auth"),
    ("unsupported command", "syntax"),
    # Huawei VRP's exact wording ("Error: Unrecognized command found at
    # '^' position.") -- bug real encontrado en vivo: distinta de
    # "unsupported command" (que sí estaba en la tabla), así que caía al
    # catch-all "unknown" pese a ser un rechazo de sintaxis clarísimo.
    ("unrecognized command", "syntax"),
    ("invalid input", "syntax"),
    ("invalid command", "syntax"),
    ("authorization failed", "auth"),
    ("access denied", "auth"),
    ("ambiguous command", "syntax"),
    ("bad command", "syntax"),
    ("error: invalid", "syntax"),
    # Business errors -- el equipo rechaza la operación con un motivo
    # concreto (no es de red, no es de sintaxis, no es de auth). Son
    # permanentes en el sentido "reintentar con los mismos parámetros
    # va a fallar igual" -- para que se corrija el usuario tiene que
    # cambiar el input. Caso real que motivó esto: usuario intentó
    # asignar ``port default vlan 45`` sobre un puerto en Huawei
    # cuando la VLAN 45 no estaba creada -- device respondió con
    # "Error: The VLAN does not exist" y sin este patrón caía a
    # "unknown" (que agrega 1 retry extra al pedo, pierde 60s en
    # sesiones SSH que van a fallar igual).
    #
    # Patrones acotados a propósito -- "not found" y "cannot be" son
    # tentadores pero matchean tanto contra "Interface not found"
    # (permanente) como contra "host not found" (transitorio DNS) o
    # "device cannot be reached" (transitorio red). Preferimos
    # false-negativos (que caiga a "unknown", 1 retry) antes que
    # false-positivos (marcar permanente y no reintentar un error
    # que sí era transitorio).
    ("does not exist", "not_found"),    # Huawei: "The VLAN does not exist"; Cisco: "VLAN X does not exist"
    ("already in use", "conflict"),     # Cisco: "% Interface ... already in use"
    ("already configured", "conflict"),
    ("is not valid", "syntax"),         # "IP address is not valid", etc.
    ("out of range", "syntax"),         # rango numérico rechazado (VLAN, threshold, etc.)
    ("in use by", "conflict"),          # "VLAN in use by port ..."
    # Tipos de excepción de auth de Paramiko/Netmiko -- llegan como prefijo
    # del str(exc) (ver _ejecutar_con_retry). Los ponemos como permanentes
    # antes de _PATRONES_TRANSITORIOS por defensa en profundidad: aunque el
    # mensaje diga "connection timeout" (que matchearía transitorio), si el
    # tipo dice AuthenticationException el problema real es de credenciales
    # y reintentar sólo empeora la situación (ej. lockout tras N intentos).
    ("authenticationexception", "auth"),
    ("badauthenticationtype", "auth"),
    ("partialauthentication", "auth"),
)

# Ruido benigno que conviene descartar ANTES de clasificar -- avisos o
# artefactos que terminan en el mismo transcript que clasificamos pero que
# NO son un rechazo real del device, y que sin filtrar le ganan el match a
# un patrón real (permanente o transitorio) por aparecer antes/matchear
# primero. Cada entrada documenta el caso real que la motivó.
_PATRONES_RUIDO_BENIGNO: tuple[re.Pattern, ...] = (
    # 1) Bug real encontrado en vivo: al asignar ``switchport access vlan
    # 1050`` sobre una VLAN inexistente, Cisco IOS no rechaza nada -- la
    # crea sola y lo avisa con "% Access VLAN does not exist. Creating
    # vlan 1050" (rc=0, el comando se aplicó). Ese texto contiene el
    # patrón permanente "does not exist" (pensado para el rechazo DURO de
    # Huawei, "Error: The VLAN does not exist"), así que cuando el mismo
    # transcript también traía un problema real de conexión ("Connection
    # ... closed by remote host", transitorio) la nota benigna de Cisco
    # ganaba el match -- el job se clasificaba "permanent" y se le hacía
    # ROLLBACK a un cambio que en realidad SÍ se había aplicado en el
    # device.
    re.compile(r"%\s*access vlan does not exist\.\s*creating vlan\s*\d*", re.IGNORECASE),
    # 2) Bug real encontrado en vivo: ``set_access_mode`` de Huawei (ver
    # commands.yaml) manda una "y" fija después de "port link-type
    # access" para responder al prompt "Continue?[Y/N]" que VRP muestra
    # SOLO cuando el puerto venía de trunk con VLANs asignadas (si no se
    # responde, el resto del bloque -- "port default vlan"/"commit" -- ni
    # se manda). Cuando el puerto NO tenía nada que perder ese prompt no
    # aparece, y la "y" se manda como comando suelto -- VRP la rechaza con
    # "Unrecognized command" (inofensivo, el resto del bloque se sigue
    # aplicando bien) pero ese texto matcheaba el patrón permanente
    # "unrecognized command" y fallaba el job entero por un artefacto de
    # NUESTRO propio placeholder, no un rechazo real del device.
    re.compile(r"\]y\r?\n\s*\^\r?\nError: Unrecognized command found at '\^' position\.", re.IGNORECASE),
)


def _limpiar_ruido_benigno(texto: str) -> str:
    for patron in _PATRONES_RUIDO_BENIGNO:
        texto = patron.sub("", texto)
    return texto

_PATRONES_TRANSITORIOS: tuple[tuple[str, str], ...] = (
    # Spec-required exact phrases
    ("ssh timeout", "connectivity"),
    ("connection timeout", "connectivity"),
    ("socket timeout", "connectivity"),
    ("temporary unreachable", "connectivity"),
    ("ssh connection failed", "connectivity"),
    ("network_cli timeout", "connectivity"),
    ("session reset", "connectivity"),
    ("connection reset", "connectivity"),
    ("eof during transport", "connectivity"),
    ("command timeout", "connectivity"),
    ("ansible persistent connection timeout", "connectivity"),
    # Broader patterns that map to the same transient class
    ("timeout", "connectivity"),
    ("timed out", "connectivity"),
    ("connection refused", "connectivity"),
    ("unable to connect", "connectivity"),
    ("ssh failure", "connectivity"),
    ("ssh error", "connectivity"),
    ("ssh connect", "connectivity"),
    ("network is unreachable", "connectivity"),
    ("no route to host", "connectivity"),
    ("broken pipe", "connectivity"),
    ("host unreachable", "connectivity"),
    ("transport endpoint", "connectivity"),
    ("reset by peer", "connectivity"),
    # ssh_direct_service.py (devices en auth_method="key"): mensaje real de
    # OpenSSH cuando VRP/IOS cortan la conexión -- confirmado en vivo esta
    # sesión que pasa tanto por un cierre benigno de fin de sesión (ya
    # filtrado antes de llegar acá por _sesion_completa()) como por un
    # corte genuino a mitad de comando, intermitente, sin patrón claro --
    # sin este pattern caía en "unknown" y solo tenía 1 reintento con delay
    # fijo de 1s en vez del backoff exponencial completo.
    ("closed by remote host", "connectivity"),
    ("end of file", "connectivity"),
    # SSH session exhaustion -- el caso concreto que motivó esta ronda de
    # reliability: el device sólo permite N sesiones SSH concurrentes
    # (típicamente 5-16 en Cisco/Huawei) y cuando N sesiones ya están
    # tomadas (por nosotros mismos, monitoreo, o alguien conectado por
    # consola), el N+1 rebota con un mensaje que ninguno de los patterns
    # de arriba matcheaba -- caía al default "permanent" de la línea 304
    # y el job moría de un tiro. Todos estos son transitorios de verdad:
    # esperar unos segundos hasta que otra sesión se cierre y reintentar
    # es exactamente lo correcto.
    ("unable to open channel", "session_limit"),
    ("channel is not open", "session_limit"),
    ("channel closed", "session_limit"),
    ("session limit", "session_limit"),
    ("max allowed sessions", "session_limit"),
    ("too many sessions", "session_limit"),
    ("ssh_msg_channel_open_failure", "session_limit"),
    ("administratively prohibited", "session_limit"),
    ("resource temporarily unavailable", "session_limit"),
    # Códigos numéricos de errno crudos (sin nombre simbólico) que llegan
    # cuando str(exc) es un OSError sin decorar. Los 4 más comunes en
    # SSH transitorio: ETIMEDOUT=110, ECONNRESET=104, ECONNREFUSED=111,
    # EHOSTUNREACH=113, EAGAIN=11.
    ("[errno 11]", "connectivity"),
    ("[errno 104]", "connectivity"),
    ("[errno 110]", "connectivity"),
    ("[errno 111]", "connectivity"),
    ("[errno 113]", "connectivity"),
    # Otras variantes de red/handshake que ya vimos escapar en producción
    # sin matchear ningún pattern anterior.
    ("remote host closed", "connectivity"),
    ("connection aborted", "connectivity"),
    ("handshake", "connectivity"),
    ("keepalive", "connectivity"),
    ("no existing session", "connectivity"),
    ("failed to connect", "connectivity"),
    ("unable to establish", "connectivity"),
    # Nombres de tipo de excepción de Python/Paramiko/Netmiko -- prefijados
    # al str(exc) por _ejecutar_con_retry (ver el except del retry loop).
    # Muchas de estas excepciones traen str(exc) vacío o críptico y el
    # tipo es la única señal léxica disponible. Los tipos de auth
    # exception NO van acá -- son permanentes (ver _PATRONES_PERMANENTES).
    ("sshexception", "connectivity"),
    ("timeouterror", "connectivity"),
    ("connectionreseterror", "connectivity"),
    ("connectionrefusederror", "connectivity"),
    ("connectionabortederror", "connectivity"),
    ("eoferror", "connectivity"),
    ("netmikotimeoutexception", "connectivity"),
    ("socket.timeout", "connectivity"),
    ("socket.gaierror", "connectivity"),
    ("ssl.sslerror", "connectivity"),
    # ansible_service.py's rc=0-but-stdout-is-literally-"None" guard --
    # ver esa nota, mismo criterio: no es un rechazo real del device, es
    # una lectura que no se pudo confiar, vale la pena reintentar.
    ("possible read desync", "read_reliability"),
)

# vlan_execution_service.py:15 -- Ansible exits with rc=4 when hosts are
# unreachable, rc=6 when unreachable+failed. rc=255 covers ansible-runner
# connection errors. Always transient at the Ansible level regardless of text.
_RC_TRANSITORIOS: frozenset = frozenset({4, 6, 255})

_MAX_RETRY_DELAY: float = 5.0


# Traducción de los patrones que ya matchea ``_clasificar_error`` a un
# mensaje corto en INGLÉS pensado para render directo en UI (a diferencia
# del ``error`` crudo, que trae stack trace/bytes del device y no se
# entiende sin leer todo). Diccionario de fallback: cada entrada mapea 1+
# patrones de ``_PATRONES_PERMANENTES``/``_PATRONES_TRANSITORIOS`` (o 1
# rc de ``_RC_TRANSITORIOS``) al mensaje. El match es "primer patrón que
# aparezca en el texto", igual que ``_clasificar_error`` -- si el error
# no cae en ningún grupo, se devuelve un genérico honesto en vez de un
# mensaje inventado.
#
# Los grupos se mantienen chicos a propósito -- no hay 1 traducción por
# patrón porque muchos son sinónimos que colapsan al mismo mensaje UX
# (ej. "invalid input" == "unsupported command" == "unknown command" son
# todos "command not supported"). Si aparece un caso nuevo que merece su
# propio mensaje, se agrega acá + se documenta la razón en el commit --
# no se inventan traducciones para patrones que todavía no aparecieron
# en producción.
_MENSAJES_ERROR: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("invalid input", "invalid command", "unsupported command",
         "unknown command", "bad command", "error: invalid"),
        "Command not supported by this device (or the feature is missing on this model).",
    ),
    (
        ("syntax error", "incomplete command", "ambiguous command"),
        "Malformed command generated by the platform — please report this error.",
    ),
    (
        ("authentication failed", "authentication failure",
         "authenticationexception", "badauthenticationtype", "partialauthentication"),
        "Authentication failed against the device — check the stored credentials.",
    ),
    (
        ("permission denied", "access denied", "authorization failed"),
        "Device rejected the operation (insufficient privileges for this user).",
    ),
    (
        ("vlan already exists",),
        "VLAN already exists on the device.",
    ),
    (
        ("invalid vlan id",),
        "Invalid VLAN ID.",
    ),
    (
        # Muy común: usuario intenta asignar a un puerto una VLAN que
        # todavía no fue creada en el device. Mensaje concreto para que
        # sepa qué corregir (crear la VLAN, o pedir una que ya exista).
        ("does not exist",),
        "Referenced object does not exist on the device (e.g. VLAN not created, ACL not defined).",
    ),
    (
        ("already in use", "already configured", "in use by"),
        "The target is already in use or configured -- change is redundant or conflicts with existing config.",
    ),
    (
        ("is not valid", "out of range"),
        "The value is out of the range accepted by the device (invalid ID, threshold, IP, etc.).",
    ),
    (
        ("unable to open channel", "channel is not open", "channel closed",
         "session limit", "max allowed sessions", "too many sessions",
         "ssh_msg_channel_open_failure", "administratively prohibited",
         "resource temporarily unavailable"),
        "Device SSH session limit reached — will retry in a few seconds.",
    ),
    (
        ("timeout", "timed out", "command timeout",
         "ssh timeout", "connection timeout", "socket timeout",
         "network_cli timeout", "ansible persistent connection timeout",
         "netmikotimeoutexception", "timeouterror"),
        "Timeout while communicating with the device.",
    ),
    (
        ("connection refused", "unable to connect", "failed to connect",
         "unable to establish", "no route to host", "network is unreachable",
         "host unreachable", "temporary unreachable",
         "connectionrefusederror", "no existing session"),
        "Device unreachable on the network.",
    ),
    (
        ("connection reset", "reset by peer", "broken pipe",
         "closed by remote host", "remote host closed", "end of file",
         "eof during transport", "session reset", "ssh connection failed",
         "ssh failure", "ssh error", "ssh connect", "connection aborted",
         "handshake", "keepalive", "transport endpoint",
         "connectionreseterror", "connectionabortederror", "eoferror",
         "sshexception", "socket.timeout", "socket.gaierror", "ssl.sslerror",
         "possible read desync"),
        "Connection dropped by the device (transient).",
    ),
    (
        # errno numéricos crudos (mismo que _PATRONES_TRANSITORIOS) --
        # OSError sin decorar. Mensaje genérico de red porque el errno
        # solo se sabe de red, no de qué específicamente falló.
        ("[errno 11]", "[errno 104]", "[errno 110]", "[errno 111]", "[errno 113]"),
        "Network error while reaching the device.",
    ),
)


def _pre_state_json_safe(pre_state: dict) -> dict:
    """Return a JSON-serializable copy of a ``reconciliar()`` result.

    ``VLAN.reconciliar()`` already returns plain JSON-safe values.
    ``Puerto.reconciliar()`` returns ``{"actual": Puerto | None, ...}`` --
    a raw dataclass instance would blow up ``Job.pre_state``'s JSON column
    on write."""
    return {k: (asdict(v) if is_dataclass(v) else v) for k, v in pre_state.items()}


# Traduce la `categoria` de un RetryDecision (o, si no hay categoria, su
# `classification`) a una frase corta en inglés, legible por un humano --
# usada para poblar Job.error_summary. No traduce el vocabulario completo
# de mensajes de vendor (eso sigue viviendo en _PATRONES_*), solo agrupa
# las ~6 categorias ya asignadas ahí arriba. Cuando `categoria` es None
# (el branch de rc de Ansible, o el catch-all "unknown" sin match de
# texto) cae al fallback genérico por `classification`.
_RESUMENES_POR_CATEGORIA: dict[str, str] = {
    "auth": "Authentication or permission problem — check the device credentials.",
    "syntax": "The device rejected the command as invalid or unsupported.",
    "conflict": "The requested change conflicts with the device's current configuration.",
    "connectivity": "Network or SSH connectivity issue reaching the device.",
    "session_limit": "The device has no free SSH sessions available right now.",
    "read_reliability": "The device's response couldn't be read reliably.",
}


def _mensaje_error(resultado: dict, fallback: str) -> str:
    """Arma el mensaje de ``DeviceExecutionError`` (-> ``Job.error``) a
    partir de un resultado de ejecución. Bug real encontrado esta sesión:
    los 4 call-sites usaban ``stderr or stdout``, mostrando SOLO stderr
    cuando venía no-vacío -- pero ``_clasificar_error()`` (ver abajo)
    clasifica sobre stderr+stdout concatenados, así que un rechazo real
    del device (ej. "% Invalid input detected...") que llegó por stdout
    quedaba invisible en pantalla cada vez que stderr también traía algo
    (típicamente un mensaje de conexión tipo "closed by remote host" de
    un corte de sesión posterior al rechazo) -- el usuario veía "se cortó
    la conexión" mientras `error_summary` (que sí ve el texto completo)
    correctamente decía "el device rechazó el comando", sin forma de
    reconciliar ambos mensajes. Ahora se muestran los dos cuando difieren."""
    stderr = (resultado.get("stderr") or "").strip()
    stdout = (resultado.get("stdout") or "").strip()
    if stderr and stdout and stderr != stdout:
        return f"{stderr}\n{stdout}"
    return stderr or stdout or fallback


def _resumir_error(decision: "RetryDecision") -> str:
    if decision.categoria in _RESUMENES_POR_CATEGORIA:
        return _RESUMENES_POR_CATEGORIA[decision.categoria]
    if decision.classification == "transient":
        return "A transient connectivity issue occurred and retries were exhausted."
    if decision.classification == "permanent":
        return "The device rejected the operation."
    return "The device gave an unfamiliar response — could not determine the exact cause."


class Orquestador:
    """FINAL_ARCHITECTURE.md §2.4 -- Template Method por composición, no
    herencia. No sabe qué es una VLAN ni un Puerto, solo el contrato
    RecursoGestionable (4 métodos). Único camino real -- ``ejecutar_comando()``
    (Fase 7, variante para "guardar configuración" sin RecursoGestionable
    detrás) se retiró en esta sesión junto con ``POST /devices/{name}/save``
    (``api/devices.py``) y ``app.tasks.guardar_config_task`` -- la feature
    queda inactiva hasta que se implemente como RecursoGestionable de
    verdad (recurso de config modular, a diseñar más adelante), en vez de
    mantener un 2do camino especial mientras tanto."""

    def __init__(self, device_repo, repos: dict, jobs, eventos, coordinador):
        self._device_repo = device_repo    # Repository[Device] o DeviceRepository, Fase 1/3
        self._repos = repos                # dict[str, Repository] -- "vlan": vlan_repository, "puerto": puerto_repository (Fase 2)
        self._jobs = jobs                  # JobRepository, Fase 4
        self._eventos = eventos            # EventDispatcher, Fase 3
        self._coordinador = coordinador    # RedisCoordinator, Fase 1 -- ver FASE_5.md A3 (bloquear())
        # y FASE_7.md §6 (limitar(), agregado ahí -- mismo tipo de gap que
        # bloquear() ya tuvo: diseñado en Fase 1, nunca conectado a ejecutar()).

    def ejecutar(self, recurso: "RecursoGestionable", device_name: str, actor: str, job: "Job") -> None:
        if job.esta_en_estado_terminal():
            return
        device = self._device_repo.get(device_name)
        if device is None:
            raise NotFoundError(device_name)
        # Corrección real encontrada revisando FINAL_ARCHITECTURE.md de punta a
        # punta: nada en todo el pipeline (GroupOperationRunner.encolar() ->
        # JobQueue.dispatch() -> ejecutar_task -> acá) asignaba `recurso.device`
        # -- las rutas construyen VLAN/Puerto sin `device` (queda "", su
        # default) y eso es lo que `self._repos[...].add(recurso)` persistía
        # más abajo. Como VLAN/Puerto tienen PK compuesta (vlan_id, device) /
        # (interface, device), toda VLAN/Puerto con el mismo vlan_id/interface
        # en CUALQUIER device colapsaba en una sola fila con device="" --
        # confirmado con un test real (2 devices, mismo vlan_id, encolar()
        # una sola vez: vlan_repository.list() devolvía 1 fila, no 2). Las
        # llamadas reales al driver (`reconciliar()`/`aplicar()`, abajo) no
        # se veían afectadas -- reciben `device` como objeto aparte, no leen
        # `recurso.device` -- pero la tabla de tracking quedaba corrupta. El
        # propio docstring de `VLAN.device` ya decía "quien la reparte se lo
        # asigna (Fase 5, GroupOperationRunner/Orquestador)" -- nunca se
        # escribió el código que lo hiciera.
        recurso.device = device_name

        pre_state = None
        # Movidos al scope del método para que la rama de reporte (outer
        # except) los vea sin importar en qué punto se rompió. Antes se
        # calculaban dentro del except y perdían el resultado del rollback
        # si el evento fallaba y volvía a caer por acá.
        rb_performed, rb_success = (False, None)
        try:
            job.marcar_iniciado()
            self._jobs.add(job)
            with self._coordinador.bloquear(device_name):
                self._coordinador.limitar(device_name)
                recurso.validar()
                # reconciliar() abre SSH contra el device para leer el estado
                # actual (get_vlans/list_ports/show run) -- si el equipo tiene
                # las sesiones SSH agotadas o el show tarda de más, este
                # llamado tira excepción y (antes de esta corrección) el job
                # moría al primer intento sin pasar por ningún retry,
                # aunque la causa fuera claramente transitoria. Envolverlo
                # con _ejecutar_con_retry le da las mismas 3 chances que
                # _aplicar(). Wrapper devuelve rc=0 en éxito y guarda el
                # pre_state real en el holder de closure -- así el retry loop
                # (que sólo entiende dicts con rc) trabaja igual que con
                # aplicar(), y nosotros recuperamos el pre_state acá abajo.
                pre_state_holder: dict = {}

                def _reconciliar():
                    pre_state_holder["value"] = recurso.reconciliar(device)
                    return {"rc": 0, "stdout": "", "stderr": ""}

                resultado_prestate, _ = self._ejecutar_con_retry(
                    _reconciliar, job, device_name, max_retries=job.max_retries,
                )
                if resultado_prestate.get("rc", 0) != 0:
                    decision = self._clasificar_error(resultado_prestate)
                    raise DeviceExecutionError(
                        _mensaje_error(resultado_prestate, "Prestate read failed"),
                        resumen=_resumir_error(decision),
                    )
                pre_state = pre_state_holder["value"]
                # Bug real encontrado en una revisión de código: pre_state
                # se calculaba acá y se usaba para el rollback, pero nunca
                # se escribía de vuelta al Job -- GET /jobs/{id} siempre
                # devolvía pre_state=null, contradiciendo la propia
                # descripción del endpoint ("Pre-state is captured for
                # rollback"). _pre_state_json_safe() reemplaza cualquier
                # dataclass anidado (Puerto.reconciliar() devuelve
                # {"actual": Puerto(...)}, no serializable tal cual a JSON)
                # por su asdict() -- VLAN.reconciliar() ya es JSON-safe de
                # por sí, la conversión ahí es un no-op.
                job.pre_state = _pre_state_json_safe(pre_state)

                primer_intento = True

                def _aplicar():
                    # El pre_state de acá arriba solo es seguro reusarlo en
                    # el primer intento -- corrección real: antes aplicar()
                    # releía el estado del device en CADA intento, incluido
                    # el primero, donde nada pudo haber cambiado todavía. A
                    # partir del 2do intento sí hay que releer de verdad: un
                    # intento previo puede haber tirado timeout (transitorio)
                    # pero haberse aplicado igual en el device, y reusar el
                    # pre_state viejo llevaría a reintentar un create sobre
                    # algo que ya existe.
                    nonlocal primer_intento
                    if primer_intento:
                        primer_intento = False
                        return recurso.aplicar(device, pre_state=pre_state)
                    return recurso.aplicar(device)

                try:
                    resultado, retry_count = self._ejecutar_con_retry(
                        _aplicar, job, device_name, max_retries=job.max_retries,
                    )
                    if resultado.get("rc", 0) != 0:
                        decision = self._clasificar_error(resultado)
                        raise DeviceExecutionError(
                            _mensaje_error(resultado, "Execution failed"),
                            resumen=_resumir_error(decision),
                        )
                except Exception:
                    # Rollback DENTRO del ``with bloquear()`` -- corrección
                    # real: el bloque anterior soltaba el lock al salir del
                    # with por la excepción, y el except lo re-adquiría.
                    # Entre medio otro job podía tocar el device y hacer
                    # que el pre_state que capturamos ya no fuera vigente
                    # cuando ejecutáramos el revert. Ahora el rollback
                    # comparte el mismo lock que tomó el apply -- 0 race,
                    # y también 0 espera por otros jobs para arrancar el
                    # revert. Budget corto (max_retries=1) para no bloquear
                    # a otros más tiempo del necesario -- el device ya está
                    # en estado inconsistente. La semántica de _rollback()
                    # se preserva: (performed, success), guardado por
                    # closure.
                    if pre_state is not None:
                        rb_state = {"performed": False, "success": None}

                        def _hacer_rollback():
                            rb_p, rb_s = self._rollback(recurso, pre_state, device)
                            rb_state["performed"] = rb_p
                            rb_state["success"] = rb_s
                            # No-op (nada que revertir) o éxito verificado
                            # -> rc=0, así no gastamos el retry. Sólo
                            # (True, False) -- se intentó revertir pero la
                            # verificación falló -- se trata como
                            # retryable.
                            if not rb_p:
                                return {"rc": 0, "stdout": "rollback no-op", "stderr": ""}
                            if rb_s is False:
                                return {"rc": 1, "stdout": "", "stderr": "rollback verification failed"}
                            return {"rc": 0, "stdout": "rollback ok", "stderr": ""}

                        self._ejecutar_con_retry(
                            _hacer_rollback, job, device_name, max_retries=1,
                        )
                        rb_performed = rb_state["performed"]
                        rb_success = rb_state["success"]
                    raise
        except Exception as error:
            # Clasificación amigable del error final: se calcula UNA vez
            # acá y se propaga a) al Job persistido (para que GET
            # /jobs/{id} devuelva `error_type`/`error_reason`/
            # `error_summary` sin re-derivar) y b) al evento de auditoría
            # (para que el frontend renderice el mensaje corto sin tener
            # que parsear el `error` crudo con bytes del device). Ver
            # ``_error_amigable()`` y ``_MENSAJES_ERROR``.
            friendly = self._error_amigable(error)
            job.marcar_fallido(str(error), rb_performed, rb_success, **friendly)
            self._jobs.add(job)
            self._eventos.despachar([DomainEvent(
                "recurso_fallido", recurso, device, actor,
                {
                    "error": str(error),
                    "rollback_performed": rb_performed,
                    "rollback_success": rb_success,
                    **friendly,
                },
                exitoso=False,
            )])
            raise
        else:
            # Bug real de producción encontrado con un log real (creación de
            # VLAN sobre un device real, Postgres): self._repos[...].add()
            # tiraba psycopg.errors.UndefinedTable porque device_vlans no
            # tenía migración Alembic (arreglado en i3msp8_device_vlans_ports).
            # Pero la causa de fondo era más grave que la tabla faltante: acá
            # abajo estaba `self._repos[...].add(recurso)` sin try propio,
            # así que esa excepción escapaba de este `else:` SIN pasar por el
            # `except Exception as error:` de arriba -- Python no cubre el
            # cuerpo de `else:` con el `except` del mismo try/except/else, son
            # 2 cosas distintas. Resultado real: nada de rollback, nada de
            # `job.marcar_fallido()`, nada de evento de auditoría de fallo --
            # el job quedaba "running" hasta que el `finally` de abajo lo
            # cerraba con el genérico "Unexpected termination", perdiendo el
            # error real. Encapsular acá para que un fallo de bookkeeping
            # (tracking row / evento) no tire por la borda un cambio que sí
            # se aplicó de verdad en el device -- a diferencia de una falla
            # real de Ansible, ACÁ no corresponde rollback (deshacer un
            # cambio exitoso en el device por un problema de guardado local
            # sería peor que el problema original).
            #
            # Bug real encontrado por el usuario: escribió NTP en Huawei
            # (POST /global-config/ntp) y el siguiente GET /global-config
            # devolvió TODO null (hostname, snmp, rutas, acls -- no solo
            # ntp). Causa: para VLAN/Puerto/Svi, `recurso` es la
            # representación completa del ítem que se acaba de tocar (ej.
            # VLAN(vlan_id, device, name) YA es todo lo que hay que
            # cachear para esa VLAN), así que escribirlo acá directo es
            # correcto. GlobalConfig es un singleton por device que
            # representa MUCHOS campos a la vez (hostname/snmp/ntp/dns/
            # rutas/acls...), pero el objeto que llega a esta escritura
            # solo tiene seteado el campo que se está escribiendo (ej.
            # `ntp_server_add`) -- todos los demás quedan en su default
            # de dataclass (``None``). `Repository.add()` hace
            # ``session.merge()`` del objeto ENTERO, así que mandarlo acá
            # tal cual pisa la fila cacheada completa con nulls, salvo el
            # campo que se acaba de escribir (que ni siquiera es una
            # columna de lectura -- ``ntp_server_add`` no es
            # ``ntp_servers``). El sync post-write de abajo es lo que
            # repuebla la fila con el estado real leído del device -- acá
            # no hay nada útil que trackear mientras tanto, al revés que
            # VLAN/Puerto/SVI.
            if recurso.repositorio() != "global_config":
                try:
                    self._repos[recurso.repositorio()].add(recurso)
                except Exception:
                    logger.exception(
                        "Tracking write failed for %s on device=%s after the device "
                        "change already succeeded -- NOT rolled back (the change is "
                        "real and wanted), only the local tracking row failed to save.",
                        recurso.repositorio(), device_name,
                    )
            job.marcar_completado(resultado)
            self._jobs.add(job)
            try:
                self._eventos.despachar([DomainEvent("recurso_aplicado", recurso, device, actor, resultado)])
            except Exception:
                logger.exception(
                    "Failed to dispatch recurso_aplicado event for %s on device=%s -- "
                    "audit trail for this successful change may be incomplete.",
                    recurso.repositorio(), device_name,
                )
            # Cache-first coherence: el ``_repos[...].add(recurso)`` de arriba
            # ya actualizó la fila puntual del recurso tocado, pero el
            # ``synced_at`` del device no cambió y cualquier drift real del
            # equipo (ej. otra VLAN aparecida por consola directa) sigue sin
            # detectarse hasta el próximo refresh manual. Un sync full del
            # scope tocado resuelve las dos cosas. Fire-and-forget: si el
            # broker no está, la escritura ya fue exitosa, sólo perdemos la
            # actualización proactiva de cache -- el usuario puede darle
            # refresh manual desde POST /devices/{name}/{vlans,ports}/refresh.
            # "global_config" faltaba acá -- por eso el bug de arriba dejaba
            # la fila en null "para siempre" (hasta un refresh manual) en vez
            # de autocorregirse en unos segundos como el resto: sin esta
            # entrada, ninguna escritura de Configuración Global disparaba el
            # sync post-write que repuebla la fila con el estado real.
            _SCOPE_POR_REPO = {"vlan": "vlans", "puerto": "ports", "svi": "svis", "global_config": "global_config"}
            sync_scope = _SCOPE_POR_REPO.get(recurso.repositorio())
            if sync_scope is not None:
                # Coalesce del sync post-write cuando llega un burst de N jobs
                # sobre el mismo (device, scope): sólo el último dispara el
                # refresh, los previos se saltean. Los jobs sobre el mismo
                # device se serializan por el lock Redis, así que "el último"
                # está bien definido -- job.marcar_completado() de arriba ya
                # sacó al actual del set pending/running.
                if self._jobs.hay_otros_activos(device_name, recurso.repositorio(), job.job_id):
                    logger.debug(
                        "Orquestador.ejecutar: skipping post-write sync for "
                        "device=%s scope=%s -- another job on same (device, scope) "
                        "is still active; the last one of the burst will refresh",
                        device_name, sync_scope,
                    )
                else:
                    try:
                        from app.tasks import sync_device_task
                        sync_device_task.delay(device_name, sync_scope)
                    except Exception as exc:
                        logger.warning(
                            "Orquestador.ejecutar: post-write sync enqueue failed for "
                            "device=%s scope=%s (%s) -- cache will stay at previous "
                            "synced_at until a manual refresh",
                            device_name, sync_scope, exc,
                        )
        finally:
            if not job.esta_en_estado_terminal():
                job.asegurar_estado_final()
                self._jobs.add(job)

    def ejecutar_lote(self, recursos: "list[RecursoGestionable]", device_name: str, actor: str, job: "Job") -> None:
        """Batching -- N recursos del MISMO tipo sobre el MISMO device en
        **1 sola conexión** en vez de N (ver ``VendorDriver.aplicar_lote()``
        y ``RecursoGestionable.resolver_paso()``). No reemplaza
        ``ejecutar()`` (que sigue siendo el camino de 1 recurso = 1
        conexión), es un método nuevo -- mismo lock/retry/rollback/eventos/
        sync post-write que ``ejecutar()``, adaptados a una lista.

        Sin escritura de tracking por-recurso (``_repos[...].add()``) a
        propósito: cada entrada del lote es una representación PARCIAL (ej.
        un ``Puerto(interface=X, admin_up=True)`` con todo lo demás en
        ``None``) -- escribirla pisaría la fila cacheada completa con
        nulls, mismo bug ya documentado más arriba para ``GlobalConfig``.
        El sync post-write (al final, sin cambios) es lo que repuebla la
        fila con el estado real leído del device."""
        if job.esta_en_estado_terminal():
            return
        device = self._device_repo.get(device_name)
        if device is None:
            raise NotFoundError(device_name)
        for recurso in recursos:
            recurso.device = device_name
        tipo = recursos[0].repositorio()

        pre_states: "list[dict] | None" = None
        # Movidos al scope del método -- mismo motivo que en ``ejecutar()``:
        # la rama de reporte final (outer except) los tiene disponibles
        # sin importar dónde se rompió, y no dependen de que el bloque
        # interno haya llegado a definirlos.
        rb_performed_total = False
        rb_success_total: "bool | None" = None
        rb_error: "str | None" = None  # mensaje crudo del fallo del rollback (device o verify)
        try:
            job.marcar_iniciado()
            self._jobs.add(job)
            with self._coordinador.bloquear(device_name):
                self._coordinador.limitar(device_name)
                for recurso in recursos:
                    recurso.validar()

                pre_states_holder: dict = {}

                def _reconciliar_lote():
                    cls = type(recursos[0])
                    pre_states_holder["value"] = cls.reconciliar_lote(recursos, device)
                    return {"rc": 0, "stdout": "", "stderr": ""}

                resultado_prestate, _ = self._ejecutar_con_retry(
                    _reconciliar_lote, job, device_name, max_retries=job.max_retries,
                )
                if resultado_prestate.get("rc", 0) != 0:
                    decision = self._clasificar_error(resultado_prestate)
                    raise DeviceExecutionError(
                        _mensaje_error(resultado_prestate, "Prestate read failed"),
                        resumen=_resumir_error(decision),
                    )
                pre_states = pre_states_holder["value"]
                job.pre_state = {"lote": [_pre_state_json_safe(ps) for ps in pre_states]}

                primer_intento = True

                def _aplicar():
                    nonlocal primer_intento
                    if primer_intento:
                        primer_intento = False
                        estados = pre_states
                    else:
                        # Mismo criterio que ejecutar(): a partir del 2do
                        # intento releer de verdad (un intento previo puede
                        # haberse aplicado igual en el device pese al
                        # timeout).
                        estados = type(recursos[0]).reconciliar_lote(recursos, device)
                    # Hook opcional (``Puerto`` no lo implementa, no lo
                    # necesita): deja que el tipo de recurso corrija el
                    # "actual" de cada entrada con lo que OTRO recurso del
                    # MISMO lote está por fijar -- ver
                    # ``SVI.ajustar_estados_lote()`` (bug real: validar
                    # dhcp_relay_add/ipv4_address_secondary contra el
                    # estado leído ANTES del batch, en vez de contra lo que
                    # el batch mismo está por aplicar).
                    ajustar = getattr(type(recursos[0]), "ajustar_estados_lote", None)
                    if ajustar is not None:
                        estados = ajustar(recursos, estados)
                    pasos = []
                    for recurso, estado in zip(recursos, estados):
                        paso = recurso.resolver_paso(device, estado.get("actual"))
                        if paso is not None:
                            pasos.append(paso)
                    if not pasos:
                        return {"rc": 0, "success": True, "changed": False, "noop": True, "stdout": "", "stderr": ""}
                    return device.driver.aplicar_lote(pasos, device, device.password, op_label=f"lote_{tipo}")

                try:
                    resultado, retry_count = self._ejecutar_con_retry(
                        _aplicar, job, device_name, max_retries=job.max_retries,
                    )
                    if resultado.get("rc", 0) != 0:
                        decision = self._clasificar_error(resultado)
                        raise DeviceExecutionError(
                            _mensaje_error(resultado, "Execution failed"),
                            resumen=_resumir_error(decision),
                        )
                except Exception:
                    # Rollback DENTRO del ``with bloquear()`` -- mismo
                    # criterio que en ``ejecutar()``: 0 race con otro job
                    # que pueda tocar el device mientras soltamos y re-
                    # adquirimos el lock, 0 espera para arrancar el revert.
                    #
                    # Batched rollback: ``_rollback_lote()`` computa TODOS
                    # los pasos de reversión en memoria, los concatena y
                    # los manda al device en 1 sola llamada a
                    # ``driver.aplicar_lote()`` (vs N llamadas del camino
                    # viejo, 1 por recurso), y verifica con 1 sola
                    # ``reconciliar_lote()`` (vs N ``list_ports()``/
                    # ``get_svis()``). Total pasa de ~2N conexiones SSH a
                    # 2 (o 3 para SVI que necesita 1 read extra para 3
                    # campos, ver docstring). Wrap con retry corto igual
                    # que antes -- si el timeout te pega en medio del
                    # apply batched del rollback, un 2do intento
                    # (idempotente, mismos pasos) suele funcionar.
                    rb_resultados: list[tuple[bool, "bool | None"]] = []
                    if pre_states is not None:
                        rb_holder: dict = {"resultados": [], "error": None}

                        def _hacer_rollback_lote():
                            resultados, error_rb_local = self._rollback_lote(
                                recursos, pre_states, device,
                            )
                            rb_holder["resultados"] = resultados
                            rb_holder["error"] = error_rb_local
                            # Retryable si CUALQUIER recurso reportó
                            # verificación fallida -- el reintento vuelve
                            # a mandar el batch entero (idempotente en
                            # cada paso). Si el problema es permanente el
                            # 2do intento falla igual y salimos.
                            if any(s is False for _p, s in resultados):
                                return {
                                    "rc": 1, "stdout": "",
                                    "stderr": error_rb_local or "rollback verification failed for one or more entries",
                                }
                            return {"rc": 0, "stdout": "rollback ok", "stderr": ""}

                        self._ejecutar_con_retry(
                            _hacer_rollback_lote, job, device_name, max_retries=1,
                        )
                        rb_resultados = rb_holder["resultados"]
                        rb_error = rb_holder["error"]
                    rb_performed_total = any(p for p, _s in rb_resultados)
                    rb_success_total = (
                        all(s is not False for _p, s in rb_resultados if _p) if rb_resultados else None
                    )
                    raise
        except Exception as error:
            friendly = self._error_amigable(error)
            # Si además falló el rollback, clasificamos ESE error aparte
            # para que el usuario tenga por qué falló cada cosa. La
            # traducción amigable del rollback usa la misma tabla que la
            # del apply -- misma clase de errores esperada (device
            # rechaza comando, timeout, verify mismatch...).
            rb_friendly: dict = {}
            if rb_error:
                clasif_rb = self._error_amigable(rb_error)
                rb_friendly = {
                    "rollback_error": rb_error,
                    "rollback_error_type": clasif_rb["error_type"],
                    "rollback_error_summary": clasif_rb["error_summary"],
                }
            job.marcar_fallido(str(error), rb_performed_total, rb_success_total, **friendly)
            # ``rollback_error`` no forma parte de ``marcar_fallido`` (es
            # una nueva columna adicional del Job) -- se setea directo
            # abajo para no crecer la firma pública del método.
            if rb_error:
                job.rollback_error = rb_error
            self._jobs.add(job)
            self._eventos.despachar([DomainEvent(
                "recurso_fallido", recursos[0], device, actor,
                {
                    "error": str(error),
                    "rollback_performed": rb_performed_total,
                    "rollback_success": rb_success_total,
                    "lote_size": len(recursos),
                    **friendly,
                    **rb_friendly,
                },
                exitoso=False,
            )])
            raise
        else:
            job.marcar_completado(resultado)
            self._jobs.add(job)
            try:
                self._eventos.despachar([DomainEvent(
                    "recurso_aplicado", recursos[0], device, actor,
                    {
                        **resultado, "lote_size": len(recursos),
                        "resumen_lote": "; ".join(r.resumen_intento() for r in recursos),
                    },
                )])
            except Exception:
                logger.exception(
                    "Failed to dispatch recurso_aplicado event for lote on device=%s -- "
                    "audit trail for this successful change may be incomplete.",
                    device_name,
                )
            _SCOPE_POR_REPO = {"vlan": "vlans", "puerto": "ports", "svi": "svis", "global_config": "global_config"}
            sync_scope = _SCOPE_POR_REPO.get(tipo)
            if sync_scope is not None:
                if self._jobs.hay_otros_activos(device_name, tipo, job.job_id):
                    logger.debug(
                        "Orquestador.ejecutar_lote: skipping post-write sync for "
                        "device=%s scope=%s -- another job on same (device, scope) "
                        "is still active", device_name, sync_scope,
                    )
                else:
                    try:
                        from app.tasks import sync_device_task
                        sync_device_task.delay(device_name, sync_scope)
                    except Exception as exc:
                        logger.warning(
                            "Orquestador.ejecutar_lote: post-write sync enqueue failed for "
                            "device=%s scope=%s (%s)", device_name, sync_scope, exc,
                        )
        finally:
            if not job.esta_en_estado_terminal():
                job.asegurar_estado_final()
                self._jobs.add(job)

    def _error_amigable(self, error: "str | Exception") -> dict:
        """Convierte un error final (str o Exception) en 3 campos para
        persistir en el Job y para mandar en el evento de auditoría:
        ``error_type`` (permanent/transient/unknown), ``error_reason``
        (patrón crudo que matcheó, útil para logs/tuning) y
        ``error_summary`` (mensaje corto en inglés listo para render).
        Sinónimo de ``_clasificar_error`` para la parte de clasificación
        + lookup contra ``_MENSAJES_ERROR`` para el mensaje amigable.
        Prefixo por tipo de excepción (mismo criterio que
        ``_ejecutar_con_retry``) cuando llega un Exception, así los
        patrones "sshexception"/"timeouterror"/etc. matchean también
        cuando ``str(exc)`` viene vacío o críptico."""
        if isinstance(error, Exception):
            tipo = type(error).__name__
            mensaje = str(error) or "<no message>"
            errno_val = getattr(error, "errno", None)
            errno_prefix = f"[Errno {errno_val}] " if isinstance(errno_val, int) else ""
            texto = f"{tipo}: {errno_prefix}{mensaje}"
        else:
            texto = error or ""
        decision = self._clasificar_error({"rc": 1, "stdout": "", "stderr": texto})
        # Mismo filtro de ruido benigno que ``_clasificar_error`` -- ese
        # método lo aplica sobre su propia copia interna de ``combinado``,
        # no sobre este ``texto``, así que sin repetirlo acá el summary
        # podía seguir mostrando "Referenced object does not exist..."
        # para un texto que ``decision`` ya clasificó bien como transient.
        lowered = _limpiar_ruido_benigno(texto).lower()
        summary = None
        for patrones, mensaje in _MENSAJES_ERROR:
            if any(p in lowered for p in patrones):
                summary = mensaje
                break
        if summary is None:
            # Bug real encontrado en vivo: un patrón de _PATRONES_PERMANENTES/
            # _PATRONES_TRANSITORIOS puede matchear (dándole a `decision` una
            # `categoria` real, ej. "conflict") sin que ese mismo texto
            # matchee TAMBIÉN algún grupo de _MENSAJES_ERROR (tabla
            # independiente, mantenida a mano aparte) -- caso real: "Error:
            # The specified address conflicts with another address."
            # (Huawei SVI) clasificaba bien como permanent/conflict pero
            # caía al genérico "Unclassified..." en vez de usar esa
            # categoria. _resumir_error() ya sabe resolver por categoria
            # antes de caer a un genérico por classification -- se reusa
            # acá en vez de duplicar la lógica.
            summary = _resumir_error(decision)
        return {
            "error_type": decision.classification,
            "error_reason": decision.reason,
            "error_summary": summary,
        }

    def _clasificar_error(self, resultado: dict) -> RetryDecision:
        """vlan_execution_service.py: _classify_result() -- el chequeo de rc
        va primero, después delega a las tablas de patrones de texto."""
        if resultado.get("rc") in _RC_TRANSITORIOS:
            return RetryDecision(
                should_retry=True, classification="transient",
                reason=f"ansible rc={resultado.get('rc')}",
            )
        combinado = (resultado.get("stderr") or "") + " " + (resultado.get("stdout") or "")
        combinado = _limpiar_ruido_benigno(combinado)
        lowered = combinado.lower()
        for patron, categoria in _PATRONES_PERMANENTES:
            if patron in lowered:
                return RetryDecision(should_retry=False, classification="permanent", reason=patron, categoria=categoria)
        for patron, categoria in _PATRONES_TRANSITORIOS:
            if patron in lowered:
                return RetryDecision(should_retry=True, classification="transient", reason=patron, categoria=categoria)
        # Sin match en ninguna tabla -- antes se trataba como permanente y
        # se mataba el job de un tiro. Cambio: se da UNA sola chance extra
        # (override=2 => 1 initial + 1 retry), independiente de max_retries.
        # Motivación: muchos errores transitorios de red/SSH no matchean
        # ningún pattern conocido (mensajes crípticos, str(exc) vacío) --
        # gastar 1 retry adicional es barato y evita fallar por prudencia.
        # Si el error es genuinamente permanente, el 2do intento falla igual
        # con el mismo mensaje y el job termina como failed.
        return RetryDecision(
            should_retry=True, classification="unknown",
            reason="unknown error — one cautious retry",
            max_attempts_override=2,
        )

    def _ejecutar_con_retry(
        self, fn, job: "Job", device: str, max_retries: int = 3, retry_base_delay: float = 1.0,
    ) -> tuple[dict, int]:
        """vlan_execution_service.py: _execute_with_retry() -- llama fn()
        hasta max_retries+1 veces, nunca relanza (normaliza cualquier
        excepción a rc=1). Persiste el progreso en cada reintento vía
        job.registrar_reintento() -- corrección real encontrada en esta
        fase: el ejemplo canónico calculaba retry_count pero nunca lo
        guardaba en el Job, perdiendo retry_count/last_error que
        api/jobs.py sí expone hoy (ver nota en app/models/job.py)."""
        resultado: dict = {"rc": 1, "stdout": "", "stderr": ""}
        retry_count = 0
        for intento in range(max_retries + 1):
            try:
                resultado = fn()
            except Exception as exc:
                # Prefijamos con el tipo (SSHException, TimeoutError,
                # ConnectionResetError, etc.) porque muchas excepciones de
                # Paramiko/Netmiko/socket llegan con str(exc)="" o mensajes
                # crípticos donde el tipo es la única señal léxica. También
                # embebemos el errno numérico (ETIMEDOUT=110, ECONNRESET=104,
                # etc.) para que los patterns [errno N] de _PATRONES_TRANSITORIOS
                # matcheen aun cuando el mensaje del OSError no lo trae
                # renderizado.
                tipo = type(exc).__name__
                mensaje = str(exc) or "<no message>"
                errno_prefix = ""
                errno_val = getattr(exc, "errno", None)
                if isinstance(errno_val, int):
                    errno_prefix = f"[Errno {errno_val}] "
                resultado = {"rc": 1, "stdout": "", "stderr": f"{tipo}: {errno_prefix}{mensaje}"}
            if resultado.get("rc") == 0:
                break
            decision = self._clasificar_error(resultado)
            error_texto = ((resultado.get("stderr") or "") + " " + (resultado.get("stdout") or "")).strip()
            error_short = error_texto[:200]
            # Tope efectivo: si la clasificación trae un override (típico de
            # "unknown" -- 1 sola chance extra), lo respetamos aun cuando el
            # max_retries global sea más alto. Traducción: max_attempts=2
            # significa "hasta 2 intentos totales" -> effective_max=1 retry
            # (además del intento inicial que ya se hizo).
            if decision.max_attempts_override is not None:
                effective_max = min(max_retries, decision.max_attempts_override - 1)
            else:
                effective_max = max_retries
            if not decision.should_retry:
                logger.warning(
                    "Orquestador retry: no reintenta job=%s device=%s attempt=%d classification=%s reason=%s error=%r",
                    job.job_id, device, intento + 1,
                    decision.classification, decision.reason, error_short,
                )
                break
            if intento >= effective_max:
                logger.warning(
                    "Orquestador retry: agotado job=%s device=%s attempts=%d classification=%s reason=%s error=%r",
                    job.job_id, device, intento + 1,
                    decision.classification, decision.reason, error_short,
                )
                break
            delay = min(retry_base_delay * (2 ** intento), _MAX_RETRY_DELAY)
            retry_count += 1
            logger.info(
                "Orquestador retry: reintentando job=%s device=%s attempt=%d/%d classification=%s reason=%s delay=%.2fs error=%r",
                job.job_id, device, intento + 1, effective_max + 1,
                decision.classification, decision.reason, delay, error_short,
            )
            job.registrar_reintento(error_texto)
            self._jobs.add(job)
            time.sleep(delay)
        return resultado, retry_count

    def _rollback_lote(
        self, recursos: "list[RecursoGestionable]", pre_states: "list[dict]", device: "Device",
    ) -> "tuple[list[tuple[bool, bool | None]], str | None]":
        """Batched rollback -- reversa del ``aplicar_lote()`` del apply.

        Reemplaza el loop per-recurso que abría 2 conexiones SSH por cada
        entrada revertida (1 write + 1 verify). Ahora:
        1. Se computa TODO el "qué revertir" en memoria vía
           ``recurso.resolver_rollback()`` (que devuelve ``(pasos,
           verificar)``, sin tocar el device -- polimórfico, mismo
           criterio que ``resolver_paso()`` para el apply; ``Orquestador``
           no sabe qué es un ``Puerto`` ni una ``SVI``, ver
           ``RecursoGestionable.resolver_rollback``).
        2. Se manda 1 sola ``aplicar_lote()`` con todos los pasos
           concatenados -- 1 conexión SSH para escribir N reversiones.
        3. Se verifica con 1 sola ``reconciliar_lote()`` -- 1 conexión SSH
           adicional; cada verificador per-recurso corre contra su
           ``actual`` post-rollback ya distribuido en memoria.

        Total: 2 conexiones SSH (o 3 si el recurso declara
        ``NECESITA_ESTADO_FRESCO_ROLLBACK``, ver abajo) sin importar
        cuántos recursos tenga el lote, contra 2N del camino viejo.

        Semántica de retorno: tupla ``(resultados, error_rb)`` donde
        ``resultados`` es la lista ``[(performed, success)]`` per-recurso
        (mismo orden que *recursos*, mismo shape que devolvía el loop
        viejo) y ``error_rb`` es el mensaje crudo del fallo del rollback
        (o ``None`` si el rollback anduvo bien / no había nada que
        revertir). Ese ``error_rb`` es lo que permite al caller
        propagarle al usuario POR QUÉ falló el revert (feedback real
        del device / de nuestro reader), en vez de dejarlo con solo
        ``rollback_success=false`` sin explicación -- corrección real
        motivada por el caso: usuario vio "rollback_success: false" y
        no tenía forma de saber si el device rechazó un comando, si
        la verificación fue estricta de más, o si hubo un timeout."""
        if not recursos:
            return [], None
        tipo = recursos[0].repositorio()  # solo para logs/op_label, no para dispatch
        cls = type(recursos[0])

        # Fresh state post-apply -- solo lo pide el recurso que lo
        # necesita (SVI hoy, para ipv4_address_secondary/acl_in/acl_out),
        # sin que Orquestador tenga que preguntar el tipo (ver
        # NECESITA_ESTADO_FRESCO_ROLLBACK en RecursoGestionable).
        actuales_ahora: list = [None] * len(recursos)
        if getattr(cls, "NECESITA_ESTADO_FRESCO_ROLLBACK", False):
            try:
                estados_ahora = cls.reconciliar_lote(recursos, device)
                actuales_ahora = [e.get("actual") for e in estados_ahora]
            except Exception:
                logger.exception(
                    "_rollback_lote: fresh reconciliar_lote() falló para tipo=%s "
                    "device=%s -- degradando a rollback con actual_ahora=None para "
                    "todos los recursos (los campos que lo necesitan van a "
                    "devolver (False, None) en su plan).", tipo, device.name,
                )

        # Plan phase: pasos + verificar por recurso, SIN tocar el device.
        # ``resolver_rollback`` es un hook OPCIONAL del contrato (mismo
        # mecanismo que ``ajustar_estados_lote``) -- VLAN/GlobalConfig no
        # lo implementan (no batchean hoy, no hay endpoint de lote para
        # esos tipos), y sin él acá se tratan como no-op batched-side, el
        # camino single-recurso los cubre en el llamador de arriba.
        resolver_rollback = getattr(cls, "resolver_rollback", None)
        planes: "list[tuple[list, callable | None]]" = []
        for recurso, pre_state, actual_ahora in zip(recursos, pre_states, actuales_ahora):
            try:
                if resolver_rollback is None:
                    plan = ([], None)
                else:
                    plan = recurso.resolver_rollback(pre_state, device, actual_ahora)
            except Exception:
                logger.exception(
                    "_rollback_lote: resolver_rollback() tiró excepción para "
                    "tipo=%s device=%s -- marcando como (True, False) para no perder "
                    "la señal de que algo intentamos.", tipo, device.name,
                )
                plan = ([("__error__", None, {})], None)  # placeholder, no se ejecuta
            planes.append(plan)

        total_pasos = sum(len(pasos) for pasos, _ in planes)
        if total_pasos == 0:
            # Nada que revertir para ningún recurso.
            return [(False, None)] * len(planes), None

        # Batch apply: 1 sola llamada al driver con todos los pasos
        # concatenados. Se preserva el orden (pasos del recurso 0, después
        # del 1, etc.) por si algún driver depende del orden -- aplicar_lote
        # los manda tal cual en el loop de Ansible.
        pasos_all = [step for pasos, _ in planes for step in pasos]
        error_rb: "str | None" = None
        try:
            resultado_apply = device.driver.aplicar_lote(
                pasos_all, device, device.password, op_label=f"rollback_lote_{tipo}",
            )
            apply_ok = resultado_apply.get("rc", 1) == 0
            if not apply_ok:
                error_rb = (
                    (resultado_apply.get("stderr") or "")
                    + (" " if resultado_apply.get("stderr") and resultado_apply.get("stdout") else "")
                    + (resultado_apply.get("stdout") or "")
                ).strip() or "rollback apply failed with no error message"
        except Exception as exc:
            logger.exception(
                "_rollback_lote: aplicar_lote() del rollback tiró excepción para "
                "tipo=%s device=%s -- todos los recursos con pasos>0 van a "
                "reportarse como (True, False).", tipo, device.name,
            )
            apply_ok = False
            error_rb = f"{type(exc).__name__}: {exc or '<no message>'}"

        if not apply_ok:
            # Recursos con pasos>0: reportados como intentados-y-fallidos.
            # Recursos con pasos vacíos (reader gap): nunca formaron parte
            # del batch, se marcan como no-op (False, None) para no
            # contaminar la métrica agregada de "cuántos se intentaron".
            return (
                [(True, False) if pasos else (False, None) for pasos, _ in planes],
                error_rb,
            )

        # Verify phase: 1 sola ``reconciliar_lote()`` distribuida por recurso.
        try:
            estados_final = cls.reconciliar_lote(recursos, device)
        except Exception as exc:
            logger.exception(
                "_rollback_lote: reconciliar_lote() de verificación falló para "
                "tipo=%s device=%s -- driver reportó apply_ok=True, marcamos "
                "todos los recursos con pasos>0 como (True, False) por no "
                "poder verificar.", tipo, device.name,
            )
            return (
                [(True, False) if pasos else (False, None) for pasos, _ in planes],
                f"Verification read failed after rollback apply: {type(exc).__name__}: {exc or '<no message>'}",
            )

        resultados: "list[tuple[bool, bool | None]]" = []
        for (pasos, verificar), estado_final in zip(planes, estados_final):
            if not pasos:
                resultados.append((False, None))
                continue
            if verificar is None:
                # Sin verificador -- confiar en rc=0 del apply batch.
                resultados.append((True, True))
                continue
            actual_final = estado_final.get("actual") if isinstance(estado_final, dict) else None
            try:
                ok = verificar(actual_final)
            except Exception:
                ok = False
            resultados.append((True, bool(ok) if ok is not None else False))

        # Si alguna verificación per-recurso falló pese a apply_ok, dejamos
        # un mensaje explícito en error_rb -- el driver dijo OK pero el
        # relectura no coincidió, útil para diferenciar de "device rechazó
        # el comando".
        if any(s is False for _p, s in resultados):
            error_rb = "Rollback commands applied (rc=0) but per-resource verification did not match the pre-state -- device state may still be inconsistent."
        return resultados, error_rb

    def retry_rollback(
        self, original_job: "Job", new_job: "Job", actor: str,
    ) -> None:
        """Manual retry of a failed rollback -- reconstruye los pasos de
        reversión desde ``original_job.pre_state`` (ya persistido) y los
        manda batched al device. NO toca al ``original_job``; el
        ``new_job`` lleva su propio ciclo de vida completo.

        Feature de recovery para el caso: el rollback original falló
        (device inaccesible por segundos, sesión SSH cortada, etc.)
        pero el ``pre_state`` sigue guardado en la fila del job vieja.
        En vez de exigir al usuario que edite manualmente el device
        para llevarlo al estado previo (que además es la parte donde
        más fácil se equivoca), reusamos el snapshot para replicar el
        rollback tantas veces como haga falta.

        Solo soporta ``puerto`` y ``svi`` -- son los tipos con
        ``reconciliar_lote()`` y ``resolver_*`` en el driver. VLAN y
        GlobalConfig tienen su propio path de rollback per-operación y
        no fue nunca batched -- si hace falta acá se agrega, pero hoy
        no aplica.

        Semántica del ``new_job``:
        - ``parameters={"retry_of_job_id": <original>}`` -- linkeo
          buscable.
        - Si al terminar todo salió bien, ``marcar_completado``. Si
          alguno falla (device rechaza / timeout / verify mismatch),
          ``marcar_fallido`` con la misma clasificación amigable que
          usa ``ejecutar_lote``. NUNCA se dispara un "rollback del
          retry-rollback" -- si esto también falla es intervención
          manual, no otro nivel de auto-recovery."""
        from app.models.port import Puerto
        from app.models.svi import SVI

        _TIPO_CLS = {"puerto": Puerto, "svi": SVI}
        tipo = original_job.operation
        if tipo not in _TIPO_CLS:
            self._retry_rollback_fallar(
                new_job, actor, device=None, recurso_referencia=None,
                error=f"retry_rollback: unsupported operation type {tipo!r} "
                      f"(only 'puerto'/'svi' are supported)",
            )
            return

        if not original_job.pre_state:
            self._retry_rollback_fallar(
                new_job, actor, device=None, recurso_referencia=None,
                error="retry_rollback: original job has no pre_state to restore from",
            )
            return

        device = self._device_repo.get(original_job.device)
        if device is None:
            self._retry_rollback_fallar(
                new_job, actor, device=None, recurso_referencia=None,
                error=f"retry_rollback: device {original_job.device!r} no existe",
            )
            return

        cls = _TIPO_CLS[tipo]
        pre_state = original_job.pre_state
        entries = (
            pre_state["lote"]
            if isinstance(pre_state, dict) and isinstance(pre_state.get("lote"), list)
            else [pre_state]
        )

        # Deserializar snapshot per entry: pre_state["actual"] llegó como
        # dict (via _pre_state_json_safe/asdict al persistir). Saltea las
        # entries sin ``existed`` (nada que restaurar) y las que perdieron
        # el ``actual`` (job antiguo pre-Fase-5 que no lo capturaba).
        snapshots = []
        for entry in entries:
            if not entry.get("existed"):
                continue
            actual_dict = entry.get("actual")
            if not actual_dict:
                continue
            actual = cls.from_dict(actual_dict)
            actual.device = device.name
            snapshots.append(actual)

        if not snapshots:
            # Nada que restaurar (todo el pre_state indica "nada existía
            # antes"). No-op limpio -- new_job termina completado sin
            # tocar el device.
            new_job.marcar_iniciado()
            self._jobs.add(new_job)
            new_job.marcar_completado({
                "rc": 0, "success": True, "changed": False, "noop": True,
                "accion": "retry_rollback",
                "retry_of_job_id": original_job.job_id,
            })
            self._jobs.add(new_job)
            return

        try:
            new_job.marcar_iniciado()
            self._jobs.add(new_job)
            with self._coordinador.bloquear(device.name):
                self._coordinador.limitar(device.name)

                # Fresh state read ANTES de armar pasos. Doble propósito:
                # (a) SVI necesita el ``actual_ahora`` para 3 campos cuyos
                #     resolvers lo piden (mismo motivo que
                #     ``_rollback_lote``), y (b) tanto Port como SVI usan
                #     esta relectura para saltear pasos donde el device
                #     YA está en el valor del snapshot -- alguien pudo
                #     haber restaurado la config por consola / vía otro
                #     job / vía un retry-rollback previo entre que este
                #     job se creó y ahora. Sin este pre-flight
                #     mandaríamos pasos idempotentes al pedo, con su
                #     conexión SSH y su ruido en logs.
                #
                # Emparejado POSICIONAL con ``snapshots`` (``zip``), no por
                # un dict keyeado por interface/vlan_id -- mismo criterio
                # que ``_rollback_lote()``: ``reconciliar_lote()`` ya
                # garantiza devolver 1 entrada por recurso de entrada, EN
                # EL MISMO ORDEN (ver su propio docstring), así que no
                # hace falta que ``Orquestador`` sepa "cuál es la clave de
                # identidad de este tipo" (``vlan_id`` para SVI,
                # ``interface`` para Puerto) para reconstruir el mapeo.
                actuales_ahora: list = [None] * len(snapshots)
                try:
                    estados_ahora = cls.reconciliar_lote(snapshots, device)
                    actuales_ahora = [e.get("actual") for e in estados_ahora]
                except Exception:
                    logger.exception(
                        "retry_rollback: fresh reconciliar_lote() falló para tipo=%s "
                        "device=%s -- degradamos a plan sin filtrar (todos los pasos "
                        "se van a mandar, incluso los que ya coinciden con el device).",
                        tipo, device.name,
                    )

                # Construir (pasos, verificar) per snapshot -- polimórfico
                # (``recurso.resolver_restore()``, mismo criterio que
                # ``resolver_paso()``/``resolver_rollback()``) en vez de
                # ``if tipo == "puerto"``. Los planners saltean campos que
                # ya coinciden con actual_ahora, así el batch queda con
                # solo lo que hace falta cambiar. ``verificar`` se guarda
                # para la fase 2 (después del apply), mismo criterio que
                # ``_rollback_lote()``.
                planes = [snap.resolver_restore(device, actual_ahora) for snap, actual_ahora in zip(snapshots, actuales_ahora)]
                pasos_all = [step for pasos, _ in planes for step in pasos]

                if not pasos_all:
                    # Device ya está en pre_state (o alguien lo restauró
                    # por otra vía). Marcar el retry como completed-noop:
                    # legítimo éxito, no fallo -- el intento del usuario
                    # se cumplió (device está OK), aunque no hayamos
                    # abierto ninguna sesión de write.
                    resultado = {
                        "rc": 0, "success": True, "changed": False, "noop": True,
                        "accion": "retry_rollback",
                        "retry_of_job_id": original_job.job_id,
                        "reason": "device already at pre_state -- no restore steps needed",
                    }
                else:
                    def _hacer_retry_rollback():
                        return device.driver.aplicar_lote(
                            pasos_all, device, device.password,
                            op_label=f"retry_rollback_{tipo}",
                        )
                    resultado, _ = self._ejecutar_con_retry(
                        _hacer_retry_rollback, new_job, device.name,
                        max_retries=new_job.max_retries,
                    )
                    if resultado.get("rc", 0) != 0:
                        raise DeviceExecutionError(
                            resultado.get("stderr") or resultado.get("stdout")
                            or "retry_rollback apply failed"
                        )

                    # Verify phase -- mismo criterio en 2 fases que
                    # ``_rollback_lote()``: fase 1 (arriba) ya cortó si el
                    # apply mismo falló, sin intentar distinguir per-recurso
                    # (el transporte no da esa señal). Acá, con rc=0
                    # confirmado, se relee 1 vez batcheado y se verifica
                    # cada recurso con SU PROPIA closure -- reportar
                    # "completed" cuando el device no quedó igual al
                    # snapshot sería peor que fallar: el usuario asumiría
                    # que la recuperación manual funcionó.
                    try:
                        estados_final = cls.reconciliar_lote(snapshots, device)
                    except Exception as exc:
                        raise DeviceExecutionError(
                            f"Verification read failed after retry_rollback apply: "
                            f"{type(exc).__name__}: {exc or '<no message>'}"
                        )
                    fallas = []
                    for snap, (pasos, verificar), estado_final in zip(snapshots, planes, estados_final):
                        if not pasos or verificar is None:
                            continue
                        actual_final = estado_final.get("actual") if isinstance(estado_final, dict) else None
                        try:
                            ok = verificar(actual_final)
                        except Exception:
                            ok = False
                        if not ok:
                            fallas.append(snap.resumen_intento())
                    if fallas:
                        raise DeviceExecutionError(
                            "retry_rollback applied (rc=0) but per-resource verification "
                            "did not match the snapshot for: " + "; ".join(fallas)
                        )
                    resultado = {**resultado, "retry_of_job_id": original_job.job_id}
        except Exception as error:
            friendly = self._error_amigable(error)
            # No hacemos un "rollback del retry-rollback" -- si ESTO
            # también falla el usuario tiene que ver el device
            # manualmente. Marcar False/None hace explícito que no hubo
            # ni intento (no queremos que el UI muestre "rollback
            # attempted" para un job que YA ERA un intento de rollback).
            new_job.marcar_fallido(str(error), False, None, **friendly)
            self._jobs.add(new_job)
            self._eventos.despachar([DomainEvent(
                "recurso_fallido", snapshots[0], device, actor,
                {
                    "error": str(error),
                    "retry_of_job_id": original_job.job_id,
                    "rollback_performed": False,
                    "rollback_success": None,
                    **friendly,
                },
                exitoso=False,
            )])
            raise
        else:
            new_job.marcar_completado(resultado)
            self._jobs.add(new_job)
            try:
                self._eventos.despachar([DomainEvent(
                    "recurso_aplicado", snapshots[0], device, actor,
                    {**resultado, "retry_of_job_id": original_job.job_id},
                )])
            except Exception:
                logger.exception(
                    "retry_rollback: dispatch de evento recurso_aplicado falló para "
                    "job=%s (original=%s) -- audit trail puede quedar incompleto.",
                    new_job.job_id, original_job.job_id,
                )
        finally:
            if not new_job.esta_en_estado_terminal():
                new_job.asegurar_estado_final()
                self._jobs.add(new_job)

    def _retry_rollback_fallar(self, new_job, actor, device, recurso_referencia, error: str) -> None:
        """Wrapper para los early-exit de ``retry_rollback`` (job antiguo
        sin pre_state, tipo no soportado, device eliminado post-original-
        job) -- marca el job como failed con clasificación permanente y
        despacha un evento acorde, sin haber tocado el device."""
        try:
            if not new_job.esta_en_estado_terminal() and new_job.status == "pending":
                new_job.marcar_iniciado()
                self._jobs.add(new_job)
        except Exception:
            pass
        friendly = {
            "error_type": "permanent",
            "error_reason": "retry_rollback precondition",
            "error_summary": error,
        }
        new_job.marcar_fallido(error, False, None, **friendly)
        self._jobs.add(new_job)
        if device is not None and recurso_referencia is not None:
            try:
                self._eventos.despachar([DomainEvent(
                    "recurso_fallido", recurso_referencia, device, actor,
                    {"error": error, "rollback_performed": False, "rollback_success": None, **friendly},
                    exitoso=False,
                )])
            except Exception:
                logger.exception("retry_rollback: dispatch de evento de fallo temprano falló")

    def _rollback(self, recurso: "RecursoGestionable", pre_state: dict, device: "Device") -> tuple[bool, "bool | None"]:
        """Unifica vlan_execution_service.py: _rollback_create/_rollback_delete/
        _rollback_update + los 7 equivalentes de puerto
        (port_execution_service.py/port_config_service.py) -- nunca propaga
        (cada rama con su propio try/except), retorna
        (rollback_performed, rollback_success), verifica contra el device
        después de revertir. FINAL_ARCHITECTURE.md §2.4 nota (9).

        Polimórfico -- ``Orquestador`` delega en ``recurso.ejecutar_rollback()``
        (implementado por ``VLAN``/``Puerto``/``SVI``/``GlobalConfig``, mismo
        criterio que ``resolver_rollback()`` para el camino batcheado) en vez
        de un ``if tipo == "vlan"/"puerto"/"svi"/"global_config"``. Corrige
        una desviación real de FASE_5.md A3 (que dejaba "revertir" fuera de
        ``RecursoGestionable`` a propósito, "responsabilidad de la Saga, no
        del recurso") -- ese criterio ya se había roto para el camino
        batcheado con ``resolver_rollback()``; unificar acá evita tener 2
        mecanismos de rollback con dueños distintos (uno polimórfico, uno
        con ``if/elif`` en ``Orquestador``) para el mismo concepto."""
        return recurso.ejecutar_rollback(pre_state, device)

