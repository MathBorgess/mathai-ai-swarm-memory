"""The report server: static HTML by date, plus the evening endpoints.

Routing is an allowlist of three exact shapes, and the static route matches a literal
`YYYY-MM-DD.html`. There is no path to join, so `../`, `%2e%2e%2f` and every other
encoding fail on the regex rather than on a sanitizer someone has to keep correct. The
resolved file is then checked to still live inside `output_dir`, which is what catches a
symlink planted in the output directory, and nothing ever lists a directory.

Only `output_dir` is served. The vault, the state directory and the environment are not
reachable through any route.

CORS is absent on purpose — no `Access-Control-Allow-*` header is ever emitted — and
mutations additionally require `Origin` to equal the configured `public_origin`, which is
what stops another site from posting the owner's evening on their behalf.
"""

from __future__ import annotations

import json
import re
import socket
import threading
from datetime import date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit

from swarm_reports.config import ReportsConfig
from swarm_reports.evening_schema import parse_evening_payload
from swarm_reports.server.access import AccessDenied, JwksCache, verify_access_jwt
from swarm_reports.server.config import (
    AUTH_ACCESS_JWT,
    AUTH_SYNTHETIC_LOCAL,
    AUTH_TUNNEL_LOOPBACK,
    ServerConfig,
    is_loopback,
)
from swarm_reports.server.outbox import (
    BoundedCommandDispatcher,
    Dispatcher,
    Outbox,
    OutboxWorker,
)
from swarm_reports.server.revisions import RevisionStore
from swarm_reports.server.submit import (
    SubmitRejected,
    latest_revision_response,
    submit_evening,
)

DATE_HTML_ROUTE = re.compile(r"^/(\d{4}-\d{2}-\d{2})\.html$")
REVISION_ROUTE = "/evening/revision"
SUBMIT_ROUTE = "/evening"
HEALTH_ROUTE = "/healthz"
JSON_CONTENT_TYPE = "application/json"

#: Inline style and script are what "no external JS" means; everything else is denied.
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; "
    "img-src 'self' data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
    "connect-src 'self'"
)


