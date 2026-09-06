from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DeviceLogs:
    """Domain representation del log buffer local de un device -- fuera
    de RF-GLOBAL-01..09, pedido del usuario ("de la misma forma que las
    tablas mac y arp"). Singleton por device, igual que ``ArpMacTables``
    (ver esa docstring para la justificación completa del patrón:
    tabla/repository/scope de sync propios, no comparte fila con
    ``GlobalConfig``) -- sin ``RecursoGestionable``, es puramente de
    lectura, nunca se escribe vía job.

    ``log_output`` queda como texto crudo (no lista de entradas
    parseadas) -- mismo criterio que ``running_config``: el formato de
    cada línea de log varía mucho entre vendors/eventos, no hay una
    estructura tabular limpia que valga la pena forzar (a diferencia de
    ARP/MAC, que sí tienen columnas bien definidas)."""

    device: str = ""
    log_output: str | None = None
