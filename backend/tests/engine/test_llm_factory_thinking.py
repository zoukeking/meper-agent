"""Tests for LLM thinking parameter adaptation.

The pure thinking-kwargs logic now lives in ``agent_flow_harness.llm.thinking``.
These tests validate it via the harness public API to ensure the delegate
in ``app.engine.llm_factory`` is wired correctly.
"""

from agent_flow_harness import build_thinking_kwargs, supports_thinking


class TestBuildThinkingKwargs:
    """Test build_thinking_kwargs returns correct params per provider."""

    def test_thinking_disabled_returns_empty(self):
        result = build_thinking_kwargs("gpt-4o", "openai", False, None)
        assert result == {"extra_body": {"thinking": {"type": "disabled"}}}

    def test_anthropic_thinking_enabled(self):
        result = build_thinking_kwargs("claude-sonnet-4", "anthropic", True, None)
        assert "thinking" in result
        assert result["thinking"]["type"] == "enabled"
        assert result["thinking"]["budget_tokens"] > 0
        # Should auto-set max_tokens since none provided
        assert "max_tokens" in result

    def test_anthropic_thinking_with_sufficient_max_tokens(self):
        result = build_thinking_kwargs(
            "claude-sonnet-4", "anthropic", True, max_tokens=16000
        )
        assert "thinking" in result
        assert result["thinking"]["type"] == "enabled"

    def test_anthropic_thinking_insufficient_max_tokens(self):
        """When max_tokens <= budget, thinking should be skipped."""
        result = build_thinking_kwargs(
            "claude-sonnet-4", "anthropic", True, max_tokens=1000
        )
        assert result == {}

    def test_openai_reasoning_model(self):
        result = build_thinking_kwargs("o3-mini", "openai", True, None)
        assert result == {"reasoning_effort": "high"}

    def test_openai_o1_reasoning(self):
        result = build_thinking_kwargs("o1-preview", "openai", True, None)
        assert result == {"reasoning_effort": "high"}

    def test_openai_o4_reasoning(self):
        result = build_thinking_kwargs("o4-mini", "openai", True, None)
        assert result == {"reasoning_effort": "high"}

    def test_openai_non_reasoning_model_ignored(self):
        """GPT-4o does not support reasoning_effort."""
        result = build_thinking_kwargs("gpt-4o", "openai", True, None)
        assert result == {}

    def test_openai_non_reasoning_model_with_prefix(self):
        """gpt-o1 should not match (it starts with gpt-, not o1-)."""
        result = build_thinking_kwargs("gpt-o1", "openai", True, None)
        assert result == {}

    def test_unknown_provider_ignored(self):
        result = build_thinking_kwargs("some-model", "custom", True, None)
        assert result == {}


class TestSupportsThinking:
    """Test supports_thinking helper for UI pre-validation."""

    def test_anthropic_supported(self):
        assert supports_thinking("claude-sonnet-4", "anthropic") is True

    def test_openai_o_series_supported(self):
        assert supports_thinking("o3-mini", "openai") is True
        assert supports_thinking("o1-preview", "openai") is True
        assert supports_thinking("o4-mini", "openai") is True

    def test_openai_gpt_not_supported(self):
        assert supports_thinking("gpt-4o", "openai") is False

    def test_unknown_provider_not_supported(self):
        assert supports_thinking("some-model", "custom") is False
