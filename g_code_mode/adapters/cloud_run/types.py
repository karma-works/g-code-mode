"""Types for the Cloud Run adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TrafficSplit:
    """One leg of a traffic split. revision=None means LATEST."""

    percent: int
    revision: str | None = None  # None → LATEST

    def to_dict(self) -> dict[str, Any]:
        return {
            "percent": self.percent,
            "revision": self.revision or "LATEST",
        }


@dataclass
class CloudRunExecuteResult:
    success: bool
    service_id: str
    region: str
    undo_recipe: dict[str, str] = field(default_factory=dict)
    snapshot: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    op_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "service_id": self.service_id,
            "region": self.region,
            "undo_recipe": self.undo_recipe,
            "snapshot": self.snapshot,
            "warnings": self.warnings,
            "details": self.details,
            "op_id": self.op_id,
        }
