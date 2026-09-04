"""GitHub REST client (TASK-021, `RES-API-002`/`RES-API-003`/`RES-API-004`).

Thin wrapper around three GitHub REST endpoints plus this module's own error
normalization, `CTR-004` truncation, and binary detection. Every public
method returns a frozen dataclass (never a raw `dict`) and never lets an
`httpx2` exception, a raw GitHub error body, or an
`InstallationTokenError`'s call site escape unnormalized.

Error normalization design decision (read before touching this file)
---------------------------------------------------------------------------
`tools/guard.py` (TASK-007) only passes three exception types through to the
client unchanged: `mcp.server.mcpserver.exceptions.ToolError`, `ResourceError`,
and `mcp.MCPError`. Everything else it treats as an unanticipated crash — the
client-facing message is replaced with a generic one and the real message is
discarded (by design: EDGE-009). Since `AC-005-7`/`AC-005-8`/`AC-005-9`
require the client to actually *read* status codes, "what wasn't found", and
retry timing, this module raises `ToolError` directly for every normalized
GitHub failure (4xx/5xx, 404, rate limit, credential failure, network
failure) rather than a private exception class. **This means error
normalization is complete in this module — `TASK-022`'s tool bodies do not
need to catch or translate anything from this client; a `ToolError` raised
here propagates through the tool body and `guard.py` unchanged.** The
alternative (a private `GitHubClientError` translated to `ToolError` in
`tools.py`) was rejected: this module's own FRD §4.2 responsibility is
already "GitHub REST 호출·오류 정규화·절단·바이너리 판정" — normalization
is explicitly this module's job, not the tool layer's, and splitting it
across two files would mean `TASK-022` has to know this module's private
exception shape.

`TokenProvider` is a `Protocol`, not `credentials.InstallationTokenProvider`
directly, so this module only depends on the one method it actually calls
(`get_token`) — `InstallationTokenProvider` satisfies it structurally, and a
test double can too without subclassing anything.

Truncation and UTF-8 safety (`CTR-004`, `AC-005-4`, `EDGE-004`, `EDGE-012`)
---------------------------------------------------------------------------
Binary detection (`AC-005-5`) is judged against the **full, untruncated**
decoded byte string — truncating first and then trying to decode the prefix
would misclassify a valid UTF-8 file as binary whenever the byte cap happens
to land mid-character. Once a file is confirmed valid UTF-8, the truncated
prefix is decoded by `_decode_utf8_prefix`, which backs off at most 3
trailing bytes so a multi-byte character split by the byte-oriented cutoff
is dropped whole rather than emitting mojibake or raising — see that
function's docstring. `_classify_content` is this shared binary/truncation
decision, used by both `read_file`'s normal (`content`/`encoding: "base64"`)
path and its raw-media-type fallback (`EDGE-012`, below) — one
implementation, so the two response shapes can never disagree on what
counts as binary or where a UTF-8-safe cutoff lands.

`encoding: "none"` (GitHub's own behavior for files past its inline-content
size limit) used to mean this module gave up and reported `"unavailable"`.
Per GitHub's official REST API docs (`contents` endpoint,
`2022-11-28`, https://docs.github.com/en/rest/repos/contents): files up to
1 MB support the full contents endpoint; **1–100 MB files still have
retrievable content, but only via the `raw` (or `object`) media type** —
"To get the contents of these larger files, use the raw media type"; files
**over 100 MB are not supported by this endpoint at all**, raw included.
`TASK-025`/`EDGE-012` acts on that: on `encoding: "none"`, this module
checks the file's `size` from the metadata-only response it already has
and, if it is within the 100 MB ceiling, issues one additional request with
`Accept: application/vnd.github.raw+json` to fetch the actual bytes, then
classifies those bytes through the exact same `_classify_content` used by
the normal path — so a 1–100 MB text file now comes back `"truncated"` at
`CTR-004`'s cap (matching `AC-005-4`'s "절단까지만 반환" requirement)
instead of `"unavailable"`. Only the genuinely unsupported case — `size` over
100 MB, where GitHub does not serve content through this endpoint by any
media type — still returns `"unavailable"`. See `FileContent`'s docstring
for the final four-value meaning and `_read_file_via_raw_fallback` for the
100 MB gate (checked from metadata *before* the second request is sent, so
a file this client already knows is unsupported never burns a rate-limit
unit on a request that would just fail).

**Why no fifth `status` value was added for the raw-fallback case:** the
raw-fallback-then-truncated outcome is observationally identical to an
ordinary `CTR-004` truncation from `caller`'s (`TASK-022`) point of view —
same fields populated the same way, same "here's a prefix, here's the real
size" semantics. A caller has no decision to make differently depending on
*why* a file was truncated, only *that* it was, so a distinct status would
be a branch `TASK-022` has to carry without ever using it differently. The
100 MB-ceiling case, by contrast, genuinely cannot be served *at all* — no
content of any size is returned — which is exactly what `"unavailable"`
already meant before this task, so it is reused rather than introduced as
new.

Sources: docs vs. recollection (handover requirement ③)
---------------------------------------------------------------------------
No web-search/fetch tool was available in this session (checked via
`ToolSearch`, matching `TASK-020`'s reported same gap) — the shapes below are
from training-time knowledge of GitHub's REST API, not a fresh doc fetch, so
treat the following as unverified against a live fetch and re-check if a
future session has doc access. The one exception is the 1 MB / 100 MB
thresholds and the `raw` media-type fallback itself (`EDGE-012` above),
which the main loop confirmed against the official GitHub docs before this
task was created (`TASK-021`'s handover note) — those are **not**
recollection, they are doc-verified, and this module hardcodes both bounds
(`_CONTENTS_RAW_FALLBACK_MAX_BYTES`) precisely because they are confirmed
GitHub-documented behavior rather than an inferred value:

- **High confidence** (extremely stable, widely documented API surface):
  `contents` endpoint's array-for-directory / object-for-file split;
  `content`/`encoding: "base64"` fields; RFC 8288 `Link` header pagination
  with `rel="next"`; `x-ratelimit-{limit,remaining,reset,used,resource}`
  headers; `Retry-After` on secondary rate limiting; the
  `X-GitHub-Api-Version` header requirement (already used identically by
  `credentials.py`).
- **Doc-verified this task** (`EDGE-012`): the ≤1 MB / 1–100 MB / >100 MB
  three-tier split and that `application/vnd.github.raw+json` is the media
  type that unlocks the middle tier.
- **Recalled, not re-verified this session**: `application/vnd.github.text-match+json`
  as the `Accept` value that makes `/search/code` include `text_matches`
  excerpt fragments; that GitHub returns 403 *or* 429 for both primary and
  secondary rate limiting (this module treats both status codes identically
  and detects "is this actually a rate limit" from headers rather than from
  the status code alone, which should be correct regardless of which status
  GitHub picks in practice).

Base URL duplication vs. sharing with `credentials.py`
---------------------------------------------------------------------------
`credentials.py` already has a private `_GITHUB_API_BASE_URL` constant.
This module defines its own copy rather than importing it or promoting it to
a shared/public constant: it is one literal string, `credentials.py` is a
completed, independently-tested module (`TASK-020`) that this task's "하지
말 것" list asks to touch only minimally, and inventing a new shared
constants module for a single literal is a bigger footprint than the
duplication it would remove.

Search scope is always a single repository, on purpose
---------------------------------------------------------------------------
`search_code(query, repo)` takes exactly one `repo: str`, matching every
other method here and, more importantly, matching `tools/guard.py`'s
`repo_arg` mechanism: the guard's allowlist check reads exactly one named
string argument per tool call (see that module's docstring). A multi-repo
search API (`repo:a/b OR repo:c/d` — GitHub's search qualifiers do support
boolean `OR`, but this module does not rely on that recollection) would need
either a second allowlist-checking mechanism or per-repo re-checking inside
this client, neither of which exists yet. `TASK-022`'s `search_code` tool is
therefore expected to take one `repo` argument, same as the other three
tools.

Path/ref traversal and search-qualifier injection defenses
(`TASK-040`/`TASK-041`, `EDGE-013`/`EDGE-014`)
---------------------------------------------------------------------------
Both defenses below are deliberately doubled: each has an independent inner
and outer layer so a bypass of one layer alone still cannot reach GitHub
outside the allowlisted scope.

`EDGE-013` (`path`/`ref` traversal, Critical): `tools/guard.py`'s allowlist
check only ever inspects the `repo` argument (`repo_arg="repo"`) --
`path`/`ref` were previously passed straight into `_encode_path`
(`quote(path.strip("/"), safe="/")`), which does not escape `/` or `.`, so a
literal `../` survived into the request URL untouched. `httpx2.URL` (like
any RFC 3986-conformant client) resolves `..`/`.` dot-segments when the
request is actually issued, letting `path="../../../victim/secret/contents/x"`
escape `/repos/{allowed}/{allowed}/contents` entirely and
`path="../../../../installation/repositories"` reach a GitHub endpoint no
tool in this server exposes at all -- both confirmed by direct reproduction
against this module's own mock-transport test harness before this fix (see
this task's handover note). Layer 1, `_reject_path_traversal`, rejects any
`.`/`..` path *segment* or a backslash in `path` or `ref` before either ever
reaches `_encode_path`. Layer 2, `_assert_contents_url_scoped`, re-checks
the URL `_contents_request_target` built by parsing it through `httpx2.URL`
(the same RFC 3986 normalization the real request will undergo) and
asserting the *normalized* path still starts with
`/repos/{owner}/{name}/contents` -- deliberately the same normalization
step the vulnerability exploited, run defensively instead of adversarially,
so a future encoding trick layer 1 has not anticipated is still caught
after normalization rather than before it. Layer 2 raising `AssertionError`
(not `ToolError`) is deliberate: reaching it at all means layer 1 already
has a bug, not that the caller supplied bad input -- `tools/guard.py` still
turns it into a generic client-safe error (EDGE-009) without leaking the
mismatch, while the real message reaches the server log. Both `read_file`
and `get_repo_tree` (plus the `EDGE-012` raw-fallback path) share the one
`_contents_request_target`, so fixing it there covers all three call sites
at once.

`EDGE-014` (`search_code` qualifier injection, High): `search_code` used to
splice the caller's `query` verbatim into GitHub's `q=` parameter alongside
a server-appended `repo:{repo}` scope. GitHub's search syntax supports
multiple `repo:`/`org:`/`user:`/`enterprise:` qualifiers and boolean
`OR`/`NOT` combination, so `query="password OR repo:victim/secret"` widens
the search past the one repo `tools/guard.py` already authorized --
`repo:victim/secret` is simply ORed in alongside the appended
`repo:{allowed}`, never overridden by it. Layer 1,
`_reject_search_qualifier_injection`, rejects a `query` containing any of
those four qualifier names or a top-level `OR`/`NOT` before it is ever
sent. Layer 2 filters the *response*: every `SearchResultItem.repository` is
re-checked against `auth.policy.is_repo_allowlisted` (the same predicate
`list_repos`, one directory up in `tools.py`, already uses) before an item
is returned, so even a qualifier this module has not thought to name in
layer 1 cannot make an out-of-allowlist item reach a caller. Importing
`auth.policy` here (rather than injecting an allowlist-checking callback) is
the same layering `tools.py` already uses -- `auth.policy` is
dependency-free pure functions with nothing importing back from `adapters`,
so this is not a new dependency direction, just the same one already
established for this exact predicate.
"""

