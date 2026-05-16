"""Types for the Firestore adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FirestoreExecuteResult:
    success: bool
    project: str
    database: str
    collection: str
    document_id: str
    undo_recipe: dict[str, str] = field(default_factory=dict)
    snapshot: dict[str, Any] | None = None
    details: dict[str, Any] = field(default_factory=dict)
    op_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "project": self.project,
            "database": self.database,
            "collection": self.collection,
            "document_id": self.document_id,
            "undo_recipe": self.undo_recipe,
            "snapshot": self.snapshot,
            "details": self.details,
            "op_id": self.op_id,
        }
