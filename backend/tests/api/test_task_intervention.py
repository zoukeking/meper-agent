"""Tests for Task intervention variables write behavior (spec-human-node-approval)."""
from unittest.mock import AsyncMock, patch

import pytest
from app.api.v1.tasks import _sanitize_node_id
from app.core.security import create_access_token
from app.schemas.user import UserResponse, UserStatus
from fastapi.testclient import TestClient

TASK_ID = "task_01HAPPROVAL"
HUMAN_NODE_ID = "node_approval_1"
USER_ID = "user_01HAPPROVER"
WORKFLOW_ID = "wf_01HWF"
HUMAN_NODE_ID_SPECIAL = "审批-质检.5"


@pytest.fixture
def auth_token() -> str:
    return create_access_token(subject=USER_ID, claims={"role": "developer"})


@pytest.fixture
def current_user() -> UserResponse:
    return UserResponse(
        id=USER_ID,
        username="approver",
        email="approver@example.com",
        role="developer",
        status=UserStatus.ACTIVE,
        created_at="2026-06-01T00:00:00",
        updated_at="2026-06-01T00:00:00",
    )


def _override_auth(user: UserResponse):

    async def _fake():
        return user

    return _fake


def _make_task_doc(*, node_id: str = HUMAN_NODE_ID, version: int = 3, status: str = "waiting_human"):
    return {
        "_id": TASK_ID,
        "workflow_id": WORKFLOW_ID,
        "status": status,
        "version": version,
        "variables": {},
        "checkpoint": {"paused_at_node": node_id},
        "created_at": "2026-06-01T00:00:00",
        "updated_at": "2026-06-01T00:00:00",
    }


def _build_client(current_user: UserResponse):
    from app.api.v1 import tasks as tasks_module
    from app.core.security import get_current_user
    from app.main import app

    app.dependency_overrides[get_current_user] = _override_auth(current_user)
    return TestClient(app), app, tasks_module


def _post_intervene(client: TestClient, body: dict) -> tuple[int, dict]:
    resp = client.post(f"/api/v1/tasks/{TASK_ID}/intervene", json=body)
    return resp.status_code, resp.json()


def test_intervene_approve_writes_human_decision_to_variables(auth_token: str, current_user: UserResponse) -> None:
    """Approve writes {decision, comment, approver, decided_at} to human_decision_<sanitized_id>."""
    client, app, tasks_module = _build_client(current_user)
    try:
        task_doc = _make_task_doc()
        updated_doc = {**task_doc, "status": "running", "version": task_doc["version"] + 1}

        update_variables_mock = AsyncMock(return_value=updated_doc)
        with (
            patch.object(tasks_module.TaskService, "get_task_or_404", AsyncMock(return_value=task_doc)),
            patch.object(tasks_module.TaskService, "transition_task", AsyncMock(return_value=updated_doc)),
            patch.object(tasks_module.TaskService, "update_variables", update_variables_mock),
            patch.object(tasks_module.TaskService, "resume_task_execution"),
        ):
            status_code, payload = _post_intervene(
                client,
                {"action": "approve", "comment": "数据已确认", "version": task_doc["version"]},
            )

        assert status_code == 200, payload
        assert update_variables_mock.await_count == 1
        call_kwargs = update_variables_mock.await_args.kwargs
        variables = call_kwargs["variables"]
        expected_key = f"human_decision_{_sanitize_node_id(HUMAN_NODE_ID)}"
        assert expected_key in variables, f"Missing key {expected_key} in {variables}"
        decision = variables[expected_key]
        assert decision["decision"] == "approve"
        assert decision["comment"] == "数据已确认"
        assert decision["approver"] == USER_ID
        assert "decided_at" in decision and decision["decided_at"]
    finally:
        app.dependency_overrides.clear()


