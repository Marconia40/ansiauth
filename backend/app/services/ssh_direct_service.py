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
import tempfile

from app.core.config import ANSIBLE_BASE_PATH
from app.services.vendors.base import limpiar_ruido_benigno as _limpiar_ruido_benigno

logger = logging.getLogger(__name__)

_SSH_CONNECT_TIMEOUT = int(os.environ.get("SSH_DIRECT_CONNECT_TIMEOUT", "15"))
_SSH_COMMAND_TIMEOUT = int(os.environ.get("SSH_DIRECT_COMMAND_TIMEOUT", "60"))

# Adaptación real por-device -- pedido explícito del usuario tras encontrar
# equipos donde un combo fijo de algoritmos legacy (lo que había acá antes,
# confirmado solo contra f3r9s1/f3r9s2) NO sirve para un 3er device con su
# propio firmware/plataforma. En vez de adivinar una lista estática,
# ``_run_ssh_interactive()`` PARSEA la oferta real del device del propio
# mensaje de error de OpenSSH -- "Unable to negotiate with HOST port PORT:
# no matching FOO found. Their offer: A,B,C" -- y reintenta con
# exactamente esos algoritmos, sea cual sea el device. Mismo criterio que
# ``alternatives``/``triggered_by_error`` en commands.yaml (reintento
# guiado por el error real, no una lista fija adivinada de antemano)
# aplicado acá al nivel de transporte.
#
# "no matching host key type" cubre tanto HostKeyAlgorithms como
# PubkeyAcceptedKeyTypes -- confirmado en vivo (f3r9s2) que hace falta
# setear los 2 con la misma oferta, uno solo no alcanza para autenticar
# por clave pública.
_NEGOTIATION_FAILURE_RE = re.compile(
    r"no matching (key exchange method|host key type|cipher) found\. "
    r"Their offer: ([\w@.,\-]+)"
)
_NEGOTIATION_OPTION_NAMES: "dict[str, list[str]]" = {
    "key exchange method": ["KexAlgorithms"],
    "host key type": ["HostKeyAlgorithms", "PubkeyAcceptedKeyTypes"],
    "cipher": ["Ciphers"],
}
# Techo de reintentos -- 1 por categoría posible (kex/host key/cipher), no
# más. Si después de adaptar las 3 el device SIGUE sin negociar, seguir
# reintentando no cambiaría nada; se devuelve el error real tal cual.
_MAX_NEGOTIATION_RETRIES = len(_NEGOTIATION_OPTION_NAMES)

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

# Directorio dedicado para las claves privadas materializadas en disco --
# separado de ANSIBLE_BASE_PATH (que ansible-runner trata como su propio
# private_data_dir y podría barrer/reescribir) para que nada de ansible-runner
# lo toque por error.
_DEVICE_KEYS_DIR = os.path.join(os.path.dirname(ANSIBLE_BASE_PATH), "device_ssh_keys")


