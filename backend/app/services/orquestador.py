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
_PATRONES_PERMANENTES: tuple[str, ...] = (
    "invalid vlan id",
    "incomplete command",
    "syntax error",
    "vlan already exists",
    "permission denied",
    "authentication failure",
    "authentication failed",
    "unsupported command",
    "invalid input",
    "invalid command",
    "authorization failed",
    "access denied",
    "ambiguous command",
    "bad command",
    "error: invalid",
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
    "does not exist",   # Huawei: "The VLAN does not exist"; Cisco: "VLAN X does not exist"
    "already in use",   # Cisco: "% Interface ... already in use"
    "already configured",
    "is not valid",     # "IP address is not valid", etc.
    "out of range",     # rango numérico rechazado (VLAN, threshold, etc.)
    "in use by",        # "VLAN in use by port ..."
    # Tipos de excepción de auth de Paramiko/Netmiko -- llegan como prefijo
    # del str(exc) (ver _ejecutar_con_retry). Los ponemos como permanentes
    # antes de _PATRONES_TRANSITORIOS por defensa en profundidad: aunque el
    # mensaje diga "connection timeout" (que matchearía transitorio), si el
    # tipo dice AuthenticationException el problema real es de credenciales
    # y reintentar sólo empeora la situación (ej. lockout tras N intentos).
    "authenticationexception",
    "badauthenticationtype",
    "partialauthentication",
)

