from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.vendors.base import VendorDriver


class PluginRegistry:
    """FINAL_ARCHITECTURE.md §1.6 — un diccionario con superpoderes: dado el
    vendor de un device, devuelve el VendorDriver que sabe hablarle. Reemplaza
    el if/elif de vendors/dispatcher.py (borrado en esta misma fase, ver abajo)."""

    def __init__(self):
        self._vendors: dict[str, "VendorDriver"] = {}

    def registrar(self, vendor: str, driver: "VendorDriver") -> None:
        self._vendors[vendor] = driver

    def obtener(self, vendor: str) -> "VendorDriver":
        if vendor not in self._vendors:
            raise ValueError(f"No hay driver registrado para vendor='{vendor}'")
        return self._vendors[vendor]
