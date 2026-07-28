"""ask_clarification 工具 — HITL 追问，直调 langgraph interrupt()。

三层工具模型第一层。工具函数内部调 interrupt(payload) 挂起 graph；
宿主收到 interrupt 事件，收集用户答案后 resume(Command(resume=answer))；
interrupt() 返回 answer，工具把它返回给 LLM。

支持两种模式：
- 单问题模式：提供 question/options，行为与历史一致。
- 向导模式：提供 fields（一组按序的待澄清问题），一次 interrupt 收齐所有
  字段，避免 LLM 多轮反复追问。用户在前端逐题作答（每题可选推荐方案或自行
  输入），全部答完后答案以 JSON 字符串回传，如
  ``{"audience":"技术人员","format":"Markdown"}``。

依赖 react_node 放行 GraphInterrupt（v0.2-x 前提修正），否则 interrupt
会被工具执行的 except Exception 吞掉。
"""
from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, BeforeValidator, Field


ClarificationType = Literal[
    "missing_info",
    "ambiguous_requirement",
    "approach_choice",
    "risk_confirmation",
    "suggestion",
]

# 表单字段支持的值类型。
FieldType = Literal["text", "number", "boolean", "select"]


def _coerce_options(v: Any) -> Any:
    """LLM 有时把 list 传成 JSON 字符串,自动解析。"""
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return v


def _coerce_fields(v: Any) -> Any:
    """LLM 有时把 fields 传成 JSON 字符串，自动解析为 list[dict]。"""
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return v


class ClarificationField(BaseModel):
    """向导模式下的单个字段（一个待澄清问题）定义。

    LLM 在 fields 列表中按提问顺序列出每个字段；用户在向导中逐题作答，
    全部答完后答案以 ``{name: value}`` 的 JSON 字符串形式聚合回传。

    展示规则（前端按此渲染）：
    - 提供了 ``options`` → 显示为「3-5 个推荐选项按钮 + 底部自由输入框」，
      用户可点选推荐或自行输入。
    - 未提供 ``options``（如密码、自由文本）→ 只显示输入框（密码字段可掩码）。
    """

    name: str = Field(..., description="字段标识，如 audience；答案以此 key 回传")
    label: str = Field(..., description="给用户看的字段名/问题文案")
    field_type: FieldType = Field(
        default="text", description="字段类型: text/number/boolean/select"
    )
    required: bool = Field(default=True, description="是否必填")
    options: list[str] | None = Field(
        default=None,
        description=(
            "推荐选项列表（建议 3-5 个）。提供时显示为推荐方案供点选，"
            "并附底部自由输入框；未提供时（如密码、纯自由输入字段）只显示输入框。"
        ),
    )
    default: str | int | float | bool | None = Field(
        default=None, description="默认值"
    )
    description: str | None = Field(default=None, description="帮助说明")


class _ClarificationArgs(BaseModel):
    """ask_clarification 的 LLM 可见参数。"""

    question: str = Field(..., description="要问用户的澄清问题，要具体清晰")
    clarification_type: ClarificationType = Field(
        default="missing_info",
        description="澄清类型: missing_info/ambiguous_requirement/approach_choice/risk_confirmation/suggestion",
    )
    context: str | None = Field(
        default=None, description="为什么需要澄清的背景说明，帮助用户理解"
    )
    options: Annotated[list[str] | None, BeforeValidator(_coerce_options)] = Field(
        default=None, description="可选项列表（approach_choice/suggestion 时提供）"
    )
    fields: Annotated[
        list[ClarificationField] | None, BeforeValidator(_coerce_fields)
    ] = Field(
        default=None,
        description=(
            "向导模式：需同时澄清多个问题时提供，按序逐题作答。每个字段含 "
            "name/label/field_type(text|number|boolean|select)/required/"
            "options(3-5 个推荐，可省略表示纯输入如密码)/default/description。"
            "提供时前端逐题引导（可返回修改），不提供则走单问题模式。"
        ),
    )


async def _ask_clarification(
    question: str,
    clarification_type: ClarificationType = "missing_info",
    context: str | None = None,
    options: list[str] | None = None,
    fields: list[ClarificationField] | None = None,
) -> str:
    """向用户提问以获取澄清信息。执行会中断，等待用户回答后继续。

    单问题模式（默认）：用户答案通常是纯文本。
    向导模式（提供 fields）：用户答案为 ``{name: value}`` 的 JSON 字符串。

    工具直调 langgraph.interrupt() 挂起 graph。宿主 resume 时传入的值
    （用户答案）作为本函数返回值，交给 LLM 继续推理。
    """
    from langgraph.types import interrupt

    # 字段序列化为 dict 列表（便于宿主/前端无强类型依赖地消费）。
    field_dicts = [f.model_dump() for f in fields] if fields else None

    payload = {
        "question": question,
        "type": clarification_type,
        "context": context,
        "options": options,
        "fields": field_dicts,
    }
    # interrupt 挂起 graph；resume(Command(resume=answer)) 后返回 answer。
    # 答案类型由宿主决定：单问题通常是 str，向导模式是 JSON 字符串。
    answer = interrupt(payload)
    return answer if isinstance(answer, str) else str(answer)


ask_clarification = StructuredTool.from_function(
    _ask_clarification,
    name="ask_clarification",
    description=(
        "向用户提问以获取澄清信息。当你缺少必要信息、需求有歧义、有多种方案"
        "需要用户选择、或要进行有风险操作需要确认时使用。调用后执行会中断，"
        "等待用户回答后继续。需要同时澄清多个问题时，用 fields 参数以向导"
        "形式逐题收集（每题可给推荐方案 + 自由输入），一次问完避免多轮追问。"
    ),
    args_schema=_ClarificationArgs,
    coroutine=_ask_clarification,
)


__all__ = ["ask_clarification", "ClarificationType", "ClarificationField", "FieldType"]