_PATRONES_TRANSITORIOS: tuple[str, ...] = (
    # Spec-required exact phrases
    "ssh timeout",
    "connection timeout",
    "socket timeout",
    "temporary unreachable",
    "ssh connection failed",
    "network_cli timeout",
    "session reset",
    "connection reset",
    "eof during transport",
    "command timeout",
    "ansible persistent connection timeout",
    # Broader patterns that map to the same transient class
    "timeout",
    "timed out",
    "connection refused",
    "unable to connect",
    "ssh failure",
    "ssh error",
    "ssh connect",
    "network is unreachable",
    "no route to host",
    "broken pipe",
    "host unreachable",
    "transport endpoint",
    "reset by peer",
    # ssh_direct_service.py (devices en auth_method="key"): mensaje real de
    # OpenSSH cuando VRP/IOS cortan la conexión -- confirmado en vivo esta
    # sesión que pasa tanto por un cierre benigno de fin de sesión (ya
    # filtrado antes de llegar acá por _sesion_completa()) como por un
    # corte genuino a mitad de comando, intermitente, sin patrón claro --
    # sin este pattern caía en "unknown" y solo tenía 1 reintento con delay
    # fijo de 1s en vez del backoff exponencial completo.
    "closed by remote host",
    "end of file",
    # SSH session exhaustion -- el caso concreto que motivó esta ronda de
    # reliability: el device sólo permite N sesiones SSH concurrentes
    # (típicamente 5-16 en Cisco/Huawei) y cuando N sesiones ya están
    # tomadas (por nosotros mismos, monitoreo, o alguien conectado por
    # consola), el N+1 rebota con un mensaje que ninguno de los patterns
    # de arriba matcheaba -- caía al default "permanent" de la línea 304
    # y el job moría de un tiro. Todos estos son transitorios de verdad:
    # esperar unos segundos hasta que otra sesión se cierre y reintentar
    # es exactamente lo correcto.
    "unable to open channel",
    "channel is not open",
    "channel closed",
    "session limit",
    "max allowed sessions",
    "too many sessions",
    "ssh_msg_channel_open_failure",
    "administratively prohibited",
    "resource temporarily unavailable",
    # Códigos numéricos de errno crudos (sin nombre simbólico) que llegan
    # cuando str(exc) es un OSError sin decorar. Los 4 más comunes en
    # SSH transitorio: ETIMEDOUT=110, ECONNRESET=104, ECONNREFUSED=111,
    # EHOSTUNREACH=113, EAGAIN=11.
    "[errno 11]",
    "[errno 104]",
    "[errno 110]",
    "[errno 111]",
    "[errno 113]",
    # Otras variantes de red/handshake que ya vimos escapar en producción
    # sin matchear ningún pattern anterior.
    "remote host closed",
    "connection aborted",
    "handshake",
    "keepalive",
    "no existing session",
    "failed to connect",
    "unable to establish",
    # Nombres de tipo de excepción de Python/Paramiko/Netmiko -- prefijados
    # al str(exc) por _ejecutar_con_retry (ver el except del retry loop).
    # Muchas de estas excepciones traen str(exc) vacío o críptico y el
    # tipo es la única señal léxica disponible. Los tipos de auth
    # exception NO van acá -- son permanentes (ver _PATRONES_PERMANENTES).
    "sshexception",
    "timeouterror",
    "connectionreseterror",
    "connectionrefusederror",
    "connectionabortederror",
    "eoferror",
    "netmikotimeoutexception",
    "socket.timeout",
    "socket.gaierror",
    "ssl.sslerror",
    # ansible_service.py's rc=0-but-stdout-is-literally-"None" guard --
    # ver esa nota, mismo criterio: no es un rechazo real del device, es
    # una lectura que no se pudo confiar, vale la pena reintentar.
    "possible read desync",
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
                    raise DeviceExecutionError(
                        resultado_prestate.get("stderr")
                        or resultado_prestate.get("stdout")
                        or "Prestate read failed"
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
                        raise DeviceExecutionError(resultado.get("stderr") or resultado.get("stdout") or "Execution failed")
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
                    raise DeviceExecutionError(
                        resultado_prestate.get("stderr")
                        or resultado_prestate.get("stdout")
                        or "Prestate read failed"
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
                        raise DeviceExecutionError(resultado.get("stderr") or resultado.get("stdout") or "Execution failed")
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
                    {**resultado, "lote_size": len(recursos)},
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
        lowered = texto.lower()
        summary = None
        for patrones, mensaje in _MENSAJES_ERROR:
            if any(p in lowered for p in patrones):
                summary = mensaje
                break
        if summary is None:
            # No matchea ninguna tabla -- honestos, no inventamos. El
            # ``error`` crudo sigue disponible para debug; ``error_summary``
            # solo agrega valor cuando de verdad podemos traducir a algo
            # útil.
            summary = "Unclassified device or connection error — see raw output for details."
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
        lowered = combinado.lower()
        for patron in _PATRONES_PERMANENTES:
            if patron in lowered:
                return RetryDecision(should_retry=False, classification="permanent", reason=patron)
        for patron in _PATRONES_TRANSITORIOS:
            if patron in lowered:
                return RetryDecision(should_retry=True, classification="transient", reason=patron)
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
           ``_plan_rollback_XXX`` (que devuelven ``(pasos, verificar)``,
           sin tocar el device).
        2. Se manda 1 sola ``aplicar_lote()`` con todos los pasos
           concatenados -- 1 conexión SSH para escribir N reversiones.
        3. Se verifica con 1 sola ``reconciliar_lote()`` -- 1 conexión SSH
           adicional; cada verificador per-recurso corre contra su
           ``actual`` post-rollback ya distribuido en memoria.

        Total: 2 conexiones SSH (o 3 para SVI, ver abajo) sin importar
        cuántos recursos tenga el lote, contra 2N del camino viejo.

        Para SVI se hace 1 ``reconciliar_lote()`` EXTRA al inicio -- 3
        campos (``ipv4_address_secondary``/``acl_in``/``acl_out``)
        necesitan el estado ACTUAL del device (post-apply-fallido) para
        armar su comando de revert (mismo motivo que
        ``SVI._resolver_ipv4_secondary()`` lo pide vía ``actual`` en el
        camino normal). Port no lo necesita -- todos sus branches
        derivan puramente de ``pre_state``.

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
        tipo = recursos[0].repositorio()
        cls = type(recursos[0])

        # Fresh state post-apply (solo SVI la necesita, ver docstring).
        actuales_ahora: list = [None] * len(recursos)
        if tipo == "svi":
            try:
                estados_ahora = cls.reconciliar_lote(recursos, device)
                actuales_ahora = [e.get("actual") for e in estados_ahora]
            except Exception:
                logger.exception(
                    "_rollback_lote: fresh reconciliar_lote() falló para tipo=svi "
                    "device=%s -- degradando a rollback con actual_ahora=None para "
                    "todos los recursos (los 3 campos que la necesitan van a "
                    "devolver (False, None) en su plan).", device.name,
                )

        # Plan phase: pasos + verificar por recurso, SIN tocar el device.
        planes: "list[tuple[list, callable | None]]" = []
        for recurso, pre_state, actual_ahora in zip(recursos, pre_states, actuales_ahora):
            try:
                if tipo == "puerto":
                    plan = self._plan_rollback_puerto(recurso, pre_state, device)
                elif tipo == "svi":
                    plan = self._plan_rollback_svi(recurso, pre_state, device, actual_ahora)
                else:
                    # VLAN/GlobalConfig no batchean hoy (no hay endpoint de
                    # lote para esos tipos) -- si llegara alguno se trata
                    # como no-op batched-side y el camino single-recurso lo
                    # cubre en el llamador de arriba.
                    plan = ([], None)
            except Exception:
                logger.exception(
                    "_rollback_lote: _plan_rollback_%s tiró excepción para "
                    "device=%s -- marcando como (True, False) para no perder "
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

    def _plan_rollback_puerto(
        self, puerto: "Puerto", pre_state: dict, device: "Device",
    ) -> "tuple[list, callable | None]":
        """Versión "plan" de ``_rollback_puerto`` (que sigue existiendo para
        el camino single-recurso de ``ejecutar()``): devuelve
        ``(pasos, verificar)`` en vez de ejecutar contra el device.

        - ``pasos``: lista de ``(op_key, variant, vars)`` -- misma forma
          que devuelve ``resolver_paso()``, lista para batching con
          ``driver.aplicar_lote()``. Vacía = no-op (equivalente a
          ``(False, None)`` en la versión eager).
        - ``verificar``: closure que recibe el ``actual`` post-rollback y
          devuelve bool. ``None`` = confiar en el rc del driver, no hay
          nada útil que releer.

        Las reglas y guards (reader gap para PoE, mode->modo previo, etc.)
        son las MISMAS que ``_rollback_puerto`` -- si aparece un caso
        nuevo, agregarlo en LOS DOS lados. Un split más agresivo (una
        sola función de "reglas" compartida) requeriría refactor de la
        rama single para no cambiar sus SSH counts -- se puede hacer en
        otra pasada."""
        if not pre_state.get("existed"):
            return [], None
        anterior = pre_state.get("actual")
        if anterior is None:
            return [], None

        if puerto.reset:
            return self._plan_rollback_puerto_reset(puerto, anterior, device)

        campos = puerto.mutation_fields
        paso = None

        if puerto.mode in ("access", "trunk"):
            if anterior.mode == "access":
                if anterior.access_vlan is None:
                    return [], None
                paso = device.driver.resolver_set_access_mode(puerto.interface, int(anterior.access_vlan))
            elif anterior.mode == "trunk":
                if anterior.access_vlan is None or not anterior.allowed_vlans:
                    return [], None
                paso = device.driver.resolver_set_trunk_mode(
                    puerto.interface, int(anterior.access_vlan), list(anterior.allowed_vlans),
                )
            else:
                return [], None
        else:
            campo = next(iter(campos))
            if campo == "description":
                valor = anterior.description or ""
                paso = device.driver.resolver_update_port_description(puerto.interface, valor)
            elif campo == "admin_up":
                if anterior.admin_up is None:
                    return [], None
                paso = device.driver.resolver_set_port_admin_state(puerto.interface, bool(anterior.admin_up))
            elif campo == "access_vlan":
                if anterior.access_vlan is None:
                    return [], None
                if anterior.mode == "trunk":
                    paso = device.driver.resolver_set_trunk_pvid_vlan(puerto.interface, int(anterior.access_vlan))
                else:
                    paso = device.driver.resolver_set_port_access_vlan(puerto.interface, int(anterior.access_vlan))
            elif campo == "allowed_vlans":
                if not anterior.allowed_vlans:
                    return [], None
                paso = device.driver.resolver_set_trunk_allowed_vlans(puerto.interface, list(anterior.allowed_vlans))
            elif campo == "poe_enabled":
                # Reader gap -- ver _rollback_puerto para el porqué.
                if anterior.poe_enabled is None:
                    return [], None
                paso = device.driver.resolver_set_port_poe(puerto.interface, bool(anterior.poe_enabled))
            elif campo in ("storm_control_enabled", "storm_control_threshold"):
                if anterior.storm_control_enabled is None:
                    return [], None
                threshold_previo = (
                    int(anterior.storm_control_threshold)
                    if anterior.storm_control_threshold is not None else 0
                )
                paso = device.driver.resolver_set_storm_control(
                    puerto.interface, bool(anterior.storm_control_enabled), threshold_previo,
                )
            else:
                return [], None

        return [paso], self._make_verificar_puerto(anterior, campos)

    def _plan_rollback_puerto_reset(
        self, puerto: "Puerto", anterior: "Any", device: "Device",
    ) -> "tuple[list, callable | None]":
        """Versión "plan" de ``_rollback_puerto_reset`` -- devuelve la
        lista de N pasos que hay que mandar para reconstruir la config
        anterior (mode+vlan, description, admin_up, storm-control) sin
        tocar el device. PoE queda fuera por el reader gap conocido."""
        pasos = []
        if anterior.mode == "access" and anterior.access_vlan is not None:
            pasos.append(device.driver.resolver_set_access_mode(
                puerto.interface, int(anterior.access_vlan),
            ))
        elif anterior.mode == "trunk" and anterior.access_vlan is not None and anterior.allowed_vlans:
            pasos.append(device.driver.resolver_set_trunk_mode(
                puerto.interface, int(anterior.access_vlan), list(anterior.allowed_vlans),
            ))
        if anterior.description is not None:
            pasos.append(device.driver.resolver_update_port_description(
                puerto.interface, anterior.description,
            ))
        if anterior.admin_up is not None:
            pasos.append(device.driver.resolver_set_port_admin_state(
                puerto.interface, bool(anterior.admin_up),
            ))
        if anterior.storm_control_enabled is not None:
            threshold_previo = (
                int(anterior.storm_control_threshold)
                if anterior.storm_control_threshold is not None else 0
            )
            pasos.append(device.driver.resolver_set_storm_control(
                puerto.interface, bool(anterior.storm_control_enabled), threshold_previo,
            ))

        def verificar(actual):
            if actual is None:
                return False
            comparaciones = (
                ("description", anterior.description),
                ("admin_up", anterior.admin_up),
                ("mode", anterior.mode),
                ("access_vlan", anterior.access_vlan),
                ("storm_control_enabled", anterior.storm_control_enabled),
            )
            for campo_ver, ant_val in comparaciones:
                if ant_val is None:
                    continue
                act_val = getattr(actual, campo_ver, None)
                if act_val is None:
                    continue  # reader gap
                if act_val != ant_val:
                    return False
            if anterior.allowed_vlans:
                if set(actual.allowed_vlans or []) != set(anterior.allowed_vlans):
                    return False
            return True

        return pasos, verificar

    def _make_verificar_puerto(self, anterior, campos):
        """Closure de verificación campo-por-campo con la misma lenientud
        para reader gaps que ``_rollback_puerto`` (act_val=None con
        ant_val=/=None -> no falla, se confía en el rc del driver)."""
        def verificar(actual):
            if actual is None:
                return False
            for c in campos:
                if not hasattr(anterior, c):
                    continue
                act_val = getattr(actual, c)
                ant_val = getattr(anterior, c)
                if act_val is None and ant_val is not None:
                    continue
                if act_val != ant_val:
                    return False
            return True
        return verificar

    def _plan_rollback_svi(
        self, svi: "SVI", pre_state: dict, device: "Device", actual_ahora: "Any | None",
    ) -> "tuple[list, callable | None]":
        """Versión "plan" de ``_rollback_svi``. Igual que Port pero acepta
        ``actual_ahora`` para los 3 campos que necesitan el estado POST-
        apply-fallido del device para construir su comando de revert
        (``ipv4_address_secondary``, ``acl_in``, ``acl_out``). Si el
        orquestador no pudo leer el estado fresco (``actual_ahora=None``),
        esos 3 se degradan a no-op -- mejor no revertir que mandar un
        comando basado en info stale."""
        if not pre_state.get("existed"):
            return [], None
        anterior = pre_state.get("actual")
        if anterior is None:
            return [], None

        campos = svi.mutation_fields
        if len(campos) != 1:
            return [], None
        campo = next(iter(campos))
        paso = None

        if campo == "description":
            paso = device.driver.resolver_set_svi_description(
                svi.vlan_id, anterior.description or "",
            )
        elif campo == "admin_up":
            if anterior.admin_up is None:
                return [], None
            paso = device.driver.resolver_set_svi_admin_state(
                svi.vlan_id, bool(anterior.admin_up),
            )
        elif campo == "ipv4_address":
            paso = device.driver.resolver_set_svi_ipv4(svi.vlan_id, anterior.ipv4_address)
        elif campo == "ipv4_address_secondary":
            if actual_ahora is None:
                return [], None
            previa = actual_ahora.ipv4_address_secondary
            paso = device.driver.resolver_set_svi_ipv4_secondary(
                svi.vlan_id, anterior.ipv4_address_secondary, previa,
            )
        elif campo == "ipv6_address":
            paso = device.driver.resolver_set_svi_ipv6(svi.vlan_id, anterior.ipv6_address)
        elif campo in ("acl_in", "acl_out"):
            if actual_ahora is None:
                return [], None
            current_acl = getattr(actual_ahora, campo)
            direccion = "in" if campo == "acl_in" else "out"
            paso = device.driver.resolver_set_svi_acl(
                svi.vlan_id, direccion, getattr(anterior, campo), current_acl_name=current_acl,
            )
        elif campo in ("dhcp_relay_add", "dhcp_relay_remove"):
            paso = device.driver.resolver_set_svi_dhcp_relay(
                svi.vlan_id, list(anterior.dhcp_relay_servers or []),
            )
        else:
            return [], None

        def verificar(actual):
            if actual is None:
                return False
            if campo in ("dhcp_relay_add", "dhcp_relay_remove"):
                return set(actual.dhcp_relay_servers or []) == set(anterior.dhcp_relay_servers or [])
            return getattr(actual, campo) == getattr(anterior, campo)

        return [paso], verificar

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

                # SVI: fresh state es necesario para resolver
                # ipv4_address_secondary/acl_in/acl_out (mismo motivo
                # que ``_rollback_lote``). Puerto no lo necesita.
                actuales_ahora_map: dict = {}
                if tipo == "svi":
                    try:
                        estados_ahora = cls.reconciliar_lote(snapshots, device)
                        for snap, est in zip(snapshots, estados_ahora):
                            actuales_ahora_map[snap.vlan_id] = est.get("actual")
                    except Exception:
                        logger.exception(
                            "retry_rollback: fresh reconciliar_lote() falló para tipo=svi "
                            "device=%s -- se degradan los 3 campos que la necesitan a "
                            "no-op (mismo criterio que _rollback_lote).", device.name,
                        )

                # Construir pasos per snapshot
                pasos_all = []
                for snap in snapshots:
                    if tipo == "puerto":
                        pasos = self._plan_restore_puerto(snap, device)
                    else:
                        pasos = self._plan_restore_svi(
                            snap, device, actuales_ahora_map.get(snap.vlan_id),
                        )
                    pasos_all.extend(pasos)

                if not pasos_all:
                    resultado = {
                        "rc": 0, "success": True, "changed": False, "noop": True,
                        "accion": "retry_rollback",
                        "retry_of_job_id": original_job.job_id,
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

    def _plan_restore_puerto(self, actual: "Puerto", device: "Device") -> list:
        """Pasos para restaurar TODO campo legible de un puerto al valor
        del snapshot -- forma más agresiva que ``_plan_rollback_puerto``
        (que solo revertía el campo que el request original había
        cambiado). Usado por ``retry_rollback``: el pre_state guardado
        es la única fuente de verdad, así que restauramos todo lo que se
        pueda leer, no solo el campo específico.

        PoE queda fuera -- reader gap conocido (``port_parser.py:140``/
        ``:708`` hardcodean ``None``). Si el snapshot dice
        ``poe_enabled=True/False`` no es info real, es siempre ``None``
        y sería intentar restaurar contra un valor inventado."""
        pasos = []
        if actual.mode == "access" and actual.access_vlan is not None:
            pasos.append(device.driver.resolver_set_access_mode(
                actual.interface, int(actual.access_vlan),
            ))
        elif actual.mode == "trunk" and actual.access_vlan is not None and actual.allowed_vlans:
            pasos.append(device.driver.resolver_set_trunk_mode(
                actual.interface, int(actual.access_vlan), list(actual.allowed_vlans),
            ))
        if actual.description is not None:
            pasos.append(device.driver.resolver_update_port_description(
                actual.interface, actual.description,
            ))
        if actual.admin_up is not None:
            pasos.append(device.driver.resolver_set_port_admin_state(
                actual.interface, bool(actual.admin_up),
            ))
        if actual.storm_control_enabled is not None:
            threshold = (
                int(actual.storm_control_threshold)
                if actual.storm_control_threshold is not None else 0
            )
            pasos.append(device.driver.resolver_set_storm_control(
                actual.interface, bool(actual.storm_control_enabled), threshold,
            ))
        return pasos

    def _plan_restore_svi(
        self, actual: "SVI", device: "Device", actual_ahora: "SVI | None",
    ) -> list:
        """Pasos para restaurar TODO campo legible de un SVI al snapshot.
        Same shape que ``_plan_restore_puerto`` pero acepta
        ``actual_ahora`` para los 3 campos cuyo resolver necesita el
        estado post-fallo del device (``ipv4_address_secondary``,
        ``acl_in``, ``acl_out``). Si ``actual_ahora`` es ``None`` (fresh
        read falló, ver ``retry_rollback``) esos 3 se saltean --
        mejor no restaurar que mandar un comando con info stale."""
        pasos = []
        if actual.description is not None:
            pasos.append(device.driver.resolver_set_svi_description(
                actual.vlan_id, actual.description,
            ))
        if actual.admin_up is not None:
            pasos.append(device.driver.resolver_set_svi_admin_state(
                actual.vlan_id, bool(actual.admin_up),
            ))
        if actual.ipv4_address is not None:
            pasos.append(device.driver.resolver_set_svi_ipv4(
                actual.vlan_id, actual.ipv4_address,
            ))
        if actual.ipv6_address is not None:
            pasos.append(device.driver.resolver_set_svi_ipv6(
                actual.vlan_id, actual.ipv6_address,
            ))
        if actual.dhcp_relay_servers:
            pasos.append(device.driver.resolver_set_svi_dhcp_relay(
                actual.vlan_id, list(actual.dhcp_relay_servers),
            ))
        # Campos que requieren actual_ahora
        if actual_ahora is not None:
            if actual.ipv4_address_secondary is not None:
                pasos.append(device.driver.resolver_set_svi_ipv4_secondary(
                    actual.vlan_id, actual.ipv4_address_secondary,
                    actual_ahora.ipv4_address_secondary,
                ))
            if actual.acl_in is not None:
                pasos.append(device.driver.resolver_set_svi_acl(
                    actual.vlan_id, "in", actual.acl_in,
                    current_acl_name=actual_ahora.acl_in,
                ))
            if actual.acl_out is not None:
                pasos.append(device.driver.resolver_set_svi_acl(
                    actual.vlan_id, "out", actual.acl_out,
                    current_acl_name=actual_ahora.acl_out,
                ))
        return pasos

    def _rollback(self, recurso: "RecursoGestionable", pre_state: dict, device: "Device") -> tuple[bool, "bool | None"]:
        """Unifica vlan_execution_service.py: _rollback_create/_rollback_delete/
        _rollback_update + los 7 equivalentes de puerto
        (port_execution_service.py/port_config_service.py) -- nunca propaga
        (cada rama con su propio try/except), retorna
        (rollback_performed, rollback_success), verifica contra el device
        después de revertir. FINAL_ARCHITECTURE.md §2.4 nota (9). No está en
        RecursoGestionable a propósito -- revertir es responsabilidad de la
        Saga, no del recurso (ver FASE_5.md A3)."""
        tipo = recurso.repositorio()
        if tipo == "vlan":
            return self._rollback_vlan(recurso, pre_state, device)
        if tipo == "puerto":
            return self._rollback_puerto(recurso, pre_state, device)
        if tipo == "svi":
            return self._rollback_svi(recurso, pre_state, device)
        if tipo == "global_config":
            return self._rollback_global_config(recurso, pre_state, device)
        return False, None

    def _rollback_vlan(self, vlan: "VLAN", pre_state: dict, device: "Device") -> tuple[bool, "bool | None"]:
        existia = pre_state.get("existed")
        nombre_previo = pre_state.get("name")
        try:
            if vlan.eliminar:
                if not existia:
                    return False, None  # no existía antes -- nada que restaurar
                resultado = device.driver.create_vlan(vlan.vlan_id, nombre_previo or vlan.name, device, device.password)
            elif existia and nombre_previo != vlan.name:
                resultado = device.driver.update_vlan(vlan.vlan_id, nombre_previo, device, device.password)
            elif not existia:
                resultado = device.driver.delete_vlan(vlan.vlan_id, device, device.password)
            else:
                return False, None
        except Exception:
            return True, False

        if resultado.get("rc", 1) != 0:
            return True, False

        try:
            actuales = device.driver.get_vlans(device, device.password)
            actual = next((v for v in actuales if v.vlan_id == vlan.vlan_id), None)
            if vlan.eliminar:
                # se había borrado -- el rollback la recreó, debe volver a existir
                verificado = actual is not None
            elif existia and nombre_previo != vlan.name:
                # se había renombrado -- el rollback restauró el nombre anterior
                verificado = actual is not None and actual.name == nombre_previo
            else:
                # se había creado -- el rollback la borró, no debe existir más
                verificado = actual is None
        except Exception:
            verificado = False
        return True, verificado

    def _rollback_puerto(self, puerto: "Puerto", pre_state: dict, device: "Device") -> tuple[bool, "bool | None"]:
        """Ramas explícitas, calzadas 1 a 1 con el dispatch de
        ``Puerto.aplicar()`` (``puerto.reset`` -> ``puerto.mode`` ->
        storm-control -> campo único).

        Corrección real de esta ronda -- el ``else`` genérico se comía
        3 campos que ``aplicar()`` sí escribe al device (``poe_enabled``,
        ``storm_control_enabled``/``storm_control_threshold``, y el path
        ``reset``): un batch que le pegaba a alguno de estos y fallaba
        a mitad de camino dejaba los cambios previos sin revertir, con
        ``rollback_performed=false`` silencioso. Ahora cada uno tiene su
        propia rama.

        Guards de "reader gap" para PoE (``port_parser.py:140/708``
        hardcodean ``poe_enabled=None`` -- el device se puede escribir
        pero no leer todavía): si ``anterior.<campo>`` no vino del
        reader, retornamos ``(False, None)`` en vez de intentar un
        rollback ciego contra un valor que no conocemos. Storm-control
        SÍ se lee (``port_parser.py:135``/``:705``), así que ese path
        sí revierte end-to-end contra un valor real.
        """
        if not pre_state.get("existed"):
            return False, None
        anterior = pre_state.get("actual")
        if anterior is None:
            return False, None

        # ``puerto.reset`` es exclusivo con el resto de los campos (ver
        # ``Puerto.validar()``) -- tratado antes que ``puerto.mode`` para
        # evitar el ``next(iter(campos))`` sobre un ``mutation_fields``
        # vacío (reset no cuenta como mutation_field, es un flag aparte).
        if puerto.reset:
            return self._rollback_puerto_reset(puerto, anterior, device)

        campos = puerto.mutation_fields
        try:
            if puerto.mode in ("access", "trunk"):
                # aplicar() cambió el modo del puerto -- restaurar significa
                # devolverlo al modo/VLAN que tenía ANTES, que puede ser
                # distinto al que se acaba de aplicar (venía de trunk y se
                # cambió a access, o viceversa).
                if anterior.mode == "access":
                    if anterior.access_vlan is None:
                        return False, None
                    resultado = device.driver.set_access_mode(puerto.interface, int(anterior.access_vlan), device, device.password)
                elif anterior.mode == "trunk":
                    if anterior.access_vlan is None or not anterior.allowed_vlans:
                        return False, None
                    resultado = device.driver.set_trunk_mode(
                        puerto.interface, int(anterior.access_vlan), list(anterior.allowed_vlans),
                        device, device.password,
                    )
                else:
                    # modo previo desconocido/no reconocido -- no hay forma
                    # segura de reconstruirlo.
                    return False, None
                exitoso = resultado.get("rc", 1) == 0
            else:
                campo = next(iter(campos))
                if campo == "description":
                    valor = anterior.description or ""
                    resultado = device.driver.update_port_description(puerto.interface, valor, device, device.password)
                    exitoso = resultado.get("rc", 1) == 0
                elif campo == "admin_up":
                    if anterior.admin_up is None:
                        return False, None
                    resultado = device.driver.set_port_admin_state(puerto.interface, bool(anterior.admin_up), device, device.password)
                    exitoso = resultado.get("rc", 1) == 0
                elif campo == "access_vlan":
                    if anterior.access_vlan is None:
                        return False, None
                    if anterior.mode == "trunk":
                        resultado = device.driver.set_trunk_pvid_vlan(puerto.interface, int(anterior.access_vlan), device, device.password)
                    else:
                        resultado = device.driver.set_port_access_vlan(puerto.interface, int(anterior.access_vlan), device, device.password)
                    exitoso = resultado.get("rc", 1) == 0
                elif campo == "allowed_vlans":
                    if not anterior.allowed_vlans:
                        return False, None
                    resultado = device.driver.set_trunk_allowed_vlans(puerto.interface, list(anterior.allowed_vlans), device, device.password)
                    exitoso = resultado.get("rc", 1) == 0
                elif campo == "poe_enabled":
                    # Reader gap: ``poe_enabled`` no se lee todavía (ver
                    # docstring del método). Sin valor previo real no
                    # tiene sentido inventar un rollback -- devolver
                    # ``(False, None)`` es consistente con "no hay nada
                    # que restaurar" (mismo shape que devuelve la rama
                    # ``admin_up`` cuando ``anterior.admin_up is None``).
                    # Cuando el parser aprenda a leer PoE (agregando un
                    # 4to comando en ``list_ports``), este branch va a
                    # empezar a funcionar sin cambios acá.
                    if anterior.poe_enabled is None:
                        return False, None
                    resultado = device.driver.set_port_poe(
                        puerto.interface, bool(anterior.poe_enabled), device, device.password,
                    )
                    exitoso = resultado.get("rc", 1) == 0
                elif campo in ("storm_control_enabled", "storm_control_threshold"):
                    # ``expandir_a_puertos`` siempre agrupa storm-control
                    # como enabled+threshold en un solo ``Puerto`` (ver
                    # ``api/ports.py:657-661``), así que ambos campos
                    # viajan juntos y ``anterior`` los tiene ambos legibles.
                    # Si el reader no pudo capturar el estado previo
                    # (device sin storm-control soportado -- ``storm_control_*``
                    # quedan en ``None``, ver
                    # ``vendors/cisco/driver.py:127-136``), no revertimos.
                    if anterior.storm_control_enabled is None:
                        return False, None
                    threshold_previo = (
                        int(anterior.storm_control_threshold)
                        if anterior.storm_control_threshold is not None else 0
                    )
                    resultado = device.driver.set_storm_control(
                        puerto.interface, bool(anterior.storm_control_enabled),
                        threshold_previo, device, device.password,
                    )
                    exitoso = resultado.get("rc", 1) == 0
                else:
                    return False, None
        except Exception:
            return True, False

        if not exitoso:
            return True, False

        try:
            puertos = device.driver.list_ports(device, device.password)
            actual = next((p for p in puertos if p.interface == puerto.interface), None)
            if actual is None:
                verificado = False
            else:
                # Verificación campo por campo -- con lenientud para el
                # caso "reader gap" (``actual.<campo> is None`` cuando el
                # parser no lee ese campo todavía, ej. PoE): confiar en
                # el ``rc=0`` del driver que ya validamos arriba es mejor
                # que reportar ``rollback_success=false`` por un campo
                # que sabemos que no se lee.
                verificado = True
                for c in campos:
                    if not hasattr(anterior, c):
                        continue
                    act_val = getattr(actual, c)
                    ant_val = getattr(anterior, c)
                    if act_val is None and ant_val is not None:
                        continue  # reader gap -- no falla la verificación
                    if act_val != ant_val:
                        verificado = False
                        break
        except Exception:
            verificado = False
        return True, verificado

    def _rollback_puerto_reset(
        self, puerto: "Puerto", anterior: "Any", device: "Device",
    ) -> tuple[bool, "bool | None"]:
        """Rollback de ``reset_port`` -- reset devuelve el puerto a defaults
        (``default interface`` en Cisco, ``clear configuration interface``
        en Huawei), así que revertir significa reconstruir la config
        anterior desde ``pre_state.actual``, campo por campo. Es la única
        rama con múltiples llamadas al driver -- todas las demás son 1
        campo, 1 llamada. Si ALGUNA subllamada falla, seguimos igual con
        las restantes (idea: dejar el puerto lo más cerca del estado
        anterior que se pueda) y reportamos ``(True, False)`` al final.
        PoE no se restaura -- reader gap conocido (ver
        ``_rollback_puerto`` -> rama ``poe_enabled``); cuando el parser
        aprenda a leerlo, agregar acá el ``set_port_poe(anterior.poe_enabled)``.
        """
        ok = True

        def _correr(fn, *args):
            nonlocal ok
            try:
                r = fn(*args, device, device.password)
                if r.get("rc", 1) != 0:
                    ok = False
            except Exception:
                ok = False

        # Mode + VLAN(s) -- restaurar antes que el resto porque un cambio
        # de modo pisa description/admin/etc. en muchos dispositivos.
        if anterior.mode == "access" and anterior.access_vlan is not None:
            _correr(device.driver.set_access_mode, puerto.interface, int(anterior.access_vlan))
        elif anterior.mode == "trunk" and anterior.access_vlan is not None and anterior.allowed_vlans:
            _correr(
                device.driver.set_trunk_mode, puerto.interface,
                int(anterior.access_vlan), list(anterior.allowed_vlans),
            )

        if anterior.description is not None:
            _correr(device.driver.update_port_description, puerto.interface, anterior.description)

        if anterior.admin_up is not None:
            _correr(device.driver.set_port_admin_state, puerto.interface, bool(anterior.admin_up))

        if anterior.storm_control_enabled is not None:
            threshold_previo = (
                int(anterior.storm_control_threshold)
                if anterior.storm_control_threshold is not None else 0
            )
            _correr(
                device.driver.set_storm_control, puerto.interface,
                bool(anterior.storm_control_enabled), threshold_previo,
            )

        # Verificación final -- misma lenientud que ``_rollback_puerto``
        # (reader gap = campo tolerado si volvemos a leerlo como None).
        try:
            puertos = device.driver.list_ports(device, device.password)
            actual = next((p for p in puertos if p.interface == puerto.interface), None)
            if actual is None:
                return True, False
            verificado = True
            comparaciones = (
                ("description", anterior.description),
                ("admin_up", anterior.admin_up),
                ("mode", anterior.mode),
                ("access_vlan", anterior.access_vlan),
                ("storm_control_enabled", anterior.storm_control_enabled),
            )
            for campo, ant_val in comparaciones:
                if ant_val is None:
                    continue
                act_val = getattr(actual, campo, None)
                if act_val is None:
                    continue  # reader gap
                if act_val != ant_val:
                    verificado = False
                    break
            if verificado and anterior.allowed_vlans:
                actuales = set(actual.allowed_vlans or [])
                if actuales != set(anterior.allowed_vlans):
                    verificado = False
        except Exception:
            verificado = False
        return True, ok and verificado

    def _svi_actual(self, device: "Device", vlan_id: int) -> "Any | None":
        svis = device.driver.get_svis(device, device.password)
        return next((s for s in svis if s.vlan_id == vlan_id), None)

    def _rollback_svi(self, svi: "SVI", pre_state: dict, device: "Device") -> tuple[bool, "bool | None"]:
        """Ver ``_rollback_puerto()`` -- mismo criterio (exactamente 1 campo
        de mutación por instancia, restaura contra ``pre_state.actual``,
        verifica releyendo después). Bug real encontrado: ``_rollback()``
        no tenía rama para ``tipo == "svi"`` -- caía al ``return False, None``
        genérico del final, es decir NUNCA revertía nada. En un
        ``ejecutar_lote()`` donde 1 campo se aplica bien y otro campo (u
        otra excepción) hace fallar el batch entero, el campo que sí se
        aplicó quedaba en el device sin revertir -- pese a que el docstring
        de la API (``PATCH .../svis/{vlan_id}/batch``) promete "rollback
        attempts to restore every field that did change".

        ``ipv4_address_secondary``/``acl_in``/``acl_out`` necesitan el
        valor ACTUAL del device (no el de *pre_state*) para armar su "undo"
        -- mismo motivo que ``SVI._resolver_ipv4_secondary()``/
        ``_resolver_acl()`` lo piden vía ``actual`` en el camino normal,
        acá se relee 1 vez con ``_svi_actual()`` en vez de asumir que
        *pre_state* sigue vigente."""
        if not pre_state.get("existed"):
            return False, None
        anterior = pre_state.get("actual")
        if anterior is None:
            return False, None

        campos = svi.mutation_fields
        if len(campos) != 1:
            return False, None
        campo = next(iter(campos))
        try:
            if campo == "description":
                resultado = device.driver.set_svi_description(
                    svi.vlan_id, anterior.description or "", device, device.password,
                )
            elif campo == "admin_up":
                if anterior.admin_up is None:
                    return False, None
                resultado = device.driver.set_svi_admin_state(
                    svi.vlan_id, bool(anterior.admin_up), device, device.password,
                )
            elif campo == "ipv4_address":
                resultado = device.driver.set_svi_ipv4(
                    svi.vlan_id, anterior.ipv4_address, device, device.password,
                )
            elif campo == "ipv4_address_secondary":
                actual_ahora = self._svi_actual(device, svi.vlan_id)
                previa = actual_ahora.ipv4_address_secondary if actual_ahora is not None else None
                resultado = device.driver.set_svi_ipv4_secondary(
                    svi.vlan_id, anterior.ipv4_address_secondary, previa, device, device.password,
                )
            elif campo == "ipv6_address":
                resultado = device.driver.set_svi_ipv6(
                    svi.vlan_id, anterior.ipv6_address, device, device.password,
                )
            elif campo in ("acl_in", "acl_out"):
                actual_ahora = self._svi_actual(device, svi.vlan_id)
                current_acl = getattr(actual_ahora, campo) if actual_ahora is not None else None
                direccion = "in" if campo == "acl_in" else "out"
                resultado = device.driver.set_svi_acl(
                    svi.vlan_id, direccion, getattr(anterior, campo), device, device.password,
                    current_acl_name=current_acl,
                )
            elif campo in ("dhcp_relay_add", "dhcp_relay_remove"):
                resultado = device.driver.set_svi_dhcp_relay(
                    svi.vlan_id, list(anterior.dhcp_relay_servers or []), device, device.password,
                )
            else:
                return False, None
            exitoso = resultado.get("rc", 1) == 0
        except Exception:
            return True, False

        if not exitoso:
            return True, False

        try:
            actual_final = self._svi_actual(device, svi.vlan_id)
            if actual_final is None:
                verificado = False
            elif campo in ("dhcp_relay_add", "dhcp_relay_remove"):
                verificado = set(actual_final.dhcp_relay_servers or []) == set(anterior.dhcp_relay_servers or [])
            else:
                verificado = getattr(actual_final, campo) == getattr(anterior, campo)
        except Exception:
            verificado = False
        return True, verificado

    # Mismo prefijo que agrega cada vendor al leer una regla de ACL (número
    # de secuencia Cisco -- "10 permit ...", o "rule N" Huawei) -- hace
    # falta pelarlo para volver a mandar la regla como input (mismo shape
    # que produce ``driver.formatear_regla_acl()``, sin el número que el
    # device asigna solo). Complementa a
    # ``GlobalConfig._SUFIJO_CONTADOR_RE`` (pela el contador de hits del
    # otro extremo de la línea).
    _PREFIJO_REGLA_ACL_RE = re.compile(r"^\s*(?:rule\s+)?\d+\s+")
    _NUMERO_PREFIJO_REGLA_ACL_RE = re.compile(r"^\s*(?:rule\s+)?(\d+)\s+")

    def _pelar_regla_acl(self, gc_cls, raw_line: str) -> str:
        sin_contador = gc_cls._SUFIJO_CONTADOR_RE.sub("", raw_line)
        return self._PREFIJO_REGLA_ACL_RE.sub("", sin_contador).strip()

    def _regla_con_secuencia_original(self, gc_cls, bare: str, previas: list[str]) -> str:
        """Busca en *previas* (líneas crudas, con el prefijo que agrega el
        device al leer) la que corresponde a *bare* (el mismo shape que
        produce ``formatear_regla_acl()``) y devuelve *bare* con su número
        de secuencia ORIGINAL antepuesto -- ver el comentario en la rama
        ``acl_rule_remove`` de ``_rollback_global_config()`` para el motivo
        real (evitar que la regla restaurada quede después de un
        catch-all y nunca se evalúe). Si no encuentra match (no debería
        pasar, ``sacadas_input`` ya filtró por presencia), devuelve *bare*
        tal cual -- se auto-asigna al final, mismo comportamiento que
        antes de este fix."""
        for raw in previas:
            sin_contador = gc_cls._SUFIJO_CONTADOR_RE.sub("", raw)
            if sin_contador.rstrip().endswith(bare):
                m = self._NUMERO_PREFIJO_REGLA_ACL_RE.match(sin_contador)
                if m:
                    return f"{m.group(1)} {bare}"
                break
        return bare

    def _rollback_global_config(
        self, gc: "GlobalConfig", pre_state: dict, device: "Device",
    ) -> tuple[bool, "bool | None"]:
        """Ver ``_rollback_svi()`` -- mismo bug real (``_rollback()`` no
        tenía rama para ``tipo == "global_config"``, GlobalConfig nunca se
        revertía). A diferencia de VLAN/Puerto/SVI, varios campos acá son
        deltas incrementales (``_add``/``_remove`` sobre una lista, no un
        "set a X") -- revertir un ``X_add`` es un ``remove_X`` del mismo
        valor y viceversa, no hay "valor anterior" que restaurar en el
        sentido de los otros tipos.

        2 campos NO son revertibles con la información que tenemos y se
        dejan explícitamente como no-op (``return False, None``) en vez de
        adivinar:
        - ``dns_domain_set``: no existe lectura de domain-name en ningún
          driver (ver docstring de ``GlobalConfig._aplicar_dns_domain()``)
          -- no hay valor previo posible de recuperar.
        - ``snmp_config`` sub-campos ``trap_source``/``trap_host``: no hay
          campo de solo-lectura para ``trap_source``, y agregar un
          ``trap_host`` no tiene contraparte "remove" en el driver -- se
          revierten ``version``/``community`` (sí legibles) cuando son
          parte del cambio, el resto queda aplicado.

        Las 3 ramas de ACL (``acl_create``/``acl_rule_remove``/
        ``acl_delete``) reconstruyen las reglas a re-aplicar pelando el
        prefijo/contador que el device agrega al leerlas (ver
        ``_pelar_regla_acl()``) -- son las de mayor riesgo de las 14 (la
        regla viaja como texto ya formateado, no como el dict estructurado
        original), confirmadas en vivo antes de darlas por buenas."""
        if not pre_state.get("existed"):
            return False, None
        anterior = pre_state.get("actual")
        if anterior is None:
            return False, None

        from app.models.global_config import GlobalConfig

        campos = gc.mutation_fields
        if len(campos) != 1:
            return False, None
        campo = next(iter(campos))
        try:
            if campo == "hostname":
                if not anterior.hostname or anterior.hostname == gc.hostname:
                    return False, None
                resultado = device.driver.set_hostname(anterior.hostname, device, device.password)
            elif campo == "snmp_config":
                cambios = {}
                if gc.snmp_config.get("version") is not None and anterior.snmp_version is not None:
                    cambios["version"] = anterior.snmp_version
                if gc.snmp_config.get("community") is not None and anterior.snmp_community is not None:
                    cambios["community"] = anterior.snmp_community
                rcs = []
                if cambios:
                    rcs.append(device.driver.set_snmp(cambios, device, device.password).get("rc", 1))
                # trap_host es un campo aparte de version/community -- se
                # agregó de nuevo (no existía en anterior), revertir es
                # sacarlo. Reusa la MISMA community que se mandó en esta
                # request (o la general del device si no vino) -- mismo
                # cálculo que ``GlobalConfig._aplicar_snmp_config()`` ya
                # hace para construir ese trap host.
                trap_host = gc.snmp_config.get("trap_host")
                if trap_host is not None and trap_host not in (anterior.snmp_trap_hosts or []):
                    community_usada = gc.snmp_config.get("community") or anterior.snmp_community
                    if community_usada:
                        rcs.append(
                            device.driver.remove_snmp_trap_host(
                                trap_host, device, device.password, community=community_usada,
                            ).get("rc", 1)
                        )
                if not rcs:
                    return False, None
                resultado = {"rc": 0 if all(rc == 0 for rc in rcs) else 1}
            elif campo == "snmp_trap_host_remove":
                # Sin no-op check contra anterior.snmp_trap_hosts a
                # propósito -- mismo motivo que
                # ``GlobalConfig._aplicar_snmp_trap_host_remove()`` (ver su
                # docstring): en Huawei esa lista viene de la ACL del
                # agente, no del target-host real que este mecanismo
                # agrega/saca, así que un host manejado 100% por
                # target-host nunca aparecería ahí. Re-agregar directo con
                # la MISMA community que se mandó para sacarlo (tuvo que
                # ser la correcta para que el remove original haya
                # funcionado) es seguro -- si por algún motivo el remove
                # original nunca llegó a aplicar, esto en el peor caso es
                # un re-add redundante, no rompe nada.
                resultado = device.driver.set_snmp(
                    {
                        "trap_host": gc.snmp_trap_host_remove["host"],
                        "trap_host_community": gc.snmp_trap_host_remove["community"],
                        "trap_version": anterior.snmp_version or "2c",
                    },
                    device, device.password,
                )
            elif campo == "route_add":
                destino = str(ipaddress.ip_network(gc.route_add["destination"], strict=False))
                next_hop = gc.route_add["next_hop"]
                ya_existia = any(
                    r.get("destination") == destino and r.get("next_hop") == next_hop
                    for r in (anterior.routes or [])
                )
                if ya_existia:
                    return False, None
                resultado = device.driver.remove_route(destino, next_hop, device, device.password)
            elif campo == "route_remove":
                destino = str(ipaddress.ip_network(gc.route_remove["destination"], strict=False))
                next_hop = gc.route_remove["next_hop"]
                existia = any(
                    r.get("destination") == destino and r.get("next_hop") == next_hop
                    for r in (anterior.routes or [])
                )
                if not existia:
                    return False, None
                resultado = device.driver.set_route(destino, next_hop, device, device.password)
            elif campo == "ntp_server_add":
                resultado = device.driver.remove_ntp_server(gc.ntp_server_add["server"], device, device.password)
            elif campo == "ntp_server_remove":
                resultado = device.driver.add_ntp_server(
                    gc.ntp_server_remove["server"], False, device, device.password,
                )
            elif campo == "dns_server_add":
                resultado = device.driver.remove_dns_server(gc.dns_server_add["server"], device, device.password)
            elif campo == "dns_server_remove":
                resultado = device.driver.add_dns_server(gc.dns_server_remove["server"], device, device.password)
            elif campo == "dns_domain_set":
                return False, None
            elif campo == "log_server_add":
                resultado = device.driver.remove_log_server(gc.log_server_add["server"], device, device.password)
            elif campo == "log_server_remove":
                resultado = device.driver.add_log_server(
                    gc.log_server_remove["server"], None, device, device.password,
                )
            elif campo == "acl_create":
                name = gc.acl_create["name"]
                existia_antes = bool(anterior.acls) and any(a.get("name") == name for a in anterior.acls)
                if not existia_antes:
                    resultado = device.driver.delete_acl(name, device, device.password)
                else:
                    previas = GlobalConfig._reglas_acl_actuales(anterior, name)
                    agregadas = [
                        formateada for r in gc.acl_create["rules"]
                        if not GlobalConfig._regla_ya_presente(
                            formateada := device.driver.formatear_regla_acl(r), previas,
                        )
                    ]
                    if not agregadas:
                        return False, None
                    resultado = device.driver.remove_acl_rules(name, agregadas, device, device.password)
            elif campo == "acl_rule_remove":
                name = gc.acl_rule_remove["name"]
                previas = GlobalConfig._reglas_acl_actuales(anterior, name)
                sacadas_input = [
                    r for r in gc.acl_rule_remove["rules"]
                    if GlobalConfig._regla_ya_presente(device.driver.formatear_regla_acl(r), previas)
                ]
                if not sacadas_input:
                    return False, None
                # Sin número de secuencia, IOS/VRP auto-asignan al final --
                # bug real encontrado probando esto: si la ACL tiene un
                # "deny ip any any" (u otro catch-all) antes de esa
                # posición, la regla restaurada queda inalcanzable (el
                # catch-all la corta antes de que evalúe nunca). Reinsertar
                # con su número de secuencia ORIGINAL (extraído de
                # *previas*, ver ``_numero_secuencia_regla_acl()``) hace
                # que quede exactamente donde estaba -- Cisco acepta el
                # número como prefijo directo del texto de la regla; VRP
                # también, porque ``create_or_update_acl()`` antepone
                # "rule " a cada línea, entonces "{N} permit ..." termina
                # como "rule {N} permit ...", la sintaxis real de VRP para
                # insertar en una posición puntual.
                rule_lines = [
                    self._regla_con_secuencia_original(
                        GlobalConfig, device.driver.formatear_regla_acl(r), previas,
                    )
                    for r in sacadas_input
                ]
                resultado = device.driver.create_or_update_acl(name, rule_lines, device, device.password)
            elif campo == "acl_delete":
                previas = GlobalConfig._reglas_acl_actuales(anterior, gc.acl_delete)
                if not previas:
                    return False, None
                rule_lines = [self._pelar_regla_acl(GlobalConfig, r) for r in previas]
                resultado = device.driver.create_or_update_acl(gc.acl_delete, rule_lines, device, device.password)
            else:
                return False, None
            exitoso = resultado.get("rc", 1) == 0
        except Exception:
            return True, False

        if not exitoso:
            return True, False

        # Las 3 ramas de ACL no comparten 1 sola forma de "verificar" --
        # cada una espera algo distinto del estado final (ACL borrada del
        # todo / ciertas reglas ausentes / ciertas reglas presentes de
        # nuevo) -- bug real encontrado probando esto: un chequeo genérico
        # de "¿la ACL existe?" reportaba rollback_success=False para el
        # caso "ACL nueva, revertir = borrarla" (donde NO existir es el
        # resultado CORRECTO), aunque el borrado hubiera funcionado bien
        # en el device real. Se verifica acá mismo, con el contexto de
        # cada rama todavía en scope, en vez de un helper genérico ciego a
        # qué caso es cada uno.
        try:
            actual_final = device.driver.get_global_config(device, device.password)
            if actual_final is None:
                verificado = False
            elif campo == "acl_create":
                if not existia_antes:
                    verificado = not (
                        actual_final.acls and any(a.get("name") == name for a in actual_final.acls)
                    )
                else:
                    reglas_ahora = GlobalConfig._reglas_acl_actuales(actual_final, name)
                    verificado = not any(
                        GlobalConfig._regla_ya_presente(r, reglas_ahora) for r in agregadas
                    )
            elif campo == "acl_rule_remove":
                # rule_lines acá trae el número de secuencia original
                # (ver el bloque de arriba) -- _regla_ya_presente() espera
                # la forma SIN prefijo (mismo shape que devuelve
                # formatear_regla_acl()), por eso se re-deriva de
                # sacadas_input en vez de reusar rule_lines directo.
                reglas_ahora = GlobalConfig._reglas_acl_actuales(actual_final, name)
                verificado = all(
                    GlobalConfig._regla_ya_presente(device.driver.formatear_regla_acl(r), reglas_ahora)
                    for r in sacadas_input
                )
            elif campo == "acl_delete":
                existe_ahora = bool(actual_final.acls) and any(
                    a.get("name") == gc.acl_delete for a in actual_final.acls
                )
                reglas_ahora = GlobalConfig._reglas_acl_actuales(actual_final, gc.acl_delete) if existe_ahora else []
                verificado = existe_ahora and all(
                    GlobalConfig._regla_ya_presente(r, reglas_ahora) for r in rule_lines
                )
            else:
                verificado = self._verificar_rollback_global_config(campo, gc, anterior, actual_final)
        except Exception:
            verificado = False
        return True, verificado

    def _verificar_rollback_global_config(self, campo: str, gc: "GlobalConfig", anterior, actual_final) -> bool:
        if actual_final is None:
            return False
        if campo == "hostname":
            return actual_final.hostname == anterior.hostname
        if campo == "snmp_config":
            trap_host = gc.snmp_config.get("trap_host")
            trap_ok = (
                trap_host is None
                or trap_host in (anterior.snmp_trap_hosts or [])
                or trap_host not in (actual_final.snmp_trap_hosts or [])
            )
            return (
                actual_final.snmp_version == anterior.snmp_version
                and actual_final.snmp_community == anterior.snmp_community
                and trap_ok
            )
        if campo == "snmp_trap_host_remove":
            # No se puede verificar releyendo -- mismo motivo que el no-op
            # check que se sacó de _aplicar_snmp_trap_host_remove() (ver su
            # docstring): en Huawei actual_final.snmp_trap_hosts viene de
            # la ACL del agente, nunca del target-host real que este
            # mecanismo re-agrega -- confirmado en vivo que el re-add
            # funciona perfecto contra el device real pero esta lista
            # nunca lo refleja. Se confía en el rc del driver (ya
            # verificado que reporta correcto en los 2 sentidos, ver
            # pruebas en vivo de esta misma sesión) en vez de una
            # relectura que sabemos que da falso negativo acá.
            return True
        if campo in ("route_add", "route_remove"):
            return (actual_final.routes or []) == (anterior.routes or [])
        if campo == "ntp_server_add":
            return gc.ntp_server_add["server"] not in (actual_final.ntp_servers or [])
        if campo == "ntp_server_remove":
            return gc.ntp_server_remove["server"] in (actual_final.ntp_servers or [])
        if campo == "dns_server_add":
            return gc.dns_server_add["server"] not in (actual_final.dns_servers or [])
        if campo == "dns_server_remove":
            return gc.dns_server_remove["server"] in (actual_final.dns_servers or [])
        if campo == "log_server_add":
            return gc.log_server_add["server"] not in (actual_final.log_servers or [])
        if campo == "log_server_remove":
            return gc.log_server_remove["server"] in (actual_final.log_servers or [])
        # acl_create/acl_rule_remove/acl_delete NO llegan acá -- se
        # verifican inline en _rollback_global_config(), donde todavía
        # está en scope el contexto de cada rama (ver su comentario).
        return False