def write_private_key_file(device_name: str, private_key: str) -> str:
    """Materializa la clave privada de *device_name* en un archivo temporal
    único (0600, directorio 0700) y devuelve su path absoluto.

    Un archivo por llamada, no un path fijo por-device -- mismo criterio que
    ``ansible_password`` en el inventory inline de ``ansible_service.py``
    (temp file + ``os.unlink()`` apenas se usa). El caller (``_start_agent()``
    acá mismo) es responsable de borrar el archivo una vez cargado en el
    ssh-agent efímero."""
    os.makedirs(_DEVICE_KEYS_DIR, mode=0o700, exist_ok=True)
    os.chmod(_DEVICE_KEYS_DIR, 0o700)
    fd, path = tempfile.mkstemp(prefix=f"{device_name}-", dir=_DEVICE_KEYS_DIR)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(private_key)
            if not private_key.endswith("\n"):
                f.write("\n")
    finally:
        os.chmod(path, 0o600)
    return path


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
    fuerza el flujo de 2 fases que el device sí entiende.

    *keyfile* es un temp file de un solo uso (``write_private_key_file()``
    acá mismo) -- se borra acá apenas ``ssh-add`` lo carga a memoria del
    agent, mismo criterio que ``ansible_password`` en el inventory inline de
    ``ansible_service.py`` (nunca queda un archivo con la clave en texto
    plano dando vueltas en disco más de lo estrictamente necesario)."""
    proc = subprocess.run(["ssh-agent", "-s"], capture_output=True, text=True, timeout=5)
    env = dict(os.environ)
    for match in _AGENT_LINE_RE.finditer(proc.stdout):
        env[match.group(1)] = match.group(2)
    try:
        subprocess.run(["ssh-add", keyfile], env=env, capture_output=True, timeout=5)
    finally:
        os.unlink(keyfile)
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
    keyfile = write_private_key_file(device.name, device.private_key)
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
# ``display clock`` anden bien. Se manda como líneas previas de cada
# sesión interactiva -- confirmado en vivo contra f3r9s2 que "no molesta"
# aunque la salida sea corta.
#
# Cisco además gana "terminal width 0" -- bug real encontrado en vivo
# contra f2r11s1: un comando de 1 sola línea pero largo (el include con
# varias fechas alternadas de get_log_buffer()) se redibuja/corta al
# tipearse si excede el ancho de terminal default (80 cols), y
# ``_extraer_salida_comando()`` busca el eco EXACTO del comando para
# recortar banner/prompt -- si el eco viene partido por el wrap, no lo
# encuentra y el transcript crudo completo (incluido el comando corrupto)
# se cuela en el resultado. "terminal width 0" desactiva el wrap, mismo
# tipo de fix que ``screen-width 512`` ya usa Huawei para el mismo
# problema en escrituras (ver commands.yaml). No confirmado si VRP
# necesita el equivalente para reads -- no tocado acá, fuera de alcance.
_PAGER_DISABLE = {
    "huawei_vrp": ["screen-length 0 temporary"],
    "cisco_ios": ["terminal length 0", "terminal width 0"],
}


def _run_ssh_interactive(
    device, lines: list[str], known_extra_opts: "list[str] | None" = None,
) -> tuple[int, str, str, list[str]]:
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
    (IOS, alias de ``exit``) corta la conexión limpio.

    Intenta primero con los algoritmos DEFAULT de OpenSSH -- pedido
    explícito del usuario: no todos los devices necesitan (o toleran) los
    mismos overrides legacy, cada fabricante/firmware ofrece su propio
    combo. Si falla por negociación de protocolo (``_NEGOTIATION_FAILURE_RE``
    -- pasa ANTES de autenticación), el mensaje de error de OpenSSH ya
    incluye la oferta REAL del device ("Their offer: A,B,C") -- se
    reintenta agregando exactamente esos algoritmos a la categoría que
    falló (KEX/host key/cipher), no una lista fija adivinada de antemano.
    Se repite hasta ``_MAX_NEGOTIATION_RETRIES`` veces (1 por categoría) o
    hasta que deje de haber progreso (misma categoría repetida = ya se
    intentó, no tiene sentido seguir). Cualquier otro tipo de fallo (auth,
    comando rechazado, timeout) no dispara ningún reintento -- cambiar de
    algoritmos no arreglaría nada ahí.

    *known_extra_opts*: algoritmos ya aprendidos en una llamada anterior
    de la MISMA sesión de lectura (ver ``_run_reads()``) -- se usan como
    punto de partida en vez de arrancar siempre desde los defaults de
    OpenSSH. Bug real observado en vivo contra f3r9s1/f3r9s2/f2r11s1:
    cada comando del batch (hasta 8 por read) reabre su propia conexión
    SSH, y sin esto cada una repetía la MISMA negociación fallida +
    retry desde cero -- 30-40s de negociación pura desperdiciada por
    sync en devices con algoritmos legacy, comiéndose buena parte del
    presupuesto de ``_SSH_COMMAND_TIMEOUT`` por comando y acercando esos
    reads al timeout total (mecanismo real detrás del wipe de inventario
    de f2r6s7 -- ver ``vendors/base.py::_leer()``). Se devuelve el
    ``extra_opts`` final (con lo aprendido en ESTA llamada incluido) para
    que el caller lo pase a la próxima invocación.

    ``LogLevel=INFO``, no ``ERROR`` -- bug real encontrado implementando
    esto: confirmado en vivo que con ``LogLevel=ERROR`` (más silencioso
    que el default real de OpenSSH, "INFO") el mensaje "Unable to
    negotiate..." queda COMPLETAMENTE suprimido -- rc=255, stdout y
    stderr vacíos, nada que parsear. Confirmado que ``INFO`` (el nivel
    donde el mensaje sí aparece) no agrega ruido extra a una sesión
    exitosa -- stdout queda igual de limpio."""
    base_cmd = [
        "ssh", "-tt",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=INFO",
        "-o", f"ConnectTimeout={_SSH_CONNECT_TIMEOUT}",
    ]
    stdin_data = "\n".join([*lines, "quit"]) + "\n"
    with _agent_for(device) as agent_env:
        extra_opts: list[str] = list(known_extra_opts) if known_extra_opts else []
        categorias_probadas: set[str] = set()
        for _ in range(_MAX_NEGOTIATION_RETRIES + 1):
            cmd = [*base_cmd, *extra_opts, f"{device.username}@{device.host}"]
            try:
                proc = subprocess.run(
                    cmd, input=stdin_data, capture_output=True, text=True,
                    timeout=_SSH_COMMAND_TIMEOUT, env=agent_env,
                )
            except subprocess.TimeoutExpired:
                return 1, "", f"ssh command timed out after {_SSH_COMMAND_TIMEOUT}s", extra_opts
            except Exception as exc:
                logger.exception("ssh_direct_service: interactive ssh invocation failed device=%s", device.name)
                return 1, "", str(exc), extra_opts
            m = _NEGOTIATION_FAILURE_RE.search(proc.stderr)
            if m is None:
                return proc.returncode, proc.stdout, proc.stderr, extra_opts
            categoria, oferta = m.groups()
            if categoria in categorias_probadas:
                # Ya adaptamos esta categoría antes y sigue fallando (o el
                # device ofrece algo que ninguna combinación resuelve) --
                # más reintentos no cambiarían nada, devolver el error real.
                return proc.returncode, proc.stdout, proc.stderr, extra_opts
            categorias_probadas.add(categoria)
            for opt_name in _NEGOTIATION_OPTION_NAMES.get(categoria, []):
                extra_opts += ["-o", f"{opt_name}=+{oferta}"]
            logger.info(
                "ssh_direct_service: negotiation failed on device=%s (%s, their offer: %s) "
                "-- retrying with that exact offer added to %s",
                device.name, categoria, oferta, _NEGOTIATION_OPTION_NAMES.get(categoria, []),
            )
        return proc.returncode, proc.stdout, proc.stderr, extra_opts


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


