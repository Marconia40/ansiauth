"""Bug real reportado por el usuario contra f2r11s1 (device grande, tabla
MAC completa): el mismo tipo de corte de sesión SSH ya documentado para
``get_log_buffer()`` (Cisco) también rompe ``get_mac_table()`` completa en
devices con muchas entradas.

Cubre 3 fixes:
- ``get_log_buffer()`` -- 2 intentos fallidos antes del real, ver la
  docstring del método para la historia completa. El real: filtrar por
  fecha (``show clock`` + ``show logging | include ^(Mon D|...)`` para los
  últimos ``_LOG_WINDOW_DAYS`` días) en vez de traer todo el buffer desde
  el principio -- ``show logging`` es cronológico ascendente, así que
  "traer lo que se pueda antes del corte" siempre daba lo más VIEJO, nunca
  lo reciente (que es lo único operacionalmente útil).
- ``_CISCO_MAC_RE`` (``arp_mac_parser.py``): el campo "type" no siempre es
  1 sola palabra ("dynamic ip,ipx,assigned,other" en f2r11s1, vs
  "DYNAMIC" en f3r9s1) -- el regex original perdía esas filas en silencio.
- ``search_mac_table()`` (nuevo, Cisco + Huawei): búsqueda puntual en vivo
  (``| include {pattern}``) para cuando la tabla completa no es viable --
  y su task de Celery (``search_mac_task``), que no toca el mecanismo de
  cache normal (``arp_mac_repository``) a propósito.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.services import ansible_service
from app.services.vendors.cisco.driver import CiscoVendor
from app.services.vendors.huawei.driver import HuaweiVendor


class _FakeDevice:
    name = "cisco-01"
    host = "192.0.2.10"
    username = "admin"
    auth_method = "password"
    vendor = "cisco_ios"


def _make_run_result(rc: int, stdouts: list[str]) -> dict:
    return {"rc": rc, "stdout": "", "stderr": "", "stdouts": stdouts}


# ── get_log_buffer: fallback a fecha reciente SOLO cuando el read completo
# se corta -- no genérico a todos los devices (pedido explícito del
# usuario: "no quiero que sea algo específico para este equipo sino cuando
# falla porque es mucha info y solo ahí") ─────────────────────────────────

def _fake_clock_and_log_run(full_read_result: dict, *, clock: str = "19:00:57.437 AR Mon Sep 14 2026"):
    """Router de comandos para el mock de run_playbook: el read completo
    ('show logging | exclude ...', resultado configurable) vs 'show clock'
    y el read por fecha del fallback (solo se llaman si el primero se
    corta)."""
    captured = {"all_commands": []}

    def _fake_run(playbook, extravars, inventory=None, device=None):
        commands = extravars.get("commands")
        captured["all_commands"].append(commands)
        if commands == ["show logging | exclude CFGLOG_LOGGEDCMD"]:
            return full_read_result
        if commands == ["show clock"]:
            return _make_run_result(rc=0, stdouts=[clock])
        # El include-por-fecha del fallback -- mismo resultado que el
        # read completo por default, tests que lo necesiten distinto lo
        # pisan reasignando captured["fallback_result"].
        return captured.get("fallback_result", full_read_result)

    return _fake_run, captured


def test_get_log_buffer_happy_path_no_fallback(monkeypatch):
    """Read completo exitoso (rc=0) -- ni 'show clock' ni el fallback por
    fecha deben dispararse en absoluto."""
    fake_run, captured = _fake_clock_and_log_run(
        _make_run_result(rc=0, stdouts=["full log, no truncation"]),
    )
    monkeypatch.setattr(ansible_service, "run_playbook", fake_run)

    result = CiscoVendor().get_log_buffer(_FakeDevice(), "pw")

    assert result == "full log, no truncation"
    assert captured["all_commands"] == [["show logging | exclude CFGLOG_LOGGEDCMD"]]


def test_get_log_buffer_falls_back_on_session_drop(monkeypatch):
    """rc != 0 CON contenido parcial capturado (corte real de sesión, ver
    docstring de get_log_buffer) -- ahí sí dispara el fallback por fecha,
    calculada desde la fecha REAL del device (show clock)."""
    from app.services.vendors.cisco import driver as driver_module

    fake_run, captured = _fake_clock_and_log_run(
        _make_run_result(rc=1, stdouts=["Feb 24 11:41:46: %SPANTREE-5-TOPOTRAP: Topology Cha"]),
    )
    captured["fallback_result"] = _make_run_result(rc=0, stdouts=["Sep 14 17:14:23: %SYS-5-CONFIG_I: ..."])
    monkeypatch.setattr(ansible_service, "run_playbook", fake_run)
    monkeypatch.setattr(driver_module, "_LOG_WINDOW_DAYS", 3)

    result = CiscoVendor().get_log_buffer(_FakeDevice(), "pw")

    assert result == "Sep 14 17:14:23: %SYS-5-CONFIG_I: ..."
    assert captured["all_commands"] == [
        ["show logging | exclude CFGLOG_LOGGEDCMD"],
        ["show clock"],
        ["show logging | include ^(Sep 14|Sep 13|Sep 12) "],
    ]


def test_get_log_buffer_fallback_tolerates_its_own_partial_read(monkeypatch):
    """Red de seguridad de 2do nivel: si incluso la ventana acotada del
    fallback se corta (actividad real inusualmente alta esos días),
    partial_ok=True debe devolver lo capturado en vez de tirar error."""
    from app.services.vendors.cisco import driver as driver_module

    partial = "Sep 12 02:35:38: %C4K_L2MAN-6-INVALIDSOURCEADDRESSPACKET: (Suppr"
    fake_run, captured = _fake_clock_and_log_run(_make_run_result(rc=1, stdouts=["old stuff"]))
    captured["fallback_result"] = _make_run_result(rc=1, stdouts=[partial])
    monkeypatch.setattr(ansible_service, "run_playbook", fake_run)
    monkeypatch.setattr(driver_module, "_LOG_WINDOW_DAYS", 3)

    result = CiscoVendor().get_log_buffer(_FakeDevice(), "pw")

    assert result == partial


def test_get_log_buffer_still_raises_when_nothing_captured(monkeypatch):
    """Device genuinely unreachable (no partial content at all on the full
    read) must keep raising immediately -- the fallback is never even
    attempted, there's nothing a narrower read would fix."""
    fake_run, captured = _fake_clock_and_log_run(_make_run_result(rc=1, stdouts=[]))
    monkeypatch.setattr(ansible_service, "run_playbook", fake_run)

    with pytest.raises(RuntimeError, match="cisco-01"):
        CiscoVendor().get_log_buffer(_FakeDevice(), "pw")

    assert captured["all_commands"] == [["show logging | exclude CFGLOG_LOGGEDCMD"]]


