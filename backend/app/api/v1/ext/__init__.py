"""External API route group — API Key authenticated + rate-limited endpoints."""
import time

from fastapi import APIRouter, Depends, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.status import HTTP_429_TOO_MANY_REQUESTS

from app.core.auth_apikey import ApiKeyPrincipal, get_api_key_principal
from app.core.rate_limiter import check_rate_limit
from app.core.user_auth_state import is_introspect_stale
from app.services.api_key_stats_service import record_request
from app.services.ext_api_call_log_service import (
    ExtCallContext,
    get_ext_call_context,
    set_ext_call_context,
)

router = APIRouter(
    prefix="/ext",
    tags=["external-api"],
)


def resolve_user_id(
    principal: ApiKeyPrincipal,
    visitor_id: str | None,
) -> str:
    """Resolve the stable user_id for session/audit attribution.

    - Callback-verification mode (api_key.user_info_url non-empty):
      principal.user_id is already set to ``f"{owner}:{sub}"`` by the
      auth dependency. visitor_id is ignored.
    - Legacy mode (user_info_url empty): compose from visitor_id;
      falls back to owner_user_id when visitor_id is absent.
    """
    if principal.user_id:
        return principal.user_id
    if visitor_id:
        return f"{principal.owner_user_id}:{visitor_id}"
    return principal.owner_user_id


def _split_user_sub(principal: ApiKeyPrincipal) -> str:
    """Extract the sub portion from principal.user_id (callback mode only).

    principal.user_id is ``f"{owner}:{sub}"`` in callback mode; we strip
    the owner prefix to leave just the partner-side sub. Returns "" in
    legacy mode.
    """
    if not principal.user_id:
        return ""
    # owner_user_id is the prefix; everything after the first ":" is sub.
    prefix = f"{principal.owner_user_id}:"
    if principal.user_id.startswith(prefix):
        return principal.user_id[len(prefix):]
    return principal.user_id


async def auth_and_rate_limit(
    request: Request,
    principal: ApiKeyPrincipal = Depends(get_api_key_principal),
) -> ApiKeyPrincipal:
    """Combined dependency: authenticate API Key then enforce rate limit.

    Runs after the request hits /api/v1/ext/* routes.
    1. Validates API Key (via get_api_key_principal — FastAPI-injected)
    2. Checks rate limit against Redis sliding window
    3. Stores metadata on request.state for downstream middleware
    4. Stashes an ExtCallContext on the ContextVar for phase-2 logging
    """
    # Rate limit check
    allowed, remaining, reset_ts = await check_rate_limit(
        api_key_id=principal.key_id,
        limit=principal.rate_limit,
    )

    # Store on request.state for the response middleware
    request.state.api_key_id = principal.key_id
    request.state.rate_limit = principal.rate_limit
    request.state.rate_remaining = remaining
    request.state.rate_reset = reset_ts

    if not allowed:
        from app.core.errors import AppError

        raise AppError(
            code="RATE_LIMIT_EXCEEDED",
            message="请求频率超限，请稍后重试",
            status_code=HTTP_429_TOO_MANY_REQUESTS,
        )

    # Stash call context for phase-2 token backfill (agent path) and
    # middleware fallback (error path). asyncio.create_task copies the
    # context, so background _run() tasks also see this.
    auth_mode = "callback" if principal.user_info_url else "legacy"
    user_sub = _split_user_sub(principal) if auth_mode == "callback" else ""
    set_ext_call_context(ExtCallContext(
        api_key_id=principal.key_id,
        owner_user_id=principal.owner_user_id,
        user_sub=user_sub,
        endpoint=_extract_endpoint(request),
        request_id=getattr(request.state, "request_id", "") or "",
        start_time_ms=int(time.time() * 1000),
    ))

    return principal


# Register sub-routers with combined auth + rate limit
from app.api.v1.ext import agents, files, tasks, workflows  # noqa: E402, F401

router.include_router(agents.router, prefix="")  # type: ignore[has-type]
router.include_router(files.router, prefix="")  # type: ignore[has-type]
router.include_router(workflows.router, prefix="")  # type: ignore[has-type]
router.include_router(tasks.router, prefix="")  # type: ignore[has-type]


