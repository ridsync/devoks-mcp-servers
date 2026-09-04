"""Composition root: build an ``MCPServer`` wired for auth, RBAC, and audit (TASK-008).

``create_server(settings) -> MCPServer`` is a **factory**, not a module-level
singleton. Nothing at module scope constructs an ``MCPServer`` or reads
``Settings`` — a server built at import time would need a fully-populated
environment just to import this module, which would make it impossible for
a test to build several servers from several ``Settings`` in one process,
and would turn "import this module" into an operation that can raise
``ConfigError``.

``token_verifier=`` and ``auth=AuthSettings(...)`` are always constructed and
passed together (FRD §7 constraint): reading
``mcp.server.mcpserver.server.MCPServer.__init__`` in the installed
``mcp==2.1.1`` package confirms it raises ``ValueError`` at construction time
— before any request is served — if ``auth`` is given without a verifier, or
a verifier is given without ``auth``. There is no code path here that could
supply one without the other, so that failure mode is structurally
unreachable rather than merely tested against.

``app.py`` (TASK-009) is the next composition layer up — it takes the
``MCPServer`` this factory returns and mounts it into a Starlette app
alongside ``/healthz`` and ``transport_security``. Nothing ASGI-related
belongs in this module.

``lifespan=`` (TASK-023, DSN-004) — the *MCP protocol* lifespan, not ASGI
------------------------------------------------------------------------
``create_server`` accepts an optional ``lifespan=`` and forwards it verbatim
into ``MCPServer(...)``. This module never builds one itself and never
imports anything from ``adapters/*`` — building the GitHub credential
provider/HTTP client needs an ``httpx2.AsyncClient`` whose lifetime and
cleanup ``app.py`` owns (see that module's docstring), and this module's own
job (§7 auth wiring, RBAC guard, tool registration) has nothing to do with
that. Two overloads keep every existing call site (``create_server(settings)``
with no ``lifespan``) typed exactly as before, ``MCPServer[None]`` — the
generic parameter only changes to whatever type a caller's ``lifespan``
yields once one is actually supplied. See
<https://py.sdk.modelcontextprotocol.io/handlers/lifespan/index.md> for the
``lifespan=`` contract this mirrors.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Final, overload

from mcp.server import MCPServer
from mcp.server.auth.settings import AuthSettings

from devoks_mcp_management.auth.verifier import StaticTableTokenVerifier
from devoks_mcp_management.config import Settings
from devoks_mcp_management.tools.guard import make_tool_guard
from devoks_mcp_management.tools.registry import Guard, register_tools

#: CTR-002 / FRD §5.1 fixes this exact scope list for Stage 1 ("required_scopes
#: 는 [\"devoks:read\"]"). Kept as a module constant rather than sourced from
#: ``Settings``: it is part of the CTR-002 contract itself, not a
#: per-deployment knob — letting an operator override it via the environment
#: would silently change what AC-002-5/EDGE-010 mean without a spec change.
REQUIRED_SCOPES: Final[list[str]] = ["devoks:read"]

#: Name advertised to MCP clients (``MCPServer.name`` / the low-level
#: ``Server``'s ``name``). Not derived from ``Settings`` — the server's
#: identity on the wire is a code-level constant, distinct from
#: deployment-level config like ``public_url``.
SERVER_NAME: Final = "devoks-management-mcp"


@overload
def create_server(settings: Settings) -> MCPServer[None]: ...


#: ``[LifespanResultT]`` (PEP 695 syntax, this project's Python 3.14 floor) is
#: the MCP-protocol lifespan's yielded context type — solved per call site
#: from whatever ``lifespan=`` a caller supplies (e.g. ``app.py``'s
#: ``GitHubLifespanContext``). Omitting ``lifespan=`` entirely resolves
#: through the overload above to ``None`` instead, matching the SDK's own
#: default lifespan (``mcp.server.lowlevel.server.lifespan``), which yields
#: nothing.
@overload
def create_server[LifespanResultT](
    settings: Settings,
    *,
    lifespan: Callable[[MCPServer[LifespanResultT]], AbstractAsyncContextManager[LifespanResultT]],
) -> MCPServer[LifespanResultT]: ...


def create_server(
    settings: Settings,
    *,
    lifespan: Callable[[MCPServer[Any]], AbstractAsyncContextManager[Any]] | None = None,
) -> MCPServer[Any]:
    """Build one ``MCPServer`` instance from ``settings``.

    - ``lifespan``: forwarded to ``MCPServer(..., lifespan=...)`` unchanged
      (TASK-023, DSN-004) — see the module docstring for why this module
      never constructs one itself. Omitting it (the first overload) yields
      ``MCPServer[None]``, matching the SDK's own default MCP-protocol
      lifespan.
    - ``token_verifier``: ``StaticTableTokenVerifier`` bound to
      ``settings.client_tokens`` (DSN-001) — resolves a Bearer token to an
      ``AccessToken`` for every request the SDK's own auth middleware admits.
    - ``auth``: ``AuthSettings`` whose ``resource_server_url``/``issuer_url``
      mirror ``settings.public_url``/``settings.issuer_url`` (AC-002-4's
      premise — the RFC 9728 metadata document's ``resource`` must match the
      configured public URL exactly) and whose ``required_scopes`` is the
      CTR-002 scope list above (AC-002-5, EDGE-010).

      ``issuer_url``/``resource_server_url`` are passed as the **plain
      strings** already validated by ``config.load_settings`` — not
      pre-wrapped in ``pydantic.AnyHttpUrl(...)``. Measured against the
      installed ``mcp==2.1.1`` (``AuthSettings.model_fields``): its
      ``model_config = ConfigDict(url_preserve_empty_path=True)`` only takes
      effect while pydantic validates a **raw string** into the field —
      handing it an *already-constructed* ``AnyHttpUrl`` (e.g. from
      pre-wrapping with ``AnyHttpUrl(settings.public_url)``, as an earlier
      draft of this module did) skips that re-validation and the value keeps
      whatever normalization ``AnyHttpUrl(...)`` itself already applied,
      which unconditionally appends a trailing ``/`` to a path-less URL. A
      path-less ``settings.public_url`` (the common case) would then end up
      as ``resource_server_url`` with a trailing slash it never had —
      breaking the exact-match premise of AC-002-4. Passing the strings
      through lets ``AuthSettings`` validate them itself and preserve the
      canonical (no trailing slash) form. See the TASK-008 handover notes
      for the same finding.
    - A ``tools.guard.make_tool_guard`` instance is built once here from the
      same ``settings`` and handed to ``tools.registry.register_tools``,
      which is currently a no-op (DSN-005; no adapters exist yet — see that
      module's docstring for TASK-022's hook-in point).
    """
    token_verifier = StaticTableTokenVerifier.from_settings(settings)
    auth = AuthSettings(
        # str -> AnyHttpUrl coercion happens at pydantic runtime validation
        # (see the paragraph above) but pyright's generated `__init__` stub
        # for `AuthSettings` is typed to the post-coercion field type, so a
        # plain `str` here is a real, expected mismatch from pyright's point
        # of view — not a mistake to silently work around differently.
        issuer_url=settings.issuer_url,  # pyright: ignore[reportArgumentType]
        resource_server_url=settings.public_url,  # pyright: ignore[reportArgumentType]
        required_scopes=REQUIRED_SCOPES,
    )
    mcp: MCPServer[Any] = MCPServer(
        SERVER_NAME,
        token_verifier=token_verifier,
        auth=auth,
        lifespan=lifespan,
    )

    # `Guard`-typed explicitly: `make_tool_guard`'s own return annotation
    # (`Callable[..., Callable[[F], F]]`) carries a TypeVar that only gets
    # solved when a *specific* tool function is decorated — which never
    # happens in this module — so an explicit annotation is what tells
    # pyright to treat this value as `Guard` (see registry.py) rather than
    # report the otherwise-unresolved TypeVar as unknown.
    guard: Guard = make_tool_guard(settings)
    register_tools(mcp, guard)

    return mcp
