"""PEP 562 entry: importing this package triggers registration of all
built-in adapters via their @ChannelRegistry.register decorators, and
long-connection factories via ChannelConnectionManager.register_factory.

Adding a new platform = create providers/<name>/ + add one import line here.
ChannelRegistry / ChannelConnectionManager themselves never need editing.
"""
from . import (
    dingtalk,  # noqa: F401
    lark,  # noqa: F401
    mock,  # noqa: F401
    wecom,  # noqa: F401
)

# Register long-connection factories (no-public-URL receive mode). Each
# provider's connection module checks its own CHANNEL_*_LONG_CONNECTION_ENABLED
# flag and no-ops if disabled. Importing here runs that check once at startup.
from .dingtalk.connection import register_dingtalk_connection  # noqa: F401
from .lark.connection import register_lark_connection  # noqa: F401

register_lark_connection()
register_dingtalk_connection()
# wecom long-connection factory added in a later task.
