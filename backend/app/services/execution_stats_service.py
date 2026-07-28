"""Execution statistics — cross-channel agent-execution overview.

Aggregates data from two sources:
1. ``execution_logs`` — per-call agent execution records (internal / api_key
   / im channels). Independent of sessions, so survives session deletion.
2. ``tasks`` — workflow execution records, grouped by ``created_by_type``.

Channel classification lives in ``execution_log_service.classify_channel``.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.db.mongodb import get_database
from app.services.execution_log_service import ExecutionLogService


def _resolve_range(
    start: str | None,
    end: str | None,
    date: str | None,
) -> tuple[str | None, str | None]:
    """Resolve query params into a [start, end) UTC ISO-string range.

    - ``date`` (single day) → that day's UTC 00:00:00 .. next day 00:00:00
    - ``start``/``end``    → pass through as-is (already ISO strings)
    """
    if date:
        try:
            day = datetime.fromisoformat(date).replace(tzinfo=UTC)
        except ValueError:
            return start, end
        day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        return day_start.isoformat(), day_end.isoformat()
    return start, end


class ExecutionStatsService:
    """Aggregate agent-execution stats grouped by access channel + task type."""

    @staticmethod
    async def get_stats(
        *,
        start: str | None = None,
        end: str | None = None,
        date: str | None = None,
    ) -> dict[str, Any]:
        """Return per-channel agent-call stats + task-execution stats.

        Returns ``{"range": {...}, "channels": {...}, "tasks": {...}, "totals": {...}}``.
        """
        start, end = _resolve_range(start, end, date)

        # ── Agent-call stats from execution_logs ──
        agent_stats = await ExecutionLogService.get_stats(start=start, end=end)

        # ── Task (workflow) stats from tasks collection ──
        task_stats = await ExecutionStatsService._get_task_stats(start, end)

        return {
            "range": {"start": start, "end": end},
            "channels": agent_stats["channels"],
            "totals": agent_stats["totals"],
            "tasks": task_stats,
        }

    @staticmethod
    async def _get_task_stats(
        start: str | None,
        end: str | None,
    ) -> dict[str, Any]:
        """Aggregate workflow task execution grouped by created_by_type.

        Maps ``created_by_type`` → channel: user→internal, api_key→api_key,
        agent→agent_triggered (子任务), system→scheduled.
        tasks.created_at is an ISO string (lexical comparison works).
        """
        match: dict[str, Any] = {}
        if start or end:
            rng: dict[str, Any] = {}
            if start:
                rng["$gte"] = start
            if end:
                rng["$lt"] = end
            if rng:
                match["created_at"] = rng

        pipeline = [
            {"$match": match},
            {
                "$group": {
                    "_id": "$created_by_type",
                    "tasks": {"$sum": 1},
                    "tokens": {"$sum": {"$ifNull": ["$total_tokens", 0]}},
                }
            },
        ]
        db = get_database()
        rows = await db["tasks"].aggregate(pipeline).to_list(length=10)

        result = {
            "internal": {"tasks": 0, "tokens": 0},
            "api_key": {"tasks": 0, "tokens": 0},
            "agent_triggered": {"tasks": 0, "tokens": 0},
            "scheduled": {"tasks": 0, "tokens": 0},
        }
        type_map = {
            "user": "internal",
            "api_key": "api_key",
            "agent": "agent_triggered",
            "system": "scheduled",
        }
        for row in rows:
            key = type_map.get(row["_id"], "scheduled")
            if key not in result:
                result[key] = {"tasks": 0, "tokens": 0}
            result[key]["tasks"] = row.get("tasks", 0)
            result[key]["tokens"] = row.get("tokens", 0)

        return result


execution_stats_service = ExecutionStatsService()
