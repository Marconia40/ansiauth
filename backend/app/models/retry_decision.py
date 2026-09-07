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
