from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bili.util import now_iso, write_json


@dataclass
class PipelineState:
    """Small, durable per-item checkpoint independent of a job id."""

    folder: Path
    version: int = 2
    stages: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def path(self) -> Path:
        return self.folder / ".pipeline.json"

    @classmethod
    def load(cls, folder: Path) -> "PipelineState":
        state = cls(folder=folder)
        try:
            raw = json.loads(state.path.read_text(encoding="utf-8"))
            if int(raw.get("version") or 0) == state.version:
                state.stages = raw.get("stages") or {}
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        return state

    def completed(self, stage: str, signature: str, outputs: list[Path] | None = None) -> bool:
        item = self.stages.get(stage) or {}
        if item.get("status") != "done" or item.get("signature") != signature:
            return False
        return all(path.exists() for path in (outputs or []))

    def mark(self, stage: str, signature: str, status: str, **details: Any) -> None:
        details.pop("status", None)
        details.pop("signature", None)
        self.stages[stage] = {
            "status": status,
            "signature": signature,
            "updated_at": now_iso(),
            **details,
        }
        write_json(
            self.path,
            {"version": self.version, "updated_at": now_iso(), "stages": self.stages},
        )

