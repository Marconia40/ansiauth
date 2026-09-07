from __future__ import annotations

import inspect
import ipaddress
import logging
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.device import Device
    from app.models.svi import SVI
    from app.models.port import Puerto
    from app.models.vlan import VLAN
    from app.models.global_config import GlobalConfig

logger = logging.getLogger(__name__)


class VendorDriver(ABC):
    """Abstract base class every vendor driver must implement — VLAN and
    port operations fused into one contract (FINAL_ARCHITECTURE.md §1.6:
    ``Device.driver`` is a single property returning a single object, so a
    vendor can no longer be split across separate VLAN/port driver classes).

    All concrete drivers must satisfy this interface so that callers can
    remain fully vendor-agnostic.

    VLAN return-value contracts
    ----------------------------
    Mutation operations (create / update / delete / save_config):
        dict with keys:
            rc      – int  : Ansible return code (0 = success, non-zero = failure)
            stdout  – str  : combined playbook stdout
            stderr  – str  : combined playbook stderr
            success – bool : True iff rc == 0  (normalized convenience field)

    Query operations (list_vlans / get_vlans / get_vlan):
        list_vlans / get_vlans → list[VLAN]
        get_vlan               → VLAN | None

    Port query-operation contract
    ------------------------------
    ``list_ports(device, password)`` must return a normalized
    ``list[Puerto]``.  Implementations must:

    * filter out pseudo-interfaces (SVIs, loopbacks, NULL, ...);
    * never invent values — missing fields become ``None``;
    * raise ``RuntimeError`` if the playbook fails or returns unparseable
      output (callers convert this to a 502/500 at the API boundary).

    Port mutation operations default to raising ``NotImplementedError`` so
    vendors that haven't wired a given operation yet are explicit about the
    gap — the orchestration layer converts this into a controlled
    ``UnsupportedVendorError``/501 for the API client.

    Shared execution helpers
    -------------------------
    Concrete drivers used to each reimplement "build extravars, run the
    playbook, normalize the result, log" for every single operation --
    8 of ~14 methods were identical Python across ``CiscoVendor``/
    ``HuaweiVendor``, differing only in which playbook they called. That
    boilerplate now lives once, here, as ``_aplicar()``/``_leer()``.
    Concrete drivers only need to declare 3 class attributes
    (``_PLAYBOOK``, ``_NETWORK_OS``, ``_CONNECTION``) and, per operation,
    build the small vendor-specific ``extravars`` dict the one shared
    playbook understands -- that command construction is the one thing
    that's genuinely different per vendor (``switchport access vlan X``
    vs. ``port default vlan X``), everything around it is not.
    """

    _PLAYBOOK: str
    _NETWORK_OS: str
    _CONNECTION: str = "network_cli"

    def _build_inventory(self, device: Device, password: str) -> dict:
        from app.services import ansible_service
        return ansible_service.build_inventory(
            device.name, device.host, device.username, password,
            network_os=self._NETWORK_OS, connection=self._CONNECTION,
        )

    def _aplicar(self, extravars: dict, device: Device, password: str, *, op_label: str) -> dict:
        """Run this driver's single playbook with *extravars* and normalize
        the result to ``{"rc", "stdout", "stderr", "success"}``.

        Shared by every mutation method (and ``save_config()``) across both
        concrete drivers -- what varies per call is only the shape of
        *extravars* (``{"lines":..., "parents":...}`` for Cisco,
        ``{"command_block":...}`` for Huawei, or ``{"commands":[...]}`` for
        either vendor's "run a bare exec command" case, e.g. Cisco's
        ``write``).
        """
        import traceback

        from app.services import ansible_service

        logger.info("%s: %s on device=%s", type(self).__name__, op_label, device.name)
        try:
            result = ansible_service.run_playbook(
                playbook=self._PLAYBOOK,
                extravars={**extravars, "device": device.name},
                inventory=self._build_inventory(device, password),
            )
            normalized = {**result, "success": result.get("rc", 1) == 0}
            if normalized["success"]:
                logger.info("%s: %s OK on device=%s", type(self).__name__, op_label, device.name)
            else:
                logger.error(
                    "%s: %s FAILED on device=%s — %s",
                    type(self).__name__, op_label, device.name,
                    result.get("stderr") or result.get("stdout"),
                )
            return normalized
        except Exception as exc:
            logger.exception(
                "FULL %s TRACEBACK [%s device=%s]: %s\n%s",
                type(self).__name__.upper(), op_label, device.name, str(exc), traceback.format_exc(),
            )
            raise

    def _leer(self, commands: list[str], device: Device, password: str) -> list[str]:
        """Run this driver's single playbook in "read" mode (``commands``)
        and return every command's stdout, in execution order.

        Raises ``RuntimeError`` on a non-zero rc or empty output -- callers
        (``list_vlans``/``list_ports``) hand the returned strings to their
        vendor-specific parser, unchanged.
        """
        from app.services import ansible_service

        logger.info("%s: run %d command(s) on device=%s", type(self).__name__, len(commands), device.name)
        result = ansible_service.run_playbook(
            playbook=self._PLAYBOOK,
            extravars={"commands": commands, "device": device.name},
            inventory=self._build_inventory(device, password),
        )
        if result["rc"] != 0:
            error = result.get("stderr") or result.get("stdout") or "playbook exited non-zero"
            logger.error("%s: read failed on device=%s — %s", type(self).__name__, device.name, error)
            raise RuntimeError(f"Cannot read state on device '{device.name}': {error}")
        stdouts = result.get("stdouts") or []
        if not stdouts:
            raise RuntimeError(f"Cannot read state on device '{device.name}': no command output returned")
        return stdouts

    _commands_cache: dict | None = None

    @classmethod
    def _cargar_comandos(cls) -> dict:
        """Carga y cachea (una vez por clase) ``commands.yaml`` ubicado
        junto al módulo del driver concreto -- ``CiscoVendor``/
        ``HuaweiVendor`` no declaran la ruta, se resuelve sola desde dónde
        vive la clase real, así una 3ra clase concreta futura solo necesita
        poner su propio ``commands.yaml`` al lado del archivo, sin tocar
        esto."""
        if cls._commands_cache is None:
            import yaml
            path = Path(inspect.getfile(cls)).parent / "commands.yaml"
            with open(path, encoding="utf-8") as f:
                cls._commands_cache = yaml.safe_load(f)
        return cls._commands_cache

    def _lineas_repetidas(self, step: dict, vars: dict) -> list[str]:
        """``repeat`` arma N líneas a partir de una lista en *vars* -- para
        operaciones "full-replace de una lista de tamaño variable" (ej.
        DHCP relay servers, ``ip helper-address``/``dhcp relay
        server-ip``/``dhcp relay server group`` uno por IP) que
        ``lines``/``block`` solos no pueden expresar (son listas FIJAS).
        Todo comando sigue viviendo en el YAML -- esto reemplaza el bypass
        que antes armaba la lista a mano en el driver.

        Shape de ``repeat`` en el YAML::

            repeat:
              over: "servers"                   # nombre de la lista en vars
              prefix_if_nonempty: ["..."]        # opcional, 1 vez, solo si la lista NO está vacía (antes de los items)
              line: "... {item} ... {index} ..."  # 1 vez por elemento -- {item} = elemento actual, {index} = posición (0-based)
              suffix_if_nonempty: ["..."]        # opcional, 1 vez, solo si la lista NO está vacía (después de los items)
              if_empty: ["..."]                  # opcional, en vez de prefix/line/suffix cuando la lista SÍ está vacía
        """
        repeat = step.get("repeat")
        if not repeat:
            return []
        items = vars.get(repeat["over"]) or []
        lineas: list[str] = []
        if items:
            lineas += [l.format(**vars) for l in repeat.get("prefix_if_nonempty", [])]
            lineas += [
                repeat["line"].format(item=item, index=i, **vars) for i, item in enumerate(items)
            ]
            lineas += [l.format(**vars) for l in repeat.get("suffix_if_nonempty", [])]
        else:
            lineas += [l.format(**vars) for l in repeat.get("if_empty", [])]
        return lineas

    def _ejecutar_paso(self, step: dict, vars: dict, device: Device, password: str, *, op_label: str) -> dict:
        """Renderiza un paso del YAML (``lines``[+``parents``], ``block``, o
        ``commands`` -- se infiere de qué clave está presente, no hace
        falta declarar el modo aparte) sustituyendo *vars* con
        ``str.format()``, arma el extravars shape que ``run.yml`` de este
        vendor entiende, y corre ``_aplicar()`` (sin tocar).

        ``repeat`` (ver ``_lineas_repetidas()``) y ``trailer`` (líneas
        fijas después de las repetidas, ej. ``commit``/``quit``/``quit``
        de Huawei) se agregan al final de ``lines``/``block`` cuando
        están presentes. ``match`` (Cisco/``ios_config`` -- ver nota en
        ``set_svi_dhcp_relay`` de ambos vendors) pasa directo al
        extravars si está presente."""
        extra = self._lineas_repetidas(step, vars)
        trailer = [l.format(**vars) for l in step.get("trailer", [])]
        if "commands" in step:
            extravars = {"commands": [c.format(**vars) for c in step["commands"]] + extra + trailer}
        elif "block" in step:
            todas = [c.format(**vars) for c in step["block"]] + extra + trailer
            extravars = {"command_block": "\n".join(todas)}
        else:
            todas = [c.format(**vars) for c in step["lines"]] + extra + trailer
            extravars = {"lines": todas}
            if "parents" in step:
                extravars["parents"] = step["parents"].format(**vars)
        if "match" in step:
            extravars["match"] = step["match"]
        return self._aplicar(extravars, device, password, op_label=op_label)

    def _aplicar_desde_template(
        self, op_key: str, vars: dict, device: Device, password: str, *, variant: str | None = None,
    ) -> dict:
        """Busca *op_key* (y *variant* si se pasa -- operaciones binarias
        como PoE/storm-control/admin-state usan 2 sub-claves nombradas en
        vez de que Python arme el texto condicional) en
        ``self._cargar_comandos()``, corre ``primary``. Si falla, recorre
        ``alternatives`` en orden y compara ``triggered_by_error`` (regex)
        contra ``stderr+stdout`` del resultado -- la primera que matchea se
        reintenta una vez. Si ninguna matchea (o no hay alternativas), se
        devuelve el resultado original con el error real, sin ocultarlo --
        mismo criterio que ya sigue el resto del pipeline de errores."""
        entry = self._cargar_comandos()[op_key]
        if variant is not None:
            entry = entry[variant]
        resultado = self._ejecutar_paso(entry["primary"], vars, device, password, op_label=op_key)
        if resultado["success"]:
            return resultado
        error_text = (resultado.get("stderr") or "") + (resultado.get("stdout") or "")
        # Bug real encontrado contra un device real: ansible.netcommon.cli_command
        # arma su mensaje de fallo con str() sobre los bytes crudos del device
        # (confirmado -- "Task failed: b'...'"), lo que deja "\r\n" como texto
        # literal (4 caracteres: \, r, \, n) en vez de los bytes de control
        # reales -- ningún trigger con \r?\n matcheaba nunca, la alternativa no
        # se disparaba jamás. Normalizar de vuelta a bytes de control reales acá,
        # una sola vez, arregla todos los triggers existentes y futuros sin
        # tocar cada regex.
        error_text = error_text.replace("\\r\\n", "\r\n").replace("\\r", "\r").replace("\\n", "\n")
        for alt in entry.get("alternatives", []):
            if re.search(alt["triggered_by_error"], error_text):
                logger.info(
                    "%s: %s primary failed, retrying with known alternative on device=%s",
                    type(self).__name__, op_key, device.name,
                )
                return self._ejecutar_paso(alt, vars, device, password, op_label=f"{op_key} (alt)")
        return resultado

    @staticmethod
    def _compress_to_ranges(vlans: list[int]) -> list[tuple[int, int]]:
        """Collapse *vlans* into (start, end) range tuples.

        Shared by both concrete drivers' VLAN-list formatting -- was
        duplicated byte-for-byte in each (``_compress_vlans_cisco``/
        ``_compress_vlans_huawei`` only differ in the join format).
        """
        if not vlans:
            return []
        sv = sorted(set(vlans))
        ranges: list[tuple[int, int]] = []
        start = sv[0]
        prev = sv[0]
        for v in sv[1:]:
            if v == prev + 1:
                prev = v
            else:
                ranges.append((start, prev))
                start = v
                prev = v
        ranges.append((start, prev))
        return ranges

    @staticmethod
    def _is_description_empty(description: str) -> bool:
        """True when *description* should clear the port description
        (``no description``/``undo description``) instead of setting it.

        Was duplicated byte-for-byte in both concrete drivers'
        ``update_port_description()`` -- only the negation keyword
        differs per vendor, so that's the only part that stays local to
        each driver."""
        return not bool(description and description.strip())

    # ── VLAN mutation operations (must be implemented by every driver) ───────

    @abstractmethod
    def create_vlan(self, vlan_id: int, name: str, device: Device, password: str) -> dict:
        """Provision a new VLAN on the target device.

        Parameters
        ----------
        vlan_id:
            Numeric VLAN identifier (1–4094).
        name:
            Human-readable label to assign to the VLAN.
        device:
            Domain device object exposing .name, .host, .username.
        password:
            Plaintext device password (decrypted by caller before passing in).

        Returns
        -------
        dict
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``
        """

    @abstractmethod
    def delete_vlan(self, vlan_id: int, device: Device, password: str) -> dict:
        """Remove an existing VLAN from the target device.

        Parameters
        ----------
        vlan_id:
            Numeric VLAN identifier of the VLAN to remove.
        device:
            Domain device object exposing .name, .host, .username.
        password:
            Plaintext device password (decrypted by caller before passing in).

        Returns
        -------
        dict
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``
        """

    @abstractmethod
    def update_vlan(self, vlan_id: int, name: str, device: Device, password: str) -> dict:
        """Rename / update the description of an existing VLAN.

        Parameters
        ----------
        vlan_id:
            Numeric VLAN identifier of the VLAN to update.
        name:
            New label to assign to the VLAN.
        device:
            Domain device object exposing .name, .host, .username.
        password:
            Plaintext device password (decrypted by caller before passing in).

        Returns
        -------
        dict
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``
        """

    @abstractmethod
    def save_config(self, device: Device, password: str) -> dict:
        """Persist the running configuration to non-volatile storage.

        On platforms where changes are committed automatically (e.g. some
        Cisco IOS versions), implementations should raise ``NotImplementedError``
        to signal that this operation is not applicable.

        Parameters
        ----------
        device:
            Domain device object exposing .name, .host, .username.
        password:
            Plaintext device password (decrypted by caller before passing in).

        Returns
        -------
        dict
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``
        """

    # ── VLAN query operations (abstract core + concrete normalized surface) ──

    @abstractmethod
    def get_vlans(self, device: Device, password: str) -> list[VLAN]:
        """Return VLANs configured on *device*.

        Backward-compatible entry point.  New code should call
        ``list_vlans()`` instead.

        Implementations must parse vendor-specific CLI output and normalize
        each entry into a ``VLAN`` object, excluding reserved / internal
        VLANs.

        Parameters
        ----------
        device:
            Domain device object exposing .name, .host, .username.
        password:
            Plaintext device password (decrypted by caller before passing in).

        Returns
        -------
        list[VLAN]
            Normalized VLAN entries.

        Raises
        ------
        RuntimeError
            If the playbook fails or returns unparseable output.
        """

    def list_vlans(self, device: Device, password: str) -> list[VLAN]:
        """Normalized entry point for listing VLANs on *device*.

        Preferred over ``get_vlans()`` in new code.  The default
        implementation delegates to ``get_vlans()`` so that drivers which
        have not yet been updated continue to work transparently.  Drivers
        that override this method should in turn make ``get_vlans()`` delegate
        here to keep both names consistent.

        Parameters
        ----------
        device:
            Domain device object exposing .name, .host, .username.
        password:
            Plaintext device password (decrypted by caller before passing in).

        Returns
        -------
        list[VLAN]
            Normalized VLAN entries.
        """
        return self.get_vlans(device, password)

    def get_vlan(self, vlan_id: int, device: Device, password: str) -> VLAN | None:
        """Return the ``VLAN`` for *vlan_id* on *device*, or ``None`` if absent.

        Default implementation performs a full ``list_vlans()`` scan.
        Drivers that support a more efficient single-item fetch may override
        this method.

        Parameters
        ----------
        vlan_id:
            Numeric VLAN identifier to look up.
        device:
            Domain device object exposing .name, .host, .username.
        password:
            Plaintext device password (decrypted by caller before passing in).

        Returns
        -------
        VLAN | None
            Matching VLAN entry, or ``None`` if not configured on the device.
        """
        return next(
            (v for v in self.list_vlans(device, password) if v.vlan_id == vlan_id),
            None,
        )

    # ── Combined core-state read (VLAN + ports + SVI in fewer SSH sessions) ─

    def read_core_state(
        self, device: Device, password: str,
    ) -> tuple[list[VLAN], list[Puerto], list[SVI]]:
        """Return ``(vlans, ports, svis)`` reading all three from the
        device with as few SSH sessions as the vendor allows.

        Default implementation calls ``get_vlans``/``list_ports``/
        ``get_svis`` sequentially -- 3 separate SSH sessions. Concrete
        vendors override this to consolidate reads: ``CiscoVendor`` fuses
        the three into a single ``_leer()`` call (1 session);
        ``HuaweiVendor`` folds them into 2 sessions (VRP requires
        discovering Vlanif IDs before it can pull their config).

        Used by ``DeviceSyncService.sync_core()`` on the ``scope="all"``
        path -- reduces the SSH-session cost of a refresh from 3+ to 1-2
        without changing external contracts. The single-scope entry
        points (``sync_vlans``/``sync_ports``/``sync_svis``) keep calling
        the individual methods, so refreshing one scope at a time is
        unaffected.

        Raises ``RuntimeError`` on any read failure, same as the
        individual methods -- the caller decides whether to persist the
        successful parts.
        """
        vlans = self.get_vlans(device, password)
        ports = self.list_ports(device, password)
        svis = self.get_svis(device, password)
        return vlans, ports, svis

    # ── Port query operation (must be implemented by every driver) ───────────

    @abstractmethod
    def list_ports(self, device: Device, password: str) -> list[Puerto]:
        """Return the physical-port inventory of *device* as ``Puerto`` objects.

        Parameters
        ----------
        device:
            Domain device object exposing ``.name``, ``.host``, ``.username``.
        password:
            Plaintext device password (decrypted by the caller before
            passing in).

        Returns
        -------
        list[Puerto]
            Normalized port entries, sorted by interface name.  Empty list
            when the device reports no physical interfaces.

        Raises
        ------
        RuntimeError
            If the underlying playbook fails or returns output that cannot
            be parsed.
        """

    # ── Port mutation operations (default: NotImplementedError stub) ─────────

    def update_port_description(
        self,
        interface: str,
        description: str,
        device: Device,
        password: str,
    ) -> dict:
        """Set the description of *interface* on *device*.

        Concrete drivers should override this; the default implementation
        raises ``NotImplementedError`` so vendors that haven't been wired
        yet are explicit about the gap. The orchestration layer converts
        this into a clean ``UnsupportedVendorError`` for the API client.

        Parameters
        ----------
        interface:
            Vendor-native interface name (e.g. ``"GigabitEthernet1/0/1"``).
        description:
            New description text.  An empty string requests the driver to
            clear the description (``undo description`` on Huawei VRP,
            ``no description`` on Cisco IOS).
        device:
            Domain device object exposing ``.name``, ``.host``, ``.username``.
        password:
            Plaintext device password (decrypted by the caller).

        Returns
        -------
        dict
            Normalized Ansible result:
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement update_port_description yet"
        )

    def set_port_admin_state(
        self,
        interface: str,
        enabled: bool,
        device: Device,
        password: str,
    ) -> dict:
        """Administratively enable or disable *interface* on *device*.

        Concrete drivers should override this; the default implementation
        raises ``NotImplementedError`` so vendors that haven't been wired
        yet are explicit about the gap, and the API layer can present a
        clean ``VENDOR_NOT_SUPPORTED`` 501 instead of leaking the stub.

        Vendor mapping:
            * Huawei VRP — ``undo shutdown`` (enable) / ``shutdown`` (disable)
              inside the interface view, followed by ``commit``.
            * Cisco IOS  — ``no shutdown`` / ``shutdown`` inside
              ``interface <name>`` parent context.

        Parameters
        ----------
        interface:
            Vendor-native interface name (e.g. ``"GigabitEthernet1/0/1"``).
        enabled:
            ``True``  → bring the interface up (``undo shutdown`` /
            ``no shutdown``).
            ``False`` → bring the interface down (``shutdown``).
        device:
            Domain device object exposing ``.name``, ``.host``, ``.username``.
        password:
            Plaintext device password (decrypted by the caller).

        Returns
        -------
        dict
            Normalized Ansible result:
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement set_port_admin_state yet"
        )

    def set_port_access_vlan(
        self,
        interface: str,
        vlan_id: int,
        device: Device,
        password: str,
    ) -> dict:
        """Assign *vlan_id* as the access VLAN on *interface*.

        Concrete drivers should override this; the default raises
        ``NotImplementedError`` so the API layer returns a controlled 501.

        **Pre-conditions are the caller's responsibility:** the
        orchestration layer must verify the port is in access mode and
        the VLAN id is valid *before* invoking this method.  Drivers
        execute the change directly without checking mode — they trust
        the caller's pre-state validation.

        Vendor mapping:
            * Huawei VRP — ``port default vlan <id>`` inside the
              interface view, followed by ``commit``.
            * Cisco IOS  — ``switchport access vlan <id>`` inside
              ``interface <name>`` parent context.

        Parameters
        ----------
        interface:
            Vendor-native interface name (e.g. ``"GigabitEthernet1/0/1"``).
        vlan_id:
            New access VLAN identifier (1–4094, excluding 1002–1005).
        device:
            Domain device object exposing ``.name``, ``.host``, ``.username``.
        password:
            Plaintext device password (decrypted by the caller).

        Returns
        -------
        dict
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement set_port_access_vlan yet"
        )

    def set_trunk_pvid_vlan(
        self,
        interface: str,
        vlan_id: int,
        device: Device,
        password: str,
    ) -> dict:
        """Set the trunk native VLAN (PVID) of *interface* on *device*.

        Used when the port is in trunk mode to set the untagged/native VLAN.
        The orchestration layer guarantees the port is in trunk mode before
        invoking this method.

        Vendor mapping:
            * Huawei VRP — ``port trunk pvid vlan <id>`` inside the interface
              view, followed by ``commit``.
            * Cisco IOS  — ``switchport trunk native vlan <id>`` inside
              ``interface <name>`` parent context.

        Returns
        -------
        dict
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement set_trunk_pvid_vlan yet"
        )

    def set_trunk_allowed_vlans(
        self,
        interface: str,
        vlan_list: list[int],
        device: Device,
        password: str,
    ) -> dict:
        """Set the trunk allowed-VLAN list of *interface* to exactly *vlan_list*.

        The driver always performs a full replace (clear existing + set
        desired), not a delta.  The orchestration layer is responsible for
        computing the desired list from the requested mode (replace / add /
        remove) and the pre-state — the driver receives only the final list.

        **Pre-conditions are the caller's responsibility:** the orchestration
        layer must verify the port is in trunk mode and *vlan_list* is valid
        before invoking this method.

        Vendor mapping:
            * Huawei VRP — ``undo port trunk allow-pass vlan all`` followed by
              ``port trunk allow-pass vlan <list>`` inside the interface view,
              then ``commit``.
            * Cisco IOS  — ``switchport trunk allowed vlan <list>`` inside
              ``interface <name>`` parent context with ``save_when: always``.

        Parameters
        ----------
        interface:
            Vendor-native interface name.
        vlan_list:
            Sorted, deduplicated list of VLAN IDs to allow on the trunk.
            Must be non-empty and validated by the caller.
        device:
            Domain device object.
        password:
            Plaintext device password (decrypted by the caller).

        Returns
        -------
        dict
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement set_trunk_allowed_vlans yet"
        )

    def set_port_poe(
        self,
        interface: str,
        enabled: bool,
        device: Device,
        password: str,
    ) -> dict:
        """RF-PUERTO-09 — enable/disable Power-over-Ethernet on *interface*.

        Vendor mapping:
            * Cisco IOS  — ``power inline auto`` (enable) / ``power inline
              never`` (disable) inside ``interface <name>`` parent context.
            * Huawei VRP — ``poe enable`` / ``poe disable`` inside
              ``interface <name>`` context.

        Returns
        -------
        dict
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement set_port_poe yet"
        )

    def set_storm_control(
        self,
        interface: str,
        enabled: bool,
        threshold: "float | None",
        device: Device,
        password: str,
    ) -> dict:
        """RF-PUERTO-07 — enable/disable broadcast storm-control on
        *interface*, with a single percentage threshold (0-100). Simplified
        scope decided with the user: one enabled flag + one threshold, not
        the 3 traffic types (broadcast/multicast/unicast) real hardware
        actually exposes separately.

        *threshold* is required (non-``None``) when *enabled* is ``True`` —
        enforced by ``Puerto.validar()`` before this is ever called.

        Vendor mapping:
            * Cisco IOS  — ``storm-control broadcast level <threshold>`` to
              enable+set in one line, ``no storm-control broadcast level``
              to disable.
            * Huawei VRP — syntax varies by platform family, verify against
              the real device (``storm-control ?`` / ``storm suppression
              ?`` in interface view) before finalizing.

        Returns
        -------
        dict
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement set_storm_control yet"
        )

    def reset_port(
        self,
        interface: str,
        device: Device,
        password: str,
    ) -> dict:
        """RF-PUERTO-10 — reset *interface* to its factory-default
        configuration. Decided with the user: "delete port config" means
        reset to defaults, not a selective per-field undo.

        Vendor mapping:
            * Cisco IOS  — ``default interface <name>`` (global-level
              command, no ``interface`` parent context).
            * Huawei VRP — ``clear configuration interface <name>`` inside
              ``system-view``.

        Returns
        -------
        dict
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement reset_port yet"
        )

    def set_access_mode(
        self,
        interface: str,
        vlan_id: int,
        device: Device,
        password: str,
    ) -> dict:
        """Set *interface* to access mode with *vlan_id* as its access VLAN,
        atomically (mode + VLAN in the same device interaction).

        Replaces the old generic ``configure_port()`` composite call for
        this one well-defined operation — a real "switch to access mode"
        request always carries the target VLAN with it, there's no
        meaningful "just change mode, keep whatever VLAN was there" case.

        Concrete drivers should override this; the default raises
        ``NotImplementedError`` so vendors without an implementation surface
        a controlled ``VENDOR_NOT_SUPPORTED`` 501 rather than a bare
        exception.

        Parameters
        ----------
        interface:
            Vendor-native interface name (e.g. ``"GigabitEthernet1/0/1"``).
        vlan_id:
            Access VLAN to assign (1–4094, excluding 1002–1005).
        device:
            Domain device object exposing ``.name``, ``.host``, ``.username``.
        password:
            Plaintext device password (decrypted by the caller).

        Returns
        -------
        dict
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement set_access_mode yet"
        )

    def set_trunk_mode(
        self,
        interface: str,
        native_vlan: int,
        vlan_list: list[int],
        device: Device,
        password: str,
    ) -> dict:
        """Set *interface* to trunk mode with *native_vlan* (PVID) and
        *vlan_list* as its allowed VLANs, atomically (mode + both VLAN
        dimensions in the same device interaction).

        Replaces the old generic ``configure_port()`` composite call for
        this operation. Distinct from ``set_trunk_allowed_vlans()``/
        ``set_trunk_pvid_vlan()`` (which assume the port is *already*
        trunk and only change one dimension) — this one is a mode change,
        so both *native_vlan* and *vlan_list* always fully replace
        whatever the port had before, there is no add/remove semantics
        here (the port may be coming from access mode with no prior trunk
        config at all).

        Concrete drivers should override this; the default raises
        ``NotImplementedError`` so vendors without an implementation surface
        a controlled ``VENDOR_NOT_SUPPORTED`` 501 rather than a bare
        exception.

        Parameters
        ----------
        interface:
            Vendor-native interface name.
        native_vlan:
            Native VLAN / PVID for the trunk (1–4094).
        vlan_list:
            VLAN IDs to allow on the trunk (non-empty, validated by the
            caller).
        device:
            Domain device object.
        password:
            Plaintext device password (decrypted by the caller).

        Returns
        -------
        dict
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement set_trunk_mode yet"
        )

    # ── Virtual interface (SVI) operations, RF-INTERV-* ───────────────────────
    # interface Vlan{id} en Cisco, interface Vlanif{id} en Huawei. La
    # identidad de la interfaz ES el vlan_id -- no hay "reasignar a otra
    # VLAN" (RF-INTERV-9), eso es borrar y crear de nuevo.

    def create_svi(self, vlan_id: int, device: Device, password: str) -> dict:
        """Create the SVI for *vlan_id* (assumes the VLAN itself already
        exists -- validated by the caller before this runs)."""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement create_svi yet"
        )

    def delete_svi(self, vlan_id: int, device: Device, password: str) -> dict:
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement delete_svi yet"
        )

    def set_svi_admin_state(self, vlan_id: int, enabled: bool, device: Device, password: str) -> dict:
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement set_svi_admin_state yet"
        )

    def set_svi_description(self, vlan_id: int, description: str, device: Device, password: str) -> dict:
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement set_svi_description yet"
        )

    def set_svi_ipv4(self, vlan_id: int, ipv4_address: "str | None", device: Device, password: str) -> dict:
        """*ipv4_address* is CIDR (``"10.10.10.11/24"``) or ``""``/``None``
        to clear. CIDR→dotted-mask conversion (needed by Cisco) is real
        computation, done by the concrete driver -- not sintaxis, doesn't
        belong in the YAML template."""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement set_svi_ipv4 yet"
        )

    def set_svi_ipv4_secondary(
        self, vlan_id: int, ipv4_address: "str | None", previous_ipv4_address: "str | None",
        device: Device, password: str,
    ) -> dict:
        """Same contract as ``set_svi_ipv4()`` but for the secondary
        IPv4 address (RF-INTERV-03) -- caller (``SVI``) already
        checked a primary exists before calling this.

        Unlike the primary address, clearing a secondary needs to name the
        exact address being removed (``no ip address <addr> <mask>
        secondary`` on IOS -- a bare ``no ip address`` wipes the primary
        *and* every secondary). *previous_ipv4_address* is the currently
        configured secondary (CIDR), passed by the caller from its
        pre-state read -- only present when *ipv4_address* is falsy
        (clearing); the caller guarantees it's non-None whenever a clear
        actually reaches the driver (a clear with nothing configured is a
        no-op handled before this is called)."""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement set_svi_ipv4_secondary yet"
        )

    def set_svi_ipv6(self, vlan_id: int, ipv6_address: "str | None", device: Device, password: str) -> dict:
        """*ipv6_address* is CIDR (``"2001:db8::1/64"``) or ``""``/``None``
        to clear. Precondition (not managed by this call): Cisco needs
        ``ipv6 unicast-routing`` enabled globally; Huawei needs ``ipv6
        enable`` on the interface first."""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement set_svi_ipv6 yet"
        )

    def set_svi_acl(
        self, vlan_id: int, direction: str, acl_name: "str | None", device: Device, password: str,
        *, current_acl_name: "str | None" = None,
    ) -> dict:
        """Bind (or clear, if *acl_name* is ``""``/``None``) an ACL that
        already exists on the device to *direction* (``"in"``/``"out"``).
        Does not create the ACL itself -- that's RF-GLOBAL-04, out of scope
        here.

        *current_acl_name* is the ACL currently bound in that direction
        (from ``reconciliar()``'s ``actual``), passed only for a clear.
        Cisco's ``no ip access-group {direction}`` doesn't need it (only 1
        ACL per direction can ever be bound, so the direction alone is
        unambiguous) but VRP's ``undo traffic-filter`` does -- confirmed
        live against f3r9s2 that ``undo traffic-filter inbound`` alone is
        rejected as "Incomplete command", it must repeat the exact ACL
        reference that was bound."""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement set_svi_acl yet"
        )

    def list_acl_names(self, device: Device, password: str) -> list[str]:
        """Names/numbers of every ACL configured on the device -- used by
        the API layer (RF-INTERV-04's "ACL previamente creada... e
        identificador válido" precondition) to reject binding an ACL that
        doesn't exist, before enqueueing the job. Minimal read -- see
        ``list_acls()`` for the full RF-GLOBAL-04 version (each ACL's
        rules too, used by ``get_global_config()``)."""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement list_acl_names yet"
        )

    def list_acls(self, device: Device, password: str) -> list[dict]:
        """Like ``list_acl_names()`` but with each ACL's raw rules too
        (RF-GLOBAL-01/04 -- "el contenido de cada una", requested after
        seeing ``get_global_config()``'s ``acls`` field with just names).
        Shape: ``[{"name": str, "type": str, "rules": list[str]}, ...]``,
        rules kept as raw lines (not further parsed into source/dest/
        protocol) -- same command as ``list_acl_names()``, no extra
        device read."""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement list_acls yet"
        )

    # ── RF-GLOBAL-05 (ACLs -- crear/agregar reglas/borrar) ────────────────────
    # Solo ACLs extended (Cisco) / advanced (Huawei) -- las standard/basic ya
    # existentes (ej. acceso-vty/acceso-snmp) siguen siendo de solo lectura.

    def formatear_regla_acl(self, rule: dict) -> str:
        """Traduce 1 regla vendor-agnóstica (``GlobalConfigAclRule``, dict
        con action/protocol/source/destination/port) a la línea CLI real
        de este vendor -- SIN ejecutar nada (pura, sin I/O). La usa
        ``GlobalConfig._aplicar_acl_create()``/``_aplicar_acl_rule_remove()``
        para no-op detection (comparar contra las reglas ya leídas en
        ``actual.acls``) antes de mandarle nada al driver de escritura."""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement formatear_regla_acl yet"
        )

    def create_or_update_acl(self, name: str, rule_lines: list[str], device: Device, password: str) -> dict:
        """Crea la ACL *name* si no existe, agrega *rule_lines* (ya
        formateadas por ``formatear_regla_acl()``) si ya existe -- mismo
        comando sirve para ambos casos en Cisco/Huawei (entrar al contexto
        de la ACL la crea si no estaba)."""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement create_or_update_acl yet"
        )

    def remove_acl_rules(self, name: str, rule_lines: list[str], device: Device, password: str) -> dict:
        """Saca *rule_lines* (ya formateadas) de la ACL *name*."""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement remove_acl_rules yet"
        )

    def delete_acl(self, name: str, device: Device, password: str) -> dict:
        """Borra la ACL *name* completa."""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement delete_acl yet"
        )

    def set_svi_dhcp_relay(
        self, vlan_id: int, servers: list[str], device: Device, password: str,
    ) -> dict:
        """Set the DHCP relay/helper server list to exactly *servers*
        (empty list clears it) -- full replace, same convention as
        ``set_trunk_allowed_vlans``."""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement set_svi_dhcp_relay yet"
        )

    def get_svis(self, device: Device, password: str) -> list[SVI]:
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement get_svis yet"
        )

    def get_global_config(self, device: Device, password: str) -> "GlobalConfig":
        """ARP/MAC NO viven acá -- tienen su propio método
        (``get_arp_table()``/``get_mac_table()``) y su propio scope de
        sync (``DeviceSyncService.sync_arp_mac()``), a pedido del usuario:
        no hacen falta para ninguna escritura y pueden traer muchísima
        info, así que su sync es específico en vez de venir pegado acá
        (confirmado en vivo que traerlos acá hacía que devices con pocas
        líneas VTY, ej. huawei01 con 5, se quedaran sin sesiones para
        escribir -- "Channel closed")."""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement get_global_config yet"
        )

    def set_hostname(self, hostname: str, device: Device, password: str) -> dict:
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement set_hostname yet"
        )

    def set_snmp(self, cambios: dict, device: Device, password: str) -> dict:
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement set_snmp yet"
        )

    def get_arp_table(self, device: Device, password: str) -> "list[dict]":
        """Tabla completa, sin filtrar -- se lee durante ``get_global_config()``
        y se cachea; ``include`` ahora se aplica del lado de la API sobre
        los datos ya cacheados (ver ``api/global_config.py``), no acá."""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement get_arp_table yet"
        )

    def get_mac_table(self, device: Device, password: str) -> "list[dict]":
        """Como ``get_arp_table()`` -- tabla completa, sin filtrar."""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement get_mac_table yet"
        )

    def get_log_buffer(self, device: Device, password: str) -> str:
        """Log buffer local del device, texto crudo (sin parsear a
        entradas -- mismo criterio que ``running_config``, el formato de
        cada línea varía demasiado para forzar una estructura). Fuera de
        RF-GLOBAL-01..09, pedido del usuario "de la misma forma que las
        tablas mac y arp" -- mismo scope de sync propio (``"logs"``, ver
        ``DeviceSyncService.sync_logs()``), afuera de ``reconciliar()`` y
        de ``"all"``."""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement get_log_buffer yet"
        )

    def add_log_server(self, server: str, level: "str | None", device: Device, password: str) -> dict:
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement add_log_server yet"
        )

    def remove_log_server(self, server: str, device: Device, password: str) -> dict:
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement remove_log_server yet"
        )

    def set_route(self, destination: str, next_hop: str, device: Device, password: str) -> dict:
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement set_route yet"
        )

    def remove_route(self, destination: str, next_hop: str, device: Device, password: str) -> dict:
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement remove_route yet"
        )

    def add_ntp_server(self, server: str, prefer: bool, device: Device, password: str) -> dict:
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement add_ntp_server yet"
        )

    def remove_ntp_server(self, server: str, device: Device, password: str) -> dict:
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement remove_ntp_server yet"
        )

    def add_dns_server(self, server: str, device: Device, password: str) -> dict:
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement add_dns_server yet"
        )

    def remove_dns_server(self, server: str, device: Device, password: str) -> dict:
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement remove_dns_server yet"
        )

    def set_dns_domain(self, domain: str, device: Device, password: str) -> dict:
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement set_dns_domain yet"
        )

    @staticmethod
    def _red_y_mascara(cidr: str) -> tuple[str, str]:
        """``"192.168.99.5/24"`` → ``("192.168.99.0", "255.255.255.0")`` --
        a diferencia de ``CiscoVendor._cidr_a_direccion_y_mascara()`` (que
        preserva la dirección de host, correcto para ``ip address`` de una
        interfaz), acá se necesita la dirección DE RED (``ip route``/``ip
        route-static`` piden red+máscara, no un host dentro de la red) --
        ``strict=False`` para no rechazar un CIDR con bits de host
        prendidos, se los pisa. Compartido por ``set_route`` de ambos
        vendors (los 2 confirmados en vivo que piden máscara punteada, no
        largo de prefijo)."""
        red = ipaddress.ip_network(cidr, strict=False)
        return str(red.network_address), str(red.netmask)

    @staticmethod
    def _red_y_wildcard(cidr: str) -> tuple[str, str]:
        """``"172.28.138.0/24"`` → ``("172.28.138.0", "0.0.0.255")`` -- la
        wildcard mask que usan las ACLs (Cisco confirmado en vivo; Huawei
        Advanced ACL usa el mismo concepto, sintaxis exacta sin confirmar
        todavía, ver RF-GLOBAL-05). Es el INVERSO de la netmask normal que
        devuelve ``_red_y_mascara()`` -- ``ipaddress`` ya expone eso
        directo vía ``.hostmask``, no hace falta invertir a mano."""
        red = ipaddress.ip_network(cidr, strict=False)
        return str(red.network_address), str(red.hostmask)

    @staticmethod
    def _combinar_resultados(resultados: list[dict]) -> dict:
        """``set_snmp``/``add_log_server`` pueden disparar más de 1
        template call (1 por sub-campo presente en ``cambios``) -- combina
        esos resultados individuales en 1 solo dict ``{rc, stdout, stderr,
        success}``, mismo shape que devuelve ``_aplicar()``."""
        if not resultados:
            return {"rc": 0, "stdout": "", "stderr": "", "success": True, "changed": False}
        return {
            "rc": max(r.get("rc", 0) for r in resultados),
            "stdout": "\n".join(r.get("stdout", "") for r in resultados),
            "stderr": "\n".join(r.get("stderr", "") for r in resultados if r.get("stderr")),
            "success": all(r.get("success") for r in resultados),
        }
