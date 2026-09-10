"""Tests for devoks_mcp_management.adapters.knowledge.github.tools (TASK-022).

Traces: AC-005-1, AC-005-2, AC-005-3, AC-005-6, CTR-005, CTR-007, CTR-008.

Scope note: this file covers tool *wiring* -- schema shape, guard/allowlist gating,
lifespan-context validation, and response rendering -- using a scripted spy
`GitHubClient` subclass (never real HTTP). It does not cover GitHub HTTP-response
normalization (truncation math, binary detection, 4xx/5xx mapping): that is
`tests/test_github_client.py`'s job (TASK-021/025), already 39 cases deep. Nor does
it cover the full-stack, real-lifespan round trip through `create_server`/`create_app`
with a mocked GitHub backend: that is TASK-024's job. This file is deliberately named
differently from PLAN.md's `tests/test_github_tools.py` (TASK-024's designated path)
so TASK-024 never overwrites it.

Every tool call test sets `auth_context_var` directly (`tools/guard.py`'s documented
requirement for `Client(mcp)`, mirrored from `tests/test_guard.py`) -- `Client(mcp)`
bypasses HTTP auth entirely, so skipping this makes every call fail with the guard's
fixed no-identity denial, which looks like a policy bug rather than a missing fixture.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator, Generator, Mapping
from contextlib import asynccontextmanager

import httpx2
import pytest
from mcp.client import Client
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ContentBlock, TextContent

from devoks_mcp_management.adapters.knowledge.github.client import (
    FileContent,
    GitHubClient,
    RepositorySummary,
    SearchResultItem,
    SearchResults,
    TreeEntry,
)
from devoks_mcp_management.adapters.knowledge.github.tools import register
from devoks_mcp_management.config import Settings
from devoks_mcp_management.tools.guard import make_tool_guard
from devoks_mcp_management.types import (
    TOOL_GET_REPO_TREE,
    TOOL_LIST_REPOS,
    TOOL_READ_FILE,
    TOOL_SEARCH_CODE,
)

READER_ROLE = "reader"
ALL_FOUR_TOOLS = frozenset({TOOL_LIST_REPOS, TOOL_GET_REPO_TREE, TOOL_READ_FILE, TOOL_SEARCH_CODE})
DEFAULT_ALLOWLIST = frozenset({"acme/widgets"})

_DEFAULT_FILE_CONTENT = FileContent(
    status="complete", content="", returned_size=0, total_size=0, message=None
)
_DEFAULT_SEARCH_RESULTS = SearchResults(items=(), total_count=0, incomplete_results=False)


# --- Test scaffolding ---------------------------------------------------------------


def _settings(
    *,
    role_tools: Mapping[str, frozenset[str]] | None = None,
    repo_allowlist: frozenset[str] = DEFAULT_ALLOWLIST,
) -> Settings:
    # Direct dataclass construction -- same pattern as test_guard.py/test_server.py
    # for modules that never touch github_app_private_key/client_tokens content.
    return Settings(
        allowed_hosts=("mcp.example.com",),
        public_url="https://mcp.example.com/mcp",
        issuer_url="https://issuer.example.com",
        repo_allowlist=repo_allowlist,
        role_tools=role_tools if role_tools is not None else {READER_ROLE: ALL_FOUR_TOOLS},
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
        github_app_private_key="unused-in-tools-wiring-tests",
    )


def _access_token(role: str | None, *, client_id: str = "client-1") -> AccessToken:
    claims = {"role": role} if role is not None else None
    return AccessToken(token="tok", client_id=client_id, scopes=["devoks:read"], claims=claims)


@contextlib.contextmanager
def _identity(access_token: AccessToken | None) -> Generator[None]:
    """Set (and always reset) the SDK's auth contextvar for the block body.

    See this module's docstring / tools/guard.py's own docstring: `Client(mcp)`
    bypasses HTTP auth, so a guarded tool call denies fail-safe unless this is set.
    """
    if access_token is None:
        yield
        return
    token = auth_context_var.set(AuthenticatedUser(access_token))
    try:
        yield
    finally:
        auth_context_var.reset(token)


def _text_of(result_content: list[ContentBlock]) -> str:
    first = result_content[0]
    assert isinstance(first, TextContent)
    return first.text


def _never_called_http_client() -> httpx2.AsyncClient:
    async def _handler(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError(
            "_SpyGitHubClient overrides every method that would use this transport"
        )

    return httpx2.AsyncClient(transport=httpx2.MockTransport(_handler))


class _NeverCalledTokenProvider:
    """Structurally satisfies `client.TokenProvider`; every method that would use it
    is overridden by `_SpyGitHubClient`, so this must never actually run."""

    async def get_token(self) -> str:
        raise AssertionError("_SpyGitHubClient overrides every method that would use this")


class _SpyGitHubClient(GitHubClient):
    """A `GitHubClient` subclass (so `isinstance(client, GitHubClient)` -- this
    module's own `tools._require_lifespan` runtime check -- still holds) that records
    every call and returns pre-scripted results instead of making any real HTTP
    request.

    `CTR-008`'s gate test asserts directly against `.calls` staying empty -- the
    whole point of that test.
    """

    def __init__(self) -> None:
        super().__init__(
            http_client=_never_called_http_client(),
            token_provider=_NeverCalledTokenProvider(),
            read_file_max_bytes=262_144,
            search_code_max_results=30,
        )
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.list_installation_repositories_result: tuple[RepositorySummary, ...] = ()
        self.get_repo_tree_result: tuple[TreeEntry, ...] = ()
        self.read_file_result: FileContent = _DEFAULT_FILE_CONTENT
        self.search_code_result: SearchResults = _DEFAULT_SEARCH_RESULTS
        self.raise_error: ToolError | None = None

    async def list_installation_repositories(self) -> tuple[RepositorySummary, ...]:
        self.calls.append(("list_installation_repositories", ()))
        if self.raise_error is not None:
            raise self.raise_error
        return self.list_installation_repositories_result

    async def get_repo_tree(
        self, repo: str, path: str = "", ref: str | None = None
    ) -> tuple[TreeEntry, ...]:
        self.calls.append(("get_repo_tree", (repo, path, ref)))
        if self.raise_error is not None:
            raise self.raise_error
        return self.get_repo_tree_result

    async def read_file(self, repo: str, path: str, ref: str | None = None) -> FileContent:
        self.calls.append(("read_file", (repo, path, ref)))
        if self.raise_error is not None:
            raise self.raise_error
        return self.read_file_result

    async def search_code(self, query: str, repo: str) -> SearchResults:
        self.calls.append(("search_code", (query, repo)))
        if self.raise_error is not None:
            raise self.raise_error
        return self.search_code_result


class _FakeLifespanContext:
    """Satisfies `tools.GitHubToolContext` structurally -- exactly the two
    attribute names TASK-023's real lifespan value must expose."""

    def __init__(self, github: GitHubClient, repo_allowlist: frozenset[str]) -> None:
        self.github = github
        self.repo_allowlist = repo_allowlist


class _WrongShapeLifespanContext:
    """Has `github` but not `repo_allowlist` -- exercises the "shape mismatch"
    branch of `_require_lifespan`, distinct from "lifespan absent entirely"."""

    def __init__(self, github: GitHubClient) -> None:
        self.github = github


def _build_mcp(
    *,
    settings: Settings,
    repo_allowlist: frozenset[str] | None = None,
    include_lifespan: bool = True,
    wrong_shape_lifespan: bool = False,
) -> tuple[MCPServer, _SpyGitHubClient]:
    """Build a minimal `MCPServer` with only the GitHub adapter's 4 tools registered.

    Deliberately bypasses `server.create_server`/`app.create_app` (out of this task's
    `file:` scope, and TASK-023 has not wired a real lifespan yet) -- this is the
    `MCPServer(..., lifespan=...)` + `register(mcp, guard)` composition `register`'s
    own docstring documents as its contract, built directly here so tests can control
    every input.
    """
    spy = _SpyGitHubClient()
    effective_allowlist = repo_allowlist if repo_allowlist is not None else settings.repo_allowlist

    lifespan = None
    if include_lifespan:

        @asynccontextmanager
        async def _lifespan(_: MCPServer) -> AsyncGenerator[object]:
            if wrong_shape_lifespan:
                yield _WrongShapeLifespanContext(github=spy)
            else:
                yield _FakeLifespanContext(github=spy, repo_allowlist=effective_allowlist)

        lifespan = _lifespan

    mcp: MCPServer = MCPServer("test-server", lifespan=lifespan)
    guard = make_tool_guard(settings)
    register(mcp, guard)
    return mcp, spy


# --- AC-001-2 / integration risk: schema shape, ctx hidden -------------------------


# AC-001-2
async def test_tools_list_exposes_all_four_tools_with_real_schemas_and_hides_ctx() -> None:
    mcp, _ = _build_mcp(settings=_settings(), include_lifespan=False)

    async with Client(mcp) as client:
        result = await client.list_tools()

    tools_by_name = {tool.name: tool for tool in result.tools}
    assert set(tools_by_name) == ALL_FOUR_TOOLS

    list_repos_schema = tools_by_name[TOOL_LIST_REPOS].input_schema
    assert list_repos_schema.get("properties", {}) == {}

    tree_schema = tools_by_name[TOOL_GET_REPO_TREE].input_schema
    assert set(tree_schema["properties"]) == {"repo", "path", "ref"}
    assert set(tree_schema.get("required", [])) == {"repo"}

    read_schema = tools_by_name[TOOL_READ_FILE].input_schema
    assert set(read_schema["properties"]) == {"repo", "path", "ref"}
    assert set(read_schema.get("required", [])) == {"repo", "path"}

    search_schema = tools_by_name[TOOL_SEARCH_CODE].input_schema
    assert set(search_schema["properties"]) == {"query", "repo"}
    assert set(search_schema.get("required", [])) == {"query", "repo"}

    for schema in (list_repos_schema, tree_schema, read_schema, search_schema):
        assert "ctx" not in schema.get("properties", {})


# --- AC-005-1: list_repos = installation ∩ allowlist -------------------------------


# AC-005-1
async def test_list_repos_returns_only_the_installation_and_allowlist_intersection() -> None:
    settings = _settings(repo_allowlist=frozenset({"acme/a", "acme/c", "acme/missing"}))
    mcp, spy = _build_mcp(settings=settings)
    spy.list_installation_repositories_result = (
        RepositorySummary(full_name="acme/a", name="a", description="A", default_branch="main"),
        RepositorySummary(full_name="acme/b", name="b", description="B", default_branch="main"),
        RepositorySummary(full_name="acme/c", name="c", description=None, default_branch="dev"),
    )

    with _identity(_access_token(READER_ROLE)):
        async with Client(mcp) as client:
            result = await client.call_tool(TOOL_LIST_REPOS, {})

    assert result.is_error is False
    assert result.structured_content is not None
    names = [repo["full_name"] for repo in result.structured_content["repos"]]
    # "acme/b" excluded: installation-visible but not allowlisted.
    # "acme/missing" absent: allowlisted but the installation never returned it --
    # never fabricated into the result.
    assert names == ["acme/a", "acme/c"]
    assert result.structured_content["count"] == 2


# --- AC-005-2: get_repo_tree entries + default root path ---------------------------


async def test_get_repo_tree_defaults_path_to_repo_root_and_renders_entries() -> None:  # AC-005-2
    mcp, spy = _build_mcp(settings=_settings())
    spy.get_repo_tree_result = (
        TreeEntry(name="src", path="src", type="dir", size=0),
        TreeEntry(name="README.md", path="README.md", type="file", size=42),
    )

    with _identity(_access_token(READER_ROLE)):
        async with Client(mcp) as client:
            result = await client.call_tool(TOOL_GET_REPO_TREE, {"repo": "acme/widgets"})

    assert result.is_error is False
    assert spy.calls == [("get_repo_tree", ("acme/widgets", "", None))]
    assert result.structured_content is not None
    assert result.structured_content["path"] == ""
    entries = result.structured_content["entries"]
    assert [(e["name"], e["type"], e["size"]) for e in entries] == [
        ("src", "dir", 0),
        ("README.md", "file", 42),
    ]
    assert result.structured_content["count"] == 2


# --- AC-005-3: read_file renders all 4 statuses distinctly -------------------------


@pytest.mark.parametrize(
    ("file_content", "expected_content_is_none"),
    [
        pytest.param(
            FileContent(
                status="complete", content="print(1)", returned_size=8, total_size=8, message=None
            ),
            False,
            id="complete",
        ),
        pytest.param(
            FileContent(
                status="truncated",
                content="prin",
                returned_size=4,
                total_size=100,
                message="Content truncated to 4 of 100 bytes (limit 4 bytes).",
            ),
            False,
            id="truncated",
        ),
        pytest.param(
            FileContent(
                status="binary",
                content=None,
                returned_size=0,
                total_size=7,
                message="File is binary (not valid UTF-8); size is 7 bytes.",
            ),
            True,
            id="binary",
        ),
        pytest.param(
            FileContent(
                status="unavailable",
                content=None,
                returned_size=0,
                total_size=200 * 1024 * 1024,
                message="GitHub cannot serve this file's content (size exceeds the limit).",
            ),
            True,
            id="unavailable",
        ),
    ],
)
async def test_read_file_renders_all_four_statuses_distinctly(  # AC-005-3
    file_content: FileContent, expected_content_is_none: bool
) -> None:
    mcp, spy = _build_mcp(settings=_settings())
    spy.read_file_result = file_content

    with _identity(_access_token(READER_ROLE)):
        async with Client(mcp) as client:
            result = await client.call_tool(
                TOOL_READ_FILE, {"repo": "acme/widgets", "path": "a.py"}
            )

    assert result.is_error is False
    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == file_content.status
    assert (payload["content"] is None) == expected_content_is_none
    if not expected_content_is_none:
        assert payload["content"] == file_content.content
    assert payload["returned_size"] == file_content.returned_size
    assert payload["total_size"] == file_content.total_size
    assert payload["message"] == file_content.message


# --- AC-005-6 / CTR-005: search_code matches + excerpt + cap notice ----------------


# AC-005-6, CTR-005
async def test_search_code_reports_the_cap_via_message_when_results_are_capped() -> None:
    mcp, spy = _build_mcp(settings=_settings())
    spy.search_code_result = SearchResults(
        items=(SearchResultItem(repository="acme/widgets", path="src/main.py", excerpt="def foo"),),
        total_count=10,
        incomplete_results=False,
    )

    with _identity(_access_token(READER_ROLE)):
        async with Client(mcp) as client:
            result = await client.call_tool(
                TOOL_SEARCH_CODE, {"query": "foo", "repo": "acme/widgets"}
            )

    assert result.is_error is False
    payload = result.structured_content
    assert payload is not None
    assert payload["returned_count"] == 1
    assert payload["total_count"] == 10
    assert payload["items"][0]["path"] == "src/main.py"
    assert payload["items"][0]["excerpt"] == "def foo"
    assert payload["message"] is not None
    assert "10" in payload["message"]
    assert "1" in payload["message"]


async def test_search_code_message_is_none_when_results_are_not_capped() -> None:  # AC-005-6
    mcp, spy = _build_mcp(settings=_settings())
    spy.search_code_result = SearchResults(
        items=(SearchResultItem(repository="acme/widgets", path="src/main.py", excerpt="def foo"),),
        total_count=1,
        incomplete_results=False,
    )

    with _identity(_access_token(READER_ROLE)):
        async with Client(mcp) as client:
            result = await client.call_tool(
                TOOL_SEARCH_CODE, {"query": "foo", "repo": "acme/widgets"}
            )

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["message"] is None


# --- CTR-008 / AC-003-3: guard denies before GitHubClient is ever touched ----------


# CTR-008, AC-003-3
async def test_guard_denies_repo_outside_allowlist_before_github_client_is_called() -> None:
    settings = _settings(repo_allowlist=frozenset({"acme/allowed"}))
    mcp, spy = _build_mcp(settings=settings)

    with _identity(_access_token(READER_ROLE)):
        async with Client(mcp) as client:
            result = await client.call_tool(
                TOOL_READ_FILE, {"repo": "someone-else/private", "path": "secret.txt"}
            )

    assert result.is_error is True
    assert spy.calls == []


# --- CTR-007: role without tool permission is denied, body never runs -------------


async def test_role_without_tool_permission_is_denied_and_body_never_runs() -> None:  # CTR-007
    settings = _settings(role_tools={READER_ROLE: frozenset({TOOL_LIST_REPOS})})
    mcp, spy = _build_mcp(settings=settings)

    with _identity(_access_token(READER_ROLE)):
        async with Client(mcp) as client:
            result = await client.call_tool(
                TOOL_READ_FILE, {"repo": "acme/widgets", "path": "a.py"}
            )

    assert result.is_error is True
    assert spy.calls == []


# --- lifespan-context validation: never a silent AttributeError -------------------


# DSN-004
async def test_missing_lifespan_context_raises_clear_tool_error_not_attribute_error() -> None:
    mcp, _ = _build_mcp(settings=_settings(), include_lifespan=False)

    with _identity(_access_token(READER_ROLE)):
        async with Client(mcp) as client:
            result = await client.call_tool(
                TOOL_READ_FILE, {"repo": "acme/widgets", "path": "a.py"}
            )

    assert result.is_error is True
    message = _text_of(result.content)
    assert "AttributeError" not in message
    assert "GitHub tools are unavailable" in message


# DSN-004
async def test_wrong_shape_lifespan_context_raises_clear_tool_error_not_attribute_error() -> None:
    mcp, _ = _build_mcp(settings=_settings(), wrong_shape_lifespan=True)

    with _identity(_access_token(READER_ROLE)):
        async with Client(mcp) as client:
            result = await client.call_tool(
                TOOL_READ_FILE, {"repo": "acme/widgets", "path": "a.py"}
            )

    assert result.is_error is True
    message = _text_of(result.content)
    assert "AttributeError" not in message
    assert "GitHub tools are unavailable" in message


# --- client.py's ToolError passes through the tool body unchanged -----------------


# AC-005-7
async def test_github_client_tool_error_passes_through_the_tool_body_unchanged() -> None:
    mcp, spy = _build_mcp(settings=_settings())
    spy.raise_error = ToolError("GitHub repository, ref, or path not found: repo='acme/widgets'")

    with _identity(_access_token(READER_ROLE)):
        async with Client(mcp) as client:
            result = await client.call_tool(
                TOOL_READ_FILE, {"repo": "acme/widgets", "path": "missing.py"}
            )

    assert result.is_error is True
    message = _text_of(result.content)
    assert "GitHub repository, ref, or path not found" in message
