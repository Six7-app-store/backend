"""Unit-Tests fuer die generierte OpenAPI-Beschreibung (``/docs``, ``/openapi.json``).

Die Doku entsteht aus dem Code. Diese Tests halten die Regeln fest, die sie
lesbar und fuer Client-Generatoren brauchbar halten.
"""
from __future__ import annotations

import pytest

from app.api_docs import OPENAPI_TAGS
from app.main import app

HTTP_METHODS = {"get", "post", "put", "patch", "delete"}


def _operations():
    schema = app.openapi()
    for path, item in schema["paths"].items():
        for method, operation in item.items():
            if method in HTTP_METHODS:
                yield f"{method.upper()} {path}", operation


@pytest.mark.unit
def test_every_operation_is_described() -> None:
    """Jeder Endpunkt hat einen Docstring, der in Swagger als Beschreibung erscheint."""
    missing = [name for name, op in _operations() if not op.get("description")]
    assert missing == []


@pytest.mark.unit
def test_every_operation_has_exactly_one_declared_tag() -> None:
    """Ein Tag pro Endpunkt, und nur Tags mit Beschreibung in ``OPENAPI_TAGS``."""
    declared = {tag["name"] for tag in OPENAPI_TAGS}
    wrong = [
        (name, op.get("tags"))
        for name, op in _operations()
        if len(op.get("tags", [])) != 1 or op["tags"][0] not in declared
    ]
    assert wrong == []


@pytest.mark.unit
def test_operation_ids_are_unique() -> None:
    """Doppelte operationIds zerlegen generierte Clients."""
    ids = [op["operationId"] for _, op in _operations()]
    assert len(ids) == len(set(ids))


@pytest.mark.unit
def test_bearer_scheme_is_documented() -> None:
    scheme = app.openapi()["components"]["securitySchemes"]["HTTPBearer"]
    assert scheme["bearerFormat"] == "JWT"
    assert scheme.get("description")
