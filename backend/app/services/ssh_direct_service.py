"""Bypass de Ansible/paramiko para devices con ``auth_method == "key"``.

Motivo real (ver docstring de ``Device._VALID_AUTH_METHODS`` y el plan de
esta sesión): ni paramiko (2 versiones probadas) ni ``ansible-pylibssh``
logran autenticar por clave pública contra el firmware real de f3r9s2
(Huawei) ni f3r9s1 (Cisco) -- confirmado en vivo contra los 2. El binario
``ssh`` real (OpenSSH del sistema) conecta perfecto y consistente en ambos.
Este módulo shellea directo a ese binario en vez de usar una librería SSH
en Python, SOLO para devices en ``auth_method == "key"`` -- todo lo demás
(el 100% del fleet en password) sigue por ``ansible_service.run_playbook()``
sin ningún cambio, ver ``VendorDriver._ejecutar()``.

Ni siquiera el ``ssh`` real alcanza solo: confirmado en vivo que
``ssh -i archivo`` (única identidad candidata, sin agent) hace que OpenSSH
mande la firma de una sola vez sin preguntar antes -- optimización válida
por RFC 4252 que ahorra 1 round-trip -- y el firmware VRP de f3r9s2 nunca
contesta ese paquete (cuelga hasta el timeout). Solo funciona el flujo de
2 fases (query sin firma -> el device responde PK_OK -> recién ahí se
firma) que OpenSSH hace cuando la identidad viene de un ssh-agent. Por eso
``_run_ssh()`` arranca un agent efímero por llamada en vez de pasar
``-i`` directo -- ver ``_start_agent()``.

``run_direct()`` devuelve el MISMO contrato que ``ansible_service.run_playbook()``
(``{"rc", "stdout", "stderr", "stdouts"}``) -- todo lo que está por encima
(``_aplicar_desde_template()`` con su lógica de alternatives, ``aplicar_lote()``,
cada resolver de ``Puerto``/``SVI``/``VLAN``) sigue funcionando sin cambios,
nunca le importó CÓMO se obtuvo el resultado.
"""
import contextlib
import logging
import os
import re
import signal
import subprocess

from app.services.vendors.base import limpiar_ruido_benigno as _limpiar_ruido_benigno

logger = logging.getLogger(__name__)

_SSH_CONNECT_TIMEOUT = int(os.environ.get("SSH_DIRECT_CONNECT_TIMEOUT", "15"))
_SSH_COMMAND_TIMEOUT = int(os.environ.get("SSH_DIRECT_COMMAND_TIMEOUT", "60"))

# Overrides de algoritmos legacy -- confirmados en vivo esta sesión contra
# f3r9s2 y f3r9s1, los 2 devices reales probados. "+" agrega a la lista
# default de OpenSSH en vez de reemplazarla, así que son inofensivos si
# algún día se corre contra un device que no los necesita.
#
# KexAlgorithms usa "^" (prepende, no solo agrega) a propósito -- confirmado
# en vivo que sin esto, f3r9s2 negocia "diffie-hellman-group-exchange-sha256"
# (el device SÍ lo soporta, y el cliente lo prioriza por sobre lo que
# agregábamos con "+") en vez de un grupo fijo -- "group-exchange" implica 1
# round-trip extra (el server tiene que generar parámetros DH a medida) y es
# más caro de computar, ~3.3s de conexión contra f3r9s2 vs ~1.2s forzando un
# grupo fijo. "group14-sha1" (lo que había antes) ni siquiera es un algoritmo
# que f3r9s2 ofrezca -- confirmado que su oferta real es
# "diffie-hellman-group14-sha256,diffie-hellman-group-exchange-sha256", el
# "+group14-sha1" de antes era muerto para este device y por eso nunca se
# usaba. f3r9s1 (Cisco) no ofrece ningún grupo fijo en sha256, solo
# "diffie-hellman-group-exchange-sha1,diffie-hellman-group14-sha1" -- por
# eso van los 2 prepend-eados, sha256 primero (gana en f3r9s2) y sha1 de
# fallback (gana en f3r9s1), cada device se queda con el que sí soporta y
# ambos evitan el group-exchange lento.
_SSH_LEGACY_OPTS = [
    "-o", "HostKeyAlgorithms=+ssh-rsa",
    "-o", "PubkeyAcceptedKeyTypes=+ssh-rsa",
    "-o", "KexAlgorithms=^diffie-hellman-group14-sha256,diffie-hellman-group14-sha1",
]