def test_intervene_reject_writes_comment_to_variables(auth_token: str, current_user: UserResponse) -> None:
    """Reject writes same structure with decision == 'reject'."""
    client, app, tasks_module = _build_client(current_user)
    try:
        task_doc = _make_task_doc()
        updated_doc = {**task_doc, "status": "failed", "version": task_doc["version"] + 1}

        update_variables_mock = AsyncMock(return_value=updated_doc)
        with (
            patch.object(tasks_module.TaskService, "get_task_or_404", AsyncMock(return_value=task_doc)),
            patch.object(tasks_module.TaskService, "transition_task", AsyncMock(return_value=updated_doc)),
            patch.object(tasks_module.TaskService, "update_variables", update_variables_mock),
        ):
            status_code, payload = _post_intervene(
                client,
                {"action": "reject", "comment": "质检不通过", "version": task_doc["version"]},
            )

        assert status_code == 200, payload
        assert update_variables_mock.await_count == 1
        variables = update_variables_mock.await_args.kwargs["variables"]
        expected_key = f"human_decision_{_sanitize_node_id(HUMAN_NODE_ID)}"
        decision = variables[expected_key]
        assert decision["decision"] == "reject"
        assert decision["comment"] == "质检不通过"
        assert decision["approver"] == USER_ID
        assert "decided_at" in decision and decision["decided_at"]
    finally:
        app.dependency_overrides.clear()


def test_intervene_with_empty_comment_writes_empty_string(auth_token: str, current_user: UserResponse) -> None:
    """Without comment, variables['comment'] == '' (not None)."""
    client, app, tasks_module = _build_client(current_user)
    try:
        task_doc = _make_task_doc()
        updated_doc = {**task_doc, "status": "running", "version": task_doc["version"] + 1}

        update_variables_mock = AsyncMock(return_value=updated_doc)
        with (
            patch.object(tasks_module.TaskService, "get_task_or_404", AsyncMock(return_value=task_doc)),
            patch.object(tasks_module.TaskService, "transition_task", AsyncMock(return_value=updated_doc)),
            patch.object(tasks_module.TaskService, "update_variables", update_variables_mock),
            patch.object(tasks_module.TaskService, "resume_task_execution"),
        ):
            status_code, payload = _post_intervene(
                client,
                {"action": "approve", "version": task_doc["version"]},
            )

        assert status_code == 200, payload
        variables = update_variables_mock.await_args.kwargs["variables"]
        decision = variables[f"human_decision_{_sanitize_node_id(HUMAN_NODE_ID)}"]
        assert decision["comment"] == ""
        assert decision["comment"] is not None
    finally:
        app.dependency_overrides.clear()


def test_intervene_rejects_when_task_not_waiting_human(auth_token: str, current_user: UserResponse) -> None:
    """Patch from review: 状态前置校验。Task 不在 WAITING_HUMAN 时拒绝审批。"""
    client, app, tasks_module = _build_client(current_user)
    try:
        task_doc = _make_task_doc(status="running")  # 已非 waiting_human
        update_variables_mock = AsyncMock()
        with (
            patch.object(tasks_module.TaskService, "get_task_or_404", AsyncMock(return_value=task_doc)),
            patch.object(tasks_module.TaskService, "transition_task", AsyncMock()),
            patch.object(tasks_module.TaskService, "update_variables", update_variables_mock),
        ):
            status_code, payload = _post_intervene(
                client,
                {"action": "approve", "comment": "test", "version": task_doc["version"]},
            )

        # 4xx 拒绝 + 不写 variables
        assert status_code in (400, 409, 422), payload
        assert update_variables_mock.await_count == 0
    finally:
        app.dependency_overrides.clear()


def test_sanitize_node_id_is_collision_resistant() -> None:
    """Patch from review: 不同 node_id sanitize 后必须不同，避免决策数据静默覆盖。"""
    # 这些原版 sanitize 后都是同一个 key '__5'，新实现必须产生不同 key
    candidates = ["审批-质检.5", "审批_质检_5", "审批.质检.5", "审批/质检@5", "审批!质检#5"]
    keys = [_sanitize_node_id(c) for c in candidates]
    assert len(set(keys)) == len(candidates), f"Collision detected: {dict(zip(candidates, keys, strict=True))}"
    # 同时保留原 node_id 的人类可读部分
    for key in keys:
        assert "5" in key, f"sanitize lost content: {key}"


