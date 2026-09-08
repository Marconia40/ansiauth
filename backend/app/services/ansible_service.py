import logging
import os
import tempfile

import ansible_runner as _runner

from app.core.config import ANSIBLE_BASE_PATH, INVENTORY_PATH

logger = logging.getLogger(__name__)

# Ansible SSH / persistent-connection timeouts (seconds).
# Defaults are raised from Ansible's 30s to 60s to give Huawei VRP devices
# more time to respond.  Override with environment variables if needed.
_ANSIBLE_TIMEOUT = os.environ.get("ANSIBLE_TIMEOUT", "60")
_ANSIBLE_PERSISTENT_COMMAND_TIMEOUT = os.environ.get("ANSIBLE_PERSISTENT_COMMAND_TIMEOUT", "60")
_ANSIBLE_PERSISTENT_CONNECT_TIMEOUT = os.environ.get("ANSIBLE_PERSISTENT_CONNECT_TIMEOUT", "60")


def _mask_inventory(inv_str: str) -> str:
    """Redact ansible_password value from inventory strings for safe logging."""
    marker = "ansible_password="
    idx = inv_str.find(marker)
    if idx == -1:
        return inv_str
    value_start = idx + len(marker)
    # Value ends at the next space (inline format) or end of string (ini format)
    value_end = inv_str.find(" ", value_start)
    if value_end == -1:
        value_end = len(inv_str)
    return inv_str[:value_start] + "***REDACTED***" + inv_str[value_end:]


