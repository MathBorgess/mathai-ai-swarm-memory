import json
import subprocess
from datetime import date
from pathlib import Path

import pytest

from swarm_reports.config import ReportsConfig
from swarm_reports.metrics.daily import FROZEN_BEGIN
from swarm_reports.metrics.state import FrozenItem, default_state_path
from swarm_reports.morning import run_morning
from swarm_reports.wiki.freeze import apply_wiki_freeze, freeze_worktree_path

DAY = date(2026, 9, 14)
TEMPLATE = "# {{date}}\n\n## Hoje\n\n- [ ] \n\n## Evolução\n\n- \n"


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return proc.stdout.strip()


def init_wiki(tmp_path: Path, *, with_daily: bool = True, with_origin: bool = False) -> Path:
    wiki = tmp_path / "wiki"
    (wiki / "daily").mkdir(parents=True)
    _git(wiki, "init", "-b", "main")
    _git(wiki, "config", "user.email", "t@example.com")
    _git(wiki, "config", "user.name", "test")
    (wiki / "daily" / "_template.md").write_text(TEMPLATE, encoding="utf-8")
    if with_daily:
        (wiki / "daily" / f"{DAY.isoformat()}.md").write_text(
            f"# {DAY.isoformat()}\n\n## Hoje\n\n- [ ] item base\n", encoding="utf-8"
        )
    _git(wiki, "add", ".")
    _git(wiki, "commit", "-m", "init")
    if with_origin:
        bare = tmp_path / "origin.git"
        _git(tmp_path, "init", "--bare", str(bare))
        _git(wiki, "remote", "add", "origin", str(bare))
        _git(wiki, "push", "-u", "origin", "main")
    return wiki


def config_for(tmp_path: Path, wiki: Path, **kwargs) -> ReportsConfig:
    weights = Path(__file__).parent / "fixtures" / "metrics" / "res-weights.json"
    defaults = dict(
        wiki_dir=wiki,
        state_dir=tmp_path / "state",
        weights_path=weights,
        output_dir=tmp_path / "out",
        owner_id="owner",
        timezone="America/Recife",
        planner_provider=None,
    )
    defaults.update(kwargs)
    return ReportsConfig(**defaults)


def plan_file(tmp_path: Path, checklist, name: str = "plan.json") -> Path:
    path = tmp_path / name
    path.write_text(
        json.dumps({"day": DAY.isoformat(), "checklist": checklist, "sources": []}),
        encoding="utf-8",
    )
    return path


ITEMS = [
    FrozenItem(task_id="MAT-1", text="task", first_planned=DAY, is_p0=True),
    FrozenItem(task_id="MAT-2", text="ordinary", first_planned=DAY),
]


def test_wiki_freeze_idempotent(tmp_path):
    wiki = init_wiki(tmp_path)
    parent = tmp_path / "wt"
    first = apply_wiki_freeze(
        wiki, DAY, ITEMS, timezone="America/Recife", worktree_parent=parent, local_only=True
    )
    assert first.applied is True
    assert first.commit_sha
    second = apply_wiki_freeze(
        wiki, DAY, ITEMS, timezone="America/Recife", worktree_parent=parent, local_only=True
    )
    assert second.applied is False
    assert second.commit_sha == first.commit_sha


def test_freeze_commit_is_not_amended_and_marker_has_no_unreachable_sha(tmp_path):
    """The marker names a snapshot id; the sha lives in state, reachable by definition."""
    wiki = init_wiki(tmp_path)
    result = apply_wiki_freeze(
        wiki,
        DAY,
        ITEMS,
        timezone="America/Recife",
        worktree_parent=tmp_path / "wt",
        local_only=True,
    )
    body = result.daily_path.read_text(encoding="utf-8")
    assert "commit=" not in body
    assert f"snapshot={result.snapshot}" in body
    # One commit, never rewritten: the sha handed back is the sha on the branch.
    log = _git(result.worktree, "log", "--format=%H", "-n", "5")
    assert log.splitlines()[0] == result.commit_sha
    assert _git(result.worktree, "rev-list", "--count", "HEAD") == "2"


def test_freeze_creates_new_day_note_from_template(tmp_path):
    wiki = init_wiki(tmp_path, with_daily=False)
    result = apply_wiki_freeze(
        wiki,
        DAY,
        ITEMS,
        timezone="America/Recife",
        worktree_parent=tmp_path / "wt",
        local_only=True,
    )
    assert result.applied is True
    body = result.daily_path.read_text(encoding="utf-8")
    assert body.startswith(f"# {DAY.isoformat()}")
    assert "## Evolução" in body
    assert FROZEN_BEGIN.search(body)


