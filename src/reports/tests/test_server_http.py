"""HTTP surface of the F3 server: routing, hardening, CSRF and body limits."""

from __future__ import annotations

import http.client
import json
from contextlib import contextmanager
from dataclasses import replace

import pytest

from swarm_reports.server.app import CONTENT_SECURITY_POLICY, ReportServer
from swarm_reports.server.outbox import Outbox
from swarm_reports.server.revisions import RevisionStore
from tests.conftest import DAY, payload_dict


@contextmanager
def running(reports_config, *, dispatcher=None, server_config=None):
    config = reports_config
    if server_config is not None:
        config = replace(reports_config, server=server_config)
    server = ReportServer(config, config.server, dispatcher=dispatcher)
    host, port = server.address
    # The Origin the browser will send is this server's own origin.
    config = replace(config, public_origin=f"http://{host}:{port}")
    server.reports_config = config
    server.start()
    try:
        yield server
    finally:
        server.stop()


def request(server, method, path, *, body=None, headers=None):
    host, port = server.address
    conn = http.client.HTTPConnection(host, port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


def post(server, body, *, origin=..., content_type="application/json", extra=None):
    headers = {}
    if content_type is not None:
        headers["Content-Type"] = content_type
    if origin is ...:
        origin = server.reports_config.public_origin
    if origin is not None:
        headers["Origin"] = origin
    raw = body if isinstance(body, (bytes, str)) else json.dumps(body)
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    headers["Content-Length"] = str(len(raw))
    headers.update(extra or {})
    return request(server, "POST", "/evening", body=raw, headers=headers)


# --------------------------------------------------------------------------------------
# Static serving
# --------------------------------------------------------------------------------------


def test_serves_the_generated_report_for_a_date(reports_config):
    with running(reports_config) as server:
        status, headers, body = request(server, "GET", f"/{DAY.isoformat()}.html")
    assert status == 200
    assert b"report" in body
    assert headers["Content-Type"].startswith("text/html")


def test_response_carries_secure_cache_and_csp_headers(reports_config):
    with running(reports_config) as server:
        _status, headers, _body = request(server, "GET", f"/{DAY.isoformat()}.html")
    assert headers["Cache-Control"] == "private, no-store, max-age=0"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
    # Inline style and script are allowed because the page has no external assets.
    assert "script-src 'unsafe-inline'" in headers["Content-Security-Policy"]
    assert "default-src 'none'" in headers["Content-Security-Policy"]


def test_cors_is_never_enabled(reports_config):
    with running(reports_config) as server:
        _status, headers, _body = request(
            server,
            "GET",
            f"/{DAY.isoformat()}.html",
            headers={"Origin": "https://evil.example"},
        )
    assert not [key for key in headers if key.lower().startswith("access-control-")]


def test_root_and_unknown_paths_do_not_list_a_directory(reports_config):
    with running(reports_config) as server:
        for path in ("/", "/index.html", f"/{DAY.isoformat()}", "/reports"):
            status, _headers, body = request(server, "GET", path)
            assert status == 404, path
            assert b"Directory listing" not in body
            assert DAY.isoformat().encode() not in body


@pytest.mark.parametrize(
    "path",
    [
        "/../../etc/passwd",
        "/..%2f..%2fetc%2fpasswd",
        "/%2e%2e%2f%2e%2e%2fetc%2fpasswd",
        "/%2e%2e/%2e%2e/etc/passwd",
        "/....//....//etc/passwd",
        "/2026-09-14.html/../../etc/passwd",
        "/%2f%2fetc%2fpasswd",
    ],
)
def test_traversal_and_encoded_traversal_are_rejected(reports_config, path):
    with running(reports_config) as server:
        status, _headers, body = request(server, "GET", path)
    assert status == 404
    assert b"root:" not in body


def test_symlink_escape_is_rejected(reports_config):
    secret = reports_config.state_dir / "reports-state.json"
    link = reports_config.output_dir / "2026-09-20.html"
    link.symlink_to(secret)
    with running(reports_config) as server:
        status, _headers, body = request(server, "GET", "/2026-09-20.html")
    assert status == 404
    assert b"frozen_checklist" not in body


def test_state_and_env_are_not_reachable(reports_config, tmp_path):
    (tmp_path / ".env").write_text("SECRET=hunter2\n", encoding="utf-8")
    with running(reports_config) as server:
        for path in (
            "/reports-state.json",
            "/.env",
            "/state/reports-state.json",
            "/evening/2026-09-14/revisions/0001.json",
        ):
            status, _headers, body = request(server, "GET", path)
            assert status in (400, 404), path
            assert b"hunter2" not in body
            assert b"frozen_checklist" not in body


def test_impossible_dates_are_not_served(reports_config):
    with running(reports_config) as server:
        status, _headers, _body = request(server, "GET", "/2026-13-45.html")
    assert status == 404


# --------------------------------------------------------------------------------------
# POST /evening
# --------------------------------------------------------------------------------------


def test_post_saves_a_revision(reports_config):
    with running(reports_config) as server:
        status, headers, body = post(server, payload_dict())
        assert status == 200
        assert headers["Cache-Control"] == "private, no-store, max-age=0"
        assert json.loads(body) == {
            "day": DAY.isoformat(),
            "revision": 1,
            "changed": True,
            "status": "pending",
        }
        assert RevisionStore(reports_config.state_dir).latest(DAY).revision == 1


def test_identical_post_reports_unchanged(reports_config):
    with running(reports_config) as server:
        post(server, payload_dict())
        status, _headers, body = post(server, payload_dict())
    assert status == 200
    assert json.loads(body)["changed"] is False


def test_post_is_acknowledged_even_when_the_worker_fails(reports_config):
    """The revision is durable before dispatch, so a broken handler is not a 500."""

    def broken(job):
        raise RuntimeError("handler is on fire")

    with running(reports_config, dispatcher=broken) as server:
        status, _headers, body = post(server, payload_dict())
        assert status == 200
        assert json.loads(body)["status"] == "pending"
        server.worker.drain_once()
    outbox = Outbox(reports_config.state_dir)
    surviving = outbox.pending() + outbox.failed()
    assert [job.revision for job in surviving] == [1]
    assert not outbox.is_done(surviving[0])
    assert "RuntimeError" in (surviving[0].last_error or "")
    assert RevisionStore(reports_config.state_dir).latest(DAY).revision == 1


@pytest.mark.parametrize("origin", ["https://evil.example", "null", "", None])
def test_mutation_requires_the_configured_origin(reports_config, origin):
    with running(reports_config) as server:
        status, _headers, body = post(server, payload_dict(), origin=origin)
    assert status == 403
    assert json.loads(body) == {"error": "bad_origin"}
    assert RevisionStore(reports_config.state_dir).latest(DAY) is None


def test_get_revision_does_not_need_an_origin(reports_config):
    """Reads are safe; only mutations carry the CSRF requirement."""
    with running(reports_config) as server:
        post(server, payload_dict(notes="x"))
        status, _headers, body = request(
            server, "GET", f"/evening/revision?day={DAY.isoformat()}"
        )
    assert status == 200
    parsed = json.loads(body)
    assert parsed["revision"] == 1
    assert parsed["payload"]["notes"] == "x"


def test_revision_url_matches_the_one_the_page_uses(reports_config):
    from swarm_reports.config import ReportsConfig

    assert isinstance(reports_config, ReportsConfig)
    assert reports_config.evening_revision_url(DAY) == f"/evening/revision?day={DAY.isoformat()}"
    assert reports_config.evening_post_url == "/evening"
    with running(reports_config) as server:
        status, _headers, _body = request(
            server, "GET", reports_config.evening_revision_url(DAY)
        )
    assert status == 200


def test_revision_endpoint_validates_the_day(reports_config):
    with running(reports_config) as server:
        assert request(server, "GET", "/evening/revision")[0] == 400
        assert request(server, "GET", "/evening/revision?day=nope")[0] == 400
        assert request(server, "GET", "/evening/revision?day=2026-09-14&day=2026-09-15")[0] == 400


def test_wrong_content_type_is_refused(reports_config):
    with running(reports_config) as server:
        status, _headers, body = post(
            server, payload_dict(), content_type="text/plain"
        )
    assert status == 415
    assert json.loads(body) == {"error": "unsupported_media_type"}


def test_missing_content_length_is_refused(reports_config):
    with running(reports_config) as server:
        host, port = server.address
        conn = http.client.HTTPConnection(host, port, timeout=10)
        try:
            conn.putrequest("POST", "/evening", skip_accept_encoding=True)
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Origin", server.reports_config.public_origin)
            conn.endheaders()
            response = conn.getresponse()
            status, body = response.status, response.read()
        finally:
            conn.close()
    assert status == 411
    assert json.loads(body) == {"error": "content_length_required"}


def test_chunked_body_is_refused(reports_config):
    with running(reports_config) as server:
        host, port = server.address
        conn = http.client.HTTPConnection(host, port, timeout=10)
        try:
            conn.putrequest("POST", "/evening", skip_accept_encoding=True)
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Origin", server.reports_config.public_origin)
            conn.putheader("Transfer-Encoding", "chunked")
            conn.endheaders()
            conn.send(b"2\r\n{}\r\n0\r\n\r\n")
            response = conn.getresponse()
            status, body = response.status, response.read()
        finally:
            conn.close()
    assert status == 411
    assert json.loads(body) == {"error": "content_length_required"}


def test_oversize_body_is_refused_before_reading(reports_config, server_config):
    tiny = replace(server_config, max_body_bytes=2048)
    with running(reports_config, server_config=tiny) as server:
        status, _headers, body = post(server, payload_dict(notes="x" * 3000))
    assert status == 413
    assert json.loads(body) == {"error": "body_too_large"}


def test_empty_body_is_refused(reports_config):
    with running(reports_config) as server:
        status, _headers, body = post(server, b"")
    assert status == 400
    assert json.loads(body) == {"error": "empty_body"}


def test_malformed_json_returns_400_without_a_traceback(reports_config):
    with running(reports_config) as server:
        status, _headers, body = post(server, b"{not json")
    assert status == 400
    assert json.loads(body) == {"error": "invalid_json"}
    assert b"Traceback" not in body
    assert b"swarm_reports" not in body


def test_schema_errors_return_400_without_echoing_the_value(reports_config):
    with running(reports_config) as server:
        status, _headers, body = post(
            server,
            payload_dict(
                notes="segredo do dono",
                checklist=[{"task_id": "MAT-1", "done": "false"}],
            ),
        )
    assert status == 400
    assert json.loads(body) == {"error": "schema_invalid"}
    assert b"segredo" not in body
    assert b"Traceback" not in body


@pytest.mark.parametrize(
    "body,expected",
    [
        (payload_dict(owner_id="attacker"), "owner_mismatch"),
        (payload_dict(day="2026-09-20", checklist=[]), "day_not_frozen"),
        (payload_dict(checklist=[{"task_id": "MAT-999", "done": True}]), "unknown_task_ids"),
    ],
)
def test_server_never_trusts_a_supplied_identity_or_day(reports_config, body, expected):
    with running(reports_config) as server:
        status, _headers, response = post(server, body)
    assert status == 400
    assert json.loads(response) == {"error": expected}


def test_unknown_post_route_is_404(reports_config):
    with running(reports_config) as server:
        status, _headers, _body = post(server, payload_dict())
        assert status == 200
        conn_status, _h, _b = request(
            server,
            "POST",
            "/evening/revision",
            body=b"{}",
            headers={
                "Content-Type": "application/json",
                "Content-Length": "2",
                "Origin": server.reports_config.public_origin,
            },
        )
    assert conn_status == 404