from __future__ import annotations

import base64
import binascii
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal, Protocol, cast
from urllib.parse import quote

import httpx2
from mcp.server.mcpserver.exceptions import ToolError

from devoks_mcp_management.adapters.knowledge.github.credentials import InstallationTokenError
from devoks_mcp_management.auth.policy import is_repo_allowlisted
from devoks_mcp_management.config import Settings

__all__ = [
    "FileContent",
    "GitHubClient",
    "RepositorySummary",
    "SearchResultItem",
    "SearchResults",
    "TokenProvider",
    "TreeEntry",
]

#: Wall-clock epoch seconds (`time.time`-shaped) — used only to turn a
#: `Retry-After: <delta-seconds>` header into an absolute retry time; see
#: `credentials.py`'s identical "Clock injection" rationale.
Clock = Callable[[], float]

_GITHUB_API_BASE_URL: Final = "https://api.github.com"
_GITHUB_ACCEPT_HEADER: Final = "application/vnd.github+json"
_GITHUB_SEARCH_ACCEPT_HEADER: Final = "application/vnd.github.text-match+json"
#: `EDGE-012` raw-content fallback media type — GitHub's documented way to
#: retrieve the actual bytes of a 1-100 MB file, which the default
#: `_GITHUB_ACCEPT_HEADER` response omits (`encoding: "none"`).
_GITHUB_RAW_ACCEPT_HEADER: Final = "application/vnd.github.raw+json"
_GITHUB_API_VERSION_HEADER: Final = "2022-11-28"