def test_intervene_reject_uses_comment_in_error_message(auth_token: str, current_user: UserResponse) -> None:
    """Patch from review: reject 路径的 error_message 必须用 comment 而非 reason 字段。"""
    client, app, tasks_module = _build_client(current_user)
    try:
        task_doc = _make_task_doc()
        updated_doc = {**task_doc, "status": "failed", "version": task_doc["version"] + 1}

        transition_mock = AsyncMock(return_value=updated_doc)
        with (
            patch.object(tasks_module.TaskService, "get_task_or_404", AsyncMock(return_value=task_doc)),
            patch.object(tasks_module.TaskService, "transition_task", transition_mock),
            patch.object(tasks_module.TaskService, "update_variables", AsyncMock(return_value=updated_doc)),
        ):
            status_code, payload = _post_intervene(
                client,
                {"action": "reject", "comment": "数据不达标", "version": task_doc["version"]},
            )

        assert status_code == 200, payload
        # 检查 transition_task 收到的 error_info 包含 comment 而非 "无原因"
        transition_kwargs = transition_mock.await_args.kwargs
        error_info = transition_kwargs.get("error_info", {})
        assert "数据不达标" in error_info.get("error_message", ""), (
            f"error_message should contain comment, got: {error_info}"
        )
        assert "无原因" not in error_info.get("error_message", ""), (
            f"error_message should not default to '无原因' when comment is given: {error_info}"
        )
    finally:
        app.dependency_overrides.clear()


# ── comment 结构化（type: text | json）测试 ──


def test_intervene_approve_comment_text_type_stores_string(current_user: UserResponse) -> None:
    """{type:'text', value:...} 归一化后 variables['comment'] 是纯字符串。"""
    client, app, tasks_module = _build_client(current_user)
    try:
        task_doc = _make_task_doc()
        updated_doc = {**task_doc, "status": "running", "version": task_doc["version"] + 1}
        update_variables_mock = AsyncMock(return_value=updated_doc)
        with (
            patch.object(tasks_module.TaskService, "get_task_or_404", AsyncMock(return_value=task_doc)),
            patch.object(tasks_module.TaskService, "transition_task", AsyncMock(return_value=updated_doc)),
            patch.object(tasks_module.TaskService, "update_variables", update_variables_mock),
            patch.object(tasks_module.TaskService, "resume_task_execution"),
        ):
            status_code, payload = _post_intervene(
                client,
                {"action": "approve", "comment": {"type": "text", "value": "确认通过"}, "version": task_doc["version"]},
            )

        assert status_code == 200, payload
        decision = update_variables_mock.await_args.kwargs["variables"][
            f"human_decision_{_sanitize_node_id(HUMAN_NODE_ID)}"
        ]
        assert decision["comment"] == "确认通过"
        assert isinstance(decision["comment"], str)
    finally:
        app.dependency_overrides.clear()


def test_intervene_approve_comment_json_type_stores_object(current_user: UserResponse) -> None:
    """{type:'json', value:{...}} 归一化后 variables['comment'] 是 dict，下游可钻取。"""
    client, app, tasks_module = _build_client(current_user)
    try:
        task_doc = _make_task_doc()
        updated_doc = {**task_doc, "status": "running", "version": task_doc["version"] + 1}
        update_variables_mock = AsyncMock(return_value=updated_doc)
        with (
            patch.object(tasks_module.TaskService, "get_task_or_404", AsyncMock(return_value=task_doc)),
            patch.object(tasks_module.TaskService, "transition_task", AsyncMock(return_value=updated_doc)),
            patch.object(tasks_module.TaskService, "update_variables", update_variables_mock),
            patch.object(tasks_module.TaskService, "resume_task_execution"),
        ):
            status_code, payload = _post_intervene(
                client,
                {
                    "action": "approve",
                    "comment": {"type": "json", "value": {"score": 8, "note": "ok"}},
                    "version": task_doc["version"],
                },
            )

        assert status_code == 200, payload
        decision = update_variables_mock.await_args.kwargs["variables"][
            f"human_decision_{_sanitize_node_id(HUMAN_NODE_ID)}"
        ]
        # variables 里 comment 存的是 dict（值本身），不是包装结构
        assert decision["comment"] == {"score": 8, "note": "ok"}
        assert isinstance(decision["comment"], dict)
        # 验证下游 ExpressionEngine 可钻取：{{node.comment.score}} == 8
        from app.engine.workflow.expression import ExpressionEngine

        engine = ExpressionEngine({"comment": decision["comment"]})
        assert engine.resolve("{{ comment.score }}") == 8
    finally:
        app.dependency_overrides.clear()


