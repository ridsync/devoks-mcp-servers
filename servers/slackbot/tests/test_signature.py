"""Tests for devoks_slackbot.slack.signature (TASK-003).

Traces: REQ-SB-001, AC-SB-001-1, AC-SB-001-2, AC-SB-001-3, AC-SB-001-4,
AC-SB-001-5, CTR-SB-001, CTR-SB-003, EDGE-SB-001, EDGE-SB-002, DSN-SB-007.
"""

import hashlib
import hmac
import json
import time

import pytest

from devoks_slackbot.slack.signature import (
    DEFAULT_TIMESTAMP_TOLERANCE_SECONDS,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    verify_slack_signature,
)

from .conftest import VALID_TEST_SIGNING_SECRET

_OTHER_SECRET = "a-different-fixture-signing-secret-not-a-real-credential"
_FIXED_NOW = 1_700_000_000.0
_TIMESTAMP = str(int(_FIXED_NOW))
_BODY = b'{"type":"event_callback","event":{"type":"app_mention","text":"hi"}}'


def _reference_signature(secret: str, timestamp: str, body: bytes) -> str:
    """Independently-built HMAC per CTR-SB-001, used only to construct fixtures.

    Written with bytes concatenation rather than the module's own
    f-string-then-encode path on purpose, so a bug in the production
    formula would not be silently mirrored by the test fixture.
    """
    basestring = b"v0:" + timestamp.encode("ascii") + b":" + body
    digest = hmac.new(secret.encode("utf-8"), basestring, hashlib.sha256).hexdigest()
    return "v0=" + digest


def _valid_headers(
    *,
    secret: str = VALID_TEST_SIGNING_SECRET,
    timestamp: str = _TIMESTAMP,
    body: bytes = _BODY,
    sig_name: str = SIGNATURE_HEADER,
    ts_name: str = TIMESTAMP_HEADER,
) -> dict[str, str]:
    return {sig_name: _reference_signature(secret, timestamp, body), ts_name: timestamp}


# --- normal cases ------------------------------------------------------------


def test_valid_signature_passes_AC_SB_001_1() -> None:
    headers = _valid_headers()
    assert (
        verify_slack_signature(
            headers=headers,
            raw_body=_BODY,
            signing_secret=VALID_TEST_SIGNING_SECRET,
            now=_FIXED_NOW,
        )
        is True
    )


def test_now_defaults_to_current_time() -> None:
    ts = str(int(time.time()))
    headers = _valid_headers(timestamp=ts)
    assert (
        verify_slack_signature(
            headers=headers, raw_body=_BODY, signing_secret=VALID_TEST_SIGNING_SECRET
        )
        is True
    )


def test_repeated_verification_is_idempotent() -> None:
    headers = _valid_headers()
    results = [
        verify_slack_signature(
            headers=headers,
            raw_body=_BODY,
            signing_secret=VALID_TEST_SIGNING_SECRET,
            now=_FIXED_NOW,
        )
        for _ in range(5)
    ]
    assert results == [True] * 5


def test_default_tolerance_matches_CTR_SB_003() -> None:
    assert DEFAULT_TIMESTAMP_TOLERANCE_SECONDS == 300


@pytest.mark.parametrize(
    ("sig_name", "ts_name"),
    [
        (SIGNATURE_HEADER, TIMESTAMP_HEADER),
        ("x-slack-signature", "x-slack-request-timestamp"),
        ("X-SLACK-SIGNATURE", "X-SLACK-REQUEST-TIMESTAMP"),
        ("X-Slack-SIGNATURE", "x-Slack-Request-Timestamp"),
    ],
)
def test_header_name_case_insensitive_AC_SB_001_5(sig_name: str, ts_name: str) -> None:
    headers = _valid_headers(sig_name=sig_name, ts_name=ts_name)
    assert (
        verify_slack_signature(
            headers=headers,
            raw_body=_BODY,
            signing_secret=VALID_TEST_SIGNING_SECRET,
            now=_FIXED_NOW,
        )
        is True
    )


def test_custom_tolerance_seconds_respected() -> None:
    ts = str(int(_FIXED_NOW) - 10)
    headers = _valid_headers(timestamp=ts)
    assert (
        verify_slack_signature(
            headers=headers,
            raw_body=_BODY,
            signing_secret=VALID_TEST_SIGNING_SECRET,
            now=_FIXED_NOW,
            tolerance_seconds=5,
        )
        is False
    )
    assert (
        verify_slack_signature(
            headers=headers,
            raw_body=_BODY,
            signing_secret=VALID_TEST_SIGNING_SECRET,
            now=_FIXED_NOW,
            tolerance_seconds=20,
        )
        is True
    )