def test_get_log_buffer_raises_on_unparseable_clock(monkeypatch):
    """'show clock' con un formato irreconocible (dentro del fallback) debe
    fallar con un error claro en vez de un ValueError críptico."""
    fake_run, captured = _fake_clock_and_log_run(
        _make_run_result(rc=1, stdouts=["partial old content"]),
        clock="garbage output, no date here",
    )
    monkeypatch.setattr(ansible_service, "run_playbook", fake_run)

    with pytest.raises(RuntimeError, match="Cannot parse 'show clock'"):
        CiscoVendor().get_log_buffer(_FakeDevice(), "pw")


# ── _CISCO_MAC_RE: "type" no siempre es 1 sola palabra ──────────────────────

def test_parse_cisco_mac_tolerates_multi_word_type():
    """Bug real: f2r11s1 reporta 'dynamic ip,ipx,assigned,other' como type
    (2 palabras) en vez del 'DYNAMIC' de 1 sola palabra visto en f3r9s1 --
    el regex original perdía la fila en silencio (sin error, sin entry)."""
    from app.services.parsers.arp_mac_parser import parse_cisco_mac

    raw = "   7      bc24.114b.9732   dynamic ip,ipx,assigned,other Port-channel1              "
    entries = parse_cisco_mac(raw)

    assert entries == [{
        "mac": "bc24.114b.9732", "vlan": "7",
        "type": "dynamic ip,ipx,assigned,other", "interface": "Port-channel1",
    }]


def test_parse_cisco_mac_single_word_type_still_works():
    """Regresión: el formato original (f3r9s1, 'DYNAMIC'/'STATIC' de 1
    palabra) no debe romperse con el regex más permisivo."""
    from app.services.parsers.arp_mac_parser import parse_cisco_mac

    assert parse_cisco_mac(" 156    0056.2b0f.996c    DYNAMIC     Gi1/0/24") == [
        {"mac": "0056.2b0f.996c", "vlan": "156", "type": "DYNAMIC", "interface": "Gi1/0/24"}
    ]
    assert parse_cisco_mac(" All    0100.0ccc.cccc STATIC      CPU") == [
        {"mac": "0100.0ccc.cccc", "vlan": "All", "type": "STATIC", "interface": "CPU"}
    ]


