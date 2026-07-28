"""loguru-based structured logging with file rotation.

Also configures structlog (used by the harness package) to route through loguru,
so all logs share a unified format and the same file sink.
"""
import json
import logging
import os
import sys
import traceback
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog
from loguru import logger

from app.core.config import settings


class _LoguruHandler(logging.Handler):
    """Bridge stdlib logging → loguru so third-party libs (uvicorn, etc.) share one sink."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str = logger.level(record.levelname).name
        except ValueError:
            level = str(record.levelno)
        logger.opt(depth=6, exception=record.exc_info).log(level, record.getMessage())


def _patch_request_id(record: dict) -> None:
    """Ensure every record carries ``request_id`` so format strings can use it safely.

    Without this, ``{extra[request_id]}`` in a format string raises ``KeyError``
    for logs emitted outside a request scope (startup, tasks without an injected id).
    A global patcher is the single place that guarantees the field always exists.
    """
    record["extra"].setdefault("request_id", None)


def _build_log_dict(record: dict) -> dict:
    """Build a compact, flat dict for JSON output.

    Pulls ``request_id`` up to the top level (alongside standard fields) and
    flattens any remaining business fields (``task_id``, ``action``, ...) from
    ``extra`` so log aggregators can query them directly instead of digging
    into a nested ``extra`` object.
    """
    extra = record["extra"]
    log = {
        "timestamp": record["time"].isoformat(),
        "level": record["level"].name,
        "logger": record["name"],
        "function": record["function"],
        "line": record["line"],
        "request_id": extra.get("request_id"),
        "event": record["message"],
        **{k: v for k, v in extra.items() if k != "request_id"},
    }
    # Format exception traceback as a multi-line string so error context
    # is not lost in JSON output.  ``record["exception"]`` is None when the
    # log call had no exception attached.
    exc = record["exception"]
    if exc is not None:
        log["exception"] = "".join(traceback.format_exception(exc.type, exc.value, exc.traceback))
    return log


def _make_json_sink(writer: Callable[[str], None]) -> Callable[[object], None]:
    """Return a sink that writes compact JSON lines via ``writer``.

    Shared by stdout and file sinks so both emit the same flat schema. The
    verbose ``serialize=True`` envelope (``text`` duplicating the whole record,
    absolute file paths, ``elapsed``, ``thread``/``process`` objects, ...) is
    avoided.
    """

    def _sink(message) -> None:
        log = _build_log_dict(message.record)
        writer(json.dumps(log, ensure_ascii=False, default=str) + "\n")

    return _sink


def _make_file_sink(
    path: str, max_bytes: int, backup_count: int
) -> Callable[[object], None]:
    """Create a file sink with size-based rotation.

    Wraps :class:`logging.handlers.RotatingFileHandler` so we keep rotation
    while emitting the same compact JSON as stdout (loguru's native
    ``serialize=True`` + ``rotation`` can't be combined with a custom sink
    function, and a ``format``-based JSON sink leaks a trailing traceback).

    Rotation is triggered via ``handler.emit()`` (writing to ``handler.stream``
    directly would bypass the rollover check).
    """
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    # A throwaway LogRecord carries our pre-formatted JSON line as ``msg``;
    # the handler's default Formatter just emits ``msg`` unchanged.
    handler.setFormatter(logging.Formatter("%(message)s"))

    def _sink(message) -> None:
        log = _build_log_dict(message.record)
        handler.emit(
            logging.LogRecord(
                name="loguru", level=0, pathname="", lineno=0,
                msg=json.dumps(log, ensure_ascii=False, default=str),
                args=None, exc_info=None,
            )
        )

    return _sink


# Dev-only human-readable format.  Safe to reference ``{extra[request_id]}``
# directly because the global patcher guarantees the key always exists.
_DEV_FORMAT = (
    "<green>{time:HH:mm:ss}</green> | "
    "<level>{level: <5}</level> | "
    "<cyan>{extra[request_id]}</cyan> | "
    "<magenta>{name}:{function}:{line}</magenta> - "
    "<level>{message}</level>"
)


def _configure_structlog() -> None:
    """Route harness structlog logs through loguru for unified formatting."""

    def _loguru_processor(_slog_logger, method_name, event_dict):  # noqa: ANN001
        """Final structlog processor: forward to loguru with structured message."""
        event = event_dict.pop("event", method_name)
        # Attach remaining kv pairs as extra for loguru's message
        parts = [f"{k}={v}" for k, v in event_dict.items() if k not in ("level",)]
        msg = event if not parts else f"{event} {' '.join(parts)}"
        # Use loguru's global logger, not the structlog PrintLogger
        level = method_name.upper() if method_name != "warn" else "WARNING"
        logger.opt(depth=6, capture=False).log(level, msg)
        # structlog requires the last processor to return a value
        return msg

    class _NilLogger:
        """No-op logger factory — actual output is done by _loguru_processor."""
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __getattr__(self, _name: str):
            return lambda *a, **kw: None

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            _loguru_processor,
        ],
        logger_factory=_NilLogger,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.LOG_LEVEL, logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


def setup_logging() -> None:
    """Configure loguru with stdout (concise) and file (JSON) sinks.

    - stdout: human-readable in dev, compact JSON in production (auto-detected
      from ``APP_ENV`` or explicitly via ``LOG_JSON_FORMAT``).
    - file: compact JSON (``logs/app.log``), 50 MB rotation, 7 backups kept.
    - A global patcher guarantees ``request_id`` is always present (``None``
      outside a request scope) so a single stdout sink suffices in dev.
    - structlog (harness) and stdlib logging are routed through loguru.
    """
    # Enable LangSmith tracing if API key is configured.
    # Must be set before LangChain/LangGraph import their tracing machinery.
    if settings.LANGSMITH_API_KEY:
        os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")
        os.environ.setdefault("LANGCHAIN_API_KEY", settings.LANGSMITH_API_KEY)
        os.environ.setdefault("LANGCHAIN_PROJECT", settings.LANGSMITH_PROJECT)
        os.environ.setdefault("LANGCHAIN_ENDPOINT", "https://api.smith.langchain.com")

    logger.remove()

    # Global patcher: ensure request_id is always present in record["extra"].
    # Must be configured before adding sinks so it applies to all of them.
    logger.configure(patcher=_patch_request_id)

    # In production (or when explicitly requested), stdout emits compact JSON for
    # log aggregation systems (ELK / Loki).  In dev, use human-readable color.
    use_json = settings.LOG_JSON_FORMAT or settings.APP_ENV == "production"

    if use_json:
        # Compact JSON stdout — flat fields, no redundant envelope.
        logger.add(_make_json_sink(sys.stdout.write), level=settings.LOG_LEVEL)
    else:
        # Human-readable stdout.  A single sink covers logs with and without
        # request_id (the patcher guarantees the field exists).
        logger.add(
            sys.stdout,
            level=settings.LOG_LEVEL,
            format=_DEV_FORMAT,
        )

    # File sink — compact JSON, size-based rotation (50 MB, 7 backups).
    logger.add(
        _make_file_sink("logs/app.log", max_bytes=50 * 1024 * 1024, backup_count=7),
        level=settings.LOG_LEVEL,
    )

    # Route stdlib logging (uvicorn, etc.) through loguru
    logging.basicConfig(handlers=[_LoguruHandler()], level=0, force=True)

    # Route harness structlog through loguru
    _configure_structlog()


__all__ = ["logger", "setup_logging"]