def test_constant_time_compare_used_AC_SB_001_4(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[bytes, bytes]] = []
    original_compare_digest = hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return original_compare_digest(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    headers = _valid_headers()
    assert (
        verify_slack_signature(
            headers=headers,
            raw_body=_BODY,
            signing_secret=VALID_TEST_SIGNING_SECRET,
            now=_FIXED_NOW,
        )
        is True
    )
    assert len(calls) == 1


# --- boundary cases (CTR-SB-003: 300s exactly passes, 301s rejects) ---------


def test_timestamp_exactly_at_tolerance_boundary_passes_CTR_SB_003() -> None:
    ts = str(int(_FIXED_NOW) - DEFAULT_TIMESTAMP_TOLERANCE_SECONDS)
    headers = _valid_headers(timestamp=ts)
    assert (
        verify_slack_signature(
            headers=headers,
            raw_body=_BODY,
            signing_secret=VALID_TEST_SIGNING_SECRET,
            now=_FIXED_NOW,
        )
        is True
    )


def test_timestamp_one_second_past_tolerance_rejected_CTR_SB_003() -> None:
    ts = str(int(_FIXED_NOW) - DEFAULT_TIMESTAMP_TOLERANCE_SECONDS - 1)
    headers = _valid_headers(timestamp=ts)
    assert (
        verify_slack_signature(
            headers=headers,
            raw_body=_BODY,
            signing_secret=VALID_TEST_SIGNING_SECRET,
            now=_FIXED_NOW,
        )
        is False
    )


def test_timestamp_too_old_rejected_AC_SB_001_3_EDGE_SB_002() -> None:
    ts = str(int(_FIXED_NOW) - DEFAULT_TIMESTAMP_TOLERANCE_SECONDS - 100)
    headers = _valid_headers(timestamp=ts)
    assert (
        verify_slack_signature(
            headers=headers,
            raw_body=_BODY,
            signing_secret=VALID_TEST_SIGNING_SECRET,
            now=_FIXED_NOW,
        )
        is False
    )


def test_timestamp_too_future_rejected_AC_SB_001_3_EDGE_SB_002() -> None:
    ts = str(int(_FIXED_NOW) + DEFAULT_TIMESTAMP_TOLERANCE_SECONDS + 100)
    headers = _valid_headers(timestamp=ts)
    assert (
        verify_slack_signature(
            headers=headers,
            raw_body=_BODY,
            signing_secret=VALID_TEST_SIGNING_SECRET,
            now=_FIXED_NOW,
        )
        is False
    )


# --- error / rejection cases (never raise, EDGE-SB-001: reject, don't parse) -


def test_tampered_signature_rejected_AC_SB_001_2_EDGE_SB_001() -> None:
    signature = _reference_signature(VALID_TEST_SIGNING_SECRET, _TIMESTAMP, _BODY)
    tampered = signature[:-1] + ("0" if signature[-1] != "0" else "1")
    headers = {SIGNATURE_HEADER: tampered, TIMESTAMP_HEADER: _TIMESTAMP}
    assert (
        verify_slack_signature(
            headers=headers,
            raw_body=_BODY,
            signing_secret=VALID_TEST_SIGNING_SECRET,
            now=_FIXED_NOW,
        )
        is False
    )


def test_wrong_secret_rejected() -> None:
    headers = _valid_headers()
    assert (
        verify_slack_signature(
            headers=headers, raw_body=_BODY, signing_secret=_OTHER_SECRET, now=_FIXED_NOW
        )
        is False
    )


def test_reserialized_json_body_breaks_signature_CTR_SB_001() -> None:
    original_body = b'{"type": "event_callback",   "event_id": "Ev123"}'
    headers = _valid_headers(body=original_body)
    reparsed_body = json.dumps(json.loads(original_body)).encode("utf-8")

    # Sanity: re-serialization really does produce different bytes here —
    # otherwise this test would not exercise CTR-SB-001's raw-body warning.
    assert reparsed_body != original_body

    assert (
        verify_slack_signature(
            headers=headers,
            raw_body=reparsed_body,
            signing_secret=VALID_TEST_SIGNING_SECRET,
            now=_FIXED_NOW,
        )
        is False
    )
    assert (
        verify_slack_signature(
            headers=headers,
            raw_body=original_body,
            signing_secret=VALID_TEST_SIGNING_SECRET,
            now=_FIXED_NOW,
        )
        is True
    )


def test_missing_signature_header_rejected() -> None:
    headers = {TIMESTAMP_HEADER: _TIMESTAMP}
    assert (
        verify_slack_signature(
            headers=headers,
            raw_body=_BODY,
            signing_secret=VALID_TEST_SIGNING_SECRET,
            now=_FIXED_NOW,
        )
        is False
    )


def test_missing_timestamp_header_rejected() -> None:
    signature = _reference_signature(VALID_TEST_SIGNING_SECRET, _TIMESTAMP, _BODY)
    headers = {SIGNATURE_HEADER: signature}
    assert (
        verify_slack_signature(
            headers=headers,
            raw_body=_BODY,
            signing_secret=VALID_TEST_SIGNING_SECRET,
            now=_FIXED_NOW,
        )
        is False
    )


def test_empty_signature_header_rejected() -> None:
    headers = {SIGNATURE_HEADER: "", TIMESTAMP_HEADER: _TIMESTAMP}
    assert (
        verify_slack_signature(
            headers=headers,
            raw_body=_BODY,
            signing_secret=VALID_TEST_SIGNING_SECRET,
            now=_FIXED_NOW,
        )
        is False
    )


def test_empty_timestamp_header_rejected() -> None:
    signature = _reference_signature(VALID_TEST_SIGNING_SECRET, _TIMESTAMP, _BODY)
    headers = {SIGNATURE_HEADER: signature, TIMESTAMP_HEADER: ""}
    assert (
        verify_slack_signature(
            headers=headers,
            raw_body=_BODY,
            signing_secret=VALID_TEST_SIGNING_SECRET,
            now=_FIXED_NOW,
        )
        is False
    )


def test_signature_missing_v0_prefix_rejected() -> None:
    full_signature = _reference_signature(VALID_TEST_SIGNING_SECRET, _TIMESTAMP, _BODY)
    digest_only = full_signature.removeprefix("v0=")
    headers = {SIGNATURE_HEADER: digest_only, TIMESTAMP_HEADER: _TIMESTAMP}
    assert (
        verify_slack_signature(
            headers=headers,
            raw_body=_BODY,
            signing_secret=VALID_TEST_SIGNING_SECRET,
            now=_FIXED_NOW,
        )
        is False
    )


def test_signature_non_hex_rejected() -> None:
    headers = {SIGNATURE_HEADER: "v0=not-hex-zzz", TIMESTAMP_HEADER: _TIMESTAMP}
    assert (
        verify_slack_signature(
            headers=headers,
            raw_body=_BODY,
            signing_secret=VALID_TEST_SIGNING_SECRET,
            now=_FIXED_NOW,
        )
        is False
    )


def test_timestamp_non_numeric_rejected() -> None:
    headers = {SIGNATURE_HEADER: "v0=deadbeef", TIMESTAMP_HEADER: "not-a-number"}
    assert (
        verify_slack_signature(
            headers=headers,
            raw_body=_BODY,
            signing_secret=VALID_TEST_SIGNING_SECRET,
            now=_FIXED_NOW,
        )
        is False
    )


def test_non_ascii_signature_header_rejected_not_raised() -> None:
    headers = {SIGNATURE_HEADER: "v0=서명값", TIMESTAMP_HEADER: _TIMESTAMP}
    # Must resolve to a plain False, never raise (TASK-043 lesson).
    assert (
        verify_slack_signature(
            headers=headers,
            raw_body=_BODY,
            signing_secret=VALID_TEST_SIGNING_SECRET,
            now=_FIXED_NOW,
        )
        is False
    )


def test_non_ascii_timestamp_header_rejected_not_raised() -> None:
    headers = {SIGNATURE_HEADER: "v0=deadbeef", TIMESTAMP_HEADER: "시간"}
    assert (
        verify_slack_signature(
            headers=headers,
            raw_body=_BODY,
            signing_secret=VALID_TEST_SIGNING_SECRET,
            now=_FIXED_NOW,
        )
        is False
    )
