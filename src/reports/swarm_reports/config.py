"""Load morning/evening report configuration from JSON (secrets via env only)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from swarm_reports.server.config import ServerConfig, load_server_config

CONFIG_ENV = "MATHAI_REPORTS_CONFIG"


#: The only planner contract F2 implements: your adapter reads request JSON on stdin
#: and writes MorningPlan JSON on stdout. Provider CLIs that emit prose need the F5
#: adapter layer, so declaring any other kind is rejected instead of half-working.
PLANNER_KIND_JSON_STDIO = "json-stdio"


@dataclass(frozen=True)
class PlannerProviderConfig:
    command: tuple[str, ...]
    timeout_seconds: int = 120
    kind: str = PLANNER_KIND_JSON_STDIO

    @classmethod
    def from_mapping(cls, raw: Any) -> PlannerProviderConfig | None:
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise ValueError("planner_provider must be an object")
        cmd = raw.get("command")
        if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) for x in cmd):
            raise ValueError("planner_provider.command must be a non-empty argv list")
        timeout = raw.get("timeout_seconds", 120)
        if isinstance(timeout, bool) or not isinstance(timeout, int):
            raise ValueError("planner_provider.timeout_seconds must be an integer")
        if timeout < 1 or timeout > 3600:
            raise ValueError("planner_provider.timeout_seconds out of range")
        kind = str(raw.get("kind") or PLANNER_KIND_JSON_STDIO)
        if kind != PLANNER_KIND_JSON_STDIO:
            raise ValueError(
                f"planner_provider.kind must be '{PLANNER_KIND_JSON_STDIO}'; "
                "adapters for text-emitting provider CLIs land in F5"
            )
        return cls(command=tuple(cmd), timeout_seconds=timeout, kind=kind)


@dataclass(frozen=True)
class ReportsConfig:
    wiki_dir: Path
    state_dir: Path
    weights_path: Path
    output_dir: Path
    owner_id: str
    timezone: str
    planner_provider: PlannerProviderConfig | None
    #: Public origin the report is served from, e.g. `https://reports.mathai.com.br`.
    #: Absent means "no server": the HTML then offers only the copy-prompt path.
    public_origin: str | None = None
    #: F3 server profile. Absent means the server cannot start at all, so forgetting to
    #: choose an auth profile can never turn into a public vault.
    server: ServerConfig | None = None

    @property
    def evening_post_url(self) -> str | None:
        return "/evening" if self.public_origin else None

    def evening_revision_url(self, day) -> str | None:
        """Same-origin, relative on purpose: the HTML must work behind any hostname."""
        if not self.public_origin:
            return None
        return f"/evening/revision?day={day.isoformat()}"

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
        try:
            ZoneInfo(tz)
        except Exception as exc:  # noqa: BLE001 - surfaces as a config error
            raise ValueError(f"unknown timezone '{tz}'") from exc
        planner = PlannerProviderConfig.from_mapping(data.get("planner_provider"))
        return cls(
            wiki_dir=wiki,
            state_dir=state,
            weights_path=weights,
            output_dir=output,
            owner_id=owner,
            timezone=tz,
            planner_provider=planner,
            public_origin=normalize_public_origin(data.get("public_origin")),
            server=load_server_config(data.get("server")),
        )


def normalize_public_origin(raw: Any) -> str | None:
    """Scheme + host + optional port, nothing else. Used for the CSRF Origin check."""
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("public_origin must be a non-empty string when present")
    text = raw.strip().rstrip("/")
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("public_origin must be http(s)://host[:port]")
    if parsed.path or parsed.query or parsed.fragment or parsed.username:
        raise ValueError("public_origin must not carry a path, query or credentials")
    return f"{parsed.scheme}://{parsed.netloc}"


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
