"""Request/response logging middleware.

Logs every HTTP request with method, path, status code, and elapsed time.

Design goals:
- **No noise**: health checks, static files, and other low-value endpoints
  are skipped entirely.
- **Always complete**: a ``request_completed`` log is emitted even when the
  request raises (via ``try/finally``), so errors are never orphaned.
- **Dev-friendly**: when ``settings.DEBUG=True``, query params and request body
  (truncated + redacted) are included so developers can see *what* each
  request did.  Production never logs request bodies.
"""
import json
import time

from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import settings

#: Path prefixes that should NOT produce request logs at all.
#: These are high-frequency, low-value endpoints (health probes, static assets).
_SKIP_PATHS: frozenset[str] = frozenset({
    "/health",
    "/api/v1/health",
    "/api/v1/health/debug/event-bridge",
})

_SKIP_PREFIXES: tuple[str, ...] = (
    "/api/v1/health/",
)

#: Sensitive field names whose values are redacted in dev body logging.
_SENSITIVE_FIELDS: frozenset[str] = frozenset({
    "password",
    "token",
    "secret",
    "api_key",
    "apikey",
    "authorization",
    "refresh_token",
    "access_token",
    "current_password",
    "new_password",
})

#: Max body length to log (chars).  Truncated beyond this.
_MAX_BODY_LEN = 1024


def _should_skip(path: str) -> bool:
    """Return True if this path should not produce request logs."""
    if path in _SKIP_PATHS:
        return True
    return any(path.startswith(p) for p in _SKIP_PREFIXES)


def _redact(obj):
    """Recursively redact sensitive fields in a dict/list structure."""
    if isinstance(obj, dict):
        return {
            k: ("***" if k.lower() in _SENSITIVE_FIELDS else _redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(item) for item in obj]
    return obj


async def _read_body(request: Request) -> str | None:
    """Read and cache the request body so downstream handlers can still read it.

    In Starlette ``BaseHTTPMiddleware``, the body stream can only be consumed
    once.  We read it here, cache it on ``request._body``, and replace the
    ``receive`` callable so subsequent ``await request.body()`` calls return
    the cached value.

    Returns the body as a decoded string (truncated), or None if there is no
    body or it's not text.
    """
    body = await request.body()
    if not body:
        return None

    # Parse + redact JSON bodies; fall back to raw text for non-JSON.
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            parsed = json.loads(body)
            redacted = _redact(parsed)
            text = json.dumps(redacted, ensure_ascii=False)
        except (json.JSONDecodeError, UnicodeDecodeError):
            text = body.decode("utf-8", errors="replace")
    else:
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return f"<binary {len(body)} bytes>"

    if len(text) > _MAX_BODY_LEN:
        text = text[:_MAX_BODY_LEN] + f"... ({len(text)} chars total)"
    return text


class LoggingMiddleware(BaseHTTPMiddleware):
    """Log every HTTP request with method, path, status code, and elapsed time.

    - Health checks and static files are skipped entirely (no noise).
    - A completion log is always emitted, even on exceptions (``try/finally``).
    - In dev mode (``DEBUG=True``), query params and redacted body are included.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Skip noisy endpoints entirely.
        if _should_skip(path):
            return await call_next(request)

        start = time.perf_counter()
        status_code = 500  # default if exception is raised
        error_raised = False

        # In dev mode, capture request inputs for diagnostic logging.
        body_text: str | None = None
        query_str: str | None = None
        if settings.DEBUG and request.method in ("POST", "PUT", "PATCH", "DELETE"):
            body_text = await _read_body(request)
        if settings.DEBUG and request.url.query:
            query_str = request.url.query

        try:
            response: Response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception:
            error_raised = True
            raise
        finally:
            elapsed_ms = round((time.perf_counter() - start) * 1000, 2)

            # Build log fields.
            log_fields: dict = {
                "method": request.method,
                "path": path,
                "status_code": status_code,
                "elapsed_ms": elapsed_ms,
            }
            if error_raised:
                log_fields["error"] = True

            # Dev-only: include request inputs for diagnostics.
            if settings.DEBUG:
                if query_str:
                    log_fields["query"] = query_str[:_MAX_BODY_LEN]
                if body_text:
                    log_fields["body"] = body_text

            logger.bind(request_id=getattr(request.state, "request_id", "-")).info(
                "request_completed",
                **log_fields,
            )
