"""Fixed-destination A2A JSON-RPC adapter using only a server-local bearer."""

import secrets
from typing import Protocol
from urllib.parse import urlsplit

import httpx


class HermesError(RuntimeError):
    pass


class HermesClient(Protocol):
    def query_as_broker(self, *, agent_id: str, query: str) -> object: ...


class HttpHermesClient:
    def __init__(self, url: str, bearer: str, *, transport: httpx.BaseTransport | None = None):
        parsed = urlsplit(url)
        loopback_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1"}
        if (parsed.scheme != "https" and not loopback_http) or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("Hermes requires HTTPS or a fixed loopback HTTP endpoint")
        if not bearer or "\n" in bearer or "\r" in bearer:
            raise ValueError("A server-local Hermes bearer is required")
        self.url = url
        self._bearer = bearer
        self._transport = transport

    def query_as_broker(self, *, agent_id: str, query: str) -> object:
        request_id = secrets.token_urlsafe(16)
        payload = {"jsonrpc": "2.0", "id": request_id, "method": "message/send", "params": {
            "message": {"kind": "message", "messageId": secrets.token_urlsafe(16), "role": "user",
                        "parts": [{"kind": "text", "text": query}]},
        }}
        try:
            with httpx.Client(transport=self._transport, timeout=120, follow_redirects=False, trust_env=False) as client:
                response = client.post(self.url, json=payload, headers={"Authorization": "Bearer " + self._bearer})
                response.raise_for_status()
                result = response.json()
            if not isinstance(result, dict) or result.get("jsonrpc") != "2.0" or result.get("id") != request_id or "error" in result or "result" not in result:
                raise HermesError("Hermes query failed")
            return result["result"]
        except (httpx.HTTPError, ValueError):
            raise HermesError("Hermes query failed") from None
