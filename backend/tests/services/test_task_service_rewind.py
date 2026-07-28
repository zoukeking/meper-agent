"""Tests for TaskService.rewind_task and _compute_downstream_nodes (human-node-rewind)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.services.task_service import TaskService


def _wf(*nodes):
    """Build a minimal workflow doc with the given nodes (node_id → list of next targets)."""
    return {
        "nodes": [
            {
                "node_id": nid,
                "type": "agent",
                "config": {"next_nodes": [{"target": t} for t in targets]},
            }
            for nid, targets in nodes
        ],
    }


class TestComputeDownstreamNodes:
    def test_linear_chain_returns_all_downstream(self):
        wf = _wf(("start", ["a"]), ("a", ["b"]), ("b", ["human"]), ("human", []))
        result = TaskService._compute_downstream_nodes(wf, "a")
        assert result == {"b", "human"}

    def test_includes_target_neighbors_but_not_target_itself(self):
        """Per spec: returns downstream *excluding* target itself; caller unions target in."""
        wf = _wf(("start", ["a"]), ("a", ["b"]), ("b", []))
        result = TaskService._compute_downstream_nodes(wf, "a")
        assert "a" not in result
        assert result == {"b"}

    def test_parallel_branches_all_collected(self):
        wf = _wf(
            ("a", ["p"]),
            ("p", ["b1", "b2"]),
            ("b1", ["join"]),
            ("b2", ["join"]),
            ("join", ["human"]),
            ("human", []),
        )
        result = TaskService._compute_downstream_nodes(wf, "p")
        assert result == {"b1", "b2", "join", "human"}

    def test_diamond_merges_back(self):
        wf = _wf(
            ("a", ["b", "c"]),
            ("b", ["d"]),
            ("c", ["d"]),
            ("d", ["human"]),
            ("human", []),
        )
        result = TaskService._compute_downstream_nodes(wf, "a")
        assert result == {"b", "c", "d", "human"}

    def test_cycle_does_not_loop_forever(self):
        """Validator forbids cycles, but the function must be defensive."""
        wf = _wf(("a", ["b"]), ("b", ["a"]), ("a2", []))
        result = TaskService._compute_downstream_nodes(wf, "a")
        # Even with a cycle, must terminate; both a's neighbours visited once.
        assert "b" in result

    def test_cycle_does_not_leak_target_into_result(self):
        wf = _wf(("a", ["b"]), ("b", ["a"]))
        result = TaskService._compute_downstream_nodes(wf, "a")
        assert "a" not in result
        assert result == {"b"}

    def test_unknown_next_target_skipped_safely(self):
        wf = _wf(("a", ["ghost"]), ("a2", []))  # 'ghost' node not defined
        result = TaskService._compute_downstream_nodes(wf, "a")
        assert result == set()

    def test_target_with_no_downstream_returns_empty(self):
        wf = _wf(("a", ["b"]), ("b", []))
        result = TaskService._compute_downstream_nodes(wf, "b")
        assert result == set()

    def test_target_not_in_workflow_returns_empty(self):
        wf = _wf(("a", ["b"]), ("b", []))
        result = TaskService._compute_downstream_nodes(wf, "nonexistent")
        assert result == set()


class TestRewindTask:
    """Tests for TaskService.rewind_task orchestration (success + variables)."""

    @pytest.fixture
    def linear_wf_doc(self):
        return {
            "_id": "wf_test",
            "nodes": [
                {"node_id": "start", "type": "start", "config": {"next_nodes": [{"target": "a"}]}},
                {"node_id": "a", "type": "agent", "config": {"next_nodes": [{"target": "human"}]}},
                {"node_id": "human", "type": "human", "config": {"next_nodes": []}},
            ],
        }

    @pytest.fixture
    def waiting_task_doc(self, linear_wf_doc):
        # Realistic checkpoint shape: the human pause node IS in completed_nodes
        # (engine.py:767 adds it before the checkpoint is saved at engine.py:806),
        # and its output is in variable_snapshot. Rewinding to 'a' trims
        # {a, human} (a's downstream is human), leaving ['start'].
        return {
            "_id": "task_1",
            "workflow_id": "wf_test",
            "status": "waiting_human",
            "version": 5,
            "variables": {"start": {"x": 1}, "a": {"out": "v1"}, "human": {"status": "waiting_human"}},
            "checkpoint": {
                "paused_at_node": "human",
                "completed_nodes": ["start", "a", "human"],
                "variable_snapshot": {
                    "start": {"x": 1},
                    "a": {"out": "v1"},
                    "human": {"status": "waiting_human"},
                },
                "human_context": {"node_id": "human", "title": "审"},
                "agent_thread_id": "",
            },
            "timeline": [],
            "variable_snapshots": [],
        }

    def test_rewind_trims_target_and_downstream_and_reruns(self, linear_wf_doc, waiting_task_doc):
        """Rewind to 'a': completed_nodes loses 'a' and its downstream 'human', paused→a, human_context cleared."""
        find_one = AsyncMock(side_effect=[waiting_task_doc, linear_wf_doc])
        find_one_and_update = AsyncMock(return_value={
            **waiting_task_doc,
            "status": "running",
            "version": 6,
            "checkpoint": {
                "paused_at_node": "a",
                "completed_nodes": ["start"],
                "variable_snapshot": {"start": {"x": 1}},
                "human_context": {},
            },
        })
        with (
            patch("app.services.task_service.get_database", MagicMock(return_value=AsyncMock(
                __getitem__=lambda self, k: _FakeColl(find_one, find_one_and_update),
            ))),
            patch.object(TaskService, "_write_audit_log", AsyncMock()),
            patch.object(TaskService, "resume_task_execution") as resume_mock,
        ):
            import asyncio
            asyncio.run(
                TaskService.rewind_task(
                    task_id="task_1",
                    target_node_id="a",
                    variables=None,
                    comment="回退重跑",
                    triggered_by="user_1",
                    version=5,
                )
            )

        # checkpoint trimmed: paused_at_node='a', 'a' and downstream 'human'
        # removed from completed_nodes, leaving only ['start'].
        update_call = find_one_and_update.await_args
        set_ops = update_call.kwargs.get("update", update_call.args[-1] if update_call.args else {})["$set"]
        assert set_ops["status"] == "running"
        assert set_ops["version"] == 6
        assert set_ops["checkpoint.paused_at_node"] == "a"
        assert set_ops["checkpoint.completed_nodes"] == ["start"]
        assert set_ops["checkpoint.human_context"] == {}
        # 'a' and its downstream 'human' outputs removed from variable_snapshot
        assert "a" not in set_ops["checkpoint.variable_snapshot"]
        assert "human" not in set_ops["checkpoint.variable_snapshot"]
        assert "start" in set_ops["checkpoint.variable_snapshot"]  # upstream preserved
        # rewoun timeline event pushed
        push_ops = update_call.kwargs.get("update", update_call.args[-1] if update_call.args else {})["$push"]
        tl_entry = push_ops["timeline"]
        assert tl_entry["event_type"] == "rewoun"
        assert tl_entry["data"]["node_id"] == "a"
        assert "a" in tl_entry["data"]["rewound_nodes"]
        # resume triggered
        assert resume_mock.call_count == 1

    def test_rewind_clears_checkpointer_threads_for_trimmed_nodes(
        self, linear_wf_doc, waiting_task_doc,
    ) -> None:
        """rewind must clear LangGraph checkpointer threads for trimmed nodes.

        Regression guard for "Received multiple non-consecutive system messages":
        without cleanup, re-executing an agent node reuses its stale thread
        ({task_id}_{node_id}) and the add_messages reducer appends a fresh
        SystemMessage after the old history → non-consecutive system messages.
        retry fixes this via _cleanup_task_artifacts; rewind must do the same
        for the trimmed (target + downstream) nodes only — upstream nodes keep
        their threads.
        """
        find_one = AsyncMock(side_effect=[waiting_task_doc, linear_wf_doc])
        find_one_and_update = AsyncMock(return_value={
            **waiting_task_doc, "status": "running", "version": 6,
            "checkpoint": {"paused_at_node": "a", "completed_nodes": ["start"]},
        })
        cp_col = MagicMock()
        cp_col.delete_many = MagicMock()
        writes_col = MagicMock()
        writes_col.delete_many = MagicMock()
        fake_checkpointer = MagicMock(
            checkpoint_collection=cp_col,
            writes_collection=writes_col,
        )
        with (
            patch("app.services.task_service.get_database", MagicMock(return_value=AsyncMock(
                __getitem__=lambda self, k: _FakeColl(find_one, find_one_and_update),
            ))),
            patch("app.engine.harness_integration.get_checkpointer", return_value=fake_checkpointer),
            patch.object(TaskService, "_write_audit_log", AsyncMock()),
            patch.object(TaskService, "resume_task_execution"),
        ):
            import asyncio
            asyncio.run(
                TaskService.rewind_task(
                    task_id="task_1",
                    target_node_id="a",
                    variables=None,
                    comment=None,
                    triggered_by="user_1",
                    version=5,
                )
            )

        # Both checkpointer collections were cleaned. trim_set = {a, human}
        # (target + downstream). The cleanup must target those threads.
        assert cp_col.delete_many.called, "checkpoint_collection.delete_many not called"
        assert writes_col.delete_many.called, "writes_collection.delete_many not called"
        cp_filter = cp_col.delete_many.call_args.args[0]
        # thread_ids for trimmed nodes must be present (task_1_a, task_1_human)
        deleted_threads = cp_filter["thread_id"]["$in"]
        assert "task_1_a" in deleted_threads, f"target thread missing: {deleted_threads}"
        assert "task_1_human" in deleted_threads, f"downstream thread missing: {deleted_threads}"
        # upstream node 'start' is NOT trimmed — its thread must NOT be deleted
        assert "task_1_start" not in deleted_threads, f"upstream thread wrongly deleted: {deleted_threads}"


        """Providing variables merges into pool and pushes a variable_snapshots entry."""
        find_one = AsyncMock(side_effect=[waiting_task_doc, linear_wf_doc])
        find_one_and_update = AsyncMock(return_value={**waiting_task_doc, "status": "running", "version": 6})
        with (
            patch("app.services.task_service.get_database", MagicMock(return_value=AsyncMock(
                __getitem__=lambda self, k: _FakeColl(find_one, find_one_and_update),
            ))),
            patch.object(TaskService, "_write_audit_log", AsyncMock()),
            patch.object(TaskService, "resume_task_execution"),
        ):
            import asyncio
            asyncio.run(
                TaskService.rewind_task(
                    task_id="task_1",
                    target_node_id="a",
                    variables={"start": {"x": 999}},
                    comment=None,
                    triggered_by="user_1",
                    version=5,
                )
            )

        update_call = find_one_and_update.await_args
        set_ops = update_call.kwargs.get("update", update_call.args[-1] if update_call.args else {})["$set"]
        # variables.start.x overridden to 999
        assert set_ops["variables.start"] == {"x": 999}
        # Fix 1 regression guard: override must also land in checkpoint.variable_snapshot
        # so resume (which reads only the snapshot) picks it up.
        assert set_ops["checkpoint.variable_snapshot"]["start"] == {"x": 999}
        # variable_snapshots pushed
        push_ops = update_call.kwargs.get("update", update_call.args[-1] if update_call.args else {})["$push"]
        snap = push_ops["variable_snapshots"]
        assert snap["reason"] == "rewind to a"
        assert snap["triggered_by"] == "user_1"
        assert snap["variables"] == {"start": {"x": 999}}
        # timeline records overridden keys
        tl_entry = push_ops["timeline"]
        assert "start" in tl_entry["data"]["variables_overridden"]

    def test_rewind_rejects_when_not_waiting_human(self, linear_wf_doc):
        """status != waiting_human → ConflictError mentioning waiting_human."""
        import asyncio

        from app.core.errors import ConflictError

        task_doc = {
            "_id": "task_1", "workflow_id": "wf_test", "status": "running",
            "version": 5, "checkpoint": {"paused_at_node": "human", "completed_nodes": ["a"]},
        }
        find_one = AsyncMock(side_effect=[task_doc])
        with (
            patch("app.services.task_service.get_database", MagicMock(return_value=AsyncMock(
                __getitem__=lambda self, k: _FakeColl(find_one, AsyncMock()),
            ))),
            pytest.raises(ConflictError, match="waiting_human"),
        ):
            asyncio.run(
                TaskService.rewind_task("task_1", "a", None, None, "u", 5)
            )

    def test_rewind_rejects_when_no_checkpoint(self, linear_wf_doc):
        """status=waiting_human but checkpoint=None → ConflictError mentioning checkpoint."""
        import asyncio

        from app.core.errors import ConflictError

        task_doc = {
            "_id": "task_1", "workflow_id": "wf_test", "status": "waiting_human",
            "version": 5, "checkpoint": None,
        }
        find_one = AsyncMock(side_effect=[task_doc])
        with (
            patch("app.services.task_service.get_database", MagicMock(return_value=AsyncMock(
                __getitem__=lambda self, k: _FakeColl(find_one, AsyncMock()),
            ))),
            pytest.raises(ConflictError, match="checkpoint"),
        ):
            asyncio.run(
                TaskService.rewind_task("task_1", "a", None, None, "u", 5)
            )

    def test_rewind_rejects_empty_target(self, waiting_task_doc, linear_wf_doc):
        """target_node_id='' → ValidationError mentioning target_node_id."""
        import asyncio

        from app.core.errors import ValidationError

        find_one = AsyncMock(side_effect=[waiting_task_doc])
        with (
            patch("app.services.task_service.get_database", MagicMock(return_value=AsyncMock(
                __getitem__=lambda self, k: _FakeColl(find_one, AsyncMock()),
            ))),
            pytest.raises(ValidationError, match="target_node_id"),
        ):
            asyncio.run(
                TaskService.rewind_task("task_1", "", None, None, "u", 5)
            )

    def test_rewind_rejects_target_not_executed(self, waiting_task_doc, linear_wf_doc):
        """target not in completed_nodes → ValidationError mentioning 未执行过."""
        import asyncio

        from app.core.errors import ValidationError

        find_one = AsyncMock(side_effect=[waiting_task_doc, linear_wf_doc])
        with (
            patch("app.services.task_service.get_database", MagicMock(return_value=AsyncMock(
                __getitem__=lambda self, k: _FakeColl(find_one, AsyncMock()),
            ))),
            pytest.raises(ValidationError, match="未执行过"),
        ):
            asyncio.run(
                TaskService.rewind_task("task_1", "nonexistent_node", None, None, "u", 5)
            )

    def test_rewind_rejects_target_equals_paused(self, waiting_task_doc, linear_wf_doc):
        """target == paused_at_node (=='human') → ValidationError mentioning 当前暂停."""
        import asyncio

        from app.core.errors import ValidationError

        find_one = AsyncMock(side_effect=[waiting_task_doc, linear_wf_doc])
        with (
            patch("app.services.task_service.get_database", MagicMock(return_value=AsyncMock(
                __getitem__=lambda self, k: _FakeColl(find_one, AsyncMock()),
            ))),
            pytest.raises(ValidationError, match="当前暂停"),
        ):
            asyncio.run(
                TaskService.rewind_task("task_1", "human", None, None, "u", 5)
            )

    def test_rewind_rejects_on_version_conflict(self, waiting_task_doc, linear_wf_doc):
        """find_one_and_update returns None (version mismatch) → ConflictError mentioning 状态已变更."""
        import asyncio

        from app.core.errors import ConflictError

        find_one = AsyncMock(side_effect=[waiting_task_doc, linear_wf_doc])
        find_one_and_update = AsyncMock(return_value=None)  # version mismatch
        with (
            patch("app.services.task_service.get_database", MagicMock(return_value=AsyncMock(
                __getitem__=lambda self, k: _FakeColl(find_one, find_one_and_update),
            ))),
            patch.object(TaskService, "_write_audit_log", AsyncMock()),
            pytest.raises(ConflictError, match="状态已变更"),
        ):
            asyncio.run(
                TaskService.rewind_task("task_1", "a", None, None, "u", 5)
            )


class _FakeColl:
    """Minimal fake MongoDB collection capturing find_one / find_one_and_update."""
    def __init__(self, find_one, find_one_and_update):
        self.find_one = find_one
        self.find_one_and_update = find_one_and_update
