"""Tests for AgentExecutionService — execution orchestration.

Covers the pure helper logic (error-source classification) that the stream/
resume top-level except blocks rely on to populate ErrorEvent.source.
"""
from app.services.agent_execution_service import _classify_error_source


# ── _classify_error_source ──────────────────────────────────────────────────


def test_classify_llm_error_by_type():
    """LLM 限流类异常（类型名含 ratelimit）归 llm。"""
    class RateLimitError(Exception):
        pass
    assert _classify_error_source(RateLimitError("too many")) == "llm"


def test_classify_llm_error_by_message():
    """错误消息含 LLM 关键词（如 quota）归 llm。"""
    assert _classify_error_source(Exception("insufficient_quota for model")) == "llm"


def test_classify_generic_error_as_graph():
    """未识别的异常归 graph。"""
    assert _classify_error_source(RuntimeError("unexpected")) == "graph"


def test_classify_empty_message_returns_graph():
    """无异常信息时归 graph。"""
    assert _classify_error_source(Exception("")) == "graph"