# ── search_mac_table: Cisco ───────────────────────────────────────────────

def test_cisco_search_mac_table_sends_include_filter(monkeypatch):
    captured = {}
    mac_output = (
        "          Mac Address Table\n"
        "-------------------------------------------\n"
        "Vlan    Mac Address       Type        Ports\n"
        "----    -----------       --------    -----\n"
        "  10    aabb.ccdd.eeff    DYNAMIC     Gi1/0/1\n"
    )

    def _fake_run(playbook, extravars, inventory=None, device=None):
        captured["commands"] = extravars.get("commands")
        return _make_run_result(rc=0, stdouts=[mac_output])

    monkeypatch.setattr(ansible_service, "run_playbook", _fake_run)

    entries = CiscoVendor().search_mac_table("aabb.ccdd.eeff", _FakeDevice(), "pw")

    assert captured["commands"] == ["show mac address-table | include aabb.ccdd.eeff"]
    assert len(entries) == 1
    assert entries[0]["mac"].lower() == "aabb.ccdd.eeff"


# ── search_mac_table: Huawei ───────────────────────────────────────────────

def test_huawei_search_mac_table_sends_include_filter(monkeypatch):
    captured = {}

    def _fake_run(playbook, extravars, inventory=None, device=None):
        captured["commands"] = extravars.get("commands")
        return _make_run_result(rc=0, stdouts=[""])

    monkeypatch.setattr(ansible_service, "run_playbook", _fake_run)

    class _FakeHuaweiDevice(_FakeDevice):
        vendor = "huawei_vrp"

    HuaweiVendor().search_mac_table("aabb-ccdd-eeff", _FakeHuaweiDevice(), "pw")

    assert captured["commands"] == ["display mac-address | include aabb-ccdd-eeff"]


# ── VendorDriver base default (unimplemented vendor) ──────────────────────

def test_base_search_mac_table_raises_not_implemented():
    """Default on the ABC itself -- called unbound so this doesn't need to
    stub every other abstract method just to instantiate a dummy subclass."""
    from app.services.vendors.base import VendorDriver

    with pytest.raises(NotImplementedError):
        VendorDriver.search_mac_table(MagicMock(__class__=VendorDriver), "x", _FakeDevice(), "pw")


# ── search_mac_task (Celery) ────────────────────────────────────────────────

def test_search_mac_task_marks_job_completed_with_entries(monkeypatch):
    from app.models.job import Job
    from app import tasks as tasks_module

    job = Job(operation="mac_search", device="f2r11s1", parameters={"pattern": "aabb"})

    class _FakeJobRepo:
        def __init__(self):
            self.saved = []

        def get(self, job_id):
            return job if job_id == job.job_id else None

        def add(self, j):
            self.saved.append(j)

    class _FakeDriver:
        def search_mac_table(self, pattern, device, password):
            assert pattern == "aabb"
            return [{"mac": "aabb.ccdd.eeff", "vlan": "10", "interface": "Gi1/0/1", "type": "DYNAMIC"}]

    fake_device = MagicMock()
    fake_device.driver = _FakeDriver()
    fake_device.password = "pw"

    class _FakeDeviceRepo:
        def get(self, name):
            return fake_device if name == "f2r11s1" else None

    fake_job_repo = _FakeJobRepo()
    monkeypatch.setattr(
        tasks_module, "celery_app",
        MagicMock(task=lambda **kw: (lambda fn: fn)),
    )
    import app.composition as composition_module
    monkeypatch.setattr(composition_module, "job_repository", fake_job_repo, raising=False)
    monkeypatch.setattr(composition_module, "device_repository", _FakeDeviceRepo(), raising=False)

    tasks_module.search_mac_task(fake_device.name if False else "f2r11s1", "aabb", job.job_id)

    assert job.status == "completed"
    assert job.result == {
        "entries": [{"mac": "aabb.ccdd.eeff", "vlan": "10", "interface": "Gi1/0/1", "type": "DYNAMIC"}],
        "pattern": "aabb",
    }