def run_playbook(
    playbook: str,
    extravars: dict,
    inventory: str | None = None,
    device: str | None = None,
) -> dict:
    """Execute an Ansible playbook and return rc/stdout/stderr."""
    if inventory is None:
        logger.warning("No dynamic inventory provided for playbook=%s, falling back to %s", playbook, INVENTORY_PATH)
    inv = inventory or INVENTORY_PATH
    device_label = device or extravars.get("device", "unknown")

    # ── Pre-execution diagnostics ──────────────────────────────────────────────
    resolved_playbook = os.path.join(ANSIBLE_BASE_PATH, "project", playbook)
    logger.info(
        "[DIAG] Running playbook=%s device=%s private_data_dir=%s extravars=%s",
        playbook,
        device_label,
        ANSIBLE_BASE_PATH,
        extravars,
    )
    logger.info(
        "[DIAG] Resolved playbook path: %s",
        resolved_playbook,
    )
    logger.info(
        "[DIAG] Execution cwd=%s",
        os.getcwd(),
    )
    logger.info(
        "[DIAG] Path existence: private_data_dir=%s  resolved_playbook=%s",
        os.path.exists(ANSIBLE_BASE_PATH),
        os.path.exists(resolved_playbook),
    )
    logger.info(
        "[DIAG] Ansible timeout config: ANSIBLE_TIMEOUT=%s  ANSIBLE_PERSISTENT_COMMAND_TIMEOUT=%s  ANSIBLE_PERSISTENT_CONNECT_TIMEOUT=%s",
        _ANSIBLE_TIMEOUT,
        _ANSIBLE_PERSISTENT_COMMAND_TIMEOUT,
        _ANSIBLE_PERSISTENT_CONNECT_TIMEOUT,
    )

    # Log inventory content before the temp-file write so the inline string is visible
    if isinstance(inv, str) and not os.path.exists(inv):
        logger.info(
            "[DIAG] Generated inventory for device=%s (inline, will be written to temp file):\n%s",
            device_label,
            _mask_inventory(inv),
        )
    else:
        logger.info("[DIAG] Using existing inventory path: %s", inv)

    # ansible-runner's dump_artifacts() writes inline inventory strings to
    # private_data_dir/inventory/hosts. When concurrent calls share the same
    # private_data_dir (ANSIBLE_BASE_PATH), each call overwrites the previous
    # one's inventory file, causing both subprocesses to target the same device.
    # Writing the inventory to a unique temp file makes dump_artifacts treat it
    # as an absolute path and skip the shared-file write entirely.
    _temp_inv = None
    if isinstance(inv, str) and not os.path.exists(inv):
        _temp_inv = tempfile.NamedTemporaryFile(mode="w", suffix=".ini", delete=False)
        _temp_inv.write("[all]\n")
        _temp_inv.write(inv + "\n")
        _temp_inv.close()
        inv = _temp_inv.name
        logger.info("[DIAG] Temp inventory file written to: %s  exists=%s", inv, os.path.exists(inv))

    logger.info(
        "[DIAG] ansible_runner.run args: playbook=%s  private_data_dir=%s  inventory=%s  extravars=%s  quiet=True",
        playbook,
        ANSIBLE_BASE_PATH,
        inv,
        extravars,
    )

    # Global SSH-concurrency semaphore: caps the number of ansible-runner
    # subprocesses in flight across the whole system (server-resource
    # protection). Not per-device -- that's ``RedisCoordinator.bloquear()``,
    # taken further up the call chain by the orchestrator/sync services.
    # Lazy import avoids a circular dependency with app.composition.
    from app.composition import redis_coordinator

    try:
        with redis_coordinator.adquirir_slot():
            r = _runner.run(
                private_data_dir=ANSIBLE_BASE_PATH,
                playbook=playbook,
                inventory=inv,
                extravars=extravars,
                quiet=True,
                # ansible_runner.dump_artifacts() only writes env/extravars (and
                # envvars/passwords/settings) when the file doesn't already
                # exist under private_data_dir -- and since this call always
                # reuses the same private_data_dir across every invocation,
                # whatever the FIRST call ever wrote there gets referenced via
                # `-e @env/extravars` on every later call, forever, for any key
                # the current call doesn't happen to override. Bug real
                # encontrado verificando otro fix: un env/extravars viejo
                # (device="sw-review", commands=["show vlan brief"]) quedó
                # pegado desde una corrida anterior y se coló en llamadas
                # posteriores que no pasaban "commands" -- una escritura de VLAN
                # sin ese extravar terminaba igual corriendo ese "show vlan
                # brief" de más contra el device real, sin loguear nada raro.
                # suppress_env_files=True hace que extravars se pase siempre
                # inline (-e '{...}'), nunca por archivo compartido.
                suppress_env_files=True,
                envvars={
                    "ANSIBLE_TIMEOUT": _ANSIBLE_TIMEOUT,
                    "ANSIBLE_PERSISTENT_COMMAND_TIMEOUT": _ANSIBLE_PERSISTENT_COMMAND_TIMEOUT,
                    "ANSIBLE_PERSISTENT_CONNECT_TIMEOUT": _ANSIBLE_PERSISTENT_CONNECT_TIMEOUT,
                },
            )
    finally:
        if _temp_inv is not None:
            os.unlink(_temp_inv.name)

    rc = r.rc
    raw_stderr = _read(r.stderr)
    raw_stdout = _read(r.stdout)

    # ── Post-execution diagnostics ─────────────────────────────────────────────
    logger.info(
        "[DIAG] Playbook completed: playbook=%s device=%s rc=%s status=%s",
        playbook,
        device_label,
        rc,
        getattr(r, "status", "unknown"),
    )
    logger.info("[DIAG] Playbook stdout:\n%s", raw_stdout)
    logger.info("[DIAG] Playbook stderr:\n%s", raw_stderr)

    stderr = raw_stderr
    # When rc != 0, prefer the actual task failure message over ios_command output
    # so ios_config errors aren't masked by an earlier ios_command's stdout.
    failure_reason = _extract_failure_reason(r) if rc != 0 else ""
    ios_cmd_output = _extract_ios_command_output(r)
    if rc != 0 and failure_reason:
        stdout = failure_reason
    elif ios_cmd_output:
        stdout = ios_cmd_output
    else:
        stdout = raw_stdout
    # Per-task stdouts for callers that need every command's output (e.g. the
    # port driver runs three read commands in one playbook).  Always populated
    # so consumers don't need to check for None. Extracted regardless of rc:
    # when ``ios_command`` gets a mixed batch and one command is rejected by
    # the device (``% Invalid input``), the task fails but the per-command
    # responses ARE captured in the event -- callers can pass ``partial_ok=True``
    # to ``_leer()`` to inspect them (used by the port driver to tolerate
    # devices that don't implement ``storm-control``).
    stdouts = _extract_all_command_outputs(r)
    combined_output = (stdout + stderr).lower()
    if "no hosts matched" in combined_output and rc == 0:
        logger.error("Playbook %s: no hosts matched on device=%s — treating as failure", playbook, device_label)
        rc = 1
        stderr = stderr or "No hosts matched in inventory"
        stdouts = []
    # Bug real encontrado en vivo contra f3r9s2: el terminal plugin de
    # Huawei (community.network.ce, deprecado -- ver warnings de ansible)
    # a veces reporta rc=0/"ok" sin haber detectado un error real que el
    # device SÍ devolvió (confirmado: el mismo command_block, reintentado,
    # o bien detecta el error de verdad o aplica limpio con stdout legible
    # -- nunca volvió a dar esto). Cuando eso pasa, el "stdout" que separa
    # es literalmente el string "None" (un to_text(None) del propio módulo,
    # no None de Python) en vez de la salida real -- ninguna escritura
    # genuinamente exitosa de esta sesión devolvió eso, siempre traen el
    # eco real de los comandos. No confiar en el rc=0 acá: se fuerza rc=1
    # con un patrón transitorio (ver _PATRONES_TRANSITORIOS en
    # orquestador.py) para que pase por el mismo reintento automático que
    # ya existe para timeouts/desconexiones, en vez de que el job quede
    # "completed" mintiendo sobre si el comando realmente se aplicó.
    if rc == 0 and stdout == "None":
        logger.warning(
            "Playbook %s: rc=0 but stdout is the literal string 'None' on device=%s "
            "-- treating as an unreliable read, forcing a retry",
            playbook, device_label,
        )
        rc = 1
        stderr = "ansible reported success with no readable output (stdout=='None') -- possible read desync, retrying"
        stdout = ""
        stdouts = []
    result = {"rc": rc, "stdout": stdout, "stderr": stderr, "stdouts": stdouts}
    logger.debug("Ansible raw result: %s", result)
    if rc != 0:
        error_output = (stderr + " " + stdout).strip()
        logger.error("Playbook %s FAILED on device=%s rc=%s error=%s", playbook, device_label, rc, error_output[:1000])
    else:
        logger.info("Playbook %s SUCCESS on device=%s rc=%s", playbook, device_label, rc)
    return result