#: `EDGE-012`: GitHub's documented hard ceiling on the contents endpoint —
#: past this size, no media type (raw included) returns content at all
#: ("this endpoint is not supported" per GitHub's REST API docs). Sending a
#: raw request past this point would just fail and burn a rate-limit unit
#: for nothing, so `_read_file_via_raw_fallback` checks a file's `size`
#: (already known from the metadata-only response) against this constant
#: *before* issuing that second request. Expressed in the same binary-unit
#: convention `CTR-004`'s own byte range uses (`1048576` = 1 MiB for its
#: 1 MB cap), for consistency within this module.
_CONTENTS_RAW_FALLBACK_MAX_BYTES: Final = 100 * 1024 * 1024

#: Mirrors `credentials.py`'s identical bound on echoed GitHub error bodies.
_ERROR_BODY_SNIPPET_LEN: Final = 200

_INSTALLATION_REPOS_PER_PAGE: Final = 100
#: Safety cap on `list_installation_repositories`'s pagination loop — at
#: 100 repos/page this is 5,000 repos, far past any Stage 1 installation.
#: Guards against a malformed/looping `Link` header, not a realistic
#: installation size.
_MAX_INSTALLATION_REPOS_PAGES: Final = 50

#: `EDGE-014` layer 1, qualifier half. Matched case-*insensitively* (`REPO:`
#: must be blocked too) via a `\b...:` word boundary so this only ever
#: matches a *bare* qualifier token — `myrepo:` does not match (no boundary
#: between `y` and `r`), so no legitimate identifier-like search term is
#: rejected as a side effect. `language:`/`path:`/`extension:` and any other
#: qualifier a legitimate caller might need are deliberately left alone —
#: none of them can widen a search past the one repo `search_code`'s own
#: `repo` argument already scopes it to.
_SEARCH_QUALIFIER_PATTERN: Final = re.compile(r"\b(?:repo|org|user|enterprise):", re.IGNORECASE)

#: `EDGE-014` layer 1, boolean half. Deliberately *not* case-insensitive,
#: unlike the qualifier pattern above: GitHub's own docs require search
#: operators to be uppercase to be recognized as operators at all — a
#: lowercase "or"/"not" is just an ordinary search term to GitHub, never a
#: scope-combining operator. Matching case-insensitively here would reject
#: any query containing the common English words "or"/"not" for zero
#: security benefit (GitHub could never treat the lowercase form as an
#: operator), which is exactly the over-blocking this task's own
#: instructions warn against. `\b...\b` keeps this to a *bare* token, so
#: identifiers like `NOT_FOUND` (underscore is a `\w` word character, so
#: there is no boundary between `T` and `_`) or `ORACLE`/`ORDER` (no
#: boundary between `R` and the next letter) are never rejected.
_SEARCH_BOOLEAN_PATTERN: Final = re.compile(r"\b(?:OR|NOT)\b")


class TokenProvider(Protocol):
    """Structural interface this module needs from a token supplier.

    `credentials.InstallationTokenProvider` satisfies this without any
    inheritance; a test double only needs the one method. See the module
    docstring for why this is a `Protocol` rather than the concrete class.
    """

    async def get_token(self) -> str: ...


# --- Return types (frozen dataclasses; FRD §4.2 project convention) --------


@dataclass(frozen=True, slots=True)
class RepositorySummary:
    """One repository the installation can access (`RES-API-002`, `AC-005-1`)."""

    full_name: str
    """``owner/repo`` — the exact format `CTR-008`'s allowlist entries use."""

    name: str
    description: str | None
    default_branch: str


@dataclass(frozen=True, slots=True)
class TreeEntry:
    """One entry of a directory listing (`RES-API-003`, `AC-005-2`)."""

    name: str
    path: str
    type: str
    """``"file"`` or ``"dir"`` per `AC-005-2`; GitHub can also emit
    ``"symlink"``/``"submodule"`` for those rare content types, passed
    through as-is rather than raising, since neither is an error case."""
    size: int


ContentStatus = Literal["complete", "truncated", "binary", "unavailable"]


