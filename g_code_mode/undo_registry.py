"""Undo recipe dataclass returned alongside every mutating execute result."""

from dataclasses import dataclass


@dataclass
class UndoRecipe:
    description: str
    call: str

    def to_dict(self) -> dict[str, str]:
        return {"description": self.description, "call": self.call}
