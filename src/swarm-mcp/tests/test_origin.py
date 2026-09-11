import pytest

from mathai_swarm_mcp.errors import OriginError
from mathai_swarm_mcp.origin import assert_same_origin, parse_origin, validate_github_verification_uri


def test_origin_must_be_https_without_path():
    assert parse_origin("https://a2a.mathai.com.br/") == "https://a2a.mathai.com.br"
    with pytest.raises(OriginError):
        parse_origin("http://a2a.mathai.com.br")
    with pytest.raises(OriginError):
        parse_origin("https://a2a.mathai.com.br/v1")
    with pytest.raises(OriginError):
        parse_origin("https://user:pass@a2a.mathai.com.br")


def test_wrong_origin_is_rejected():
    origin = "https://a2a.mathai.com.br"
    with pytest.raises(OriginError):
        assert_same_origin(origin, "https://evil.example/v1/context/query")
    with pytest.raises(OriginError):
        assert_same_origin(origin, "http://a2a.mathai.com.br/v1/context/query")


def test_github_device_uri_allowlist():
    assert validate_github_verification_uri("https://github.com/login/device")
    assert validate_github_verification_uri("https://github.com/login/device?user_code=WD4X-T7NK")
    with pytest.raises(OriginError):
        validate_github_verification_uri("http://github.com/login/device")
    with pytest.raises(OriginError):
        validate_github_verification_uri("https://evil.example/login/device")
    with pytest.raises(OriginError):
        validate_github_verification_uri("https://github.com.evil.example/login/device")
    with pytest.raises(OriginError):
        validate_github_verification_uri("https://github.com/login/oauth/authorize")
    with pytest.raises(OriginError):
        validate_github_verification_uri("https://github.com/login/device/../../evil")
