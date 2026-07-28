"""Channel error taxonomy.

Two families:
- TransientChannelError: temporary (LLM rate limit, tool blip) → Celery retry
- PermanentChannelError: not retryable (bad creds, code bug) → fallback reply

Each error carries a `user_message` safe to send back to IM users.
The constructor detail MUST NOT be reflected in user_message (no info leak).
"""
from __future__ import annotations


class ChannelError(Exception):
    """Base class for all channel-layer errors."""
    user_message: str = "处理失败,请稍后重试"


class TransientChannelError(ChannelError):
    """Temporary failure. Celery task should retry with backoff."""
    user_message = "服务繁忙,正在重试..."


class PermanentChannelError(ChannelError):
    """Not retryable. Send fallback reply to user, log details."""
    user_message = "处理失败,请联系管理员"


# ── Specific transient errors (raised by adapters / service) ──

class LLMRateLimitError(TransientChannelError):
    user_message = "请求过多,请稍后再试"


class ToolExecutionError(TransientChannelError):
    user_message = "工具暂时不可用,正在重试"


# ── Specific permanent errors ──

class InvalidCredentialsError(PermanentChannelError):
    user_message = "机器人配置异常,请联系管理员"


class AgentRuntimeError(PermanentChannelError):
    user_message = "服务异常,请联系管理员"


class SendFailedError(PermanentChannelError):
    """Channel.send() failed after internal retries. Reply already produced
    by agent but couldn't be delivered to the IM platform."""
    user_message = "消息发送失败,请稍后重试"
