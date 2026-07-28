"""Request-scoped state for end-user authentication.

Holds flags that the auth dependency sets and middleware reads, to avoid
threading ad-hoc parameters through the whole call stack.
"""
from __future__ import annotations

from contextvars import ContextVar

# True when the current request's introspection result came from a stale
# cache fallback (partner introspection endpoint unreachable). The ext
# middleware reads this to set the ``X-User-Auth-Stale: true`` response
# header (AC7).
_introspect_stale: ContextVar[bool] = ContextVar(
    "introspect_stale",
    default=False,
)


def mark_introspect_stale() -> None:
    """Flag the current request as having used a stale introspection cache."""
    _introspect_stale.set(True)


def is_introspect_stale() -> bool:
    """Return True iff the current request used a stale introspection cache."""
    return _introspect_stale.get()


def reset_introspect_stale() -> None:
    """Reset the flag (mainly for tests)."""
    _introspect_stale.set(False)
