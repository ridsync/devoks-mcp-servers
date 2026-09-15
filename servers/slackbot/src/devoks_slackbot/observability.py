"""Query observability record — one JSON line per query, no raw question (TASK-008).

``REQ-SB-007``: every Slack query — regardless of outcome — leaves exactly one
structured line on stdout so an operator can answer "who asked what, when,
how long did it take, and what did it cost" from CloudWatch Logs Insights
alone, without a database. ``CTR-SB-008`` (FRD §5.1) is the field-set SSOT;
the field names/order below must match it exactly — do not rename or
reorder, operators query by these names.

Why this module builds the record (unlike Stage 1's ``audit/logger.py``)
--------------------------------------------------------------------------
Stage 1's ``DSN-003`` deliberately keeps ``audit/logger.py`` free of any
record-construction logic (no clock, no ``request_id`` generation) — the
caller assembles a complete ``AuditRecord`` and hands it to ``emit``. This
module reuses that same serialize-and-emit shape (``to_json_line``/``emit``
below are a direct port), but it *does* add one more responsibility:
``build_record`` turns a raw question string into ``question_len`` +
``question_sha256`` and *never returns or stores the original string*
anywhere. ``AC-SB-007-3`` requires the raw question to never reach the
record — the safest way to guarantee that for every future caller is to make
the length/hash transformation happen in exactly one place, inside this
module, rather than trusting each call site (``worker.py``, ``handler.py``,
...) to remember to hash before constructing a record by hand.

Why the hash is truncated to 16 hex characters (``QUESTION_HASH_PREFIX_LEN``)
-------------------------------------------------------------------------------
The full record already carries ``question_len``, so the hash's only job is
to let an operator notice "this is the same question as that other row" or
correlate with a support ticket that also has the question text on hand to
re-hash and compare — it is not meant to be collision-proof against a
deliberate adversary. 16 hex characters (64 bits) is effectively unique for
this purpose while keeping log volume down across a high query rate; SHA-256
itself (not a faster/weaker hash) is used because it is already a dependency
of nothing new, is one-way, and reversing even a 16-character prefix back to
the original question is computationally infeasible — the truncation only
costs the operator collision-resistance headroom they never needed, not any
practical amount of pre-image resistance.

``usage`` — plain numbers only, never the SDK object (``AC-SB-007-2``, ``EDGE-SB-017``)
-------------------------------------------------------------------------------------------
**This module never imports ``anthropic``.** Measured cost: 1,384 ms
(workspace PLAN §1) — paid once per cold start for whichever Lambda imports
it, and this module must stay importable from the handler's 3-second ACK
budget too (``CTR-SB-002``), not just the worker's. ``build_record`` therefore
accepts ``usage`` as a plain ``Mapping[str, int | None]`` (or ``None``) — the caller
(``ask.py``/``worker.py``) is responsible for pulling ``input_tokens``/
``output_tokens``/``cache_read_input_tokens`` off the Anthropic SDK's usage
object *before* calling here. Passing the SDK object itself in would either
break JSON serialization outright or leak whatever other fields that object
happens to carry. ``usage`` is ``None`` on the ``denied``/most ``error``
paths (no Claude API call was ever made — nothing to report), and any of the
three keys may be absent even when present (a failed/partial response) —
both are handled without raising.

Single-line guarantee (``AC-SB-007-1``)
------------------------------------------
Same mechanism as Stage 1's ``CTR-003`` records: ``json.dumps`` with no
``indent`` never emits a literal newline, and escapes every control
character (including an embedded ``\\n``/``\\r`` inside, say, a Korean
``reason_code`` string) as a multi-character escape sequence instead. One
CloudWatch/Lambda log line in == one JSON record out, no matter what
``reason_code``/``error_kind`` a caller passes in.

``ensure_ascii=True`` (deliberate, not the library default's accidental
side effect)
-------------------------------------------------------------------------
``reason_code`` can carry non-ASCII text (a Korean-language reason surfaced
from elsewhere in this package). ``ensure_ascii=True`` escapes every
non-ASCII character to a ``\\uXXXX`` sequence, so the emitted line is pure
ASCII regardless of what encoding assumption a downstream log shipper makes
— matching Stage 1's ``CTR-003`` choice for the identical reason, so both
servers' log lines are safe to `grep`/pipe through the same tooling without
a mojibake risk either could introduce alone.

Failure policy: recording must never kill the caller's real work
--------------------------------------------------------------------
``signature.py``/``events.py`` are never-raises by contract; this module
adopts the same stance for a different reason. A Slack query that Claude
already answered (or that was correctly denied) must still reach the user
even if writing *this* observability line fails (a closed stdout, a stream
that raises on ``write``/``flush``). ``emit`` therefore catches every
``Exception`` raised while writing, logs it once via the standard
``logging`` module (operator-visible, never re-raised), and returns.
``emit_query_observation`` extends the same guarantee to ``build_record``
itself, so a caller that just wants "record this query, never let recording
break anything" has exactly one function to call. Callers who need the
individual pieces (tests, or a caller that wants to inspect the record
before emitting it) can still call ``build_record``/``to_json_line``/``emit``
directly — those remain unsuppressed (they will raise on a genuinely
malformed call) so a test can tell "recording degraded" from "recording is
outright buggy."

Why stdout is safe here
---------------------------
Both Lambdas in this package (handler and worker) are invoked directly by
the Lambda runtime, not over an stdio-based protocol — stdout carries no
wire traffic for either, so the AWS Lambda log driver collecting it line by
line (into the CloudWatch log group) is exactly the intended use, same
rationale as Stage 1's ``audit/logger.py``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal, Protocol

logger = logging.getLogger(__name__)

#: CTR-SB-008: the record kind. Fixed — every record this module emits has
#: this exact value, there is no other kind of record in this file.
EVENT_SLACK_QUERY: Final = "slack_query"

#: See the module docstring's "Why the hash is truncated" section.
QUESTION_HASH_PREFIX_LEN: Final = 16

__all__ = [
    "EVENT_SLACK_QUERY",
    "QUESTION_HASH_PREFIX_LEN",
    "ObservationRecord",
    "ObservationStream",
    "Outcome",
    "UsageSummary",
    "build_record",
    "emit",
    "emit_query_observation",
    "to_json_line",
]

#: CTR-SB-008's three outcome values. Matches Stage 1's ``AuditOutcome``
#: shape exactly (``types.py``), kept as an independent alias here rather
#: than imported — FRD §4.4 forbids a shared package between the two
#: servers, so each keeps its own copy of this small contract.
Outcome = Literal["ok", "denied", "error"]


class ObservationStream(Protocol):
    """The minimal stream capability ``emit`` needs — mirrors Stage 1's ``AuditStream``.

    A structural ``Protocol`` (write + flush only) lets ``sys.stdout``, an
    ``io.StringIO`` in tests, or any other write+flush sink satisfy it
    without subclassing anything.
    """

    def write(self, s: str, /) -> object: ...
    def flush(self) -> None: ...


@dataclass(frozen=True, slots=True)
class UsageSummary:
    """The three Claude API usage fields ``CTR-SB-008`` names — nothing else.

    Every field is independently nullable: the Anthropic SDK's usage object
    can arrive with any subset of these populated depending on how the
    response terminated (``EDGE-SB-017``), and this type only ever holds
    plain numbers — never the SDK object itself (see module docstring).
    """

    input_tokens: int | None
    output_tokens: int | None
    cache_read_input_tokens: int | None


@dataclass(frozen=True, slots=True)
class ObservationRecord:
    """One CTR-SB-008 record. Field set/names are fixed by the contract — see module docstring.

    Notably absent: the raw question text. There is no field here that could
    hold it (``AC-SB-007-3``) — only ``question_len``/``question_sha256``,
    which ``build_record`` derives from it without ever storing the original.
    """

    ts: str
    """Event time, ISO 8601 with a UTC offset — caller-supplied (this module
    reads no clock, same reasoning as Stage 1's ``DSN-003``: a pure record
    type is trivial to test without freezing time)."""

    event: str
    slack_user_id: str | None
    """``None`` when the caller could not be identified at all (``identity.py``'s
    ``user_unidentified`` path) — distinct from an identified-but-unregistered
    user, whose ``slack_user_id`` is still known and recorded."""

    client_id: str | None
    """The looked-up MCP client/person identifier (e.g. ``"okwon"``) — never a
    token. ``None`` whenever no credential was resolved (denied/unidentified)."""

    channel: str | None
    thread_ts: str | None
    question_len: int
    question_sha256: str
    """SHA-256 hex digest of the question, truncated to ``QUESTION_HASH_PREFIX_LEN``
    characters. Never the question itself."""

    outcome: Outcome
    reason_code: str | None
    """Operator-only classification of a ``denied``/``error`` outcome (e.g.
    ``identity.py``'s ``reason_code``, ``idempotency.py``'s ``reason``). ``None``
    for ``outcome="ok"``."""

    error_kind: str | None
    """Exception class name for ``outcome="error"``, else ``None``."""

    duration_ms: int
    usage: UsageSummary | None
    """``None`` when no Claude API call was ever made for this query (most
    ``denied``/some ``error`` outcomes) — distinct from a call that returned
    with some usage fields missing, which is an ``UsageSummary`` with one or
    more ``None`` fields."""

    request_id: str


def build_record(
    *,
    ts: str,
    slack_user_id: str | None,
    client_id: str | None,
    channel: str | None,
    thread_ts: str | None,
    question: str,
    outcome: Outcome,
    reason_code: str | None,
    error_kind: str | None,
    duration_ms: int,
    usage: Mapping[str, int | None] | None,
    request_id: str,
) -> ObservationRecord:
    """Build one ``ObservationRecord`` for ``question`` (``AC-SB-007-3``, ``CTR-SB-008``).

    ``question`` is used only to compute ``question_len``/``question_sha256``
    — it is never copied into the returned record and this function never
    logs it. This is the *only* place in the package that should ever derive
    a length/hash pair from a question; every caller building an
    ``ObservationRecord`` should route through here rather than hashing a
    question itself.

    ``usage`` accepts a plain mapping with up to three integer keys
    (``input_tokens``, ``output_tokens``, ``cache_read_input_tokens``) — never
    the Anthropic SDK's usage object (module docstring). Pass ``None`` when no
    Claude API call happened; a present mapping may omit any of the three keys
    safely.
    """
    return ObservationRecord(
        ts=ts,
        event=EVENT_SLACK_QUERY,
        slack_user_id=slack_user_id,
        client_id=client_id,
        channel=channel,
        thread_ts=thread_ts,
        question_len=len(question),
        question_sha256=_hash_question(question),
        outcome=outcome,
        reason_code=reason_code,
        error_kind=error_kind,
        duration_ms=duration_ms,
        usage=_summarize_usage(usage),
        request_id=request_id,
    )


def _hash_question(question: str) -> str:
    return hashlib.sha256(question.encode("utf-8")).hexdigest()[:QUESTION_HASH_PREFIX_LEN]


def _summarize_usage(usage: Mapping[str, int | None] | None) -> UsageSummary | None:
    if usage is None:
        return None
    return UsageSummary(
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        cache_read_input_tokens=usage.get("cache_read_input_tokens"),
    )


def to_json_line(record: ObservationRecord) -> str:
    """Serialize ``record`` to one CTR-SB-008 JSON line (no trailing newline).

    Pure — no I/O — so serialization can be tested directly, without a
    stream to capture. Field names/order match ``CTR-SB-008`` exactly; do not
    rename these keys, operators query CloudWatch Logs Insights by them. See
    the module docstring for why ``ensure_ascii=True`` is deliberate here.
    """
    usage_payload: dict[str, int | None] | None = (
        None
        if record.usage is None
        else {
            "input_tokens": record.usage.input_tokens,
            "output_tokens": record.usage.output_tokens,
            "cache_read_input_tokens": record.usage.cache_read_input_tokens,
        }
    )
    payload: dict[str, object] = {
        "ts": record.ts,
        "event": record.event,
        "slack_user_id": record.slack_user_id,
        "client_id": record.client_id,
        "channel": record.channel,
        "thread_ts": record.thread_ts,
        "question_len": record.question_len,
        "question_sha256": record.question_sha256,
        "outcome": record.outcome,
        "reason_code": record.reason_code,
        "error_kind": record.error_kind,
        "duration_ms": record.duration_ms,
        "usage": usage_payload,
        "request_id": record.request_id,
    }
    return json.dumps(payload, ensure_ascii=True)


def emit(record: ObservationRecord, *, stream: ObservationStream | None = None) -> None:
    """Write one observation line for ``record`` and flush it. Never raises.

    ``stream`` defaults to the caller's current ``sys.stdout``, resolved at
    call time (not at function-definition time), same as Stage 1's
    ``audit/logger.py``. Any failure while writing/flushing (a closed stream,
    a stream that raises) is caught, logged once via the standard ``logging``
    module, and swallowed — see the module docstring's "Failure policy". The
    line and its trailing newline are written in a single ``write`` call so
    nothing else sharing the stream can interleave a partial line between them.
    """
    out: ObservationStream = sys.stdout if stream is None else stream
    try:
        out.write(to_json_line(record) + "\n")
        out.flush()
    except Exception:
        logger.error(
            "failed to write query observation record (request_id=%s)",
            record.request_id,
            exc_info=True,
        )


def emit_query_observation(
    *,
    ts: str,
    slack_user_id: str | None,
    client_id: str | None,
    channel: str | None,
    thread_ts: str | None,
    question: str,
    outcome: Outcome,
    reason_code: str | None,
    error_kind: str | None,
    duration_ms: int,
    usage: Mapping[str, int | None] | None,
    request_id: str,
    stream: ObservationStream | None = None,
) -> None:
    """Build and emit one query's observation record in a single, never-raising call.

    The recommended entry point for ``worker.py``/``handler.py``: builds the
    record (``build_record``) and writes it (``emit``) inside one ``try``, so
    a failure in *either* step — not just the write — can never propagate to
    the caller and interrupt a Slack reply that has already been decided.
    See the module docstring's "Failure policy".
    """
    try:
        record = build_record(
            ts=ts,
            slack_user_id=slack_user_id,
            client_id=client_id,
            channel=channel,
            thread_ts=thread_ts,
            question=question,
            outcome=outcome,
            reason_code=reason_code,
            error_kind=error_kind,
            duration_ms=duration_ms,
            usage=usage,
            request_id=request_id,
        )
    except Exception:
        logger.error(
            "failed to build query observation record (request_id=%s)", request_id, exc_info=True
        )
        return
    emit(record, stream=stream)
