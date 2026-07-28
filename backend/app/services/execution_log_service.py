"""Execution log service — unified agent-execution records across channels.

Writes one ``execution_logs`` document per agent invocation (invoke / stream /
resume), regardless of the access channel (internal / api_key / im). This
collection is independent of ``sessions`` so statistics survive session
deletion. See ``ExecutionLog`` model for the document schema.

Channel classification mirrors ``execution_stats_service.classify_channel``:
``channel:`` prefix → im, ``user_`` without colon → internal, colon present
→ api_key.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger

from app.db.mongodb import get_database
from app.models.execution_log import ExecutionLog

COLLECTION = "execution_logs"
TTL_SECONDS = 365 * 86400  # 365 days — internal audit needs long retention

CHANNEL_INTERNAL = "internal"
CHANNEL_API_KEY = "api_key"
CHANNEL_IM = "im"


def classify_channel(user_id: str) -> str:
    """Classify a user_id into an access channel.

    Priority: ``channel:`` prefix → im; bare ``user_`` (no colon) → internal;
    anything else with a colon → api_key.
    """
    if not user_id:
        return CHANNEL_INTERNAL
    if user_id.startswith("channel:"):
        return CHANNEL_IM
    if user_id.startswith("user_") and ":" not in user_id:
        return CHANNEL_INTERNAL
    if ":" in user_id:
        return CHANNEL_API_KEY
    return CHANNEL_INTERNAL


def _extract_channel_id(user_id: str) -> str:
    """Extract the channel_id from an IM user_id ``channel:{id}:{chat}``."""
    parts = user_id.split(":", 2)
    return parts[1] if len(parts) >= 2 else ""


class ExecutionLogService:
    """CRUD + queries for ``execution_logs``."""

    COLLECTION = COLLECTION
    TTL_SECONDS = TTL_SECONDS

    @staticmethod
    def _collection():
        return get_database()[ExecutionLogService.COLLECTION]

    # ── Indexes ──

    @staticmethod
    async def ensure_indexes() -> None:
        col = ExecutionLogService._collection()
        await col.create_index(
            [("source", 1), ("timestamp", -1)],
            name="idx_xlog_source_time",
        )
        await col.create_index(
            [("user_id", 1), ("timestamp", -1)],
            name="idx_xlog_user_time",
        )
        await col.create_index(
            [("agent_id", 1), ("timestamp", -1)],
            name="idx_xlog_agent_time",
        )
        await col.create_index("session_id", name="idx_xlog_session")
        await col.create_index("request_id", name="idx_xlog_request_id")
        # API Key 维度查询（服务 API Keys 页面的 /stats /logs /users）。
        await col.create_index(
            [("api_key_id", 1), ("timestamp", -1)],
            name="idx_xlog_key_time",
        )
        await col.create_index(
            [("api_key_id", 1), ("user_sub", 1), ("timestamp", -1)],
            name="idx_xlog_key_user_time",
        )
        # TTL: timestamp MUST be a BSON date for the TTL monitor to expire docs.
        await col.create_index(
            "timestamp",
            expireAfterSeconds=ExecutionLogService.TTL_SECONDS,
            name="idx_xlog_ttl",
        )
        logger.info("ExecutionLog indexes ensured")

    # ── Write ──

    @staticmethod
    async def write_log(
        *,
        user_id: str,
        agent_id: str = "",
        session_id: str = "",
        request_id: str = "",
        api_key_id: str = "",
        user_sub: str = "",
        visitor_id: str = "",
        endpoint: str = "",
        status: str = "success",
        status_code: int = 0,
        latency_ms: int = 0,
        total_tokens: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        llm_calls: int = 0,
    ) -> str | None:
        """Insert one execution-log document.

        Channel (source) is derived from ``user_id``. Failure is logged but
        never raised — execution logging must not break the user-facing
        request flow.

        Returns the inserted document id, or None on failure.
        """
        source = classify_channel(user_id)
        channel_id = _extract_channel_id(user_id) if source == CHANNEL_IM else ""
        doc = ExecutionLog(
            source=source,
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
            request_id=request_id,
            api_key_id=api_key_id,
            user_sub=user_sub,
            visitor_id=visitor_id,
            endpoint=endpoint,
            channel_id=channel_id,
            status=status,
            status_code=status_code,
            latency_ms=latency_ms,
            total_tokens=total_tokens,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            llm_calls=llm_calls,
        )
        try:
            col = ExecutionLogService._collection()
            await col.insert_one(doc.model_dump(by_alias=True))
            return doc.id
        except Exception as exc:
            logger.warning("execution_log_write_failed", error=str(exc))
            return None

    # ── Stats (per-channel aggregation) ──

    @staticmethod
    async def get_stats(
        *,
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, Any]:
        """Aggregate execution stats grouped by source channel.

        Args:
            start: ISO datetime string (inclusive lower bound).
            end:   ISO datetime string (exclusive upper bound).

        Returns ``{"channels": {internal|api_key|im: {...}}, "totals": {...}}``.
        """
        match: dict[str, Any] = {}
        if start or end:
            rng: dict[str, Any] = {}
            try:
                if start:
                    rng["$gte"] = datetime.fromisoformat(start)
            except ValueError:
                pass
            try:
                if end:
                    rng["$lt"] = datetime.fromisoformat(end)
            except ValueError:
                pass
            if rng:
                match["timestamp"] = rng

        pipeline = [
            {"$match": match},
            {
                "$group": {
                    "_id": "$source",
                    "calls": {"$sum": 1},
                    "tokens": {"$sum": "$total_tokens"},
                    "input_tokens": {"$sum": "$input_tokens"},
                    "output_tokens": {"$sum": "$output_tokens"},
                    "llm_calls": {"$sum": "$llm_calls"},
                    "avg_latency_ms": {"$avg": "$latency_ms"},
                    "success": {"$sum": {"$cond": [{"$eq": ["$status", "success"]}, 1, 0]}},
                    "failed": {"$sum": {"$cond": [{"$ne": ["$status", "success"]}, 1, 0]}},
                }
            },
        ]
        col = ExecutionLogService._collection()
        rows = await col.aggregate(pipeline).to_list(length=10)

        channels = {
            CHANNEL_INTERNAL: _empty_stats(),
            CHANNEL_API_KEY: _empty_stats(),
            CHANNEL_IM: _empty_stats(),
        }
        for row in rows:
            name = row["_id"] or CHANNEL_INTERNAL
            if name not in channels:
                channels[name] = _empty_stats()
            channels[name]["calls"] = row.get("calls", 0)
            channels[name]["tokens"] = row.get("tokens", 0)
            channels[name]["input_tokens"] = row.get("input_tokens", 0)
            channels[name]["output_tokens"] = row.get("output_tokens", 0)
            channels[name]["llm_calls"] = row.get("llm_calls", 0)
            channels[name]["avg_latency_ms"] = round(row.get("avg_latency_ms") or 0, 0)
            channels[name]["success"] = row.get("success", 0)
            channels[name]["failed"] = row.get("failed", 0)

        # Totals across the three primary channels.
        totals = _empty_stats()
        for name in (CHANNEL_INTERNAL, CHANNEL_API_KEY, CHANNEL_IM):
            ch = channels[name]
            for k, v in ch.items():
                if isinstance(v, (int, float)):
                    totals[k] += v
        total_calls = totals["calls"]
        totals["success_rate"] = round(totals["success"] / total_calls * 100, 1) if total_calls else 0.0

        return {"channels": channels, "totals": totals}

    # ── List (paginated detail) ──

    @staticmethod
    async def list_logs(
        *,
        source: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        start: str | None = None,
        end: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[dict], int]:
        """Paginated execution-log detail query."""
        query: dict[str, Any] = {}
        if source:
            query["source"] = source
        if agent_id:
            query["agent_id"] = agent_id
        if session_id:
            query["session_id"] = session_id
        if start or end:
            rng: dict[str, Any] = {}
            try:
                if start:
                    rng["$gte"] = datetime.fromisoformat(start)
            except ValueError:
                pass
            try:
                if end:
                    rng["$lt"] = datetime.fromisoformat(end)
            except ValueError:
                pass
            if rng:
                query["timestamp"] = rng

        col = ExecutionLogService._collection()
        total = await col.count_documents(query)
        cursor = (
            col.find(query, {"_id": 0})
            .sort("timestamp", -1)
            .skip((page - 1) * page_size)
            .limit(page_size)
        )
        items = await cursor.to_list(length=page_size)
        for it in items:
            ts = it.get("timestamp")
            if hasattr(ts, "isoformat"):
                it["timestamp"] = ts.isoformat()
        await _enrich_caller_names(items)
        return items, total

    # ── API-Key-scoped queries (服务 API Keys 页面审计) ──

    @staticmethod
    async def list_logs_by_api_key(
        api_key_id: str,
        *,
        user_sub: str | None = None,
        visitor_id: str | None = None,
        session_id: str | None = None,
        endpoint: str | None = None,
        start: str | None = None,
        end: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[dict], int]:
        """Paginated log query scoped to one API Key."""
        query: dict[str, Any] = {"api_key_id": api_key_id, "source": CHANNEL_API_KEY}
        if user_sub:
            query["user_sub"] = user_sub
        if visitor_id:
            query["visitor_id"] = visitor_id
        if session_id:
            query["session_id"] = session_id
        if endpoint:
            query["endpoint"] = endpoint
        if start or end:
            ts = _time_range(start, end)
            if ts:
                query["timestamp"] = ts

        col = ExecutionLogService._collection()
        total = await col.count_documents(query)
        cursor = (
            col.find(query, {"_id": 0})
            .sort("timestamp", -1)
            .skip((page - 1) * page_size)
            .limit(page_size)
        )
        items = await cursor.to_list(length=page_size)
        for it in items:
            ts = it.get("timestamp")
            if hasattr(ts, "isoformat"):
                it["timestamp"] = ts.isoformat()
        await _enrich_caller_names(items)
        return items, total

    @staticmethod
    async def get_token_summary_by_api_key(
        api_key_id: str,
        *,
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, Any]:
        """Aggregate token totals for an API Key (optionally time-windowed)."""
        match: dict[str, Any] = {"api_key_id": api_key_id, "source": CHANNEL_API_KEY}
        if start or end:
            ts = _time_range(start, end)
            if ts:
                match["timestamp"] = ts

        pipeline = [
            {"$match": match},
            {
                "$group": {
                    "_id": None,
                    "total_tokens": {"$sum": "$total_tokens"},
                    "input_tokens": {"$sum": "$input_tokens"},
                    "output_tokens": {"$sum": "$output_tokens"},
                    "calls": {"$sum": 1},
                }
            },
        ]
        col = ExecutionLogService._collection()
        rows = await col.aggregate(pipeline).to_list(length=1)
        if not rows:
            return {"total_tokens": 0, "input_tokens": 0, "output_tokens": 0, "calls": 0}
        r = rows[0]
        return {
            "total_tokens": r.get("total_tokens", 0),
            "input_tokens": r.get("input_tokens", 0),
            "output_tokens": r.get("output_tokens", 0),
            "calls": r.get("calls", 0),
        }

    @staticmethod
    async def get_users_summary_by_api_key(
        api_key_id: str,
        *,
        period_days: int = 7,
    ) -> list[dict[str, Any]]:
        """Active end-users (by user_sub) ranked by token usage."""
        cutoff = datetime.now(UTC) - timedelta(days=period_days)
        pipeline = [
            {
                "$match": {
                    "api_key_id": api_key_id,
                    "source": CHANNEL_API_KEY,
                    "user_sub": {"$ne": ""},
                    "timestamp": {"$gte": cutoff},
                }
            },
            {
                "$group": {
                    "_id": "$user_sub",
                    "total_tokens": {"$sum": "$total_tokens"},
                    "calls": {"$sum": 1},
                    "last_seen": {"$max": "$timestamp"},
                }
            },
            {"$sort": {"total_tokens": -1}},
            {"$limit": 100},
        ]
        col = ExecutionLogService._collection()
        rows = await col.aggregate(pipeline).to_list(length=100)
        for r in rows:
            r["user_sub"] = r.pop("_id", "")
            ts = r.get("last_seen")
            r["last_seen_at"] = ts.isoformat() if hasattr(ts, "isoformat") else ""
        return rows


def _time_range(start: str | None, end: str | None) -> dict[str, Any] | None:
    """Build a MongoDB datetime range from ISO string bounds, or None."""
    rng: dict[str, Any] = {}
    try:
        if start:
            rng["$gte"] = datetime.fromisoformat(start)
    except ValueError:
        pass
    try:
        if end:
            rng["$lt"] = datetime.fromisoformat(end)
    except ValueError:
        pass
    return rng or None


def _empty_stats() -> dict[str, Any]:
    """Zeroed stats block for a channel."""
    return {
        "calls": 0,
        "tokens": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "llm_calls": 0,
        "avg_latency_ms": 0,
        "success": 0,
        "failed": 0,
    }


async def _enrich_caller_names(items: list[dict]) -> None:
    """Resolve a human-readable ``caller_name`` for each log item in place.

    Internal → users.username; api_key → api_keys.name (by api_key_id, then
    fall back to the owner's username); im → channels.name (by channel_id).
    Falls back to user_id when the lookup misses (deleted user/key/channel).
    """
    if not items:
        return
    db = get_database()

    # Collect ids to resolve, grouped by collection.
    internal_uids: set[str] = set()
    api_key_ids: set[str] = set()
    owner_uids: set[str] = set()
    channel_ids: set[str] = set()
    for it in items:
        src = it.get("source", "")
        uid = it.get("user_id", "")
        if src == CHANNEL_INTERNAL and uid:
            internal_uids.add(uid)
        elif src == CHANNEL_API_KEY:
            ak = it.get("api_key_id", "")
            if ak:
                api_key_ids.add(ak)
            # owner is the part before ':' in user_id
            if ":" in uid:
                owner_uids.add(uid.split(":", 1)[0])
        elif src == CHANNEL_IM:
            cid = it.get("channel_id", "")
            if cid:
                channel_ids.add(cid)

    name_maps: dict[str, dict[str, str]] = {"internal": {}, "apikey": {}, "owner": {}, "im": {}}

    # users (internal callers + api_key owners)
    user_ids = internal_uids | owner_uids
    if user_ids:
        async for doc in db["users"].find({"_id": {"$in": list(user_ids)}}, {"username": 1}):
            name_maps["internal"].setdefault(doc["_id"], doc.get("username", ""))

    # api_keys
    if api_key_ids:
        async for doc in db["api_keys"].find({"_id": {"$in": list(api_key_ids)}}, {"name": 1}):
            name_maps["apikey"][doc["_id"]] = doc.get("name", "")

    # channels
    if channel_ids:
        async for doc in db["channels"].find({"_id": {"$in": list(channel_ids)}}, {"name": 1}):
            name_maps["im"][doc["_id"]] = doc.get("name", "")

    for it in items:
        src = it.get("source", "")
        uid = it.get("user_id", "")
        if src == CHANNEL_INTERNAL:
            it["caller_name"] = name_maps["internal"].get(uid) or uid
        elif src == CHANNEL_API_KEY:
            ak = it.get("api_key_id", "")
            name = name_maps["apikey"].get(ak, "")
            if not name and ":" in uid:
                owner = uid.split(":", 1)[0]
                owner_name = name_maps["internal"].get(owner)
                name = f"{owner_name} 的 Key" if owner_name else uid
            it["caller_name"] = name or uid
        elif src == CHANNEL_IM:
            cid = it.get("channel_id", "")
            it["caller_name"] = name_maps["im"].get(cid) or uid
        else:
            it["caller_name"] = uid


execution_log_service = ExecutionLogService()
