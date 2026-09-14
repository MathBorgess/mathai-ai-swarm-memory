"""RES (reach-engagement score) — deterministic, weights from wiki JSON."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

ZeroReachPolicy = Literal["undefined", "zero"]


@dataclass(frozen=True)
class PostMetrics:
    reach: float | None = None
    outside_fraction: float | None = None
    signals: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> PostMetrics:
        reach = data.get("reach") or data.get("impressions") or data.get("alcançados")
        outside = (
            data.get("outside_fraction")
            or data.get("impressions_outside_network_fraction")
            or data.get("non_follower_reach_fraction")
        )
        signals_raw = data.get("signals")
        signals: dict[str, float] = {}
        if isinstance(signals_raw, dict):
            for key, value in signals_raw.items():
                if value is None:
                    continue
                signals[str(key)] = float(value)
        else:
            for key, value in data.items():
                if key in {"reach", "impressions", "alcançados", "outside_fraction",
                           "impressions_outside_network_fraction", "non_follower_reach_fraction"}:
                    continue
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    signals[str(key)] = float(value)
        return cls(
            reach=float(reach) if reach is not None else None,
            outside_fraction=float(outside) if outside is not None else None,
            signals=signals,
        )

    def validate(self) -> None:
        if self.reach is not None and (math.isnan(self.reach) or self.reach < 0):
            raise ValueError("reach must be finite and non-negative")
        if self.outside_fraction is not None:
            if math.isnan(self.outside_fraction) or self.outside_fraction < 0:
                raise ValueError("outside_fraction must be finite and non-negative")
        for value in self.signals.values():
            if math.isnan(value) or value < 0:
                raise ValueError("signal counts must be finite and non-negative")


@dataclass(frozen=True)
class ResWeights:
    version: int
    platforms: dict[str, dict[str, float]]

    def signal_weights(self, platform: str) -> dict[str, float]:
        platform_key = platform.lower()
        block = self.platforms.get(platform_key) or self.platforms.get("linkedin", {})
        return {str(k): float(v) for k, v in block.items()}


def load_res_weights(path: Path | str) -> ResWeights:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    version = int(raw.get("version", 1))
    platforms: dict[str, dict[str, float]] = {}
    platform_block = raw.get("platforms") or {}
    if isinstance(platform_block, dict):
        for name, cfg in platform_block.items():
            if not isinstance(cfg, dict):
                continue
            signals = cfg.get("signals") or cfg
            if isinstance(signals, dict):
                platforms[str(name).lower()] = {
                    str(k): float(v) for k, v in signals.items()
                }
    return ResWeights(version=version, platforms=platforms)


def compute_engagement(weights: dict[str, float], signals: dict[str, float]) -> float:
    total = 0.0
    for name, count in signals.items():
        if count <= 0:
            continue
        weight = weights.get(name)
        if weight is None:
            raise KeyError(f"unknown signal '{name}' for platform weights")
        total += count * weight
    return total


def compute_res(
    engagement: float,
    reach: float,
    outside_fraction: float | None = None,
    *,
    zero_reach_policy: ZeroReachPolicy = "undefined",
) -> float | None:
    if engagement < 0 or math.isnan(engagement):
        raise ValueError("engagement must be finite and non-negative")
    if reach < 0 or math.isnan(reach):
        raise ValueError("reach must be finite and non-negative")
    if reach == 0:
        if zero_reach_policy == "zero":
            return 0.0
        return None
    base = engagement / math.sqrt(reach / 1000.0)
    if outside_fraction is None:
        factor = 1.0
    else:
        if outside_fraction < 0 or math.isnan(outside_fraction):
            raise ValueError("outside_fraction must be finite and non-negative")
        factor = 0.5 + outside_fraction
    return base * factor


def period_res_summary(
    scores: list[tuple[bool, float | None]],
    *,
    period_days: int = 7,
) -> dict[str, Any]:
    guided = [s for guided_flag, s in scores if guided_flag and s is not None]
    spontaneous = [s for guided_flag, s in scores if not guided_flag and s is not None]
    weeks = max(period_days / 7.0, 1e-9)

    def avg(values: list[float]) -> float | None:
        if not values:
            return None
        return sum(values) / len(values)

    return {
        "period_days": period_days,
        "guided": {
            "average_res": avg(guided),
            "post_count": len(guided),
            "posts_per_week": len(guided) / weeks,
        },
        "spontaneous": {
            "average_res": avg(spontaneous),
            "post_count": len(spontaneous),
            "posts_per_week": len(spontaneous) / weeks,
        },
    }
