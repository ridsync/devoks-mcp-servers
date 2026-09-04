"""Tests for devoks_mcp_management.audit.logger (TASK-005).

Traces: AC-004-1, AC-004-3, CTR-003, DSN-003.
"""

import io
import json
from collections.abc import Mapping

from devoks_mcp_management.audit.logger import MAX_ARG_VALUE_CHARS, emit, to_json_line
from devoks_mcp_management.types import (
    AUDIT_EVENT_TOOL_CALL,
    TOOL_READ_FILE,
    AuditOutcome,
    AuditRecord,
)

_CTR_003_FIELDS = frozenset(
    {
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
    }
)


def _make_record(
    *,
    outcome: AuditOutcome = "ok",
    reason_code: str | None = None,
    error_kind: str | None = None,
    args_summary: Mapping[str, str] | None = None,
    request_id: str = "req-1",
) -> AuditRecord:
    return AuditRecord(
        ts="2026-09-03T10:00:00+00:00",
        event=AUDIT_EVENT_TOOL_CALL,
        client_id="client-abc",
        role="reader",
        tool=TOOL_READ_FILE,
        args_summary=args_summary if args_summary is not None else {"repo": "org/repo"},
        outcome=outcome,
        reason_code=reason_code,
        error_kind=error_kind,
        duration_ms=42,
        request_id=request_id,
    )


class _RecordingStream:
    """Fake ``AuditStream`` recording writes/flushes for assertions."""

    def __init__(self) -> None:
        self.chunks: list[str] = []
        self.flush_count = 0

    def write(self, s: str) -> int:
        self.chunks.append(s)
        return len(s)

    def flush(self) -> None:
        self.flush_count += 1


# --- single line + valid JSON (AC-004-1) -------------------------------------


def test_emits_exactly_one_line() -> None:
    # AC-004-1
    stream = io.StringIO()

    emit(_make_record(), stream=stream)

    output = stream.getvalue()
    assert output.endswith("\n")
    assert output.count("\n") == 1


def test_emitted_line_is_valid_json() -> None:
    # AC-004-1
    stream = io.StringIO()

    emit(_make_record(), stream=stream)

    parsed = json.loads(stream.getvalue())
    assert isinstance(parsed, dict)


# --- CTR-003 field coverage ---------------------------------------------------


def test_all_ctr_003_fields_present_with_matching_names() -> None:
    # CTR-003
    line = to_json_line(_make_record())

    parsed = json.loads(line)

    assert set(parsed.keys()) == _CTR_003_FIELDS


# --- three outcome shapes ------------------------------------------------------


def test_ok_outcome_has_null_reason_code_and_error_kind() -> None:
    line = to_json_line(_make_record(outcome="ok"))

    parsed = json.loads(line)

    assert parsed["outcome"] == "ok"
    assert parsed["reason_code"] is None
    assert parsed["error_kind"] is None


def test_denied_outcome_carries_reason_code() -> None:
    # AC-003-5 landing point / CTR-003
    line = to_json_line(_make_record(outcome="denied", reason_code="repo_not_allowlisted"))

    parsed = json.loads(line)

    assert parsed["outcome"] == "denied"
    assert parsed["reason_code"] == "repo_not_allowlisted"
    assert parsed["error_kind"] is None


def test_error_outcome_carries_error_kind() -> None:
    line = to_json_line(_make_record(outcome="error", error_kind="ValueError"))

    parsed = json.loads(line)

    assert parsed["outcome"] == "error"
    assert parsed["error_kind"] == "ValueError"
    assert parsed["reason_code"] is None


# --- single-line guarantee under embedded control characters (AC-004-1) ------


def test_embedded_newline_in_args_summary_stays_one_line() -> None:
    # AC-004-1
    record = _make_record(args_summary={"query": "line one\nline two"})

    line = to_json_line(record)

    assert "\n" not in line
    parsed = json.loads(line)
    assert parsed["args_summary"]["query"] == "line one\nline two"


