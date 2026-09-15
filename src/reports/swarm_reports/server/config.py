"""Server configuration, and the gate that stops a public start without Access.

The report renders the private vault. `AGENTS.md` and the design note both say Cloudflare
Access must sit in front of the hostname before anything is published, so the config
refuses to produce a server that could be reachable without it:

| profile | what it asserts | bind |
|---|---|---|
| `cloudflare-access-jwt` | every request carries a verified Access JWT | any host |
| `tunnel-loopback` | only `cloudflared` can reach us, Access is enforced at the edge | loopback only |
| `synthetic-local` | nothing; local development and tests | loopback only |

Only the first may bind a non-loopback address, and it may only do so with a complete
Access block. The other two refuse, which turns "I forgot to configure Access" into a
startup error instead of a public vault.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

AUTH_ACCESS_JWT = "cloudflare-access-jwt"
AUTH_TUNNEL_LOOPBACK = "tunnel-loopback"
AUTH_SYNTHETIC_LOCAL = "synthetic-local"
AUTH_MODES = (AUTH_ACCESS_JWT, AUTH_TUNNEL_LOOPBACK, AUTH_SYNTHETIC_LOCAL)
LOOPBACK_ONLY_MODES = (AUTH_TUNNEL_LOOPBACK, AUTH_SYNTHETIC_LOCAL)

DEFAULT_MAX_BODY_BYTES = 256 * 1024
MAX_MAX_BODY_BYTES = 4 * 1024 * 1024


def is_loopback(host: str) -> bool:
    text = (host or "").strip().strip("[]").lower()
    if text in ("localhost", ""):
        return text == "localhost"
    try:
        return ip_address(text).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class AccessConfig:
    """Cloudflare Access verification material, all from configuration."""

    team_domain: str
    audience: str
    certs_url: str
    allowed_emails: tuple[str, ...]
    leeway_seconds: int = 60

    @property
    def issuer(self) -> str:
        return f"https://{self.team_domain}"

    @classmethod
    def from_mapping(cls, raw: Any) -> AccessConfig:
        if not isinstance(raw, dict):
            raise ValueError("server.access must be an object")
        team = str(raw.get("team_domain") or "").strip().lower()
        if not team or "/" in team or " " in team:
            raise ValueError("server.access.team_domain must be a bare hostname")
        audience = str(raw.get("audience") or "").strip()
        if len(audience) < 16:
            raise ValueError("server.access.audience must be the Access AUD tag")
        certs_url = str(raw.get("certs_url") or f"https://{team}/cdn-cgi/access/certs").strip()
        parsed = urlparse(certs_url)
        if parsed.scheme != "https" or parsed.hostname != team:
            # A JWKS endpoint on another host would let whoever controls it mint owners.
            raise ValueError("server.access.certs_url must be https on team_domain")
        emails_raw = raw.get("allowed_emails")
        if not isinstance(emails_raw, list) or not emails_raw:
            raise ValueError("server.access.allowed_emails must be a non-empty list")
        emails = tuple(sorted({str(x).strip().lower() for x in emails_raw if str(x).strip()}))
        if not emails:
            raise ValueError("server.access.allowed_emails must contain at least one address")
        leeway = raw.get("leeway_seconds", 60)
        if isinstance(leeway, bool) or not isinstance(leeway, int) or not (0 <= leeway <= 300):
            raise ValueError("server.access.leeway_seconds must be an int in 0..300")
        return cls(
            team_domain=team,
            audience=audience,
            certs_url=certs_url,
            allowed_emails=emails,
            leeway_seconds=leeway,
        )


@dataclass(frozen=True)
class ServerConfig:
    bind_host: str = "127.0.0.1"
    port: int = 8787
    auth_mode: str = AUTH_TUNNEL_LOOPBACK
    access: AccessConfig | None = None
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    request_timeout_seconds: float = 10.0
    dispatch_command: tuple[str, ...] | None = None
    dispatch_timeout_seconds: int = 900
    max_attempts: int = 5

    @property
    def binds_publicly(self) -> bool:
        return not is_loopback(self.bind_host)

    @classmethod
    def from_mapping(cls, raw: Any) -> ServerConfig:
        if not isinstance(raw, dict):
            raise ValueError("server must be an object")
        known = {
            "bind_host",
            "port",
            "auth_mode",
            "access",
            "max_body_bytes",
            "request_timeout_seconds",
            "dispatch_command",
            "dispatch_timeout_seconds",
            "max_attempts",
        }
        extra = sorted(set(raw) - known)
        if extra:
            raise ValueError(f"server has unknown keys: {', '.join(extra)}")

        host = str(raw.get("bind_host") or "127.0.0.1").strip()
        port = raw.get("port", 8787)
        if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
            raise ValueError("server.port must be an int in 1..65535")
        mode = str(raw.get("auth_mode") or AUTH_TUNNEL_LOOPBACK).strip()
        if mode not in AUTH_MODES:
            raise ValueError(f"server.auth_mode must be one of {list(AUTH_MODES)}")

        access_raw = raw.get("access")
        access = AccessConfig.from_mapping(access_raw) if access_raw is not None else None

        max_body = raw.get("max_body_bytes", DEFAULT_MAX_BODY_BYTES)
        if isinstance(max_body, bool) or not isinstance(max_body, int):
            raise ValueError("server.max_body_bytes must be an integer")
        if not (1024 <= max_body <= MAX_MAX_BODY_BYTES):
            raise ValueError(f"server.max_body_bytes must be in 1024..{MAX_MAX_BODY_BYTES}")

        timeout = raw.get("request_timeout_seconds", 10.0)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("server.request_timeout_seconds must be a number")
        if not (0.5 <= float(timeout) <= 120.0):
            raise ValueError("server.request_timeout_seconds must be in 0.5..120")

        command_raw = raw.get("dispatch_command")
        command: tuple[str, ...] | None = None
        if command_raw is not None:
            if (
                not isinstance(command_raw, list)
                or not command_raw
                or not all(isinstance(x, str) and x for x in command_raw)
            ):
                raise ValueError("server.dispatch_command must be a non-empty argv list")
            command = tuple(command_raw)

        dispatch_timeout = raw.get("dispatch_timeout_seconds", 900)
        if (
            isinstance(dispatch_timeout, bool)
            or not isinstance(dispatch_timeout, int)
            or not (1 <= dispatch_timeout <= 7200)
        ):
            raise ValueError("server.dispatch_timeout_seconds must be an int in 1..7200")

        attempts = raw.get("max_attempts", 5)
        if isinstance(attempts, bool) or not isinstance(attempts, int) or not (1 <= attempts <= 50):
            raise ValueError("server.max_attempts must be an int in 1..50")

        config = cls(
            bind_host=host,
            port=port,
            auth_mode=mode,
            access=access,
            max_body_bytes=max_body,
            request_timeout_seconds=float(timeout),
            dispatch_command=command,
            dispatch_timeout_seconds=dispatch_timeout,
            max_attempts=attempts,
        )
        config.check_bind_gate()
        return config

    def check_bind_gate(self) -> None:
        """Refuse any configuration that could serve the vault without Access."""
        if self.auth_mode == AUTH_ACCESS_JWT:
            if self.access is None:
                raise ValueError(
                    "server.auth_mode 'cloudflare-access-jwt' requires a server.access block"
                )
            return
        if self.binds_publicly:
            raise ValueError(
                f"server.auth_mode '{self.auth_mode}' may only bind a loopback address; "
                f"'{self.bind_host}' would expose the vault without Cloudflare Access. "
                "Put the hostname behind Access and use 'cloudflare-access-jwt', or keep "
                "the bind on 127.0.0.1 behind the named tunnel."
            )

    def require_ready_for_mutation(self, public_origin: str | None) -> None:
        """A mutation needs an origin to compare against; there is no safe default."""
        if not public_origin:
            raise ValueError(
                "public_origin is required to accept POST /evening: without it the "
                "same-origin check on mutations cannot be enforced"
            )
        if self.auth_mode == AUTH_SYNTHETIC_LOCAL and not is_loopback(
            urlparse(public_origin).hostname or ""
        ):
            # `synthetic-local` verifies nobody. Accepting a public origin under it would
            # mean the owner's real hostname is served by a profile with no identity check.
            raise ValueError(
                f"server.auth_mode 'synthetic-local' requires a loopback public_origin; "
                f"'{public_origin}' names a real host, which needs Cloudflare Access"
            )


def load_server_config(raw: Any) -> ServerConfig | None:
    if raw is None:
        return None
    return ServerConfig.from_mapping(raw)


def report_html_path(output_dir: Path, day: date) -> Path:
    return output_dir / f"{day.isoformat()}.html"
