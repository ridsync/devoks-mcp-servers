"""Audit record serialization and emission (DSN-003, CTR-003).

Pure serialize-and-write module — the "independent emit unit" DSN-003 calls
for. It does not construct ``AuditRecord`` values (no clock, no
``request_id`` generation, no duration measurement) and does not depend on
the MCP SDK at all; the caller (``tools/guard.py``, TASK-007) assembles a
complete ``AuditRecord`` for every outcome — ``ok``, ``denied`` (AC-004-2),
and ``error`` (AC-004-4) — and hands it to ``emit`` here. Because
``AuditRecord`` already models all three outcomes (``outcome`` +
``reason_code`` + ``error_kind``), a single ``emit(record)`` call is the
whole interface — there is no separate "emit a denial" or "emit an error"
function to keep in sync with this one.

Why stdout is safe here (and might not be elsewhere)
-------------------------------------------------------
For a stdio-transport MCP server, writing anything to stdout is fatal:
stdout *is* the JSON-RPC transport, and one stray line corrupts the frame.
This server is deployed over Streamable HTTP only (FRD §7) — stdout carries
no protocol traffic there, so an ECS log driver can collect it line by line
as intended (the SDK's own docs note that HTTP-based servers can log to
stdout safely). **If someone runs this server over stdio instead** (e.g. for
local debugging against an stdio-only client), every audit line emitted
here will corrupt that transport. The stream is injectable specifically so
an operator in that situation can redirect audit output elsewhere — this
module does not assume Streamable HTTP, only its caller's deployment does.

Why not ``print()``
--------------------
``print()`` always targets the current ``sys.stdout`` with no way to inject
a substitute, which breaks (a) tests, which need to capture output without
mutating global state, and (b) the stdio-safety escape hatch above. ``emit``
takes an explicit, defaulted-to-``None`` stream parameter instead, resolved
against ``sys.stdout`` at call time — not at function-definition time — so
a caller that reassigns ``sys.stdout`` later still gets the current stream,
which a literal ``stream: AuditStream = sys.stdout`` default would not
(defaults bind once, at import).

Flushing
--------
Every record is flushed immediately after being written. Tool calls are
network-bound (GitHub REST, GitHub App token exchange), not a tight loop, so
one extra flush per call is not a hot-path cost — but a buffered line lost
when the container is SIGKILLed after an unflushed SIGTERM grace period is a
missing audit record, i.e. a compliance gap, not just a UX rough edge.

Masking (AC-004-3) — two layers, do not conflate them
--------------------------------------------------------
1. **Structural** (already holds by construction, nothing to implement
   here): ``AuditRecord`` has no field for a bearer token, a private key, or
   file contents. ``args_summary`` is documented as identifying arguments
   only — repo, path, ref, query (see ``types.AuditRecord``).
2. **Runtime** (implemented here, in ``_redact``): a defensive backstop for
   a caller that puts something it should not have into ``args_summary``
   anyway — a length cap, plus pattern-based redaction of the secret shapes
   the FRD names explicitly (a PEM header, an ``Authorization: Bearer``
   value, and the four documented GitHub token prefixes). This is
   deliberately narrow: it defends known, cheap-to-detect secret shapes, not
   general PII or every conceivable leak — a caller that routes file
   contents through ``args_summary`` is still violating the contract; this
   backstop only limits the blast radius (truncated, not fully suppressed).

Single-line guarantee (AC-004-1)
----------------------------------
``json.dumps`` with its default settings (``ensure_ascii=True``, no
``indent``) escapes every control character in a string value — including a
raw ``\\n`` or ``\\r`` — as a two-character sequence or a ``\\u00XX`` escape,
and never emits a literal newline itself outside of an explicit ``indent``.
That is what makes "one record = one line" hold even when ``args_summary``
(or, in principle, any other field) carries embedded newlines or control
characters: the escaping is a property of the JSON encoder, not of the
masking above, so it protects every field in the record, not only the ones
``_redact`` touches.
"""

import json
import re
import sys
from typing import Final, Protocol

from devoks_mcp_management.types import AuditRecord

#: Runtime redaction cap (AC-004-3). Generous enough for legitimate
#: identifying values (a deep repo path, a long search query) while far
#: smaller than any file content a caller might mistakenly pass through.
#: Public (unlike the other masking internals below) so boundary tests can
#: reference it directly instead of duplicating the number.
MAX_ARG_VALUE_CHARS: Final = 500
_TRUNCATION_SUFFIX: Final = "...<truncated>"

#: Secret shapes named explicitly by the FRD (AC-004-3): a PEM header, a
#: Bearer credential, and the four documented GitHub token prefixes.
#: Case-insensitive on purpose — over-redacting a value that merely looks
#: like a secret is the safe direction; under-redacting an actual one is
#: not (mirrors the fail-safe stance ``auth.policy`` takes elsewhere).
_SECRET_PATTERN: Final = re.compile(
    r"-----BEGIN"
    r"|Bearer\s+\S+"
    r"|\b(?:ghp|gho|ghs)_[A-Za-z0-9]+"
    r"|\bgithub_pat_[A-Za-z0-9_]+",
    re.IGNORECASE,
)

_REDACTED: Final = "[REDACTED]"

__all__ = ["MAX_ARG_VALUE_CHARS", "AuditStream", "emit", "to_json_line"]


class AuditStream(Protocol):
    """The minimal stream capability ``emit`` needs.

    Deliberately narrower than ``typing.TextIO`` — ``emit`` only ever writes
    and flushes, so a structural ``Protocol`` lets ``sys.stdout``, an
    ``io.StringIO`` in tests, or any other write+flush sink satisfy it
    without subclassing anything.
    """

    def write(self, s: str, /) -> object: ...
    def flush(self) -> None: ...


def to_json_line(record: AuditRecord) -> str:
    """Serialize ``record`` to one CTR-003 JSON line (no trailing newline).

    Pure — no I/O — so serialization and masking can be tested directly,
    without a stream to capture. Field names match CTR-003 exactly; do not
    rename these keys, operators query CloudWatch Logs Insights by them.
    """
    payload: dict[str, object] = {
        "ts": record.ts,
        "event": record.event,
        "client_id": record.client_id,
        "role": record.role,
        "tool": record.tool,
        "args_summary": {key: _redact(value) for key, value in record.args_summary.items()},
        "outcome": record.outcome,
        "reason_code": record.reason_code,
        "error_kind": record.error_kind,
        "duration_ms": record.duration_ms,
        "request_id": record.request_id,
    }
    return json.dumps(payload, ensure_ascii=True)


def emit(record: AuditRecord, *, stream: AuditStream | None = None) -> None:
    """Write one audit line for ``record`` and flush it.

    ``stream`` defaults to the caller's current ``sys.stdout`` (see module
    docstring for why the resolution happens here, at call time). The line
    and its trailing newline are written in a single ``write`` call so
    nothing else sharing the stream can interleave a partial line between
    them.
    """
    out: AuditStream = sys.stdout if stream is None else stream
    out.write(to_json_line(record) + "\n")
    out.flush()


def _redact(value: str) -> str:
    """Runtime backstop for a value that should not have reached here (AC-004-3).

    Checked against the *full*, untruncated value first, so a secret
    pattern positioned past ``MAX_ARG_VALUE_CHARS`` is still caught —
    truncating first could let it survive past the cut boundary undetected.
    """
    if _SECRET_PATTERN.search(value):
        return _REDACTED
    if len(value) > MAX_ARG_VALUE_CHARS:
        return value[:MAX_ARG_VALUE_CHARS] + _TRUNCATION_SUFFIX
    return value
