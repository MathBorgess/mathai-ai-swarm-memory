"""CLI for mathai-swarm-reports (also invoked via mathai-swarm report)."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from swarm_reports.config import load_config
from swarm_reports.morning import run_morning


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mathai-swarm-reports",
        description="Daily report cycle (morning HTML, evening schema).",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Absolute path to reports JSON config (or set MATHAI_REPORTS_CONFIG)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    report = sub.add_parser("report", help="Generate reports")
    report_sub = report.add_subparsers(dest="report_command", required=True)
    morning = report_sub.add_parser("morning", help="Morning HTML + wiki freeze")
    morning.add_argument("--date", help="Calendar day (YYYY-MM-DD, America/Recife default)")
    morning.add_argument(
        "--plan",
        type=Path,
        help="Offline replay: morning plan JSON (skips live provider)",
    )
    morning.add_argument("--dry-run", action="store_true", help="Do not mutate wiki/state")
    morning.add_argument("--replay", action="store_true", help="Render only; no freeze")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)

    if args.command != "report" or args.report_command != "morning":
        parser.print_help()
        return 2

    try:
        config = load_config(args.config)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    report_day: date | None = None
    if args.date:
        try:
            report_day = date.fromisoformat(args.date)
        except ValueError:
            print("error: --date must be YYYY-MM-DD", file=sys.stderr)
            return 2

    plan_path = args.plan
    if plan_path is not None and not plan_path.is_absolute():
        print("error: --plan must be an absolute path", file=sys.stderr)
        return 2

    try:
        result = run_morning(
            config,
            day=report_day,
            plan_path=plan_path,
            dry_run=bool(args.dry_run),
            replay=bool(args.replay),
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(result.html_path)
    if result.skipped_duplicate:
        print("note: duplicate morning run skipped (idempotent)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
