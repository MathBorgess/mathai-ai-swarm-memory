"""RES (reach-engagement score) — deterministic, weights from wiki JSON."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

ZeroReachPolicy = Literal["undefined", "zero"]

_REACH_KEYS = ("reach", "impressions", "alcançados")
_OUTSIDE_KEYS = (
    "outside_fraction",
    "impressions_outside_network_fraction",
    "non_follower_reach_fraction",
)
_RESERVED_TOP_LEVEL = set(_REACH_KEYS) | set(_OUTSIDE_KEYS) | {"signals"}


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError("boolean is not a numeric metric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("metric must be finite")
    return number


def _first_present(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in data:
            return data[key]
    return None


@dataclass(frozen=True)
class PostMetrics:
    reach: float | None = None
    outside_fraction: float | None = None
    signals: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> PostMetrics:
        reach_raw = _first_present(data, _REACH_KEYS)
        outside_raw = _first_present(data, _OUTSIDE_KEYS)
        reach = _optional_float(reach_raw) if reach_raw is not None else None
        outside = _optional_float(outside_raw) if outside_raw is not None else None
        signals_raw = data.get("signals")
        signals: dict[str, float] = {}
        if isinstance(signals_raw, dict):
            for key, value in signals_raw.items():
                if value is None:
                    continue
                signals[str(key)] = _signal_count(value)
        else:
            for key, value in data.items():
                if key in _RESERVED_TOP_LEVEL:
                    continue
                if isinstance(value, bool):
                    raise TypeError("boolean is not a signal count")
                if isinstance(value, (int, float)):
                    signals[str(key)] = _signal_count(value)
        metrics = cls(reach=reach, outside_fraction=outside, signals=signals)
        metrics.validate()
        return metrics

    def validate(self) -> None:
        if self.reach is not None:
            if not math.isfinite(self.reach) or self.reach < 0:
                raise ValueError("reach must be finite and non-negative")
        if self.outside_fraction is not None:
            if not math.isfinite(self.outside_fraction):
                raise ValueError("outside_fraction must be finite")
            if self.outside_fraction < 0 or self.outside_fraction > 1:
                raise ValueError("outside_fraction must be between 0 and 1")
        for value in self.signals.values():
            if not math.isfinite(value) or value < 0:
                raise ValueError("signal counts must be finite and non-negative")


def _signal_count(value: Any) -> float:
    if isinstance(value, bool):
        raise TypeError("boolean is not a signal count")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("signal count must be finite")
    if number < 0:
        raise ValueError("signal count must be non-negative")
    return number


@dataclass(frozen=True)
class ResWeights:
    version: int
    platforms: dict[str, dict[str, float]]

    def signal_weights(self, platform: str) -> dict[str, float]:
        platform_key = platform.lower()
        block = self.platforms.get(platform_key)
        if block is None:
            raise KeyError(f"unknown platform '{platform}' in RES weights")
        return {str(k): float(v) for k, v in block.items()}


def load_res_weights(path: Path | str) -> ResWeights:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("RES weights root must be an object")
    if "version" not in raw:
        raise ValueError("RES weights require 'version'")
    version = int(raw["version"])
    platform_block = raw.get("platforms")
    if not isinstance(platform_block, dict) or not platform_block:
        raise ValueError("RES weights require non-empty 'platforms'")
    platforms: dict[str, dict[str, float]] = {}
    for name, cfg in platform_block.items():
        if not isinstance(cfg, dict):
            raise ValueError(f"platform '{name}' must be an object")
        signals = cfg.get("signals")
        if not isinstance(signals, dict) or not signals:
            raise ValueError(f"platform '{name}' requires non-empty 'signals'")
        weights = {}
        for signal_name, weight in signals.items():
            value = float(weight)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"signal weight for '{signal_name}' must be finite and non-negative")
            weights[str(signal_name)] = value
        platforms[str(name).lower()] = weights
    return ResWeights(version=version, platforms=platforms)


def compute_engagement(weights: dict[str, float], signals: dict[str, float]) -> float:
    total = 0.0
    for name, count in signals.items():
        if not math.isfinite(count) or count < 0:
            raise ValueError("signal counts must be finite and non-negative")
        if count == 0:
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
    if zero_reach_policy not in ("undefined", "zero"):
        raise ValueError("zero_reach_policy must be 'undefined' or 'zero'")
    if not math.isfinite(engagement) or engagement < 0:
        raise ValueError("engagement must be finite and non-negative")
    if not math.isfinite(reach) or reach < 0:
        raise ValueError("reach must be finite and non-negative")
    if reach == 0:
        if zero_reach_policy == "zero":
            return 0.0
        return None
    base = engagement / math.sqrt(reach / 1000.0)
    if outside_fraction is None:
        factor = 1.0
    else:
        if not math.isfinite(outside_fraction):
            raise ValueError("outside_fraction must be finite")
        if outside_fraction < 0 or outside_fraction > 1:
            raise ValueError("outside_fraction must be between 0 and 1")
        factor = 0.5 + outside_fraction
    return base * factor


def period_res_summary(
    scores: list[tuple[bool, float | None]],
    *,
    period_days: int = 7,
) -> dict[str, Any]:
    if not isinstance(period_days, (int, float)) or not math.isfinite(float(period_days)):
        raise ValueError("period_days must be a positive finite number")
    if period_days <= 0:
        raise ValueError("period_days must be positive")
    guided_scores = [s for guided_flag, s in scores if guided_flag and s is not None]
    spontaneous_scores = [s for guided_flag, s in scores if not guided_flag and s is not None]
    guided_posts = sum(1 for guided_flag, _s in scores if guided_flag)
    spontaneous_posts = sum(1 for guided_flag, _s in scores if not guided_flag)
    weeks = max(float(period_days) / 7.0, 1e-9)

    def avg(values: list[float]) -> float | None:
        if not values:
            return None
        return sum(values) / len(values)

    return {
        "period_days": period_days,
        "guided": {
            "average_res": avg(guided_scores),
            "post_count": guided_posts,
            "posts_per_week": guided_posts / weeks,
        },
        "spontaneous": {
            "average_res": avg(spontaneous_scores),
            "post_count": spontaneous_posts,
            "posts_per_week": spontaneous_posts / weeks,
        },
    }
