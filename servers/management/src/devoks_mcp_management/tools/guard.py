"""Tool guard decorator (TASK-007, DSN-003, FRD §4.1 step ③).

This is the "③ 툴 래퍼 (``@guarded``)" box in the FRD's data-flow diagram: it
sits between the SDK's own auth layer (which already resolved ``AccessToken``
or 401'd the request before a tool wrapper ever runs) and each tool's body.
It has three jobs, all enforced in one place so a tool author cannot forget
one of them by writing the body first:

1. Run ``auth.policy.authorize`` *before* the body — a denial never reaches
   the tool, so a denied call never touches GitHub (AC-003-2, AC-003-3).
2. Emit exactly one ``CTR-003`` audit record per call, for every outcome —
   ``ok``, ``denied`` (AC-004-2), or ``error`` (AC-004-4) — via
   ``try/except/else/finally`` so an exception from the body can never skip
   the audit emit.
3. Normalize whatever the body raises into something safe to hand back to
   the client: a deliberate ``mcp.server.mcpserver.exceptions.ToolError`` (or
   ``ResourceError``/``mcp.MCPError``) passes through unchanged — the SDK's
   own contract already treats those as client-safe, and a downstream tool
   (TASK-021/022's GitHub error normalization) depends on its own
   ``ToolError`` messages reaching the model intact. Anything else is an
   unanticipated crash (EDGE-009): the traceback goes to the server log via
   ``logging`` (never to the client, never to the audit record — the audit
   only ever gets ``error_kind``, the exception's class name), and the
   client gets a generic, request-id-bearing message with no trace of the
   original exception text.

Why a factory (``make_tool_guard``) rather than a bare decorator
-------------------------------------------------------------------
The decorator needs a ``Settings`` (for ``role_tools``/``repo_allowlist``)
and an emit sink, and every audit field that is normally "ambient" —
``ts``, ``duration_ms``, ``request_id`` — has to be swappable for a test to
assert on it deterministically. Reading ``Settings`` from a module-global or
calling ``time.time()``/``uuid.uuid4()`` directly would make both
impossible without patching globals. ``make_tool_guard(settings, *, emit=,
clock=, timestamp_factory=, request_id_factory=)`` takes all of that by
injection and returns ``guard``, the actual per-tool decorator factory:

    guard = make_tool_guard(settings)

    @guard("read_file", repo_arg="repo", audit_args=("repo", "path", "ref"))
    async def read_file(repo: str, path: str, ref: str | None = None) -> str:
        ...

``clock`` is ``time.perf_counter`` by default (monotonic) rather than
``time.time`` — an NTP step during a call must never produce a negative
``duration_ms``.

Why ``repo_arg``/``audit_args`` are named, not inferred
------------------------------------------------------------
Every tool's argument shape differs (``list_repos`` has no repository
argument at all; ``read_file`` does). Guessing which parameter is "the repo"
from its name is exactly the kind of heuristic that goes quietly wrong the
day a parameter is renamed — and a wrong guess here does not fail loudly, it
authorizes a call it should have checked against the allowlist. So the tool
author names the parameter explicitly per call to ``guard(...)``; a tool
that does not pass ``repo_arg`` is authorized with ``repo=None`` (skips the
allowlist check, matching ``auth.policy.authorize``'s own contract for
repo-less tools). The same reasoning applies to ``audit_args``: this module
never dumps "every keyword argument" into ``args_summary``, because that
would make a future parameter (say, a tool grows a ``content`` argument)
leak into the audit log by default. Only the names the caller opts in
through ``audit_args`` are ever stringified into the record — the
identifying arguments CTR-003 asks for (repo, path, ref, query), never a
file body or a tool's return value, which this module never even looks at
for anything other than the ``else`` branch's ``return result``.

Why identity ``None`` is a fail-safe *deny*, not a pass-through
----------------------------------------------------------------
``mcp.server.auth.middleware.auth_context.get_access_token()`` returns
``None`` on any request that never went through the SDK's HTTP bearer-auth
middleware — stdio transport, or the in-memory ``Client(mcp)`` test
transport (FRD §7). Over Streamable HTTP, the SDK's own auth middleware
already turns a missing/invalid token into a 401 before a tool wrapper ever
runs, so ``None`` reaching *this* code means the call arrived by a path with
no authentication layer in front of it at all. Treating that as "role
unknown, deny" is what keeps RBAC meaningful the moment this server is ever
run over stdio instead: the alternative (treat ``None`` as some default
role) would make every stdio deployment fully open, silently.

The audit record for that case cannot carry a real ``client_id``/``role`` —
there is no ``AccessToken`` to read one from — so both fields are recorded
as the literal string ``"anonymous"`` (picked over ``""`` so a log query for
this exact condition cannot be confused with an — otherwise impossible,
since ``config.py`` rejects empty role names and token rows always have a
``client_id`` — empty value from a real token). The audit ``reason_code``
is the more specific ``"no_identity"`` rather than ``auth.policy``'s
``"role_unknown"``, even though this path is implemented by calling
``authorize()`` with an empty-string role (guaranteed to never match a
configured role, so it always resolves through ``authorize()``'s own
``role_unknown`` branch) purely to obtain the single canonical
client-facing denial string without duplicating that literal here. The
*audit* reason is overridden to ``"no_identity"`` because it is a
meaningfully different operational signal from a genuine RBAC
misconfiguration: an operator seeing ``"no_identity"`` in the logs should
ask "why did a request reach a tool without going through HTTP auth?", not
"why isn't this role assigned this tool?".

**Consequence for TASK-024 (GitHub tools' in-memory integration tests):**
because ``Client(mcp)`` bypasses HTTP auth, every tool call in an in-memory
test is denied by this fail-safe unless the test first does

    from mcp.server.auth.middleware.auth_context import auth_context_var
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

    token = auth_context_var.set(AuthenticatedUser(access_token))
    try:
        ...  # call the tool
    finally:
        auth_context_var.reset(token)

before calling a guarded tool. Skipping this makes every call fail with the
fixed denial message, which looks like a policy bug rather than a missing
test fixture.

Async-only, on purpose
-----------------------
SDK v2 tools are async by default (a sync ``def`` tool is run on a worker
thread by the SDK itself), and every tool this Stage introduces (the GitHub
adapter, TASK-020-022) is async. Wrapping sync tools too would mean this
module re-implementing the SDK's own thread-offload machinery for a case
nothing in this codebase needs — so ``guard`` only wraps
``Callable[..., Awaitable[Any]]`` and raises ``TypeError`` at decoration
time (not at call time) if handed a non-coroutine function, so a mistake is
caught immediately rather than surfacing as a confusing runtime failure
inside ``await fn(...)``.
"""

