"""Tests for devoks_mcp_management.app (TASK-009).

Traces: AC-001-1, AC-001-3, AC-001-4, AC-002-3, AC-002-4, CTR-001, CTR-006,
EDGE-011, DSN-007.

Every HTTP-level assertion here goes through the ASGI transport
(``httpx2.ASGITransport``), never the in-memory ``Client(mcp)`` — the
in-memory client bypasses the SDK's HTTP auth/transport-security middleware
entirely (FRD §7), so it cannot exercise 401/403/421 at all.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx2
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette
from starlette.routing import Mount, Route

from devoks_mcp_management import app as app_module
from devoks_mcp_management.app import create_app, create_app_from_env
from devoks_mcp_management.config import ClientToken, Settings
from devoks_mcp_management.server import SERVER_NAME

_ALLOWED_HOST = "mcp.example.com"
_PUBLIC_URL = "https://mcp.example.com/mcp"
_VALID_TOKEN = "secret-token"  # noqa: S105 -- test fixture literal, not a real credential


def _settings(
    *,
    allowed_hosts: tuple[str, ...] = (_ALLOWED_HOST,),
    public_url: str = _PUBLIC_URL,
    client_tokens: dict[str, ClientToken] | None = None,
    log_level: str = "INFO",
    stateless_http: bool = True,
    json_response: bool = True,
) -> Settings:
    # Direct dataclass construction, not load_settings() -- same pattern
    # test_server.py already uses for HTTP/wiring tests that never touch
    # github_app_private_key content, so no PEM fixture is needed here.
    return Settings(
        allowed_hosts=allowed_hosts,
        public_url=public_url,
        issuer_url="https://issuer.example.com",
        repo_allowlist=frozenset({"ridsync/devoks-mcp-servers"}),
        role_tools={
            "reader": frozenset({"list_repos", "get_repo_tree", "read_file", "search_code"})
        },
        github_app_id="app-id",
        github_app_installation_id="install-id",
        port=8000,
        log_level=log_level,
        read_file_max_bytes=262_144,
        search_code_max_results=30,
        token_refresh_leeway_seconds=300,
        stateless_http=stateless_http,
        json_response=json_response,
        client_tokens=client_tokens
        if client_tokens is not None
        else {_VALID_TOKEN: ClientToken(client_id="c1", role="reader", scopes=("devoks:read",))},
        github_app_private_key="unused-in-app-tests",
    )


def _generate_pem() -> str:
    """A throwaway RSA key generated in-process (mirrors test_config.py) -- only
    needed by the create_app_from_env() tests, which go through load_settings()."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem.decode("utf-8")


@pytest.fixture(scope="module")
def pem() -> str:
    return _generate_pem()


def _valid_env(pem_value: str) -> dict[str, str]:
    return {
        "MCP_ALLOWED_HOSTS": _ALLOWED_HOST,
        "MCP_PUBLIC_URL": _PUBLIC_URL,
        "MCP_ISSUER_URL": "https://issuer.example.com",
        "MCP_CLIENT_TOKENS": json.dumps(
            {
                _VALID_TOKEN: {
                    "client_id": "claude-code",
                    "role": "reader",
                    "scopes": ["devoks:read"],
                }
            }
        ),
        "MCP_REPO_ALLOWLIST": "ridsync/devoks-mcp-servers",
        "MCP_ROLE_TOOLS": json.dumps(
            {"reader": ["list_repos", "get_repo_tree", "read_file", "search_code"]}
        ),
        "GITHUB_APP_ID": "123456",
        "GITHUB_APP_PRIVATE_KEY": pem_value,
        "GITHUB_APP_INSTALLATION_ID": "789012",
    }


@asynccontextmanager
async def _running_client(
    app: Starlette, *, base_url: str = f"https://{_ALLOWED_HOST}"
) -> AsyncGenerator[httpx2.AsyncClient]:
    """Drive the app's real ASGI lifespan, then hand back an HTTP client.

    ``mcp.streamable_http_app()``'s own lifespan is dead once mounted (see
    app.py's module docstring) -- ``app.router.lifespan_context`` is exactly
    the ``lifespan()`` function ``create_app`` builds and hands to
    ``Starlette(lifespan=...)``, so entering it here reproduces what a real
    ASGI server's startup event does, session-manager task group included.
    """
    transport = httpx2.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(transport=transport, base_url=base_url) as client,
    ):
        yield client


