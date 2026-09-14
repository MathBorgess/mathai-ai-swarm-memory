"""Load morning/evening report configuration from JSON (secrets via env only)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONFIG_ENV = "MATHAI_REPORTS_CONFIG"


@dataclass(frozen=True)
class PlannerProviderConfig:
    command: tuple[str, ...]
    timeout_seconds: int = 120

    @classmethod
    def from_mapping(cls, raw: Any) -> PlannerProviderConfig | None:
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise ValueError("planner_provider must be an object")
        cmd = raw.get("command")
        if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) for x in cmd):
            raise ValueError("planner_provider.command must be a non-empty argv list")
        timeout = int(raw.get("timeout_seconds", 120))
        if timeout < 1 or timeout > 3600:
            raise ValueError("planner_provider.timeout_seconds out of range")
        return cls(command=tuple(cmd), timeout_seconds=timeout)


@dataclass(frozen=True)
class ReportsConfig:
    wiki_dir: Path
    state_dir: Path
    weights_path: Path
    output_dir: Path
    owner_id: str
    timezone: str
    planner_provider: PlannerProviderConfig | None

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> ReportsConfig:
        wiki = _require_abs_path(data, "wiki_dir")
        state = _require_abs_path(data, "state_dir")
        weights = _require_abs_path(data, "weights_path")
        output = _require_abs_path(data, "output_dir")
        owner = str(data.get("owner_id") or "").strip()
        if not owner or len(owner) > 64:
            raise ValueError("owner_id is required (max 64 chars)")
        tz = str(data.get("timezone") or "America/Recife").strip()
        if len(tz) > 64:
            raise ValueError("timezone too long")
        planner = PlannerProviderConfig.from_mapping(data.get("planner_provider"))
        return cls(
            wiki_dir=wiki,
            state_dir=state,
            weights_path=weights,
            output_dir=output,
            owner_id=owner,
            timezone=tz,
            planner_provider=planner,
        )


def _require_abs_path(data: dict[str, Any], key: str) -> Path:
    raw = data.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{key} must be a non-empty absolute path string")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{key} must be absolute")
    return path


def load_config(path: Path | None = None) -> ReportsConfig:
    config_path = path
    if config_path is None:
        env = os.environ.get(CONFIG_ENV)
        if not env:
            raise ValueError(
                f"report config required: pass --config or set {CONFIG_ENV} to an absolute JSON path"
            )
        config_path = Path(env)
    if not config_path.is_absolute():
        raise ValueError("--config must be an absolute path")
    data = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("config root must be a JSON object")
    return ReportsConfig.from_mapping(data)
