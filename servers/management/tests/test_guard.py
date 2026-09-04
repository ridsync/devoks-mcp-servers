"""Tests for devoks_mcp_management.tools.guard (TASK-007).

Traces: AC-003-2, AC-004-2, AC-004-4, EDGE-009, DSN-003.

Every test that needs an authenticated caller sets
``mcp.server.auth.middleware.auth_context.auth_context_var`` directly via the
``_identity`` context manager below — the same thing an in-memory
``Client(mcp)`` test (TASK-024) must do, since ``Client(mcp)`` bypasses HTTP
auth entirely (FRD §7, and see ``tools/guard.py``'s module docstring).
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Generator

import pytest
from mcp import MCPError
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import INVALID_PARAMS

from devoks_mcp_management.config import Settings
from devoks_mcp_management.tools.guard import make_tool_guard
from devoks_mcp_management.types import AuditRecord

READER_ROLE = "reader"
READER_TOOLS = frozenset({"read_file", "list_repos"})
ALLOWED_REPO = "ridsync/devoks-mcp-servers"
DISALLOWED_REPO = "someone-else/private-repo"


def _settings(
    *,
    role_tools: dict[str, frozenset[str]] | None = None,
    repo_allowlist: frozenset[str] = frozenset({ALLOWED_REPO}),
) -> Settings:
    return Settings(
        allowed_hosts=("mcp.example.com",),
        public_url="https://mcp.example.com",
        issuer_url="https://issuer.example.com",
        repo_allowlist=repo_allowlist,
        role_tools=role_tools if role_tools is not None else {READER_ROLE: READER_TOOLS},
        github_app_id="app-id",
        github_app_installation_id="install-id",
        port=8000,
        log_level="INFO",
        read_file_max_bytes=262_144,
        search_code_max_results=30,
        token_refresh_leeway_seconds=300,
        client_tokens={},
        github_app_private_key="unused-in-guard-tests",
    )


def _access_token(role: str | None, *, client_id: str = "client-1") -> AccessToken:
    # Mirrors the shape auth.verifier.StaticTableTokenVerifier actually
    # produces (role under claims["role"]) — see that module's docstring for
    # why guard.py itself only ever reads this back through get_role().
    claims = {"role": role} if role is not None else None
    return AccessToken(token="tok", client_id=client_id, scopes=["devoks:read"], claims=claims)


@contextlib.contextmanager
def _identity(access_token: AccessToken | None) -> Generator[None]:
    """Set (and always reset) the SDK's auth contextvar for the block body."""
    if access_token is None:
        yield
        return
    token = auth_context_var.set(AuthenticatedUser(access_token))
    try:
        yield
    finally:
        auth_context_var.reset(token)


class _Recorder:
    """Fake ``emit`` sink: collects every ``AuditRecord`` handed to it."""

    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    def __call__(self, record: AuditRecord) -> None:
        self.records.append(record)


def _sequential_clock(values: list[float]) -> Callable[[], float]:
    iterator = iter(values)

    def clock() -> float:
        return next(iterator)

    return clock


def _counting_factory(prefix: str) -> tuple[Callable[[], str], list[str]]:
    produced: list[str] = []

    def factory() -> str:
        value = f"{prefix}-{len(produced)}"
        produced.append(value)
        return value

    return factory, produced


def _make_guard(
    settings: Settings,
    *,
    emit: _Recorder,
    clock_values: list[float] | None = None,
    ts: str = "2026-01-01T00:00:00+00:00",
    request_id: str = "req-fixed",
):
    # No explicit return annotation: letting pyright infer it directly from
    # `make_tool_guard`'s own fully-typed return value keeps the `F` TypeVar
    # (and thus each decorated tool's real signature) intact for every
    # caller of this helper — a hand-written `Callable[..., Callable[..., object]]`
    # annotation here would erase it and make every `@guard(...)`-decorated
    # function in this file untyped.
    return make_tool_guard(
        settings,
        emit=emit,
        clock=_sequential_clock(clock_values if clock_values is not None else [0.0, 0.25]),
        timestamp_factory=lambda: ts,
        request_id_factory=lambda: request_id,
    )


# --- AC-003-1: allowed call runs the body ------------------------------------


