"""Tests for devoks_mcp_management.auth.policy (TASK-004).

Traces: AC-003-1, AC-003-2, AC-003-3, AC-003-4, AC-003-5, CTR-007, CTR-008,
EDGE-001, DSN-002.
"""

import pytest

from devoks_mcp_management.auth.policy import (
    AuthorizationDecision,
    authorize,
    filter_allowlisted,
    is_repo_allowlisted,
    list_allowlisted_repos,
)
from devoks_mcp_management.types import (
    CORE_GITHUB_TOOLS,
    TOOL_GET_REPO_TREE,
    TOOL_LIST_REPOS,
    TOOL_READ_FILE,
    TOOL_SEARCH_CODE,
)

READER_TOOLS: frozenset[str] = frozenset(
    {TOOL_LIST_REPOS, TOOL_GET_REPO_TREE, TOOL_READ_FILE, TOOL_SEARCH_CODE}
)
ROLE_TOOLS: dict[str, frozenset[str]] = {
    "reader": READER_TOOLS,
    # A defined role with no tools granted — distinct from an undefined role.
    "guest": frozenset(),
}
ALLOWED_REPO = "ridsync/devoks-mcp-servers"
OTHER_ALLOWED_REPO = "org/other-repo"
ALLOWLIST = frozenset({ALLOWED_REPO, OTHER_ALLOWED_REPO})
DISALLOWED_REPO = "someone-else/private-repo"


# --- role x tool (AC-003-1, AC-003-2, CTR-007) -------------------------------


def test_role_with_permitted_tool_is_allowed() -> None:
    # AC-003-1
    decision = authorize("reader", TOOL_LIST_REPOS, role_tools=ROLE_TOOLS, repo_allowlist=ALLOWLIST)

    assert decision == AuthorizationDecision(allowed=True, client_message=None, reason_code=None)


def test_role_without_tool_is_denied() -> None:
    # AC-003-2: tool body must not run; the caller reads `allowed` to decide
    # that, so this asserts the value that gates execution.
    decision = authorize("guest", TOOL_LIST_REPOS, role_tools=ROLE_TOOLS, repo_allowlist=ALLOWLIST)

    assert decision.allowed is False
    assert decision.reason_code == "tool_not_permitted"


def test_undefined_role_is_denied() -> None:
    decision = authorize(
        "unknown-role", TOOL_LIST_REPOS, role_tools=ROLE_TOOLS, repo_allowlist=ALLOWLIST
    )

    assert decision.allowed is False
    assert decision.reason_code == "role_unknown"


@pytest.mark.parametrize("role", sorted(ROLE_TOOLS))
@pytest.mark.parametrize("tool", sorted(CORE_GITHUB_TOOLS))
def test_role_tool_matrix(role: str, tool: str) -> None:
    # CTR-007: every role x tool combination decides solely by membership in
    # role_tools[role] — no tool is special-cased.
    decision = authorize(role, tool, role_tools=ROLE_TOOLS, repo_allowlist=ALLOWLIST)

    assert decision.allowed is (tool in ROLE_TOOLS[role])


# --- repo allowlist (AC-003-3, AC-003-4, EDGE-001) ---------------------------


def test_repo_in_allowlist_is_allowed() -> None:
    decision = authorize(
        "reader",
        TOOL_READ_FILE,
        ALLOWED_REPO,
        role_tools=ROLE_TOOLS,
        repo_allowlist=ALLOWLIST,
    )

    assert decision.allowed is True


def test_repo_outside_allowlist_is_denied() -> None:
    # AC-003-3
    decision = authorize(
        "reader",
        TOOL_READ_FILE,
        DISALLOWED_REPO,
        role_tools=ROLE_TOOLS,
        repo_allowlist=ALLOWLIST,
    )

    assert decision.allowed is False
    assert decision.reason_code == "repo_not_allowlisted"


@pytest.mark.parametrize("repo", [ALLOWED_REPO, OTHER_ALLOWED_REPO, DISALLOWED_REPO])
def test_empty_allowlist_denies_every_repo(repo: str) -> None:
    # AC-003-4, EDGE-001: an empty allowlist must not be read as "no
    # constraint" — it is fail-safe deny-all, even for a repo that would
    # have been allowed under a non-empty allowlist.
    decision = authorize(
        "reader", TOOL_READ_FILE, repo, role_tools=ROLE_TOOLS, repo_allowlist=frozenset()
    )

    assert decision.allowed is False
    assert decision.reason_code == "repo_not_allowlisted"


def test_tool_without_repo_argument_skips_repo_check() -> None:
    # list_repos has no single target repo — filtering allowlisted results
    # is the caller's job (list_allowlisted_repos/filter_allowlisted), not
    # this check. An empty allowlist must not block the call itself.
    decision = authorize(
        "reader", TOOL_LIST_REPOS, None, role_tools=ROLE_TOOLS, repo_allowlist=frozenset()
    )

    assert decision.allowed is True


# --- client-facing message hides the reason (AC-003-5) -----------------------


