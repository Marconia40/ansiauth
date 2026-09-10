"""Tests de rollback contra la arquitectura actual (Orquestador/
RecursoGestionable, ver docs/FINAL_ARCHITECTURE.md) -- reemplaza
test_rollback.py/test_rollback_hardened.py/test_huawei_port_driver_write.py
(borrados: importaban vlan_service/audit_service/vendors.huawei.port_driver,
módulos que ya no existen tras la migración).

Unit tests directos sobre los modelos de dominio y sobre Orquestador, con
un FakeDriver/FakeDevice mínimo por test -- mismo nivel que se usó para
verificar en vivo cada uno de los bugs reales de esta sesión (no
MockVendor: ese driver está pensado para EXECUTION_MODE=mock end-to-end,
no implementa los métodos resolver_*/batch que hacen falta acá). Sin
FastAPI TestClient, sin DB.

Cada clase de test replica un bug real encontrado en vivo esta sesión:
- TestNoOpRollback / TestRevertRealAplicado: bug #3 (SVI ACL revertido sin
  que nada se hubiera aplicado -- comando roto armado igual).
- TestRollbackLoteDosFases / TestRetryRollbackDosFases: bug #4 (falta de
  verificación per-recurso, reporte "completed" sin chequear si el device
  realmente quedó como el snapshot).
- TestRuidoBenigno: bugs #1/#2 (prompt Y/N sin responder -> falso
  "completed"; placeholder rechazado -> falso "failed").
- TestGlobalConfigNoOpRollback: misma clase que el bug #3 (revertir algo
  que nunca se aplicó), encontrada por auditoría en 5 ramas más de
  GlobalConfig.ejecutar_rollback() (route_add, ntp_server_add,
  dns_server_add, log_server_add, acl_create) -- ninguna rota en vivo
  todavía, arregladas preventivamente antes de que pasara.
- TestSVIIpv4SecondaryRollback: misma clase de nuevo, en
  SVI.ejecutar_rollback()/resolver_rollback() (ipv4_secondary_add) --
  Puerto se auditó también (limpio, ningún campo usa remove_*/undo_*,
  todos son set_* idempotentes). De paso, resolver_rollback() no tenía
  NINGUNA rama para ipv4_secondary_add/remove (gap de cobertura
  distinto, no revertía nada en el camino batcheado) y su verificar()
  genérico hubiera dado falso positivo para estos 2 campos.
"""
from __future__ import annotations

import contextlib

from app.models.device import Device
from app.models.global_config import GlobalConfig
from app.models.job import Job
from app.models.port import Puerto
from app.models.svi import SVI
from app.services import ssh_direct_service
from app.services.orquestador import Orquestador


# ── Helpers ──────────────────────────────────────────────────────────────────

def _device(vendor: str = "huawei_vrp") -> Device:
    d = Device(
        name="f3r9s2", host="192.0.2.50", vendor=vendor,
        username="admin", encrypted_password="x", platform="vrp",
    )
    d._driver = object()  # sobreescrito por cada test con su FakeDriver
    d._password = "fake-password"
    return d


class _FakeCoordinador:
    """Stub de RedisCoordinator -- retry_rollback() solo necesita
    bloquear()/limitar() para no tocar Redis en un test unitario."""

    @contextlib.contextmanager
    def bloquear(self, device_id: str, timeout=None):
        yield

    def limitar(self, device_id: str) -> None:
        pass


class _FakeRepo:
    """Stub de JobRepository -- solo se usa add(), sin persistir nada."""

    def __init__(self):
        self.added: list = []

    def add(self, job) -> None:
        self.added.append(job)

    def get(self, name):
        return None


class _FakeEventos:
    def __init__(self):
        self.despachados: list = []

    def despachar(self, eventos) -> None:
        self.despachados.extend(eventos)


def _orquestador(device: Device) -> Orquestador:
    """object.__new__() en vez de __init__ real -- _rollback_lote() no usa
    ningún colaborador inyectado (solo recurso.resolver_rollback() +
    device.driver), retry_rollback() sí necesita los 4 de abajo."""
    orq = object.__new__(Orquestador)
    orq._device_repo = _FakeDeviceRepo(device)
    orq._jobs = _FakeRepo()
    orq._eventos = _FakeEventos()
    orq._coordinador = _FakeCoordinador()
    return orq


