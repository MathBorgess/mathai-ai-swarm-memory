from mathai_swarm_mcp.errors import redact


def test_redact_tokens_jwts_and_private_fields():
    jwt = "eyJhbGciOiJFUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0In0.signaturepartvalueshere"
    text = redact(
        'Authorization: DPoP supersecretvalue access_token="s3cretvalue" refresh_token=refreshsecret '
        "device_code=devicesecret "
        + jwt
        + ' {"access_token":"abcsecret","d":"privateexponent"}'
    )
    assert "supersecretvalue" not in text
    assert "s3cretvalue" not in text
    assert "refreshsecret" not in text
    assert "devicesecret" not in text
    assert jwt not in text
    assert "privateexponent" not in text
    assert "[redacted]" in text
