"""Tests for devoks_mcp_management.server (TASK-008).

Traces: AC-001-2, AC-002-4, AC-002-5, CTR-001, CTR-002, CTR-007, EDGE-010, DSN-005.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping

import pytest
from mcp.client import Client
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer

from devoks_mcp_management import server as server_module
from devoks_mcp_management.auth.verifier import StaticTableTokenVerifier
from devoks_mcp_management.config import Settings
from devoks_mcp_management.server import REQUIRED_SCOPES, SERVER_NAME, create_server


def _settings(
    *,
    public_url: str = "https://mcp.example.com",
    issuer_url: str = "https://issuer.example.com",
    role_tools: Mapping[str, frozenset[str]] | None = None,
) -> Settings:
    # Built directly via the `Settings` dataclass (the pattern `test_guard.py`
    # already uses), not `load_settings` — this module never touches
    # `github_app_private_key`/`client_tokens` content, so no PEM fixture is
    # needed here (unlike `test_config.py`, which validates the real key).
    return Settings(
        allowed_hosts=("mcp.example.com",),
        public_url=public_url,
        issuer_url=issuer_url,
        repo_allowlist=frozenset({"ridsync/devoks-mcp-servers"}),
        role_tools=role_tools
        if role_tools is not None
        else {"reader": frozenset({"list_repos", "get_repo_tree", "read_file", "search_code"})},
        github_app_id="app-id",
        github_app_installation_id="install-id",
        port=8000,
        log_level="INFO",
        read_file_max_bytes=262_144,
        search_code_max_results=30,
        token_refresh_leeway_seconds=300,
        stateless_http=True,
        json_response=True,
        client_tokens={},
        github_app_private_key="unused-in-server-tests",
    )


# --- create_server: basic shape ---------------------------------------------


def test_create_server_returns_mcpserver_with_configured_name() -> None:
    mcp = create_server(_settings())

    assert isinstance(mcp, MCPServer)
    assert mcp.name == SERVER_NAME


def test_create_server_builds_independent_instances_from_different_settings() -> None:
    # Factory, not a singleton (module docstring's design requirement): two
    # calls with two different `Settings` must not share state or collapse
    # to one server.
    mcp_a = create_server(_settings(public_url="https://a.example.com"))
    mcp_b = create_server(_settings(public_url="https://b.example.com"))

    assert mcp_a is not mcp_b
    assert mcp_a.settings.auth is not None
    assert mcp_b.settings.auth is not None
    assert str(mcp_a.settings.auth.resource_server_url) == "https://a.example.com"
    assert str(mcp_b.settings.auth.resource_server_url) == "https://b.example.com"


# --- §7 제약: token_verifier + auth 동반 주입 --------------------------------


def test_create_server_wires_token_verifier_and_auth_together() -> None:
    # §7 constraint: create_server must actually supply both. There is no
    # public accessor for the SDK's internally-stored verifier
    # (`MCPServer._token_verifier`, verified against mcp==2.1.1's source —
    # see server.py's module docstring), so this reads the private attribute
    # directly rather than skip verifying it.
    mcp = create_server(_settings())

    assert mcp.settings.auth is not None
    token_verifier = mcp._token_verifier  # pyright: ignore[reportPrivateUsage]
    assert isinstance(token_verifier, StaticTableTokenVerifier)


def test_sdk_raises_when_auth_given_without_token_verifier() -> None:
    # §7 제약을 SDK 소스에서 직접 재현: auth만 주고 token_verifier를 생략하면
    # MCPServer.__init__ 자체가 ValueError를 낸다 (mcp==2.1.1 실측,
    # mcp.server.mcpserver.server.MCPServer.__init__ 참고). create_server를
    # 거치지 않고 SDK 제약 자체를 고정한다.
    auth = AuthSettings(
        issuer_url="https://issuer.example.com",  # pyright: ignore[reportArgumentType]
        resource_server_url="https://mcp.example.com",  # pyright: ignore[reportArgumentType]
        required_scopes=REQUIRED_SCOPES,
    )

    with pytest.raises(ValueError, match="auth_server_provider or token_verifier"):
        MCPServer("test-server", auth=auth)


def test_sdk_raises_when_token_verifier_given_without_auth() -> None:
    # Mirror case: a verifier without `auth=` is equally rejected.
    verifier = StaticTableTokenVerifier({})

    with pytest.raises(ValueError, match="without auth settings"):
        MCPServer("test-server", token_verifier=verifier)


# --- AC-002-4 전제: resource_server_url / issuer_url이 Settings와 일치 -------


def test_auth_settings_resource_server_url_matches_public_url() -> None:
    mcp = create_server(_settings(public_url="https://custom.example.com"))

    assert mcp.settings.auth is not None
    # AC-002-4: RFC 9728 문서의 `resource`가 CTR-006의 공개 URL과 정확히
    # 일치해야 한다 — 문자열 그대로(트레일링 슬래시 없이) 비교. server.py의
    # 인계 노트: AnyHttpUrl(...)로 먼저 감싸면 트레일링 슬래시가 붙어 이 비교가
    # 깨진다(실측, mcp==2.1.1 / pydantic 2.13.5).
    assert str(mcp.settings.auth.resource_server_url) == "https://custom.example.com"


def test_auth_settings_issuer_url_matches_settings_issuer_url() -> None:
    mcp = create_server(_settings(issuer_url="https://custom-issuer.example.com"))

    assert mcp.settings.auth is not None
    assert str(mcp.settings.auth.issuer_url) == "https://custom-issuer.example.com"


# --- CTR-002 / AC-002-5 / EDGE-010: required_scopes -------------------------


def test_required_scopes_matches_ctr_002_value() -> None:
    mcp = create_server(_settings())

    assert mcp.settings.auth is not None
    assert mcp.settings.auth.required_scopes == ["devoks:read"]
    assert REQUIRED_SCOPES == ["devoks:read"]


# --- AC-001-2 / DSN-005: tools/list reflects the registered adapters --------


async def test_tools_list_returns_the_four_github_tools_after_task_022() -> None:
    # AC-001-2: `tools/list` must succeed and return the full set of
    # registered tools. TASK-022 wired the GitHub adapter's core 4 tools into
    # `tools/registry.py`'s `_ADAPTER_REGISTRARS`, so `create_server` now
    # exposes exactly those 4 — this was `result.tools == []` before TASK-022
    # (Stage 1 had zero adapters wired in); see `test_github_tools_wiring.py`
    # for `input_schema`-level assertions (TASK-022's own scope).
    #
    # NOTE: `Client(mcp)` connects in-process and bypasses the SDK's HTTP
    # bearer-auth middleware entirely (FRD §7) — this call reaches
    # `tools/list` with no `Authorization` header at all and still succeeds,
    # which is expected for this handler (tool *listing* is unauthenticated
    # at the MCP protocol level; only `tools/call` is guarded). It says
    # nothing about whether HTTP-level 401/403 enforcement works — TASK-010
    # covers that over the real ASGI/HTTP transport.
    mcp = create_server(_settings())

    async with Client(mcp) as client:
        result = await client.list_tools()

    assert {tool.name for tool in result.tools} == {
        "list_repos",
        "get_repo_tree",
        "read_file",
        "search_code",
    }


# --- import-time side effects ------------------------------------------------


def test_importing_server_module_has_no_side_effects_without_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Design requirement (module docstring): nothing at module scope may
    # construct an MCPServer or read Settings/the environment. Clear every
    # required CTR-006 key and reload the module — a reload that raises
    # would mean some module-level statement is doing real work at import
    # time instead of inside `create_server`.
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

    importlib.reload(server_module)  # must not raise
