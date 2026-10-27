"""Offline token validation rejects signed JWTs without mandatory claims."""
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from app.utils import keycloak_auth


@pytest.mark.unit
def test_signed_token_requires_exp_issuer_and_subject(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(keycloak_auth, "_get_realm_public_key_pem", lambda: key.public_key())
    monkeypatch.setattr(keycloak_auth.settings, "KEYCLOAK_TOKEN_ISSUER", "https://issuer.example/realms/test")
    monkeypatch.setattr(keycloak_auth.settings, "KEYCLOAK_TOKEN_AUDIENCE", "appstore-backend")
    monkeypatch.setattr(keycloak_auth.settings, "KEYCLOAK_ENFORCE_TOKEN_BINDING", True)
    claims = {
        "sub": "user-1", "iss": "https://issuer.example/realms/test",
        "exp": datetime.now(UTC) + timedelta(minutes=5),
        "aud": "appstore-backend",
    }
    token = jwt.encode(claims, key, algorithm="RS256")
    assert keycloak_auth.verify_keycloak_token_offline(token)["sub"] == "user-1"
    for missing in ("exp", "iss", "sub", "aud"):
        token = jwt.encode({k: v for k, v in claims.items() if k != missing}, key, algorithm="RS256")
        with pytest.raises(HTTPException) as exc:
            keycloak_auth.verify_keycloak_token_offline(token)
        assert exc.value.status_code == 401
    for changed in ({"iss": "https://other.example/realms/test"}, {"aud": "other-client"}):
        token = jwt.encode({**claims, **changed}, key, algorithm="RS256")
        with pytest.raises(HTTPException) as exc:
            keycloak_auth.verify_keycloak_token_offline(token)
        assert exc.value.status_code == 401
