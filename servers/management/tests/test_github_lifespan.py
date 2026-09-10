"""Tests for the MCP-protocol GitHub credentials lifespan (TASK-023, DSN-004).

Traces: AC-006-1, DSN-004.

Covers ``app._make_github_lifespan``/``app.GitHubLifespanContext`` (the concrete
value that fills ``ctx.request_context.lifespan_context`` for
``adapters/knowledge/github/tools.py``'s ``GitHubToolContext`` contract) and
``server.create_server``'s new ``lifespan=`` forwarding. Deliberately distinct
from:

- ``tests/test_server.py`` (TASK-008): §7 auth/RBAC wiring, no GitHub lifespan.
- ``tests/test_app.py`` (TASK-009): ASGI routes/transport-security/HTTP auth —
  unaffected by this task (no mount-structure change), already re-verified by
  its own unmodified 20+ cases on every run of this suite.
- ``tests/test_github_tools_wiring.py`` (TASK-022): tool schema/guard/response
  shape, using a hand-built ``MCPServer`` + a fake lifespan context. This file
  instead drives the *real* production wiring (``app._make_github_lifespan`` +
  ``server.create_server(..., lifespan=...)``), so a fake ``GitHubClient``
  double is not appropriate here.
- ``tests/test_github_tools.py`` (TASK-024, not yet written): full-stack
  in-memory ``Client(mcp)`` + GitHub HTTP mock coverage of all 4 tools'
  response contracts. This file only needs one successful ``list_repos`` call
  to prove the lifespan wiring itself works end-to-end (AC-006-1); the other
  3 tools' behavior is out of this task's scope.

No real GitHub network calls: ``httpx2.AsyncClient`` is monkeypatched at its
one production call site (``app.py``'s ``_make_github_lifespan``) to attach an
``httpx2.MockTransport`` instead, mirroring ``tests/test_credentials.py``'s/
``tests/test_github_client.py``'s technique one level up the stack.

``app_module.GitHubLifespanContext``, not a top-level ``from ... import
GitHubLifespanContext`` -- deliberately, and required for correctness, not
just style: ``tests/test_app.py``'s
``test_importing_app_module_has_no_side_effects_without_env`` reloads
``devoks_mcp_management.app`` in the same test session (``importlib.reload``
re-executes the module body **in place**, so ``app_module`` stays the same
object but ``class GitHubLifespanContext`` is redefined -- a genuinely new
class, distinct from whatever a `from ... import GitHubLifespanContext`
statement captured before that reload ran). ``_make_github_lifespan``'s
closure resolves the name ``GitHubLifespanContext`` from ``app_module``'s
(shared, mutated-in-place) globals at *call* time, so it always yields an
instance of whichever class currently lives there -- matching that means
reading ``app_module.GitHubLifespanContext`` here at *assertion* time too,
never a name statically imported once at collection time.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Coroutine, Generator
from datetime import UTC, datetime
from typing import Any

import httpx2
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp.client import Client
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken

from devoks_mcp_management import app as app_module
from devoks_mcp_management.adapters.knowledge.github.client import GitHubClient
from devoks_mcp_management.adapters.knowledge.github.credentials import InstallationTokenProvider
from devoks_mcp_management.config import Settings
from devoks_mcp_management.server import create_server
from devoks_mcp_management.types import TOOL_LIST_REPOS

#: Private (module-internal) by design -- see server.py/app.py's own module
#: docstrings for why the composition root, not this test file, owns
#: building it. Bound once here (same pattern test_registry.py already uses
#: for `_ADAPTER_REGISTRARS`) rather than a `# pyright: ignore` at every call
#: site below.
_make_github_lifespan = app_module._make_github_lifespan  # pyright: ignore[reportPrivateUsage]

READER_ROLE = "reader"
ALL_FOUR_TOOLS = frozenset({"list_repos", "get_repo_tree", "read_file", "search_code"})

#: Matches ``httpx2._transports.mock.AsyncHandler`` exactly (``Coroutine``, not
#: the broader ``Awaitable``) -- ``httpx2.MockTransport.__init__`` is typed as
#: ``SyncHandler | AsyncHandler`` and rejects the wider ``Awaitable`` shape.
Handler = Callable[[httpx2.Request], Coroutine[None, None, httpx2.Response]]

#: Captured before any test monkeypatches ``httpx2.AsyncClient`` (module
#: import always precedes any fixture/test body) -- `_FakeAsyncClientFactory`
#: uses this, not `httpx2.AsyncClient` itself, to build its real client:
#: `monkeypatch.setattr(httpx2, "AsyncClient", factory)` (patching via this
#: file's own `import httpx2` -- `app.py` does a plain `import httpx2` too,
#: at module scope, so `app_module.httpx2` and this file's `httpx2` name are
#: the exact same `sys.modules` entry; patching this one is enough, and
#: avoids `reportPrivateImportUsage` for reaching into `app_module.httpx2`,
#: a name `app.py` never re-exports) rebinds the attribute on that shared
#: module object -- referencing `httpx2.AsyncClient` from inside the factory
#: itself would recurse into the patched name instead of the real class.
_REAL_ASYNC_CLIENT = httpx2.AsyncClient

#: `_make_github_lifespan`'s returned closure takes the owning `MCPServer` as
#: its sole argument but never reads it (see app.py's docstring) -- every
#: direct-entry test below calls it with this in place of a real server.
_UNUSED_MCP_ARG: Any = None


# --- Test scaffolding ---------------------------------------------------------------


def _generate_pem() -> str:
    """A throwaway RSA key generated in-process (mirrors test_credentials.py/
    test_config.py/test_app.py) -- only tests that actually exercise token
    issuance need a real, parseable PEM."""
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


def _settings(
    *,
    private_key: str = "unused-in-this-test",
    repo_allowlist: frozenset[str] = frozenset({"acme/widgets"}),
) -> Settings:
    # Direct dataclass construction -- same pattern test_server.py/
    # test_github_tools_wiring.py already use.
    return Settings(
        allowed_hosts=("mcp.example.com",),
        public_url="https://mcp.example.com/mcp",
        issuer_url="https://issuer.example.com",
        repo_allowlist=repo_allowlist,
        role_tools={READER_ROLE: ALL_FOUR_TOOLS},
        github_app_id="app-123456",
        github_app_installation_id="install-789012",
        port=8000,
        log_level="INFO",
        read_file_max_bytes=262_144,
        search_code_max_results=30,
        token_refresh_leeway_seconds=300,
        stateless_http=True,
        json_response=True,
        client_tokens={},
        github_app_private_key=private_key,
    )


def _access_token(role: str, *, client_id: str = "client-1") -> AccessToken:
    return AccessToken(
        token="tok", client_id=client_id, scopes=["devoks:read"], claims={"role": role}
    )


@contextlib.contextmanager
def _identity(access_token: AccessToken) -> Generator[None]:
    """See tests/test_github_tools_wiring.py's module docstring: `Client(mcp)`
    bypasses HTTP auth, so a guarded tool call denies fail-safe unless this is set."""
    token = auth_context_var.set(AuthenticatedUser(access_token))
    try:
        yield
    finally:
        auth_context_var.reset(token)


def _iso_far_future() -> str:
    return datetime.fromtimestamp(4_102_444_800.0, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _repo_payload(full_name: str, *, default_branch: str = "main") -> dict[str, object]:
    return {
        "full_name": full_name,
        "name": full_name.split("/", 1)[1],
        "description": None,
        "default_branch": default_branch,
    }


class _GitHubHandler:
    """Fake GitHub backend for the two calls a `list_repos` round trip makes:
    the installation-token exchange (`credentials.py`) and the installation
    repository listing (`client.py`). Records every request it serves."""

    def __init__(self, repos: tuple[dict[str, object], ...] = ()) -> None:
        self.requests: list[httpx2.Request] = []
        self.repos = repos

    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/access_tokens"):
            return httpx2.Response(
                201, json={"token": "ghs_test_token", "expires_at": _iso_far_future()}
            )
        if path == "/installation/repositories":
            return httpx2.Response(200, json={"repositories": list(self.repos)})
        raise AssertionError(f"unexpected GitHub request: {request.method} {request.url}")


class _FakeAsyncClientFactory:
    """Monkeypatch target for ``httpx2.AsyncClient`` at its one production call
    site (``app._make_github_lifespan``): swaps every constructed client's
    transport for a ``MockTransport`` wired to ``handler``, and records each
    call's ``timeout=`` and the resulting client instances -- so tests can
    assert DSN-004's "exactly one shared instance" and the configured timeout
    without any real network access.
    """

    def __init__(self, handler: Handler) -> None:
        self._handler = handler
        self.timeouts: list[float] = []
        self.clients: list[httpx2.AsyncClient] = []

    def __call__(self, *, timeout: float) -> httpx2.AsyncClient:
        self.timeouts.append(timeout)
        client = _REAL_ASYNC_CLIENT(transport=httpx2.MockTransport(self._handler), timeout=timeout)
        self.clients.append(client)
        return client

    @property
    def call_count(self) -> int:
        return len(self.clients)


def _patch_github_http_client(
    monkeypatch: pytest.MonkeyPatch, handler: Handler
) -> _FakeAsyncClientFactory:
    factory = _FakeAsyncClientFactory(handler)
    monkeypatch.setattr(httpx2, "AsyncClient", factory)
    return factory


# --- AC-006-1 / DSN-004: real tool call succeeds end-to-end through the lifespan --


async def test_lifespan_populates_github_and_repo_allowlist_end_to_end(
    monkeypatch: pytest.MonkeyPatch, pem: str
) -> None:  # AC-006-1, DSN-004
    settings = _settings(private_key=pem, repo_allowlist=frozenset({"acme/widgets"}))
    handler = _GitHubHandler(
        repos=(_repo_payload("acme/widgets"), _repo_payload("acme/not-allowed"))
    )
    factory = _patch_github_http_client(monkeypatch, handler.handle)

    mcp = create_server(settings, lifespan=_make_github_lifespan(settings))

    with _identity(_access_token(READER_ROLE)):
        async with Client(mcp) as client:
            result = await client.call_tool(TOOL_LIST_REPOS, {})

    assert result.is_error is False
    assert result.structured_content is not None
    names = [repo["full_name"] for repo in result.structured_content["repos"]]
    # "acme/not-allowed" excluded: installation-visible but not in the
    # configured allowlist -- proves `repo_allowlist` from the lifespan
    # context, not just `github`, actually reached the tool.
    assert names == ["acme/widgets"]
    assert factory.call_count == 1


# --- repo_allowlist: matches Settings verbatim, frozenset (not set/tuple) --------


async def test_lifespan_context_repo_allowlist_matches_settings_as_frozenset(
    monkeypatch: pytest.MonkeyPatch, pem: str
) -> None:  # AC-006-1, DSN-004
    repo_allowlist = frozenset({"acme/widgets", "acme/other"})
    settings = _settings(private_key=pem, repo_allowlist=repo_allowlist)
    _patch_github_http_client(monkeypatch, _GitHubHandler().handle)

    async with _make_github_lifespan(settings)(_UNUSED_MCP_ARG) as ctx:
        assert isinstance(ctx, app_module.GitHubLifespanContext)
        assert isinstance(ctx.github, GitHubClient)
        assert type(ctx.repo_allowlist) is frozenset
        assert ctx.repo_allowlist == repo_allowlist


# --- DSN-004: one http_client instance shared by provider and client -------------


async def test_http_client_is_one_shared_instance_between_provider_and_client(
    monkeypatch: pytest.MonkeyPatch, pem: str
) -> None:  # DSN-004
    settings = _settings(private_key=pem)
    factory = _patch_github_http_client(monkeypatch, _GitHubHandler().handle)

    async with _make_github_lifespan(settings)(_UNUSED_MCP_ARG) as ctx:
        # Exactly one httpx2.AsyncClient was ever constructed for this
        # lifespan entry -- since app._make_github_lifespan's source passes
        # the same local `http_client` to both `InstallationTokenProvider`
        # and `GitHubClient`, this alone proves the sharing DSN-004 requires.
        assert factory.call_count == 1
        assert ctx.github._http_client is factory.clients[0]  # pyright: ignore[reportPrivateUsage]


# --- design requirement: lifespan entry never calls GitHub (lazy token issuance) --


async def test_lifespan_entry_makes_no_github_network_call(
    monkeypatch: pytest.MonkeyPatch, pem: str
) -> None:
    settings = _settings(private_key=pem)
    handler = _GitHubHandler()
    _patch_github_http_client(monkeypatch, handler.handle)

    async with _make_github_lifespan(settings)(_UNUSED_MCP_ARG):
        # Entering alone -- no tool call -- must never touch GitHub: token
        # issuance only happens lazily inside get_token(), on a tool's first
        # real call. A GitHub outage must never turn into a failed startup.
        assert handler.requests == []


# --- DSN-004: http_client closes when the lifespan exits -------------------------


async def test_http_client_closes_when_lifespan_exits(
    monkeypatch: pytest.MonkeyPatch, pem: str
) -> None:
    settings = _settings(private_key=pem)
    factory = _patch_github_http_client(monkeypatch, _GitHubHandler().handle)

    async with _make_github_lifespan(settings)(_UNUSED_MCP_ARG):
        assert factory.clients[0].is_closed is False

    assert factory.clients[0].is_closed is True


# --- exception safety: a mid-construction failure still cleans up what exists ----


async def test_partial_construction_failure_cleans_up_already_built_resources(
    monkeypatch: pytest.MonkeyPatch, pem: str
) -> None:
    settings = _settings(private_key=pem)
    factory = _patch_github_http_client(monkeypatch, _GitHubHandler().handle)

    aclose_calls: list[str] = []
    real_provider_aclose = InstallationTokenProvider.aclose

    async def _spy_provider_aclose(self: InstallationTokenProvider) -> None:
        aclose_calls.append("provider")
        await real_provider_aclose(self)

    monkeypatch.setattr(InstallationTokenProvider, "aclose", _spy_provider_aclose)

    class _BoomingGitHubClient:
        """Stands in for `GitHubClient` at app.py's `GitHubClient.from_settings`
        call site (its third and last construction step) so this test can
        simulate a failure *after* the http_client and provider already exist."""

        @classmethod
        def from_settings(cls, *args: object, **kwargs: object) -> GitHubClient:
            raise RuntimeError("boom: simulated GitHubClient construction failure")

    monkeypatch.setattr(app_module, "GitHubClient", _BoomingGitHubClient)

    lifespan_cm = _make_github_lifespan(settings)
    with pytest.raises(RuntimeError, match="boom"):
        async with lifespan_cm(_UNUSED_MCP_ARG):
            pytest.fail("unreachable: the exception must occur during __aenter__")

    # Ordering (TASK-023 handover requirement): provider.aclose() before
    # http_client.aclose() -- observed here as "provider's cleanup ran" (only
    # cleanup callback registered before the failure) plus the client itself
    # closed, which the AsyncExitStack unwind guarantees runs afterward.
    assert aclose_calls == ["provider"]
    assert factory.clients[0].is_closed is True


# --- backward compatibility: create_server(settings) with no lifespan= -----------


async def test_create_server_without_lifespan_still_builds_and_denies_gracefully(
    pem: str,
) -> None:
    # No httpx2 patch needed: with no lifespan=, the SDK's own default
    # MCP-protocol lifespan yields `{}` and the GitHub tool call is denied by
    # tools.py's own `_require_lifespan` before anything touches GitHub.
    settings = _settings(private_key=pem)
    mcp = create_server(settings)

    with _identity(_access_token(READER_ROLE)):
        async with Client(mcp) as client:
            result = await client.call_tool(TOOL_LIST_REPOS, {})

    assert result.is_error is True