class ExtApiStatsMiddleware(BaseHTTPMiddleware):
    """Post-response middleware: record API Key stats + inject rate limit headers.

    Also serves as the fallback writer for execution_logs: if phase 2
    (agent execution) did not consume the stashed ExtCallContext, we
    write a token-less record here so failed/error calls are still
    audited.
    """

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)

        # Only process /ext/ routes that went through auth
        api_key_id = getattr(request.state, "api_key_id", None)
        if api_key_id and str(request.url.path).startswith("/api/v1/ext/"):
            endpoint = _extract_endpoint(request)
            await record_request(
                api_key_id=api_key_id,
                endpoint=endpoint,
                status_code=response.status_code,
            )

            # Fallback: if phase 2 didn't write the unified execution log
            # (e.g. the request failed before reaching agent execution —
            # 401/403/429/422), write a token-less record here so failed
            # calls are still audited.
            #
            # IMPORTANT: Streaming endpoints (invoke/stream, invoke/resume)
            # return a StreamingResponse whose body hasn't been consumed
            # when this middleware fires — the background _run() task is
            # likely still executing and will write the real log.
            # If we wrote here too, we'd produce a duplicate (token=0 +
            # token=real). So we SKIP the fallback for streaming endpoints:
            # they are solely responsible for their own log.
            # Trade-off: if _run() crashes before writing, the streaming
            # call has no log. That's preferable to duplicate logs.
            is_streaming = endpoint in ("agents:invoke:stream", "agents:invoke:resume")
            ctx = get_ext_call_context()
            if ctx is not None and not ctx.consumed and not is_streaming:
                latency_ms = int(time.time() * 1000) - ctx.start_time_ms
                status = "success" if 200 <= response.status_code < 400 else "error"
                from app.services.execution_log_service import ExecutionLogService

                await ExecutionLogService.write_log(
                    user_id=f"{ctx.owner_user_id}:{ctx.user_sub}" if ctx.user_sub else ctx.owner_user_id,
                    api_key_id=ctx.api_key_id,
                    user_sub=ctx.user_sub,
                    visitor_id=ctx.visitor_id,
                    endpoint=ctx.endpoint,
                    request_id=ctx.request_id,
                    status=status,
                    status_code=response.status_code,
                    latency_ms=latency_ms,
                )
                ctx.consumed = True

        # Add rate limit headers when available
        if hasattr(request.state, "rate_limit"):
            response.headers["X-RateLimit-Limit"] = str(request.state.rate_limit)
            response.headers["X-RateLimit-Remaining"] = str(
                getattr(request.state, "rate_remaining", 0)
            )
            response.headers["X-RateLimit-Reset"] = str(
                getattr(request.state, "rate_reset", 0)
            )

        # Surface stale introspection fallback (AC7): when the partner
        # introspection endpoint was unreachable and we degraded to a
        # stale cached result, flag it so clients can retry later.
        if is_introspect_stale():
            response.headers["X-User-Auth-Stale"] = "true"

        return response


def _extract_endpoint(request: Request) -> str:
    """Extract a logical endpoint name from the request path.

    Maps paths like /api/v1/ext/agents/xxx/invoke -> "agents:invoke"
    """
    path = str(request.url.path)

    if "/agents/" in path and path.endswith("/invoke/stream"):
        return "agents:invoke:stream"
    if "/agents/" in path and path.endswith("/invoke/resume"):
        return "agents:invoke:resume"
    if "/agents/" in path and path.endswith("/invoke"):
        return "agents:invoke"
    if "/agents/" in path and path.endswith("/sessions"):
        return "agents:sessions"
    if "/agents" in path:
        return "agents:read"
    if "/workflows/" in path and path.endswith("/invoke"):
        return "workflows:invoke"
    if "/workflows" in path:
        return "workflows:read"
    if "/tasks/" in path:
        return "tasks:read"
    if "/sessions/" in path and "/files" in path:
        return "sessions:files"
    return f"{request.method.lower()}:unknown"
