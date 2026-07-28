"""Channel adapter registry.

Uses a @register decorator pattern. Providers register themselves on import
via PEP 562 (see app/channels/providers/__init__.py), matching the existing
TOOL_REGISTRY pattern in packages/harness.
"""
from __future__ import annotations

import contextlib

from app.channels.base import Channel


class ChannelRegistry:
    _registry: dict[str, type[Channel]] = {}

    @classmethod
    def register(cls, provider: str):
        """Class decorator: register a Channel subclass under `provider`."""
        def wrapper(channel_cls: type[Channel]):
            if not getattr(channel_cls, "provider", None):
                raise ValueError(
                    f"{channel_cls.__name__} must set a ClassVar `provider`"
                )
            cls._registry[provider] = channel_cls
            return channel_cls
        return wrapper

    @classmethod
    def get(cls, provider: str) -> Channel:
        """Return a fresh adapter instance for `provider`.

        Lazily imports providers package on first miss to trigger PEP 562
        registration. Stateless — each call returns a new instance.
        """
        if provider not in cls._registry:
            cls._load_providers()
        if provider not in cls._registry:
            raise KeyError(
                f"No channel adapter registered for provider={provider!r}. "
                f"Known: {list(cls._registry.keys())}"
            )
        return cls._registry[provider]()

    @classmethod
    def known_providers(cls) -> list[str]:
        cls._load_providers()
        return list(cls._registry.keys())

    @classmethod
    def _load_providers(cls) -> None:
        """Trigger PEP 562 provider imports. No-op if the providers package
        isn't installed yet (e.g. before Task 4)."""
        with contextlib.suppress(ImportError):
            from app.channels import providers  # noqa: F401