class _FakeDeviceRepo:
    def __init__(self, device: Device):
        self._device = device

    def get(self, name: str):
        return self._device if name == self._device.name else None


# ── Bug #3: revertir un campo que NUNCA se aplicó no debe armar un comando ──

class TestNoOpRollback:
    def test_svi_acl_resolver_rollback_no_op_cuando_nada_se_aplico(self):
        """Réplica exacta del bug real: apply de acl_in rechazado antes de
        tocar el device -- anterior y actual_ahora ambos sin ACL atada.
        resolver_rollback() no debe llamar al driver en absoluto (antes
        armaba "undo traffic-filter ... acl name" sin nombre, VRP lo
        rechazaba con "Incomplete command")."""
        class FakeDriver:
            def resolver_set_svi_acl(self, *a, **k):
                raise AssertionError("no debería llamarse -- nada que revertir")

        device = _device()
        device._driver = FakeDriver()
        snap = SVI(vlan_id=50, device="f3r9s2", acl_in="dasfdf")
        pre_state = {"existed": True, "actual": SVI(vlan_id=50, device="f3r9s2", acl_in=None)}
        actual_ahora = SVI(vlan_id=50, device="f3r9s2", acl_in=None)

        pasos, verificar = snap.resolver_rollback(pre_state, device, actual_ahora)

        assert pasos == []
        assert verificar is None

    def test_svi_acl_resolver_restore_no_op_cuando_nada_se_aplico(self):
        """Mismo gap latente en resolver_restore() (usado por
        retry_rollback) -- no disparable hoy (los parsers nunca producen
        "" para acl_in/acl_out) pero cubierto por consistencia."""
        class FakeDriver:
            def resolver_set_svi_acl(self, *a, **k):
                raise AssertionError("no debería llamarse -- nada que revertir")

        device = _device()
        device._driver = FakeDriver()
        snap = SVI(vlan_id=50, device="f3r9s2", acl_in="")
        actual_ahora = SVI(vlan_id=50, device="f3r9s2", acl_in=None)

        pasos, verificar = snap.resolver_restore(device, actual_ahora)

        assert pasos == []

    def test_puerto_resolver_rollback_no_op_cuando_nada_cambio(self):
        """Puerto no tiene el mismo bug (ningún driver eager se auto-
        protege para storm-control/mode/etc, confirmado por auditoría),
        pero el contrato general de "nada que revertir -> sin pasos" debe
        seguir cumpliéndose: pre_state sin 'existed' -> no-op."""
        class FakeDriver:
            def resolver_set_access_mode(self, *a, **k):
                raise AssertionError("no debería llamarse")

        device = _device()
        device._driver = FakeDriver()
        snap = Puerto(interface="GE0/0/10", device="f3r9s2", mode="access", access_vlan=10)
        pre_state = {"existed": False, "actual": None}

        pasos, verificar = snap.resolver_rollback(pre_state, device)

        assert pasos == []
        assert verificar is None


# ── El caso inverso: sí hay algo que revertir -- debe generar el paso correcto ──

class TestRevertRealAplicado:
    def test_svi_acl_resolver_rollback_revierte_cuando_si_se_aplico(self):
        """El apply SÍ llegó a aplicarse (actual_ahora refleja el ACL
        nuevo) y hay que revertir al ACL anterior -- debe generar el paso,
        no degradar a no-op."""
        llamadas = []

        class FakeDriver:
            def resolver_set_svi_acl(self, vlan_id, direction, acl_name, current_acl_name=None):
                llamadas.append((vlan_id, direction, acl_name, current_acl_name))
                return ("set_svi_acl", "clear_named", {"vlan_id": vlan_id, "acl_name": acl_name})

        device = _device()
        device._driver = FakeDriver()
        snap = SVI(vlan_id=50, device="f3r9s2", acl_in="dasfdf")
        pre_state = {"existed": True, "actual": SVI(vlan_id=50, device="f3r9s2", acl_in="old-acl")}
        actual_ahora = SVI(vlan_id=50, device="f3r9s2", acl_in="dasfdf")

        pasos, verificar = snap.resolver_rollback(pre_state, device, actual_ahora)

        assert len(pasos) == 1
        assert llamadas == [(50, "in", "old-acl", "dasfdf")]
        assert verificar(SVI(vlan_id=50, device="f3r9s2", acl_in="old-acl")) is True
        assert verificar(SVI(vlan_id=50, device="f3r9s2", acl_in="dasfdf")) is False

    def test_puerto_ejecutar_rollback_revierte_trunk_con_vlans(self):
        """Réplica del bug #1/#2 original (puerto trunk-con-vlans -> access,
        rollback debe restaurar mode=trunk + allowed_vlans)."""
        class FakeDriver:
            def set_trunk_mode(self, iface, pvid, vlans, device, password):
                return {"rc": 0}
            def list_ports(self, device, password):
                return [Puerto(interface="GE0/0/10", device="f3r9s2", mode="trunk", access_vlan=10, allowed_vlans=[10, 20])]

        device = _device()
        device._driver = FakeDriver()
        puerto = Puerto(interface="GE0/0/10", device="f3r9s2", mode="access", access_vlan=1060)
        anterior = Puerto(interface="GE0/0/10", device="f3r9s2", mode="trunk", access_vlan=10, allowed_vlans=[10, 20])
        pre_state = {"existed": True, "actual": anterior}

        performed, success = puerto.ejecutar_rollback(pre_state, device)

        assert (performed, success) == (True, True)


