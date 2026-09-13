"""GitHub authorization-code identity. Upstream tokens are never returned or stored."""

from __future__ import annotations

from typing import Protocol

import httpx


class GitHubIdentityError(RuntimeError):
    pass


class GitHubAuthorizationCode(Protocol):
    def authorization_url(self, *, state: str, redirect_uri: str) -> str: ...
    def exchange_code(self, *, code: str, redirect_uri: str) -> str: ...


class HttpGitHubAuthorizationCode:
    AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
    TOKEN_URL = "https://github.com/login/oauth/access_token"
    USER_URL = "https://api.github.com/user"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        callback_url: str,
        transport: httpx.BaseTransport | None = None,
    ):
        if not client_id or not client_secret or not callback_url:
            raise ValueError("GitHub OAuth credentials and callback URL are required")
        self.client_id = client_id
        self._client_secret = client_secret
        self.callback_url = callback_url
        self._transport = transport

    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        if redirect_uri != self.callback_url or not state:
            raise GitHubIdentityError("GitHub redirect_uri must be the broker callback")
        query = httpx.QueryParams(
            {
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "state": state,
                "scope": "read:user",
            }
        )
        return f"{self.AUTHORIZE_URL}?{query}"

    def exchange_code(self, *, code: str, redirect_uri: str) -> str:
        if redirect_uri != self.callback_url or not code:
            raise GitHubIdentityError("GitHub identity validation failed")
        try:
            with httpx.Client(
                transport=self._transport, timeout=15, follow_redirects=False, trust_env=False,
            ) as client:
                response = client.post(
                    self.TOKEN_URL,
                    data={
                        "client_id": self.client_id,
                        "client_secret": self._client_secret,
                        "code": code,
                        "redirect_uri": redirect_uri,
                    },
                    headers={"Accept": "application/json"},
                )
                if response.status_code != 200:
                    raise GitHubIdentityError("GitHub identity validation failed")
                payload = response.json()
                token = payload.get("access_token")
                if not isinstance(token, str) or not token:
                    raise GitHubIdentityError("GitHub identity validation failed")
                identity = client.get(
                    self.USER_URL,
                    headers={
                        "Authorization": "Bearer " + token,
                        "Accept": "application/vnd.github+json",
                    },
                )
                if identity.status_code != 200:
                    raise GitHubIdentityError("GitHub identity validation failed")
                user_id = identity.json().get("id")
                if not isinstance(user_id, int) or user_id <= 0:
                    raise GitHubIdentityError("GitHub identity validation failed")
                return str(user_id)
        except GitHubIdentityError:
            raise
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            raise GitHubIdentityError("GitHub identity validation failed") from None
