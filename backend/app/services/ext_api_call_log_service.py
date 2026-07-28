"""External-call request context (ContextVar carrier).

This module used to own the ``ext_api_call_logs`` collection + its CRUD
service. That collection has been merged into the unified ``execution_logs``
table (see ``execution_log_service``). What remains here is the
request-scoped ``ExtCallContext`` — a ContextVar that carries ext-only
fields (api_key_id / user_sub / endpoint / ...) from the
``auth_and_rate_limit`` dependency into the background ``_run()`` task
where the unified execution log is written.

``asyncio.create_task`` copies the context, so the ContextVar set during
request handling is visible inside the streamed agent's background task.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExtCallContext:
    """Stashed ext-call context — populated by ``auth_and_rate_limit``.

    Carries ext-only fields that ``_record_execution_log`` reads to enrich
    the unified execution_logs record (api_key_id / user_sub / endpoint /
    visitor_id). Also carries ``start_time_ms`` for latency and ``consumed``
    so the stats middleware fallback avoids duplicate writes.
    """

    api_key_id: str
    owner_user_id: str = ""
    user_sub: str = ""
    visitor_id: str = ""
    endpoint: str = ""
    request_id: str = ""
    start_time_ms: int = 0
    # Filled lazily once the agent route handler knows them:
    agent_id: str = ""
    session_id: str = ""
    # Set to True once the unified log has been written, so the middleware
    # fallback knows to skip (avoids a duplicate token-less record).
    consumed: bool = False
    # Free-form metadata (e.g. introspect_stale flag).
    extra: dict[str, Any] = field(default_factory=dict)


_ext_call_ctx: ContextVar[ExtCallContext | None] = ContextVar(
    "ext_call_ctx", default=None
)


def set_ext_call_context(ctx: ExtCallContext) -> None:
    """Attach call context to the current async context."""
    _ext_call_ctx.set(ctx)


def get_ext_call_context() -> ExtCallContext | None:
    """Read the stashed call context (or None if not an ext request)."""
    return _ext_call_ctx.get()


def update_ext_call_context(**updates: Any) -> None:
    """Patch fields on the stashed context (e.g. agent_id / session_id)."""
    ctx = _ext_call_ctx.get()
    if ctx is None:
        return
    for k, v in updates.items():
        if v:
            setattr(ctx, k, v)


def clear_ext_call_context() -> None:
    """Reset to None (defensive; mainly for tests)."""
    _ext_call_ctx.set(None)
