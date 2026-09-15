"""Tests for devoks_slackbot.identity (TASK-005).

Traces: REQ-SB-004, AC-SB-004-1, AC-SB-004-2, AC-SB-004-3, AC-SB-004-4,
CTR-SB-006, EDGE-SB-006, EDGE-SB-012, DSN-SB-003.
"""

from __future__ import annotations

import logging

import pytest

from devoks_slackbot.identity import (
    MAPPING_SIZE_WARNING_THRESHOLD,
    resolve_credentials,
)

# A unique, unambiguous sentinel — never a value that could plausibly appear
# by accident elsewhere (module source, log format strings, etc.), so a
# substring search for it is a reliable leak detector (AC-SB-004-4).
_SENTINEL_TOKEN = "mcp-tok-sentinel-9f3c7a1e-do-not-print-me"

_REGISTERED_USER_ID = "U01REGISTERED"
_UNREGISTERED_USER_ID = "U99UNREGISTERED"


def _fixture_mapping(count: int) -> dict[str, str]:
    return {f"U{index:010d}": f"mcp-fixture-token-{index:03d}" for index in range(count)}


# --- AC-SB-004-1: registered user gets their own token -----------------------


def test_registered_user_id_returns_their_mapped_token() -> None:
    mapping = {_REGISTERED_USER_ID: _SENTINEL_TOKEN}

    result = resolve_credentials(_REGISTERED_USER_ID, mapping)

    assert result.granted is True
    assert result.mcp_token == _SENTINEL_TOKEN
    assert result.client_message is None
    assert result.reason_code is None


def test_registered_user_among_many_gets_only_their_own_token() -> None:
    mapping = {**_fixture_mapping(5), _REGISTERED_USER_ID: _SENTINEL_TOKEN}

    result = resolve_credentials(_REGISTERED_USER_ID, mapping)

    assert result.granted is True
    assert result.mcp_token == _SENTINEL_TOKEN


# --- AC-SB-004-2 / EDGE-SB-006: unregistered user is denied, fail-safe -------


def test_unregistered_user_id_is_denied() -> None:
    mapping = {_REGISTERED_USER_ID: _SENTINEL_TOKEN}

    result = resolve_credentials(_UNREGISTERED_USER_ID, mapping)

    assert result.granted is False
    assert result.mcp_token is None
    assert result.client_message is not None
    assert result.reason_code == "user_unregistered"


def test_empty_mapping_denies_any_user() -> None:
    result = resolve_credentials(_UNREGISTERED_USER_ID, {})

    assert result.granted is False
    assert result.mcp_token is None


def test_empty_string_token_value_denies_safely() -> None:
    # config.py already rejects empty-string token values at parse time, but
    # this module does not trust that every caller went through that path —
    # a mapping that somehow carries one must still deny rather than grant a
    # blank/invalid credential.
    mapping = {_REGISTERED_USER_ID: ""}

    result = resolve_credentials(_REGISTERED_USER_ID, mapping)

    assert result.granted is False
    assert result.mcp_token is None


# --- EDGE-SB-019 input contract: extract_user_id can hand back None ----------


def test_unidentified_user_none_is_denied() -> None:
    mapping = {_REGISTERED_USER_ID: _SENTINEL_TOKEN}

    result = resolve_credentials(None, mapping)

    assert result.granted is False
    assert result.mcp_token is None
    assert result.reason_code == "user_unidentified"


def test_empty_string_user_id_is_treated_as_unidentified() -> None:
    # extract_user_id (TASK-004) never returns "", but this module does not
    # rely on that upstream guarantee — an empty string carries no more
    # identity than None does.
    mapping = {_REGISTERED_USER_ID: _SENTINEL_TOKEN}

    result = resolve_credentials("", mapping)

    assert result.granted is False
    assert result.reason_code == "user_unidentified"


def test_none_and_unregistered_denials_are_distinguished_in_operator_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    mapping = {_REGISTERED_USER_ID: _SENTINEL_TOKEN}

    with caplog.at_level(logging.INFO, logger="devoks_slackbot.identity"):
        resolve_credentials(None, mapping)
        resolve_credentials(_UNREGISTERED_USER_ID, mapping)

    messages = [record.getMessage() for record in caplog.records]
    assert any("user_unidentified" in message for message in messages)
    assert any("user_unregistered" in message for message in messages)


