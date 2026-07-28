"""PromptTemplate model — fixed-slot prompt composition.

Slot schema is fixed and defined in SLOT_SCHEMA:
  role → task → constraints → context → output_format → tool_declaration (auto)

Templates only store default values for each slot.

The SLOT_SCHEMA / SLOT_NAMES / SlotDef / TOOL_DECLARATION_SLOT constants are
re-exported from ``agent_flow_harness.slots.schema`` (the single source of
truth) so both harness and the application share one definition.
"""
from __future__ import annotations

# ── Fixed slot schema (re-exported from harness) ─────────────────────
from agent_flow_harness import (  # noqa: F401
    SLOT_NAMES,
    SLOT_SCHEMA,
    TOOL_DECLARATION_SLOT,
    SlotDef,
)
from pydantic import BaseModel, ConfigDict, Field

from app.models.base import generate_id, utc_now


class PromptTemplate(BaseModel):
    """A reusable prompt template with default values for fixed slots."""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default_factory=lambda: generate_id("tmpl"), alias="_id")
    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="", max_length=1000)
    slot_defaults: dict[str, str] = Field(default_factory=dict)
    version: int = Field(default=1)
    tags: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())
    updated_at: str = Field(default_factory=lambda: utc_now().isoformat())
