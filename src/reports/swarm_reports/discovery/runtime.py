"""F6 config and durable outside-plan presentation; never modifies the freeze/claims."""
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from .orchestrator import DiscoveryConfig, run_discovery
from .checkpoints import load_checkpoints, default_checkpoint_path
from .sources.github import GithubConfig
from .sources.linear import LinearConfig
from .sources.wiki_log import WikiLogConfig


def config_from_reports(config):
    raw = config.discovery or {}
    github = raw.get("github")
    linear = raw.get("linear")
    wiki = raw.get("wiki_log")
    allowed = set()
    if config.dispatch_policy_path:
        from swarm_reports.dispatch.policy_config import load_dispatch_policy
        allowed = {r.repo for r in load_dispatch_policy(config.dispatch_policy_path).repos}
    repos = tuple(github.get("repos") or ()) if github else ()
    if repos and not set(repos) <= allowed:
        raise ValueError("discovery repos must belong to dispatch policy allowlist")
    return DiscoveryConfig(state_dir=config.state_dir,
        github=GithubConfig(repos=repos, max_pages=github.get("max_pages", 3),
            per_page=github.get("per_page", 30), timeout_seconds=github.get("timeout_seconds", 20),
            gh_bin=github.get("gh_bin", "gh")) if github else None,
        linear=LinearConfig(token_env=linear.get("token_env"), command=tuple(linear["command"]) if linear.get("command") else None,
            max_pages=linear.get("max_pages", 3), timeout_seconds=linear.get("timeout_seconds", 20)) if linear else None,
        wiki_log=WikiLogConfig(log_path=Path(wiki.get("path") or config.wiki_dir / "wiki/log.md"),
            wiki_root=config.wiki_dir) if wiki else None)


def visible_items(config, day):
    state = load_checkpoints(default_checkpoint_path(config.state_dir))
    zone = ZoneInfo(config.timezone)
    rows = []
    for row in state.items.values():
        seen = datetime.fromisoformat(row["first_seen"]).astimezone(zone).date()
        if day - timedelta(days=1) <= seen <= day:
            rows.append(row["item"])
    return sorted(rows, key=lambda item: (item["observed_at"], item["id"]))[-64:]


def refresh(config, day):
    batch = run_discovery(config_from_reports(config))
    from swarm_reports.evening.ledger import LedgerStore
    records = LedgerStore(config.state_dir).entries_between(day - timedelta(days=1), day)
    links = {r.link for r in records if r.link}
    ids = {r.target for r in records} | {r.entry_id for r in records}
    items = [x for x in visible_items(config, day) if x["id"] not in ids and x.get("url") not in links]
    if config.dispatch_policy_path:
        from swarm_reports.dispatch.policy_config import load_dispatch_policy
        from swarm_reports.dispatch.evening_autonomy import collect_pr_digest
        from swarm_reports.dispatch.gh_cli import GhCliTransport
        policy = load_dispatch_policy(config.dispatch_policy_path)
        github = (config.discovery or {}).get("github") or {}
        gh = GhCliTransport(gh_bin=github.get("gh_bin", "gh"))
        # Review only new events, and retry failed reviews on the next refresh.
        from swarm_reports.dispatch.runtime_store import load_runtime
        known = {c.get("pr") for c in load_runtime(config.state_dir).digest_cards}
        for item in items:
            meta = item.get("meta") or {}
            if item["kind"].startswith("pr_") and f'{meta["repo"]}#{meta["number"]}' not in known:
                try:
                    collect_pr_digest(policy, config.state_dir, meta["repo"], meta["number"], gh)
                except (RuntimeError, ValueError, OSError):
                    pass
    note = f"Descoberta parcial: {len(batch.errors)} fonte(s) indisponível(is)." if batch.errors else ""
    return items, note