from __future__ import annotations

import inspect
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from functools import wraps
from typing import Any, TypeVar, cast

from mcp import MCPError
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver.exceptions import ResourceError, ToolError

from devoks_mcp_management.audit.logger import emit as _default_emit
from devoks_mcp_management.auth.policy import authorize
from devoks_mcp_management.auth.verifier import get_role
from devoks_mcp_management.config import Settings
from devoks_mcp_management.types import AUDIT_EVENT_TOOL_CALL, AuditOutcome, AuditRecord

__all__ = ["make_tool_guard"]

logger = logging.getLogger(__name__)

#: Bound to any async tool body. The wrapper is functionally
#: call-compatible with ``fn`` (same signature, same return value on
#: success), which is what the ``cast(F, wrapper)`` at the bottom of
#: ``decorator`` documents — mirroring the ``functools.wraps``-preserves-the-
#: signature idiom used because a plain ``ParamSpec`` here would need a
#: second level of genericity (a decorator *factory*, not a decorator) that
#: only a dedicated ``Generic`` wrapper class (see typeshed's
#: ``functools._Wrapped``) expresses precisely — overkill for a wrapper
#: whose callers are the MCP SDK's own dynamic, kwargs-based dispatch, not
#: statically type-checked call sites in this codebase.
F = TypeVar("F", bound=Callable[..., Awaitable[Any]])

AuditEmitter = Callable[[AuditRecord], None]
Clock = Callable[[], float]
TimestampFactory = Callable[[], str]
RequestIdFactory = Callable[[], str]

#: See "Why identity None is a fail-safe deny" above.
_NO_IDENTITY_SENTINEL = "anonymous"
_NO_IDENTITY_REASON_CODE = "no_identity"

#: Never a real role (``config.py`` rejects empty ``MCP_ROLE_TOOLS`` keys),
#: so passing this to ``authorize()`` always resolves through its
#: ``role_unknown`` deny branch — used both when there is no identity at all
#: and when an ``AccessToken`` carries no role claim.
_UNKNOWN_ROLE_SENTINEL = ""


def _default_timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _default_request_id() -> str:
    return uuid.uuid4().hex