# ── Bug #4: criterio de 2 fases en el rollback batcheado ─────────────────────

class TestRollbackLoteDosFases:
    def test_apply_falla_marca_todos_los_recursos_con_pasos_uniforme(self):
        class FakeDriver:
            def aplicar_lote(self, pasos, device, password, op_label=""):
                return {"rc": 1, "stdout": "", "stderr": "device rejected"}
            def resolver_set_svi_description(self, vlan_id, desc):
                return ("set_svi_description", None, {"vlan_id": vlan_id, "description": desc})
            def get_svis(self, device, password):
                # SVI.NECESITA_ESTADO_FRESCO_ROLLBACK dispara una lectura
                # fresca antes de planear -- no relevante para este test
                # (el apply falla antes de importar), pero hace falta el
                # stub para no ensuciar el output con la excepción
                # atrapada internamente por _rollback_lote().
                return []

        device = _device()
        device._driver = FakeDriver()
        orq = _orquestador(device)

        recursos = [
            SVI(vlan_id=50, device="f3r9s2", description="a"),
            SVI(vlan_id=51, device="f3r9s2", description="b"),
        ]
        pre_states = [
            {"existed": True, "actual": SVI(vlan_id=50, device="f3r9s2", description="old-a")},
            {"existed": True, "actual": SVI(vlan_id=51, device="f3r9s2", description="old-b")},
        ]

        resultados, error_rb = orq._rollback_lote(recursos, pre_states, device)

        assert resultados == [(True, False), (True, False)]
        assert error_rb is not None

    def test_apply_ok_pero_1_de_2_no_verifica(self):
        class FakeDriver:
            def aplicar_lote(self, pasos, device, password, op_label=""):
                return {"rc": 0, "stdout": "ok", "stderr": ""}
            def resolver_set_svi_description(self, vlan_id, desc):
                return ("set_svi_description", None, {"vlan_id": vlan_id, "description": desc})

        # get_svis: la vlan_id=50 SÍ quedó como el pre_state pedía, la
        # vlan_id=51 se aplicó pero no coincide con lo esperado (verify
        # debe fallar SOLO para esa).
        class ReadDriver(FakeDriver):
            def get_svis(self, device, password):
                return [
                    SVI(vlan_id=50, device="f3r9s2", description="old-a"),
                    SVI(vlan_id=51, device="f3r9s2", description="MISMATCH"),
                ]

        device = _device()
        device._driver = ReadDriver()
        orq = _orquestador(device)

        recursos = [
            SVI(vlan_id=50, device="f3r9s2", description="a"),
            SVI(vlan_id=51, device="f3r9s2", description="b"),
        ]
        pre_states = [
            {"existed": True, "actual": SVI(vlan_id=50, device="f3r9s2", description="old-a")},
            {"existed": True, "actual": SVI(vlan_id=51, device="f3r9s2", description="old-b")},
        ]

        resultados, error_rb = orq._rollback_lote(recursos, pre_states, device)

        assert resultados == [(True, True), (True, False)]
        assert error_rb is not None


# ── Bug #4 (retry_rollback): mismo criterio de 2 fases ────────────────────────

