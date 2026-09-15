"""Tests for devoks_slackbot.observability (TASK-008).

Traces: REQ-SB-007, AC-SB-007-1, AC-SB-007-2, AC-SB-007-3, CTR-SB-008,
EDGE-SB-017, CTR-003.
"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Mapping

import pytest

from devoks_slackbot.observability import (
    QUESTION_HASH_PREFIX_LEN,
    ObservationRecord,
    UsageSummary,
    build_record,
    emit,
    emit_query_observation,
    to_json_line,
)

_CTR_SB_008_FIELDS = frozenset(
    {
        "ts",
        "event",
        "slack_user_id",
        "client_id",
        "channel",
        "thread_ts",
        "question_len",
        "question_sha256",
        "outcome",
        "reason_code",
        "error_kind",
        "duration_ms",
        "usage",
        "request_id",
    }
)

# A unique, unambiguous sentinel — never a value that could plausibly appear
# by accident elsewhere (module source, JSON structure, etc.), so a substring
# search for it is a reliable leak detector (AC-SB-007-3), same technique as
# identity.py's tests.
_SENTINEL_QUESTION = "sentinel-question-4f21c9a0-do-not-leak-me-into-the-record"

_FAKE_MCP_TOKEN = "mcp-tok-fake-should-never-appear-abc123"
_FAKE_API_KEY = "sk-ant-fake-should-never-appear-def456"


def _make_record(
    *,
    ts: str = "2026-09-14T10:00:00+00:00",
    slack_user_id: str | None = "U01ABCDEF",
    client_id: str | None = "okwon",
    channel: str | None = "C01CHANNEL",
    thread_ts: str | None = "1699999999.000100",
    question: str = "hello",
    outcome: str = "ok",
    reason_code: str | None = None,
    error_kind: str | None = None,
    duration_ms: int = 42,
    usage: Mapping[str, int] | None = None,
    request_id: str = "req-1",
) -> ObservationRecord:
    return build_record(
        ts=ts,
        slack_user_id=slack_user_id,
        client_id=client_id,
        channel=channel,
        thread_ts=thread_ts,
        question=question,
        outcome=outcome,  # type: ignore[arg-type]
        reason_code=reason_code,
        error_kind=error_kind,
        duration_ms=duration_ms,
        usage=usage,
        request_id=request_id,
    )


class _RecordingStream:
    """Fake ``ObservationStream`` recording writes/flushes for assertions."""

    def __init__(self) -> None:
        self.chunks: list[str] = []
        self.flush_count = 0

    def write(self, s: str) -> int:
        self.chunks.append(s)
        return len(s)

    def flush(self) -> None:
        self.flush_count += 1


class _RaisingStream:
    """Fake stream whose write/flush always raise — simulates a broken sink."""

    def write(self, s: str) -> int:
        raise OSError("stream is closed")

    def flush(self) -> None:
        raise OSError("stream is closed")


class _RaisingUsageMapping(Mapping[str, int]):
    """Simulates an internal failure while summarizing ``usage``."""

    def __getitem__(self, key: str) -> int:
        raise RuntimeError("boom")

    def __iter__(self):  # noqa: ANN202
        raise RuntimeError("boom")

    def __len__(self) -> int:
        raise RuntimeError("boom")

    def get(self, key: str, default: object = None) -> object:  # type: ignore[override]
        raise RuntimeError("boom")


# --- AC-SB-007-1: one valid JSON line, all CTR-SB-008 fields present --------


def test_emits_exactly_one_line() -> None:
    stream = io.StringIO()

    emit(_make_record(), stream=stream)

    output = stream.getvalue()
    assert output.endswith("\n")
    assert output.count("\n") == 1


def test_emitted_line_is_valid_json() -> None:
    stream = io.StringIO()

    emit(_make_record(), stream=stream)

    parsed = json.loads(stream.getvalue())
    assert isinstance(parsed, dict)


def test_all_ctr_sb_008_fields_present_with_matching_names() -> None:
    line = to_json_line(_make_record())

    parsed = json.loads(line)

    assert set(parsed.keys()) == _CTR_SB_008_FIELDS


def test_event_field_is_fixed_slack_query() -> None:
    parsed = json.loads(to_json_line(_make_record()))

    assert parsed["event"] == "slack_query"


# --- AC-SB-007-3: raw question never reaches the record ---------------------


def test_raw_question_never_appears_in_serialized_output() -> None:
    line = to_json_line(_make_record(question=_SENTINEL_QUESTION))

    assert _SENTINEL_QUESTION not in line


def test_raw_question_never_appears_in_record_repr_or_str() -> None:
    record = _make_record(question=_SENTINEL_QUESTION)

    assert _SENTINEL_QUESTION not in repr(record)
    assert _SENTINEL_QUESTION not in str(record)


def test_build_record_has_no_field_for_the_raw_question() -> None:
    record = _make_record(question=_SENTINEL_QUESTION)

    field_names = {f for f in record.__dataclass_fields__}
    assert "question" not in field_names


def test_question_len_matches_actual_length() -> None:
    question = "hello world 안녕하세요 🎉"

    record = _make_record(question=question)

    assert record.question_len == len(question)


def test_question_sha256_matches_first_16_hex_chars_of_sha256() -> None:
    question = "what does src/main.py do?"
    expected = hashlib.sha256(question.encode("utf-8")).hexdigest()[:16]

    record = _make_record(question=question)

    assert record.question_sha256 == expected
    assert len(record.question_sha256) == QUESTION_HASH_PREFIX_LEN


def test_different_questions_produce_different_hashes() -> None:
    first = _make_record(question="question one")
    second = _make_record(question="question two")

    assert first.question_sha256 != second.question_sha256


def test_empty_question_is_handled_safely() -> None:
    record = _make_record(question="")

    assert record.question_len == 0
    assert record.question_sha256 == hashlib.sha256(b"").hexdigest()[:16]


# --- single-line guarantee under adversarial content -------------------------


@pytest.mark.parametrize(
    "question",
    [
        "line one\nline two\r\nwith \"quotes\" and 'apostrophes'",
        "한국어 질문입니다 - 이 저장소는 무엇을 하나요?",
        "emoji party 🎉🚀🔥 and \x00 control chars \t tabs",
    ],
)
def test_output_stays_one_line_regardless_of_question_content(question: str) -> None:
    line = to_json_line(_make_record(question=question))

    assert "\n" not in line
    assert "\r" not in line
    json.loads(line)  # must still be valid JSON


def test_output_stays_one_line_with_korean_reason_code() -> None:
    line = to_json_line(
        _make_record(outcome="denied", reason_code="등록되지 않은 사용자\n두번째 줄")
    )

    assert "\n" not in line
    parsed = json.loads(line)
    assert parsed["reason_code"] == "등록되지 않은 사용자\n두번째 줄"


# --- AC-SB-007-2 / EDGE-SB-017: usage (cost visibility) ----------------------


def test_usage_three_fields_are_present_when_given() -> None:
    usage = {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 10}

    parsed = json.loads(to_json_line(_make_record(outcome="ok", usage=usage)))

    assert parsed["usage"] == {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_input_tokens": 10,
    }


def test_usage_none_serializes_to_null() -> None:
    parsed = json.loads(to_json_line(_make_record(outcome="denied", usage=None)))

    assert parsed["usage"] is None


def test_usage_with_missing_fields_fills_them_with_null() -> None:
    parsed = json.loads(to_json_line(_make_record(usage={"input_tokens": 7})))

    assert parsed["usage"] == {
        "input_tokens": 7,
        "output_tokens": None,
        "cache_read_input_tokens": None,
    }


def test_usage_summary_type_holds_only_the_three_fields() -> None:
    summary = UsageSummary(input_tokens=1, output_tokens=2, cache_read_input_tokens=3)

    field_names = set(summary.__dataclass_fields__)
    assert field_names == {"input_tokens", "output_tokens", "cache_read_input_tokens"}


# --- outcome / reason_code / error_kind --------------------------------------


def test_ok_outcome_has_null_reason_code_and_error_kind() -> None:
    parsed = json.loads(to_json_line(_make_record(outcome="ok")))

    assert parsed["outcome"] == "ok"
    assert parsed["reason_code"] is None
    assert parsed["error_kind"] is None


def test_denied_outcome_carries_reason_code() -> None:
    parsed = json.loads(
        to_json_line(_make_record(outcome="denied", reason_code="user_unidentified"))
    )

    assert parsed["outcome"] == "denied"
    assert parsed["reason_code"] == "user_unidentified"
    assert parsed["error_kind"] is None


def test_error_outcome_carries_error_kind() -> None:
    parsed = json.loads(to_json_line(_make_record(outcome="error", error_kind="TimeoutError")))

    assert parsed["outcome"] == "error"
    assert parsed["error_kind"] == "TimeoutError"
    assert parsed["reason_code"] is None


# --- design-level guarantee: no field exists for a token/key ------------------


def test_build_record_signature_has_no_token_or_key_parameter() -> None:
    with pytest.raises(TypeError):
        build_record(
            ts="2026-09-14T10:00:00+00:00",
            slack_user_id="U01",
            client_id="okwon",
            channel="C01",
            thread_ts="1.1",
            question="q",
            outcome="ok",
            reason_code=None,
            error_kind=None,
            duration_ms=1,
            usage=None,
            request_id="req-1",
            mcp_token=_FAKE_MCP_TOKEN,  # type: ignore[call-arg]  # not a real parameter
        )


def test_fake_credential_values_never_appear_when_passed_as_legitimate_fields() -> None:
    # client_id/slack_user_id are legitimate identifier fields (client_id is a
    # person, e.g. "okwon" — never a token), so passing a token-shaped string
    # into `question` (the only field that could plausibly carry one by
    # mistake) must still never surface it, since `question` never reaches
    # the record at all.
    record = _make_record(question=f"my token is {_FAKE_API_KEY}")

    line = to_json_line(record)

    assert _FAKE_API_KEY not in line
    assert _FAKE_MCP_TOKEN not in line


# --- stream injection + flush --------------------------------------------------


def test_stream_injection_is_captured_by_stringio() -> None:
    stream = io.StringIO()

    emit(_make_record(request_id="req-xyz"), stream=stream)

    parsed = json.loads(stream.getvalue())
    assert parsed["request_id"] == "req-xyz"


def test_emit_flushes_the_stream() -> None:
    stream = _RecordingStream()

    emit(_make_record(), stream=stream)

    assert stream.flush_count == 1


def test_multiple_emits_produce_one_line_per_record_in_order() -> None:
    stream = io.StringIO()
    record_count = 20

    for i in range(record_count):
        emit(_make_record(request_id=f"req-{i}"), stream=stream)

    lines = stream.getvalue().splitlines()
    assert len(lines) == record_count
    for i, line in enumerate(lines):
        assert json.loads(line)["request_id"] == f"req-{i}"


# --- failure policy: recording must never kill the caller's real work -------


def test_emit_swallows_write_failures_without_raising() -> None:
    emit(_make_record(), stream=_RaisingStream())  # must not raise


def test_emit_logs_the_failure_when_the_stream_raises(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    with caplog.at_level(logging.ERROR, logger="devoks_slackbot.observability"):
        emit(_make_record(request_id="req-fail"), stream=_RaisingStream())

    assert any("req-fail" in record.getMessage() for record in caplog.records)


def test_emit_query_observation_never_raises_even_with_a_broken_stream() -> None:
    emit_query_observation(
        ts="2026-09-14T10:00:00+00:00",
        slack_user_id="U01",
        client_id="okwon",
        channel="C01",
        thread_ts="1.1",
        question="hello",
        outcome="ok",
        reason_code=None,
        error_kind=None,
        duration_ms=5,
        usage=None,
        request_id="req-composite",
        stream=_RaisingStream(),
    )  # must not raise


def test_emit_query_observation_never_raises_even_when_build_fails() -> None:
    emit_query_observation(
        ts="2026-09-14T10:00:00+00:00",
        slack_user_id="U01",
        client_id="okwon",
        channel="C01",
        thread_ts="1.1",
        question="hello",
        outcome="ok",
        reason_code=None,
        error_kind=None,
        duration_ms=5,
        usage=_RaisingUsageMapping(),
        request_id="req-build-fail",
        stream=io.StringIO(),
    )  # must not raise


def test_emit_query_observation_writes_a_valid_line_on_the_happy_path() -> None:
    stream = io.StringIO()

    emit_query_observation(
        ts="2026-09-14T10:00:00+00:00",
        slack_user_id="U01",
        client_id="okwon",
        channel="C01",
        thread_ts="1.1",
        question=_SENTINEL_QUESTION,
        outcome="ok",
        reason_code=None,
        error_kind=None,
        duration_ms=5,
        usage={"input_tokens": 3, "output_tokens": 4, "cache_read_input_tokens": None},
        request_id="req-happy",
        stream=stream,
    )

    output = stream.getvalue()
    assert output.count("\n") == 1
    assert _SENTINEL_QUESTION not in output
    parsed = json.loads(output)
    assert parsed["request_id"] == "req-happy"
    assert parsed["usage"] == {
        "input_tokens": 3,
        "output_tokens": 4,
        "cache_read_input_tokens": None,
    }


# --- repeated / rapid emission (연타) ------------------------------------------


def test_rapid_repeated_emits_for_the_same_thread_each_produce_a_full_valid_record() -> None:
    # Coalescing (EDGE-SB-015) blocks duplicate work upstream, but this module
    # itself makes no such assumption — every call it receives must still
    # produce one complete, well-formed record.
    stream = io.StringIO()

    for i in range(5):
        emit_query_observation(
            ts="2026-09-14T10:00:00+00:00",
            slack_user_id="U01",
            client_id="okwon",
            channel="C01",
            thread_ts="1699999999.000100",
            question=f"question number {i}",
            outcome="ok",
            reason_code=None,
            error_kind=None,
            duration_ms=i,
            usage=None,
            request_id=f"req-rapid-{i}",
            stream=stream,
        )

    lines = stream.getvalue().splitlines()
    assert len(lines) == 5
    for i, line in enumerate(lines):
        parsed = json.loads(line)
        assert parsed["request_id"] == f"req-rapid-{i}"
        assert set(parsed.keys()) == _CTR_SB_008_FIELDS