@dataclass(frozen=True, slots=True)
class FileContent:
    """Result of reading one file's content (`RES-API-003`, `CTR-004`, `AC-005-3/4/5`).

    ``status`` is how a caller (`TASK-022`) tells the four cases apart
    (handover requirement ④):

    - ``"complete"``: the whole file fit under the byte cap and decoded as
      UTF-8. ``content`` is the entire file; ``returned_size ==
      total_size``; ``message`` is ``None`` (nothing to notify).
    - ``"truncated"``: the file exceeded the configured byte cap
      (`CTR-004`, `AC-005-4`, `EDGE-004`). ``content`` holds a *byte-prefix*
      of the file, decoded losslessly as UTF-8 (see module docstring on
      `_decode_utf8_prefix`); ``returned_size`` is the actual UTF-8 byte
      length of ``content`` (may be a few bytes under the configured cap
      when the cutoff landed mid-character); ``total_size`` is the file's
      real size from GitHub. ``message`` states both sizes. This also
      covers a 1-100 MB file retrieved through the `EDGE-012` raw-media-type
      fallback — from this dataclass's shape there is no way to tell the two
      apart, by design (see module docstring, "Why no fifth status value").
    - ``"binary"``: the full file failed to decode as UTF-8 (`AC-005-5`,
      `EDGE-005`). ``content`` is ``None``, ``returned_size`` is 0,
      ``total_size`` is the full file size. ``message`` states the size.
      Reached from either the normal path or the `EDGE-012` raw fallback.
    - ``"unavailable"``: GitHub's contents endpoint cannot serve this
      file's content *at all*, through any media type — its ``size``
      exceeds GitHub's documented 100 MB ceiling on the endpoint
      (`EDGE-012`; `_CONTENTS_RAW_FALLBACK_MAX_BYTES`). ``content`` is
      ``None``, ``returned_size`` is 0, ``total_size`` comes from GitHub's
      ``size`` field (the only size source available in this case, since no
      raw request is even attempted past this ceiling).
    """

    status: ContentStatus
    content: str | None
    returned_size: int
    total_size: int
    message: str | None


@dataclass(frozen=True, slots=True)
class SearchResultItem:
    """One code search hit (`RES-API-004`, `AC-005-6`)."""

    repository: str
    """``owner/repo`` of the hit (should equal the ``repo`` argument passed
    to `search_code`, since search is always scoped to exactly one repo)."""
    path: str
    excerpt: str | None
    """Joined ``text_matches`` fragments GitHub returns for the
    ``text-match`` media type, or ``None`` when GitHub returned none."""


@dataclass(frozen=True, slots=True)
class SearchResults:
    """`CTR-005`-capped code search results (`RES-API-004`, `AC-005-6`)."""

    items: tuple[SearchResultItem, ...]
    """Length is capped at the configured `CTR-005` maximum — enforced by
    this client (both via ``per_page`` and a defensive re-slice), not left
    to the tool layer."""
    total_count: int
    """GitHub's own total match count — informational, uncapped, may exceed
    ``len(items)``."""
    incomplete_results: bool


