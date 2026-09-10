"""Minimal GitHub OAuth adapter; upstream tokens are never persisted."""
from typing import Protocol
import httpx

class GitHubOAuthError(RuntimeError):
    pass

class GitHubOAuth(Protocol):
    def exchange_and_identify(self, code: str, redirect_uri: str) -> str: ...

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
                login = identity.json().get("login")
                if not isinstance(login, str) or not login:
                    raise GitHubOAuthError("GitHub identity validation failed")
                return login
        except (httpx.HTTPError, ValueError, TypeError):
            raise GitHubOAuthError("GitHub OAuth validation failed") from None
