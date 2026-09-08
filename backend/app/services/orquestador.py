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
# explicar mejor los errores de device").
_PATRONES_PERMANENTES: tuple[tuple[str, str], ...] = (
    ("invalid vlan id", "syntax"),
    ("incomplete command", "syntax"),
    ("syntax error", "syntax"),
    ("vlan already exists", "conflict"),
    ("permission denied", "auth"),
    ("authentication failure", "auth"),
    ("authentication failed", "auth"),
    ("unsupported command", "syntax"),
    ("invalid input", "syntax"),
    ("invalid command", "syntax"),
    ("authorization failed", "auth"),
    ("access denied", "auth"),
    ("ambiguous command", "syntax"),
    ("bad command", "syntax"),
    ("error: invalid", "syntax"),
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
                        resultado_prestate.get("stderr")
                        or resultado_prestate.get("stdout")
                        or "Prestate read failed",
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

                resultado, retry_count = self._ejecutar_con_retry(
                    _aplicar, job, device_name, max_retries=job.max_retries,
                )
                if resultado.get("rc", 0) != 0:
                    decision = self._clasificar_error(resultado)
                    raise DeviceExecutionError(
                        resultado.get("stderr") or resultado.get("stdout") or "Execution failed",
                        resumen=_resumir_error(decision),
                    )
        except Exception as error:
            rb_performed, rb_success = (False, None)
            if pre_state is not None:
                # Rollback bajo retry corto: si el revert falla por un timeout
                # transitorio (ej. la sesión SSH que quedó abierta se corta
                # justo cuando vamos a revertir), un segundo intento suele
                # ser suficiente. Budget de 1 retry para no bloquear el lock
                # del device por más de lo mínimo -- el device ya está en un
                # estado inconsistente y otros jobs esperan atrás. La
                # semántica de _rollback() se preserva: retorna
                # (performed, success), guardado por closure.
                rb_state = {"performed": False, "success": None}

                def _hacer_rollback():
                    rb_p, rb_s = self._rollback(recurso, pre_state, device)
                    rb_state["performed"] = rb_p
                    rb_state["success"] = rb_s
                    # No-op (nada que revertir) o éxito verificado -> rc=0,
                    # así no gastamos el retry. Sólo (True, False) -- se
                    # intentó revertir pero la verificación falló -- se
                    # trata como retryable.
                    if not rb_p:
                        return {"rc": 0, "stdout": "rollback no-op", "stderr": ""}
                    if rb_s is False:
                        return {"rc": 1, "stdout": "", "stderr": "rollback verification failed"}
                    return {"rc": 0, "stdout": "rollback ok", "stderr": ""}

                with self._coordinador.bloquear(device_name):
                    self._ejecutar_con_retry(
                        _hacer_rollback, job, device_name, max_retries=1,
                    )
                rb_performed = rb_state["performed"]
                rb_success = rb_state["success"]
            job.marcar_fallido(
                str(error), rb_performed, rb_success,
                error_summary=getattr(error, "resumen", None),
            )
            self._jobs.add(job)
            self._eventos.despachar([DomainEvent(
                "recurso_fallido", recurso, device, actor,
                {
                    "error": str(error), "rollback_performed": rb_performed, "rollback_success": rb_success,
                    "error_summary": getattr(error, "resumen", None),
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
                        resultado_prestate.get("stderr")
                        or resultado_prestate.get("stdout")
                        or "Prestate read failed",
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

                resultado, retry_count = self._ejecutar_con_retry(
                    _aplicar, job, device_name, max_retries=job.max_retries,
                )
                if resultado.get("rc", 0) != 0:
                    decision = self._clasificar_error(resultado)
                    raise DeviceExecutionError(
                        resultado.get("stderr") or resultado.get("stdout") or "Execution failed",
                        resumen=_resumir_error(decision),
                    )
        except Exception as error:
            rb_performed_total = False
            rb_resultados: list[tuple[bool, "bool | None"]] = []
            if pre_states is not None:
                with self._coordinador.bloquear(device_name):
                    for recurso, estado in zip(recursos, pre_states):
                        try:
                            rb_resultados.append(self._rollback(recurso, estado, device))
                        except Exception:
                            logger.exception(
                                "ejecutar_lote: rollback failed for one entry, "
                                "continuing with the rest, device=%s", device_name,
                            )
                            rb_resultados.append((True, False))
            rb_performed_total = any(p for p, _s in rb_resultados)
            rb_success_total = (
                all(s is not False for _p, s in rb_resultados if _p) if rb_resultados else None
            )
            job.marcar_fallido(
                str(error), rb_performed_total, rb_success_total,
                error_summary=getattr(error, "resumen", None),
            )
            self._jobs.add(job)
            self._eventos.despachar([DomainEvent(
                "recurso_fallido", recursos[0], device, actor,
                {"error": str(error), "rollback_performed": rb_performed_total, "rollback_success": rb_success_total,
                 "lote_size": len(recursos), "error_summary": getattr(error, "resumen", None)},
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
        ``Puerto.aplicar()`` (``puerto.mode`` primero, después el único
        campo restante) -- corrección real: la versión anterior decidía
        acá con `len(campos) > 1`, mientras `aplicar()`/`Puerto._es_composite`
        (ya retirada) decidían con `len(campos) > 1 or "mode" in campos`.
        Un `Puerto(mode="trunk")` de un solo campo caía acá en la rama de
        UN campo, que no tenía caso para `"mode"`, y el rollback quedaba
        como no-op silencioso (`return False, None`) aunque `aplicar()` sí
        había cambiado el device. Ya no hay heurística de conteo de
        campos que pueda desalinearse -- ambos lados miran `puerto.mode`
        directo."""
        if not pre_state.get("existed"):
            return False, None
        anterior = pre_state.get("actual")
        if anterior is None:
            return False, None

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
                else:
                    return False, None
        except Exception:
            return True, False

        if not exitoso:
            return True, False

        try:
            puertos = device.driver.list_ports(device, device.password)
            actual = next((p for p in puertos if p.interface == puerto.interface), None)
            verificado = actual is not None and all(
                getattr(actual, c) == getattr(anterior, c) for c in campos if hasattr(anterior, c)
            )
        except Exception:
            verificado = False
        return True, verificado

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
