"""User-facing CLI. Consent lives here, never in an MCP tool response."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import timedelta
from typing import Callable, Sequence, TextIO

import httpx

from mathai_swarm_mcp.errors import LoginRequiredError, SwarmMcpError, redact
from mathai_swarm_mcp.http import DEFAULT_SCOPES, SwarmHttpClient
from mathai_swarm_mcp.keystore import KeyringStore, public_material, ensure_key
from mathai_swarm_mcp.origin import parse_origin
from mathai_swarm_mcp.server import run_stdio


def main(
    argv: Sequence[str] | None = None,
    *,
    store=None,
    transport: httpx.BaseTransport | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    sleeper: Callable[[float], None] | None = None,
    clock=None,
) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        return int(exc.code or 0)
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    try:
        if args.cmd == "login":
            return _login(args, store=store, transport=transport, stdout=out, stderr=err, sleeper=sleeper, clock=clock)
        if args.cmd == "logout":
            return _logout(args, store=store, transport=transport, stdout=out, stderr=err, clock=clock)
        if args.cmd == "show-key":
            return _show_key(args, store=store, stdout=out)
        if args.cmd == "serve":
            return _serve(args, store=store, transport=transport, clock=clock)
        parser.print_help(out)
        return 2
    except SwarmMcpError as exc:
        print(str(exc), file=err)
        return 1
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {redact(exc)}", file=err)
        return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mathai-swarm-mcp",
        description="Local MCP adapter for the mathai swarm origin. Never pass a bearer in harness config.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    login = sub.add_parser("login", help="GitHub Device Flow in this terminal; stores a refresh token in the OS keystore")
    _origin_principal(login)
    login.add_argument("--scope", action="append", dest="scopes", help="ctx:read:* or ctx:propose:* scope (repeatable)")
    logout = sub.add_parser("logout", help="Revoke this principal's family and delete the local key")
    _origin_principal(logout)
    show = sub.add_parser("show-key", help="Print public JWK and thumbprint for registration via the auth CLI")
    _origin_principal(show)
    serve = sub.add_parser("serve", help="Run the MCP server on stdio")
    _origin_principal(serve)
    return parser


def _origin_principal(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--origin", required=True, help="HTTPS origin, e.g. https://a2a.mathai.com.br")
    parser.add_argument("--principal", required=True, help="principal_id of this harness installation")


def _client(args, store, transport, clock) -> SwarmHttpClient:
    return SwarmHttpClient(
        parse_origin(args.origin),
        args.principal,
        store=store if store is not None else KeyringStore(),
        transport=transport,
        clock=clock,
    )


def _show_key(args, *, store, stdout: TextIO) -> int:
    client_store = store if store is not None else KeyringStore()
    record = ensure_key(client_store, args.origin, args.principal)
    material = public_material(record)
    json.dump({"principal_id": args.principal, "origin": parse_origin(args.origin), **material}, stdout)
    stdout.write("\n")
    return 0


def _login(args, *, store, transport, stdout: TextIO, stderr: TextIO, sleeper, clock) -> int:
    from datetime import datetime, timezone

    tick = clock or (lambda: datetime.now(timezone.utc))
    client = _client(args, store, transport, tick)
    try:
        material = client.public_material()
        print(f"public_jkt {material['jkt']}", file=stdout)
        started = client.start_device(args.scopes or list(DEFAULT_SCOPES))
        print(f"Open {started['verification_uri']} and enter code {started['user_code']}", file=stdout)
        print("Waiting for GitHub Device Flow approval in this terminal. This is not an MCP tool.", file=stdout)
        sleep = sleeper or time.sleep
        deadline = tick() + timedelta(seconds=int(started["expires_in"]))
        interval = max(0, int(started["interval"]))
        while tick() < deadline:
            sleep(interval)
            result = client.poll_device(started["device_code"])
            error = (result or {}).get("error")
            if error == "slow_down":
                interval += 5
                continue
            if error == "authorization_pending":
                continue
            if result and result.get("status") == "authorized":
                print("authorized; refresh token stored in the OS keystore. Access tokens stay in memory.", file=stdout)
                return 0
            raise SwarmMcpError("device authorization failed")
        raise LoginRequiredError("device code expired; run login again")
    finally:
        client.close()


def _logout(args, *, store, transport, stdout: TextIO, stderr: TextIO, clock) -> int:
    client = _client(args, store, transport, clock)
    try:
        client.logout()
        print("local credential removed", file=stdout)
        return 0
    finally:
        client.close()


def _serve(args, *, store, transport, clock) -> int:
    client = _client(args, store, transport, clock)
    run_stdio(client)
    return 0