class TestRetryRollbackDosFases:
    def test_retry_rollback_falla_cuando_verificacion_no_coincide(self):
        """El apply del retry sale rc=0, pero la relectura posterior no
        coincide con el snapshot -- new_job debe terminar failed, no
        completed (antes de este fix, retry_rollback() no verificaba
        nada post-apply)."""
        class FakeDriver:
            def aplicar_lote(self, pasos, device, password, op_label=""):
                return {"rc": 0, "stdout": "ok", "stderr": ""}
            def get_svis(self, device, password):
                # Snapshot pedía description="restored", el device quedó
                # con otra cosa -- verificación debe fallar.
                return [SVI(vlan_id=50, device="f3r9s2", description="WRONG")]
            def resolver_set_svi_description(self, vlan_id, desc):
                return ("set_svi_description", None, {"vlan_id": vlan_id, "description": desc})

        device = _device()
        device._driver = FakeDriver()
        orq = _orquestador(device)

        original_job = Job(
            operation="svi", device="f3r9s2", max_retries=1,
            pre_state={"lote": [{
                "existed": True,
                "actual": SVI(vlan_id=50, device="f3r9s2", description="restored").to_dict(),
            }]},
        )
        new_job = Job(operation="retry_rollback", device="f3r9s2", max_retries=1)

        raised = False
        try:
            orq.retry_rollback(original_job, new_job, actor="tester")
        except Exception:
            raised = True

        assert raised is True
        assert new_job.status == "failed"

    def test_retry_rollback_completa_cuando_todo_verifica(self):
        class FakeDriver:
            def aplicar_lote(self, pasos, device, password, op_label=""):
                return {"rc": 0, "stdout": "ok", "stderr": ""}
            def get_svis(self, device, password):
                return [SVI(vlan_id=50, device="f3r9s2", description="restored")]
            def resolver_set_svi_description(self, vlan_id, desc):
                return ("set_svi_description", None, {"vlan_id": vlan_id, "description": desc})

        device = _device()
        device._driver = FakeDriver()
        orq = _orquestador(device)

        original_job = Job(
            operation="svi", device="f3r9s2", max_retries=1,
            pre_state={"lote": [{
                "existed": True,
                "actual": SVI(vlan_id=50, device="f3r9s2", description="restored").to_dict(),
            }]},
        )
        new_job = Job(operation="retry_rollback", device="f3r9s2", max_retries=1)

        orq.retry_rollback(original_job, new_job, actor="tester")

        assert new_job.status == "completed"


# ── Bugs #1/#2: ruido benigno en la determinación de rc/success ─────────────

