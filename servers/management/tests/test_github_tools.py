"""Full-stack GitHub tool integration tests (TASK-024, TASK-040, TASK-041).

Traces: AC-005-2, AC-005-3, AC-005-6, AC-005-7, AC-005-8, AC-005-9, AC-003-3,
CTR-008, EDGE-001, EDGE-003, EDGE-004, EDGE-005, EDGE-013, EDGE-014.

Scope note -- read before adding a case here
-----------------------------------------------
This file exists to catch bugs at the *seam* between three already-tested
layers, not to re-verify any one layer in isolation:

- ``tests/test_github_tools_wiring.py`` (TASK-022) already covers tool
  schema shape, guard/RBAC gating, and response rendering -- but with a
  scripted ``_SpyGitHubClient`` standing in for the real client. It only
  proves the *tool* half of the pipeline is correct assuming the client
  already handed it the right dataclass.
- ``tests/test_github_lifespan.py`` (TASK-023) already drives the real
  lifespan/``GitHubClient``/``InstallationTokenProvider`` assembly through
  ``httpx2.MockTransport`` -- but exercises only ``list_repos``.
- ``tests/test_github_client.py`` (TASK-021/025) already covers GitHub
  response normalization (truncation math, binary detection, 4xx/5xx/rate
  limit precedence) exhaustively -- but calls ``GitHubClient`` directly,
  never through a tool or an MCP response.

None of the three proves that ``get_repo_tree``/``read_file``/``search_code``
survive the *whole* pipeline (JWT sign -> token exchange -> GitHub HTTP mock
-> ``GitHubClient`` parsing -> tool ``TypedDict`` conversion -> MCP
``structured_content``) without losing or distorting information -- e.g. a
``read_file`` truncation could be computed correctly by the client and still
get flattened to just `content` by the tool layer, silently dropping
`status`/`total_size`. That is exactly what every case below asserts on:
the full ``structured_content``/error-text shape a calling model actually
sees, built from an HTTP-mocked GitHub backend and nothing more privileged.

Every tool call sets ``auth_context_var`` directly, same requirement/reason
as the other two GitHub test files (``Client(mcp)`` bypasses HTTP auth, so a
guarded tool call denies fail-safe unless this is set -- see
``tools/guard.py``'s own docstring, "Consequence for TASK-024").

``app_module.GitHubLifespanContext`` would need the same dynamic-reference
treatment ``tests/test_github_lifespan.py`` documents (``test_app.py``
reloads ``devoks_mcp_management.app`` mid-session) -- this file never does an
``isinstance`` check against that class, so it never actually needs the
dynamic reference; the one place its name appears here is a
``TYPE_CHECKING``-only import for a type annotation, which ``from __future__
import annotations`` keeps unevaluated at runtime regardless of any later
reload.
"""

from __future__ import annotations

import base64
import contextlib
from collections.abc import Callable, Coroutine, Generator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx2
import pytest
from mcp.client import Client
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver import MCPServer
from mcp_types import ContentBlock, TextContent

from conftest import make_settings
from devoks_mcp_management import app as app_module
from devoks_mcp_management.config import Settings
from devoks_mcp_management.server import create_server
from devoks_mcp_management.types import (
    TOOL_GET_REPO_TREE,
    TOOL_LIST_REPOS,
    TOOL_READ_FILE,
    TOOL_SEARCH_CODE,
)

if TYPE_CHECKING:
    # Type-only: see module docstring for why a statically-imported name is
    # safe here despite test_app.py's mid-session `importlib.reload`.
    from devoks_mcp_management.app import GitHubLifespanContext

#: Private (module-internal) by design -- same pattern
#: tests/test_github_lifespan.py already uses for the identical reason (the
#: composition root, not a test file, owns building this).
_make_github_lifespan = app_module._make_github_lifespan  # pyright: ignore[reportPrivateUsage]

