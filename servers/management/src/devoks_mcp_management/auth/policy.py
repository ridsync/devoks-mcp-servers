"""Authorization policy (DSN-002).

Pure functions only — no I/O, no network, no clock, no logging, no global
state. Authorization bugs are security incidents, so this module is kept in
the cheapest-to-test shape possible: every role x tool x repo combination can
be checked exhaustively without a GitHub call (FRD §4.3 DSN-002).

``authorize`` returns a value rather than raising, because the caller
(``tools/guard.py``, TASK-007) has to use the result for two different
purposes — the tool error response sent to the client, and the audit record
written for operators — and a raised exception would force that caller to
route the "allowed" path through a try/except too.

Every denial carries two layers, kept apart deliberately (AC-003-5):

  - ``client_message`` — shown to the caller. It is the *same* fixed string
    for every denial reason (unknown role, tool not permitted, repo outside
    the allowlist, or an empty allowlist). A caller must not be able to
    distinguish "this repo isn't in your allowlist" from "your role can't
    use this tool" from "the allowlist is empty" — any distinction here
    would let a caller probe for which repositories are allowlisted, or
    otherwise infer more about server configuration than "this call was
    denied".
  - ``reason_code`` — an operator-facing code, for the audit record only.
    Never sent to the client. This is what lets an operator debug *why* a
    call was denied without weakening the client-facing response.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

#: AC-003-5: the single client-facing denial message. Every deny branch in
#: this module returns exactly this string — never one derived from the
#: requested repo, the allowlist contents, or which check failed.
_CLIENT_DENIAL_MESSAGE = "Not authorized to perform this request."

#: Audit-only classification of why ``authorize`` denied a call. Never
#: surfaced to the client (see module docstring).
ReasonCode = Literal["role_unknown", "tool_not_permitted", "repo_not_allowlisted"]


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    """Result of one ``authorize`` call.

    ``client_message`` and ``reason_code`` are both ``None`` when
    ``allowed`` is ``True``; a caller should only read the denial fields
    once ``allowed`` is confirmed ``False``.
    """

    allowed: bool
    client_message: str | None
    reason_code: ReasonCode | None


def authorize(
    role: str,
    tool: str,
    repo: str | None = None,
    *,
    role_tools: Mapping[str, frozenset[str]],
    repo_allowlist: frozenset[str],
) -> AuthorizationDecision:
    """Decide whether ``role`` may run ``tool``, optionally against ``repo``.

    ``role_tools`` and ``repo_allowlist`` are passed in rather than read
    from ``config`` directly, keeping this function pure — the caller
    (``tools/guard.py``) owns fetching them once from ``Settings``.

    ``repo`` is optional: tools that do not target a single repository
    (``list_repos``, and ``search_code``'s repo-scope-less global search)
    pass ``None`` and skip the repo check entirely — filtering results
    against the allowlist for those tools is the caller's job, using
    ``is_repo_allowlisted``/``list_allowlisted_repos`` below.

    Order: role -> tool permission first (AC-003-1, AC-003-2), then the
    repo check (AC-003-3, AC-003-4) — a call denied on role/tool never
    leaks whether the requested repo would have passed the repo check.
    """
    allowed_tools = role_tools.get(role)
    if allowed_tools is None:
        return _deny("role_unknown")
    if tool not in allowed_tools:
        return _deny("tool_not_permitted")
    if repo is not None and not is_repo_allowlisted(repo, repo_allowlist):
        return _deny("repo_not_allowlisted")
    return AuthorizationDecision(allowed=True, client_message=None, reason_code=None)


def is_repo_allowlisted(repo: str, repo_allowlist: frozenset[str]) -> bool:
    """CTR-008: exact 'owner/repo' match, no wildcards.

    Comparison is case-sensitive by design. CTR-008 calls this "완전일치"
    (exact match); a case-insensitive comparison would accept more strings
    than were explicitly configured, which runs against the fail-safe
    direction used everywhere else in this module (unknown role, tool not
    permitted, and an empty allowlist all deny). A case-sensitive compare
    can only ever deny *more* than a case-insensitive one would — it never
    admits a repo an operator did not literally list — so on the rare
    legitimate case-variant request the caller gets a denial, not a
    bypass. If case-insensitive matching is ever needed, the caller should
    normalize its own input before calling, not this module silently
    normalize on its behalf.

    A repo argument is checked against the allowlist as-is except for
    surrounding whitespace, which is stripped before comparison.

    An empty ``repo_allowlist`` denies every repo by construction — no
    special-casing needed, since membership in the empty set is always
    ``False`` (EDGE-001, AC-003-4).
    """
    return repo.strip() in repo_allowlist


def list_allowlisted_repos(repo_allowlist: frozenset[str]) -> tuple[str, ...]:
    """Enumerate the configured allowlist for a filtering caller.

    ``list_repos`` (AC-005-1) must return only allowlisted repositories;
    this accessor is how that tool gets the allowlist to filter against.
    This is a separate concern from ``AuthorizationDecision.client_message``
    above: an authorized caller reading the allowlist through this
    accessor is not the same as leaking the allowlist inside a denial
    response.
    """
    return tuple(sorted(repo_allowlist))


def filter_allowlisted(repos: Iterable[str], repo_allowlist: frozenset[str]) -> list[str]:
    """Keep only the entries of ``repos`` present in ``repo_allowlist``.

    Convenience wrapper around ``is_repo_allowlisted`` for callers filtering
    a list of candidate repos (e.g. GitHub API results) rather than
    checking one repo at a time.
    """
    return [repo for repo in repos if is_repo_allowlisted(repo, repo_allowlist)]


def _deny(reason_code: ReasonCode) -> AuthorizationDecision:
    return AuthorizationDecision(
        allowed=False, client_message=_CLIENT_DENIAL_MESSAGE, reason_code=reason_code
    )
