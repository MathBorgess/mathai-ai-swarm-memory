from __future__ import annotations

import json

import pytest

from swarm_reports.discovery.checkpoints import SourceCheckpoint
from swarm_reports.discovery.sources.base import SourceError
from swarm_reports.discovery.sources.linear import DISCOVER_QUERY, LinearConfig, LinearSource


def test_query_is_read_only_no_mutation_string():
    assert "mutation" not in DISCOVER_QUERY.lower()
    assert "query" in DISCOVER_QUERY.lower()


def test_unconfigured_source_raises_not_silent_empty_success():
    config = LinearConfig()
    with pytest.raises(SourceError):
        LinearSource(config).discover(SourceCheckpoint())


def test_missing_token_env_raises():
    config = LinearConfig(token_env="LINEAR_TOKEN_DOES_NOT_EXIST_XYZ")
    with pytest.raises(SourceError):
        LinearSource(config).discover(SourceCheckpoint())


def test_token_adapter_never_fabricates_and_parses_real_shape(monkeypatch):
    monkeypatch.setenv("LINEAR_TOKEN_TEST", "secret-token-value")
    captured_headers = {}

    def fake_opener(url, headers, body, timeout):
        captured_headers.update(headers)
        payload = json.loads(body.decode("utf-8"))
        assert payload["query"] == DISCOVER_QUERY
        response = {
            "data": {
                "issues": {
                    "nodes": [
                        {
                            "id": "uuid-1",
                            "identifier": "MAT-999",
                            "title": "Real issue",
                            "url": "https://linear.app/x/issue/MAT-999",
                            "updatedAt": "2026-09-14T08:00:00.000Z",
                            "state": {"name": "In Progress"},
                        }
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        }
        from swarm_reports.discovery.sources.linear import HttpResponse

        return HttpResponse(200, json.dumps(response))

    config = LinearConfig(token_env="LINEAR_TOKEN_TEST", opener=fake_opener)
    result = LinearSource(config).discover(SourceCheckpoint())
    assert len(result.items) == 1
    assert result.items[0].id == "linear:MAT-999:2026-09-14T08:00:00.000Z"
    assert result.items[0].meta["identifier"] == "MAT-999"
    assert captured_headers["Authorization"] == "secret-token-value"


def test_token_value_never_appears_in_items_or_errors(monkeypatch):
    monkeypatch.setenv("LINEAR_TOKEN_TEST2", "super-secret-abc")

    def fake_opener(url, headers, body, timeout):
        from swarm_reports.discovery.sources.linear import HttpResponse

        return HttpResponse(401, "unauthorized")

    config = LinearConfig(token_env="LINEAR_TOKEN_TEST2", opener=fake_opener)
    with pytest.raises(SourceError) as excinfo:
        LinearSource(config).discover(SourceCheckpoint())
    assert "super-secret-abc" not in str(excinfo.value)


def test_command_adapter_reads_json_array_no_network(monkeypatch):
    def fake_runner(argv, timeout):
        return json.dumps(
            [
                {
                    "identifier": "MAT-42",
                    "title": "Via CLI",
                    "url": "https://linear.app/x/issue/MAT-42",
                    "updatedAt": "2026-09-14T07:00:00Z",
                    "state": {"name": "Done"},
                }
            ]
        )

    config = LinearConfig(command=("linear-cli", "list", "--json"), command_runner=fake_runner)
    result = LinearSource(config).discover(SourceCheckpoint())
    assert result.items[0].id == "linear:MAT-42:2026-09-14T07:00:00Z"


def test_command_adapter_rejects_non_array_json():
    config = LinearConfig(command=("x",), command_runner=lambda *a: json.dumps({"not": "array"}))
    with pytest.raises(SourceError):
        LinearSource(config).discover(SourceCheckpoint())


def test_cursor_filters_out_stale_issues():
    def fake_runner(argv, timeout):
        return json.dumps(
            [{"identifier": "MAT-1", "title": "old", "url": None, "updatedAt": "2026-09-01T00:00:00Z"}]
        )

    config = LinearConfig(command=("x",), command_runner=fake_runner)
    result = LinearSource(config).discover(SourceCheckpoint(cursor="2026-09-10T00:00:00Z"))
    assert result.items == []
