"""HTTP-layer auth success path + insufficient_scope + RFC 9728 + middleware order (TASK-010).

Traces: AC-001-1, AC-001-4, AC-002-1, AC-002-2, AC-002-3, AC-002-4, AC-002-5,
EDGE-010, EDGE-011.

``test_app.py`` (TASK-009) already covers 401 (missing `Authorization`), 421
(disallowed Host, *with* a valid token), 403 (disallowed Origin), and the
`resource` field of the RFC 9728 document matching ``MCP_PUBLIC_URL`` exactly
-- none of that is repeated here. This file fills the four gaps TASK-009 left
open: a full MCP protocol handshake actually succeeding over HTTP with a
valid token, 403 ``insufficient_scope`` as a *distinct* rejection from 401
``invalid_token``, the RFC 9728 document's full field set (not just
``resource``), and the auth-before-transport-security middleware ordering.

Every HTTP-level assertion here goes through the ASGI transport
(``httpx2.ASGITransport``), never the in-memory ``Client(mcp)`` -- the
in-memory client bypasses the SDK's HTTP auth/transport-security middleware
entirely (FRD §7).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx2
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette

from conftest import make_settings
from devoks_mcp_management.app import create_app
from devoks_mcp_management.config import ClientToken, Settings
from devoks_mcp_management.server import REQUIRED_SCOPES, SERVER_NAME

_ALLOWED_HOST = "mcp.example.com"
_PUBLIC_URL = "https://mcp.example.com/mcp"
_VALID_TOKEN = "test-fixture-token-not-a-real-credential"  # noqa: S105
_NO_SCOPE_TOKEN = "test-fixture-token-without-any-scopes-xx"  # noqa: S105


def _settings(
    *,
    client_tokens: dict[str, ClientToken] | None = None,
    stateless_http: bool = True,
    json_response: bool = True,
) -> Settings:
    # Direct dataclass construction, not load_settings() -- same pattern
    # test_app.py already uses for HTTP/wiring tests that never touch
    # github_app_private_key content. Duplicated here rather than imported
    # from test_app.py (test modules don't import each other) or extracted
    # to conftest.py -- see the handover notes for why extraction was
    # skipped for this task.
    return make_settings(
        stateless_http=stateless_http,
        json_response=json_response,
        client_tokens=client_tokens
        if client_tokens is not None
        else {_VALID_TOKEN: ClientToken(client_id="c1", role="reader", scopes=("devoks:read",))},
    )


@asynccontextmanager
async def _running_client(app: Starlette) -> AsyncGenerator[httpx2.AsyncClient]:
    """Drive the app's real ASGI lifespan, then hand back an HTTP client.

    Mirrors ``test_app.py``'s helper of the same name/shape -- see that
    module's docstring for why entering ``app.router.lifespan_context``
    (rather than relying on ``mcp.streamable_http_app()``'s own, now-dead
    lifespan) is required before any ``/mcp`` request will succeed.
    """
    transport = httpx2.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(transport=transport, base_url=f"https://{_ALLOWED_HOST}") as client,
    ):
        yield client


# --- AC-001-1 / AC-002-1: full protocol handshake succeeds with a valid token -


async def test_authenticated_client_initialize_then_list_tools_succeeds_end_to_end() -> None:
    """The actual gap TASK-009 left open: does a *real* MCP client, driven
    through the SDK's own ``ClientSession``, complete ``initialize`` ->
    ``notifications/initialized`` -> ``tools/list`` successfully over HTTP
    with a valid Bearer token?

    ``ClientSession`` is bound to ``streamable_http_client``'s stream pair,
    itself fed an ``httpx2.AsyncClient`` wired to ``ASGITransport`` -- so the
    handshake is driven exactly as a real MCP client over HTTP would drive
    it, while still exercising this process's auth middleware (unlike the
    in-memory ``Client(mcp)``, which bypasses HTTP entirely -- FRD §7). This
    was tried first per the handover notes and works; no hand-assembled
    JSON-RPC/header combination was needed.

    ``tools/list`` returning the 4 GitHub tools (TASK-022 wired
    ``_ADAPTER_REGISTRARS`` in ``tools/registry.py``; this was ``[]`` before
    that task) is secondary here -- the point of this test is that the
    request completed rather than being rejected by auth. AC-002-1's
    "handler can look up client_id/scopes" half is exercised at the unit
    level in test_verifier.py/test_guard.py; calling a tool's body
    end-to-end over real HTTP (which would need a live/mocked GitHub
    backend) is TASK-024's job, not this file's.
    """
    app = create_app(_settings())

    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url=f"https://{_ALLOWED_HOST}",
            headers={"Authorization": f"Bearer {_VALID_TOKEN}"},
        ) as http_client,
        streamable_http_client("/mcp", http_client=http_client) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        init_result = await session.initialize()
        tools_result = await session.list_tools()

    assert init_result.server_info.name == SERVER_NAME
    assert {tool.name for tool in tools_result.tools} == {
        "list_repos",
        "get_repo_tree",
        "read_file",
        "search_code",
    }


# --- AC-002-5 / EDGE-010: insufficient_scope is a distinct 403, not a 401 ----


async def test_valid_token_missing_required_scope_returns_403_insufficient_scope() -> None:
    """A token that verifies fine (unlike AC-002-2's 401 `invalid_token`,
    already covered by test_app.py) but lacks CTR-002's required scope must
    be rejected with **403** `insufficient_scope`, not 401 -- a different
    status code and a different `WWW-Authenticate` error, asserted here as
    two separate facts so a future change collapsing them back to 401 fails
    loudly. A plain httpx2 POST (not `ClientSession`) is enough: the
    rejection happens on the very first `initialize` POST, so there is no
    handshake to drive.
    """
    app = create_app(
        _settings(
            client_tokens={_NO_SCOPE_TOKEN: ClientToken(client_id="c2", role="reader", scopes=())}
        )
    )

    async with _running_client(app) as client:
        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={
                "Authorization": f"Bearer {_NO_SCOPE_TOKEN}",
                "Accept": "application/json, text/event-stream",
            },
        )

    assert response.status_code == 403
    assert response.status_code != 401
    www_authenticate = response.headers.get("www-authenticate", "")
    assert 'error="insufficient_scope"' in www_authenticate
    assert REQUIRED_SCOPES[0] in www_authenticate
    assert response.json()["error"] == "insufficient_scope"


# --- AC-002-3 / AC-002-4: RFC 9728 document, full field set -----------------


async def test_well_known_metadata_returns_full_rfc9728_document() -> None:
    """The full RFC 9728 envelope, not just `resource` -- test_app.py already
    pins `resource`'s exact match to `MCP_PUBLIC_URL` and separately checks
    `authorization_servers`/`scopes_supported`. This is a whole-object
    equality assertion instead of field-by-field: it also catches an
    unexpected extra or missing key, and is the only place
    `bearer_methods_supported` (how a client is told to actually send the
    token) gets asserted anywhere in the suite.
    """
    app = create_app(_settings())

    async with _running_client(app) as client:
        response = await client.get("/.well-known/oauth-protected-resource/mcp")

    assert response.status_code == 200
    assert response.json() == {
        "resource": _PUBLIC_URL,
        "authorization_servers": ["https://issuer.example.com"],
        "scopes_supported": list(REQUIRED_SCOPES),
        "bearer_methods_supported": ["header"],
    }


# --- AC-001-4: middleware order -- auth runs before transport security ------


async def test_missing_token_with_disallowed_host_returns_401_not_421() -> None:
    """Middleware-order regression, pinned per FRD's AC-001-4 quote block:
    the SDK stack runs `bearer_auth` before `transport_security`, so an
    *unauthenticated* request to a disallowed Host gets 401 `invalid_token`,
    never 421 -- 421 only appears once a valid token is also present
    (test_app.py's `test_mcp_post_with_disallowed_host_returns_421` already
    sends a valid token together with the bad Host, covering that half).

    This ordering is deliberate, not a bug to silently "fix" if it changes:
    telling an unauthenticated caller "your Host is wrong" before checking
    who they are would leak topology/config information to a caller whose
    identity was never established, so 401-first is the safer order. This
    test exists so a future SDK upgrade that reorders the middleware stack
    fails loudly here instead of silently changing that safety property.
    """
    app = create_app(_settings())

    async with _running_client(app) as client:
        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={
                "Host": "evil.example.com",
                "Accept": "application/json, text/event-stream",
            },
        )

    assert response.status_code == 401
    assert response.status_code != 421
    assert 'error="invalid_token"' in response.headers.get("www-authenticate", "")


# --- CTR-011 / EDGE-019: protocol mode is what makes Lambda deployable (TASK-055) ---
#
# Measured, not assumed. The four combinations below were probed against the
# installed mcp==2.1.1 before these assertions were written, and they are
# fully orthogonal:
#
#   stateless_http -> controls whether `Mcp-Session-Id` is issued at all
#   json_response  -> controls the response media type (JSON vs SSE stream)
#
# The legacy `2025-11-25` leg is used deliberately: it is the only leg that
# issues session IDs (FRD §7's protocol-leg table), so it is the only leg on
# which `stateless_http` is observable. On `2026-07-28` no session is issued
# regardless of the flag, which would make the test pass for the wrong reason.
# (That leg also requires `params._meta` to carry the protocol-version
# marker; a hand-rolled POST without it is rejected -32602, which is why the
# full-handshake test below drives `ClientSession` instead of raw JSON.)

_LEGACY_PROTOCOL_VERSION = "2025-11-25"

_INITIALIZE_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": _LEGACY_PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "task-055-probe", "version": "0"},
    },
}


@pytest.mark.parametrize(
    ("stateless_http", "json_response", "expected_media_type", "expects_session_id"),
    [
        # The Lambda-required combination (CTR-011 defaults): a single
        # complete JSON body and no session for a later invocation to lose.
        (True, True, "application/json", False),
        # The sticky-load-balancer combination (FRD §7 option (a)).
        (False, False, "text/event-stream", True),
        # Both off-diagonals, asserted so a future change that collapses the
        # two flags into one knob fails loudly instead of quietly coupling
        # session issuance to the media type.
        (True, False, "text/event-stream", False),
        (False, True, "application/json", True),
    ],
)
async def test_protocol_mode_flags_control_session_and_media_type_independently(
    stateless_http: bool,
    json_response: bool,
    expected_media_type: str,
    expects_session_id: bool,
) -> None:
    app = create_app(_settings(stateless_http=stateless_http, json_response=json_response))

    async with _running_client(app) as client:
        response = await client.post(
            "/mcp",
            json=_INITIALIZE_BODY,
            headers={
                "Authorization": f"Bearer {_VALID_TOKEN}",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": _LEGACY_PROTOCOL_VERSION,
            },
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(expected_media_type)
    assert (response.headers.get("mcp-session-id") is not None) is expects_session_id


async def test_default_settings_are_the_lambda_safe_combination() -> None:
    """The deployed default must need no extra env configuration to be
    Lambda-correct. ``_settings()`` here mirrors ``load_settings``' defaults
    (both ``True``, asserted directly in test_config.py), so this pins the
    end-to-end consequence: no session handed out, plain JSON back.
    """
    app = create_app(_settings())

    async with _running_client(app) as client:
        response = await client.post(
            "/mcp",
            json=_INITIALIZE_BODY,
            headers={
                "Authorization": f"Bearer {_VALID_TOKEN}",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": _LEGACY_PROTOCOL_VERSION,
            },
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert "mcp-session-id" not in response.headers


async def test_full_handshake_still_succeeds_in_stateless_json_mode() -> None:
    """The load-bearing regression guard for the Lambda switch: dropping
    sessions must not break the handshake a real MCP client drives.

    ``streamable_http_client`` + ``ClientSession`` negotiate for real (the
    SDK's own client defaults to the legacy leg -- FRD §7), so this covers
    ``initialize`` -> ``notifications/initialized`` -> ``tools/list`` with no
    session ID in play at any point. If stateless mode broke resumability in
    a way that also broke the plain request/response path, this fails.
    """
    app = create_app(_settings(stateless_http=True, json_response=True))

    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url=f"https://{_ALLOWED_HOST}",
            headers={"Authorization": f"Bearer {_VALID_TOKEN}"},
        ) as http_client,
        streamable_http_client("/mcp", http_client=http_client) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        init_result = await session.initialize()
        tools_result = await session.list_tools()

    assert init_result.server_info.name == SERVER_NAME
    assert {tool.name for tool in tools_result.tools} == {
        "list_repos",
        "get_repo_tree",
        "read_file",
        "search_code",
    }
