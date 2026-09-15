"""Tests for devoks_slackbot.slack.events (TASK-004).

Traces: AC-SB-006-3, CTR-SB-006, EDGE-SB-011, EDGE-SB-019.
"""

from __future__ import annotations

from typing import Any

import pytest

from devoks_slackbot.slack.events import (
    extract_challenge,
    extract_channel,
    extract_event_id,
    extract_question_text,
    extract_reply_target_ts,
    extract_user_id,
    is_app_mention_event,
    is_bot_self_message,
    is_url_verification,
)

from .conftest import VALID_TEST_BOT_USER_ID

# Slack's own documented `app_mention` example payload (FRD §... / task
# context) — used verbatim so CTR-SB-006 is pinned against the real shape,
# not a hand-rolled approximation of it.
_OFFICIAL_APP_MENTION_PAYLOAD: dict[str, Any] = {
    "token": "XXYYZZ",
    "team_id": "T123ABC456",
    "api_app_id": "A123ABC456",
    "event": {
        "type": "app_mention",
        "user": "U123ABC456",
        "text": "<@U0LAN0Z89> is it everything a river should be?",
        "ts": "1515449522.000016",
        "channel": "C123ABC456",
        "event_ts": "1515449522000016",
    },
    "type": "event_callback",
    "authorizations": [
        {
            "team_id": "T123ABC456",
            "user_id": "U123ABC456",
            "is_bot": False,
            "is_enterprise_install": False,
        }
    ],
    "event_id": "Ev123ABC456",
    "event_time": 123456789,
}

_URL_VERIFICATION_PAYLOAD: dict[str, Any] = {
    "token": "XXYYZZ",
    "challenge": "3eZbrw1aBm2rZgRNFdxV2595E9CY3gmdALWMmHkvFXO7tYXAYM8P",
    "type": "url_verification",
}


def _app_mention_payload(
    *,
    user: str | None = "U123ABC456",
    bot_id: str | None = None,
    thread_ts: str | None = None,
    ts: str = "1515449522.000016",
    event_id: str | None = "Ev123ABC456",
    authorization_user_id: str = "U999AUTHOR",
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "app_mention",
        "text": "<@U0LAN0Z89> question",
        "ts": ts,
        "channel": "C123ABC456",
        "event_ts": "1515449522000016",
    }
    if user is not None:
        event["user"] = user
    if bot_id is not None:
        event["bot_id"] = bot_id
    if thread_ts is not None:
        event["thread_ts"] = thread_ts

    payload: dict[str, Any] = {
        "token": "XXYYZZ",
        "team_id": "T123ABC456",
        "event": event,
        "type": "event_callback",
        "authorizations": [
            {
                "team_id": "T123ABC456",
                "user_id": authorization_user_id,
                "is_bot": False,
                "is_enterprise_install": False,
            }
        ],
        "event_time": 123456789,
    }
    if event_id is not None:
        payload["event_id"] = event_id
    return payload


# --- CTR-SB-006: official payload shape --------------------------------------


def test_official_app_mention_payload_is_recognized_CTR_SB_006() -> None:
    assert is_app_mention_event(_OFFICIAL_APP_MENTION_PAYLOAD) is True


def test_official_app_mention_payload_user_extracted_CTR_SB_006() -> None:
    assert extract_user_id(_OFFICIAL_APP_MENTION_PAYLOAD) == "U123ABC456"


def test_official_app_mention_payload_is_not_bot_self_message() -> None:
    assert (
        is_bot_self_message(_OFFICIAL_APP_MENTION_PAYLOAD, bot_user_id=VALID_TEST_BOT_USER_ID)
        is False
    )


def test_official_app_mention_payload_event_id_extracted() -> None:
    assert extract_event_id(_OFFICIAL_APP_MENTION_PAYLOAD) == "Ev123ABC456"


def test_official_app_mention_payload_reply_target_falls_back_to_ts() -> None:
    assert extract_reply_target_ts(_OFFICIAL_APP_MENTION_PAYLOAD) == "1515449522.000016"


# --- EDGE-SB-019: authorizations[].user_id must never be mistaken for the ---
# --- questioner ---------------------------------------------------------------


def test_extract_user_id_uses_event_user_not_authorizations_EDGE_SB_019() -> None:
    payload = _app_mention_payload(user="U_QUESTIONER01", authorization_user_id="U_INSTALLER99")
    assert payload["authorizations"][0]["user_id"] != payload["event"]["user"]
    assert extract_user_id(payload) == "U_QUESTIONER01"
    assert extract_user_id(payload) != payload["authorizations"][0]["user_id"]


# --- url_verification ---------------------------------------------------------


def test_url_verification_payload_recognized() -> None:
    assert is_url_verification(_URL_VERIFICATION_PAYLOAD) is True
    assert is_app_mention_event(_URL_VERIFICATION_PAYLOAD) is False


def test_url_verification_challenge_extracted() -> None:
    assert (
        extract_challenge(_URL_VERIFICATION_PAYLOAD)
        == "3eZbrw1aBm2rZgRNFdxV2595E9CY3gmdALWMmHkvFXO7tYXAYM8P"
    )


