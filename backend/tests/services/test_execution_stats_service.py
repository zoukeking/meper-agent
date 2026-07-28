"""Tests for ExecutionStatsService — range resolution + task aggregation."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.services.execution_stats_service import ExecutionStatsService


def test_resolve_range_single_day() -> None:
    """A date param expands to a [00:00, next-day 00:00) UTC window."""
    from app.services.execution_stats_service import _resolve_range

    start, end = _resolve_range(None, None, "2026-07-22")
    assert start == "2026-07-22T00:00:00+00:00"
    assert end == "2026-07-23T00:00:00+00:00"


def test_resolve_range_passthrough() -> None:
    """start/end are passed through unchanged when no date is given."""
    from app.services.execution_stats_service import _resolve_range

    start, end = _resolve_range("2026-07-01T00:00:00+00:00", "2026-08-01T00:00:00+00:00", None)
    assert start == "2026-07-01T00:00:00+00:00"
    assert end == "2026-08-01T00:00:00+00:00"


class _MockAggCursor:
    def __init__(self, rows):
        self._rows = list(rows)

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for row in self._rows:
            yield row

    async def to_list(self, length=None):
        return list(self._rows)


@pytest.mark.asyncio
async def test_get_stats_merges_agent_and_task_stats() -> None:
    """get_stats combines execution_logs (agent) + tasks (workflow) stats."""
    # Mock ExecutionLogService.get_stats (agent-call stats).
    agent_stats = {
        "channels": {
            "internal": {"calls": 2, "tokens": 100, "success": 2, "failed": 0},
            "api_key": {"calls": 3, "tokens": 50, "success": 1, "failed": 2},
            "im": {"calls": 0, "tokens": 0, "success": 0, "failed": 0},
        },
        "totals": {"calls": 5, "tokens": 150, "success": 3, "failed": 2, "success_rate": 60.0},
    }
    # Mock tasks aggregation.
    task_rows = [
        {"_id": "user", "tasks": 10, "tokens": 5000},
        {"_id": "api_key", "tasks": 5, "tokens": 2000},
        {"_id": "agent", "tasks": 3, "tokens": 1500},
    ]

    mock_db = MagicMock()
    mock_tasks_col = MagicMock()
    mock_tasks_col.aggregate = MagicMock(return_value=_MockAggCursor(task_rows))
    mock_db.__getitem__.side_effect = lambda key: mock_tasks_col if key == "tasks" else MagicMock()

    with (
        patch(
            "app.services.execution_stats_service.ExecutionLogService.get_stats",
            new_callable=AsyncMock,
            return_value=agent_stats,
        ),
        patch("app.services.execution_stats_service.get_database", return_value=mock_db),
    ):
        result = await ExecutionStatsService.get_stats(date="2026-07-22")

    # Agent-call stats passed through.
    assert result["channels"]["internal"]["calls"] == 2
    assert result["totals"]["calls"] == 5
    # Task stats mapped by created_by_type.
    assert result["tasks"]["internal"]["tasks"] == 10
    assert result["tasks"]["api_key"]["tasks"] == 5
    assert result["tasks"]["agent_triggered"]["tasks"] == 3
    # Range surfaced.
    assert result["range"]["start"] == "2026-07-22T00:00:00+00:00"
