"""The Power Platform connector files are generated from the live OpenAPI document; keep them valid and current."""

import importlib.util
import json
import re
from pathlib import Path

import pytest

DIR = Path(__file__).resolve().parent.parent / "integrations" / "microsoft"


@pytest.fixture(scope="module")
def generated():
    spec = importlib.util.spec_from_file_location("generate_swagger", DIR / "generate_swagger.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from apps.api.api import api

    return module, module.outputs(api.get_openapi_schema(path_prefix="/api/v1/"))


def _walk(node):
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


@pytest.mark.django_db
def test_committed_files_match_the_current_api(generated):
    _, docs = generated
    for name, doc in docs.items():
        assert (DIR / name).read_text() == json.dumps(doc, indent=2) + "\n", f"{name} is stale: run generate_swagger.py"


@pytest.mark.django_db
def test_rest_swagger_is_two_point_zero_and_self_consistent(generated):
    _, docs = generated
    doc = docs["postdelegate-rest.swagger.json"]
    assert doc["swagger"] == "2.0" and doc["schemes"] == ["https"]
    assert len(doc["info"]["title"]) <= 30
    assert 30 < len(doc["info"]["description"]) < 500
    refs = {n["$ref"] for n in _walk(doc) if isinstance(n, dict) and "$ref" in n}
    assert refs
    for ref in refs:
        assert ref.startswith("#/definitions/") and ref.split("/")[-1] in doc["definitions"], ref
    assert not any(isinstance(n, dict) and ("anyOf" in n or "oneOf" in n) for n in _walk(doc))
    assert '"null"' not in json.dumps(doc)
    ids = [op["operationId"] for item in doc["paths"].values() for op in item.values()]
    assert ids and len(ids) == len(set(ids))
    for item in doc["paths"].values():
        for op in item.values():
            assert re.fullmatch(r"[A-Za-z0-9() ]{1,80}", op["summary"]), op["summary"]
            assert op["description"].endswith((".", "!", "?"))
            assert sum(p["in"] == "body" for p in op.get("parameters", [])) <= 1
    assert not any(p.startswith("/api/v1/mcp") for p in doc["paths"])


@pytest.mark.django_db
def test_rest_connector_uses_the_api_key_header(generated):
    _, docs = generated
    scheme = docs["postdelegate-rest.swagger.json"]["securityDefinitions"]["api_key"]
    assert (scheme["type"], scheme["in"], scheme["name"]) == ("apiKey", "header", "Authorization")


@pytest.mark.django_db
def test_mcp_connector_declares_streamable_mcp_and_oauth(generated):
    _, docs = generated
    doc = docs["postdelegate-mcp.swagger.json"]
    operation = doc["paths"]["/api/v1/mcp"]["post"]
    assert operation["x-ms-agentic-protocol"] == "mcp-streamable-1.0"
    oauth = doc["securityDefinitions"]["oauth2-auth"]
    assert oauth["flow"] == "accessCode"
    assert oauth["authorizationUrl"].endswith("/oauth/authorize/") and oauth["tokenUrl"].endswith("/oauth/token/")
    assert "mcp" in oauth["scopes"]