class ReportRequestHandler(BaseHTTPRequestHandler):
    server_version = "mathai-reports"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # ---- plumbing ----------------------------------------------------------------

    @property
    def app(self) -> ReportServer:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        self.app.log(f"{self.command} {self.path.split('?')[0]} {format % args}")

    def log_error(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        self.app.log(f"error {format % args}")

    def setup(self) -> None:
        # A client that opens a socket and stalls must not hold a worker thread forever.
        self.request.settimeout(self.app.server_config.request_timeout_seconds)
        super().setup()

    def _security_headers(self) -> None:
        self.send_header("Cache-Control", "private, no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        self.send_header("Permissions-Policy", "geolocation=(), camera=(), microphone=()")
        origin = self.app.reports_config.public_origin or ""
        if origin.startswith("https://"):
            self.send_header("Strict-Transport-Security", "max-age=31536000")

    def _respond(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self._respond(status, body, f"{JSON_CONTENT_TYPE}; charset=utf-8")

    def _error(self, status: HTTPStatus, code: str, *, detail: str = "") -> None:
        """Stable code to the client; the reason stays in the local log."""
        if detail:
            self.app.log(f"reject {code}: {detail}")
        self._json(status, {"error": code})

    # ---- authentication ----------------------------------------------------------

    def _peer_is_loopback(self) -> bool:
        return is_loopback(self.client_address[0] if self.client_address else "")

    def _authenticate(self) -> bool:
        mode = self.app.server_config.auth_mode
        if mode in (AUTH_TUNNEL_LOOPBACK, AUTH_SYNTHETIC_LOCAL):
            # No header is consulted. The bind gate already guarantees loopback-only.
            if not self._peer_is_loopback():
                self._error(HTTPStatus.FORBIDDEN, "not_loopback", detail=str(self.client_address))
                return False
            return True
        if mode == AUTH_ACCESS_JWT:
            token = self.headers.get("Cf-Access-Jwt-Assertion") or self._cookie("CF_Authorization")
            if not token:
                self._error(HTTPStatus.UNAUTHORIZED, "access_required", detail="no Access JWT")
                return False
            try:
                identity = verify_access_jwt(
                    token,
                    self.app.server_config.access,
                    self.app.jwks,
                    now=self.app.now(),
                )
            except AccessDenied as exc:
                self._error(HTTPStatus.UNAUTHORIZED, "access_denied", detail=f"{exc.code}: {exc.detail}")
                return False
            self.app.log(f"access ok for {identity.email}")
            return True
        self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "auth_misconfigured")
        return False

    def _cookie(self, name: str) -> str | None:
        raw = self.headers.get("Cookie") or ""
        for chunk in raw.split(";"):
            key, _, value = chunk.strip().partition("=")
            if key == name and value:
                return value
        return None

    # ---- routes ------------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)

        if path == HEALTH_ROUTE:
            if not self._peer_is_loopback():
                self._error(HTTPStatus.NOT_FOUND, "not_found")
                return
            self._respond(HTTPStatus.OK, b"ok\n", "text/plain; charset=utf-8")
            return

        if not self._authenticate():
            return

        match = DATE_HTML_ROUTE.match(path)
        if match:
            self._serve_report(match.group(1))
            return
        if path == REVISION_ROUTE:
            self._serve_revision(parsed.query)
            return
        self._error(HTTPStatus.NOT_FOUND, "not_found")

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib signature
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802 - stdlib signature
        parsed = unquote(urlsplit(self.path).path)
        if parsed != SUBMIT_ROUTE:
            self._error(HTTPStatus.NOT_FOUND, "not_found")
            return
        if not self._authenticate():
            return
        if not self._check_origin():
            return
        body = self._read_body()
        if body is None:
            return
        self._submit(body)

    def _check_origin(self) -> bool:
        expected = self.app.reports_config.public_origin
        origin = (self.headers.get("Origin") or "").strip()
        if not expected:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "origin_not_configured")
            return False
        if origin != expected:
            # `null` and a missing header land here too. The owner's fallback is the
            # copy-prompt button plus `report evening --input`, documented in F3 ops.
            self._error(
                HTTPStatus.FORBIDDEN,
                "bad_origin",
                detail=f"origin {origin!r} is not {expected!r}",
            )
            return False
        return True

    def _read_body(self) -> bytes | None:
        limit = self.app.server_config.max_body_bytes
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != JSON_CONTENT_TYPE:
            self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "unsupported_media_type")
            return None
        if self.headers.get("Transfer-Encoding"):
            # A chunked body has no declared length, so the limit could not be enforced
            # before reading it.
            self._error(HTTPStatus.LENGTH_REQUIRED, "content_length_required")
            return None
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self._error(HTTPStatus.LENGTH_REQUIRED, "content_length_required")
            return None
        try:
            length = int(raw_length)
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "bad_content_length")
            return None
        if length <= 0:
            self._error(HTTPStatus.BAD_REQUEST, "empty_body")
            return None
        if length > limit:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body_too_large")
            return None
        try:
            body = self.rfile.read(length)
        except (socket.timeout, TimeoutError, OSError):
            self._error(HTTPStatus.REQUEST_TIMEOUT, "read_timeout")
            return None
        if len(body) != length:
            self._error(HTTPStatus.BAD_REQUEST, "short_body")
            return None
        return body

    def _submit(self, body: bytes) -> None:
        try:
            raw = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, "invalid_json", detail=str(exc)[:200])
            return
        try:
            payload = parse_evening_payload(raw)
        except ValueError as exc:
            # The message names the offending field and never echoes the value, so a
            # schema error cannot become a log of the owner's notes.
            self._error(HTTPStatus.BAD_REQUEST, "schema_invalid", detail=str(exc)[:200])
            return
        try:
            outcome = submit_evening(
                self.app.reports_config,
                payload,
                store=self.app.store,
                outbox=self.app.outbox,
            )
        except SubmitRejected as exc:
            self._error(HTTPStatus.BAD_REQUEST, exc.code, detail=exc.detail)
            return
        except (OSError, ValueError) as exc:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "storage_error", detail=str(exc)[:200])
            return

        # The revision and its job are already durable. Dispatch happens on the worker
        # thread, so a broken handler cannot turn a saved evening into a failed request.
        if outcome.changed:
            self.app.worker.notify()
        self._json(
            HTTPStatus.OK,
            {
                "day": outcome.day,
                "revision": outcome.revision,
                "changed": outcome.changed,
                "status": "pending" if outcome.changed else "unchanged",
            },
        )

    def _serve_report(self, day_text: str) -> None:
        try:
            day = date.fromisoformat(day_text)
        except ValueError:
            self._error(HTTPStatus.NOT_FOUND, "not_found")
            return
        root = self.app.reports_config.output_dir.resolve()
        target = root / f"{day.isoformat()}.html"
        if target.is_symlink():
            self._error(HTTPStatus.NOT_FOUND, "not_found", detail="symlink in output_dir")
            return
        try:
            resolved = target.resolve(strict=True)
        except (OSError, RuntimeError):
            self._error(HTTPStatus.NOT_FOUND, "not_found")
            return
        if not resolved.is_relative_to(root) or not resolved.is_file():
            self._error(HTTPStatus.NOT_FOUND, "not_found", detail="resolved outside output_dir")
            return
        body = resolved.read_bytes()
        self._respond(HTTPStatus.OK, body, "text/html; charset=utf-8")

    def _serve_revision(self, query: str) -> None:
        values = parse_qs(query, keep_blank_values=False)
        day_values = values.get("day") or []
        if len(day_values) != 1:
            self._error(HTTPStatus.BAD_REQUEST, "day_required")
            return
        try:
            day = date.fromisoformat(day_values[0])
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "bad_day")
            return
        try:
            payload = latest_revision_response(self.app.reports_config, day)
        except (OSError, ValueError) as exc:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "storage_error", detail=str(exc)[:200])
            return
        self._json(HTTPStatus.OK, payload)