class TestRuidoBenigno:
    def test_placeholder_y_rechazado_no_cuenta_como_error(self):
        raw = (
            "[f3r9s2-GigabitEthernet0/0/40]y\n"
            "                               ^\n"
            "Error: Unrecognized command found at '^' position."
        )
        assert ssh_direct_service._tiene_error(raw) is False

    def test_rechazo_real_de_sintaxis_si_cuenta_como_error(self):
        raw = "[f3r9s2]vlanx 99\n         ^\nError: Unrecognized command found at '^' position."
        assert ssh_direct_service._tiene_error(raw) is True

    def test_error_real_mezclado_con_ruido_benigno_si_cuenta(self):
        raw = (
            "[f3r9s2-GigabitEthernet0/0/40]y\n"
            "                               ^\n"
            "Error: Unrecognized command found at '^' position.\n"
            "% Invalid input detected"
        )
        assert ssh_direct_service._tiene_error(raw) is True

    def test_aviso_cisco_autocreate_no_gana_señal_transitoria_real(self):
        from app.services.orquestador import Orquestador

        inst = object.__new__(Orquestador)
        raw = (
            "Connection to 172.16.61.126 closed by remote host.\n"
            "% Access VLAN does not exist. Creating vlan 1050"
        )
        decision = Orquestador._clasificar_error(inst, {"rc": 1, "stdout": raw, "stderr": ""})
        assert decision.classification == "transient"
        assert decision.reason == "closed by remote host"

    def test_error_amigable_no_engancha_ruido_no_relacionado(self):
        """Bug real encontrado en vivo contra f3r9s1: '% 192.168.100.0 is
        assigned to Vlan10' (permanent/conflict, clasificado bien) +
        'Connection ... closed by remote host' en el MISMO transcript (el
        device corta la sesión justo después del rechazo). _MENSAJES_ERROR
        no tiene grupo para 'is assigned to' pero SÍ para 'closed by
        remote host' -- un rescan de texto libre enganchaba ese grupo
        aunque no tuviera nada que ver con el rechazo real."""
        from app.services.orquestador import Orquestador

        inst = object.__new__(Orquestador)
        raw = (
            "Connection to 172.16.61.126 closed by remote host.\n"
            "f3r9s1(config-if)#ip address 192.168.100.50 255.255.255.0 secondary\n"
            "% 192.168.100.0 is assigned to Vlan10"
        )
        friendly = inst._error_amigable(raw)
        assert friendly["error_type"] == "permanent"
        assert friendly["error_reason"] == "is assigned to"
        assert friendly["error_summary"] == "The requested change conflicts with the device's current configuration."

    def test_invalid_address_es_permanente_no_transitorio(self):
        """Bug real encontrado en vivo contra f3r9s1: 'ntp server
        255.255.255.255' -> '% Invalid address' -- rechazo determinístico
        que no matcheaba ningún patrón permanente, así que caía a
        transitorio por el 'closed by remote host' que lo acompaña en el
        mismo transcript. Costaba los 3 reintentos completos (~35s) en
        algo que iba a fallar igual las 4 veces."""
        from app.services.orquestador import Orquestador

        inst = object.__new__(Orquestador)
        raw = (
            "Connection to 172.16.61.126 closed by remote host.\n"
            "f3r9s1(config)#ntp server 255.255.255.255\n"
            "% Invalid address"
        )
        decision = Orquestador._clasificar_error(inst, {"rc": 1, "stdout": raw, "stderr": ""})
        assert decision.classification == "permanent"
        assert decision.should_retry is False
        assert decision.reason == "invalid address"

    def test_is_invalid_es_permanente_no_unknown(self):
        """Bug real encontrado en vivo contra f3r9s2 (Huawei): 'dns server
        255.255.255.255' -> 'Error: The specified IP address is invalid.'
        -- frase distinta de 'is not valid' (ya en la tabla) y de 'error:
        invalid' (que matchea la forma corta 'Invalid IP address.' usada
        por otros comandos del mismo device) -- caía a 'unknown' (1 retry
        extra al pedo)."""
        from app.services.orquestador import Orquestador

        inst = object.__new__(Orquestador)
        raw = "Error: The specified IP address is invalid."
        decision = Orquestador._clasificar_error(inst, {"rc": 1, "stdout": raw, "stderr": ""})
        assert decision.classification == "permanent"
        assert decision.should_retry is False
        assert decision.reason == "is invalid"

    def test_can_not_support_es_permanente_no_unknown(self):
        """Bug real encontrado en vivo contra f3r9s2 (Huawei): habilitar
        PoE en un puerto/modelo sin soporte -> 'Error: Interface
        GigabitEthernet0/0/19 can not support PoE.' -- rechazo de
        hardware, 100% determinístico, caía a 'unknown' (1 retry extra
        al pedo) porque no matcheaba 'unsupported command'."""
        from app.services.orquestador import Orquestador

        inst = object.__new__(Orquestador)
        raw = "Error: Interface GigabitEthernet0/0/19 can not support PoE."
        decision = Orquestador._clasificar_error(inst, {"rc": 1, "stdout": raw, "stderr": ""})
        assert decision.classification == "permanent"
        assert decision.should_retry is False
        assert decision.reason == "can not support"


# ── GlobalConfig: mismo tipo de bug que el de ACL, encontrado por auditoría ──