class GitHubClient:
    """Wraps `RES-API-002`/`003`/`004`. See module docstring for the error
    normalization contract this class implements.
    """

    def __init__(
        self,
        *,
        http_client: httpx2.AsyncClient,
        token_provider: TokenProvider,
        read_file_max_bytes: int,
        search_code_max_results: int,
        repo_allowlist: frozenset[str] = frozenset(),
        clock: Clock = time.time,
    ) -> None:
        self._http_client = http_client
        self._token_provider = token_provider
        self._read_file_max_bytes = read_file_max_bytes
        self._search_code_max_results = search_code_max_results
        #: `EDGE-014` layer 2 (`search_code`'s result-side re-filter). Defaults
        #: to the empty set — deny-all — matching `CTR-008`/`config.py`'s own
        #: "empty allowlist denies every repo" convention, so a caller that
        #: forgets to pass this explicitly fails safe rather than open.
        self._repo_allowlist = repo_allowlist
        self._clock = clock

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        http_client: httpx2.AsyncClient,
        token_provider: TokenProvider,
        clock: Clock = time.time,
    ) -> GitHubClient:
        """Convenience constructor for the composition root (`TASK-023`'s lifespan)."""
        return cls(
            http_client=http_client,
            token_provider=token_provider,
            read_file_max_bytes=settings.read_file_max_bytes,
            search_code_max_results=settings.search_code_max_results,
            repo_allowlist=settings.repo_allowlist,
            clock=clock,
        )

    # --- RES-API-002 --------------------------------------------------------

    async def list_installation_repositories(self) -> tuple[RepositorySummary, ...]:
        """`GET /installation/repositories`, fully paginated (`AC-005-1`).

        Follows the ``Link: rel="next"`` header until exhausted rather than
        returning only the first page: `CTR-008`'s allowlist is small
        relative to a typical installation, but a repo that happens to land
        on page 2 must not become invisible to the allowlist filter a
        caller applies to this method's result.
        """
        summaries: list[RepositorySummary] = []
        url: str | None = f"{_GITHUB_API_BASE_URL}/installation/repositories"
        params: dict[str, Any] | None = {"per_page": _INSTALLATION_REPOS_PER_PAGE}
        pages_fetched = 0

        while url is not None:
            pages_fetched += 1
            if pages_fetched > _MAX_INSTALLATION_REPOS_PAGES:
                raise ToolError(
                    "GitHub installation repository listing exceeded the pagination "
                    f"safety limit ({_MAX_INSTALLATION_REPOS_PAGES} pages)"
                )

            response = await self._request("GET", url, params=params)
            self._raise_for_status(response, not_found_message="GitHub installation was not found")
            payload = self._parse_json_object(response, context="list_repos")

            raw_repos = payload.get("repositories")
            if not isinstance(raw_repos, list):
                raise ToolError(
                    "GitHub installation repositories response is missing 'repositories'"
                )
            for raw_repo in cast(list[Any], raw_repos):
                summaries.append(_parse_repository_summary(_require_object(raw_repo, "list_repos")))

            next_link = response.links.get("next")
            url = next_link["url"] if next_link else None
            params = None  # the next URL already carries its own query string

        return tuple(summaries)

    # --- RES-API-003 --------------------------------------------------------

    async def get_repo_tree(
        self, repo: str, path: str = "", ref: str | None = None
    ) -> tuple[TreeEntry, ...]:
        """`GET /repos/{o}/{r}/contents/{path}?ref=` (`AC-005-2`).

        GitHub returns a JSON array for a directory and a single JSON
        object for a file; the latter is wrapped in a one-element tuple so
        a caller pointing this at a file path still gets a listing back
        instead of a shape surprise.
        """
        payload = await self._get_contents(repo, path, ref)
        raw_entries: list[Any] = (
            cast(list[Any], payload) if isinstance(payload, list) else [payload]
        )
        return tuple(
            _parse_tree_entry(_require_object(entry, "get_repo_tree")) for entry in raw_entries
        )

    async def read_file(self, repo: str, path: str, ref: str | None = None) -> FileContent:
        """`GET /repos/{o}/{r}/contents/{path}?ref=` (`AC-005-3/4/5`, `CTR-004`).

        See `FileContent`'s docstring for the four possible ``status``
        values and the module docstring for the truncation/binary-detection
        ordering. ``encoding: "none"`` (a file past GitHub's inline-content
        limit) does **not** short-circuit to a second HTTP call for every
        file — only files that actually hit that condition pay for the
        `EDGE-012` raw-fallback request; the common ≤1 MB path here still
        issues exactly one request, same as before this task.
        """
        payload = await self._get_contents(repo, path, ref)
        if isinstance(payload, list):
            raise ToolError(
                f"GitHub path is a directory, not a file: repo={repo!r} path={path!r} ref={ref!r}"
            )
        obj = _require_object(payload, "read_file")

        if obj.get("encoding") == "none":
            return await self._read_file_via_raw_fallback(repo, path, ref, obj)

        content_b64 = obj.get("content")
        if not isinstance(content_b64, str):
            raise ToolError(f"GitHub response is missing file content: repo={repo!r} path={path!r}")
        try:
            raw_bytes = base64.b64decode(content_b64, validate=False)
        except (binascii.Error, ValueError) as exc:
            raise ToolError(
                f"GitHub file content was not valid base64: repo={repo!r} path={path!r}"
            ) from exc

        return _classify_content(
            raw_bytes, total_size=len(raw_bytes), max_bytes=self._read_file_max_bytes
        )

    async def _read_file_via_raw_fallback(
        self, repo: str, path: str, ref: str | None, metadata: dict[str, Any]
    ) -> FileContent:
        """`EDGE-012`: fetch actual bytes for a 1-100 MB file via the raw
        media type, or report the file as unsupported past 100 MB — see
        module docstring for the doc-verified thresholds this implements.

        ``metadata`` is the already-parsed metadata-only response that
        triggered this fallback (``encoding: "none"``); its ``size`` field
        is the only place `total_size` comes from in either outcome below,
        since the raw response is a bare byte stream with no `size` field
        of its own (handover requirement ⑤).
        """
        total_size = _require_int(metadata, "size", "read_file")

        if total_size > _CONTENTS_RAW_FALLBACK_MAX_BYTES:
            # No raw request is sent at all: GitHub does not serve this
            # file's content through this endpoint by any media type past
            # this size, so a request here would only fail and cost a
            # rate-limit unit for nothing.
            return FileContent(
                status="unavailable",
                content=None,
                returned_size=0,
                total_size=total_size,
                message=(
                    "GitHub cannot serve this file's content through the contents API "
                    f"(size {total_size} bytes exceeds GitHub's "
                    f"{_CONTENTS_RAW_FALLBACK_MAX_BYTES}-byte limit for this endpoint, "
                    "even via the raw media type)."
                ),
            )

        url, params = self._contents_request_target(repo, path, ref)
        response = await self._request(
            "GET", url, params=params, headers={"Accept": _GITHUB_RAW_ACCEPT_HEADER}
        )
        self._raise_for_status(
            response, not_found_message=self._contents_not_found_message(repo, path, ref)
        )
        return _classify_content(
            response.content, total_size=total_size, max_bytes=self._read_file_max_bytes
        )

    def _contents_request_target(
        self, repo: str, path: str, ref: str | None
    ) -> tuple[str, dict[str, Any] | None]:
        """Shared URL/params builder for both the metadata request
        (`_get_contents`) and the `EDGE-012` raw-fallback request — both
        hit the exact same contents endpoint, differing only in the
        ``Accept`` header sent with the request.

        `EDGE-013`'s two-layer path/ref traversal defense lives here so both
        `read_file` and `get_repo_tree` (and the raw-fallback path, which
        also calls this method) get it for free: `_reject_path_traversal`
        (layer 1) rejects a `.`/`..` segment or a backslash in `path`/`ref`
        before either reaches `_encode_path`, and `_assert_contents_url_scoped`
        (layer 2) re-checks the *normalized* URL after it is built. See the
        module docstring for why two independent layers.
        """
        owner, name = _split_repo(repo)
        _reject_path_traversal(path, field="path")
        if ref is not None:
            _reject_path_traversal(ref, field="ref")
        encoded_path = _encode_path(path)
        suffix = f"/{encoded_path}" if encoded_path else ""
        url = f"{_GITHUB_API_BASE_URL}/repos/{owner}/{name}/contents{suffix}"
        _assert_contents_url_scoped(url, owner=owner, name=name)
        params: dict[str, Any] | None = {"ref": ref} if ref else None
        return url, params

    def _contents_not_found_message(self, repo: str, path: str, ref: str | None) -> str:
        # AC-003-5 / EDGE-006: this 404 is only ever reached for a `repo`
        # `tools/guard.py`'s authorize() has already confirmed is on
        # CTR-008's allowlist — the allowlist gate runs before a tool body
        # (and therefore this client) is ever invoked. So naming the
        # repo/path/ref here does not leak the existence of an
        # out-of-allowlist repository; it only ever describes one the
        # caller was already permitted to know about.
        return f"GitHub repository, ref, or path not found: repo={repo!r} path={path!r} ref={ref!r}"

    async def _get_contents(self, repo: str, path: str, ref: str | None) -> Any:
        url, params = self._contents_request_target(repo, path, ref)
        response = await self._request("GET", url, params=params)
        self._raise_for_status(
            response, not_found_message=self._contents_not_found_message(repo, path, ref)
        )
        return self._parse_json(response, context="get_repo_tree/read_file")

    # --- RES-API-004 --------------------------------------------------------

    async def search_code(self, query: str, repo: str) -> SearchResults:
        """`GET /search/code`, scoped to one repository (`AC-005-6`, `CTR-005`).

        Uses the ``text-match`` media type so GitHub includes excerpt
        fragments (see module docstring on doc-confidence for this Accept
        value). Search has its own, much tighter rate limit than the
        primary API — handled identically here since rate-limit detection
        is header-driven, not endpoint-specific (see `_raise_for_status`).

        `EDGE-014`'s two-layer qualifier-injection defense: `query` is
        rejected up front by `_reject_search_qualifier_injection` (layer 1)
        if it carries a `repo:`/`org:`/`user:`/`enterprise:` qualifier or a
        top-level `OR`/`NOT`, and every returned item's `repository` is
        re-checked against the allowlist below (layer 2) regardless. See
        the module docstring for why both layers exist.
        """
        _reject_search_qualifier_injection(query)
        # `repo:{repo}` leads the query (not appended) so the server-imposed
        # scope reads as the primary qualifier and any caller-supplied text
        # is unambiguously secondary — a cosmetic/documentation-only choice
        # since GitHub's query parser does not care about qualifier order,
        # but it makes the intent legible to anyone reading a logged query.
        full_query = f"repo:{repo} {query}"
        url = f"{_GITHUB_API_BASE_URL}/search/code"
        params: dict[str, Any] = {"q": full_query, "per_page": self._search_code_max_results}

        response = await self._request(
            "GET", url, params=params, headers={"Accept": _GITHUB_SEARCH_ACCEPT_HEADER}
        )
        self._raise_for_status(
            response,
            not_found_message=f"GitHub search scope not found: repo={repo!r}",
        )
        payload = self._parse_json_object(response, context="search_code")

        total_count = _require_int(payload, "total_count", "search_code", default=0)
        incomplete_results = bool(payload.get("incomplete_results", False))
        raw_items = payload.get("items")
        items_list = cast(list[Any], raw_items) if isinstance(raw_items, list) else []

        # CTR-005: enforced here regardless of what `per_page` already
        # asked GitHub for — a defensive re-slice, not a trust boundary on
        # GitHub's own honoring of the request.
        parsed_items = (
            _parse_search_item(_require_object(item, "search_code"))
            for item in items_list[: self._search_code_max_results]
        )
        # EDGE-014 layer 2: drop any item whose repository is not
        # allowlisted (this also drops an item with no parseable
        # `repository` at all — `_parse_search_item` reports that as `""`,
        # which can never be in a non-empty allowlist — since this module
        # cannot vouch for a result it cannot attribute to a known repo).
        items = tuple(
            item
            for item in parsed_items
            if is_repo_allowlisted(item.repository, self._repo_allowlist)
        )
        return SearchResults(
            items=items, total_count=total_count, incomplete_results=incomplete_results
        )

    # --- shared transport / error normalization -----------------------------

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx2.Response:
        """Attach a fresh installation token and issue one request.

        `get_token()` is awaited on every call (never cached across calls
        by this client) — the token provider owns caching/refresh, and
        holding a token here would defeat its expiry handling. See
        `credentials.py`'s module docstring, "Usage from TASK-021".
        """
        try:
            token = await self._token_provider.get_token()
        except InstallationTokenError as exc:
            # InstallationTokenError's own message is already guaranteed
            # secret-free (see its docstring) — safe to embed verbatim.
            raise ToolError(f"GitHub credentials unavailable: {exc}") from exc

        request_headers: dict[str, str] = {
            "Authorization": f"Bearer {token}",
            "Accept": _GITHUB_ACCEPT_HEADER,
            "X-GitHub-Api-Version": _GITHUB_API_VERSION_HEADER,
        }
        if headers:
            request_headers.update(headers)

        try:
            return await self._http_client.request(
                method, url, params=params, headers=request_headers
            )
        except httpx2.HTTPError as exc:
            # Deliberately hand-built rather than str(exc) verbatim — same
            # reasoning as credentials.py's identical construct: this must
            # never end up including request headers regardless of what a
            # future httpx2 version puts in the exception's own string form.
            raise ToolError(
                f"GitHub API request failed before a response was received ({type(exc).__name__})"
            ) from exc

    def _raise_for_status(self, response: httpx2.Response, *, not_found_message: str) -> None:
        """Normalize a non-2xx response into `ToolError` (`AC-005-7/8/9`, `EDGE-003/006`).

        Precedence for 403/429 (rate limit vs. ordinary permission denial):
        1. A `Retry-After` header present -> rate limited (secondary limit).
        2. `x-ratelimit-remaining: 0` + `x-ratelimit-reset` -> rate limited
           (primary or search limit).
        3. Bare `429` with neither header -> still treated as rate limited
           (HTTP semantics: 429 always means "too many requests"), just
           without a known retry time.
        4. Otherwise, `403` is an ordinary permission denial, not a rate
           limit.
        """
        if not response.is_error:
            return
        status = response.status_code

        if status == 404:
            raise ToolError(not_found_message)

        if status in (403, 429):
            is_rate_limited, retry_at = self._rate_limit_retry_at(response)
            if is_rate_limited:
                raise ToolError(self._rate_limit_message(retry_at, response))

        raise ToolError(self._generic_error_message(response))

    def _rate_limit_retry_at(self, response: httpx2.Response) -> tuple[bool, datetime | None]:
        retry_after_header = response.headers.get("retry-after")
        if retry_after_header is not None:
            try:
                delta_seconds = int(retry_after_header)
            except ValueError:
                return True, None
            return True, datetime.fromtimestamp(self._clock() + delta_seconds, tz=UTC)

        remaining = response.headers.get("x-ratelimit-remaining")
        reset = response.headers.get("x-ratelimit-reset")
        if remaining == "0" and reset is not None:
            try:
                return True, datetime.fromtimestamp(int(reset), tz=UTC)
            except ValueError, OSError, OverflowError:
                return True, None

        if response.status_code == 429:
            return True, None

        return False, None

    def _rate_limit_message(self, retry_at: datetime | None, response: httpx2.Response) -> str:
        resource = response.headers.get("x-ratelimit-resource")
        scope = f" ({resource})" if resource else ""
        if retry_at is not None:
            return f"GitHub API rate limit exceeded{scope}; retry after {retry_at.isoformat()}"
        return f"GitHub API rate limit exceeded{scope}; retry time was not provided by GitHub"

    def _generic_error_message(self, response: httpx2.Response) -> str:
        snippet = response.text[:_ERROR_BODY_SNIPPET_LEN]
        return f"GitHub API request failed: HTTP {response.status_code} {snippet!r}"

    def _parse_json(self, response: httpx2.Response, *, context: str) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise ToolError(f"GitHub {context} response was not valid JSON") from exc

    def _parse_json_object(self, response: httpx2.Response, *, context: str) -> dict[str, Any]:
        return _require_object(self._parse_json(response, context=context), context)