READER_ROLE = "reader"
ALL_FOUR_TOOLS = frozenset({TOOL_LIST_REPOS, TOOL_GET_REPO_TREE, TOOL_READ_FILE, TOOL_SEARCH_CODE})

Handler = Callable[[httpx2.Request], Coroutine[None, None, httpx2.Response]]

#: Captured before any test monkeypatches ``httpx2.AsyncClient`` -- see
#: tests/test_github_lifespan.py's identical constant for why this, not
#: ``httpx2.AsyncClient`` itself, is what ``_FakeAsyncClientFactory`` builds
#: real clients from.
_REAL_ASYNC_CLIENT = httpx2.AsyncClient

#: ``_make_github_lifespan``'s returned closure takes the owning ``MCPServer``
#: as its sole argument but never reads it -- every direct-entry helper below
#: calls it with this in place of a real server (mirrors
#: tests/test_github_lifespan.py's identical constant).
_UNUSED_MCP_ARG: Any = None


# --- Test scaffolding ---------------------------------------------------------------


def _settings(
    *,
    private_key: str,
    repo_allowlist: frozenset[str] = frozenset({"acme/widgets"}),
    read_file_max_bytes: int = 262_144,
    search_code_max_results: int = 30,
) -> Settings:
    return make_settings(
        repo_allowlist=repo_allowlist,
        github_app_id="app-123456",
        github_app_installation_id="install-789012",
        read_file_max_bytes=read_file_max_bytes,
        search_code_max_results=search_code_max_results,
        github_app_private_key=private_key,
    )


def _access_token(role: str, *, client_id: str = "client-1") -> AccessToken:
    return AccessToken(
        token="tok", client_id=client_id, scopes=["devoks:read"], claims={"role": role}
    )


@contextlib.contextmanager
def _identity(access_token: AccessToken) -> Generator[None]:
    """See tools/guard.py's docstring ("Consequence for TASK-024"): `Client(mcp)`
    bypasses HTTP auth, so a guarded tool call denies fail-safe unless this is set."""
    token = auth_context_var.set(AuthenticatedUser(access_token))
    try:
        yield
    finally:
        auth_context_var.reset(token)


