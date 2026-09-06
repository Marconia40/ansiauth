from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ArpMacTables:
    """Domain representation de las tablas ARP/MAC de un device -- fuera
    de RF-GLOBAL-01..09, pedido del usuario. Singleton por device (1 fila
    por device, PK simple), igual que ``GlobalConfig``, pero sin
    ``RecursoGestionable`` (``validar``/``reconciliar``/``aplicar``) --
    es puramente de lectura, nunca se escribe vía job, solo se puebla por
    ``DeviceSyncService.sync_arp_mac()``. Separada de ``GlobalConfig`` en
    tabla/repository propios porque tiene su propio scope de sync
    (``"arp_mac"``, no forma parte de ``"all"`` ni de ``reconciliar()``
    de ninguna escritura) -- ver docstring de ``DeviceArpMacModel``."""

    device: str = ""
    arp_table: list[dict] | None = None
    mac_table: list[dict] | None = None
