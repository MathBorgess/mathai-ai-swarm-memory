"""Minimal GitHub OAuth adapter; upstream tokens are never persisted."""
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote, urlsplit
import re
import httpx

GITHUB_API_ORIGIN = "https://api.github.com"
_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_SUBJECT_RE = re.compile(r"^[1-9][0-9]{0,19}$")


class GitHubOAuthError(RuntimeError):
    pass


class GitHubIdentityError(RuntimeError):
    pass


class GitHubOAuth(Protocol):
    def start_device(self) -> dict: ...
    def poll_device(self, device_code: str) -> str | None: ...


@dataclass(frozen=True)
class GitHubAccount:
    subject: str
    login: str


def parse_github_login(raw: str) -> str:
    if not isinstance(raw, str):
        raise ValueError("GitHub login must be a username")
    login = raw[1:] if raw.startswith("@") else raw
    if not _LOGIN_RE.fullmatch(login):
        raise ValueError("GitHub login must be a username")
    return login


def parse_github_subject(raw: str) -> str:
    if not isinstance(raw, str) or not _SUBJECT_RE.fullmatch(raw):
        raise ValueError("GitHub subject must be a numeric account id")
    return raw


def lookup_github_login(login: str, *, transport: httpx.BaseTransport | None = None) -> GitHubAccount:
    parsed = parse_github_login(login)
    return _get_github_account(f"{GITHUB_API_ORIGIN}/users/{quote(parsed, safe='')}", transport=transport)


def lookup_github_subject(subject: str, *, transport: httpx.BaseTransport | None = None) -> GitHubAccount:
    parsed = parse_github_subject(subject)
    return _get_github_account(f"{GITHUB_API_ORIGIN}/user/{parsed}", transport=transport)


def _get_github_account(url: str, *, transport: httpx.BaseTransport | None) -> GitHubAccount:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "api.github.com":
        raise GitHubIdentityError("GitHub user lookup failed")
    try:
        with httpx.Client(transport=transport, timeout=15, follow_redirects=False, trust_env=False) as client:
            response = client.get(url, headers={"Accept": "application/vnd.github+json"})
    except (httpx.HTTPError, ValueError, TypeError):
        raise GitHubIdentityError("GitHub user lookup failed") from None
    if response.status_code != 200:
        raise GitHubIdentityError("GitHub user lookup failed")
    try:
        payload = response.json()
    except ValueError:
        raise GitHubIdentityError("GitHub user lookup failed") from None
    if not isinstance(payload, dict):
        raise GitHubIdentityError("GitHub user lookup failed")
    user_id = payload.get("id")
    login = payload.get("login")
    if not isinstance(user_id, int) or user_id <= 0 or not isinstance(login, str) or not login:
        raise GitHubIdentityError("GitHub user lookup failed")
    return GitHubAccount(subject=str(user_id), login=login)


class HttpGitHubOAuth:
    def __init__(self, client_id: str, client_secret: str, *, transport: httpx.BaseTransport | None = None):
        if not client_id or not client_secret:
            raise ValueError("GitHub OAuth credentials are required")
        self.client_id, self._client_secret, self._transport = client_id, client_secret, transport

    def start_device(self) -> dict:
        try:
            with httpx.Client(transport=self._transport, timeout=15, trust_env=False) as client:
                response = client.post("https://github.com/login/device/code", data={"client_id": self.client_id, "scope": "read:user"}, headers={"Accept": "application/json"})
                response.raise_for_status(); payload = response.json()
                if not all(isinstance(payload.get(k), str) for k in ("device_code", "user_code", "verification_uri")):
                    raise GitHubOAuthError("GitHub device flow failed")
                return payload
        except (httpx.HTTPError, ValueError, TypeError):
            raise GitHubOAuthError("GitHub device flow failed") from None

    def poll_device(self, device_code: str) -> str | None:
        try:
            with httpx.Client(transport=self._transport, timeout=15, trust_env=False) as client:
                response = client.post("https://github.com/login/oauth/access_token", data={"client_id": self.client_id, "device_code": device_code, "grant_type": "urn:ietf:params:oauth:grant-type:device_code"}, headers={"Accept": "application/json"})
                payload = response.json()
                if "error" in payload and payload["error"] in {"authorization_pending", "slow_down"}:
                    return None
                token = payload.get("access_token")
                if not isinstance(token, str) or not token:
                    raise GitHubOAuthError("GitHub device approval failed")
                identity = client.get("https://api.github.com/user", headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"})
                identity.raise_for_status(); user_id = identity.json().get("id")
                if not isinstance(user_id, int) or user_id <= 0:
                    raise GitHubOAuthError("GitHub identity validation failed")
                return str(user_id)
        except (httpx.HTTPError, ValueError, TypeError):
            raise GitHubOAuthError("GitHub device validation failed") from None