def test_denied_client_message_does_not_contain_allowlist_entries() -> None:
    decision = authorize(
        "reader",
        TOOL_READ_FILE,
        DISALLOWED_REPO,
        role_tools=ROLE_TOOLS,
        repo_allowlist=ALLOWLIST,
    )

    assert decision.client_message is not None
    for entry in ALLOWLIST:
        assert entry not in decision.client_message
    assert DISALLOWED_REPO not in decision.client_message


def test_repo_denial_and_unrelated_denial_share_client_message() -> None:
    # AC-003-5: a repo denied for being outside the allowlist must be
    # indistinguishable, from the client's side, from a call denied for a
    # completely different reason (role forbids the tool) even though that
    # second call's repo argument *is* in the allowlist. If these differed,
    # a caller could infer allowlist membership from which message came
    # back.
    repo_denied = authorize(
        "reader",
        TOOL_READ_FILE,
        DISALLOWED_REPO,
        role_tools=ROLE_TOOLS,
        repo_allowlist=ALLOWLIST,
    )
    role_denied_with_allowlisted_repo = authorize(
        "guest",
        TOOL_READ_FILE,
        ALLOWED_REPO,
        role_tools=ROLE_TOOLS,
        repo_allowlist=ALLOWLIST,
    )

    assert repo_denied.allowed is False
    assert role_denied_with_allowlisted_repo.allowed is False
    assert repo_denied.client_message == role_denied_with_allowlisted_repo.client_message


def test_empty_allowlist_denial_shares_client_message_with_non_empty_denial() -> None:
    # Same guarantee across the empty-allowlist path (EDGE-001) and the
    # non-empty-but-not-a-member path (AC-003-3) — a caller cannot tell
    # from the message alone whether the allowlist is empty or simply
    # doesn't include this repo.
    empty_allowlist_denial = authorize(
        "reader",
        TOOL_READ_FILE,
        ALLOWED_REPO,
        role_tools=ROLE_TOOLS,
        repo_allowlist=frozenset(),
    )
    non_member_denial = authorize(
        "reader",
        TOOL_READ_FILE,
        DISALLOWED_REPO,
        role_tools=ROLE_TOOLS,
        repo_allowlist=ALLOWLIST,
    )

    assert empty_allowlist_denial.client_message == non_member_denial.client_message


# --- case sensitivity decision (CTR-008) -------------------------------------


def test_case_variant_repo_is_denied() -> None:
    # Decided behaviour: matching is case-sensitive (see
    # is_repo_allowlisted docstring) — a differently-cased spelling of an
    # allowlisted repo does not match, the stricter of the two options.
    case_variant = ALLOWED_REPO.upper()
    assert case_variant != ALLOWED_REPO  # sanity: fixture actually varies case

    decision = authorize(
        "reader",
        TOOL_READ_FILE,
        case_variant,
        role_tools=ROLE_TOOLS,
        repo_allowlist=ALLOWLIST,
    )

    assert decision.allowed is False


# --- allowlist accessors (support AC-005-1 filtering, not part of denial) ---


def test_is_repo_allowlisted_true_for_member() -> None:
    assert is_repo_allowlisted(ALLOWED_REPO, ALLOWLIST) is True


def test_is_repo_allowlisted_false_for_non_member() -> None:
    assert is_repo_allowlisted(DISALLOWED_REPO, ALLOWLIST) is False


def test_list_allowlisted_repos_enumerates_sorted() -> None:
    assert list_allowlisted_repos(ALLOWLIST) == tuple(sorted(ALLOWLIST))


def test_list_allowlisted_repos_empty_allowlist_is_empty() -> None:
    assert list_allowlisted_repos(frozenset()) == ()


def test_filter_allowlisted_keeps_only_members() -> None:
    candidates = [ALLOWED_REPO, DISALLOWED_REPO, OTHER_ALLOWED_REPO]

    assert filter_allowlisted(candidates, ALLOWLIST) == [ALLOWED_REPO, OTHER_ALLOWED_REPO]


def test_filter_allowlisted_empty_allowlist_keeps_nothing() -> None:
    assert filter_allowlisted([ALLOWED_REPO, OTHER_ALLOWED_REPO], frozenset()) == []


# --- purity / determinism ----------------------------------------------------


def test_authorize_is_deterministic_across_repeated_calls() -> None:
    args: tuple[str, str, str | None] = ("reader", TOOL_READ_FILE, ALLOWED_REPO)

    first = authorize(*args, role_tools=ROLE_TOOLS, repo_allowlist=ALLOWLIST)
    second = authorize(*args, role_tools=ROLE_TOOLS, repo_allowlist=ALLOWLIST)

    assert first == second


def test_authorize_denial_is_deterministic_across_repeated_calls() -> None:
    args: tuple[str, str, str | None] = ("reader", TOOL_READ_FILE, DISALLOWED_REPO)

    first = authorize(*args, role_tools=ROLE_TOOLS, repo_allowlist=ALLOWLIST)
    second = authorize(*args, role_tools=ROLE_TOOLS, repo_allowlist=ALLOWLIST)

    assert first == second
