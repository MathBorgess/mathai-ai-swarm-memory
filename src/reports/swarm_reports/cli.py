"""CLI for mathai-swarm-reports (also reached via `mathai-swarm report ...`).

`--config` is accepted both before and after the subcommand, because the documented
broker delegation is `mathai-swarm report morning --config /abs/reports.json` and an
option that only parses in one position is a documented command that does not run.
"""

from __future__ import annotations

import argparse
import json
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
        help="Absolute path to a copied evening payload JSON, or '-' for stdin "
        "(same body as POST /evening)",
    )
    evening.add_argument(
        "--job-stdin",
        action="store_true",
        help="Read an outbox job on stdin and run the night for the revision it names",
    )
    evening.add_argument(
        "--revision",
        type=int,
        help="Run the night for an already-stored revision (default: the latest)",
    )
    evening.add_argument(
        "--no-publish",
        action="store_true",
        help="Commit the vault edit locally and skip push/PR (local demo)",
    )
    evening.add_argument(
        "--submit-only",
        action="store_true",
        help="Store the revision and leave the night to the outbox worker",
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
        host.add_argument(
            "--no-publish",
            action="store_true",
            help="Night commits the vault edit locally and skips push/PR (local demo)",
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
    """Three ways in, one engine.

    `--input` is the copy-prompt path the owner uses when the VPS is down: the payload
    goes through `submit_evening`, exactly like the form POST, and then the night runs
    on the revision that produced. `--job-stdin` is the outbox dispatcher: the job names
    a revision that is already stored, so nothing is enqueued again. Without either, the
    latest stored revision is (re)processed, which is how a parked job is retried by hand.
    """
    from swarm_reports.server.outbox import Outbox, OutboxJob
    from swarm_reports.server.revisions import RevisionStore
    from swarm_reports.server.submit import SubmitRejected, parse_payload_text, submit_evening

    publish = not args.no_publish

    if args.job_stdin:
        if args.input is not None:
            print("error: --job-stdin and --input are mutually exclusive", file=sys.stderr)
            return 2
        try:
            job = OutboxJob.from_json(json.loads(sys.stdin.read()))
        except (ValueError, KeyError) as exc:
            print(f"error: unreadable outbox job on stdin: {exc}", file=sys.stderr)
            return 2
        return _run_night(
            config, date.fromisoformat(job.day), job.revision, job.content_hash or None, publish
        )

    day = report_day
    revision = args.revision

    if args.input is not None:
        text, source = _read_payload(args.input)
        if text is None:
            print(f"error: {source}", file=sys.stderr)
            return 2
        try:
            payload = parse_payload_text(text)
            if day is not None and payload.day != day:
                raise SubmitRejected("day_mismatch", "--date does not match the payload day")
            outcome = submit_evening(config, payload)
        except (OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"revision {outcome.revision} ({'new' if outcome.changed else 'unchanged'})")
        day = date.fromisoformat(outcome.day)
        revision = outcome.revision
        if args.submit_only:
            print("note: night left to the outbox worker (--submit-only)", file=sys.stderr)
            return 0

    if day is None:
        print("error: report evening needs --input, --job-stdin or --date", file=sys.stderr)
        return 2
    if revision is None:
        latest = RevisionStore(config.state_dir).latest(day)
        if latest is None:
            print(f"error: no evening revision stored for {day.isoformat()}", file=sys.stderr)
            return 2
        revision = latest.revision

    code = _run_night(config, day, revision, None, publish)
    if code == 0:
        # The CLI already did the work the queued job names; leaving it pending would
        # make the worker replay a no-op forever.
        Outbox(config.state_dir).resolve(day.isoformat(), revision)
    return code


def _read_payload(raw: str) -> tuple[str | None, str]:
    if raw == "-":
        return sys.stdin.read(), ""
    path = Path(raw)
    if not path.is_absolute():
        return None, "--input must be an absolute path or '-'"
    try:
        return path.read_text(encoding="utf-8"), ""
    except OSError as exc:
        return None, str(exc)


def _run_night(
    config,
    day: date,
    revision: int,
    expected_hash: str | None,
    publish: bool,
) -> int:
    from swarm_reports.evening.session import EveningRejected, run_evening_session

    try:
        result = run_evening_session(
            config,
            day=day,
            revision=revision,
            expected_hash=expected_hash,
            publish=publish,
        )
    except EveningRejected as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"evening {result.day} revision {result.revision}: {result.status}")
    if result.wiki_commit:
        print(f"note: {result.wiki_branch} @ {result.wiki_commit[:12]}", file=sys.stderr)
    if result.pull_request_url:
        print(f"note: {result.pull_request_url}", file=sys.stderr)
    elif result.publish_status:
        print(f"note: publish {result.publish_status}: {result.detail}", file=sys.stderr)
    # A failed pull request is a failed effect, not a failed night: the metrics are
    # durable. Exit 1 so a supervisor notices without treating the day as unclosed.
    return 1 if result.publish_status == "failed" else 0


def _evening_dispatcher(config, *, publish: bool = True):
    """The handler `serve` uses by default.

    `server.dispatch_command` stays available for an operator who wants the night in a
    separate process, but it is no longer the only way to get one: a default install
    that runs `report serve` closes the day without any wrapper script.
    """
    from swarm_reports.evening.session import make_dispatcher
    from swarm_reports.server.outbox import BoundedCommandDispatcher

    if config.server is not None and config.server.dispatch_command:
        return BoundedCommandDispatcher(
            config.server.dispatch_command,
            timeout_seconds=config.server.dispatch_timeout_seconds,
        )
    return make_dispatcher(config, publish=publish)


def _serve(config, args: argparse.Namespace) -> int:
    from swarm_reports.server.app import build_server
    from swarm_reports.server.outbox import Outbox, OutboxWorker

    dispatcher = _evening_dispatcher(config, publish=not args.no_publish)

    if args.drain_outbox:
        # Useful after a crash: runs everything waiting without opening a socket.
        outbox = Outbox(config.state_dir)
        pending = outbox.pending()
        for job in pending:
            print(f"pending {job.day} revision {job.revision} attempts {job.attempts}")
        worker = OutboxWorker(
            outbox,
            dispatcher,
            on_event=lambda message: print(message, file=sys.stderr),
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
            dispatcher=dispatcher,
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
