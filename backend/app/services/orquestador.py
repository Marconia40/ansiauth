import logging
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
    "end of file",
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
                pre_state = recurso.reconciliar(device)
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
                    raise DeviceExecutionError(resultado.get("stderr") or resultado.get("stdout") or "Execution failed")
        except Exception as error:
            rb_performed, rb_success = (False, None)
            if pre_state is not None:
                with self._coordinador.bloquear(device_name):
                    rb_performed, rb_success = self._rollback(recurso, pre_state, device)
            job.marcar_fallido(str(error), rb_performed, rb_success)
            self._jobs.add(job)
            self._eventos.despachar([DomainEvent(
                "recurso_fallido", recurso, device, actor,
                {"error": str(error), "rollback_performed": rb_performed, "rollback_success": rb_success},
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
            _SCOPE_POR_REPO = {"vlan": "vlans", "puerto": "ports"}
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
        return RetryDecision(should_retry=False, classification="permanent", reason="unknown error — defaulting to permanent")

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
                resultado = {"rc": 1, "stdout": "", "stderr": str(exc)}
            if resultado.get("rc") == 0:
                break
            decision = self._clasificar_error(resultado)
            if not decision.should_retry or intento >= max_retries:
                break
            delay = min(retry_base_delay * (2 ** intento), _MAX_RETRY_DELAY)
            retry_count += 1
            error_texto = (resultado.get("stderr") or "") + " " + (resultado.get("stdout") or "")
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
