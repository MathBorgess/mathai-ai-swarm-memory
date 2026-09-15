from __future__ import annotations

import math
from pathlib import Path

import pytest

from swarm_reports.metrics.posts import parse_post_markdown
from swarm_reports.metrics.res import (
    PostMetrics,
    compute_engagement,
    compute_res,
    load_res_weights,
    period_res_summary,
)

FIXTURES = Path(__file__).parent / "fixtures" / "metrics"


def test_outlier_a_res_matches_design_note():
    weights = load_res_weights(FIXTURES / "res-weights.json")
    post = parse_post_markdown((FIXTURES / "outlier-a-post.md").read_text(encoding="utf-8"))
    assert post.metrics is not None
    w = weights.signal_weights(post.platform)
    engagement = compute_engagement(w, post.metrics.signals)
    assert engagement == 545
    score = compute_res(engagement, post.metrics.reach, post.metrics.outside_fraction)
    assert score is not None
    assert round(score, 1) == 84.6


def test_outlier_b_res_matches_design_note():
    weights = load_res_weights(FIXTURES / "res-weights.json")
    post = parse_post_markdown((FIXTURES / "outlier-b-post.md").read_text(encoding="utf-8"))
    assert post.metrics is not None
    w = weights.signal_weights(post.platform)
    engagement = compute_engagement(w, post.metrics.signals)
    assert engagement == 25
    score = compute_res(engagement, post.metrics.reach, post.metrics.outside_fraction)
    assert score is not None
    assert round(score, 1) == 56.1


def test_missing_outside_fraction_is_neutral_multiplier():
    score = compute_res(25.0, 320.0, None)
    base = 25.0 / math.sqrt(320.0 / 1000.0)
    assert score == pytest.approx(base)


def test_zero_reach_policy_undefined():
    assert compute_res(10.0, 0.0, None, zero_reach_policy="undefined") is None


def test_zero_reach_policy_zero():
    assert compute_res(10.0, 0.0, None, zero_reach_policy="zero") == 0.0


def test_invalid_negative_signal_rejected():
    metrics = PostMetrics(reach=100, signals={"reactions": -1})
    with pytest.raises(ValueError):
        metrics.validate()


def test_period_summary_splits_guided_and_spontaneous():
    summary = period_res_summary([(True, 80.0), (True, 90.0), (False, 56.1)], period_days=7)
    assert summary["guided"]["post_count"] == 2
    assert summary["spontaneous"]["post_count"] == 1
    assert summary["guided"]["average_res"] == 85.0