def test_search_mac_task_marks_job_failed_on_driver_error(monkeypatch):
    from app.models.job import Job
    from app import tasks as tasks_module

    job = Job(operation="mac_search", device="f2r11s1", parameters={"pattern": "aabb"})

    class _FakeJobRepo:
        def get(self, job_id):
            return job if job_id == job.job_id else None

        def add(self, j):
            pass

    class _FakeDriver:
        def search_mac_table(self, pattern, device, password):
            raise RuntimeError("Connection to 172.16.61.75 closed by remote host.")

    fake_device = MagicMock()
    fake_device.driver = _FakeDriver()
    fake_device.password = "pw"

    class _FakeDeviceRepo:
        def get(self, name):
            return fake_device if name == "f2r11s1" else None

    import app.composition as composition_module
    monkeypatch.setattr(composition_module, "job_repository", _FakeJobRepo(), raising=False)
    monkeypatch.setattr(composition_module, "device_repository", _FakeDeviceRepo(), raising=False)

    tasks_module.search_mac_task("f2r11s1", "aabb", job.job_id)

    assert job.status == "failed"
    assert "closed by remote host" in job.error


# ── POST /devices/{name}/global-config/mac/search (API wiring) ─────────────

_MAC_SEARCH_PAYLOAD = {
    "name": "mac-search-dev",
    "host": "192.168.1.11",
    "vendor": "cisco_ios",
    "username": "admin",
    "password": "admin",
    "site_id": 1,
}


@pytest.fixture(autouse=True)
def _cleanup_mac_search_device():
    from app.db.models import DeviceModel
    from app.db.session import get_session

    def _clear():
        with get_session() as session:
            session.query(DeviceModel).filter_by(name=_MAC_SEARCH_PAYLOAD["name"]).delete(
                synchronize_session=False
            )

    _clear()
    yield
    _clear()


def test_search_endpoint_validates_include_pattern(admin_client):
    admin_client.post("/api/v1/devices/", json=_MAC_SEARCH_PAYLOAD)
    r = admin_client.post(
        f"/api/v1/devices/{_MAC_SEARCH_PAYLOAD['name']}/global-config/mac/search",
        params={"include": "not valid; has spaces and semicolons"},
    )
    assert r.status_code == 422


def test_search_endpoint_creates_job_and_runs_task(admin_client):
    """CELERY_TASK_ALWAYS_EAGER (conftest.py) makes .delay() run inline --
    conftest's mock_ansible_service fixture stubs run_playbook without a
    "stdouts" key, so this device's real CiscoVendor._leer() raises
    (no command output) and the job legitimately ends up failed. Still
    proves the endpoint -> task -> Job wiring works end-to-end without
    needing a real device."""
    admin_client.post("/api/v1/devices/", json=_MAC_SEARCH_PAYLOAD)

    r = admin_client.post(
        f"/api/v1/devices/{_MAC_SEARCH_PAYLOAD['name']}/global-config/mac/search",
        params={"include": "aabb.ccdd.eeff"},
    )
    assert r.status_code == 202, r.text
    body = r.json()["data"]
    assert "job_id" in body

    job_resp = admin_client.get(f"/api/v1/jobs/{body['job_id']}")
    assert job_resp.status_code == 200
    job_data = job_resp.json()["data"]
    assert job_data["status"] in ("completed", "failed")
    assert job_data["operation"] == "mac_search"


# ── ssh_direct_service: terminal width 0, no solo length 0 ─────────────────

def test_cisco_reads_disable_both_pager_and_line_wrap(monkeypatch):
    """Bug real encontrado en vivo contra f2r11s1: un comando largo de 1
    sola línea (el include con fechas alternadas del fallback de
    get_log_buffer) se redibuja/corta al tipearse si excede el ancho de
    terminal default (80 cols) -- _extraer_salida_comando() busca el eco
    EXACTO del comando para recortar banner/prompt, y si el wrap lo
    corrompe, el transcript crudo completo (comando incluido) se cuela en
    el resultado. 'terminal width 0' debe mandarse siempre junto con
    'terminal length 0' para devices Cisco, mismo criterio que
    'screen-width 512' ya usa Huawei para el mismo problema en escrituras."""
    from app.services import ssh_direct_service

    captured = {}

    def _fake_interactive(device, lines, known_extra_opts=None):
        captured["lines"] = lines
        return 0, "some output\nf2r11s1#quit", "", []

    monkeypatch.setattr(ssh_direct_service, "_run_ssh_interactive", _fake_interactive)

    class _Dev:
        name = "f2r11s1"
        vendor = "cisco_ios"

    ssh_direct_service._run_reads(_Dev(), ["show logging | include ^(Sep 14) "], op_label="test")

    assert captured["lines"] == [
        "terminal length 0", "terminal width 0", "show logging | include ^(Sep 14) ",
    ]