# --- AC-SB-004-3: no information disclosure in the client-facing message ----


def test_denial_message_never_reveals_other_user_ids() -> None:
    mapping = _fixture_mapping(5)
    other_ids = list(mapping.keys())

    result = resolve_credentials(_UNREGISTERED_USER_ID, mapping)

    assert result.client_message is not None
    for other_id in other_ids:
        assert other_id not in result.client_message


def test_denial_message_never_reveals_registered_count_or_mapping_size() -> None:
    mapping = _fixture_mapping(5)

    result = resolve_credentials(_UNREGISTERED_USER_ID, mapping)

    assert result.client_message is not None
    assert "5" not in result.client_message
    assert str(len(mapping)) not in result.client_message


def test_unregistered_denial_message_is_identical_for_empty_and_populated_mapping() -> None:
    empty_result = resolve_credentials(_UNREGISTERED_USER_ID, {})
    populated_result = resolve_credentials(_UNREGISTERED_USER_ID, _fixture_mapping(5))

    assert empty_result.client_message == populated_result.client_message


def test_unidentified_and_unregistered_denials_share_the_same_client_message() -> None:
    mapping = _fixture_mapping(5)

    unidentified_result = resolve_credentials(None, mapping)
    unregistered_result = resolve_credentials(_UNREGISTERED_USER_ID, mapping)

    assert unidentified_result.client_message == unregistered_result.client_message


# --- AC-SB-004-4: the token value never appears in repr/log/exception -------


def test_token_never_appears_in_result_repr() -> None:
    mapping = {_REGISTERED_USER_ID: _SENTINEL_TOKEN}

    result = resolve_credentials(_REGISTERED_USER_ID, mapping)

    assert _SENTINEL_TOKEN not in repr(result)
    assert _SENTINEL_TOKEN not in str(result)


def test_token_never_appears_in_operator_log_for_any_outcome(
    caplog: pytest.LogCaptureFixture,
) -> None:
    mapping = {_REGISTERED_USER_ID: _SENTINEL_TOKEN}

    with caplog.at_level(logging.DEBUG, logger="devoks_slackbot.identity"):
        resolve_credentials(_REGISTERED_USER_ID, mapping)
        resolve_credentials(_UNREGISTERED_USER_ID, mapping)
        resolve_credentials(None, mapping)

    for record in caplog.records:
        assert _SENTINEL_TOKEN not in record.getMessage()


# --- EDGE-SB-012: mapping size warning ---------------------------------------


def test_mapping_size_at_threshold_does_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    mapping = _fixture_mapping(MAPPING_SIZE_WARNING_THRESHOLD)

    with caplog.at_level(logging.WARNING, logger="devoks_slackbot.identity"):
        resolve_credentials(_UNREGISTERED_USER_ID, mapping)

    assert caplog.records == []


def test_mapping_size_below_threshold_does_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    mapping = _fixture_mapping(MAPPING_SIZE_WARNING_THRESHOLD - 1)

    with caplog.at_level(logging.WARNING, logger="devoks_slackbot.identity"):
        resolve_credentials(_UNREGISTERED_USER_ID, mapping)

    assert caplog.records == []


def test_mapping_size_over_threshold_warns_without_leaking_tokens_or_ids(
    caplog: pytest.LogCaptureFixture,
) -> None:
    mapping = _fixture_mapping(MAPPING_SIZE_WARNING_THRESHOLD + 1)

    with caplog.at_level(logging.WARNING, logger="devoks_slackbot.identity"):
        resolve_credentials(_UNREGISTERED_USER_ID, mapping)

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert str(MAPPING_SIZE_WARNING_THRESHOLD + 1) in message
    for user_id, token in mapping.items():
        assert user_id not in message
        assert token not in message


def test_repeated_lookups_against_an_oversized_mapping_warn_every_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Deliberate: a warm Lambda container can serve many events without a
    # cold start, so the warning fires per call rather than only once, or an
    # operator watching a live log could miss it entirely.
    mapping = _fixture_mapping(MAPPING_SIZE_WARNING_THRESHOLD + 1)

    with caplog.at_level(logging.WARNING, logger="devoks_slackbot.identity"):
        resolve_credentials(_UNREGISTERED_USER_ID, mapping)
        resolve_credentials(_UNREGISTERED_USER_ID, mapping)
        resolve_credentials(_UNREGISTERED_USER_ID, mapping)

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 3
