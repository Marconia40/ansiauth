from typing import Any, Protocol


class RecursoGestionable(Protocol):
    def validar(self) -> None: ...
    def reconciliar(self, device: "Device") -> dict: ...
    def aplicar(self, device: "Device", pre_state: "dict | None" = None) -> dict: ...
    def repositorio(self) -> str: ...

    def resolver_paso(self, device: "Device", actual: "Any | None") -> "tuple[str, str | None, dict] | None":
        """Devuelve ``(op_key, variant, vars)`` -- lo mismo que ``aplicar()``
        ya calcula hoy antes de llamar a un método con nombre propio del
        driver -- o ``None`` si es no-op contra *actual*. NO toca el
        device (no abre conexión). ``op_key`` es el mismo nombre de
        entrada de ``commands.yaml`` / método del driver que ``aplicar()``
        usaría para este mismo recurso.

        Usado por ``Orquestador.ejecutar_lote()`` para juntar N recursos
        (mismo device, mismo tipo) en 1 sola conexión vía
        ``VendorDriver.aplicar_lote()`` -- ver esa docstring. ``aplicar()``
        sigue siendo el camino de 1 recurso = 1 conexión de siempre, sin
        cambios de comportamiento; ambos reusan la misma lógica de
        no-op/dispatch por debajo, no hay 2 copias."""
        ...

    # ``ajustar_estados_lote(recursos, estados) -> estados`` es OPCIONAL,
    # no forma parte de este Protocol -- ``Orquestador.ejecutar_lote()`` lo
    # busca vía ``getattr(tipo, "ajustar_estados_lote", None)`` antes de
    # llamar ``resolver_paso()`` en cada recurso. Solo lo implementa ``SVI``
    # hoy (corrige el "actual" de una entrada con lo que OTRA entrada del
    # MISMO lote está por fijar, para validaciones cross-campo dentro del
    # mismo batch -- ver ``SVI.ajustar_estados_lote()``). ``Puerto`` no lo
    # necesita: ningún campo suyo depende del valor nuevo de otro campo del
    # mismo lote.

    # ``resolver_rollback(pre_state, device, actual_ahora) -> (pasos, verificar)``
    # también es OPCIONAL, mismo mecanismo que ``ajustar_estados_lote`` --
    # ``Orquestador._rollback_lote()`` lo busca vía
    # ``getattr(tipo, "resolver_rollback", None)`` en vez de un
    # ``if tipo == "puerto"``/``elif tipo == "svi"`` hardcodeado. Devuelve
    # los pasos de reversión (misma forma que ``resolver_paso()``, sin
    # tocar el device) + una closure de verificación opcional, para que
    # ``_rollback_lote()`` pueda batchear N reversiones en 1 sola
    # ``aplicar_lote()`` en vez de 1 conexión por recurso. Hoy lo
    # implementan ``Puerto``/``SVI`` (los únicos tipos con endpoint de
    # lote) -- ``VLAN``/``ConfiguracionGlobal`` no lo necesitan mientras
    # no batcheen, y no romper nada si algún día se agregan: sin este
    # método, ``_rollback_lote()`` los trata como no-op.
    #
    # ``NECESITA_ESTADO_FRESCO_ROLLBACK: bool`` (atributo de clase,
    # también opcional) -- ``True`` cuando el recurso necesita releer el
    # estado ACTUAL del device (post-apply-fallido) antes de poder armar
    # su plan de rollback (``SVI``: ``ipv4_address_secondary``/``acl_in``/
    # ``acl_out``). Sin esto, ``_rollback_lote()`` tendría que preguntar
    # de nuevo "sos una SVI?" para decidir si hace la lectura extra.
