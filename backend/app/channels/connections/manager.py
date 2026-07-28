"""ChannelConnectionManager — owns all live long-connection clients.

Singleton driven by FastAPI lifespan. On startup it scans the DB for channels
with ``receive_mode="long_connection"`` (and ``enabled=True``), and starts one
ConnectionClient per channel. On channel CRUD the API layer calls
``reload_channel(id)`` to add/remove/restart individual connections without a
full server restart.

Design notes
------------
- One asyncio task per channel task wraps the client's connect/keepalive loop.
  Disconnect cancels the task and awaits it.
- ``_clients`` maps ``channel_id -> ConnectionClient``; mutating it is
  serialized through an asyncio.Lock to make concurrent reload_channel calls
  safe.
- ``stop()`` (lifespan shutdown) cancels everything.
- Per-provider availability: if a provider has no ConnectionClient registered
  (e.g. wecom in first iteration), the manager logs and skips that channel
  rather than crashing.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from app.models.channel import ChannelConfig, ChannelProvider

if TYPE_CHECKING:
    # Avoid circular import at runtime — ConnectionClient lives in
    # connections.base, which itself imports from this module indirectly.
    from app.channels.connections.base import ConnectionClient

logger = logging.getLogger(__name__)

# Factory type: (config) -> ConnectionClient
# Providers register their factory here at import time (like ChannelRegistry).
ConnectionClientFactory = Callable[[ChannelConfig], "ConnectionClient"]


class ChannelConnectionManager:
    def __init__(self) -> None:
        # provider name -> factory
        self._factories: dict[str, ConnectionClientFactory] = {}
        # channel_id -> live client
        self._clients: dict[str, ConnectionClient] = {}
        # channel_id -> asyncio task running the client's connect loop
        self._tasks: dict[str, asyncio.Task] = {}
        # channel_id -> asyncio.Semaphore capping concurrent inline executions
        # (long-connection mode runs ChannelService.execute in-process).
        self._execution_sems: dict[str, asyncio.Semaphore] = {}
        self._lock = asyncio.Lock()
        self._stopped: bool = False

    # ── Factory registration ──

    def register_factory(self, provider: str, factory: ConnectionClientFactory) -> None:
        """Register a ConnectionClient factory for a provider.

        Called by provider ``connection.py`` modules at import time.
        """
        self._factories[provider] = factory
        logger.debug("connection_factory_registered provider=%s", provider)

    def supports(self, provider: str | ChannelProvider) -> bool:
        return str(provider) in self._factories

    # ── Lifecycle (FastAPI lifespan) ──

    async def start(self) -> None:
        """Scan DB and start connections for all enabled long-connection channels."""
        self._stopped = False
        try:
            channels = await self._load_long_connection_channels()
        except Exception as exc:
            logger.error("connection_manager_load_failed err=%s", exc)
            return

        for cfg in channels:
            await self._start_channel(cfg)
        logger.info(
            "connection_manager_started channels=%d", len(self._clients)
        )

    async def stop(self) -> None:
        """Cancel every live connection. Called on FastAPI shutdown."""
        self._stopped = True
        async with self._lock:
            channel_ids = list(self._clients.keys())
        for channel_id in channel_ids:
            await self._stop_channel(channel_id)
        logger.info("connection_manager_stopped")

    @property
    def is_running(self) -> bool:
        return not self._stopped

    # ── Per-channel lifecycle (hot reload) ──

    async def reload_channel(self, channel_id: str) -> None:
        """Apply the latest config for a channel.

        Called by the channel CRUD endpoints (create/update/delete/enable/
        disable). Decides whether to start, restart, or stop the connection
        based on the current DB state.

        Safe to call on a channel that doesn't exist or isn't a long-connection
        channel — those resolve to "stop if running, otherwise no-op".
        """
        if self._stopped:
            return

        cfg = await self._load_channel(channel_id)
        async with self._lock:
            already_running = channel_id in self._clients

        should_run = (
            cfg is not None
            and cfg.enabled
            and cfg.receive_mode == "long_connection"
            and self.supports(cfg.provider)
        )

        if not should_run:
            if already_running:
                await self._stop_channel(channel_id)
            return

        # Config changed (or first start) → restart with fresh config
        if already_running:
            await self._stop_channel(channel_id)
        await self._start_channel(cfg)  # type: ignore[arg-type]

    # ── Status ──

    def connection_status(self, channel_id: str) -> str:
        """Return a human-readable status for display in the management API.

        Returns one of: 'long_connection_connected',
        'long_connection_disconnected', 'not_long_connection', 'unknown_channel'.
        """
        client = self._clients.get(channel_id)
        if client is None:
            return "not_long_connection"
        return (
            "long_connection_connected"
            if client.is_connected
            else "long_connection_disconnected"
        )

    def execution_semaphore(self, channel_id: str) -> asyncio.Semaphore | None:
        """Return the per-channel concurrency-limiting semaphore, or None if
        the channel isn't an active long-connection channel.

        dispatch_inbound uses this to cap how many ChannelService.execute
        calls run at once for one channel, preventing a single busy chat
        from exhausting the LLM quota.
        """
        # Lazy-create on first request — channels that never receive events
        # don't allocate a semaphore.
        if channel_id not in self._clients:
            return None
        sem = self._execution_sems.get(channel_id)
        if sem is None:
            from app.core.config import settings
            sem = asyncio.Semaphore(
                settings.CHANNEL_MAX_CONCURRENT_EXECUTIONS_PER_CHANNEL
            )
            self._execution_sems[channel_id] = sem
        return sem

    # ── Internals ──

    async def _load_long_connection_channels(self) -> list[ChannelConfig]:
        from app.services.channel_service import ChannelService

        # ChannelService.list_channels is owner-scoped. The manager runs as
        # system, so we go straight to the collection and filter by mode.
        coll = ChannelService._configs_coll()
        cursor = coll.find({"receive_mode": "long_connection", "enabled": True})
        docs = await cursor.to_list(length=1000)
        return [ChannelConfig(**d) for d in docs]

    async def _load_channel(self, channel_id: str) -> ChannelConfig | None:
        from app.services.channel_service import ChannelService

        return await ChannelService.get_config(channel_id)

    async def _start_channel(self, cfg: ChannelConfig) -> None:
        provider = str(cfg.provider)
        factory = self._factories.get(provider)
        if factory is None:
            logger.warning(
                "connection_no_factory channel=%s provider=%s — skipping",
                cfg.id, provider,
            )
            return

        client = factory(cfg)
        async with self._lock:
            if cfg.id in self._clients:
                # Race: another reload already started it. Back off.
                logger.warning(
                    "connection_already_started channel=%s — race, skipping",
                    cfg.id,
                )
                return
            self._clients[cfg.id] = client

        task = asyncio.create_task(self._run_channel(cfg.id, client))
        async with self._lock:
            self._tasks[cfg.id] = task
        logger.info("connection_started channel=%s provider=%s", cfg.id, provider)

    async def _run_channel(self, channel_id: str, client: ConnectionClient) -> None:
        """Run one connection, reconnecting on failure until cancelled.

        Reconnect uses CHANNEL_CONNECTION_RECONNECT_INTERVAL seconds between
        attempts. Cancellation (via task.cancel()) exits cleanly.
        """
        from app.core.config import settings

        backoff = settings.CHANNEL_CONNECTION_RECONNECT_INTERVAL
        while not self._stopped:
            try:
                logger.info("connection_connecting channel=%s", channel_id)
                await client.connect()
                # connect() returning normally means the SDK exited its loop.
                # That's unexpected for a long-connection — treat as disconnect.
                logger.warning(
                    "connection_exited channel=%s — reconnecting in %ds",
                    channel_id, backoff,
                )
            except asyncio.CancelledError:
                logger.info("connection_cancelled channel=%s", channel_id)
                raise
            except Exception as exc:
                logger.error(
                    "connection_failed channel=%s err=%s — reconnecting in %ds",
                    channel_id, exc, backoff,
                )
            if self._stopped:
                break
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                raise

    async def _stop_channel(self, channel_id: str) -> None:
        async with self._lock:
            client = self._clients.pop(channel_id, None)
            task = self._tasks.pop(channel_id, None)
            # Drop the execution semaphore — a fresh one is created if the
            # channel reconnects. In-flight executions hold their own ref.
            self._execution_sems.pop(channel_id, None)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if client is not None:
            with contextlib.suppress(Exception):
                await client.disconnect()
            logger.info("connection_stopped channel=%s", channel_id)


# ── Module-level singleton ──

_manager: ChannelConnectionManager | None = None


def get_connection_manager() -> ChannelConnectionManager:
    """Return the process-wide ChannelConnectionManager singleton."""
    global _manager
    if _manager is None:
        _manager = ChannelConnectionManager()
    return _manager