async def test_allowed_call_runs_body_returns_value_and_audits_ok() -> None:
    # AC-003-1
    recorder = _Recorder()
    guard = _make_guard(_settings(), emit=recorder)
    calls: list[str] = []

    @guard("read_file", repo_arg="repo", audit_args=("repo", "path"))
    async def read_file(repo: str, path: str, ref: str | None = None) -> str:
        calls.append(repo)
        return "file contents"

    with _identity(_access_token(READER_ROLE)):
        result = await read_file(repo=ALLOWED_REPO, path="src/main.py")

    assert result == "file contents"
    assert calls == [ALLOWED_REPO]
    assert len(recorder.records) == 1
    record = recorder.records[0]
    assert record.outcome == "ok"
    assert record.reason_code is None
    assert record.error_kind is None
    assert record.client_id == "client-1"
    assert record.role == READER_ROLE


# --- AC-003-2, AC-004-2: role denial -> body never runs, audited as denied --


async def test_role_without_tool_permission_denies_before_body_runs() -> None:
    # AC-003-2, AC-004-2
    recorder = _Recorder()
    settings = _settings(role_tools={READER_ROLE: frozenset()})  # reader, no tools granted
    guard = _make_guard(settings, emit=recorder)
    body_ran = False

    @guard("read_file", repo_arg="repo")
    async def read_file(repo: str) -> str:
        nonlocal body_ran
        body_ran = True
        return "should never happen"

    with _identity(_access_token(READER_ROLE)), pytest.raises(ToolError) as excinfo:
        await read_file(repo=ALLOWED_REPO)

    assert body_ran is False
    assert len(recorder.records) == 1
    record = recorder.records[0]
    assert record.outcome == "denied"
    assert record.reason_code == "tool_not_permitted"
    assert str(excinfo.value) != ""


async def test_denied_response_never_exposes_reason_code_or_allowlist() -> None:
    # AC-003-5 direction: the client-facing message must be the fixed
    # string, never leak which rule fired or what the allowlist contains.
    recorder = _Recorder()
    settings = _settings(repo_allowlist=frozenset({ALLOWED_REPO}))
    guard = _make_guard(settings, emit=recorder)

    @guard("read_file", repo_arg="repo")
    async def read_file(repo: str) -> str:
        return "unreachable"

    with _identity(_access_token(READER_ROLE)), pytest.raises(ToolError) as excinfo:
        await read_file(repo=DISALLOWED_REPO)

    message = str(excinfo.value)
    assert "repo_not_allowlisted" not in message
    assert ALLOWED_REPO not in message
    assert DISALLOWED_REPO not in message
    # Audit still records the specific reason for operators.
    assert recorder.records[0].reason_code == "repo_not_allowlisted"


async def test_repo_outside_allowlist_denies_before_github_would_be_called() -> None:
    # AC-003-3 direction: the "GitHub call" spy must never increment.
    recorder = _Recorder()
    guard = _make_guard(_settings(), emit=recorder)
    github_calls = 0

    @guard("read_file", repo_arg="repo")
    async def read_file(repo: str) -> str:
        nonlocal github_calls
        github_calls += 1
        return "unreachable"

    with _identity(_access_token(READER_ROLE)), pytest.raises(ToolError):
        await read_file(repo=DISALLOWED_REPO)

    assert github_calls == 0
    assert recorder.records[0].outcome == "denied"
    assert recorder.records[0].reason_code == "repo_not_allowlisted"


# --- Fail-safe: no identity -> deny, body never runs (the "most important") -


async def test_missing_identity_denies_body_never_runs_and_is_audited() -> None:
    # get_access_token() returning None (stdio / in-memory Client transport,
    # FRD §7) must fail-safe deny — never execute the body.
    recorder = _Recorder()
    guard = _make_guard(_settings(), emit=recorder)
    body_ran = False

    @guard("read_file", repo_arg="repo")
    async def read_file(repo: str) -> str:
        nonlocal body_ran
        body_ran = True
        return "unreachable"

    with pytest.raises(ToolError):
        await read_file(repo=ALLOWED_REPO)  # no _identity(...) context set

    assert body_ran is False
    assert len(recorder.records) == 1
    record = recorder.records[0]
    assert record.outcome == "denied"
    assert record.reason_code == "no_identity"
    assert record.client_id == "anonymous"
    assert record.role == "anonymous"