def _exito(stdout: str, raw: str, vendor: "str | None" = None) -> bool:
    # Bug real encontrado en vivo contra f2r11s1: el shortcut "rc == 0 ->
    # éxito" (ya sacado del todo, ni se recibe rc como parámetro) asumía
    # que un rc limpio del cliente ssh local implica que la
    # sesión remota llegó hasta el final -- falso para este device en un
    # read de mucho volumen. El device a veces cierra el canal "prolijo"
    # desde su lado cuando decide cortar la salida (no manda un RST/error,
    # simplemente deja de escribir y cierra) -- OpenSSH lo interpreta como
    # una terminación normal y el cliente sale con rc=0 igual, aunque el
    # comando nunca terminó de verdad (nuestro "quit" final nunca llegó a
    # ecoarse, confirmado comparando el mismo read repetido: a veces rc=0
    # truncado a los ~57KB de siempre, a veces rc=1 con "Connection closed
    # by remote host" -- mismo corte, señal de rc inconsistente). Por
    # eso ya no se confía en rc en absoluto para decidir éxito -- solo
    # importa si vimos nuestro propio terminador ecoado (ver
    # _sesion_completa(), ya confiable en la dirección opuesta: VRP/IOS
    # rc!=0 con la sesión en realidad completa).
    if _tiene_error(stdout, vendor):
        return False
    return _sesion_completa(raw)


def _run_write(device, block: str, *, op_label: str) -> dict:
    logger.info("ssh_direct_service: %s on device=%s", op_label, device.name)
    _, stdout, stderr, _ = _run_ssh_interactive(device, block.split("\n"))
    rc = 0 if _exito(stdout, stdout, vendor=device.vendor) else 1
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
    # Bug real encontrado en vivo contra f2r11s1: un comando con un espacio
    # final literal (el include-por-fecha de get_log_buffer(), construido
    # con "...) " a propósito) nunca matcheaba -- ``line.rstrip()`` pela
    # el espacio final del eco antes de comparar, pero *command* (sin
    # rstrip) seguía terminando en espacio, así que ``endswith(command)``
    # jamás daba True. Con ``command`` también rstripeado, ambos lados se
    # comparan igual de "sin espacios finales", sin importar qué haya
    # tipeado el caller.
    command = command.rstrip()
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