# Tablas de error por vendor -- reemplaza el regex único genérico que había
# acá antes (r"^\s*(Error:|%\s)"), portado de los patrones `terminal_stderr_re`
# de los plugins terminal de Ansible ya instalados en este entorno como
# dependencia del fleet en password (cisco.ios/plugins/terminal/ios.py,
# community.network/plugins/terminal/ce.py) -- confirmados contra hardware
# real por la comunidad, no inventados. El regex viejo solo miraba el
# principio de línea ("Error:"/"%") y se perdía rechazos reales de texto
# libre como "invalid input"/"unknown command"/"syntax error"/"connection
# timed out" -- un rechazo real del device podía clasificarse como éxito.
#
# Limitación conocida, aceptada por ahora: esto escanea TODO el stdout
# combinado de un bloque multi-comando, sin saber a qué comando pertenece
# cada línea. Los 2 bugs reales encontrados antes de este cambio (un aviso
# benigno de Cisco, y un placeholder "y" propio rechazado por VRP en
# devices que no lo necesitaban) eran colisiones CROSS-VENDOR contra una
# única tabla compartida -- separar por vendor ya cierra esa clase de bug
# de raíz. Ya no hay evidencia de que haga falta además un tracking
# por-comando dentro del mismo vendor (ver vendors/base.py::RUIDO_BENIGNO,
# que sigue filtrando el ruido benigno conocido ANTES de este chequeo,
# sin cambios). Un parser que trackee resultado por-comando seguiría siendo
# una reescritura real de ``_run_ssh_interactive()``/``_extraer_salida_comando()``
# con riesgo alto (2 vendors, prompts interactivos que rompen el matching
# de eco, submodos de config que cambian el prompt a mitad de bloque) --
# evaluado y diferido a propósito, no un descuido.
_ERROR_RE = re.compile(r"^\s*(Error:|%\s)", re.MULTILINE)  # fallback para vendor desconocido

_ERROR_PATTERNS: "dict[str, re.Pattern]" = {
    "cisco_ios": re.compile(
        r"%\s*Error|invalid input|(?:incomplete|ambiguous) command|"
        r"connection timed out|[^\r\n]+ not found|bad mask|"
        r"bad secret|command authorization failed|"
        r"overlaps with|informational:|command rejected",
        re.IGNORECASE,
    ),
    "huawei_vrp": re.compile(
        r"%\s*Error:|^%\s*\w+|%\s*Bad secret|invalid input|"
        r"(?:incomplete|ambiguous) command|connection timed out|"
        r"[^\r\n]+ not found|syntax error|unknown command|"
        r"Error\[\d+\]:|Error:",
        re.IGNORECASE | re.MULTILINE,
    ),
}


def _tiene_error(texto: str, vendor: "str | None" = None) -> bool:
    patron = _ERROR_PATTERNS.get(vendor, _ERROR_RE)
    return bool(patron.search(_limpiar_ruido_benigno(texto)))


_AGENT_LINE_RE = re.compile(r"(SSH_AUTH_SOCK|SSH_AGENT_PID)=([^;]+);")


def _start_agent(keyfile: str) -> dict:
    """Arranca un ssh-agent efímero (1 por llamada) y le carga *keyfile*.

    Confirmado en vivo esta sesión: el firmware VRP de f3r9s2 solo contesta
    el flujo de auth por clave pública en 2 fases (query sin firma -> el
    device responde PK_OK -> recién ahí el cliente firma y reenvía) -- que
    es exactamente lo que hace OpenSSH cuando la identidad viene de un
    agent. Con ``-i archivo`` directo (única identidad candidata), OpenSSH
    manda la firma de una sola vez sin preguntar antes (optimización válida
    por RFC, ahorra 1 round-trip) y este device nunca contesta ese paquete
    -- cuelga hasta el timeout. Pasar por un agent, aunque sea efímero,
    fuerza el flujo de 2 fases que el device sí entiende."""
    proc = subprocess.run(["ssh-agent", "-s"], capture_output=True, text=True, timeout=5)
    env = dict(os.environ)
    for match in _AGENT_LINE_RE.finditer(proc.stdout):
        env[match.group(1)] = match.group(2)
    subprocess.run(["ssh-add", keyfile], env=env, capture_output=True, timeout=5)
    return env


def _stop_agent(env: dict) -> None:
    pid = env.get("SSH_AGENT_PID")
    if not pid:
        return
    try:
        os.kill(int(pid), signal.SIGTERM)
    except (ValueError, ProcessLookupError, PermissionError):
        pass


@contextlib.contextmanager
def _agent_for(device):
    from app.services import ansible_service

    keyfile = ansible_service.write_private_key_file(device.name, device.private_key)
    agent_env = _start_agent(keyfile)
    try:
        yield agent_env
    finally:
        _stop_agent(agent_env)


