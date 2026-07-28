"""ConnectionClient — abstract base for provider-specific long-connection clients.

Each provider that supports a "no public URL" receive mode implements this ABC
to wrap its platform SDK (lark-oapi ws.Client, dingtalk-stream, etc.) into a
uniform lifecycle the ChannelConnectionManager can drive.

Lifecycle contract
------------------
1. ``__init__(config)`` stores the ChannelConfig (no I/O)
2. ``await connect()`` establishes the WebSocket / long-connection, returns
   once the connection is healthy. May block on SDK ``start()`` via
   ``asyncio.to_thread`` internally — never call ``connect`` without an await.
3. ``await disconnect()`` tears down the connection and any background thread.
   Idempotent — safe to call on an already-stopped client.
4. ``is_connected`` reflects live state (not just "connect was called").

Event flow
----------
On receiving an event from the platform, the implementation builds the same
JSON body the HTTP webhook would have produced, then calls the shared helper
``dispatch_inbound(config, body)`` from ``app.channels.connections.dispatch``.
That helper parses, dedups, persists, and enqueues the Celery task — so the
downstream pipeline is identical between webhook and long-connection modes.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from app.models.channel import ChannelConfig


class ConnectionClient(ABC):
    """A live long-connection for one ChannelConfig.

    One instance per channel. NOT thread-safe across channels — the manager
    creates a fresh instance per channel config.
    """

    def __init__(self, config: ChannelConfig) -> None:
        self.config = config

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """True if the underlying transport is currently connected."""

    @abstractmethod
    async def connect(self) -> None:
        """Establish the connection. Blocks until connected or raises.

        Implementations MUST confine any blocking SDK ``start()`` call to a
        thread via ``asyncio.to_thread`` so the event loop is not stalled.
        """

    @abstractmethod
    async def disconnect(self) -> None:
        """Tear down the connection. Idempotent."""

    @property
    def channel_id(self) -> str:
        return self.config.id