class TestGlobalConfigNoOpRollback:
    """5 ramas de ``GlobalConfig.ejecutar_rollback()`` con el mismo riesgo
    que ya rompió con SVI ACL: revertir un campo incremental (``_add``)
    cuyo apply original nunca llegó a tocar el device. Cada test confirma
    (a) que el caso "nunca se aplicó" no llama al driver de remove, y (b)
    que el caso "sí se aplicó" sigue revirtiendo de verdad."""

    def test_route_add_no_op_cuando_nunca_se_aplico(self):
        class FakeDriver:
            def get_global_config(self, device, password):
                return GlobalConfig(device="f3r9s1", routes=[])
            def remove_route(self, *a, **k):
                raise AssertionError("no debería llamarse -- nunca se aplicó")

        device = _device(); device._driver = FakeDriver()
        gc = GlobalConfig(device="f3r9s1", route_add={"destination": "10.10.10.0/24", "next_hop": "10.10.10.1"})
        pre_state = {"existed": True, "actual": GlobalConfig(device="f3r9s1", routes=[])}

        assert gc.ejecutar_rollback(pre_state, device) == (False, None)

    def test_route_add_revierte_cuando_si_se_aplico(self):
        class FakeDriver:
            def get_global_config(self, device, password):
                return GlobalConfig(device="f3r9s1", routes=[{"destination": "10.10.10.0/24", "next_hop": "10.10.10.1"}])
            def remove_route(self, destino, next_hop, device, password):
                return {"rc": 0}

        device = _device(); device._driver = FakeDriver()
        gc = GlobalConfig(device="f3r9s1", route_add={"destination": "10.10.10.0/24", "next_hop": "10.10.10.1"})
        pre_state = {"existed": True, "actual": GlobalConfig(device="f3r9s1", routes=[])}

        performed, _success = gc.ejecutar_rollback(pre_state, device)
        assert performed is True

    def test_ntp_server_add_no_op_cuando_nunca_se_aplico(self):
        class FakeDriver:
            def get_global_config(self, device, password):
                return GlobalConfig(device="f3r9s1", ntp_servers=[])
            def remove_ntp_server(self, *a, **k):
                raise AssertionError("no debería llamarse -- nunca se aplicó")

        device = _device(); device._driver = FakeDriver()
        gc = GlobalConfig(device="f3r9s1", ntp_server_add={"server": "10.0.0.1"})
        pre_state = {"existed": True, "actual": GlobalConfig(device="f3r9s1", ntp_servers=[])}

        assert gc.ejecutar_rollback(pre_state, device) == (False, None)

    def test_dns_server_add_no_op_cuando_nunca_se_aplico(self):
        class FakeDriver:
            def get_global_config(self, device, password):
                return GlobalConfig(device="f3r9s1", dns_servers=[])
            def remove_dns_server(self, *a, **k):
                raise AssertionError("no debería llamarse -- nunca se aplicó")

        device = _device(); device._driver = FakeDriver()
        gc = GlobalConfig(device="f3r9s1", dns_server_add={"server": "8.8.8.8"})
        pre_state = {"existed": True, "actual": GlobalConfig(device="f3r9s1", dns_servers=[])}

        assert gc.ejecutar_rollback(pre_state, device) == (False, None)

    def test_log_server_add_no_op_cuando_nunca_se_aplico(self):
        class FakeDriver:
            def get_global_config(self, device, password):
                return GlobalConfig(device="f3r9s1", log_servers=[])
            def remove_log_server(self, *a, **k):
                raise AssertionError("no debería llamarse -- nunca se aplicó")

        device = _device(); device._driver = FakeDriver()
        gc = GlobalConfig(device="f3r9s1", log_server_add={"server": "10.0.0.9"})
        pre_state = {"existed": True, "actual": GlobalConfig(device="f3r9s1", log_servers=[])}

        assert gc.ejecutar_rollback(pre_state, device) == (False, None)

    def test_acl_create_nueva_no_op_cuando_nunca_se_aplico(self):
        """Sub-caso "ACL no existía antes" -- revertir sería delete_acl()."""
        class FakeDriver:
            def get_global_config(self, device, password):
                return GlobalConfig(device="f3r9s1", acls=[])
            def delete_acl(self, *a, **k):
                raise AssertionError("no debería llamarse -- nunca se aplicó")

        device = _device(); device._driver = FakeDriver()
        gc = GlobalConfig(device="f3r9s1", acl_create={"name": "test-acl", "rules": [{"action": "permit"}]})
        pre_state = {"existed": True, "actual": GlobalConfig(device="f3r9s1", acls=[])}

        assert gc.ejecutar_rollback(pre_state, device) == (False, None)

    def test_acl_create_nueva_revierte_cuando_si_se_aplico(self):
        class FakeDriver:
            def get_global_config(self, device, password):
                return GlobalConfig(device="f3r9s1", acls=[{"name": "test-acl", "rules": []}])
            def delete_acl(self, name, device, password):
                return {"rc": 0}

        device = _device(); device._driver = FakeDriver()
        gc = GlobalConfig(device="f3r9s1", acl_create={"name": "test-acl", "rules": [{"action": "permit"}]})
        pre_state = {"existed": True, "actual": GlobalConfig(device="f3r9s1", acls=[])}

        performed, _success = gc.ejecutar_rollback(pre_state, device)
        assert performed is True