def _iter_routes(app: Starlette, prefix: str = "") -> list[tuple[str, frozenset[str] | None]]:
    """Flatten ``app.routes`` (including one level of ``Mount``) to (path, methods)."""
    flat: list[tuple[str, frozenset[str] | None]] = []
    for route in app.routes:
        if isinstance(route, Mount):
            sub = route.app
            for sub_route in getattr(sub, "routes", []):
                assert isinstance(sub_route, Route)
                methods = frozenset(sub_route.methods) if sub_route.methods else None
                flat.append((prefix + route.path + sub_route.path, methods))
        elif isinstance(route, Route):
            methods = frozenset(route.methods) if route.methods else None
            flat.append((prefix + route.path, methods))
    return flat


# --- AC-001-3: /healthz -------------------------------------------------------


async def test_healthz_returns_200_with_name_and_version_no_auth() -> None:
    app = create_app(_settings())

    async with _running_client(app) as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    # Public, unauthenticated endpoint (AC-001-3) -- must expose nothing
    # beyond identity, never a Settings value or secret.
    assert body == {"name": SERVER_NAME, "version": body["version"]}
    assert set(body.keys()) == {"name", "version"}
    assert body["version"]  # non-empty


async def test_healthz_reachable_without_authorization_header() -> None:
    app = create_app(_settings())

    async with _running_client(app) as client:
        # Deliberately no Authorization header at all.
        response = await client.get("/healthz")

    assert response.status_code == 200


# --- AC-002-2 / AC-002-3: /mcp without auth -----------------------------------


async def test_mcp_post_without_authorization_returns_401_with_resource_metadata_pointer() -> None:
    app = create_app(_settings())

    async with _running_client(app) as client:
        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )

    assert response.status_code == 401
    www_authenticate = response.headers.get("www-authenticate", "")
    assert 'error="invalid_token"' in www_authenticate
    assert (
        f'resource_metadata="https://{_ALLOWED_HOST}/.well-known/oauth-protected-resource/mcp"'
        in www_authenticate
    )


# --- AC-002-4 / CTR-001: RFC 9728 metadata document ---------------------------


async def test_well_known_protected_resource_metadata_resource_matches_public_url_exactly() -> None:
    app = create_app(_settings(public_url=_PUBLIC_URL))

    async with _running_client(app) as client:
        response = await client.get("/.well-known/oauth-protected-resource/mcp")

    assert response.status_code == 200
    body = response.json()
    # Exact-match, including the absence of a trailing slash -- see app.py's
    # module docstring: a trailing slash on MCP_PUBLIC_URL would move this
    # document to a *different* path than CTR-001's fixed one.
    assert body["resource"] == _PUBLIC_URL
    assert not _PUBLIC_URL.endswith("/mcp/")


async def test_well_known_metadata_lists_configured_issuer_and_required_scope() -> None:
    app = create_app(_settings())

    async with _running_client(app) as client:
        response = await client.get("/.well-known/oauth-protected-resource/mcp")

    body = response.json()
    assert body["authorization_servers"] == ["https://issuer.example.com"]
    assert body["scopes_supported"] == ["devoks:read"]


# --- AC-001-4: disallowed Host -------------------------------------------------


async def test_mcp_post_with_disallowed_host_returns_421() -> None:
    app = create_app(_settings())

    async with _running_client(app) as client:
        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={
                "Authorization": f"Bearer {_VALID_TOKEN}",
                "Host": "evil.example.com",
                "Accept": "application/json, text/event-stream",
            },
        )

    assert response.status_code == 421


async def test_mcp_post_with_allowed_host_including_port_succeeds_via_wildcard_expansion() -> None:
    # DSN-007 / CTR-006: create_app expands a bare configured host to both
    # the portless form and the "<host>:*" wildcard -- this is the wildcard
    # half. A bare `Host: mcp.example.com` (no port) is covered by every
    # other test in this module already succeeding.
    app = create_app(_settings())

    async with _running_client(app, base_url=f"https://{_ALLOWED_HOST}:8443") as client:
        response = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2026-07-28",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0"},
                },
            },
            headers={
                "Authorization": f"Bearer {_VALID_TOKEN}",
                "Accept": "application/json, text/event-stream",
            },
        )

    assert response.status_code != 421
    assert response.status_code == 200


# --- EDGE-011: allowed Host, disallowed Origin --------------------------------


