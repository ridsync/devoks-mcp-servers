"""Core 4 GitHub MCP tools: argument schemas and response transformation (TASK-022).

`register(mcp, guard) -> None` is this module's export, per `tools/registry.py`'s
contract — `server.create_server` calls it once (through `tools.registry.register_tools`)
with a `Guard` already built from that server instance's `Settings`.

Tool functions are defined **undecorated** at module scope and only wrapped with
`guard(...)` inside `register()`, never with a bare `@guard(...)` atop the `def`.
`guard` does not exist at import time — it is `tools.guard.make_tool_guard(settings)`'s
return value, built once per server instance from that instance's `Settings`
(see `tools/guard.py`'s "Why a factory" and `tools/registry.py`'s "Dependency timing"
sections) — so decoration has to happen at `register()`'s call time, not at module
definition time.

Why the JSON Schema `guard`-wrapping risk turned out to be a non-issue
-----------------------------------------------------------------------
`tools/guard.py`'s wrapper already applies `functools.wraps(fn)`, which copies
`__wrapped__ = fn` onto the wrapper. Both places the SDK derives a tool's shape from
follow that chain automatically:

- `mcp.server.mcpserver.utilities.func_metadata.func_metadata` builds the input-schema
  Pydantic model from `inspect.signature(func, eval_str=True)`, and `inspect.signature`
  defaults `follow_wrapped=True` — it silently unwraps to the *original* `fn`'s real
  signature (parameter names, types, defaults), not the wrapper's `*args, **kwargs`.
- `mcp.server.mcpserver.utilities.context_injection.find_context_parameter` calls
  `typing.get_type_hints(wrapper)`; `get_type_hints` walks the same `__wrapped__` chain
  to resolve forward-reference globals against `fn`'s own module, while still reading
  `hints = wrapper.__annotations__` — the very dict `functools.wraps` copied from `fn` —
  so the `ctx: Context` parameter is found and excluded from the schema (`skip_names`)
  exactly as if `find_context_parameter` had been run on `fn` directly.

Verified empirically, not just by source reading — see this task's test file for a
`tools/list` assertion that every one of the 4 tools' `input_schema.properties` names
its real domain parameters (`repo`/`path`/`ref`/`query`) and never `ctx`, `args`, or
`kwargs`. Net effect: `tools/guard.py` needed no change for this task.

Lifespan contract with TASK-023 (`GitHubToolContext`)
-------------------------------------------------------
A tool is *registered* at `create_server` time, well before TASK-023's lifespan ever
runs (`tools/registry.py`'s "Dependency timing" section) — so every tool body reaches
for its `GitHubClient` through `ctx.request_context.lifespan_context` at *call* time,
per-request, rather than closing over one built at import/registration time.

`GitHubToolContext` (below) is this module's half of that contract: the Protocol shape
TASK-023's lifespan value must satisfy. This module never trusts the Protocol as a
*runtime* guarantee, though — a `Protocol` is a static-only check unless decorated
`@runtime_checkable`, and even then only verifies attribute presence, never that
`github` actually holds a `GitHubClient`. Until TASK-023 wires a real lifespan into
`MCPServer(...)`, the SDK's own default lifespan (`mcp.server.lowlevel.server.lifespan`,
verified in the installed `mcp==2.1.1`) yields a bare `{}` — so `_require_lifespan`
below always re-validates both attributes with `isinstance` before use, turning a
missing/wrong-shaped lifespan context into a clear `ToolError` (never a silent
`AttributeError` from attribute access on a plain `dict`).

`repo_allowlist` rides along in the same Protocol, not just `github`
-----------------------------------------------------------------------
`list_repos` (`AC-005-1`) is the one tool with no `repo` argument for `guard(...)`'s
`repo_arg` mechanism to check — filtering against the allowlist is explicitly this
tool's own job (handover note, `tools/guard.py`'s docstring). But nothing hands a
`Settings` (or its `repo_allowlist`) to `register(mcp, guard)` — the `Registrar`
signature is `(mcp, guard) -> None` only (`tools/registry.py`). The lifespan context
is the only value TASK-023 constructs *with* access to `Settings` that also reaches
every tool call, so `repo_allowlist: frozenset[str]` is carried on `GitHubToolContext`
alongside `github` rather than invented as a second injection path.

`list_repos`: installation-list-then-filter, not per-allowlist-entry lookups
-----------------------------------------------------------------------------
`GitHubClient` has no "fetch one repository's details" method — only
`list_installation_repositories()` returns the `RepositorySummary` fields `AC-005-1`
needs (`name`/`description`/`default_branch`), and adding a new client method is out of
this task's scope (`client.py` is on the "하지 말 것" list). So the only feasible
(and correct) strategy is: fetch the installation's full, already-paginated repository
list once, then keep only the entries also present in `repo_allowlist`
(`auth.policy.is_repo_allowlisted`). This also directly satisfies AC-005-1's "installation
이 못 보는 allowlist 항목은 결과에 없어야 한다" — an allowlist entry the installation
never returned is never synthesized into the result, since the loop only ever narrows
what GitHub actually reported.

`search_code`'s rate-limit note lives in its own docstring (RES-API-004: GitHub's code
search endpoint allows only 10 authenticated requests/minute — its own, much tighter
bucket than other search endpoints' 30/minute), not just in this module docstring,
because the tool docstring is what actually reaches the calling model.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol, TypedDict, cast

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

from devoks_mcp_management.adapters.knowledge.github.client import (
    FileContent,
    GitHubClient,
    RepositorySummary,
    SearchResultItem,
    SearchResults,
    TreeEntry,
)
from devoks_mcp_management.auth.policy import is_repo_allowlisted
from devoks_mcp_management.types import (
    TOOL_GET_REPO_TREE,
    TOOL_LIST_REPOS,
    TOOL_READ_FILE,
    TOOL_SEARCH_CODE,
)

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    # Deferred to type-checking only: `registry.py` imports this module's
    # `register` at its own module scope (`tools/registry.py`'s "how TASK-022
    # appends" section), so a *runtime* import here of anything from
    # `registry.py` would be circular. `Guard` is only ever used in a type
    # position below, and `from __future__ import annotations` (this file's
    # first import) keeps every annotation an unevaluated string at runtime,
    # so this forward reference never needs to actually resolve outside a
    # type checker.
    from devoks_mcp_management.tools.registry import Guard

__all__ = ["GitHubToolContext", "register"]


class GitHubToolContext(Protocol):
    """Expected shape of the MCP *protocol* lifespan context these tools need.

    TASK-023 constructs the real value inside its `lifespan()` and yields it as the
    argument `MCPServer(..., lifespan=...)` receives — see the module docstring's
    "Lifespan contract with TASK-023" section for why this is a `Protocol` rather
    than a concrete dataclass this module owns, and for why every attribute read
    through it is still re-validated with `isinstance` at call time.

    Attribute names below are exact and load-bearing: TASK-023 must populate an
    object exposing precisely `github` and `repo_allowlist`.
    """

    github: GitHubClient
    """The `GitHubClient` TASK-023 builds once at startup (`DSN-004`)."""

    repo_allowlist: frozenset[str]
    """`CTR-008`'s configured allowlist — `Settings.repo_allowlist` verbatim.
    Only `list_repos` reads this; see the module docstring for why."""


def _require_lifespan(ctx: Context) -> tuple[GitHubClient, frozenset[str]]:
    """Fetch and validate `ctx`'s lifespan context against `GitHubToolContext`.

    Raises `ToolError` — never a bare `AttributeError` — when the lifespan context is
    absent (the SDK's own default lifespan yields `{}` until TASK-023 wires the real
    one) or does not match the expected shape. See `GitHubToolContext`'s docstring for
    why this re-checks with `isinstance` rather than trusting the static Protocol
    annotation alone.
    """
    lifespan_context = ctx.request_context.lifespan_context
    github = getattr(lifespan_context, "github", None)
    repo_allowlist = getattr(lifespan_context, "repo_allowlist", None)
    if not isinstance(github, GitHubClient) or not isinstance(repo_allowlist, frozenset):
        raise ToolError(
            "GitHub tools are unavailable: the server has not finished configuring its "
            "GitHub client yet. This is a server configuration issue, not a caller "
            "error — contact the server operator."
        )
    # `isinstance(x, frozenset)` only narrows the container, not its element type
    # (pyright: reportUnknownVariableType) -- `repo_allowlist` is this server's own
    # `Settings.repo_allowlist` (already `frozenset[str]`-validated by `config.py`),
    # never external/untrusted input, so a shallow container check is sufficient and
    # this cast is safe.
    return github, cast(frozenset[str], repo_allowlist)


# --- Structured response payloads (SDK auto-detects a `TypedDict` return as this
# tool's `output_schema` / `structured_content` — see `func_metadata`'s docstring
# in the installed `mcp==2.1.1`) --------------------------------------------------


class RepoSummaryPayload(TypedDict):
    full_name: str
    name: str
    description: str | None
    default_branch: str


class ListReposResult(TypedDict):
    repos: list[RepoSummaryPayload]
    count: int


class TreeEntryPayload(TypedDict):
    name: str
    path: str
    type: str
    size: int


class GetRepoTreeResult(TypedDict):
    repo: str
    path: str
    ref: str | None
    entries: list[TreeEntryPayload]
    count: int


#: Mirrors `client.ContentStatus` (not imported directly: that alias is not in
#: `client.py`'s `__all__`, so this module treats it as private to that module and
#: keeps its own copy — the 4 values are closed-by-design, see that module's
#: `FileContent` docstring, "Why no fifth status value was added").
ReadFileStatus = Literal["complete", "truncated", "binary", "unavailable"]


class ReadFileResult(TypedDict):
    repo: str
    path: str
    ref: str | None
    status: ReadFileStatus
    content: str | None
    """The file's content, or a byte-prefix of it — **check `status` before trusting
    this as the whole file**; see `read_file`'s own docstring, which is what reaches
    the calling model."""
    returned_size: int
    total_size: int
    message: str | None


class SearchResultItemPayload(TypedDict):
    repository: str
    path: str
    excerpt: str | None


class SearchCodeResult(TypedDict):
    query: str
    repo: str
    items: list[SearchResultItemPayload]
    returned_count: int
    total_count: int
    incomplete_results: bool
    message: str | None
    """Set only when `returned_count < total_count`, explaining the cap and steering
    the model away from repeating the (rate-limited) search — see `search_code`'s
    own docstring."""


def _repo_payload(repo: RepositorySummary) -> RepoSummaryPayload:
    return {
        "full_name": repo.full_name,
        "name": repo.name,
        "description": repo.description,
        "default_branch": repo.default_branch,
    }


def _tree_entry_payload(entry: TreeEntry) -> TreeEntryPayload:
    return {"name": entry.name, "path": entry.path, "type": entry.type, "size": entry.size}


def _search_item_payload(item: SearchResultItem) -> SearchResultItemPayload:
    return {"repository": item.repository, "path": item.path, "excerpt": item.excerpt}


# --- Tool bodies (undecorated — see module docstring for why) -------------------


async def list_repos(ctx: Context) -> ListReposResult:
    """List the GitHub repositories this server is allowed to access.

    Returns only repositories that are BOTH visible to this server's GitHub App
    installation AND present in the server's configured repository allowlist — the
    intersection of the two, never the full installation list and never an
    allowlisted name the installation cannot actually see. Call this first to find
    out which `repo` values ("owner/repo") you may pass to `get_repo_tree`,
    `read_file`, or `search_code`.
    """
    github, repo_allowlist = _require_lifespan(ctx)
    installation_repos = await github.list_installation_repositories()
    allowed = [
        repo for repo in installation_repos if is_repo_allowlisted(repo.full_name, repo_allowlist)
    ]
    return {"repos": [_repo_payload(repo) for repo in allowed], "count": len(allowed)}


async def get_repo_tree(
    repo: str, ctx: Context, path: str = "", ref: str | None = None
) -> GetRepoTreeResult:
    """List the files and directories at a path inside a repository.

    `repo` must be `"owner/repo"` and must be one of the repositories `list_repos`
    returned. `path` defaults to the repository root (`""`) so you can start
    exploring from the top; pass a subdirectory path to descend further. `ref` is a
    branch, tag, or commit SHA — omit it to use the repository's default branch.
    Each entry reports its `name`, `path`, `type` (`"file"` or `"dir"`), and `size`
    in bytes; use this to navigate before calling `read_file` on a specific file.
    """
    github, _ = _require_lifespan(ctx)
    entries = await github.get_repo_tree(repo, path, ref)
    return {
        "repo": repo,
        "path": path,
        "ref": ref,
        "entries": [_tree_entry_payload(entry) for entry in entries],
        "count": len(entries),
    }


async def read_file(repo: str, path: str, ctx: Context, ref: str | None = None) -> ReadFileResult:
    """Read the text content of one file in a repository.

    `repo` must be `"owner/repo"` and must be one of the repositories `list_repos`
    returned; `path` is the file's path as reported by `get_repo_tree`. `ref` is a
    branch, tag, or commit SHA — omit it to use the repository's default branch.

    Always check `status` before trusting `content`:
    - `"complete"`: `content` is the entire file.
    - `"truncated"`: `content` is only a PREFIX of the file — `message` states how
      many of the file's `total_size` bytes were actually returned. Do not treat
      this as the whole file, and do not draw conclusions about code past the cut
      point.
    - `"binary"`: the file is not valid UTF-8 text; `content` is `None`.
      `total_size` still reports the file's size.
    - `"unavailable"`: the file is too large for this server to read at all (over
      GitHub's 100 MB content-API limit); `content` is `None`.
    """
    github, _ = _require_lifespan(ctx)
    result: FileContent = await github.read_file(repo, path, ref)
    return {
        "repo": repo,
        "path": path,
        "ref": ref,
        "status": result.status,
        "content": result.content,
        "returned_size": result.returned_size,
        "total_size": result.total_size,
        "message": result.message,
    }


async def search_code(query: str, repo: str, ctx: Context) -> SearchCodeResult:
    """Search for code matching a query inside one repository.

    `repo` must be `"owner/repo"` and must be one of the repositories `list_repos`
    returned. Returns matching file paths with a short excerpt of each match.

    IMPORTANT rate limit: GitHub's code search endpoint allows only 10 requests per
    minute, even when authenticated — its own, much tighter bucket than other
    GitHub search endpoints (30/minute). Do not call this tool repeatedly to explore
    a repository; after an initial search, prefer `get_repo_tree` and `read_file` to
    narrow in on specific files instead of re-running searches. Results are capped
    at a server-configured maximum — `returned_count`/`total_count` in the response
    tell you whether more matches exist beyond what was returned.
    """
    github, _ = _require_lifespan(ctx)
    results: SearchResults = await github.search_code(query, repo)
    returned_count = len(results.items)
    message: str | None = None
    if returned_count < results.total_count:
        message = (
            f"Showing {returned_count} of {results.total_count} total matches "
            "(server-side cap). Narrow your query, or switch to get_repo_tree/"
            "read_file instead of repeating this search — code search is rate "
            "limited to 10 requests/minute."
        )
    return {
        "query": query,
        "repo": repo,
        "items": [_search_item_payload(item) for item in results.items],
        "returned_count": returned_count,
        "total_count": results.total_count,
        "incomplete_results": results.incomplete_results,
        "message": message,
    }


def register(mcp: MCPServer, guard: Guard) -> None:
    """Register the core 4 GitHub tools (`DSN-005`) behind `guard`.

    `guard(...)` is applied here, at registration time, not with a bare `@guard(...)`
    atop each `def` above — see the module docstring for why. `name=` is passed
    explicitly to `mcp.add_tool` (rather than relying on `fn.__name__`, which would
    also happen to match) so the SDK-exposed tool name and `guard`'s own `tool`
    argument (used for `CTR-007` RBAC and `CTR-003` audit records) can never drift
    apart from `types.py`'s `TOOL_*` constants, even if a tool function is ever
    renamed.
    """
    mcp.add_tool(guard(TOOL_LIST_REPOS)(list_repos), name=TOOL_LIST_REPOS)
    mcp.add_tool(
        guard(TOOL_GET_REPO_TREE, repo_arg="repo", audit_args=("repo", "path", "ref"))(
            get_repo_tree
        ),
        name=TOOL_GET_REPO_TREE,
    )
    mcp.add_tool(
        guard(TOOL_READ_FILE, repo_arg="repo", audit_args=("repo", "path", "ref"))(read_file),
        name=TOOL_READ_FILE,
    )
    mcp.add_tool(
        guard(TOOL_SEARCH_CODE, repo_arg="repo", audit_args=("repo", "query"))(search_code),
        name=TOOL_SEARCH_CODE,
    )