def test_freeze_requires_remote_unless_local_only(tmp_path):
    wiki = init_wiki(tmp_path)
    with pytest.raises(RuntimeError, match="no 'origin' remote"):
        apply_wiki_freeze(
            wiki, DAY, ITEMS, timezone="America/Recife", worktree_parent=tmp_path / "wt"
        )


def test_freeze_starts_from_fetched_origin_main(tmp_path):
    wiki = init_wiki(tmp_path, with_origin=True)
    # Dirty live vault: private, uncommitted owner text must never be copied.
    live = wiki / "daily" / f"{DAY.isoformat()}.md"
    live.write_text(live.read_text(encoding="utf-8") + "\nsegredo do dono\n", encoding="utf-8")
    result = apply_wiki_freeze(
        wiki,
        DAY,
        ITEMS,
        timezone="America/Recife",
        worktree_parent=tmp_path / "wt",
    )
    assert result.base_ref == "origin/main"
    assert "segredo do dono" not in result.daily_path.read_text(encoding="utf-8")
    assert "segredo do dono" in live.read_text(encoding="utf-8")


def test_freeze_worktree_path_is_per_day(tmp_path):
    a = freeze_worktree_path(tmp_path, DAY)
    b = freeze_worktree_path(tmp_path, date(2026, 9, 15))
    assert a != b
    assert a.is_absolute()


def test_freeze_rejects_worktree_on_the_wrong_branch(tmp_path):
    wiki = init_wiki(tmp_path)
    parent = tmp_path / "wt"
    result = apply_wiki_freeze(
        wiki, DAY, ITEMS, timezone="America/Recife", worktree_parent=parent, local_only=True
    )
    _git(result.worktree, "checkout", "--quiet", "-b", "someone-elses-branch")
    with pytest.raises(RuntimeError, match="expected"):
        apply_wiki_freeze(
            wiki, DAY, ITEMS, timezone="America/Recife", worktree_parent=parent, local_only=True
        )


def test_morning_twice_same_day_reuses_freeze_and_still_renders(tmp_path):
    wiki = init_wiki(tmp_path)
    config = config_for(tmp_path, wiki)
    path = plan_file(
        tmp_path,
        [
            {"task_id": "MAT-1", "text": "P0 task", "is_p0": True},
            {"task_id": "MAT-2", "text": "ordinary item"},
        ],
    )
    first = run_morning(config, plan_path=path, day=DAY, local_only_wiki=True)
    assert first.freeze_reused is False
    assert first.freeze_applied is True
    first_html = first.html_path.read_text(encoding="utf-8")

    second = run_morning(config, plan_path=path, day=DAY, local_only_wiki=True)
    assert second.freeze_reused is True
    # The old duplicate path returned before rendering, leaving a stale report.
    assert second.html_path.is_file()
    assert second.html_path.read_text(encoding="utf-8") == first_html

    wt_daily = (
        freeze_worktree_path(config.state_dir / "wiki-worktrees", DAY)
        / "daily"
        / f"{DAY.isoformat()}.md"
    )
    assert FROZEN_BEGIN.search(wt_daily.read_text(encoding="utf-8"))
    state = json.loads(default_state_path(config.state_dir).read_text(encoding="utf-8"))
    assert state["days"][DAY.isoformat()]["frozen_at_commit"]


def test_morning_freezes_the_full_checklist_not_only_p0(tmp_path):
    wiki = init_wiki(tmp_path)
    config = config_for(tmp_path, wiki)
    path = plan_file(
        tmp_path,
        [
            {"task_id": "MAT-1", "text": "P0 task", "is_p0": True},
            {"task_id": "MAT-2", "text": "ordinary item"},
            {"task_id": "MAT-3", "text": "another ordinary item"},
        ],
    )
    result = run_morning(config, plan_path=path, day=DAY, local_only_wiki=True)
    state = json.loads(default_state_path(config.state_dir).read_text(encoding="utf-8"))
    frozen = state["days"][DAY.isoformat()]["frozen_checklist"]
    assert [item["task_id"] for item in frozen] == ["MAT-1", "MAT-2", "MAT-3"]
    assert [item["task_id"] for item in frozen if item["is_p0"]] == ["MAT-1"]
    html = result.html_path.read_text(encoding="utf-8")
    assert "another ordinary item" in html
    assert 'data-check-row="MAT-3"' in html
