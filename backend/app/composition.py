"""Raíz de composición — arma el grafo de dependencias del proceso una sola
vez al arrancar. FINAL_ARCHITECTURE.md §1.6.

No existía ningún lugar central antes de esta fase — main.py conectaba
FastAPI directo contra cada servicio. Este archivo va a ir creciendo en
fases futuras (Repository[X] concretos, Orquestador, GroupOperationRunner,
etc.) — no hace falta anticipar esas partes acá, cada fase agrega lo suyo.
"""

from __future__ import annotations

from app.services.plugin_registry import PluginRegistry


def build_plugin_registry() -> PluginRegistry:
    from app.core.config import EXECUTION_MODE

    registry = PluginRegistry()
    if EXECUTION_MODE == "mock":
        from app.services.vendors.mock import MockVendor
        registry.registrar("huawei_vrp", MockVendor())
        registry.registrar("cisco_ios", MockVendor())
    else:
        from app.services.vendors.huawei.driver import HuaweiVendor
        from app.services.vendors.cisco.driver import CiscoVendor
        registry.registrar("huawei_vrp", HuaweiVendor())
        registry.registrar("cisco_ios", CiscoVendor())
    return registry


plugin_registry = build_plugin_registry()

# TODO: secret_vault, redis_coordinator — Línea B (FASE_1.md B1/B2)
