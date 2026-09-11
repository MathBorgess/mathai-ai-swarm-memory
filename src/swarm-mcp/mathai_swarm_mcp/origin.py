"""Trust only the configured HTTPS origin and GitHub Device Flow consent URLs."""

from __future__ import annotations

from urllib.parse import urlsplit

from mathai_swarm_mcp.errors import OriginError

GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})
GITHUB_DEVICE_PATH = "/login/device"


def parse_origin(origin: str) -> str:
    raw = (origin or "").strip()
    parts = urlsplit(raw)
    if parts.scheme != "https" or parts.username or parts.password:
        raise OriginError("origin must be an https URL without userinfo")
    if not parts.hostname or parts.path not in {"", "/"} or parts.query or parts.fragment:
        raise OriginError("origin must be an https origin without path, query, or fragment")
    host = parts.hostname.lower()
    netloc = f"{host}:{parts.port}" if parts.port else host
    return f"https://{netloc}"


def request_url(origin: str, path: str) -> str:
    if not path.startswith("/") or path.startswith("//") or ".." in path.split("/"):
        raise OriginError("refusing non-absolute or traversing path")
    return parse_origin(origin) + path


def assert_same_origin(origin: str, url: str) -> None:
    frozen = parse_origin(origin)
    parts = urlsplit(url)
    candidate = f"{parts.scheme}://{parts.netloc}"
    if parts.scheme != "https" or parse_origin(candidate) != frozen:
        raise OriginError("refusing request outside the configured origin")


def canonical_htu(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc:
        raise OriginError("DPoP htu must be an https URL")
    path = parts.path if parts.path else "/"
    return f"{parts.scheme}://{parts.netloc}{path}"


def validate_github_verification_uri(uri: str) -> str:
    parts = urlsplit((uri or "").strip())
    host = (parts.hostname or "").lower()
    path = (parts.path or "").rstrip("/") or "/"
    if parts.scheme != "https" or host not in GITHUB_HOSTS or path != GITHUB_DEVICE_PATH:
        raise OriginError("consent URL is not a GitHub device-flow verification URI")
    if parts.username or parts.password or parts.fragment:
        raise OriginError("consent URL is not a GitHub device-flow verification URI")
    return uri.strip()
