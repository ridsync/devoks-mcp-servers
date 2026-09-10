"""ASGI composition root: Starlette(``/healthz`` + ``Mount /mcp``) + lifespan (TASK-009).

``create_app(settings) -> Starlette`` is a **factory**, mirroring
``server.create_server``'s own module docstring rationale: nothing at module
scope builds a ``Settings`` or a ``Starlette`` app, so importing this module
never needs a populated environment and never raises ``ConfigError``.

``create_app_from_env`` is the process entry point uvicorn/the container
actually run — see its docstring for the exact invocation. TASK-030
(Dockerfile), TASK-031 (CI smoke test), and TASK-032 (README) all point at
this function; changing its name or signature is a breaking change for them.

Route assembly (CTR-001) — read before touching the ``routes=`` list
----------------------------------------------------------------------
``mcp.streamable_http_app()`` already returns a complete ``Starlette`` app
whose own route is ``/mcp`` (``streamable_http_path`` defaults to that) plus,
because ``server.create_server`` always supplies both ``token_verifier=`` and
``auth=``, a second route the SDK adds itself: RFC 9728 Protected Resource
Metadata at ``/.well-known/oauth-protected-resource`` + the *path component*
of ``settings.public_url`` (verified against the installed ``mcp==2.1.1``,
``mcp.server.auth.routes.create_protected_resource_routes`` /
``build_resource_metadata_url``). For that to land on the fixed
``/.well-known/oauth-protected-resource/mcp`` path CTR-001 requires,
``MCP_PUBLIC_URL`` must itself carry a ``/mcp`` path **with no trailing
slash**: ``https://mcp.example.com/mcp``, not
``https://mcp.example.com/mcp/`` (produces
``.../oauth-protected-resource/mcp/``, an extra trailing slash CTR-001 does
not have) and not the bare domain ``https://mcp.example.com`` (produces
``.../oauth-protected-resource`` with no ``/mcp`` suffix at all — verified
empirically against all three shapes, not merely inferred). This is
independent of anything in this file; it is a property of the value
operators put in ``MCP_PUBLIC_URL`` (**see handover notes for TASK-032's
``.env.example``**).

Given that, this module mounts the MCP sub-app at the *root* —
``Mount("/", app=mcp.streamable_http_app(...))`` — not at ``Mount("/mcp",
...)``. Mounting at ``/mcp`` would double the prefix (the sub-app's own route
is already ``/mcp``), producing ``/mcp/mcp`` and
``/mcp/.well-known/oauth-protected-resource/mcp`` — silently breaking both
CTR-001 and AC-002-4. This exact trap, and the ``Mount("/", ...)`` fix, is
documented at
<https://py.sdk.modelcontextprotocol.io/run/asgi/index.md#mounting-it>.
Because ``Mount("/")`` matches every path, ``/healthz`` is listed *before*
it in ``routes=`` — Starlette tries routes in list order, and anything after
a ``Mount("/")`` is unreachable.

Lifespan (DSN-004) — two distinct "lifespan"s, both live here now
-------------------------------------------------------------------
There are **two** distinct "lifespan"s in play, and TASK-023 wires both:

1. **The ASGI/transport lifespan** — entered below, in this module's
   ``lifespan()``. It owns ``mcp.session_manager.run()``, the StreamableHTTP
   session manager's background task group. Skipping this is not a style
   choice: the SDK's own docs warn that mounting the MCP sub-app *disables*
   the built-in lifespan ``streamable_http_app()`` wires into the object it
   returns (a mounted sub-application's lifespan is never invoked by
   Starlette), so the **host** app — this one — must enter it explicitly, or
   the first request to ``/mcp`` fails with ``RuntimeError: Task group is
   not initialized``.
2. **The MCP protocol lifespan** — the ``lifespan=`` keyword
   ``MCPServer.__init__`` accepts (see ``mcp.server.mcpserver.server``),
   forwarded through ``server.create_server(settings, lifespan=...)``
   (TASK-023). This is the one that populates
   ``ctx.request_context.lifespan_context`` inside a tool function (see
   ``tools/registry.py``'s module docstring for the exact shape TASK-022's
   tools expect, and ``adapters/knowledge/github/tools.py``'s
   ``GitHubToolContext`` for this module's half of that contract).

**These do not need a shared ``AsyncExitStack`` spanning both** — reading
``mcp.server.streamable_http_manager.StreamableHTTPSessionManager.run()`` in
the installed ``mcp==2.1.1`` shows it does ``async with
self.app.lifespan(self.app) as lifespan_state, anyio.create_task_group() as
tg:`` where ``self.app`` is the low-level ``Server`` wrapped by
``lifespan_wrapper`` around whatever was passed as ``MCPServer(...,
lifespan=...)``. In other words, entering ``mcp.session_manager.run()``
(transport lifespan, #1, below) **already enters the MCP-protocol lifespan
(#2) as part of the same ``async with``**, once, for the process's lifetime
— ``lifespan()`` below needs no change of shape to pick up #2; it only needs
``create_server`` to be called with one.

``_github_lifespan`` (built by ``_make_github_lifespan``, below) is the
``lifespan=`` value handed to ``create_server``. Its own body is where
``contextlib.AsyncExitStack`` actually does its job — constructing the
shared ``httpx2.AsyncClient`` (DSN-004: exactly one, injected into both the
token provider and the GitHub REST client), then the token provider, then
the client, registering each resource's cleanup immediately after
constructing it so a mid-construction exception (or, ordinarily, process
shutdown) unwinds only what was actually built, in reverse order. No GitHub
network call happens anywhere in this path — token issuance is lazy, inside
``InstallationTokenProvider.get_token()``, the first time a tool actually
calls GitHub — so a GitHub outage can never turn into a failed server
startup here; only a genuine local construction failure (there currently is
none) would.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version
from typing import Final

import httpx2
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from devoks_mcp_management.adapters.knowledge.github.client import GitHubClient
from devoks_mcp_management.adapters.knowledge.github.credentials import InstallationTokenProvider
from devoks_mcp_management.config import Settings, load_settings
from devoks_mcp_management.server import SERVER_NAME, create_server

#: Must match ``[project].name`` in ``servers/management/pyproject.toml`` —
#: that is the distribution name ``importlib.metadata`` looks up, not the
#: importable package name (they happen to be spelled the same here).
_DISTRIBUTION_NAME = "devoks_mcp_management"

#: Network timeout for the one shared ``httpx2.AsyncClient`` this lifespan
#: builds (DSN-004) — applies to every GitHub REST call made through it
#: (connect/read/write/pool alike; ``httpx2.AsyncClient(timeout=<float>)``
#: applies one bound to all four). ``httpx2.AsyncClient()``'s own default is
#: ``Timeout(timeout=5.0)`` (verified against the installed ``httpx2>=2.5.0``
#: package) — tight enough that an ordinary GitHub slow patch (paginated
#: ``list_installation_repositories`` across many pages, or a large file read
#: through the ``EDGE-012`` raw-media-type fallback) could spuriously fail a
#: tool call. Leaving the client's timeout unset entirely is not the fix
#: either: this process runs as an ECS task, and a connection that never
#: times out can wedge that task's request-handling indefinitely on a single
#: stalled GitHub call. 30s is the explicit middle ground — generous enough
#: to absorb realistic GitHub latency without another retry layer, short
#: enough that a genuinely stuck connection still surfaces as an ordinary
#: ``ToolError`` (via ``client.py``'s own ``httpx2.HTTPError`` handling)
#: within one request's lifetime rather than hanging it forever.
# EDGE-018: kept strictly *below* the fronting layer's own ceiling so this
# server is always the one that times out first. API Gateway HTTP API's
# integration timeout is a hard 30s maximum (configurable 50–30,000 ms, not
# raisable), so an equal 30.0 here left zero headroom: a slow GitHub reply
# would surface as an API Gateway 504 that never passes through this
# server's own error normalization (EDGE-003 rate-limit hint / EDGE-009
# tool-error shaping), losing the audit record's `error_kind` too.
_GITHUB_HTTP_TIMEOUT_SECONDS: Final = 20.0


@dataclass(frozen=True, slots=True)
class GitHubLifespanContext:
    """The MCP-protocol lifespan value this app yields (DSN-004, AC-006-1).

    Structurally satisfies ``adapters.knowledge.github.tools.GitHubToolContext``
    — that module's own ``_require_lifespan`` re-validates both attribute
    names and types with ``isinstance`` at call time (its docstring explains
    why), so this dataclass does not need to inherit from that ``Protocol``;
    matching its two attribute names and types exactly is what makes the
    match. Frozen, matching this project's convention for every other
    constructed-once value (``config.Settings``, ``client.py``'s return
    types, ...).
    """

    github: GitHubClient
    repo_allowlist: frozenset[str]


#: The exact shape ``MCPServer(..., lifespan=...)`` (and, forwarded,
#: ``server.create_server(..., lifespan=...)``) needs — named so
#: ``_make_github_lifespan``'s own return type stays under this project's
#: line-length limit.
_GitHubLifespan = Callable[
    [MCPServer[GitHubLifespanContext]], AbstractAsyncContextManager[GitHubLifespanContext]
]


def _make_github_lifespan(settings: Settings) -> _GitHubLifespan:
    """Build the MCP-protocol ``lifespan=`` value for ``create_server`` (DSN-004).

    Returns a fresh async-context-manager factory closed over ``settings`` —
    not the entered context manager itself — because ``MCPServer(...,
    lifespan=...)`` needs a *callable* it invokes itself once
    ``mcp.session_manager.run()`` starts (see the module docstring's
    "Lifespan" section for exactly when that happens).

    Construction order and cleanup, once entered
    -----------------------------------------------
    1. One ``httpx2.AsyncClient`` (``_GITHUB_HTTP_TIMEOUT_SECONDS``) — the
       single instance DSN-004 requires shared between the token provider and
       the GitHub REST client. Its ``aclose()`` is registered with the exit
       stack immediately, before anything else is built.
    2. ``InstallationTokenProvider.from_settings(settings, http_client=...)``
       — never calls GitHub itself (token issuance is lazy; see module
       docstring). Its ``aclose()`` (drops only its own cached token state,
       never the injected client — see that module's own docstring) is
       registered immediately after.
    3. ``GitHubClient.from_settings(settings, http_client=..., token_provider=...)``
       — has no ``aclose()`` of its own (it owns no resource beyond the
       shared client and the provider, both already covered above).

    ``AsyncExitStack`` unwinds in reverse registration order on exit *or* on
    an exception raised partway through this sequence — so ``provider.aclose()``
    always runs before ``http_client.aclose()`` (the ordering TASK-023's
    handover notes require), and a failure after step 1 still closes the
    client that step already opened rather than leaking it.
    """

    @asynccontextmanager
    async def _github_lifespan(
        _: MCPServer[GitHubLifespanContext],
    ) -> AsyncGenerator[GitHubLifespanContext]:
        async with AsyncExitStack() as stack:
            http_client = httpx2.AsyncClient(timeout=_GITHUB_HTTP_TIMEOUT_SECONDS)
            stack.push_async_callback(http_client.aclose)

            provider = InstallationTokenProvider.from_settings(settings, http_client=http_client)
            stack.push_async_callback(provider.aclose)

            github = GitHubClient.from_settings(
                settings, http_client=http_client, token_provider=provider
            )

            yield GitHubLifespanContext(github=github, repo_allowlist=settings.repo_allowlist)

    return _github_lifespan


def _server_version() -> str:
    """Package version for the ``/healthz`` body (AC-001-3).

    Sourced from installed package metadata rather than a hardcoded literal
    so the two can never drift. The ``PackageNotFoundError`` fallback is
    defensive only — every supported run path (``uv sync`` locally, the
    container image) installs this distribution with metadata — but a
    missing distribution must never turn a public, unauthenticated health
    check into a 500.
    """
    try:
        return _package_version(_DISTRIBUTION_NAME)
    except PackageNotFoundError:
        return "0.0.0+unknown"


def _expand_allowed_hosts(hosts: tuple[str, ...]) -> list[str]:
    """Derive ``TransportSecuritySettings.allowed_hosts`` from ``Settings.allowed_hosts``.

    ``allowed_hosts`` entries are matched by exact string equality against
    the request's ``Host`` header (verified against
    ``mcp.server.transport_security.TransportSecurityMiddleware._validate_host``
    in the installed ``mcp==2.1.1``) — a bare ``"mcp.example.com"`` entry
    matches only a portless ``Host`` header, and only a literal
    ``"mcp.example.com:*"`` entry matches one with a port. A request's actual
    ``Host`` header can carry a port or not depending on what sits in front
    of this server (a local ``uvicorn`` run almost always includes one; an
    ALB in front of a standard 443/80 listener usually does not) — the
    SDK's own deploy guide lists both forms side by side for exactly this
    reason. Rather than push that duplication onto every value of
    ``MCP_ALLOWED_HOSTS`` (DSN-007: this is an env-injected list, so every
    redundant entry is an extra thing to get right *and* keep in sync across
    environments), each configured host is expanded to both forms here,
    unless it already specifies a port (contains ``:``) — an operator who
    deliberately pins a host to a fixed port meant only that port to match.
    """
    expanded: list[str] = []
    for host in hosts:
        expanded.append(host)
        if ":" not in host:
            expanded.append(f"{host}:*")
    return expanded


async def _healthz(request: Request) -> JSONResponse:
    """GET /healthz (CTR-001, AC-001-3) — unauthenticated by design.

    An ALB/orchestrator health check never carries a Bearer token, so this
    route sits directly on the outer Starlette app, outside the MCP sub-app
    ``Mount`` entirely — it never passes through ``TransportSecurity`` or
    ``TokenVerifier``. The body is intentionally minimal (name + version
    only): this is a public endpoint, so it must never echo back
    ``Settings`` or any other configuration value.
    """
    return JSONResponse({"name": SERVER_NAME, "version": _server_version()})


def create_app(settings: Settings) -> Starlette:
    """Build one Starlette app wired for CTR-001's three routes, from ``settings``.

    A factory, not a module-level singleton — see the module docstring.
    Call this once per process (or once per ``Settings`` in a test); nothing
    here is safe or unsafe to call twice, it just builds independent objects
    each time, the same guarantee ``server.create_server`` already gives.
    """
    mcp = create_server(settings, lifespan=_make_github_lifespan(settings))

    # Root logger *threshold* only, not a second logging.basicConfig(...):
    # create_server() -> MCPServer.__init__() already called
    # mcp.server.mcpserver.utilities.logging.configure_logging("INFO") (an
    # SDK-internal, hardcoded default — server.py has no parameter to
    # override it), which calls logging.basicConfig(...) and installs the
    # root logger's handlers. basicConfig() only takes effect on a root
    # logger that has no handlers yet, so calling it again here would
    # silently do nothing. setLevel() changes the threshold regardless of
    # who installed the handlers, which is what actually makes
    # MCP_LOG_LEVEL affect server-log verbosity (e.g. the transport
    # security middleware's Host/Origin rejection warnings, AC-001-4). This
    # is deliberately separate from audit logging (audit/logger.py writes
    # its JSON line straight to stdout, bypassing the logging module
    # entirely) — an operator raising MCP_LOG_LEVEL never filters audit
    # records, only this server-log stream.
    logging.getLogger().setLevel(settings.log_level)

    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_expand_allowed_hosts(settings.allowed_hosts),
        # EDGE-011 / DSN-007: CTR-006 (FRD §5.2) defines no env key for a
        # browser-facing Origin allowlist, and this task is explicitly not
        # authorized to add one (a new env key is an FRD contract change —
        # see the handover notes for what the main loop needs to decide).
        # An empty list is the safe default in the meantime:
        # TransportSecurityMiddleware only checks Origin when the header is
        # present at all (same-origin requests and every non-browser MCP
        # client never send one, per
        # mcp.server.transport_security.TransportSecurityMiddleware._validate_origin),
        # so this blocks browser-based callers outright — the conservative
        # direction — rather than guessing at an allowlist nobody
        # configured. Stage 1 has no browser client in FRD §2's context.
        allowed_origins=[],
    )

    # CTR-011 / FRD §7 "배포 타깃 제약": both flags come from settings so the
    # one image runs on Lambda (both True — the default) and, unchanged,
    # behind a sticky-session load balancer (both False).
    #
    # `stateless_http=True` does NOT remove the need for the `lifespan()`
    # below. Verified against the installed mcp==2.1.1 source:
    # `StreamableHTTPSessionManager.run()` is what enters the MCP-protocol
    # lifespan (`_make_github_lifespan`, passed to `create_server` above) and
    # creates the anyio task group that `_handle_stateless_request` starts
    # each per-request transport in. Stateless mode only stops the manager
    # from tracking `_server_instances`/`_session_owners`; it does not make
    # `run()` optional. `run()` also raises RuntimeError if called twice per
    # instance, which is why the app must be built once per process (uvicorn
    # boots it once per container) and never per Lambda invocation.
    mcp_app = mcp.streamable_http_app(
        transport_security=security,
        stateless_http=settings.stateless_http,
        json_response=settings.json_response,
    )

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncGenerator[None]:
        """Enter/exit the StreamableHTTP session manager for the app's lifetime.

        See the module docstring ("Lifespan (DSN-004)") for why this specific
        line is required once the MCP app is *mounted* rather than run
        standalone, and for why entering it also enters the MCP-protocol
        lifespan ``create_server`` was given above (``_make_github_lifespan``)
        — nothing further needs to happen in this function for TASK-023.
        """
        async with mcp.session_manager.run():
            yield

    return Starlette(
        routes=[
            # Must precede the Mount("/", ...) below: Starlette matches
            # routes in list order, and Mount("/") matches every path, so
            # anything listed after it is unreachable.
            Route("/healthz", endpoint=_healthz, methods=["GET"]),
            Mount("/", app=mcp_app),
        ],
        lifespan=lifespan,
    )


def create_app_from_env(env: Mapping[str, str] | None = None) -> Starlette:
    """Process entry point: ``uvicorn devoks_mcp_management.app:create_app_from_env --factory``.

    Chosen over a lazily-evaluated module-level ``app = create_app(...)``
    attribute so that **importing this module never reads the environment
    or can raise ``ConfigError``** — only calling this function does. This
    is the exact entry point TASK-030 (Dockerfile ``CMD``), TASK-031 (CI
    health-check smoke test), and TASK-032 (README run instructions) are
    expected to invoke; ``env`` defaults to ``os.environ`` and exists only
    so a caller (a future ``__main__``, a test) can inject a different
    mapping without mutating process state.
    """
    return create_app(load_settings(env if env is not None else os.environ))
