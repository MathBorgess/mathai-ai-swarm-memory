"""Operator CLI for principals, grants and token-family revocation.

Policy is never administered over HTTP or MCP. The store path is required;
this command does not infer a default database.
"""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.adapters.github import GitHubIdentityError, lookup_github_login
from app.adapters.sqlite import SqlitePairingStore
from app.auth.scopes import MAX_GRANT_TTL_DAYS, ScopeError, ROLES, validate_role, validate_scope


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load_jwk(raw: str) -> dict:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("JWK must be public JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("JWK must be public JSON")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mathai-swarm",
        description="Administer swarm principals and grants in a local SQLite store.",
    )
    parser.add_argument("--store", required=True, help="Absolute path to the broker SQLite file")
    sub = parser.add_subparsers(dest="command", required=True)

    principal = sub.add_parser("principal", help="Register or list principals")
    principal_sub = principal.add_subparsers(dest="principal_command", required=True)
    add = principal_sub.add_parser("add", help="Bind a principal id to a GitHub subject and JWK")
    add.add_argument("--id", required=True, dest="principal_id")
    add.add_argument("--subject", required=True, help="Numeric GitHub account id")
    add.add_argument("--jwk", required=True, help="Public JWK as JSON")
    add.add_argument("--role", required=True, choices=sorted(ROLES))
    principal_sub.add_parser("list", help="List principals without private material")

    allow = sub.add_parser("allow", help="Grant a GitHub username without enrolling a DPoP key")
    allow.add_argument("login", help="GitHub login, optionally prefixed with @")
    allow.add_argument("--role", required=True, choices=sorted(ROLES))
    allow.add_argument("--scope", required=True, action="append", dest="scopes",
                       help="Repeatable ctx scope; validated before GitHub lookup")
    allow.add_argument("--ttl", required=True, type=int, help=f"TTL in days, maximum {MAX_GRANT_TTL_DAYS}")

    grant = sub.add_parser("grant", help="Set, list or revoke explicit grants")
    grant_sub = grant.add_subparsers(dest="grant_command", required=True)
    grant_set = grant_sub.add_parser("set", help="Replace the active grant for one scope")
    grant_set.add_argument("--principal", required=True)
    grant_set.add_argument("--scope", required=True)
    grant_set.add_argument("--days", required=True, type=int, help=f"TTL in days, maximum {MAX_GRANT_TTL_DAYS}")
    grant_list = grant_sub.add_parser("list", help="List active grants")
    grant_list.add_argument("--principal", required=True)
    grant_revoke = grant_sub.add_parser("revoke", help="Revoke an active grant")
    grant_revoke.add_argument("--principal", required=True)
    grant_revoke.add_argument("--scope", required=True)

    token = sub.add_parser("token", help="Revoke issued token families")
    token_sub = token.add_subparsers(dest="token_command", required=True)
    token_revoke = token_sub.add_parser("revoke", help="Revoke a refresh family immediately")
    token_revoke.add_argument("--family", required=True)
    return parser


def main(argv: list[str] | None = None, *, github_transport=None, now: datetime | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    store_path = Path(args.store)
    if not store_path.is_absolute():
        print("error: --store must be an absolute path", file=sys.stderr)
        return 2
    clock = now or _now()
    try:
        with closing(SqlitePairingStore(store_path)) as store:
            if args.command == "principal" and args.principal_command == "add":
                principal = store.add_principal(
                    args.principal_id,
                    github_subject=args.subject,
                    jwk=_load_jwk(args.jwk),
                    role=args.role,
                    now=clock,
                )
                print(f"{principal.id} {principal.role} {principal.github_subject} {principal.jwk_thumbprint}")
            elif args.command == "principal" and args.principal_command == "list":
                for principal in store.list_principals():
                    status = "revoked" if principal.revoked_at else "active"
                    thumb = principal.jwk_thumbprint or "-"
                    print(f"{principal.id} {principal.role} {principal.github_subject} {thumb} {status}")
            elif args.command == "allow":
                if args.ttl < 1 or args.ttl > MAX_GRANT_TTL_DAYS:
                    raise ScopeError(f"Grant TTL must be between 1 and {MAX_GRANT_TTL_DAYS} days")
                validate_role(args.role)
                for scope in args.scopes:
                    validate_scope(scope, role=args.role)
                account = lookup_github_login(args.login, transport=github_transport)
                principal = store.allow_github_principal(
                    github_subject=account.subject,
                    github_login=account.login,
                    role=args.role,
                    scopes=tuple(args.scopes),
                    now=clock,
                    expires_at=clock + timedelta(days=args.ttl),
                )
                print(f"{principal.id} {principal.role} {principal.github_subject} {principal.github_login or '-'}")
                for grant in store.list_grants(principal.id, clock):
                    print(f"{grant.principal_id} {grant.scope} {grant.expires_at.isoformat()}")
            elif args.command == "grant" and args.grant_command == "set":
                if args.days < 1 or args.days > MAX_GRANT_TTL_DAYS:
                    raise ScopeError(f"Grant TTL must be between 1 and {MAX_GRANT_TTL_DAYS} days")
                grant = store.set_grant(
                    args.principal, args.scope, clock, clock + timedelta(days=args.days),
                )
                print(f"{grant.principal_id} {grant.scope} {grant.expires_at.isoformat()}")
            elif args.command == "grant" and args.grant_command == "list":
                grants = store.list_grants(args.principal, clock)
                if not grants:
                    print(f"{args.principal} (no active grants)")
                for grant in grants:
                    print(f"{grant.principal_id} {grant.scope} {grant.expires_at.isoformat()}")
            elif args.command == "grant" and args.grant_command == "revoke":
                store.revoke_grant(args.principal, args.scope, clock)
                print(f"{args.principal} {args.scope} revoked")
            elif args.command == "token" and args.token_command == "revoke":
                store.revoke_family(args.family, clock)
                print(f"family {args.family} revoked")
            else:
                parser.print_help()
                return 2
    except (ScopeError, ValueError, OSError, GitHubIdentityError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
