#!/usr/bin/env python
"""Generate the Microsoft Power Platform / Copilot Studio connector files from the app's own OpenAPI document.

    python integrations/microsoft/generate_swagger.py            # write the files next to this script
    python integrations/microsoft/generate_swagger.py --check    # exit 1 when the committed files are stale

Writes:
* ``postdelegate-rest.swagger.json``  OpenAPI 2.0 for the REST API (a custom connector's only accepted format),
  with an API-key security definition (the REST API takes a PostDelegate token, not an OAuth access token).
* ``postdelegate-mcp.swagger.json``   the MCP connector: one ``x-ms-agentic-protocol: mcp-streamable-1.0`` operation,
  with an OAuth 2.0 security definition pointing at the server's authorization endpoints.
* ``apiProperties.json``              connector properties (brand colour, OAuth settings placeholder).

The conversion handles what the Ninja-generated document uses (3.1 nullable ``anyOf``, ``$ref``, bodies, query and
path parameters). It is a converter for this API, not a general OpenAPI 3.1 to 2.0 tool.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
HOST = "app.postdelegate.com"
TITLE = "PostDelegate"
DESCRIPTION = (
    "Plan, draft, schedule and review social media posts across your connected channels, "
    "read analytics, and handle the inbox."
)
BRAND_COLOR = "#7357FF"
METHODS = ("get", "put", "post", "delete", "patch")
_DROP = {
    "title",
    "examples",
    "example",
    "$defs",
    "discriminator",
    "deprecated",
    "readOnly",
    "writeOnly",
    "contentMediaType",
}
_SUMMARY_MAX = 80


def _nullable(schema: dict) -> dict:
    schema["x-nullable"] = True
    return schema


def convert_schema(node):
    """OpenAPI 3.1 schema to a Swagger 2.0 schema."""
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        return {"$ref": node["$ref"].replace("#/components/schemas/", "#/definitions/")}
    out = {k: v for k, v in node.items() if k not in _DROP}
    for union in ("anyOf", "oneOf"):
        if union in out:
            variants = out.pop(union)
            real = [v for v in variants if v.get("type") != "null"]
            has_null = len(real) != len(variants)
            if len(real) == 1:
                merged = {**convert_schema(real[0]), **{k: v for k, v in out.items() if k not in ("type",)}}
            else:
                types = {v.get("type") for v in real}
                merged = {**{k: v for k, v in out.items()}}
                if len(types) == 1 and None not in types and "$ref" not in {k for v in real for k in v}:
                    merged.update(convert_schema(real[0]))
                else:
                    merged.setdefault("description", "Accepts more than one shape; see the API reference.")
            out = _nullable(merged) if has_null else merged
    if "allOf" in out:
        parts = [convert_schema(p) for p in out.pop("allOf")]
        out["allOf"] = parts
    if "const" in out:
        out["enum"] = [out.pop("const")]
    kind = out.get("type")
    if isinstance(kind, list):
        real = [t for t in kind if t != "null"]
        out["type"] = real[0] if real else "string"
        if len(real) != len(kind):
            _nullable(out)
    if out.get("type") == "null":
        out.pop("type")
        _nullable(out)
    if "properties" in out:
        out["properties"] = {k: convert_schema(v) for k, v in out["properties"].items()}
    if isinstance(out.get("items"), dict):
        out["items"] = convert_schema(out["items"])
    if isinstance(out.get("additionalProperties"), dict):
        out["additionalProperties"] = convert_schema(out["additionalProperties"])
    for key in ("exclusiveMinimum", "exclusiveMaximum"):
        # 3.1 makes these numbers; 2.0 wants a boolean next to minimum / maximum.
        if isinstance(out.get(key), int | float) and not isinstance(out[key], bool):
            out[key[len("exclusive") :].lower()] = out.pop(key)
    return out


def _clean_summary(text: str, fallback: str) -> str:
    """Power Platform summaries: 80 characters, letters, digits, spaces and parentheses only."""
    cleaned = re.sub(r"[^A-Za-z0-9() ]+", " ", text or fallback)
    cleaned = re.sub(r"\s+", " ", cleaned).strip() or fallback
    return cleaned[:_SUMMARY_MAX].rstrip()


def _operation_id(operation_id: str) -> str:
    """``apps_api_routers_posts_retrieve`` to ``PostsRetrieve``."""
    words = re.sub(r"^apps_api_routers_", "", operation_id).split("_")
    return "".join(w[:1].upper() + w[1:] for w in words if w)


def _parameter(param: dict) -> dict:
    schema = convert_schema(param.get("schema", {}))
    out = {"name": param["name"], "in": param["in"], "required": bool(param.get("required", False))}
    if param.get("description"):
        out["description"] = param["description"]
    for key in ("type", "format", "enum", "default", "minimum", "maximum", "minLength", "maxLength", "items"):
        if key in schema:
            out[key] = schema[key]
    if "type" not in out:
        out["type"] = "string"
    if out["type"] == "array" and "items" not in out:
        out["items"] = {"type": "string"}
    return out


def convert_operation(path: str, method: str, op: dict) -> dict:
    out: dict = {
        "operationId": _operation_id(op["operationId"]),
        "summary": _clean_summary(op.get("summary", ""), f"{method} {path}"),
        "x-ms-visibility": "important",
    }
    description = op.get("description")
    if description:
        out["description"] = description if description.rstrip().endswith((".", "!", "?")) else description + "."
    else:
        out["description"] = out["summary"] + "."
    params = [_parameter(p) for p in op.get("parameters", []) if p.get("in") in ("path", "query")]
    body = (op.get("requestBody") or {}).get("content", {}).get("application/json", {}).get("schema")
    if body is not None:
        params.append(
            {
                "name": "body",
                "in": "body",
                "required": bool(op["requestBody"].get("required")),
                "schema": convert_schema(body),
            }
        )
    if params:
        out["parameters"] = params
    ok = (op.get("responses") or {}).get("200", {})
    schema = ok.get("content", {}).get("application/json", {}).get("schema")
    response: dict = {"description": ok.get("description", "OK")}
    if schema is not None:
        response["schema"] = convert_schema(schema)
    out["responses"] = {"200": response}
    out["tags"] = op.get("tags", [])
    return out


def convert(openapi: dict) -> dict:
    """The REST swagger document."""
    paths: dict = {}
    for path, item in openapi["paths"].items():
        if path.startswith("/api/v1/mcp"):
            continue  # the MCP endpoint is its own connector
        converted = {m: convert_operation(path, m, item[m]) for m in METHODS if m in item}
        if converted:
            paths[path] = converted
    return {
        "swagger": "2.0",
        "info": {
            "title": TITLE,
            "description": DESCRIPTION,
            "version": "1.0.0",
            "contact": {"email": "support@postdelegate.com"},
        },
        "host": HOST,
        "basePath": "/",
        "schemes": ["https"],
        "consumes": ["application/json"],
        "produces": ["application/json"],
        "paths": paths,
        "definitions": {
            name: convert_schema(s) for name, s in openapi.get("components", {}).get("schemas", {}).items()
        },
        "securityDefinitions": {
            "api_key": {
                "type": "apiKey",
                "in": "header",
                "name": "Authorization",
                "description": "Enter Bearer, a space, then a PostDelegate token created under Organization, API keys.",
            }
        },
        "security": [{"api_key": []}],
    }


def mcp_swagger() -> dict:
    return {
        "swagger": "2.0",
        "info": {
            "title": TITLE,
            "description": "Let your agent plan, draft, schedule and review social posts in PostDelegate.",
            "version": "1.0.0",
            "contact": {"email": "support@postdelegate.com"},
        },
        "host": HOST,
        "basePath": "/",
        "schemes": ["https"],
        "paths": {
            "/api/v1/mcp": {
                "post": {
                    "summary": "PostDelegate social scheduling server",
                    "x-ms-agentic-protocol": "mcp-streamable-1.0",
                    "operationId": "InvokeMCP",
                    "responses": {"200": {"description": "Success"}},
                }
            }
        },
        "securityDefinitions": {
            "oauth2-auth": {
                "type": "oauth2",
                "flow": "accessCode",
                "authorizationUrl": f"https://{HOST}/oauth/authorize/",
                "tokenUrl": f"https://{HOST}/oauth/token/",
                "scopes": {"mcp": "Use PostDelegate tools on your behalf"},
            }
        },
        "security": [{"oauth2-auth": ["mcp"]}],
    }


def api_properties() -> dict:
    return {
        "properties": {
            "connectionParameters": {
                "token": {
                    "type": "oAuthSetting",
                    "oAuthSettings": {
                        "identityProvider": "oauth2",
                        "scopes": ["mcp"],
                        "redirectMode": "GlobalPerConnector",
                        "customParameters": {
                            "AuthorizationUrl": {"value": f"https://{HOST}/oauth/authorize/"},
                            "TokenUrl": {"value": f"https://{HOST}/oauth/token/"},
                            "RefreshUrl": {"value": f"https://{HOST}/oauth/token/"},
                        },
                        "clientId": "REPLACE_WITH_REGISTERED_CLIENT_ID",
                    },
                    "uiDefinition": None,
                }
            },
            "iconBrandColor": BRAND_COLOR,
            "capabilities": [],
            "publisher": "Nasida Apps LLC",
            "stackOwner": "Nasida Apps LLC",
        }
    }


def _load_openapi() -> dict:
    sys.path.insert(0, str(ROOT))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")
    import django

    django.setup()
    from apps.api.api import api

    return api.get_openapi_schema(path_prefix="/api/v1/")


def outputs(openapi: dict) -> dict[str, dict]:
    return {
        "postdelegate-rest.swagger.json": convert(openapi),
        "postdelegate-mcp.swagger.json": mcp_swagger(),
        "apiProperties.json": api_properties(),
    }


def main(argv: list[str]) -> int:
    check = "--check" in argv
    stale = 0
    for name, doc in outputs(_load_openapi()).items():
        text = json.dumps(doc, indent=2, sort_keys=False) + "\n"
        target = HERE / name
        if check:
            if not target.exists() or target.read_text() != text:
                print(f"stale: {target}", file=sys.stderr)
                stale += 1
        else:
            target.write_text(text)
            print(f"wrote {target} ({len(text)} bytes)")
    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