def test_extraer_salida_comando_tolerates_trailing_space_in_command():
    """Bug real: get_log_buffer()'s date-filter command ends in a literal
    trailing space ("...) "). The device's echo comes back rstripped, so
    matching must rstrip the command too, or the echo (and everything
    before it -- banner, prompt) leaks into the returned output instead of
    being cut."""
    from app.services import ssh_direct_service

    command = "show logging | include ^(Sep 14) "
    raw = (
        "f2r11s1.psi#terminal length 0\n"
        "f2r11s1.psi#terminal width 0\n"
        f"f2r11s1.psi#{command}\n"
        "Sep 14 17:14:23: %SYS-5-CONFIG_I: Configured from console\n"
        "f2r11s1.psi#quit"
    )

    result = ssh_direct_service._extraer_salida_comando(raw, command)

    assert result == "Sep 14 17:14:23: %SYS-5-CONFIG_I: Configured from console"
    assert "terminal width" not in result
    assert "include" not in result


# ── _exito: rc==0 ya no es suficiente para dar por completa una sesión ─────

def test_exito_rejects_clean_rc_when_session_never_reached_our_quit():
    """Bug real encontrado en vivo contra f2r11s1: el device a veces cierra
    el canal SSH "prolijo" desde su lado (sin RST/error) cuando decide
    cortar una salida muy larga -- OpenSSH lo interpreta como una
    terminación normal, así que el cliente sale con rc=0 IGUAL, aunque el
    comando nunca haya llegado a nuestro "quit" final. Confirmado
    comparando el mismo read repetido contra el mismo device: a veces
    rc=0 truncado a mitad de palabra, a veces rc=1 con "Connection closed
    by remote host" -- mismo corte real, rc inconsistente. _exito() ya no
    debe confiar en rc en absoluto, solo en si el eco de "quit" aparece."""
    from app.services import ssh_direct_service

    truncated_no_quit_echo = (
        "f2r11s1.psi#show logging\n"
        "Sep 14 17:14:12: %PARSER-5-CFGLOG_LOGGEDCMD: Topology Cha"  # cut mid-word, no "quit" echo
    )

    assert ssh_direct_service._exito(truncated_no_quit_echo, truncated_no_quit_echo, vendor="cisco_ios") is False


def test_exito_accepts_clean_rc_when_session_reached_our_quit():
    """Regresión: una sesión que sí completa (nuestro "quit" ecoado al
    final) sigue dando éxito, sin importar que ya no se mire rc."""
    from app.services import ssh_direct_service

    complete = (
        "f2r11s1.psi#show logging\n"
        "Sep 14 17:14:23: %SYS-5-CONFIG_I: Configured from console\n"
        "f2r11s1.psi#quit"
    )

    assert ssh_direct_service._exito(complete, complete, vendor="cisco_ios") is True


# ── _run_reads(): algoritmos aprendidos se reusan entre comandos del batch ──

def test_run_reads_fuses_all_commands_into_one_ssh_session(monkeypatch):
    """Bug real observado en vivo (f3r9s1/f3r9s2/f2r11s1/f2r10s1): antes,
    cada uno de los hasta 8 comandos de un read abría su PROPIA conexión
    SSH -- confirmado en el log de producción como ~24 conexiones
    TCP+SSH en ~16s para UN SOLO read contra un device con algoritmos
    legacy, la clase de ráfaga que puede empujar a un device con
    firmware viejo a resetear la conexión. Ahora ``_run_reads()`` manda
    TODOS los comandos por stdin de una sola sesión (mismo mecanismo que
    ``_run_write()`` ya usaba para bloques de config) -- 1 sola
    negociación para todo el batch, sin importar cuántos comandos tenga."""
    from app.services import ssh_direct_service

    state = {"calls": 0}
    transcript = (
        "<f3r9s2>screen-length 0 temporary\n"
        "<f3r9s2>display version\n"
        "VRP version 5.130\n"
        "<f3r9s2>display vlan\n"
        "VLAN ID  Name\n"
        "1        default\n"
        "<f3r9s2>display interface brief\n"
        "Eth0/0/1  up\n"
        "<f3r9s2>quit"
    )

    def _fake_run(cmd, input, capture_output, text, timeout, env):
        state["calls"] += 1
        tiene_opcion = any("HostKeyAlgorithms=+ssh-rsa" in part for part in cmd)
        class _Proc:
            pass
        proc = _Proc()
        if tiene_opcion:
            proc.returncode = 0
            proc.stdout = transcript
            proc.stderr = ""
        else:
            proc.returncode = 255
            proc.stdout = ""
            proc.stderr = (
                "Unable to negotiate with 172.16.61.210 port 22: no matching "
                "host key type found. Their offer: ssh-rsa"
            )
        return proc

    monkeypatch.setattr(ssh_direct_service.subprocess, "run", _fake_run)
    monkeypatch.setattr(ssh_direct_service, "_agent_for", lambda device: _NullAgentCtx())

    class _Dev:
        name = "f3r9s2"
        host = "172.16.61.210"
        username = "netconf"
        vendor = "huawei_vrp"

    result = ssh_direct_service._run_reads(
        _Dev(), ["display version", "display vlan", "display interface brief"],
        op_label="test",
    )

    # 1 sola sesión para los 3 comandos -- 1 intento fallido de
    # negociación + 1 exitoso, sin importar cuántos comandos haya en el
    # batch (antes: 2 por comando x 3 = 6).
    assert state["calls"] == 2
    assert result["rc"] == 0
    assert result["stdouts"] == [
        "VRP version 5.130",
        "VLAN ID  Name\n1        default",
        "Eth0/0/1  up",
    ]


