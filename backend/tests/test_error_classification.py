"""Clasificación de errores de device + traducción a una frase legible
(``Job.error_summary``) -- plan de esta sesión ("Clasificar y explicar
mejor los errores de device"). ``Orquestador._clasificar_error()`` ya
existía (decide reintentar-tal-cual vs. abandonar); esto solo agrega una
`categoria` a los patrones que ya matcheaba y una función nueva,
``_resumir_error()``, que la traduce a texto. Un caso representativo por
categoría, más los 2 casos sin categoria (rc-based transient y el
catch-all "unknown") -- no busca cobertura exhaustiva de cada patrón en
las tablas, esas ya estaban probadas en producción por el mecanismo de
retry que las usa hace rato.

``Orquestador._clasificar_error()`` no toca ``self`` -- se puede instanciar
con dependencias dummy solo para llamar este método.
"""
from app.services.orquestador import Orquestador, _resumir_error


def _clasificar(resultado: dict):
    orq = Orquestador(None, None, None, None, None)
    return orq._clasificar_error(resultado)


def test_auth_error_no_retry_and_summary():
    decision = _clasificar({"stderr": "% Authentication failure", "stdout": ""})
    assert decision.should_retry is False
    assert decision.classification == "permanent"
    assert decision.categoria == "auth"
    assert "credentials" in _resumir_error(decision)


def test_syntax_error_no_retry_and_summary():
    decision = _clasificar({"stderr": "% Invalid input detected at '^' marker.", "stdout": ""})
    assert decision.should_retry is False
    assert decision.categoria == "syntax"
    assert "rejected" in _resumir_error(decision)


def test_conflict_error_no_retry_and_summary():
    decision = _clasificar({"stderr": "", "stdout": "VLAN already exists on this device"})
    assert decision.should_retry is False
    assert decision.categoria == "conflict"
    assert "conflicts" in _resumir_error(decision)


def test_connectivity_error_retries_and_summary():
    decision = _clasificar({"stderr": "Connection reset by peer", "stdout": ""})
    assert decision.should_retry is True
    assert decision.classification == "transient"
    assert decision.categoria == "connectivity"
    assert "connectivity" in _resumir_error(decision)


def test_session_limit_error_retries_and_summary():
    decision = _clasificar({"stderr": "Error: too many sessions", "stdout": ""})
    assert decision.should_retry is True
    assert decision.categoria == "session_limit"
    assert "SSH sessions" in _resumir_error(decision)


def test_read_reliability_error_retries_and_summary():
    decision = _clasificar({"stderr": "possible read desync", "stdout": ""})
    assert decision.should_retry is True
    assert decision.categoria == "read_reliability"
    assert "read reliably" in _resumir_error(decision)


def test_ansible_rc_transient_has_no_categoria_but_gets_generic_summary():
    decision = _clasificar({"rc": 4, "stderr": "", "stdout": ""})
    assert decision.should_retry is True
    assert decision.classification == "transient"
    assert decision.categoria is None
    assert _resumir_error(decision) == "A transient connectivity issue occurred and retries were exhausted."


def test_unknown_error_gets_generic_summary():
    decision = _clasificar({"stderr": "some totally unrecognized gibberish", "stdout": ""})
    assert decision.should_retry is True
    assert decision.classification == "unknown"
    assert decision.categoria is None
    assert _resumir_error(decision) == (
        "The device gave an unfamiliar response — could not determine the exact cause."
    )
