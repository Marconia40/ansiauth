"""Bug real encontrado en un job contra f3r9s1 (job #165, confirmado leyendo
la tabla ``jobs`` de la base directo): ``set_storm_control``'s primary manda
``storm-control broadcast include multicast`` incondicionalmente, pero ese
IOS real la rechaza (``% Invalid input detected at '^' marker``). Ansible
manda TODO el bloque antes de revisar errores, así que las líneas
siguientes (``storm-control broadcast level``/``storm-control action``) se
aplicaron igual pese al rechazo -- el job terminó failed con el device en
un estado parcial.

Misma técnica ya usada para ~10 operaciones Huawei (``alternatives``/
``triggered_by_error``, motor genérico en
``VendorDriver._aplicar_desde_template()``): el primary se deja igual (no
hay evidencia de que ``include multicast`` esté mal en TODOS los Cisco,
sólo confirmado que falla acá) y se agrega una alternativa que manda el
multicast como línea separada (sintaxis IOS estándar,
``storm-control multicast level``, independiente de ``broadcast``).
"""
from __future__ import annotations

import re

from app.services.vendors.cisco.driver import CiscoVendor


def _comandos():
    return CiscoVendor()._cargar_comandos()["set_storm_control"]


def test_primary_incluye_include_multicast():
    comandos = _comandos()
    assert "storm-control broadcast include multicast" in comandos["enabled"]["primary"]["lines"]


def test_alternativa_enabled_manda_multicast_por_separado():
    comandos = _comandos()
    alt = comandos["enabled"]["alternatives"][0]
    assert "storm-control broadcast include multicast" not in alt["lines"]
    assert "storm-control multicast level {threshold}" in alt["lines"]
    # El resto del bloque (broadcast level + repeat de action_lines) es
    # idéntico al primary, solo cambia la línea de multicast.
    assert "storm-control broadcast level {threshold}" in alt["lines"]
    assert alt["repeat"] == comandos["enabled"]["primary"]["repeat"]


def test_alternativa_disabled_manda_multicast_por_separado():
    comandos = _comandos()
    assert "no storm-control broadcast include multicast" in comandos["disabled"]["primary"]["lines"]
    alt = comandos["disabled"]["alternatives"][0]
    assert "no storm-control broadcast include multicast" not in alt["lines"]
    assert "no storm-control multicast level" in alt["lines"]
    assert "no storm-control broadcast level" in alt["lines"]


def test_regex_alternativa_matchea_el_error_real_observado():
    """Texto real capturado en vivo contra f3r9s1 (pegado por el usuario
    del job que falló)."""
    comandos = _comandos()
    alt = comandos["enabled"]["alternatives"][0]
    error_real = (
        "f3r9s1(config)#interface Gi1/0/1\r\n"
        "f3r9s1(config-if)#storm-control broadcast include multicast\r\n"
        "                                          ^\r\n"
        "% Invalid input detected at '^' marker.\r\n\r\n"
        "f3r9s1(config-if)#storm-control broadcast level 10.0\r\n"
        "f3r9s1(config-if)#storm-control action trap\r\n"
        "f3r9s1(config-if)#end\r\n"
        "f3r9s1#quit"
    )
    assert re.search(alt["triggered_by_error"], error_real)


def test_regex_alternativa_no_matchea_otro_error_no_relacionado():
    """El trigger es específico a la línea rechazada -- un error distinto
    en el mismo bloque (ej. threshold fuera de rango) no debe disparar
    este fallback silenciosamente."""
    comandos = _comandos()
    alt = comandos["enabled"]["alternatives"][0]
    error_no_relacionado = (
        "f3r9s1(config-if)#storm-control broadcast level 999\r\n"
        "                                                 ^\r\n"
        "% Invalid input detected at '^' marker.\r\n"
    )
    assert not re.search(alt["triggered_by_error"], error_no_relacionado)
