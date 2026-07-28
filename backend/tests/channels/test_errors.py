"""Channel error taxonomy tests."""
from app.channels.errors import (
    AgentRuntimeError,
    ChannelError,
    InvalidCredentialsError,
    LLMRateLimitError,
    PermanentChannelError,
    SendFailedError,
    ToolExecutionError,
    TransientChannelError,
)


class TestErrorHierarchy:
    def test_transient_is_channel_error(self):
        assert issubclass(TransientChannelError, ChannelError)

    def test_permanent_is_channel_error(self):
        assert issubclass(PermanentChannelError, ChannelError)

    def test_specific_transient_errors(self):
        assert issubclass(LLMRateLimitError, TransientChannelError)
        assert issubclass(ToolExecutionError, TransientChannelError)

    def test_specific_permanent_errors(self):
        assert issubclass(InvalidCredentialsError, PermanentChannelError)
        assert issubclass(AgentRuntimeError, PermanentChannelError)
        assert issubclass(SendFailedError, PermanentChannelError)


class TestUserMessages:
    def test_transient_default_message(self):
        err = TransientChannelError("boom")
        assert err.user_message == "服务繁忙,正在重试..."

    def test_permanent_default_message(self):
        err = PermanentChannelError("boom")
        assert err.user_message == "处理失败,请联系管理员"

    def test_llm_rate_limit_message(self):
        err = LLMRateLimitError()
        assert err.user_message == "请求过多,请稍后再试"

    def test_invalid_credentials_message(self):
        err = InvalidCredentialsError()
        assert err.user_message == "机器人配置异常,请联系管理员"

    def test_send_failed_message(self):
        err = SendFailedError()
        assert err.user_message == "消息发送失败,请稍后重试"

    def test_message_independent_of_detail(self):
        err = PermanentChannelError("internal trace")
        # detail 不应泄漏到 user_message
        assert "internal trace" not in err.user_message
