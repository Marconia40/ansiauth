from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse


class HSTSMiddleware(BaseHTTPMiddleware):
    """Add Strict-Transport-Security header to every response."""

    def __init__(self, app, max_age: int = 31536000):
        super().__init__(app)
        self._header_value = f"max-age={max_age}; includeSubDomains"

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["Strict-Transport-Security"] = self._header_value
        return response


class HTTPSRedirectMiddleware(BaseHTTPMiddleware):
    """Redirect plain-HTTP requests to HTTPS.

    Works in two scenarios:
    - Direct Uvicorn with TLS: Uvicorn sets request.url.scheme to 'https', so
      only requests that somehow arrive as plain HTTP are redirected.
    - Behind a reverse proxy (nginx/Caddy): proxy sets X-Forwarded-Proto; this
      middleware redirects when that header is 'http'.
    """

    async def dispatch(self, request: Request, call_next):
        forwarded_proto = request.headers.get("X-Forwarded-Proto")
        scheme = forwarded_proto if forwarded_proto else request.url.scheme
        if scheme == "http":
            https_url = str(request.url).replace("http://", "https://", 1)
            return RedirectResponse(https_url, status_code=301)
        return await call_next(request)
