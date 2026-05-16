"""Shared types for Vertex AI adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExecuteResult:
    success: bool
    resource_name: str | None = None
    undo_recipe: dict[str, str] = field(default_factory=dict)
    snapshot: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    op_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "resource_name": self.resource_name,
            "undo_recipe": self.undo_recipe,
            "snapshot": self.snapshot,
            "warnings": self.warnings,
            "details": self.details,
            "op_id": self.op_id,
        }
