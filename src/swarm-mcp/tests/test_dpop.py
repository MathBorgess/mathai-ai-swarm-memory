from mathai_swarm_mcp.dpop import access_token_hash, generate_private_jwk, jwk_thumbprint, proof, public_jwk
from mathai_swarm_mcp.origin import canonical_htu
import jwt


def test_thumbprint_is_public_only_and_stable():
    jwk = generate_private_jwk()
    assert jwk_thumbprint(jwk) == jwk_thumbprint(public_jwk(jwk))
    other = generate_private_jwk()
    assert jwk_thumbprint(jwk) != jwk_thumbprint(other)


def test_proof_header_has_public_jwk_and_new_jti_each_time():
    jwk = generate_private_jwk()
    url = "https://a2a.mathai.com.br/v1/oauth/token"
    first = proof(jwk, "POST", url, access_token="access_test")
    second = proof(jwk, "POST", url, access_token="access_test")
    header = jwt.get_unverified_header(first)
    assert header["typ"] == "dpop+jwt"
    assert header["alg"] == "ES256"
    assert "d" not in header["jwk"]
    assert jwt.get_unverified_header(second)["jwk"] == header["jwk"]
    claims = jwt.decode(first, options={"verify_signature": False})
    other = jwt.decode(second, options={"verify_signature": False})
    assert claims["jti"] != other["jti"]
    assert claims["htm"] == "POST"
    assert claims["htu"] == canonical_htu(url + "?ignored=1")
    assert claims["ath"] == access_token_hash("access_test")
    assert "ath" not in jwt.decode(proof(jwk, "GET", url), options={"verify_signature": False})
