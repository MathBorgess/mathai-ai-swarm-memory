"""CLI for mathai-swarm-reports (also reached via `mathai-swarm report ...`).

`--config` is accepted both before and after the subcommand, because the documented
broker delegation is `mathai-swarm report morning --config /abs/reports.json` and an
option that only parses in one position is a documented command that does not run.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

from swarm_reports.config import load_config
from swarm_reports.morning import run_morning
from swarm_reports.storage import MorningRunBusy


def _config_flag(parser: argparse.ArgumentParser, *, suppress: bool = False) -> None:
    # Subparsers use SUPPRESS so that an unset subcommand flag does not overwrite the
    # value already parsed at the top level with argparse's default.
    parser.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS if suppress else None,
        help="Absolute path to reports JSON config (or set MATHAI_REPORTS_CONFIG)",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mathai-swarm-reports",
        description="Daily report cycle (morning HTML, evening schema).",
    )
    _config_flag(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    report = sub.add_parser("report", help="Generate reports")
    report_sub = report.add_subparsers(dest="report_command", required=True)

    morning = report_sub.add_parser("morning", help="Morning HTML + wiki freeze")
    _config_flag(morning, suppress=True)
    morning.add_argument("--date", help="Calendar day (YYYY-MM-DD, America/Recife default)")
    morning.add_argument(
        "--plan",
        type=Path,
        help="Absolute path to a morning plan JSON produced by the daily-plan skill",
    )
    morning.add_argument("--dry-run", action="store_true", help="Do not mutate wiki/state")
    morning.add_argument("--replay", action="store_true", help="Render only; no freeze")
    morning.add_argument(
        "--force-replan",
        action="store_true",
        help="Discard a cached planner result for the day and dispatch again",
    )
    morning.add_argument(
        "--wiki-local-only",
        action="store_true",
        help="Freeze against local HEAD instead of a fetched origin/main (tests only)",
    )

    evening = report_sub.add_parser("evening", help="Evening report (F4)")
    _config_flag(evening, suppress=True)
    evening.add_argument("--date", help="Calendar day (YYYY-MM-DD)")
    evening.add_argument(
        "--input",
        type=Path,
        help="Absolute path to a copied evening payload JSON (same body as POST /evening)",
    )

    for host in (sub.add_parser("serve", help="Serve reports and receive POST /evening"),
                 report_sub.add_parser("serve", help="Alias of the top-level serve")):
        _config_flag(host, suppress=True)
        host.add_argument("--host", help="Override server.bind_host (still gated by auth_mode)")
        host.add_argument("--port", type=int, help="Override server.port")
        host.add_argument(
            "--drain-outbox",
            action="store_true",
            help="Process pending evening jobs once and exit; never binds a socket",
        )
    return parser


def _resolve_config(args: argparse.Namespace):
    path = getattr(args, "config", None)
    return load_config(path)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)

    command = args.command
    if command == "report":
        command = args.report_command
    if command not in ("morning", "evening", "serve"):
        parser.print_help()
        return 2

    try:
        config = _resolve_config(args)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    report_day: date | None = None
    if getattr(args, "date", None):
        try:
            report_day = date.fromisoformat(args.date)
        except ValueError:
            print("error: --date must be YYYY-MM-DD", file=sys.stderr)
            return 2

    if command == "serve":
        return _serve(config, args)
    if command == "evening":
        return _evening(config, args, report_day)
    return _morning(config, args, report_day)


def _morning(config, args: argparse.Namespace, report_day: date | None) -> int:
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
            force_replan=bool(args.force_replan),
            local_only_wiki=bool(args.wiki_local_only),
        )
    except MorningRunBusy as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(result.html_path)
    if result.freeze_reused:
        print(
            "note: checklist already frozen; report regenerated from the snapshot",
            file=sys.stderr,
        )
    if result.freeze_branch:
        print(f"note: freeze branch {result.freeze_branch} @ {result.freeze_commit}", file=sys.stderr)
    return 0


def _evening(config, args: argparse.Namespace, report_day: date | None) -> int:
    if args.input is None:
        print(
            "error: report evening needs --input until F4 wires the night session",
            file=sys.stderr,
        )
        return 2
    if not args.input.is_absolute():
        print("error: --input must be an absolute path", file=sys.stderr)
        return 2
    try:
        from swarm_reports.server.submit import submit_from_file
    except ImportError:
        print("error: evening submission requires the F3 server module", file=sys.stderr)
        return 2
    try:
        outcome = submit_from_file(config, args.input, day=report_day)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"revision {outcome.revision} ({'new' if outcome.changed else 'unchanged'})")
    if outcome.changed:
        print("note: night session runs in F4; the job is queued in the outbox", file=sys.stderr)
    return 0


def _serve(config, args: argparse.Namespace) -> int:
    from swarm_reports.server.app import build_server
    from swarm_reports.server.outbox import BoundedCommandDispatcher, Outbox, OutboxWorker

    if args.drain_outbox:
        # Useful after a crash or before F4 exists: shows what is waiting without
        # opening a socket at all.
        outbox = Outbox(config.state_dir)
        pending = outbox.pending()
        if config.server is None or not config.server.dispatch_command:
            for job in pending:
                print(f"pending {job.day} revision {job.revision} attempts {job.attempts}")
            print(f"{len(pending)} pending; no evening handler configured (F4)")
            return 0
        worker = OutboxWorker(
            outbox,
            BoundedCommandDispatcher(
                config.server.dispatch_command,
                timeout_seconds=config.server.dispatch_timeout_seconds,
            ),
        )
        print(f"completed {worker.drain_once()} of {len(pending)} pending")
        return 0

    overrides: dict[str, object] = {}
    if args.host:
        overrides["bind_host"] = args.host
    if args.port:
        overrides["port"] = args.port
    try:
        server = build_server(
            config,
            logger=lambda message: print(message, file=sys.stderr),
            overrides=overrides or None,
        )
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    host, port = server.address
    print(f"serving {config.output_dir} on http://{host}:{port} "
          f"({server.server_config.auth_mode})")
    server.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
