"""Tests for ExecutionLogService — channel classification, write, stats."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.services.execution_log_service import (
    CHANNEL_API_KEY,
    CHANNEL_IM,
    CHANNEL_INTERNAL,
    ExecutionLogService,
    classify_channel,
)

# ---------------------------------------------------------------------------
# classify_channel — pure function
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("user_id", "expected"),
    [
        ("user_01KTNVBYQSKQQNW1BAXC436ZJ4", CHANNEL_INTERNAL),
        ("user_abc", CHANNEL_INTERNAL),
        ("user_01KTNVBYQSKQQNW1BAXC436ZJ4:1", CHANNEL_API_KEY),
        ("user_01KTNVBYQSKQQNW1BAXC436ZJ4:visitor-uuid", CHANNEL_API_KEY),
        ("channel:ch_01KXYY5SXB9882A7Q7PZFWFJT6:oc_682f108", CHANNEL_IM),
        ("", CHANNEL_INTERNAL),
        ("weird", CHANNEL_INTERNAL),
    ],
)
def test_classify_channel(user_id: str, expected: str) -> None:
    assert classify_channel(user_id) == expected


# ---------------------------------------------------------------------------
# write_log
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_log_inserts_with_correct_source() -> None:
    """write_log derives source from user_id and inserts one document."""
    mock_col = MagicMock()
    mock_col.insert_one = AsyncMock(return_value=MagicMock(inserted_id="xlog_1"))

    mock_db = MagicMock()
    mock_db.__getitem__.side_effect = lambda key: mock_col if key == "execution_logs" else MagicMock()

    with patch("app.services.execution_log_service.get_database", return_value=mock_db):
        # internal
        await ExecutionLogService.write_log(user_id="user_01", agent_id="agent_1", total_tokens=100)
        inserted_internal = mock_col.insert_one.call_args_list[0].args[0]
        assert inserted_internal["source"] == CHANNEL_INTERNAL
        assert inserted_internal["agent_id"] == "agent_1"
        assert inserted_internal["total_tokens"] == 100

        # api_key
        await ExecutionLogService.write_log(user_id="user_01:1", api_key_id="apikey_1")
        inserted_ext = mock_col.insert_one.call_args_list[1].args[0]
        assert inserted_ext["source"] == CHANNEL_API_KEY
        assert inserted_ext["api_key_id"] == "apikey_1"

        # im
        await ExecutionLogService.write_log(user_id="channel:ch_1:oc_1")
        inserted_im = mock_col.insert_one.call_args_list[2].args[0]
        assert inserted_im["source"] == CHANNEL_IM
        assert inserted_im["channel_id"] == "ch_1"


@pytest.mark.asyncio
async def test_write_log_failure_does_not_raise() -> None:
    """A DB error during write_log must be swallowed (logged), not raised."""
    mock_col = MagicMock()
    mock_col.insert_one = AsyncMock(side_effect=Exception("DB down"))

    mock_db = MagicMock()
    mock_db.__getitem__.side_effect = lambda key: mock_col if key == "execution_logs" else MagicMock()

    with patch("app.services.execution_log_service.get_database", return_value=mock_db):
        result = await ExecutionLogService.write_log(user_id="user_01")
    assert result is None  # failure returns None, no exception


# ---------------------------------------------------------------------------
# get_stats — aggregation (mocked)
# ---------------------------------------------------------------------------


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
async def test_get_stats_groups_by_source() -> None:
    """get_stats aggregates execution_logs by source channel."""
    agg_rows = [
        {"_id": CHANNEL_INTERNAL, "calls": 10, "tokens": 5000, "input_tokens": 4000,
         "output_tokens": 1000, "llm_calls": 20, "avg_latency_ms": 1500.0,
         "success": 9, "failed": 1},
        {"_id": CHANNEL_API_KEY, "calls": 5, "tokens": 2000, "input_tokens": 1800,
         "output_tokens": 200, "llm_calls": 8, "avg_latency_ms": 800.0,
         "success": 5, "failed": 0},
    ]
    mock_col = MagicMock()
    mock_col.aggregate = MagicMock(return_value=_MockAggCursor(agg_rows))
    mock_col.count_documents = AsyncMock(return_value=15)

    mock_db = MagicMock()
    mock_db.__getitem__.side_effect = lambda key: mock_col if key == "execution_logs" else MagicMock()

    with patch("app.services.execution_log_service.get_database", return_value=mock_db):
        result = await ExecutionLogService.get_stats()

    channels = result["channels"]
    assert channels[CHANNEL_INTERNAL]["calls"] == 10
    assert channels[CHANNEL_INTERNAL]["tokens"] == 5000
    assert channels[CHANNEL_INTERNAL]["success"] == 9
    assert channels[CHANNEL_API_KEY]["calls"] == 5
    assert channels[CHANNEL_IM]["calls"] == 0  # no data → zeros
    # totals
    totals = result["totals"]
    assert totals["calls"] == 15  # 10 + 5
    assert totals["tokens"] == 7000
    assert totals["success_rate"] == round(14 / 15 * 100, 1)


# ---------------------------------------------------------------------------
# list_logs — paginated query
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_logs_paginates_and_filters() -> None:
    """list_logs applies filters and returns (items, total)."""
    mock_col = MagicMock()
    mock_col.count_documents = AsyncMock(return_value=3)
    cursor = MagicMock()
    cursor.sort = MagicMock(return_value=cursor)
    cursor.skip = MagicMock(return_value=cursor)
    cursor.limit = MagicMock(return_value=cursor)
    cursor.to_list = AsyncMock(return_value=[{"source": "internal", "agent_id": "a1"}])
    mock_col.find = MagicMock(return_value=cursor)

    mock_db = MagicMock()
    mock_db.__getitem__.side_effect = lambda key: mock_col if key == "execution_logs" else MagicMock()

    with patch("app.services.execution_log_service.get_database", return_value=mock_db):
        items, total = await ExecutionLogService.list_logs(
            source="internal", page=1, page_size=10,
        )

    assert total == 3
    assert len(items) == 1
    assert items[0]["source"] == "internal"