async def test_access_token_without_role_claim_denies_as_role_unknown() -> None:
    # A distinct edge from "no identity at all": the caller *is*
    # authenticated (a real client_id), but the AccessToken carries no role
    # claim. This must still deny, and the audit should show the known
    # client_id (unlike the true no-identity case).
    recorder = _Recorder()
    guard = _make_guard(_settings(), emit=recorder)

    @guard("read_file", repo_arg="repo")
    async def read_file(repo: str) -> str:
        return "unreachable"

    with _identity(_access_token(None, client_id="roleless-client")), pytest.raises(ToolError):
        await read_file(repo=ALLOWED_REPO)

    record = recorder.records[0]
    assert record.outcome == "denied"
    assert record.reason_code == "role_unknown"
    assert record.client_id == "roleless-client"


# --- AC-004-4, EDGE-009: body raises an unanticipated exception -------------


async def test_unexpected_exception_is_audited_and_normalized_for_the_client() -> None:
    # AC-004-4, EDGE-009
    recorder = _Recorder()
    guard = _make_guard(_settings(), emit=recorder)

    @guard("read_file", repo_arg="repo")
    async def read_file(repo: str) -> str:
        raise ValueError("internal detail: /etc/secret-path leaked here")

    with _identity(_access_token(READER_ROLE)), pytest.raises(ToolError) as excinfo:
        await read_file(repo=ALLOWED_REPO)

    client_message = str(excinfo.value)
    assert "internal detail" not in client_message
    assert "/etc/secret-path" not in client_message
    assert "Traceback" not in client_message

    assert len(recorder.records) == 1
    record = recorder.records[0]
    assert record.outcome == "error"
    assert record.error_kind == "ValueError"


async def test_audit_is_emitted_even_when_body_raises_finally_guarantee() -> None:
    # try/finally guarantee: exactly one audit record, regardless of crash.
    recorder = _Recorder()
    guard = _make_guard(_settings(), emit=recorder)

    @guard("read_file", repo_arg="repo")
    async def read_file(repo: str) -> str:
        raise RuntimeError("boom")

    with _identity(_access_token(READER_ROLE)), pytest.raises(ToolError):
        await read_file(repo=ALLOWED_REPO)

    assert len(recorder.records) == 1
    assert recorder.records[0].outcome == "error"
    assert recorder.records[0].error_kind == "RuntimeError"


async def test_deliberate_tool_error_passes_through_unchanged_but_is_audited() -> None:
    # A downstream tool's own deliberate ToolError (e.g. TASK-021/022's
    # GitHub 4xx/5xx normalization) must reach the client verbatim, not be
    # replaced by this module's generic crash message.
    recorder = _Recorder()
    guard = _make_guard(_settings(), emit=recorder)

    @guard("read_file", repo_arg="repo")
    async def read_file(repo: str) -> str:
        raise ToolError("GitHub API returned 404: repository not found")

    with _identity(_access_token(READER_ROLE)), pytest.raises(ToolError) as excinfo:
        await read_file(repo=ALLOWED_REPO)

    assert str(excinfo.value) == "GitHub API returned 404: repository not found"
    assert recorder.records[0].outcome == "error"
    assert recorder.records[0].error_kind == "ToolError"


async def test_deliberate_mcp_error_passes_through_unchanged() -> None:
    recorder = _Recorder()
    guard = _make_guard(_settings(), emit=recorder)

    @guard("read_file", repo_arg="repo")
    async def read_file(repo: str) -> str:
        raise MCPError(code=INVALID_PARAMS, message="bad ref")

    with _identity(_access_token(READER_ROLE)), pytest.raises(MCPError, match="bad ref"):
        await read_file(repo=ALLOWED_REPO)

    assert recorder.records[0].outcome == "error"
    assert recorder.records[0].error_kind == "MCPError"


# --- CTR-003: args_summary never carries file contents or return values -----


async def test_args_summary_excludes_unlisted_and_none_arguments() -> None:
    recorder = _Recorder()
    guard = _make_guard(_settings(), emit=recorder)

    @guard("read_file", repo_arg="repo", audit_args=("repo", "path", "ref"))
    async def read_file(repo: str, path: str, ref: str | None = None, secret: str = "x") -> str:
        return "a" * 10_000  # the "file body" — must never reach args_summary

    with _identity(_access_token(READER_ROLE)):
        result = await read_file(repo=ALLOWED_REPO, path="src/main.py")

    record = recorder.records[0]
    assert record.args_summary == {"repo": ALLOWED_REPO, "path": "src/main.py"}
    assert "ref" not in record.args_summary  # was None -> omitted
    assert "secret" not in record.args_summary  # not in audit_args -> never logged
    assert result not in record.args_summary.values()