def test_url_verification_missing_challenge_returns_none() -> None:
    payload = {"token": "XXYYZZ", "type": "url_verification"}
    assert is_url_verification(payload) is True
    assert extract_challenge(payload) is None


# --- EDGE-SB-011 / AC-SB-006-3: bot self-trigger filtering --------------------


def test_bot_self_message_by_matching_user_id_EDGE_SB_011() -> None:
    payload = _app_mention_payload(user=VALID_TEST_BOT_USER_ID)
    assert is_bot_self_message(payload, bot_user_id=VALID_TEST_BOT_USER_ID) is True


def test_bot_self_message_by_bot_id_with_no_user_EDGE_SB_011() -> None:
    payload = _app_mention_payload(user=None, bot_id="B0123456")
    assert is_bot_self_message(payload, bot_user_id=VALID_TEST_BOT_USER_ID) is True


def test_bot_self_message_by_bot_id_even_if_user_also_present_AC_SB_006_3() -> None:
    payload = _app_mention_payload(user="U_SOMEONE_ELSE", bot_id="B0123456")
    assert is_bot_self_message(payload, bot_user_id=VALID_TEST_BOT_USER_ID) is True


def test_ordinary_user_message_not_filtered_AC_SB_006_3() -> None:
    payload = _app_mention_payload(user="U_REAL_HUMAN01")
    assert is_bot_self_message(payload, bot_user_id=VALID_TEST_BOT_USER_ID) is False


# --- thread reply target -------------------------------------------------------


def test_reply_target_uses_thread_ts_when_present() -> None:
    payload = _app_mention_payload(ts="100.001", thread_ts="99.000")
    assert extract_reply_target_ts(payload) == "99.000"


def test_reply_target_uses_ts_when_no_thread_ts() -> None:
    payload = _app_mention_payload(ts="100.001", thread_ts=None)
    assert extract_reply_target_ts(payload) == "100.001"


# --- TASK-014: channel / raw question text extraction ------------------------


def test_extract_channel_returns_event_channel() -> None:
    assert extract_channel(_OFFICIAL_APP_MENTION_PAYLOAD) == "C123ABC456"


def test_extract_question_text_returns_raw_text_including_mention_token() -> None:
    # Raw means raw -- stripping the "<@BOT_ID>" mention token is worker.py's
    # job (TASK-014), not this module's (see extract_question_text's docstring).
    assert (
        extract_question_text(_OFFICIAL_APP_MENTION_PAYLOAD)
        == "<@U0LAN0Z89> is it everything a river should be?"
    )


def test_extract_channel_missing_event_returns_none() -> None:
    payload: dict[str, Any] = {"type": "event_callback", "event_id": "Ev1"}
    assert extract_channel(payload) is None


def test_extract_question_text_missing_event_returns_none() -> None:
    payload: dict[str, Any] = {"type": "event_callback", "event_id": "Ev1"}
    assert extract_question_text(payload) is None


# --- defensive payload variants: never raise, resolve to safe values ---------


def test_missing_event_key_never_raises() -> None:
    payload: dict[str, Any] = {"type": "event_callback", "event_id": "Ev1"}
    assert is_app_mention_event(payload) is False
    assert extract_user_id(payload) is None
    assert is_bot_self_message(payload, bot_user_id=VALID_TEST_BOT_USER_ID) is False
    assert extract_reply_target_ts(payload) is None


@pytest.mark.parametrize("bad_event", ["not-a-dict", 123, ["a", "list"], None])
def test_event_not_a_dict_never_raises(bad_event: Any) -> None:
    payload: dict[str, Any] = {"type": "event_callback", "event": bad_event, "event_id": "Ev1"}
    assert is_app_mention_event(payload) is False
    assert extract_user_id(payload) is None
    assert is_bot_self_message(payload, bot_user_id=VALID_TEST_BOT_USER_ID) is False
    assert extract_reply_target_ts(payload) is None


@pytest.mark.parametrize("bad_user", [None, "", 12345, ["U123"], {"id": "U123"}])
def test_event_user_missing_or_malformed_returns_none(bad_user: Any) -> None:
    event: dict[str, Any] = {"type": "app_mention", "ts": "1.0"}
    if bad_user is not None:
        event["user"] = bad_user
    payload: dict[str, Any] = {"type": "event_callback", "event": event, "event_id": "Ev1"}
    assert extract_user_id(payload) is None
    # Also must not be mistaken for a bot self-message purely from a missing
    # user, absent a bot_id (EDGE-SB-011 requires the bot_id/user_id signal).
    assert is_bot_self_message(payload, bot_user_id=VALID_TEST_BOT_USER_ID) is False


def test_missing_event_id_returns_none_not_raise() -> None:
    payload = _app_mention_payload(event_id=None)
    assert "event_id" not in payload
    assert extract_event_id(payload) is None


def test_unknown_top_level_type_is_neither_verification_nor_mention() -> None:
    payload: dict[str, Any] = {"type": "some_future_event_type", "event": {"type": "app_mention"}}
    assert is_url_verification(payload) is False
    assert is_app_mention_event(payload) is False


def test_missing_top_level_type_never_raises() -> None:
    payload: dict[str, Any] = {"event": {"type": "app_mention", "user": "U1"}}
    assert is_url_verification(payload) is False
    assert is_app_mention_event(payload) is False
