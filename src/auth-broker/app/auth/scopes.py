"""Explicit ctx scope grammar and role eligibility for the personal pilot."""

from __future__ import annotations

from datetime import timedelta

ROLES = frozenset({"owner", "self-harness", "advisor"})
NAMESPACE_PATHS = {"pesquisa.tcc": "pesquisa/tcc"}
ALLOWED_SCOPES = frozenset({
    "ctx:read:pesquisa.tcc",
    "ctx:propose:pesquisa.tcc",
})
MAX_GRANT_TTL_DAYS = 90
DEFAULT_ACCESS_TOKEN_SECONDS = 300
MAX_ACCESS_TOKEN_SECONDS = 3600

_ADVISOR_CLASSIFICATIONS = ("public", "shared")
_OWNER_CLASSIFICATIONS = ("public", "shared", "internal", "restricted")


class ScopeError(ValueError):
    """Unknown, empty, or ineligible scope set."""


def validate_role(role: str) -> str:
    if role not in ROLES:
        raise ScopeError("Unknown principal role")
    return role


def eligibility(role: str) -> frozenset[str]:
    validate_role(role)
    if role == "advisor":
        return ALLOWED_SCOPES
    return ALLOWED_SCOPES


def classifications_for(role: str) -> tuple[str, ...]:
    validate_role(role)
    if role == "advisor":
        return _ADVISOR_CLASSIFICATIONS
    return _OWNER_CLASSIFICATIONS


def validate_scope(scope: str, *, role: str | None = None) -> str:
    if not isinstance(scope, str) or scope not in ALLOWED_SCOPES:
        raise ScopeError("Unknown or forbidden scope")
    if role is not None and scope not in eligibility(role):
        raise ScopeError("Scope is not eligible for this principal role")
    return scope


def requested_scopes(raw) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise ScopeError("Requested scopes are required")
    seen: list[str] = []
    for item in raw:
        validate_scope(item)
        if item not in seen:
            seen.append(item)
    return tuple(seen)


def effective_scopes(requested: tuple[str, ...], granted: frozenset[str], role: str) -> tuple[str, ...]:
    allowed = eligibility(role)
    matched = tuple(scope for scope in requested if scope in granted and scope in allowed)
    if not matched:
        raise ScopeError("Effective scope set is empty")
    return matched


def validate_grant_ttl(*, now, expires_at) -> None:
    if expires_at <= now:
        raise ScopeError("Grant expiry must follow issuance")
    if expires_at - now > timedelta(days=MAX_GRANT_TTL_DAYS):
        raise ScopeError("Grant TTL exceeds the 90-day pilot maximum")