# VRP y IOS paginan la salida de comandos largos (``---- More ----`` /
# ``--More--``) por default -- un canal exec sin PTY no puede contestar
# esa pausa, así que un ``display``/``show`` con muchas líneas (ej.
# ``display interface description`` en un device con varias decenas de
# interfaces) se cuelga hasta el timeout aunque comandos cortos como
# ``display clock`` anden bien. Se manda como 1ra línea de cada sesión
# interactiva -- confirmado en vivo contra f3r9s2 que "no molesta" aunque
# la salida sea corta.
_PAGER_DISABLE = {
    "huawei_vrp": "screen-length 0 temporary",
    "cisco_ios": "terminal length 0",
}


def _run_ssh_interactive(device, lines: list[str]) -> tuple[int, str, str]:
    """Sesión SSH real con PTY asignado (``-tt``), las *lines* se mandan
    por stdin igual que un humano tipeando -- necesario tanto para
    escrituras que entran a modo de configuración (``system-view``/
    ``configure terminal`` no completan la secuencia de submodos como
    comando no-interactivo de 1 sola conexión -- confirmado en vivo contra
    f3r9s2: VRP se queda esperando sin contestar ni siquiera el ``commit``)
    como para lecturas que necesitan la línea de ``_PAGER_DISABLE`` antes
    del comando real. Se agrega un ``quit`` final para cerrar la sesión
    del lado del cliente en vez de esperar el timeout completo --
    confirmado en vivo que ``quit`` en user-view (VRP) / privileged exec
    (IOS, alias de ``exit``) corta la conexión limpio."""
    with _agent_for(device) as agent_env:
        cmd = [
            "ssh", "-tt",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            "-o", f"ConnectTimeout={_SSH_CONNECT_TIMEOUT}",
            *_SSH_LEGACY_OPTS,
            f"{device.username}@{device.host}",
        ]
        stdin_data = "\n".join([*lines, "quit"]) + "\n"
        try:
            proc = subprocess.run(
                cmd, input=stdin_data, capture_output=True, text=True,
                timeout=_SSH_COMMAND_TIMEOUT, env=agent_env,
            )
            return proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired:
            return 1, "", f"ssh command timed out after {_SSH_COMMAND_TIMEOUT}s"
        except Exception as exc:
            logger.exception("ssh_direct_service: interactive ssh invocation failed device=%s", device.name)
            return 1, "", str(exc)


# VRP (y también IOS, mismo patrón confirmado en vivo contra f3r9s1) no
# siempre cierra limpio el canal exec al final de la sesión -- el cliente
# ssh puede salir con rc != 0 aunque el device haya hecho todo lo pedido.
# Probamos primero confiar en el *stderr* (``Connection reset by peer`` /
# ``Broken pipe`` / etc.) pero resultó frágil: confirmado en vivo que un
# cierre igual de benigno, contra el mismo device, a veces sale con rc=255
# y stderr COMPLETAMENTE VACÍO (sin ningún mensaje) cuando la salida del
# comando es más larga -- no hay texto fijo con el que matchear. La señal
# confiable es otra: si el transcript crudo llegó hasta el eco de nuestro
# propio "quit" final (el terminador que ``_run_ssh_interactive()``
# siempre agrega), la sesión completó todo lo que le pedimos sin importar
# qué rc devuelva el cliente después -- si se cortó ANTES de ese eco (a
# mitad de un comando, por ejemplo), ahí sí fue un corte real.
def _sesion_completa(raw: str) -> bool:
    # Los bloques de Huawei ya traen "quit" en el medio como sintaxis
    # normal para salir de submodos (ej. "interface Vlanif1 / description
    # X / quit / commit / quit") -- "aparece en algún lado" no alcanza,
    # tiene que ser el eco de NUESTRO terminador final, que va a quedar
    # cerca del final del transcript (a lo sumo seguido del banner de
    # logout del device). Por eso se mira solo la cola, no todo el texto.
    lines = [l for l in raw.splitlines() if l.strip()]
    return any(line.rstrip().endswith("quit") for line in lines[-5:])


def _exito(rc: int, stdout: str, raw: str, vendor: "str | None" = None) -> bool:
    if _tiene_error(stdout, vendor):
        return False
    if rc == 0:
        return True
    return _sesion_completa(raw)


def _run_write(device, block: str, *, op_label: str) -> dict:
    logger.info("ssh_direct_service: %s on device=%s", op_label, device.name)
    rc, stdout, stderr = _run_ssh_interactive(device, block.split("\n"))
    rc = 0 if _exito(rc, stdout, stdout, vendor=device.vendor) else 1
    if rc == 0:
        logger.info("ssh_direct_service: %s OK on device=%s", op_label, device.name)
    else:
        logger.error(
            "ssh_direct_service: %s FAILED on device=%s — %s",
            op_label, device.name, stderr or stdout,
        )
    return {"rc": rc, "stdout": stdout, "stderr": stderr, "stdouts": [stdout] if stdout else []}