def test_intervene_comment_plain_string_back_compat(current_user: UserResponse) -> None:
    """裸字符串 comment（老用法）归一化后仍是原字符串，向后兼容。"""
    client, app, tasks_module = _build_client(current_user)
    try:
        task_doc = _make_task_doc()
        updated_doc = {**task_doc, "status": "running", "version": task_doc["version"] + 1}
        update_variables_mock = AsyncMock(return_value=updated_doc)
        with (
            patch.object(tasks_module.TaskService, "get_task_or_404", AsyncMock(return_value=task_doc)),
            patch.object(tasks_module.TaskService, "transition_task", AsyncMock(return_value=updated_doc)),
            patch.object(tasks_module.TaskService, "update_variables", update_variables_mock),
            patch.object(tasks_module.TaskService, "resume_task_execution"),
        ):
            _post_intervene(
                client,
                {"action": "approve", "comment": "裸字符串文本", "version": task_doc["version"]},
            )

        decision = update_variables_mock.await_args.kwargs["variables"][
            f"human_decision_{_sanitize_node_id(HUMAN_NODE_ID)}"
        ]
        assert decision["comment"] == "裸字符串文本"
    finally:
        app.dependency_overrides.clear()


def test_intervene_reject_json_comment_renders_in_error_message(current_user: UserResponse) -> None:
    """reject 路径：json 类型 comment 在 error_message 里渲染为 JSON 文本。"""
    client, app, tasks_module = _build_client(current_user)
    try:
        task_doc = _make_task_doc()
        updated_doc = {**task_doc, "status": "failed", "version": task_doc["version"] + 1}
        transition_mock = AsyncMock(return_value=updated_doc)
        with (
            patch.object(tasks_module.TaskService, "get_task_or_404", AsyncMock(return_value=task_doc)),
            patch.object(tasks_module.TaskService, "transition_task", transition_mock),
            patch.object(tasks_module.TaskService, "update_variables", AsyncMock(return_value=updated_doc)),
        ):
            status_code, payload = _post_intervene(
                client,
                {
                    "action": "reject",
                    "comment": {"type": "json", "value": {"reason": "score_too_low", "min": 60}},
                    "version": task_doc["version"],
                },
            )

        assert status_code == 200, payload
        error_info = transition_mock.await_args.kwargs.get("error_info", {})
        msg = error_info.get("error_message", "")
        assert "score_too_low" in msg, f"json comment 未渲染进 error_message: {msg}"
        assert "60" in msg, f"json comment 未渲染进 error_message: {msg}"
    finally:
        app.dependency_overrides.clear()


def test_task_intervene_schema_accepts_rewind_action() -> None:
    """Schema must accept action='rewind' and optional target_node_id/variables."""
    from app.schemas.task import TaskIntervene

    body = TaskIntervene(
        action="rewind",
        version=3,
        target_node_id="node_a",
        variables={"input": {"q": "hi"}},
    )
    assert body.action == "rewind"
    assert body.target_node_id == "node_a"
    assert body.variables == {"input": {"q": "hi"}}


