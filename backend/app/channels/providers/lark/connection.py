"""Lark (飞书) long-connection client — receive events without a public URL.

Wraps ``lark_oapi.ws.Client`` (WebSocket) into our ConnectionClient lifecycle.
The SDK's ``start()`` is blocking and runs its own event loop internally, so
we run it inside a thread via ``asyncio.to_thread``. Cancellation of that
thread task is how the manager stops us.

Event flow on receipt:
  SDK callback → reconstruct the v2 event envelope JSON →
  ``dispatch_inbound`` (parses, dedups, persists, enqueues Celery) →
  the same pipeline as HTTP webhook mode.

No signature verification is needed here — the SDK transport authenticates
the platform-side TLS + session, so events arriving via ws.Client are trusted
by construction. URL-verification challenges are NOT sent over WebSocket.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
from typing import TYPE_CHECKING

from app.channels.connections.base import ConnectionClient
from app.channels.connections.dispatch import dispatch_inbound
from app.channels.connections.manager import get_connection_manager
from app.channels.providers.lark.verify import parse_lark_event
from app.core.crypto import decrypt_secret
from app.models.channel import ChannelConfig

if TYPE_CHECKING:
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

logger = logging.getLogger(__name__)


def register_lark_connection() -> None:
    """Register the lark ConnectionClient factory with the global manager.

    Called from ``app.channels.providers.__init__`` (PEP 562 import side
    effect) so the manager knows lark supports long-connection mode.
    """
    if not _is_long_connection_enabled():
        logger.info("lark_long_connection_disabled_by_config")
        return
    get_connection_manager().register_factory("lark", LarkConnectionClient)
    logger.info("lark_long_connection_registered")


def _is_long_connection_enabled() -> bool:
    from app.core.config import settings
    return settings.CHANNEL_LARK_LONG_CONNECTION_ENABLED


class LarkConnectionClient(ConnectionClient):
    """One WebSocket connection to Lark for one ChannelConfig.

    Thread-safety: a single instance is owned by exactly one manager task;
    no external concurrency.
    """

    # The lark-oapi ws.Client uses a module-level ``loop`` variable captured
    # at import time (lark_oapi/ws/client.py:32) and calls
    # ``loop.run_until_complete(self._connect())`` inside ``start()``. That
    # loop is the main process loop — which FastAPI/uvicorn is already running.
    # Running start() as-is raises "this event loop is already running".
    #
    # Workaround: run start() in a dedicated worker thread that owns its OWN
    # asyncio loop, and temporarily swap the SDK's module-level ``loop`` to
    # point at it. Because the swap mutates a shared module global, concurrent
    # SDK starts across channels would race — so we serialize them with this
    # class-level lock. Each start() runs under the lock; the swap is reverted
    # before release so the next acquirer sees a clean state.
    _sdk_loop_lock = threading.Lock()

    def __init__(self, config: ChannelConfig) -> None:
        super().__init__(config)
        self._ws_client = None
        self._loop: asyncio.AbstractEventLoop | None = None  # set in connect()
        # The dedicated asyncio loop running the SDK inside _run_sdk's thread.
        # Kept as an instance attribute so disconnect() can close it to force
        # start() to return (the SDK has no public stop API).
        self._sdk_loop: asyncio.AbstractEventLoop | None = None
        self._stop_event = threading.Event()
        self._connected = threading.Event()
        # Set once ws.Client.start() has returned (clean exit or crash).
        self._exited = threading.Event()

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set() and not self._exited.is_set()

    async def connect(self) -> None:
        """Build the SDK client and start it in a worker thread.

        ``ws.Client.start()`` blocks forever (until disconnect cancels it).
        We can't ``await`` that directly — it would block the asyncio loop.
        Instead we run it in a thread and ``await`` an asyncio.Event that's
        set when the thread exits.

        The asyncio task wrapping this coroutine is what the manager cancels
        on disconnect; cancellation propagates here as CancelledError, which
        we translate into stopping the SDK client.
        """
        import lark_oapi as lark
        from lark_oapi import ws

        app_id = self._get_credential("app_id")
        app_secret = self._get_credential("app_secret")
        encrypt_key = self._get_optional_credential("encrypt_key") or ""
        verification_token = self._get_optional_credential("verification_token") or ""

        # Build event handler — same parser as webhook mode (parse_lark_event
        # handles text filtering, content extraction, etc.). We pass the
        # decrypted verification_token/encrypt_key to the SDK so it can
        # decrypt event payloads for us (when configured).
        event_handler = (
            lark.EventDispatcherHandler.builder(encrypt_key, verification_token)
            .register_p2_im_message_receive_v1(self._on_message_receive)
            .build()
        )

        self._ws_client = ws.Client(
            app_id=app_id,
            app_secret=app_secret,
            event_handler=event_handler,
            auto_reconnect=True,  # SDK reconnects on transient drops
            log_level=lark.LogLevel.INFO,
        )

        # Capture the running loop so the SDK callback (which fires from the
        # SDK's own thread) can schedule dispatch_inbound back onto it.
        self._loop = asyncio.get_running_loop()

        # Mark "connected" optimistically once the SDK is asked to start.
        # The SDK doesn't expose a connect callback; auto_reconnect + the
        # start() loop running is the best proxy we have.
        self._stop_event.clear()
        self._exited.clear()
        self._connected.set()

        try:
            # Run the blocking start() in a thread; await until it returns
            # (normal exit) or we're cancelled.
            await asyncio.to_thread(self._run_sdk)
        except asyncio.CancelledError:
            self._stop_sdk()
            raise
        finally:
            self._connected.clear()
            self._exited.set()

    def _run_sdk(self) -> None:
        """Run ws.Client.start() in a worker thread with its own event loop.

        The lark SDK captures a module-level ``loop`` at import time and uses
        it inside start() via ``loop.run_until_complete(...)``. That loop is
        the main process loop, which FastAPI is already running — so calling
        start() directly raises "this event loop is already running".

        Fix: in this worker thread, create a fresh asyncio loop, make it the
        thread-local current loop, AND temporarily point the SDK's module-level
        ``loop`` at it. The module-level swap is serialized by a class-level
        lock so concurrent channel starts don't race on the shared global.
        """
        import lark_oapi.ws.client as ws_module

        if self._ws_client is None:
            return

        # Create a fresh loop owned by this thread. Stored on self so
        # disconnect() can close it to force start() to return.
        thread_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(thread_loop)
        self._sdk_loop = thread_loop

        saved_sdk_loop = ws_module.loop
        acquired = False
        try:
            # Serialize the module-level swap across all LarkConnectionClients.
            self._sdk_loop_lock.acquire()
            acquired = True
            ws_module.loop = thread_loop
            self._ws_client.start()
        except Exception as exc:
            logger.error(
                "lark_ws_client_exited channel=%s err=%s", self.config.id, exc
            )
        finally:
            if acquired:
                # Restore the SDK's module-level loop before releasing the
                # lock, so the next acquirer finds a clean state. The fresh
                # thread_loop is closed below and not referenced anywhere else.
                with contextlib.suppress(Exception):
                    ws_module.loop = saved_sdk_loop
                self._sdk_loop_lock.release()
            with contextlib.suppress(Exception):
                thread_loop.close()

    async def disconnect(self) -> None:
        """Stop the SDK client and join the worker thread.

        The lark SDK exposes no stop API; start() blocks on its loop's
        ``run_until_complete(_select())``. The only way to unblock it is to
        stop the loop it's running on. We schedule loop.stop() from this
        coroutine (the FastAPI loop) — it wakes the worker loop, start()
        returns, _run_sdk's finally block restores the SDK global and closes
        the worker loop.
        """
        self._stop_event.set()
        self._connected.clear()
        sdk_loop = self._sdk_loop
        if sdk_loop is not None and not sdk_loop.is_closed():
            # Schedule stop from this thread. loop.stop() is thread-safe.
            with contextlib.suppress(RuntimeError):
                sdk_loop.call_soon_threadsafe(sdk_loop.stop)
        self._exited.set()

    def _stop_sdk(self) -> None:
        """Legacy sync stop — only used as a fallback during cancellation.
        Prefer disconnect() which properly wakes the worker loop."""
        self._stop_event.set()
        self._connected.clear()
        sdk_loop = self._sdk_loop
        if sdk_loop is not None and not sdk_loop.is_closed():
            with contextlib.suppress(RuntimeError):
                sdk_loop.call_soon_threadsafe(sdk_loop.stop)
        self._exited.set()

    # ── SDK event callback (runs in the SDK's thread) ──

    def _on_message_receive(self, data: P2ImMessageReceiveV1) -> None:
        """Callback fired by the lark SDK on each im.message.receive_v1 event.

        ``data`` is the SDK's typed event object. We reconstruct the original
        v2 event envelope JSON so ``parse_lark_event`` can process it exactly
        as in webhook mode (single source of truth for parsing).
        """
        try:
            body = self._reconstruct_envelope(data)
        except Exception as exc:
            logger.warning(
                "lark_ws_reconstruct_failed channel=%s err=%s",
                self.config.id, exc,
            )
            return

        # dispatch_inbound is async; the SDK callback fires from the SDK's
        # own thread (no asyncio loop there). Schedule it on the loop our
        # connect() is running on — captured at connect() time.
        loop = self._loop
        if loop is None or loop.is_closed():
            logger.error(
                "lark_ws_no_loop channel=%s — dropping event", self.config.id,
            )
            return

        asyncio.run_coroutine_threadsafe(
            dispatch_inbound(
                config=self.config, body=body, parser=parse_lark_event,
            ),
            loop,
        )

    @staticmethod
    def _reconstruct_envelope(data: P2ImMessageReceiveV1) -> str:
        """Turn the SDK's typed event into the v2 envelope JSON webhook mode
        would have received. parse_lark_event consumes this shape.

        The lark SDK's event objects are plain Python objects (NOT pydantic
        models) — they don't have model_dump(). We use _obj_to_dict which
        walks __dict__ recursively.
        """
        def _obj_to_dict(obj, depth=0):
            """Recursively convert a lark SDK object to a dict via __dict__."""
            if depth > 10:  # safety: prevent infinite recursion
                return str(obj)
            if obj is None:
                return None
            if isinstance(obj, str):
                return obj
            if isinstance(obj, (int, float, bool)):
                return obj
            if isinstance(obj, (list, tuple)):
                return [_obj_to_dict(x, depth + 1) for x in obj]
            if isinstance(obj, dict):
                return {k: _obj_to_dict(v, depth + 1) for k, v in obj.items()}
            # SDK object: recurse into __dict__
            if hasattr(obj, '__dict__'):
                result = {}
                for k, v in obj.__dict__.items():
                    if v is not None:  # skip None fields
                        result[k] = _obj_to_dict(v, depth + 1)
                return result
            return str(obj)

        header = _obj_to_dict(data.header) if data.header else {}
        event = _obj_to_dict(data.event) if data.event else {}
        envelope = {"schema": "2.0", "header": header, "event": event}
        return json.dumps(envelope, ensure_ascii=False)

    # ── Credential access ──

    def _get_credential(self, key: str) -> str:
        from app.channels.errors import InvalidCredentialsError
        encrypted = self.config.credentials.get(key)
        if not encrypted:
            raise InvalidCredentialsError(f"missing credential: {key}")
        return decrypt_secret(encrypted)

    def _get_optional_credential(self, key: str) -> str | None:
        encrypted = self.config.credentials.get(key)
        if not encrypted:
            return None
        try:
            return decrypt_secret(encrypted)
        except Exception:
            return None