async def test_mcp_post_with_allowed_host_and_disallowed_origin_returns_403() -> None:
    app = create_app(_settings())

    async with _running_client(app) as client:
        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={
                "Authorization": f"Bearer {_VALID_TOKEN}",
                "Origin": "https://evil.example.com",
                "Accept": "application/json, text/event-stream",
            },
        )

    assert response.status_code == 403


# --- CTR-001: exact route surface, no /mcp/mcp duplication --------------------


def test_routes_expose_exactly_ctr_001_paths_without_duplication() -> None:
    app = create_app(_settings())

    paths = {path for path, _methods in _iter_routes(app)}

    assert paths == {
        "/healthz",
        "/mcp",
        "/.well-known/oauth-protected-resource/mcp",
    }
    # The failure mode this guards: Mount("/mcp", app=mcp.streamable_http_app())
    # instead of Mount("/", ...) would double the prefix.
    assert "/mcp/mcp" not in paths
    assert "/mcp/.well-known/oauth-protected-resource/mcp" not in paths


def test_healthz_route_precedes_mcp_mount_in_route_list() -> None:
    # Starlette matches routes in list order and Mount("/") matches every
    # path -- /healthz must be listed first or it is unreachable.
    app = create_app(_settings())

    healthz_index: int | None = None
    mount_index: int | None = None
    for index, route in enumerate(app.routes):
        if isinstance(route, Mount) and mount_index is None:
            mount_index = index
        elif isinstance(route, Route) and route.path == "/healthz":
            healthz_index = index

    assert healthz_index is not None
    assert mount_index is not None
    assert healthz_index < mount_index


# --- Lifespan actually runs (TASK-023 precondition) ---------------------------


async def test_mcp_endpoint_fails_without_lifespan_having_run() -> None:
    # Negative control for the next test: prove the session manager truly
    # needs create_app's lifespan, not some other automatic mechanism.
    app = create_app(_settings())
    transport = httpx2.ASGITransport(app=app)

    async with httpx2.AsyncClient(
        transport=transport, base_url=f"https://{_ALLOWED_HOST}"
    ) as client:
        with pytest.raises(RuntimeError, match="Task group is not initialized"):
            await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2026-07-28",
                        "capabilities": {},
                        "clientInfo": {"name": "t", "version": "0"},
                    },
                },
                headers={
                    "Authorization": f"Bearer {_VALID_TOKEN}",
                    "Accept": "application/json, text/event-stream",
                },
            )


async def test_mcp_endpoint_succeeds_once_lifespan_has_started() -> None:
    # Positive counterpart: with create_app's lifespan actually entered
    # (via _running_client), the same request that raises above now
    # completes -- proving mcp.session_manager.run() is really wired into
    # the host app's lifespan (this is the exact regression the SDK's own
    # "Add to an existing app" doc warns about under mounting).
    app = create_app(_settings())

    async with _running_client(app) as client:
        response = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2026-07-28",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0"},
                },
            },
            headers={
                "Authorization": f"Bearer {_VALID_TOKEN}",
                "Accept": "application/json, text/event-stream",
            },
        )

    assert response.status_code == 200
    assert "devoks-management-mcp" in response.text


# --- Factory shape: no import-time side effects, independent instances -------


def test_create_app_builds_independent_instances_from_different_settings() -> None:
    app_a = create_app(
        _settings(public_url="https://a.example.com/mcp", allowed_hosts=("a.example.com",))
    )
    app_b = create_app(
        _settings(public_url="https://b.example.com/mcp", allowed_hosts=("b.example.com",))
    )

    assert app_a is not app_b


def test_importing_app_module_has_no_side_effects_without_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in (
        "MCP_ALLOWED_HOSTS",
        "MCP_PUBLIC_URL",
        "MCP_ISSUER_URL",
        "MCP_CLIENT_TOKENS",
        "MCP_ROLE_TOOLS",
        "GITHUB_APP_ID",
        "GITHUB_APP_PRIVATE_KEY",
        "GITHUB_APP_INSTALLATION_ID",
    ):
        monkeypatch.delenv(key, raising=False)

    importlib.reload(app_module)  # must not raise


# --- create_app_from_env: the uvicorn --factory entry point -------------------


def test_create_app_from_env_builds_app_from_injected_mapping(pem: str) -> None:
    app = create_app_from_env(_valid_env(pem))

    assert isinstance(app, Starlette)
    paths = {path for path, _methods in _iter_routes(app)}
    assert "/healthz" in paths
    assert "/mcp" in paths