def make_tool_guard(
    settings: Settings,
    *,
    emit: AuditEmitter = _default_emit,
    clock: Clock = time.perf_counter,
    timestamp_factory: TimestampFactory = _default_timestamp,
    request_id_factory: RequestIdFactory = _default_request_id,
) -> Callable[..., Callable[[F], F]]:
    """Build the ``guard`` decorator factory for one server instance.

    ``settings`` supplies ``role_tools``/``repo_allowlist`` to
    ``auth.policy.authorize``. ``emit``/``clock``/``timestamp_factory``/
    ``request_id_factory`` default to the real implementations
    (``audit.logger.emit``, ``time.perf_counter``, wall-clock ISO 8601,
    ``uuid4``) and exist to be overridden by tests — see the module
    docstring for why ambient time/identity sources make deterministic
    testing impossible otherwise.
    """

    def guard(
        tool: str,
        *,
        repo_arg: str | None = None,
        audit_args: tuple[str, ...] = (),
    ) -> Callable[[F], F]:
        """Decorator for one tool. See the module docstring for the full contract.

        ``tool`` is the CTR-007 tool name checked against
        ``role_tools``/recorded in the audit line — independent of the
        wrapped function's own ``__name__``, so a tool can be registered
        under a name distinct from its Python identifier if needed.
        ``repo_arg`` names the parameter (if any) holding the repository
        this call targets; omit it for a tool with no single-repo argument.
        ``audit_args`` names the parameters to stringify into
        ``args_summary`` (``repo_arg``, if given, is always included even if
        omitted from ``audit_args``).
        """
        log_arg_names = tuple(dict.fromkeys((*audit_args, *((repo_arg,) if repo_arg else ()))))

        def decorator(fn: F) -> F:
            if not inspect.iscoroutinefunction(fn):
                raise TypeError(
                    f"make_tool_guard only wraps async tool functions; {fn!r} is not "
                    "one (sync tools are out of scope for this guard — see module "
                    "docstring)"
                )
            signature = inspect.signature(fn)

            @wraps(fn)
            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                start = clock()
                ts = timestamp_factory()
                request_id = request_id_factory()

                bound = signature.bind_partial(*args, **kwargs)
                bound.apply_defaults()
                repo = _extract_str_arg(bound.arguments, repo_arg)
                args_summary = _build_args_summary(bound.arguments, log_arg_names)

                def record(
                    *,
                    outcome: AuditOutcome,
                    client_id: str,
                    role: str,
                    reason_code: str | None = None,
                    error_kind: str | None = None,
                ) -> None:
                    elapsed_ms = max(0, round((clock() - start) * 1000))
                    emit(
                        AuditRecord(
                            ts=ts,
                            event=AUDIT_EVENT_TOOL_CALL,
                            client_id=client_id,
                            role=role,
                            tool=tool,
                            args_summary=args_summary,
                            outcome=outcome,
                            reason_code=reason_code,
                            error_kind=error_kind,
                            duration_ms=elapsed_ms,
                            request_id=request_id,
                        )
                    )

                access_token = get_access_token()
                identity_present = access_token is not None
                client_id = (
                    access_token.client_id if access_token is not None else _NO_IDENTITY_SENTINEL
                )
                role = get_role(access_token) if access_token is not None else None
                effective_role = role if role is not None else _UNKNOWN_ROLE_SENTINEL

                decision = authorize(
                    effective_role,
                    tool,
                    repo,
                    role_tools=settings.role_tools,
                    repo_allowlist=settings.repo_allowlist,
                )
                if not decision.allowed:
                    client_message = decision.client_message
                    reason_code = decision.reason_code
                    assert client_message is not None  # policy guarantees this when denied
                    assert reason_code is not None
                    # AC-003-2 / AC-004-2: audited, body never runs. `client_id`
                    # already collapsed to the sentinel above when there is no
                    # identity; `role`/`reason_code` need the same override
                    # here since `effective_role`/`decision.reason_code` carry
                    # the "" / "role_unknown" values authorize() produced for
                    # the sentinel role, not the no-identity-specific ones.
                    record(
                        outcome="denied",
                        client_id=client_id,
                        role=effective_role if identity_present else _NO_IDENTITY_SENTINEL,
                        reason_code=reason_code if identity_present else _NO_IDENTITY_REASON_CODE,
                    )
                    raise ToolError(client_message)

                outcome: AuditOutcome = "ok"
                error_kind: str | None = None
                try:
                    result = await fn(*args, **kwargs)
                except (ToolError, ResourceError, MCPError) as exc:
                    # Deliberate, already client-safe (SDK contract) —
                    # audited, then passed through unchanged so a
                    # downstream tool's own message still reaches the
                    # model verbatim.
                    outcome = "error"
                    error_kind = type(exc).__name__
                    logger.info("Tool %r failed with a deliberate %s: %s", tool, error_kind, exc)
                    raise
                except Exception as exc:
                    # EDGE-009 / AC-004-4: unanticipated crash. Traceback to
                    # the server log only; the client gets a generic,
                    # request-id-bearing message with no trace of the
                    # original exception's text (it may name an internal
                    # path, a query string, or other server-internal
                    # detail this module has no way to vet).
                    outcome = "error"
                    error_kind = type(exc).__name__
                    logger.exception("Tool %r crashed", tool)
                    raise ToolError(
                        f"Internal error while executing tool {tool!r}. request_id={request_id}"
                    ) from exc
                else:
                    return result
                finally:
                    record(
                        outcome=outcome,
                        client_id=client_id,
                        role=effective_role,
                        error_kind=error_kind,
                    )

            return cast(F, wrapper)

        return decorator

    return guard


def _extract_str_arg(arguments: Mapping[str, Any], name: str | None) -> str | None:
    """Read the tool's designated repo argument (see module docstring: named, never inferred)."""
    if name is None:
        return None
    value = arguments.get(name)
    return value if isinstance(value, str) else None


def _build_args_summary(arguments: Mapping[str, Any], names: tuple[str, ...]) -> dict[str, str]:
    """CTR-003 ``args_summary``: only the explicitly named arguments, stringified.

    A parameter left at its default (e.g. an omitted ``ref``) is bound to
    ``None`` by ``bind_partial().apply_defaults()``; such values are left
    out of the summary entirely rather than serialized as the literal
    string ``"None"``.
    """
    summary: dict[str, str] = {}
    for name in names:
        if name not in arguments:
            continue
        value = arguments[name]
        if value is None:
            continue
        summary[name] = str(value)
    return summary