def _extraer_salidas_comandos(raw: str, commands: list[str]) -> list[str]:
    """Generaliza ``_extraer_salida_comando()`` a N comandos mandados en UNA
    sola sesión (ver ``_run_reads()``): busca el eco de cada *command*, EN
    ORDEN, avanzando siempre hacia adelante desde donde terminó el anterior
    -- así un comando repetido dos veces en el mismo batch igual se
    resuelve contra su propia ocurrencia. La salida de cada comando va
    desde su eco hasta el eco del PRÓXIMO comando encontrado (o hasta
    nuestro "quit" final para el último) -- mismo criterio de "buscar el
    terminador desde el final" que ya usa la versión de 1 solo comando
    para no confundirse con un "quit" legítimo que venga en el medio del
    output real (ej. bloques ``crypto pki certificate chain`` de Cisco).

    Si el eco de un comando no aparece (la sesión se cortó antes de
    llegar a tipearlo -- device se cayó a mitad del batch), ese slot
    devuelve "" -- por construcción (la búsqueda de cada eco arranca
    donde terminó la anterior, sobre un stream que se manda de una sola
    vez), si el eco del comando N no aparece ninguno de los siguientes
    puede aparecer tampoco, así que todo lo que sobró del transcript
    (incluido un eventual mensaje de corte real, ej. "Connection reset by
    peer", si llegó a aparecer en stdout antes de morir) queda atado al
    ÚLTIMO comando cuyo eco sí se encontró -- mismo criterio que
    ``_extraer_salida_comando()`` ya usaba para 1 comando (correr hasta
    el final si nuestro "quit" nunca se ecoa)."""
    lines = raw.splitlines()
    quit_idx = len(lines)
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].rstrip().endswith("quit"):
            quit_idx = i
            break

    echo_idxs: "list[int | None]" = []
    search_from = 0
    for command in commands:
        cmd = command.rstrip()
        found = None
        for i in range(search_from, len(lines)):
            if lines[i].rstrip().endswith(cmd):
                found = i
                break
        echo_idxs.append(found)
        if found is not None:
            search_from = found + 1

    outputs: list[str] = []
    for pos, idx in enumerate(echo_idxs):
        if idx is None:
            outputs.append("")
            continue
        end = quit_idx
        for later_idx in echo_idxs[pos + 1:]:
            if later_idx is not None:
                end = later_idx
                break
        outputs.append("\n".join(lines[idx + 1:end]).strip("\n"))
    return outputs


def _run_reads(device, commands: list[str], *, op_label: str) -> dict:
    """Manda TODOS los *commands* del batch por stdin de UNA sola sesión
    SSH (mismo mecanismo que ``_run_write()`` ya usa para bloques de
    config multi-línea), en vez de abrir una conexión nueva por comando.

    Bug real observado en vivo (f3r9s1/f3r9s2/f2r11s1/f2r10s1): con 1
    conexión por comando, un read de 8 comandos contra un device con
    algoritmos SSH legacy pagaba la negociación completa 8 veces --
    ~24 conexiones TCP+SSH en ~16s para UN SOLO read (confirmado en el
    log de producción), la clase de ráfaga que puede empujar a un device
    con firmware viejo a resetear la conexión. Con 1 sola sesión para
    todo el batch, la negociación se paga una sola vez por read (y el
    lock del device se sostiene una fracción del tiempo).

    Los outputs de cada comando se separan del transcript combinado con
    ``_extraer_salidas_comandos()`` -- el contrato de salida (lista de N
    strings, mismo orden que *commands*) no cambia, así que los parsers
    (``CiscoPortParser``, etc.) no necesitan tocarse."""
    logger.info("ssh_direct_service: %s (%d command(s)) on device=%s", op_label, len(commands), device.name)
    pager_disable = _PAGER_DISABLE.get(device.vendor, [])
    lines = [*pager_disable, *commands]
    _, raw_out, err, _extra_opts = _run_ssh_interactive(device, lines)
    stdouts = _extraer_salidas_comandos(raw_out, commands)
    stderrs = [err] if err else []

    # _sesion_completa() se evalúa 1 sola vez para todo el batch (antes
    # era por comando, porque cada uno tenía su propia sesión/su propio
    # "quit" propio) -- si la sesión compartida no llegó a nuestro
    # terminador final, algo se cortó en algún punto del batch, así que
    # el read completo se marca fallido aunque los comandos que sí
    # llegaron a ecoarse conserven su contenido real (para que
    # ``partial_ok`` los siga pudiendo usar).
    session_ok = _sesion_completa(raw_out)
    rc = 0
    for out in stdouts:
        if not session_ok or _tiene_error(out, device.vendor):
            rc = 1
            break
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
