"""Tool registry — the single collection point for layer adapters' tools (DSN-005, TASK-008).

``register_tools`` is the only place that knows *which* layer adapters exist.
Every other module (``server.py``, an adapter's own ``tools.py``) stays
ignorant of that list. Adding a new source (Notion, Sentry, a second GitHub
org, ...) is meant to cost exactly one directory plus one line here:

1. Create ``adapters/<layer>/<source>/tools.py`` exposing a
   ``register(mcp: MCPServer, guard: Guard) -> None`` function that decorates
   its async tool functions with ``guard(...)`` and adds them to ``mcp``
   (via ``@mcp.tool()`` or ``mcp.add_tool(...)``).
2. Import that function above ``_ADAPTER_REGISTRARS`` and append it to the
   tuple, e.g.::

       from devoks_mcp_management.adapters.knowledge.github.tools import (
           register as register_github_tools,
       )

       _ADAPTER_REGISTRARS: tuple[Registrar, ...] = (register_github_tools,)

Stage 1 ships with **zero** adapters — ``adapters/knowledge/github/tools.py``
is TASK-022's job, which depends on TASK-020/021 (GitHub credentials + REST
client) and is out of scope here. ``_ADAPTER_REGISTRARS`` therefore starts
empty and ``register_tools`` is a deliberate no-op on an empty tuple: the
server boots with an empty tool surface, and ``tools/list`` returns ``[]``
(AC-001-2). This module never registers a placeholder/dummy tool to fill
that gap — a fake tool would show up in the real, audited tool surface this
server exists to protect (see TASK-008 handover notes for the reasoning).

Dependency timing (for TASK-022/023, DSN-004 / DSN-005)
---------------------------------------------------------
A tool is *registered* here at server-construction time (``create_server``,
called once at process startup), but the GitHub client that TASK-022's tools
need does not exist yet at that point — ``DSN-004`` puts its construction in
the ASGI app's ``lifespan`` (TASK-023, ``app.py``), which only runs once the
server starts serving requests. Registration and dependency construction are
therefore two different points in time, and a registrar function must not
try to close over a client instance built during import/registration.

The SDK's own answer to this is the ``Context`` parameter (see
``mcp.server.mcpserver.context.Context``, and
<https://py.sdk.modelcontextprotocol.io/handlers/lifespan/index.md>): a tool
function that adds a parameter annotated ``ctx: Context`` gets it injected
per-call, and ``ctx.request_context.lifespan_context`` is exactly the object
``lifespan`` yielded at startup. So the expected TASK-022/023 shape is::

    # app.py (TASK-023) builds the client once, in an @asynccontextmanager
    # lifespan function passed to MCPServer(..., lifespan=...), and yields
    # it as the lifespan context (a dataclass/TypedDict if more than one
    # value is ever needed).

    # adapters/knowledge/github/tools.py (TASK-022)
    @guard("read_file", repo_arg="repo", audit_args=("repo", "path", "ref"))
    async def read_file(repo: str, path: str, ctx: Context, ref: str | None = None) -> str:
        client: GitHubClient = ctx.request_context.lifespan_context.github_client
        ...

    def register(mcp: MCPServer, guard: Guard) -> None:
        mcp.add_tool(read_file)
        ...

This registry does not need to know about that ``Context`` plumbing at all —
it only ever forwards ``mcp`` and ``guard`` to each registrar unchanged, so
this file needs no change when TASK-023 wires the lifespan.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from devoks_mcp_management.adapters.knowledge.github.tools import register as register_github_tools

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

#: The decorator factory ``tools.guard.make_tool_guard(settings)`` returns —
#: ``guard(tool, *, repo_arg=None, audit_args=())`` applied to a tool
#: function. Registrars receive this already built (one per server
#: instance) rather than building their own, so every registered tool shares
#: one ``Settings``/audit sink.
#:
#: ``guard.py`` types this more precisely as
#: ``Callable[..., Callable[[F], F]]`` for an async-tool-bound ``F`` — that
#: TypeVar only ever gets solved at the point a *specific* tool function is
#: decorated (inside an adapter's own ``tools.py``), not here, where the
#: callable is only ever forwarded, never applied. Spelling the inner
#: ``Callable`` with concrete ``Any`` boundaries (rather than re-declaring a
#: local TypeVar tied to nothing) is what keeps this alias non-generic and
#: therefore usable in a plain function signature below.
Guard = Callable[..., Callable[[Callable[..., Any]], Callable[..., Any]]]

#: One registrar per adapter: ``(mcp, guard) -> None``, expected to register
#: its tools onto ``mcp`` (typically via ``@guard(...)`` then
#: ``mcp.add_tool``) and return nothing.
Registrar = Callable[["MCPServer", Guard], None]

#: DSN-005 collection point. See module docstring for how TASK-022 appends
#: to this.
_ADAPTER_REGISTRARS: tuple[Registrar, ...] = (register_github_tools,)


def register_tools(mcp: MCPServer, guard: Guard) -> None:
    """Register every layer adapter's tools onto ``mcp``.

    Called once from ``server.create_server`` after ``guard`` has been built
    for that server instance. A no-op when ``_ADAPTER_REGISTRARS`` is empty
    (Stage 1's current state, AC-001-2) — no exception, no placeholder tool.
    """
    for register in _ADAPTER_REGISTRARS:
        register(mcp, guard)
