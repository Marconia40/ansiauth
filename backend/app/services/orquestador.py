import time

from app.core.exceptions import DeviceExecutionError, NotFoundError
from app.models.domain_event import DomainEvent
from app.models.retry_decision import RetryDecision

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


class Orquestador:
    """FINAL_ARCHITECTURE.md §2.4 -- Template Method por composición, no
    herencia. No sabe qué es una VLAN ni un Puerto, solo el contrato
    RecursoGestionable (4 métodos)."""

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

        pre_state = None
        try:
            job.marcar_iniciado()
            self._jobs.add(job)
            with self._coordinador.bloquear(device_name):
                self._coordinador.limitar(device_name)
                recurso.validar()
                pre_state = recurso.reconciliar(device)
                resultado, retry_count = self._ejecutar_con_retry(
                    lambda: recurso.aplicar(device), job, device_name,
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
            self._repos[recurso.repositorio()].add(recurso)
            job.marcar_completado(resultado)
            self._jobs.add(job)
            self._eventos.despachar([DomainEvent("recurso_aplicado", recurso, device, actor, resultado)])
        finally:
            if not job.esta_en_estado_terminal():
                job.asegurar_estado_final()
                self._jobs.add(job)

    def ejecutar_comando(self, fn, device_name: str, actor: str, job: "Job", tipo_evento: str) -> None:
        """Variante de ejecutar() para comandos a nivel device que no son un
        RecursoGestionable -- Fase 7, encontrado con ``POST /devices/{name}/
        save`` (guardar configuración): no hay ``validar()``/``reconciliar()``/
        ``aplicar()`` ni un recurso con identidad que persistir en un
        Repository (`self._repos[...].add(...)` no aplica acá, es la única
        diferencia real con ``ejecutar()``) -- pero sí hace falta el mismo
        lock por device, la misma clasificación de errores/reintentos, y el
        mismo manejo de estado del Job. Reusa ``_coordinador``/
        ``_ejecutar_con_retry`` (privados de esta clase) en vez de duplicar
        esa lógica en un módulo aparte.

        *fn* recibe el ``Device`` ya resuelto (``fn(device) -> dict``) --
        distinto de ``ejecutar()``, que arma el callable con el recurso ya
        cerrado sobre `device` desde afuera.

        El guard ``esta_en_estado_terminal()`` -- corrección real encontrada
        vía un chequeo de trazabilidad RNF-API-05 (idempotencia): faltaba
        acá, aunque ``ejecutar()`` (arriba) sí lo tiene desde Fase 4/5. Sin
        él, una reentrega de Celery sobre un job ya completado llamaba
        ``job.marcar_iniciado()`` de nuevo -- `_TRANSICIONES_VALIDAS`
        (`models/job.py`) no permite `completed`/`failed` -> `running`, así
        que la reentrega no era un no-op silencioso sino un crash real
        (`TransicionInvalidaError`)."""
        if job.esta_en_estado_terminal():
            return
        device = self._device_repo.get(device_name)
        if device is None:
            raise NotFoundError(device_name)
        try:
            job.marcar_iniciado()
            self._jobs.add(job)
            with self._coordinador.bloquear(device_name):
                self._coordinador.limitar(device_name)
                resultado, _ = self._ejecutar_con_retry(lambda: fn(device), job, device_name)
                if resultado.get("rc", 0) != 0:
                    raise DeviceExecutionError(resultado.get("stderr") or resultado.get("stdout") or "Execution failed")
        except Exception as error:
            job.marcar_fallido(str(error))
            self._jobs.add(job)
            self._eventos.despachar([DomainEvent(
                tipo_evento, device, device, actor, {"error": str(error)}, exitoso=False,
            )])
            raise
        else:
            job.marcar_completado(resultado)
            self._jobs.add(job)
            self._eventos.despachar([DomainEvent(tipo_evento, device, device, actor, resultado)])
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
        if not pre_state.get("existed"):
            return False, None
        anterior = pre_state.get("actual")
        if anterior is None:
            return False, None

        campos = puerto.mutation_fields
        try:
            if len(campos) > 1:
                from app.models.port import Puerto as _Puerto
                restaurar = _Puerto(
                    interface=puerto.interface,
                    device=puerto.device,
                    description=anterior.description if "description" in campos else None,
                    admin_up=anterior.admin_up if "admin_up" in campos else None,
                    mode=anterior.mode if "mode" in campos else None,
                    access_vlan=anterior.access_vlan if "access_vlan" in campos else None,
                    allowed_vlans=anterior.allowed_vlans if "allowed_vlans" in campos else None,
                    poe_enabled=anterior.poe_enabled if "poe_enabled" in campos else None,
                )
                if not restaurar.mutation_fields:
                    return False, None
                resultado = device.driver.configure_port(restaurar, device, device.password)
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