def _extraer_salida_comando(raw: str, command: str) -> str:
    """El transcript crudo de una sesión PTY trae de arreglo el banner de
    login, el eco de cada comando tipeado y el prompt de vuelta -- nada de
    eso lo devolvía ``ansible.netcommon`` (lo pelaba automático), y los
    parsers existentes (``HuaweiPortParser``, etc.) están escritos
    asumiendo esa salida ya limpia -- confirmado en vivo que sin este
    recorte una línea de banner puede colarse como si fuera un registro de
    interfaz y romper la validación. Recorta buscando la línea que hace
    eco de *command* (todo lo de ANTES es banner/pager-disable) y cortando
    en la ÚLTIMA línea que termina en "quit" -- el terminador que
    ``_run_ssh_interactive()`` siempre agrega al final de la sesión, así
    que su eco marca confiablemente el final de la salida real, sin
    necesidad de un regex de prompt por-vendor.

    Bug real encontrado probando rollback de GlobalConfig: tiene que ser
    la ÚLTIMA ocurrencia, no la primera -- ``show running-config`` en
    Cisco cortaba a la mitad de un bloque ``crypto pki certificate
    chain`` porque esos bloques terminan con un "quit" LEGÍTIMO como parte
    del config real (nada que ver con nuestro terminador), y la primera
    ocurrencia encontrada era esa, no la nuestra. Nuestro "quit" siempre
    es la ÚLTIMA línea que hacemos eco (se manda al final de todo), así
    que buscar desde el final es la señal confiable sin importar cuántos
    "quit" legítimos traiga el output real en el medio."""
    lines = raw.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.rstrip().endswith(command):
            start = i + 1
            break
    if start is None:
        return raw.strip("\n")
    end = len(lines)
    for i in range(len(lines) - 1, start - 1, -1):
        if lines[i].rstrip().endswith("quit"):
            end = i
            break
    return "\n".join(lines[start:end]).strip("\n")


def _run_reads(device, commands: list[str], *, op_label: str) -> dict:
    logger.info("ssh_direct_service: %s (%d command(s)) on device=%s", op_label, len(commands), device.name)
    pager_disable = _PAGER_DISABLE.get(device.vendor)
    stdouts: list[str] = []
    stderrs: list[str] = []
    rc = 0
    for command in commands:
        lines = [pager_disable, command] if pager_disable else [command]
        r, raw_out, err = _run_ssh_interactive(device, lines)
        out = _extraer_salida_comando(raw_out, command)
        stdouts.append(out)
        if err:
            stderrs.append(err)
        if not _exito(r, out, raw_out, vendor=device.vendor):
            rc = 1
    if rc != 0:
        logger.error("ssh_direct_service: %s FAILED on device=%s", op_label, device.name)
    return {
        "rc": rc,
        "stdout": stdouts[0] if stdouts else "",
        "stderr": "\n".join(stderrs),
        "stdouts": stdouts,
    }


def _armar_bloque_cisco(lines: list[str], parents: "str | None") -> str:
    """Traduce la forma Cisco (``lines``[+``parents``] vía ``ios_config``) a
    un bloque de texto crudo autocontenido, mismo criterio que Huawei
    (``command_block``): ``configure terminal`` / ``parents`` (si viene) /
    cada línea / ``end``. Confirmado en vivo contra f3r9s1 que IOS acepta
    esto como 1 solo comando exec no-interactivo, igual que VRP."""
    block = ["configure terminal"]
    if parents:
        block.append(parents)
    block.extend(lines)
    block.append("end")
    return "\n".join(block)


def run_direct(extravars: dict, device) -> dict:
    """Punto de entrada -- ver docstring del módulo. Inspecciona qué forma
    trae *extravars* (las mismas claves mutuamente excluyentes que ya
    imponen ``huawei/run.yml``/``cisco/run.yml``, no hace falta mirar
    ``device.vendor`` aparte)."""
    if "command_block" in extravars:
        return _run_write(device, extravars["command_block"], op_label="command_block")

    if "command_blocks" in extravars:
        combined = "\n".join(extravars["command_blocks"])
        return _run_write(device, combined, op_label="command_blocks (lote)")

    if "lines" in extravars:
        block = _armar_bloque_cisco(extravars["lines"], extravars.get("parents"))
        return _run_write(device, block, op_label="lines")

    if "config_steps" in extravars:
        blocks = [
            _armar_bloque_cisco(step["lines"], step.get("parents"))
            for step in extravars["config_steps"]
        ]
        return _run_write(device, "\n".join(blocks), op_label="config_steps (lote)")

    if "commands" in extravars:
        return _run_reads(device, extravars["commands"], op_label="commands")

    raise ValueError(f"ssh_direct_service.run_direct(): unsupported extravars shape {sorted(extravars)}")
