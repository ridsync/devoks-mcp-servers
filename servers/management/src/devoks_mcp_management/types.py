"""Domain types and constants for the Management MCP server.

Pure declarations only: no I/O, no serialization, no validation logic. The
modules that need behaviour import the shapes and bounds from here —
serialization lives in ``audit.logger`` and range validation in ``config`` — so
that the contract values in FRD §5 have exactly one definition in the codebase.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

# --- Tool names (CTR-007 role/tool mapping keys) ----------------------------

TOOL_LIST_REPOS: Final = "list_repos"
TOOL_GET_REPO_TREE: Final = "get_repo_tree"
TOOL_READ_FILE: Final = "read_file"
TOOL_SEARCH_CODE: Final = "search_code"

#: The Stage 1 tool surface. Every name a role may be granted must appear here,
#: which lets ``config`` reject a role/tool mapping that references a tool the
#: server does not actually expose (a silent authorization hole otherwise).
CORE_GITHUB_TOOLS: Final[frozenset[str]] = frozenset(
    {
        TOOL_LIST_REPOS,
        TOOL_GET_REPO_TREE,
        TOOL_READ_FILE,
        TOOL_SEARCH_CODE,
    }
)

# --- Audit record (CTR-003) -------------------------------------------------

#: ``ok`` the tool ran and returned, ``denied`` authorization refused it before
#: the body ran, ``error`` the body ran and failed.
AuditOutcome = Literal["ok", "denied", "error"]

AUDIT_EVENT_TOOL_CALL: Final = "tool_call"


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """One audit line. Field set is fixed by CTR-003.

    Frozen so that a record cannot be mutated between construction and emit,
    and so the redaction guarantee (AC-004-3) holds for whatever the emitter
    receives: ``args_summary`` carries repository, path and query values only —
    never file contents — and no field carries a bearer token or private key.
    """

    ts: str
    """Event time, ISO 8601 with a UTC offset."""

    event: str
    """Record kind. ``AUDIT_EVENT_TOOL_CALL`` for tool calls."""

    client_id: str
    """OAuth client that called, from the verified access token."""

    role: str
    """Role the authorization decision was made against."""

    tool: str
    """Tool name as exposed over MCP."""

    args_summary: Mapping[str, str]
    """Identifying arguments only (repo, path, ref, query). No file contents."""

    outcome: AuditOutcome

    reason_code: str | None
    """Which authorization rule refused, when ``outcome`` is ``denied``, else
    ``None``.

    Operator-facing only. AC-003-5 requires every denial to look identical to
    the caller, so the client-facing message cannot say which rule fired — but
    an operator answering "why can't this client read that repo?" needs exactly
    that. This field is where the two-layer split from ``auth.policy`` lands:
    the client gets one fixed string, the audit line gets the reason code.

    Kept separate from ``error_kind`` rather than overloading one field, so a
    log query can distinguish "authorization refused it" from "the body raised"
    without parsing the value."""

    error_kind: str | None
    """Error class name when ``outcome`` is ``error``, else ``None``. Never a
    message or traceback — those stay in the server log."""

    duration_ms: int

    request_id: str
    """Correlates this record with the server log lines for the same request."""


# --- Contract bounds and defaults -------------------------------------------
# Ranges come from FRD §5.1. `config` validates against MIN/MAX and falls back
# to DEFAULT, so an out-of-range environment value fails at startup rather than
# silently degrading a limit that protects the model's context or our rate
# limit budget.

#: CTR-004 — cap on bytes returned by ``read_file`` before truncation.
READ_FILE_MAX_BYTES_DEFAULT: Final = 262_144
READ_FILE_MAX_BYTES_MIN: Final = 1
READ_FILE_MAX_BYTES_MAX: Final = 1_048_576

#: CTR-005 — cap on ``search_code`` hits, protecting the caller's context.
SEARCH_CODE_MAX_RESULTS_DEFAULT: Final = 30
SEARCH_CODE_MAX_RESULTS_MIN: Final = 1
SEARCH_CODE_MAX_RESULTS_MAX: Final = 100

#: CTR-009 — refresh an installation token once its remaining lifetime drops to
#: this, so a call never starts with a token that expires mid-flight.
TOKEN_REFRESH_LEEWAY_SECONDS_DEFAULT: Final = 300
TOKEN_REFRESH_LEEWAY_SECONDS_MIN: Final = 60
TOKEN_REFRESH_LEEWAY_SECONDS_MAX: Final = 1_800