def test_embedded_control_characters_stay_one_line() -> None:
    # AC-004-1
    record = _make_record(args_summary={"query": "tab\there\rcarriage\x00null"})

    line = to_json_line(record)

    assert "\n" not in line
    assert "\r" not in line
    parsed = json.loads(line)
    assert parsed["args_summary"]["query"] == "tab\there\rcarriage\x00null"


# --- masking (AC-004-3) --------------------------------------------------------


def test_bearer_token_is_not_present_in_output() -> None:
    # AC-004-3
    secret = "Bearer sk-live-abcdef0123456789"
    record = _make_record(args_summary={"note": secret})

    line = to_json_line(record)

    assert "sk-live-abcdef0123456789" not in line
    assert "[REDACTED]" in line


def test_pem_header_is_not_present_in_output() -> None:
    # AC-004-3
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA...\n-----END RSA PRIVATE KEY-----"
    record = _make_record(args_summary={"note": pem})

    line = to_json_line(record)

    assert "MIIEowIBAAKCAQEA" not in line
    assert "[REDACTED]" in line


def test_github_token_prefixes_are_redacted() -> None:
    # AC-004-3
    tokens = (
        "ghp_1234567890abcdef1234567890abcdef1234",
        "gho_1234567890abcdef1234567890abcdef1234",
        "ghs_1234567890abcdef1234567890abcdef1234",
        "github_pat_11ABCDEFG0abcdefghijklmnop",
    )
    for token in tokens:
        line = to_json_line(_make_record(args_summary={"note": token}))
        assert token not in line
        assert "[REDACTED]" in line


def test_long_value_is_truncated_and_original_absent() -> None:
    # AC-004-3
    file_body = "x" * 300_000  # simulated file content mistakenly passed through
    record = _make_record(args_summary={"note": file_body})

    line = to_json_line(record)

    assert file_body not in line
    parsed = json.loads(line)
    assert len(parsed["args_summary"]["note"]) < len(file_body)


def test_value_at_length_cap_is_not_truncated() -> None:
    # AC-004-3 boundary: exactly at the cap survives untouched.
    value = "a" * MAX_ARG_VALUE_CHARS
    record = _make_record(args_summary={"note": value})

    parsed = json.loads(to_json_line(record))

    assert parsed["args_summary"]["note"] == value


def test_value_one_over_length_cap_is_truncated() -> None:
    # AC-004-3 boundary: one character past the cap is truncated.
    value = "a" * (MAX_ARG_VALUE_CHARS + 1)
    record = _make_record(args_summary={"note": value})

    parsed = json.loads(to_json_line(record))

    assert parsed["args_summary"]["note"] != value
    assert parsed["args_summary"]["note"].startswith("a" * MAX_ARG_VALUE_CHARS)


def test_ordinary_identifying_value_is_not_redacted() -> None:
    # AC-004-3 negative case: normal identifying args must survive untouched.
    record = _make_record(args_summary={"repo": "org/repo", "path": "src/main.py"})

    parsed = json.loads(to_json_line(record))

    assert parsed["args_summary"] == {"repo": "org/repo", "path": "src/main.py"}


def test_empty_args_summary_serializes_to_empty_object() -> None:
    record = _make_record(args_summary={})

    parsed = json.loads(to_json_line(record))

    assert parsed["args_summary"] == {}


# --- stream injection + flush --------------------------------------------------


def test_stream_injection_is_captured_by_stringio() -> None:
    stream = io.StringIO()

    emit(_make_record(), stream=stream)

    parsed = json.loads(stream.getvalue())
    assert parsed["request_id"] == "req-1"


def test_emit_flushes_the_stream() -> None:
    stream = _RecordingStream()

    emit(_make_record(), stream=stream)

    assert stream.flush_count == 1


def test_multiple_emits_produce_one_line_per_record_in_order() -> None:
    # JSON Lines format guarantee: N records emitted -> N lines, in order.
    stream = io.StringIO()
    record_count = 20

    for i in range(record_count):
        emit(_make_record(request_id=f"req-{i}"), stream=stream)

    lines = stream.getvalue().splitlines()
    assert len(lines) == record_count
    for i, line in enumerate(lines):
        assert json.loads(line)["request_id"] == f"req-{i}"