def _iso_far_future() -> str:
    return datetime.fromtimestamp(4_102_444_800.0, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text_of(result_content: list[ContentBlock]) -> str:
    first = result_content[0]
    assert isinstance(first, TextContent)
    return first.text


def _repo_payload(full_name: str, *, default_branch: str = "main") -> dict[str, object]:
    return {
        "full_name": full_name,
        "name": full_name.split("/", 1)[1],
        "description": None,
        "default_branch": default_branch,
    }


def _dir_entry(name: str, path: str, entry_type: str, size: int) -> dict[str, object]:
    return {"name": name, "path": path, "type": entry_type, "size": size}


def _file_object(name: str, path: str, content_bytes: bytes) -> dict[str, object]:
    return {
        "name": name,
        "path": path,
        "type": "file",
        "size": len(content_bytes),
        "encoding": "base64",
        "content": base64.b64encode(content_bytes).decode("ascii"),
    }


class _GitHubHandler:
    """Fake GitHub backend covering the full HTTP surface this file exercises:
    installation-token exchange (``credentials.py``), installation repository
    listing, the repo-contents endpoint (both directory-array and
    single-file-object shapes, keyed by exact URL path since ``ref`` is
    always omitted in these tests), and code search. Every request served is
    recorded so tests can assert directly on GitHub call counts/absence
    (``AC-003-3``, ``EDGE-001``'s fail-safe confirmation).
    """

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.repos: tuple[dict[str, object], ...] = ()
        self.contents: dict[str, httpx2.Response] = {}
        self.search_response: httpx2.Response | None = None

    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/access_tokens"):
            return httpx2.Response(
                201, json={"token": "ghs_test_token", "expires_at": _iso_far_future()}
            )
        if path == "/installation/repositories":
            return httpx2.Response(200, json={"repositories": list(self.repos)})
        if path == "/search/code":
            assert self.search_response is not None, "search_response not configured"
            return self.search_response
        if path in self.contents:
            return self.contents[path]
        raise AssertionError(f"unexpected GitHub request: {request.method} {request.url}")


class _FakeAsyncClientFactory:
    """Monkeypatch target for ``httpx2.AsyncClient`` -- identical technique to
    ``tests/test_github_lifespan.py``'s own factory (reused here, not
    imported, since each GitHub test file is self-contained by this
    project's convention)."""

    def __init__(self, handler: Handler) -> None:
        self._handler = handler
        self.clients: list[httpx2.AsyncClient] = []

    def __call__(self, *, timeout: float) -> httpx2.AsyncClient:
        client = _REAL_ASYNC_CLIENT(transport=httpx2.MockTransport(self._handler), timeout=timeout)
        self.clients.append(client)
        return client

    @property
    def call_count(self) -> int:
        return len(self.clients)


def _build_server(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, handler: _GitHubHandler
) -> tuple[MCPServer[GitHubLifespanContext], _FakeAsyncClientFactory]:
    """Real composition root, mirroring tests/test_github_lifespan.py's
    established technique: `server.create_server` + `app._make_github_lifespan`,
    with `httpx2.AsyncClient` monkeypatched to attach a `MockTransport` at its
    one production call site -- never a hand-built `MCPServer`/fake lifespan.
    """
    factory = _FakeAsyncClientFactory(handler.handle)
    monkeypatch.setattr(httpx2, "AsyncClient", factory)
    mcp = create_server(settings, lifespan=_make_github_lifespan(settings))
    return mcp, factory


async def _call_tool_denied_and_expect_no_github_calls(
    mcp: MCPServer[GitHubLifespanContext], handler: _GitHubHandler, repo: str
) -> None:
    """Drive all 3 repo-scoped tools against `repo` and assert each is denied
    *and* that the guard blocked every one of them before `GitHubClient` ever
    touched the (shared) mocked transport -- the CTR-008 fail-safe gate
    (AC-003-3/EDGE-001) proven at the full-stack level, not just against a
    spy client (tests/test_github_tools_wiring.py already did that).
    """
    calls: list[tuple[str, dict[str, str]]] = [
        (TOOL_GET_REPO_TREE, {"repo": repo}),
        (TOOL_READ_FILE, {"repo": repo, "path": "a.py"}),
        (TOOL_SEARCH_CODE, {"query": "foo", "repo": repo}),
    ]
    with _identity(_access_token(READER_ROLE)):
        async with Client(mcp) as client:
            for tool_name, args in calls:
                result = await client.call_tool(tool_name, args)
                assert result.is_error is True, f"{tool_name} was not denied for repo={repo!r}"
    assert handler.requests == []


# --- AC-005-2: get_repo_tree full stack ---------------------------------------------


async def test_get_repo_tree_full_stack_lists_directory_entries(
    monkeypatch: pytest.MonkeyPatch, pem: str
) -> None:  # AC-005-2
    settings = _settings(private_key=pem)
    handler = _GitHubHandler()
    handler.contents["/repos/acme/widgets/contents"] = httpx2.Response(
        200,
        json=[
            _dir_entry("src", "src", "dir", 0),
            _dir_entry("README.md", "README.md", "file", 42),
        ],
    )
    mcp, factory = _build_server(monkeypatch, settings, handler)

    with _identity(_access_token(READER_ROLE)):
        async with Client(mcp) as client:
            result = await client.call_tool(TOOL_GET_REPO_TREE, {"repo": "acme/widgets"})

    assert result.is_error is False
    payload = result.structured_content
    assert payload is not None
    assert payload["path"] == ""
    entries = payload["entries"]
    assert [(e["name"], e["type"], e["size"]) for e in entries] == [
        ("src", "dir", 0),
        ("README.md", "file", 42),
    ]
    assert payload["count"] == 2
    # DSN-004 sanity: exactly one shared httpx2.AsyncClient built for the
    # whole lifespan, no per-call duplication.
    assert factory.call_count == 1


# --- AC-005-3: read_file full stack, all statuses ------------------------------------


async def test_read_file_full_stack_returns_complete_content(
    monkeypatch: pytest.MonkeyPatch, pem: str
) -> None:  # AC-005-3
    settings = _settings(private_key=pem)
    handler = _GitHubHandler()
    content = b"print(1)\n"
    handler.contents["/repos/acme/widgets/contents/main.py"] = httpx2.Response(
        200, json=_file_object("main.py", "main.py", content)
    )
    mcp, _ = _build_server(monkeypatch, settings, handler)

    with _identity(_access_token(READER_ROLE)):
        async with Client(mcp) as client:
            result = await client.call_tool(
                TOOL_READ_FILE, {"repo": "acme/widgets", "path": "main.py"}
            )

    assert result.is_error is False
    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "complete"
    assert payload["content"] == content.decode("utf-8")
    assert payload["returned_size"] == len(content)
    assert payload["total_size"] == len(content)
    assert payload["message"] is None


async def test_read_file_full_stack_truncates_over_cap_and_reports_both_sizes(
    monkeypatch: pytest.MonkeyPatch, pem: str
) -> None:  # AC-005-4, EDGE-004, CTR-004
    settings = _settings(private_key=pem, read_file_max_bytes=4)
    handler = _GitHubHandler()
    full_content = b"0123456789"
    handler.contents["/repos/acme/widgets/contents/big.txt"] = httpx2.Response(
        200, json=_file_object("big.txt", "big.txt", full_content)
    )
    mcp, _ = _build_server(monkeypatch, settings, handler)

    with _identity(_access_token(READER_ROLE)):
        async with Client(mcp) as client:
            result = await client.call_tool(
                TOOL_READ_FILE, {"repo": "acme/widgets", "path": "big.txt"}
            )

    assert result.is_error is False
    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "truncated"
    # A model reading this response must not be able to mistake the prefix
    # for the whole file: content is strictly a prefix, sizes disagree, and
    # message states both numbers explicitly.
    assert payload["content"] == "0123"
    assert payload["content"] != full_content.decode("utf-8")
    assert payload["returned_size"] == 4
    assert payload["total_size"] == 10
    message = payload["message"]
    assert message is not None
    assert "4" in message
    assert "10" in message


async def test_read_file_full_stack_reports_binary_with_size_and_no_content(
    monkeypatch: pytest.MonkeyPatch, pem: str
) -> None:  # AC-005-5, EDGE-005
    settings = _settings(private_key=pem)
    handler = _GitHubHandler()
    binary_content = b"\xff\xfe\x00\x01"
    handler.contents["/repos/acme/widgets/contents/image.bin"] = httpx2.Response(
        200, json=_file_object("image.bin", "image.bin", binary_content)
    )
    mcp, _ = _build_server(monkeypatch, settings, handler)

    with _identity(_access_token(READER_ROLE)):
        async with Client(mcp) as client:
            result = await client.call_tool(
                TOOL_READ_FILE, {"repo": "acme/widgets", "path": "image.bin"}
            )

    assert result.is_error is False
    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "binary"
    assert payload["content"] is None
    assert payload["returned_size"] == 0
    assert payload["total_size"] == len(binary_content)
    message = payload["message"]
    assert message is not None
    assert str(len(binary_content)) in message


# --- AC-005-6 / CTR-005: search_code full stack, cap + excerpt -----------------------


async def test_search_code_full_stack_reports_cap_and_excerpt(
    monkeypatch: pytest.MonkeyPatch, pem: str
) -> None:  # AC-005-6, CTR-005
    settings = _settings(private_key=pem, search_code_max_results=1)
    handler = _GitHubHandler()
    handler.search_response = httpx2.Response(
        200,
        json={
            "total_count": 5,
            "incomplete_results": False,
            "items": [
                {
                    "path": "src/a.py",
                    "repository": {"full_name": "acme/widgets"},
                    "text_matches": [{"fragment": "def foo(): pass"}],
                },
                {
                    "path": "src/b.py",
                    "repository": {"full_name": "acme/widgets"},
                    "text_matches": [{"fragment": "def foo2(): pass"}],
                },
            ],
        },
    )
    mcp, _ = _build_server(monkeypatch, settings, handler)

    with _identity(_access_token(READER_ROLE)):
        async with Client(mcp) as client:
            result = await client.call_tool(
                TOOL_SEARCH_CODE, {"query": "foo", "repo": "acme/widgets"}
            )

    assert result.is_error is False
    payload = result.structured_content
    assert payload is not None
    # Client-side re-slice to CTR-005's cap (1) even though GitHub's mocked
    # response carried 2 items -- proving the cap survives to the tool layer.
    assert payload["returned_count"] == 1
    assert payload["total_count"] == 5
    assert len(payload["items"]) == 1
    assert payload["items"][0]["path"] == "src/a.py"
    assert payload["items"][0]["excerpt"] == "def foo(): pass"
    message = payload["message"]
    assert message is not None
    assert "1" in message
    assert "5" in message


# --- AC-005-8 / EDGE-006: 404 full stack ----------------------------------------------


async def test_read_file_full_stack_404_names_what_was_not_found(
    monkeypatch: pytest.MonkeyPatch, pem: str
) -> None:  # AC-005-8
    settings = _settings(private_key=pem)
    handler = _GitHubHandler()
    handler.contents["/repos/acme/widgets/contents/missing.py"] = httpx2.Response(
        404, json={"message": "Not Found"}
    )
    mcp, _ = _build_server(monkeypatch, settings, handler)

    with _identity(_access_token(READER_ROLE)):
        async with Client(mcp) as client:
            result = await client.call_tool(
                TOOL_READ_FILE, {"repo": "acme/widgets", "path": "missing.py"}
            )

    assert result.is_error is True
    message = _text_of(result.content)
    assert "acme/widgets" in message
    assert "missing.py" in message


# --- AC-005-7: generic 4xx/5xx full stack, normalized without a stacktrace -----------


async def test_read_file_full_stack_5xx_is_normalized_without_a_stacktrace(
    monkeypatch: pytest.MonkeyPatch, pem: str
) -> None:  # AC-005-7
    settings = _settings(private_key=pem)
    handler = _GitHubHandler()
    handler.contents["/repos/acme/widgets/contents/broken.py"] = httpx2.Response(
        500, json={"message": "Internal Server Error"}
    )
    mcp, _ = _build_server(monkeypatch, settings, handler)

    with _identity(_access_token(READER_ROLE)):
        async with Client(mcp) as client:
            result = await client.call_tool(
                TOOL_READ_FILE, {"repo": "acme/widgets", "path": "broken.py"}
            )

    assert result.is_error is True
    message = _text_of(result.content)
    assert "500" in message
    assert "Traceback" not in message
    assert "site-packages" not in message
    assert "devoks_mcp_management" not in message


# --- AC-005-9 / EDGE-003: rate limit full stack, retry time in the message ----------


async def test_read_file_full_stack_rate_limit_reports_a_retry_time(
    monkeypatch: pytest.MonkeyPatch, pem: str
) -> None:  # AC-005-9, EDGE-003
    settings = _settings(private_key=pem)
    handler = _GitHubHandler()
    reset_epoch = 4_102_444_800  # matches _iso_far_future's far-future epoch
    handler.contents["/repos/acme/widgets/contents/limited.py"] = httpx2.Response(
        403,
        headers={
            "x-ratelimit-remaining": "0",
            "x-ratelimit-reset": str(reset_epoch),
            "x-ratelimit-resource": "core",
        },
        json={"message": "API rate limit exceeded"},
    )
    mcp, _ = _build_server(monkeypatch, settings, handler)

    with _identity(_access_token(READER_ROLE)):
        async with Client(mcp) as client:
            result = await client.call_tool(
                TOOL_READ_FILE, {"repo": "acme/widgets", "path": "limited.py"}
            )

    assert result.is_error is True
    message = _text_of(result.content)
    assert "rate limit" in message.lower()
    expected_retry_at = datetime.fromtimestamp(reset_epoch, tz=UTC).isoformat()
    assert expected_retry_at in message


# --- EDGE-001 / AC-003-4: empty allowlist ---------------------------------------------


async def test_empty_allowlist_full_stack_list_repos_returns_empty_result(
    monkeypatch: pytest.MonkeyPatch, pem: str
) -> None:  # EDGE-001, AC-003-4
    settings = _settings(private_key=pem, repo_allowlist=frozenset())
    handler = _GitHubHandler()
    handler.repos = (_repo_payload("acme/widgets"),)
    mcp, _ = _build_server(monkeypatch, settings, handler)

    with _identity(_access_token(READER_ROLE)):
        async with Client(mcp) as client:
            result = await client.call_tool(TOOL_LIST_REPOS, {})

    assert result.is_error is False
    payload = result.structured_content
    assert payload is not None
    assert payload["repos"] == []
    assert payload["count"] == 0


async def test_empty_allowlist_full_stack_denies_the_other_3_tools_with_zero_github_calls(
    monkeypatch: pytest.MonkeyPatch, pem: str
) -> None:  # EDGE-001, AC-003-4
    settings = _settings(private_key=pem, repo_allowlist=frozenset())
    handler = _GitHubHandler()
    mcp, _ = _build_server(monkeypatch, settings, handler)

    # A repo that would exist and be visible if the allowlist were not empty
    # -- proves the denial is EDGE-001's fail-safe (empty allowlist), not
    # merely "this particular repo is unknown".
    await _call_tool_denied_and_expect_no_github_calls(mcp, handler, "acme/widgets")


# --- AC-003-3: repo outside a non-empty allowlist -------------------------------------


async def test_repo_outside_allowlist_full_stack_denies_3_tools_with_zero_github_calls(
    monkeypatch: pytest.MonkeyPatch, pem: str
) -> None:  # AC-003-3
    settings = _settings(private_key=pem, repo_allowlist=frozenset({"acme/widgets"}))
    handler = _GitHubHandler()
    mcp, _ = _build_server(monkeypatch, settings, handler)

    await _call_tool_denied_and_expect_no_github_calls(mcp, handler, "someone-else/private")


# --- EDGE-013 / AC-003-3 / CTR-008: path traversal, full stack (TASK-040) ------------
#
# These reproduce the exact attacks that proved this vulnerability real,
# driven through the real guard -> tool -> GitHubClient stack (not a spy),
# with a `_GitHubHandler` that would happily serve the escaped-to endpoint
# if the traversal ever reached it -- so a regression here would not just
# raise, it would return the victim/installation data.


async def test_read_file_path_traversal_full_stack_blocked_with_zero_github_calls(
    monkeypatch: pytest.MonkeyPatch, pem: str
) -> None:  # EDGE-013, AC-003-3, CTR-008
    settings = _settings(private_key=pem)
    handler = _GitHubHandler()
    mcp, _ = _build_server(monkeypatch, settings, handler)

    with _identity(_access_token(READER_ROLE)):
        async with Client(mcp) as client:
            result = await client.call_tool(
                TOOL_READ_FILE,
                {
                    "repo": "acme/widgets",
                    "path": "../../../victim-org/secret-repo/contents/.env",
                },
            )

    assert result.is_error is True
    assert handler.requests == []


async def test_get_repo_tree_path_traversal_to_installation_endpoint_full_stack_blocked(
    monkeypatch: pytest.MonkeyPatch, pem: str
) -> None:  # EDGE-013, AC-003-3, CTR-008
    settings = _settings(private_key=pem)
    handler = _GitHubHandler()
    # If the traversal fix regressed, this handler would actually answer the
    # escaped-to request with real (fake, but plausible) installation data
    # instead of the framework's own "unexpected request" AssertionError --
    # proving the block, not just an incidental 404.
    handler.repos = (_repo_payload("acme/widgets"),)
    mcp, _ = _build_server(monkeypatch, settings, handler)

    with _identity(_access_token(READER_ROLE)):
        async with Client(mcp) as client:
            result = await client.call_tool(
                TOOL_GET_REPO_TREE,
                {"repo": "acme/widgets", "path": "../../../../installation/repositories"},
            )

    assert result.is_error is True
    assert handler.requests == []


# --- EDGE-014 / AC-003-3 / CTR-008: search qualifier injection, full stack (TASK-041) -


async def test_search_code_qualifier_injection_full_stack_blocked_with_zero_github_calls(
    monkeypatch: pytest.MonkeyPatch, pem: str
) -> None:  # EDGE-014, AC-003-3, CTR-008
    settings = _settings(private_key=pem)
    handler = _GitHubHandler()
    mcp, _ = _build_server(monkeypatch, settings, handler)

    with _identity(_access_token(READER_ROLE)):
        async with Client(mcp) as client:
            result = await client.call_tool(
                TOOL_SEARCH_CODE,
                {"query": "password OR repo:victim-org/secret-repo", "repo": "acme/widgets"},
            )

    assert result.is_error is True
    assert handler.requests == []


# --- TASK-044: an empty allowlist must not spend a GitHub round trip -----------


async def test_list_repos_with_empty_allowlist_makes_no_github_request(
    monkeypatch: pytest.MonkeyPatch, pem: str
) -> None:
    """CTR-008's default allowlist is *empty*, and an empty allowlist denies
    every repository by construction, so the intersection ``list_repos``
    computes is empty no matter what the installation can see.

    Calling GitHub anyway spent a rate-limit unit (EDGE-003) on a response
    whose every element was then filtered away -- and on a freshly deployed
    server where ``MCP_REPO_ALLOWLIST`` has not been set yet, that is *every*
    ``list_repos`` call. The assertion is on ``handler.requests``, not on the
    result, because the result was already correct before the fix; only the
    wasted round trip changed.

    Note ``handler.repos`` is left empty *and* unreachable: the handler would
    raise on any unexpected path, so a regression that removes the guard
    clause fails loudly rather than silently.
    """
    settings = _settings(private_key=pem, repo_allowlist=frozenset())
    handler = _GitHubHandler()
    mcp, _factory = _build_server(monkeypatch, settings, handler)

    with _identity(_access_token(READER_ROLE)):
        async with Client(mcp) as client:
            result = await client.call_tool(TOOL_LIST_REPOS, {})

    assert result.is_error is False
    assert result.structured_content == {"repos": [], "count": 0}
    assert handler.requests == []


async def test_list_repos_with_non_empty_allowlist_still_calls_github(
    monkeypatch: pytest.MonkeyPatch, pem: str
) -> None:
    """The guard clause must be scoped to the empty case only -- a fix that
    short-circuited unconditionally would also pass the test above."""
    settings = _settings(private_key=pem, repo_allowlist=frozenset({"acme/widgets"}))
    handler = _GitHubHandler()
    handler.repos = ({"full_name": "acme/widgets", "name": "widgets", "default_branch": "main"},)
    mcp, _factory = _build_server(monkeypatch, settings, handler)

    with _identity(_access_token(READER_ROLE)):
        async with Client(mcp) as client:
            result = await client.call_tool(TOOL_LIST_REPOS, {})

    payload = result.structured_content
    assert payload is not None
    assert payload["count"] == 1
    assert any(request.url.path == "/installation/repositories" for request in handler.requests)