def build_inventory(
    device_id: str,
    ip: str,
    username: str,
    password: str,
    network_os: str = "ios",
    connection: str = "network_cli",
    port: int | None = None,
) -> str:
    """Build a single-host inline inventory string from device credentials.

    Password-only -- devices with ``auth_method == "key"`` never reach
    this (``VendorDriver._ejecutar()`` routes those to
    ``ssh_direct_service`` instead, bypassing ansible_runner entirely, see
    its docstring for why). *password* is always required here."""
    logger.info(
        "[DIAG] Inventory host vars for %s: host=%s user=%s network_os=%s connection=%s port=%s",
        device_id,
        ip,
        username,
        network_os,
        connection,
        port,
    )
    inv = (
        f"{device_id} "
        f"ansible_host={ip} "
        f"ansible_user={username} "
        f"ansible_password={password} "
        f"ansible_network_os={network_os} "
        f"ansible_connection={connection}"
    )
    if port is not None:
        inv += f" ansible_port={port}"
    return inv


# Directorio dedicado para las claves privadas materializadas en disco --
# separado de ANSIBLE_BASE_PATH (que ansible-runner trata como su propio
# private_data_dir y podría barrer/reescribir) para que nada de ansible-runner
# lo toque por error.
_DEVICE_KEYS_DIR = os.path.join(os.path.dirname(ANSIBLE_BASE_PATH), "device_ssh_keys")


