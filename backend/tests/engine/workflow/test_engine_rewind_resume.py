"""Engine integration tests for the rewind feature.

These tests lock down the linchpin correctness claim of the rewind design
(``docs/superpowers/specs/2026-07-17-human-node-rewind-design.md`` §6.2):
after ``rewind_task`` trims ``target_node_id`` + its downstream from
``checkpoint.completed_nodes`` and clears ``checkpoint.human_context``,
``WorkflowEngine.resume_from_checkpoint`` must RE-EXECUTE the target node and
its whole downstream subgraph, while SKIPPING nodes that remain in
``completed_nodes``.

No engine code was changed for rewind — this works because clearing
``human_context`` (and ``agent_thread_id`` already being empty) makes
``resume_from_checkpoint`` take its "node-boundary cancel" branch
(engine.py:493-498), which re-executes ``paused_at_node`` (= the rewind
target) and recurses downstream via ``_execute_node``'s next_nodes handling,
skipping any node still in ``completed_nodes`` (engine.py:720).

``_execute_node`` is replaced with a fake that mimics the real node-routing
semantics (record the call, skip nodes in ``_completed_nodes``, recurse into
``config.next_nodes``). This lets us assert on the FULL re-execution chain
including deep downstream, without standing up real node executors.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.engine.workflow.engine import WorkflowEngine


def _workflow_doc():
    """Linear workflow: start → a → human (human has no downstream)."""
    return {
        "_id": "wf_test",
        "nodes": [
            {"node_id": "start", "type": "start", "config": {"next_nodes": [{"target": "a"}]}},
            {"node_id": "a", "type": "agent", "config": {"next_nodes": [{"target": "human"}]}},
            {"node_id": "human", "type": "human", "config": {"next_nodes": []}},
        ],
        "edges": [],
    }


def _task_doc_after_rewind_to_a():
    """Checkpoint shape AFTER rewind_task has trimmed target 'a' + downstream 'human'.

    paused_at_node='a' (the rewind target), completed_nodes=['start'] (a and
    human removed), human_context={} (cleared by rewind so the engine takes the
    re-execute branch), agent_thread_id='' (empty for a human-pause checkpoint).
    """
    return {
        "_id": "task_1",
        "workflow_id": "wf_test",
        "status": "running",
        "version": 6,
        "created_by": "user_1",
        "checkpoint": {
            "paused_at_node": "a",
            "completed_nodes": ["start"],
            "variable_snapshot": {
                "start": {"x": 1},
                "system": {"task_id": "task_1", "user_id": "user_1", "workflow_id": "wf_test"},
            },
            "human_context": {},  # <-- cleared by rewind → re-execute branch
            "agent_thread_id": "",  # <-- empty → not the agent-cancel branch
        },
    }


def _build_mock_db(task_doc, workflow_doc):
    """Build a mock motor db whose ['tasks'] and ['workflows'] return find_one docs."""
    tasks_col = MagicMock()
    tasks_col.find_one = AsyncMock(return_value=task_doc)
    tasks_col.update_one = AsyncMock()
    workflows_col = MagicMock()
    workflows_col.find_one = AsyncMock(return_value=workflow_doc)
    db = MagicMock()
    db.__getitem__ = lambda self, key: tasks_col if key == "tasks" else workflows_col
    return db


def _install_routing_fake(engine: WorkflowEngine) -> list[str]:
    """Replace engine._execute_node with a fake that mirrors real routing.

    The fake records every invocation in the returned list, skips nodes already
    in ``engine._completed_nodes`` (mirroring engine.py:720), and recurses into
    ``config.next_nodes`` (mirroring engine.py:913-924) so deep downstream is
    exercised. This isolates the resume-routing behavior under test from the
    concrete node executors.
    """
    executed: list[str] = []

    async def _fake_execute_node(node_id: str):
        # Mirror the real skip-completed guard (engine.py:720).
        if node_id in engine._completed_nodes:
            return None
        executed.append(node_id)
        # Mark complete so the same node isn't re-entered on a diamond/cycle.
        engine._completed_nodes.add(node_id)
        # Mirror the real next_nodes recursion (engine.py:913-924).
        node = engine._node_map.get(node_id) or {}
        for nxt in (node.get("config") or {}).get("next_nodes") or []:
            target = nxt.get("target") if isinstance(nxt, dict) else None
            if target:
                await _fake_execute_node(target)

    engine._execute_node = _fake_execute_node  # type: ignore[method-assign]
    return executed


class TestResumeAfterRewind:
    """Verify resume_from_checkpoint re-executes trimmed nodes after a rewind."""

    @pytest.mark.asyncio
    async def test_resume_reruns_target_and_downstream(self) -> None:
        """Rewind to 'a': resume must re-execute 'a' (target) and 'human' (its downstream).

        'start' is still in completed_nodes and must NOT be re-executed.
        """
        engine = WorkflowEngine()
        with (
            patch("app.db.mongodb.get_database", return_value=_build_mock_db(
                _task_doc_after_rewind_to_a(), _workflow_doc(),
            )),
            patch("app.engine.workflow.engine.TaskService.transition_task", AsyncMock()) as transition_mock,
        ):
            executed = _install_routing_fake(engine)
            await engine.resume_from_checkpoint("task_1")

        # 'a' (target) re-executed, then its downstream 'human' re-executed.
        # 'start' must be skipped (still in completed_nodes).
        assert "a" in executed, f"target 'a' not re-executed; got {executed}"
        assert "human" in executed, f"downstream 'human' not re-executed; got {executed}"
        assert "start" not in executed, f"untrimmed 'start' was re-executed; got {executed}"

        # Workflow reached the end (no more downstream after human) → COMPLETED.
        assert transition_mock.await_count >= 1
        from app.models.task import TaskStatus
        to_status = transition_mock.await_args.kwargs.get("to_status")
        assert to_status == TaskStatus.COMPLETED, f"expected COMPLETED, got {to_status}"

    @pytest.mark.asyncio
    async def test_resume_skips_nodes_still_in_completed(self) -> None:
        """Nodes remaining in completed_nodes after rewind are never re-executed.

        4-node chain start → a → b → human, rewind to 'a' (trim {a, b, human}).
        'start' stays completed and must be skipped; 'a', 'b', 'human' all re-run.
        """
        wf = {
            "_id": "wf_test",
            "nodes": [
                {"node_id": "start", "type": "start", "config": {"next_nodes": [{"target": "a"}]}},
                {"node_id": "a", "type": "agent", "config": {"next_nodes": [{"target": "b"}]}},
                {"node_id": "b", "type": "agent", "config": {"next_nodes": [{"target": "human"}]}},
                {"node_id": "human", "type": "human", "config": {"next_nodes": []}},
            ],
            "edges": [],
        }
        task = {
            "_id": "task_1", "workflow_id": "wf_test", "status": "running", "version": 6,
            "created_by": "user_1",
            "checkpoint": {
                "paused_at_node": "a",
                "completed_nodes": ["start"],  # a, b, human trimmed
                "variable_snapshot": {
                    "start": {},
                    "system": {"task_id": "task_1", "user_id": "user_1", "workflow_id": "wf_test"},
                },
                "human_context": {},
                "agent_thread_id": "",
            },
        }
        engine = WorkflowEngine()
        with (
            patch("app.db.mongodb.get_database", return_value=_build_mock_db(task, wf)),
            patch("app.engine.workflow.engine.TaskService.transition_task", AsyncMock()),
        ):
            executed = _install_routing_fake(engine)
            await engine.resume_from_checkpoint("task_1")

        assert "start" not in executed
        for expected in ("a", "b", "human"):
            assert expected in executed, f"{expected} not re-executed; got {executed}"

    @pytest.mark.asyncio
    async def test_resume_takes_reexecute_branch_when_human_context_empty(self) -> None:
        """The branch selection: empty human_context + empty agent_thread_id → re-execute.

        This is the precondition the rewind design relies on. If a future
        engine refactor changed the branch logic to the approve-branch (which
        marks paused_at_node complete and SKIPS its execution), this test would
        catch it: the target 'a' must actually be passed to _execute_node.
        """
        engine = WorkflowEngine()
        with (
            patch("app.db.mongodb.get_database", return_value=_build_mock_db(
                _task_doc_after_rewind_to_a(), _workflow_doc(),
            )),
            patch("app.engine.workflow.engine.TaskService.transition_task", AsyncMock()),
        ):
            executed = _install_routing_fake(engine)
            await engine.resume_from_checkpoint("task_1")

        # paused_at_node ('a') MUST have been executed. The approve-branch
        # (human_context non-empty) would instead add 'a' to completed_nodes
        # and skip it — so 'a' in `executed` proves the re-execute branch ran.
        assert "a" in executed, "paused_at_node was not executed — wrong resume branch taken"
