"""Minimal GitHub OAuth adapter; upstream tokens are never persisted."""
from typing import Protocol
import httpx

class GitHubOAuthError(RuntimeError):
    pass

class GitHubOAuth(Protocol):
    def exchange_and_identify(self, code: str, redirect_uri: str) -> str: ...
    def start_device(self) -> dict: ...
    def poll_device(self, device_code: str) -> str | None: ...

class HttpGitHubOAuth:
    def __init__(self, client_id: str, client_secret: str, *, transport: httpx.BaseTransport | None = None):
        if not client_id or not client_secret:
            raise ValueError("GitHub OAuth credentials are required")
        self.client_id, self._client_secret, self._transport = client_id, client_secret, transport

    def exchange_and_identify(self, code: str, redirect_uri: str) -> str:
        if not code or not redirect_uri.startswith("https://"):
            raise GitHubOAuthError("Invalid OAuth callback")
        try:
            with httpx.Client(transport=self._transport, timeout=15, follow_redirects=False, trust_env=False) as client:
                token = client.post("https://github.com/login/oauth/access_token", data={"client_id": self.client_id, "client_secret": self._client_secret, "code": code, "redirect_uri": redirect_uri}, headers={"Accept": "application/json"})
                token.raise_for_status()
                access_token = token.json().get("access_token")
                if not isinstance(access_token, str) or not access_token:
                    raise GitHubOAuthError("GitHub token exchange failed")
                identity = client.get("https://api.github.com/user", headers={"Authorization": "Bearer " + access_token, "Accept": "application/vnd.github+json"})
                identity.raise_for_status()
                user_id = identity.json().get("id")
                if not isinstance(user_id, int) or user_id <= 0:
                    raise GitHubOAuthError("GitHub identity validation failed")
                return str(user_id)
        except (httpx.HTTPError, ValueError, TypeError):
            raise GitHubOAuthError("GitHub OAuth validation failed") from None

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