def test_extraer_salidas_comandos_handles_session_cut_mid_batch():
    """Si la sesión se corta antes de llegar a un comando (device se cae a
    mitad del batch), ese comando y los que le siguen en la lista nunca
    pueden haberse tipeado tampoco (se manda todo por el mismo stream de
    una sola vez) -- devuelven "". Lo que haya sobrado del transcript
    (acá, el mensaje de corte) queda atado al ÚLTIMO comando cuyo eco sí
    se encontró, mismo criterio que ``_extraer_salida_comando()`` ya usa
    para 1 comando cuando nuestro "quit" final nunca se ecoa."""
    from app.services import ssh_direct_service

    raw = (
        "<dev>display version\n"
        "VRP version 5.130\n"
        "Connection reset by peer"
    )
    result = ssh_direct_service._extraer_salidas_comandos(
        raw, ["display version", "display vlan", "display interface brief"],
    )

    assert result[0] == "VRP version 5.130\nConnection reset by peer"
    assert result[1] == ""
    assert result[2] == ""


# ── SSH algorithm negotiation: adapta por device, no fuerza legacy siempre ──

def test_run_ssh_interactive_tries_default_algorithms_first(monkeypatch):
    """Pedido explícito del usuario: no todos los devices necesitan (o
    toleran) el set de algoritmos legacy confirmado contra f3r9s1/f3r9s2 --
    el intento normal (sin overrides) va primero."""
    from app.services import ssh_direct_service

    captured_cmds = []

    def _fake_run(cmd, input, capture_output, text, timeout, env):
        captured_cmds.append(cmd)
        class _Proc:
            returncode = 0
            stdout = "ok\nf2r11s1#quit"
            stderr = ""
        return _Proc()

    monkeypatch.setattr(ssh_direct_service.subprocess, "run", _fake_run)
    monkeypatch.setattr(ssh_direct_service, "_agent_for", lambda device: _NullAgentCtx())

    class _Dev:
        name = "somedevice"
        host = "192.0.2.1"
        username = "admin"

    ssh_direct_service._run_ssh_interactive(_Dev(), ["show version"])

    assert len(captured_cmds) == 1
    assert not any("KexAlgorithms" in part for part in captured_cmds[0])


def test_run_ssh_interactive_retries_with_devices_own_kex_offer(monkeypatch):
    """Cuando el intento default falla por negociación de KEX, reintenta
    con EXACTAMENTE lo que el device ofreció en su propio mensaje de error
    -- no una lista fija adivinada de antemano. Usa un algoritmo inventado
    ("totally-made-up-kex-2000") a propósito, para probar que es
    verdaderamente dinámico y no una coincidencia con algo hardcodeado."""
    from app.services import ssh_direct_service

    captured_cmds = []

    def _fake_run(cmd, input, capture_output, text, timeout, env):
        captured_cmds.append(cmd)
        class _Proc:
            pass
        proc = _Proc()
        if len(captured_cmds) == 1:
            proc.returncode = 255
            proc.stdout = ""
            proc.stderr = (
                "Unable to negotiate with 192.0.2.1 port 22: no matching key "
                "exchange method found. Their offer: totally-made-up-kex-2000"
            )
        else:
            proc.returncode = 0
            proc.stdout = "ok\nsomedevice#quit"
            proc.stderr = ""
        return proc

    monkeypatch.setattr(ssh_direct_service.subprocess, "run", _fake_run)
    monkeypatch.setattr(ssh_direct_service, "_agent_for", lambda device: _NullAgentCtx())

    class _Dev:
        name = "somedevice"
        host = "192.0.2.1"
        username = "admin"

    rc, stdout, stderr, _ = ssh_direct_service._run_ssh_interactive(_Dev(), ["show version"])

    assert len(captured_cmds) == 2
    assert not any("KexAlgorithms" in part for part in captured_cmds[0])
    assert "KexAlgorithms=+totally-made-up-kex-2000" in captured_cmds[1]
    assert rc == 0
    assert stdout == "ok\nsomedevice#quit"