# --- module-level parsing helpers -------------------------------------------


def _split_repo(repo: str) -> tuple[str, str]:
    owner, _, name = repo.partition("/")
    if not owner or not name:
        raise ToolError(f"repo must be 'owner/repo', got {repo!r}")
    return owner, name


def _encode_path(path: str) -> str:
    return quote(path.strip("/"), safe="/")


def _reject_path_traversal(value: str, *, field: str) -> None:
    """`EDGE-013` layer 1: reject a `path`/`ref` value carrying a `.`/`..`
    path segment or a backslash, before it ever reaches `_encode_path`/the
    GitHub URL — see module docstring for the vulnerability this closes.

    Checked *per-segment* (``value.split("/")``), not as a substring test,
    so a legitimate filename containing dots — ``foo..bar``, ``...md`` — is
    never rejected; only a segment that is *exactly* ``.`` or ``..`` is a
    traversal token. Backslash is rejected outright, anywhere in the value
    (not just as a whole segment): no legitimate GitHub `path`/`ref`
    segment needs one, and some HTTP/URL stacks treat it as a path
    separator the way ``/`` is, which a segment-only check would miss.

    Raises `ToolError` naming only the offending field and rule — never the
    value itself, the allowlist contents, or whether some other repository
    exists — matching `auth.policy`'s own denial-message contract (a caller
    learns *that* its input was rejected, never anything about server
    configuration).
    """
    if "\\" in value:
        raise ToolError(f"Invalid {field!r} argument: backslashes are not allowed")
    for segment in value.split("/"):
        if segment in (".", ".."):
            raise ToolError(
                f"Invalid {field!r} argument: '.' and '..' path segments are not allowed"
            )