# ── SVI ipv4_secondary_add: mismo tipo de bug que ACL, encontrado por auditoría ──

class TestSVIIpv4SecondaryRollback:
    def test_ejecutar_rollback_no_op_cuando_ip_nunca_se_aplico(self):
        class FakeDriver:
            def get_svis(self, device, password):
                return [SVI(vlan_id=50, device="f3r9s2", ipv4_address_secondary=[])]
            def set_svi_ipv4_secondary(self, *a, **k):
                raise AssertionError("no debería llamarse -- nunca se aplicó")

        device = _device(); device._driver = FakeDriver()
        snap = SVI(vlan_id=50, device="f3r9s2", ipv4_secondary_add="172.20.1.5/24")
        pre_state = {"existed": True, "actual": SVI(vlan_id=50, device="f3r9s2", ipv4_address_secondary=[])}

        assert snap.ejecutar_rollback(pre_state, device) == (False, None)

    def test_ejecutar_rollback_revierte_cuando_ip_si_se_aplico(self):
        class FakeDriver:
            def get_svis(self, device, password):
                return [SVI(vlan_id=50, device="f3r9s2", ipv4_address_secondary=["172.20.1.5/24"])]
            def set_svi_ipv4_secondary(self, vlan_id, ip, prev, device, password):
                return {"rc": 0}

        device = _device(); device._driver = FakeDriver()
        snap = SVI(vlan_id=50, device="f3r9s2", ipv4_secondary_add="172.20.1.5/24")
        pre_state = {"existed": True, "actual": SVI(vlan_id=50, device="f3r9s2", ipv4_address_secondary=[])}

        performed, _success = snap.ejecutar_rollback(pre_state, device)
        assert performed is True

    def test_resolver_rollback_no_op_cuando_ip_nunca_se_aplico(self):
        """Antes de este fix, resolver_rollback() no tenía NINGUNA rama
        para ipv4_secondary_add -- caía al else genérico y nunca
        revertía nada en el camino batcheado. Confirma que ahora sí
        existe la rama Y que respeta el guard de estado fresco."""
        class FakeDriver:
            def resolver_set_svi_ipv4_secondary(self, *a, **k):
                raise AssertionError("no debería llamarse -- nunca se aplicó")

        device = _device(); device._driver = FakeDriver()
        snap = SVI(vlan_id=50, device="f3r9s2", ipv4_secondary_add="172.20.1.5/24")
        pre_state = {"existed": True, "actual": SVI(vlan_id=50, device="f3r9s2", ipv4_address_secondary=[])}
        actual_ahora = SVI(vlan_id=50, device="f3r9s2", ipv4_address_secondary=[])

        pasos, verificar = snap.resolver_rollback(pre_state, device, actual_ahora)
        assert pasos == []

    def test_resolver_rollback_revierte_y_verifica_correctamente(self):
        """Confirma el paso real Y que verificar() no da falso positivo
        (bug real: el fallback genérico comparaba un campo de REQUEST,
        siempre None==None -> True, sin importar si el revert funcionó)."""
        class FakeDriver:
            def resolver_set_svi_ipv4_secondary(self, vlan_id, ip, prev):
                return ("set_svi_ipv4_secondary", "clear", {"vlan_id": vlan_id})

        device = _device(); device._driver = FakeDriver()
        snap = SVI(vlan_id=50, device="f3r9s2", ipv4_secondary_add="172.20.1.5/24")
        pre_state = {"existed": True, "actual": SVI(vlan_id=50, device="f3r9s2", ipv4_address_secondary=[])}
        actual_ahora = SVI(vlan_id=50, device="f3r9s2", ipv4_address_secondary=["172.20.1.5/24"])

        pasos, verificar = snap.resolver_rollback(pre_state, device, actual_ahora)
        assert len(pasos) == 1
        # La IP ya no está -- revert exitoso, verificar debe dar True.
        assert verificar(SVI(vlan_id=50, device="f3r9s2", ipv4_address_secondary=[])) is True
        # La IP TODAVÍA está -- revert falló, verificar debe dar False
        # (con el bug viejo, esto daba False positivo == True).
        assert verificar(SVI(vlan_id=50, device="f3r9s2", ipv4_address_secondary=["172.20.1.5/24"])) is False
