"""Channel long-connection infrastructure.

This subpackage hosts the WebSocket / long-poll client framework that lets
channels receive events without exposing a public callback URL (deployment
behind NAT / firewall). HTTP webhook mode remains the default; long-connection
mode is opt-in per channel via ``ChannelConfig.receive_mode``.

Public surface:
    - ConnectionClient: ABC each provider implements for its SDK
    - ChannelConnectionManager: singleton that owns all live connections,
      driven by FastAPI lifespan and reloaded on channel CRUD
"""
from app.channels.connections.manager import (
    ChannelConnectionManager,
    get_connection_manager,
)

__all__ = ["ChannelConnectionManager", "get_connection_manager"]
