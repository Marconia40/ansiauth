class ValidationError(Exception):
    pass


class DeviceExecutionError(Exception):
    pass


class NotFoundError(Exception):
    pass


class ConflictError(Exception):
    pass


class TransicionInvalidaError(Exception):
    """Transición de estado de Job rechazada por _TRANSICIONES_VALIDAS
    (models/job.py) — mapea a 409, mismo criterio que ConflictError. El
    handler HTTP se cablea en Fase 5 (api/jobs.py: cancel_job() la atrapa
    y traduce al mismo 409 que ya devuelve hoy)."""
    pass


class SiteHasDevicesError(Exception):
    """Raised when attempting to delete a Site that still owns devices —
    Fase 6, A3. Movida desde site_service.py (misma clase, mismo mensaje)
    para que ``api/sites.py`` no dependa de un módulo que esta fase deja
    sin caller real; mismo criterio que ``TransicionInvalidaError``."""

    def __init__(self, site_id: int, device_count: int):
        super().__init__(
            f"Site {site_id} still contains {device_count} device(s) — reassign them before deletion"
        )
        self.site_id = site_id
        self.device_count = device_count


class DefaultGroupImmutableError(ValueError):
    """Raised by rename/delete when the target group is a Site's Default
    (D7) — Fase 6, A2. Movida desde device_group_service.py, mismo criterio
    que ``SiteHasDevicesError`` arriba."""


class UnsupportedVendorError(Exception):
    """Raised when an operation is invoked against a device whose vendor has
    no driver registered for that operation.

    Distinct from ``NotFoundError`` (the device exists, we just can't speak
    its vendor's dialect for this feature) and from ``ValidationError``
    (the request was well-formed; the platform itself is the gap).

    Carries the raw vendor / platform strings in ``vendor`` and ``platform``
    attributes so internal logs can stay diagnostic, while the API layer
    surfaces only an operator-friendly message to the client.
    """

    def __init__(self, vendor: str, platform: str, operation: str = "port management"):
        self.vendor = vendor
        self.platform = platform
        self.operation = operation
        super().__init__(
            f"{operation} is not supported for vendor='{vendor}' platform='{platform}'"
        )