def write_private_key_file(device_name: str, private_key: str) -> str:
    """Materializa la clave privada de *device_name* en un archivo estable
    (0600, directorio 0700) y devuelve su path absoluto.

    Path fijo por-device (no un temp file por conexión) a propósito: evita
    tener que enhebrar un path efímero a través de ``_build_inventory()``
    -> ``run_playbook()`` -> cleanup -- la clave se sobreescribe cada vez
    que se llama (barato, un archivo chico) así que siempre queda al día
    si el device rota su clave vía ``Device.actualizar()``. El contenido
    nunca se loguea (a diferencia de ``ansible_password`` en el inventory,
    acá ni siquiera aparece en la línea del inventory -- solo el path)."""
    os.makedirs(_DEVICE_KEYS_DIR, mode=0o700, exist_ok=True)
    os.chmod(_DEVICE_KEYS_DIR, 0o700)
    path = os.path.join(_DEVICE_KEYS_DIR, device_name)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(private_key)
            if not private_key.endswith("\n"):
                f.write("\n")
    finally:
        os.chmod(path, 0o600)
    return path


def _extract_ios_command_output(r) -> str:
    """Return the first command stdout string from ansible-runner events.

    Handles two result formats:
    - ios_command / ce_command: res.stdout is a list; return stdout[0]
    - cli_command (netcommon): res.stdout is a plain string; return it directly
    Config modules (ios_config, cli_config) produce no stdout field here, so
    they fall through and return "" as expected.
    """
    try:
        for event in r.events:
            if event.get("event") == "runner_on_ok":
                res = event.get("event_data", {}).get("res", {})
                stdout_val = res.get("stdout")
                if isinstance(stdout_val, list) and stdout_val:
                    logger.debug("Extracted list-format command output from events (%d chars)", len(stdout_val[0]))
                    return stdout_val[0]
                if isinstance(stdout_val, str) and stdout_val:
                    logger.debug("Extracted string-format command output from events (%d chars)", len(stdout_val))
                    return stdout_val
    except Exception as exc:
        logger.debug("Could not extract command output from events: %s", exc)
    return ""


def _extract_all_command_outputs(r) -> list[str]:
    """Return every command stdout, in execution order, from ansible-runner events.

    Companion to ``_extract_ios_command_output`` for playbooks that issue
    multiple read commands in sequence and need to consume all of them.
    Handles the same two result shapes:

    * ``ios_command`` / ``ce_command``: ``res.stdout`` is a list per task.
      Each element is appended to the returned list separately so callers
      see one entry per device-side command.
    * ``cli_command`` (netcommon): ``res.stdout`` is a plain string per task.

    Tasks that produce no stdout at all (config modules, ``set_fact``, etc.
    -- the key is simply absent) are silently skipped. A command that DID
    run and legitimately returned an empty string (e.g. "display
    current-configuration configuration dhcp" on a device with nothing
    configured there) is kept as ``""`` -- bug real encontrado probando
    esto contra un device real: el truthiness check viejo (``and
    item_stdout``) tiraba esas entradas vacías en vez de preservarlas,
    lo que corría el índice de todos los comandos siguientes en la misma
    tanda -- ``HuaweiVendor.get_svis()`` (que arma ``config``/``dhcp_groups``
    por posición) terminaba leyendo la salida de una interfaz como si
    fuera la del comando de DHCP, y la interfaz siguiente desaparecía sin
    ningún error visible.

    Returns
    -------
    list[str]
        Command stdouts in the order their tasks completed, 1 por comando
        enviado (incluye entradas ``""`` cuando el device no devolvió nada
        para ese comando puntual). Empty list if no relevant events were
        found.
    """
    outputs: list[str] = []
    try:
        for event in r.events:
            # ``runner_on_failed`` is included on purpose: when ``ios_command``
            # runs a list and one command is rejected by the device (e.g.
            # ``% Invalid input`` on an IOSv image without ``storm-control``),
            # the task fails but Ansible still captures the per-command
            # responses -- ``res.stdout`` holds the same list shape as on
            # success, with the offending entries containing the device's
            # error text. Callers opting into ``_leer(..., partial_ok=True)``
            # inspect those entries to decide which commands to treat as
            # "unsupported" vs. "actually broken".
            if event.get("event") not in ("runner_on_ok", "runner_on_failed"):
                continue
            res = event.get("event_data", {}).get("res", {})
            # Looped task (e.g. huawei/run.yml's "Run read commands", one
            # cli_command per item; cisco/run.yml's tolerant path, one
            # ios_command per item): the aggregate runner_on_ok event has
            # no top-level "stdout" at all -- each item's own stdout lives
            # in its own dict under res["results"] instead. Same event
            # shape that _extract_failure_reason() already accounts for on
            # the failure side.
            #
            # Shape del stdout por item depende del módulo usado en el loop:
            #   - cli_command (Huawei): stdout es str          → append directo
            #   - ios_command (Cisco):  stdout es list[str]    → aplanar SIEMPRE 1 entry (loop 1:1)
            #   - iteración fallida / skipped: stdout ausente  → append "" para no correr el índice
            #
            # Preservar 1-entry-por-iteración es crítico para callers que
            # slicean por posición (list_ports con _STORM_INDEX=3,
            # read_core_state con v_end/p_end); una iteración fallida sin
            # stdout se conserva como "" y el parser degrada la sección
            # afectada a valores None (comportamiento tolerante).
            results = res.get("results")
            if isinstance(results, list):
                for item in results:
                    if not isinstance(item, dict):
                        outputs.append("")
                        continue
                    item_stdout = item.get("stdout")
                    if isinstance(item_stdout, str):
                        outputs.append(item_stdout)
                    elif isinstance(item_stdout, list) and item_stdout:
                        first = item_stdout[0]
                        outputs.append(first if isinstance(first, str) else "")
                    else:
                        outputs.append("")
                continue
            stdout_val = res.get("stdout")
            if isinstance(stdout_val, list):
                for entry in stdout_val:
                    if isinstance(entry, str):
                        outputs.append(entry)
            elif isinstance(stdout_val, str):
                outputs.append(stdout_val)
    except Exception as exc:
        logger.debug("Could not extract command outputs from events: %s", exc)
    logger.debug("Extracted %d command output(s) from events", len(outputs))
    return outputs