class _ThreadingServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class ReportServer:
    """Wires the config, the revision store and the single outbox worker together."""

    def __init__(
        self,
        reports_config: ReportsConfig,
        server_config: ServerConfig,
        *,
        dispatcher: Dispatcher | None = None,
        jwks: JwksCache | None = None,
        logger: Callable[[str], None] | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        server_config.check_bind_gate()
        server_config.require_ready_for_mutation(reports_config.public_origin)
        self.reports_config = reports_config
        self.server_config = server_config
        self._logger = logger or (lambda message: None)
        self._now = now
        self.store = RevisionStore(reports_config.state_dir)
        self.outbox = Outbox(reports_config.state_dir, max_attempts=server_config.max_attempts)
        if dispatcher is None and server_config.dispatch_command:
            dispatcher = BoundedCommandDispatcher(
                server_config.dispatch_command,
                timeout_seconds=server_config.dispatch_timeout_seconds,
            )
        self.worker = OutboxWorker(self.outbox, dispatcher, on_event=self.log)
        if server_config.auth_mode == AUTH_ACCESS_JWT:
            assert server_config.access is not None  # guaranteed by check_bind_gate
            self.jwks = jwks or JwksCache(server_config.access.certs_url)
        else:
            self.jwks = jwks

        self.httpd = _ThreadingServer(
            (server_config.bind_host, server_config.port), ReportRequestHandler
        )
        self.httpd.app = self  # type: ignore[attr-defined]
        self._thread: threading.Thread | None = None

    def log(self, message: str) -> None:
        self._logger(message)

    def now(self) -> float | None:
        return self._now() if self._now else None

    @property
    def address(self) -> tuple[str, int]:
        host, port = self.httpd.server_address[:2]
        return str(host), int(port)

    @property
    def origin(self) -> str:
        host, port = self.address
        return f"http://{host}:{port}"

    def start(self) -> None:
        """Recover pending outbox work first: a restart must not lose a saved evening."""
        self.worker.start()
        self.worker.notify()
        self._thread = threading.Thread(
            target=self.httpd.serve_forever, name="report-http", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.worker.stop()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> ReportServer:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()


def build_server(
    reports_config: ReportsConfig,
    *,
    dispatcher: Dispatcher | None = None,
    jwks: JwksCache | None = None,
    logger: Callable[[str], None] | None = None,
    now: Callable[[], float] | None = None,
    overrides: dict[str, Any] | None = None,
) -> ReportServer:
    if reports_config.server is None:
        raise ValueError(
            "reports config has no 'server' block; F3 will not start without an explicit "
            "auth profile, so a public bind cannot happen by omission"
        )
    server_config = reports_config.server
    if overrides:
        from dataclasses import replace

        server_config = replace(server_config, **overrides)
        server_config.check_bind_gate()
    return ReportServer(
        reports_config,
        server_config,
        dispatcher=dispatcher,
        jwks=jwks,
        logger=logger,
        now=now,
    )