def test_run_ssh_interactive_retries_host_key_type_sets_both_options(monkeypatch):
    """'no matching host key type' debe setear TANTO HostKeyAlgorithms
    como PubkeyAcceptedKeyTypes con la oferta real -- confirmado en vivo
    (f3r9s2) que uno solo no alcanza para autenticar por clave pública."""
    from app.services import ssh_direct_service

    captured_cmds = []

    def _fake_run(cmd, input, capture_output, text, timeout, env):
        captured_cmds.append(cmd)
        class _Proc:
            pass
        proc = _Proc()
        if len(captured_cmds) == 1:
            proc.returncode = 255
            proc.stdout = ""
            proc.stderr = (
                "Unable to negotiate with 172.16.61.210 port 22: no matching "
                "host key type found. Their offer: ssh-rsa"
            )
        else:
            proc.returncode = 0
            proc.stdout = "ok\nf3r9s2#quit"
            proc.stderr = ""
        return proc

    monkeypatch.setattr(ssh_direct_service.subprocess, "run", _fake_run)
    monkeypatch.setattr(ssh_direct_service, "_agent_for", lambda device: _NullAgentCtx())

    class _Dev:
        name = "f3r9s2"
        host = "172.16.61.210"
        username = "netconf"

    ssh_direct_service._run_ssh_interactive(_Dev(), ["display logbuffer"])

    assert "HostKeyAlgorithms=+ssh-rsa" in captured_cmds[1]
    assert "PubkeyAcceptedKeyTypes=+ssh-rsa" in captured_cmds[1]


def test_run_ssh_interactive_stops_retrying_when_same_category_repeats(monkeypatch):
    """Si la MISMA categoría (ej. kex) sigue fallando después de adaptarla
    una vez, no tiene sentido seguir reintentando -- se corta y devuelve
    el error real en vez de loopear indefinidamente."""
    from app.services import ssh_direct_service

    captured_cmds = []

    def _fake_run(cmd, input, capture_output, text, timeout, env):
        captured_cmds.append(cmd)
        class _Proc:
            returncode = 255
            stdout = ""
            stderr = (
                "Unable to negotiate with 192.0.2.1 port 22: no matching key "
                "exchange method found. Their offer: some-kex-nobody-supports"
            )
        return _Proc()

    monkeypatch.setattr(ssh_direct_service.subprocess, "run", _fake_run)
    monkeypatch.setattr(ssh_direct_service, "_agent_for", lambda device: _NullAgentCtx())

    class _Dev:
        name = "somedevice"
        host = "192.0.2.1"
        username = "admin"

    rc, stdout, stderr, _ = ssh_direct_service._run_ssh_interactive(_Dev(), ["show version"])

    # 1 intento default + 1 reintento adaptado -- la 3ra vez matchearía la
    # MISMA categoría otra vez, así que corta ahí sin un 3er intento.
    assert len(captured_cmds) == 2
    assert rc == 255


def test_run_ssh_interactive_does_not_retry_on_unrelated_failure(monkeypatch):
    """Un fallo que NO es de negociación (ej. auth rechazada) no debe
    disparar el reintento con otros algoritmos -- cambiarlos no arreglaría
    nada ahí, y solo agregaría latencia/ruido."""
    from app.services import ssh_direct_service

    captured_cmds = []

    def _fake_run(cmd, input, capture_output, text, timeout, env):
        captured_cmds.append(cmd)
        class _Proc:
            returncode = 255
            stdout = ""
            stderr = "Permission denied (publickey)."
        return _Proc()

    monkeypatch.setattr(ssh_direct_service.subprocess, "run", _fake_run)
    monkeypatch.setattr(ssh_direct_service, "_agent_for", lambda device: _NullAgentCtx())

    class _Dev:
        name = "somedevice"
        host = "192.0.2.1"
        username = "admin"

    ssh_direct_service._run_ssh_interactive(_Dev(), ["show version"])

    assert len(captured_cmds) == 1


class _NullAgentCtx:
    def __enter__(self):
        return {}

    def __exit__(self, *a):
        return False


