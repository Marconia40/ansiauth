from dataclasses import dataclass


@dataclass
class RetryDecision:
    should_retry: bool
    classification: str  # "transient" | "permanent" | "unknown"
    reason: str
    # Tope opcional al total de intentos para esta clasificación específica --
    # sirve para dar 1 sola chance extra a errores "unknown" (que antes se
    # trataban como permanentes) sin gastarles los 3-4 intentos completos
    # de un transitorio clásico. None => usar el max_retries global del job.
    max_attempts_override: "int | None" = None
    # Etiqueta corta de QUÉ tipo de problema es (no solo si conviene
    # reintentar) -- "auth"/"syntax"/"conflict"/"connectivity"/
    # "session_limit"/"read_reliability", o None cuando la clasificación
    # vino del chequeo de rc de Ansible (_RC_TRANSITORIOS) o del catch-all
    # "unknown" (ningún patrón de texto matcheó). Usado por
    # Orquestador._resumir_error() para armar Job.error_summary -- separado
    # de `reason` (que sigue siendo el patrón/código crudo que matcheó,
    # útil para debug) porque `categoria` es el eje que importa para
    # traducir a una frase legible.
    categoria: "str | None" = None