def _assert_contents_url_scoped(url: str, *, owner: str, name: str) -> None:
    """`EDGE-013` layer 2: re-check `url` *after* RFC 3986 normalization
    (`httpx2.URL`), independent of `_reject_path_traversal` above, so a
    future dot-segment bypass this module has not anticipated (a different
    encoding, a normalization quirk) still cannot reach GitHub outside this
    repo's `contents` scope. Checked on the *normalized* `.path` (dot-segments
    resolved, percent-decoded) rather than the raw `url` string — normalizing
    is exactly the step that let the original vulnerability through, so this
    runs that same step defensively instead of adversarially. See module
    docstring for the full rationale and reproduction.

    Raises `AssertionError`, not `ToolError`: layer 1 above should already
    make this unreachable in practice, so tripping it means this module's
    own URL construction has a bug, not that the caller supplied bad input.
    `tools/guard.py` still turns an uncaught exception into a generic
    client-safe error (EDGE-009) without leaking the mismatch, while this
    message reaches the server log for whoever has to debug it.
    """
    expected_prefix = f"/repos/{owner}/{name}/contents"
    normalized_path = httpx2.URL(url).path
    if normalized_path != expected_prefix and not normalized_path.startswith(f"{expected_prefix}/"):
        raise AssertionError(
            "GitHub contents URL escaped its expected scope after normalization: "
            f"expected prefix {expected_prefix!r}, got {normalized_path!r}"
        )