def _extract_failure_reason(r) -> str:
    """Return the failure message from the first runner_on_failed event.

    ios_config and other modules store the error in event_data.res.msg.
    This is checked before ios_command output so actual errors aren't masked
    by a successful earlier task's stdout.

    Bug real de producción encontrado con un fallo real (Huawei list_vlans
    devolviendo "Cannot read state on device '...': One or more items
    failed" -- inútil para diagnosticar nada): para una task con ``loop:``
    (huawei/run.yml's "Run read commands", agregada en la unificación de
    drivers de esta sesión -- Cisco no tiene este problema, su equivalente
    no usa loop), ``res.msg`` en el evento ``runner_on_failed`` es SIEMPRE
    el resumen genérico de Ansible ``"One or more items failed"`` -- el
    error real de CADA item vive en ``res["results"]`` (una lista, un dict
    por iteración del loop), nunca antes revisado acá. Verificado con un
    playbook local real (loop de 2 comandos, uno falla) reproduciendo el
    evento exacto: ``res == {"results": [...], "msg": "One or more items
    failed", ...}``, con el item fallido en ``results[i]`` cargando su
    propio ``msg``/``stdout``/``stderr`` reales."""
    try:
        for event in r.events:
            if event.get("event") == "runner_on_failed":
                data = event.get("event_data", {})
                res = data.get("res", {})
                results = res.get("results")
                if isinstance(results, list):
                    for item in results:
                        if isinstance(item, dict) and item.get("failed"):
                            item_msg = (
                                item.get("msg") or item.get("stderr") or item.get("stdout")
                            )
                            if item_msg:
                                item_label = item.get("item", "")
                                combined = f"[{item_label}] {item_msg}" if item_label else str(item_msg)
                                logger.debug("Extracted per-item failure reason from loop results: %s", combined[:200])
                                return combined
                msg = res.get("msg") or res.get("stdout") or data.get("task", "")
                if msg:
                    logger.debug("Extracted failure reason from events: %s", str(msg)[:200])
                    return str(msg)
    except Exception as exc:
        logger.debug("Could not extract failure reason from events: %s", exc)
    return ""


def _read(stream) -> str:
    if not stream:
        return ""
    try:
        return stream.read() if hasattr(stream, "read") else str(stream)
    except Exception:
        return ""