def test_run_ssh_interactive_adapts_cipher_too(monkeypatch):
    """Caso reportado por el usuario contra f2r6s3: el device solo ofrece
    ciphers legacy (aes128-cbc, 3des-cbc, aes192-cbc, aes256-cbc) que
    OpenSSH moderno no ofrece por default -- confirma que la categoria
    'cipher' (la 3ra de _NEGOTIATION_OPTION_NAMES, no probada en vivo
    todavia contra un device real) tambien se adapta con la oferta real,
    no solo kex/host key."""
    from app.services import ssh_direct_service

    captured_cmds = []

    def _fake_run(cmd, input, capture_output, text, timeout, env):
        captured_cmds.append(cmd)
        class _Proc:
            pass
        proc = _Proc()
        if len(captured_cmds) == 1:
            proc.returncode = 255
            proc.stdout = ""
            proc.stderr = (
                "Unable to negotiate with 172.16.61.53 port 22: no matching "
                "cipher found. Their offer: aes128-cbc,3des-cbc,aes192-cbc,aes256-cbc"
            )
        else:
            proc.returncode = 0
            proc.stdout = "ok\nf2r6s3#quit"
            proc.stderr = ""
        return proc

    monkeypatch.setattr(ssh_direct_service.subprocess, "run", _fake_run)
    monkeypatch.setattr(ssh_direct_service, "_agent_for", lambda device: _NullAgentCtx())

    class _Dev:
        name = "f2r6s3"
        host = "172.16.61.53"
        username = "ansiauthtest"

    rc, stdout, stderr, _ = ssh_direct_service._run_ssh_interactive(_Dev(), ["show version"])

    assert len(captured_cmds) == 2
    assert "Ciphers=+aes128-cbc,3des-cbc,aes192-cbc,aes256-cbc" in captured_cmds[1]
    assert rc == 0


def test_run_ssh_interactive_adapts_all_three_categories_in_sequence(monkeypatch):
    """Peor caso posible: un device que necesita adaptar KEX, host key Y
    cipher, los 3 en secuencia (no confirmado en vivo contra ningun
    device real todavia, pero f2r6s3 ya mostro que necesita al menos
    KEX+host-key+cipher combinados via un test manual del usuario) --
    confirma que _MAX_NEGOTIATION_RETRIES (3, uno por categoria) alcanza
    para los 4 intentos totales que hacen falta."""
    from app.services import ssh_direct_service

    captured_cmds = []
    failures = [
        "Unable to negotiate with 172.16.61.53 port 22: no matching key "
        "exchange method found. Their offer: diffie-hellman-group14-sha1",
        "Unable to negotiate with 172.16.61.53 port 22: no matching host "
        "key type found. Their offer: ssh-rsa",
        "Unable to negotiate with 172.16.61.53 port 22: no matching "
        "cipher found. Their offer: aes128-cbc,3des-cbc,aes192-cbc,aes256-cbc",
    ]

    def _fake_run(cmd, input, capture_output, text, timeout, env):
        captured_cmds.append(cmd)
        class _Proc:
            pass
        proc = _Proc()
        attempt = len(captured_cmds)
        if attempt <= len(failures):
            proc.returncode = 255
            proc.stdout = ""
            proc.stderr = failures[attempt - 1]
        else:
            proc.returncode = 0
            proc.stdout = "ok\nf2r6s3#quit"
            proc.stderr = ""
        return proc

    monkeypatch.setattr(ssh_direct_service.subprocess, "run", _fake_run)
    monkeypatch.setattr(ssh_direct_service, "_agent_for", lambda device: _NullAgentCtx())

    class _Dev:
        name = "f2r6s3"
        host = "172.16.61.53"
        username = "ansiauthtest"

    rc, stdout, stderr, _ = ssh_direct_service._run_ssh_interactive(_Dev(), ["show version"])

    # 3 intentos fallidos (1 por categoria) + el 4to que ya trae las 3
    # adaptadas y conecta -- justo el techo de _MAX_NEGOTIATION_RETRIES.
    assert len(captured_cmds) == 4
    final_cmd = captured_cmds[3]
    assert "KexAlgorithms=+diffie-hellman-group14-sha1" in final_cmd
    assert "HostKeyAlgorithms=+ssh-rsa" in final_cmd
    assert "PubkeyAcceptedKeyTypes=+ssh-rsa" in final_cmd
    assert "Ciphers=+aes128-cbc,3des-cbc,aes192-cbc,aes256-cbc" in final_cmd
    assert rc == 0
    assert stdout == "ok\nf2r6s3#quit"