def _reject_search_qualifier_injection(query: str) -> None:
    """`EDGE-014` layer 1: reject a `search_code` `query` that tries to widen
    or redirect its scope via a GitHub search qualifier
    (`repo:`/`org:`/`user:`/`enterprise:`) or a top-level boolean
    (`OR`/`NOT`) — both let a caller combine the server-appended
    `repo:{repo}` scope with an attacker-chosen additional scope via
    GitHub's own documented multi-qualifier/boolean query syntax. See the
    module docstring for the full reproduction and for why the qualifier
    check is case-insensitive while the boolean check is not.

    Raises `ToolError` naming only which rule was violated — never the
    allowlist contents or any other repository's existence.
    """
    qualifier_match = _SEARCH_QUALIFIER_PATTERN.search(query)
    if qualifier_match is not None:
        raise ToolError(
            f"Invalid search query: {qualifier_match.group().rstrip(':')!r}-style "
            "qualifiers are not allowed; search is always scoped to the requested "
            "repository"
        )
    if _SEARCH_BOOLEAN_PATTERN.search(query) is not None:
        raise ToolError("Invalid search query: top-level OR/NOT boolean operators are not allowed")


def _is_valid_utf8(data: bytes) -> bool:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _classify_content(raw_bytes: bytes, *, total_size: int, max_bytes: int) -> FileContent:
    """Shared binary-detection + `CTR-004` truncation decision for
    `read_file`'s two response shapes (base64 ``content`` field, and the
    `EDGE-012` raw-media-type fallback) — see module docstring's
    "Truncation and UTF-8 safety" section for why this must be one
    implementation rather than two.

    ``total_size`` is the caller-supplied ground truth for the file's real
    size (from GitHub's ``size`` field in the base64 path's own response,
    or from the metadata-only response in the raw-fallback path — the raw
    response itself has no ``size`` field). ``raw_bytes`` is what actually
    got decoded/truncated; the two are the same value in the base64 path
    but distinct sources in the raw-fallback path (handover requirement ⑤).
    """
    # Binary detection is judged on the *full* decoded content, never a
    # truncated prefix — see module docstring.
    if not _is_valid_utf8(raw_bytes):
        return FileContent(
            status="binary",
            content=None,
            returned_size=0,
            total_size=total_size,
            message=f"File is binary (not valid UTF-8); size is {total_size} bytes.",
        )

    if len(raw_bytes) <= max_bytes:
        return FileContent(
            status="complete",
            content=raw_bytes.decode("utf-8"),
            returned_size=len(raw_bytes),
            total_size=total_size,
            message=None,
        )

    truncated_bytes = raw_bytes[:max_bytes]
    text = _decode_utf8_prefix(truncated_bytes)
    returned_size = len(text.encode("utf-8"))
    return FileContent(
        status="truncated",
        content=text,
        returned_size=returned_size,
        total_size=total_size,
        message=(
            f"Content truncated to {returned_size} of {total_size} bytes (limit {max_bytes} bytes)."
        ),
    )


def _decode_utf8_prefix(data: bytes) -> str:
    """Decode a byte-prefix of a document already confirmed to be valid UTF-8.

    `CTR-004` truncates by byte count, but the cutoff can land inside a
    multi-byte UTF-8 character. Backing off up to 3 trailing bytes (the
    longest a UTF-8 character can be) always finds a valid boundary because
    the *full* document already decoded successfully — the only way a
    prefix can fail to decode is a character split at the very end. This is
    preferred over ``errors="ignore"`` because it drops at most one whole
    trailing character rather than silently discarding any other malformed
    byte sequence it happens to find.
    """
    for backoff in range(4):
        candidate = data[: len(data) - backoff] if backoff else data
        try:
            return candidate.decode("utf-8")
        except UnicodeDecodeError:
            continue
    raise AssertionError(
        "unreachable: _decode_utf8_prefix requires the full document to already be valid UTF-8"
    )


def _require_object(value: object, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ToolError(f"GitHub {context} response contained a non-object entry")
    return cast(dict[str, Any], value)


def _require_str(obj: Mapping[str, Any], key: str, context: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str):
        raise ToolError(f"GitHub {context} response is missing a valid {key!r} field")
    return value


def _require_int(obj: Mapping[str, Any], key: str, context: str, default: int | None = None) -> int:
    value = obj.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        if default is not None:
            return default
        raise ToolError(f"GitHub {context} response is missing a valid {key!r} field")
    return value


def _parse_repository_summary(raw: Mapping[str, Any]) -> RepositorySummary:
    description = raw.get("description")
    return RepositorySummary(
        full_name=_require_str(raw, "full_name", "list_repos"),
        name=_require_str(raw, "name", "list_repos"),
        description=description if isinstance(description, str) else None,
        default_branch=_require_str(raw, "default_branch", "list_repos"),
    )


def _parse_tree_entry(raw: Mapping[str, Any]) -> TreeEntry:
    return TreeEntry(
        name=_require_str(raw, "name", "get_repo_tree"),
        path=_require_str(raw, "path", "get_repo_tree"),
        type=_require_str(raw, "type", "get_repo_tree"),
        size=_require_int(raw, "size", "get_repo_tree", default=0),
    )


def _parse_search_item(raw: Mapping[str, Any]) -> SearchResultItem:
    path = _require_str(raw, "path", "search_code")
    repo_obj = raw.get("repository")
    full_name = None
    if isinstance(repo_obj, dict):
        candidate = cast(dict[str, Any], repo_obj).get("full_name")
        full_name = candidate if isinstance(candidate, str) else None
    return SearchResultItem(
        repository=full_name or "", path=path, excerpt=_extract_excerpt(raw.get("text_matches"))
    )


def _extract_excerpt(text_matches: object) -> str | None:
    if not isinstance(text_matches, list):
        return None
    fragments: list[str] = []
    for match in cast(list[Any], text_matches):
        if isinstance(match, dict):
            fragment = cast(dict[str, Any], match).get("fragment")
            if isinstance(fragment, str):
                fragments.append(fragment)
    return "\n---\n".join(fragments) if fragments else None