async def test_no_repo_arg_tool_authorizes_with_repo_none() -> None:
    # list_repos-shaped tool: no repo_arg configured at all.
    recorder = _Recorder()
    guard = _make_guard(_settings(), emit=recorder)

    @guard("list_repos")
    async def list_repos() -> list[str]:
        return [ALLOWED_REPO]

    with _identity(_access_token(READER_ROLE)):
        result = await list_repos()

    assert result == [ALLOWED_REPO]
    assert recorder.records[0].outcome == "ok"
    assert recorder.records[0].args_summary == {}


# --- duration_ms / request_id / timestamp injection --------------------------


async def test_duration_ms_is_a_nonnegative_integer_from_the_injected_clock() -> None:
    recorder = _Recorder()
    guard = _make_guard(_settings(), emit=recorder, clock_values=[100.0, 100.25])

    @guard("list_repos")
    async def list_repos() -> list[str]:
        return []

    with _identity(_access_token(READER_ROLE)):
        await list_repos()

    duration = recorder.records[0].duration_ms
    assert isinstance(duration, int)
    assert duration == 250
    assert duration >= 0


async def test_injected_request_id_and_timestamp_are_used_verbatim() -> None:
    recorder = _Recorder()
    guard = _make_guard(
        _settings(), emit=recorder, ts="2030-05-04T03:02:01+00:00", request_id="rid-42"
    )

    @guard("list_repos")
    async def list_repos() -> list[str]:
        return []

    with _identity(_access_token(READER_ROLE)):
        await list_repos()

    record = recorder.records[0]
    assert record.request_id == "rid-42"
    assert record.ts == "2030-05-04T03:02:01+00:00"


async def test_all_ctr_003_fields_present_on_every_outcome() -> None:
    recorder = _Recorder()
    guard = _make_guard(_settings(), emit=recorder)

    @guard("list_repos")
    async def list_repos() -> list[str]:
        return []

    with _identity(_access_token(READER_ROLE)):
        await list_repos()

    record = recorder.records[0]
    for field in (
        "ts",
        "event",
        "client_id",
        "role",
        "tool",
        "args_summary",
        "outcome",
        "reason_code",
        "error_kind",
        "duration_ms",
        "request_id",
    ):
        assert hasattr(record, field)
    assert record.event == "tool_call"
    assert record.tool == "list_repos"


# --- async-only enforcement ---------------------------------------------------


def test_guard_rejects_sync_function_at_decoration_time() -> None:
    guard = make_tool_guard(_settings())

    with pytest.raises(TypeError):
        # `guard` is typed to only accept async callables (see guard.py's
        # module docstring); decorating a sync function is a static type
        # error too, on top of the runtime TypeError under test here.
        @guard("list_repos")  # pyright: ignore[reportArgumentType, reportUntypedFunctionDecorator]
        def _sync_tool() -> list[str]:  # pyright: ignore[reportUnusedFunction]
            return []


# --- repeated / rapid-fire calls do not cross-contaminate audit state -------


async def test_repeated_calls_each_produce_their_own_independent_audit_record() -> None:
    recorder = _Recorder()
    request_id_factory, produced_ids = _counting_factory("rid")
    guard = make_tool_guard(
        _settings(),
        emit=recorder,
        clock=lambda: 0.0,
        timestamp_factory=lambda: "2026-01-01T00:00:00+00:00",
        request_id_factory=request_id_factory,
    )

    @guard("read_file", repo_arg="repo")
    async def read_file(repo: str) -> str:
        return repo

    with _identity(_access_token(READER_ROLE)):
        await read_file(repo=ALLOWED_REPO)
        with pytest.raises(ToolError):
            await read_file(repo=DISALLOWED_REPO)
        await read_file(repo=ALLOWED_REPO)

    assert len(recorder.records) == 3
    assert [r.outcome for r in recorder.records] == ["ok", "denied", "ok"]
    assert [r.request_id for r in recorder.records] == produced_ids
    assert len(set(produced_ids)) == 3