def test_task_intervene_schema_rejects_unknown_action() -> None:
    """Unknown action must fail Pydantic validation."""
    from app.schemas.task import TaskIntervene
    from pydantic import ValidationError as PydanticValidationError

    with pytest.raises(PydanticValidationError):
        TaskIntervene(action="bogus", version=1)


def test_task_intervene_schema_target_and_variables_optional() -> None:
    """For non-rewind actions, target_node_id/variables remain optional (None)."""
    from app.schemas.task import TaskIntervene

    body = TaskIntervene(action="approve", version=1)
    assert body.target_node_id is None
    assert body.variables is None


def test_intervene_rewind_calls_service_and_returns_running(
    auth_token: str, current_user: UserResponse,
) -> None:
    """POST intervene action=rewind delegates to TaskService.rewind_task, returns running."""
    client, app, tasks_module = _build_client(current_user)
    try:
        task_doc = _make_task_doc()
        rewound_doc = {**task_doc, "status": "running", "version": task_doc["version"] + 1}
        with (
            patch.object(tasks_module.TaskService, "get_task_or_404", AsyncMock(return_value=task_doc)),
            patch.object(tasks_module.TaskService, "rewind_task", AsyncMock(return_value=rewound_doc)) as rewind_mock,
        ):
            status_code, payload = _post_intervene(
                client,
                {
                    "action": "rewind",
                    "target_node_id": "node_a",
                    "variables": {"input": {"q": "new"}},
                    "comment": "回退修改",
                    "version": task_doc["version"],
                },
            )

        assert status_code == 200, payload
        assert payload["status"] == "running"
        assert payload["version"] == task_doc["version"] + 1
        # rewind_task called with right args
        assert rewind_mock.await_count == 1
        kwargs = rewind_mock.await_args.kwargs
        assert kwargs["task_id"] == TASK_ID
        assert kwargs["target_node_id"] == "node_a"
        assert kwargs["variables"] == {"input": {"q": "new"}}
        assert kwargs["triggered_by"] == USER_ID
        assert kwargs["version"] == task_doc["version"]
    finally:
        app.dependency_overrides.clear()


def test_intervene_rewind_propagates_validation_error(
    auth_token: str, current_user: UserResponse,
) -> None:
    """When rewind_task raises ValidationError (422), API returns 422 via ExceptionMiddleware."""
    from app.core.errors import ValidationError as AppValidationError

    client, app, tasks_module = _build_client(current_user)
    try:
        task_doc = _make_task_doc()
        with (
            patch.object(tasks_module.TaskService, "get_task_or_404", AsyncMock(return_value=task_doc)),
            patch.object(
                tasks_module.TaskService,
                "rewind_task",
                AsyncMock(side_effect=AppValidationError(
                    code="REWIND_TARGET_NOT_EXECUTED",
                    message="目标节点 node_x 未执行过，无法回退",
                )),
            ),
        ):
            status_code, payload = _post_intervene(
                client,
                {"action": "rewind", "target_node_id": "node_x", "version": task_doc["version"]},
            )

        assert status_code == 422
        assert "未执行过" in str(payload)
    finally:
        app.dependency_overrides.clear()


def test_intervene_rewind_without_variables_works(
    auth_token: str, current_user: UserResponse,
) -> None:
    """rewind with no variables field — minimal closed loop (pure rerun)."""
    client, app, tasks_module = _build_client(current_user)
    try:
        task_doc = _make_task_doc()
        rewound_doc = {**task_doc, "status": "running", "version": task_doc["version"] + 1}
        with (
            patch.object(tasks_module.TaskService, "get_task_or_404", AsyncMock(return_value=task_doc)),
            patch.object(tasks_module.TaskService, "rewind_task", AsyncMock(return_value=rewound_doc)) as rewind_mock,
        ):
            status_code, payload = _post_intervene(
                client,
                {"action": "rewind", "target_node_id": "node_a", "version": task_doc["version"]},
            )

        assert status_code == 200, payload
        kwargs = rewind_mock.await_args.kwargs
        assert kwargs["variables"] is None
    finally:
        app.dependency_overrides.clear()
