"""Tests for devoks_slackbot.slack.client (TASK-010).

Traces: REQ-SB-006, AC-SB-006-1, AC-SB-006-4, EDGE-SB-020.

No real Slack network calls — every response is served by
``httpx2.MockTransport`` (same technique as
``servers/management/tests/test_github_client.py``).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable

import httpx2
import pytest

from devoks_slackbot.slack import client as client_module
from devoks_slackbot.slack.client import (
    ACKNOWLEDGEMENT_MESSAGE,
    post_message,
)

Handler = Callable[[httpx2.Request], Awaitable[httpx2.Response]]

#: Fixture-only literals — never real credentials/content — named to make
#: that unmistakable, same convention as tests/conftest.py's VALID_TEST_*.
SENTINEL_BOT_TOKEN = "xoxb-test-fixture-sentinel-token-not-a-real-credential"  # noqa: S105
SENTINEL_ANSWER_BODY = "SENTINEL-ANSWER-BODY-should-never-appear-in-a-log-line"


class _RecordingTransport:
    """``MockTransport``-backed fake Slack: queues canned responses/exceptions, records requests."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self._outcomes: list[httpx2.Response | Exception] = []

    def queue(self, outcome: httpx2.Response | Exception) -> None:
        self._outcomes.append(outcome)

    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        outcome: httpx2.Response | Exception
        if self._outcomes:
            outcome = self._outcomes.pop(0)
        else:
            outcome = httpx2.Response(200, json={"ok": True, "channel": "C1", "ts": "1.1"})
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _http_client(transport: _RecordingTransport) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(transport.handle))


# --- AC-SB-006-1: success, thread_ts, ts round trip -------------------------


async def test_post_message_success_posts_to_thread_and_returns_ts() -> None:  # AC-SB-006-1
    transport = _RecordingTransport()
    transport.queue(
        httpx2.Response(200, json={"ok": True, "channel": "C123", "ts": "1700000000.000100"})
    )
    http_client = _http_client(transport)

    result = await post_message(
        channel="C123",
        text="the answer",
        bot_token=SENTINEL_BOT_TOKEN,
        thread_ts="1699999999.000001",
        http_client=http_client,
    )

    assert result.ok is True
    assert result.ts == "1700000000.000100"
    assert result.channel == "C123"
    assert result.retryable is None
    assert result.reason_code is None
    assert len(transport.requests) == 1
    sent_payload = json.loads(transport.requests[0].content)
    assert sent_payload["thread_ts"] == "1699999999.000001"
    assert sent_payload["channel"] == "C123"
    await http_client.aclose()


async def test_post_message_without_thread_ts_omits_it_from_request() -> None:  # AC-SB-006-1
    transport = _RecordingTransport()
    transport.queue(httpx2.Response(200, json={"ok": True, "channel": "C1", "ts": "1.1"}))
    http_client = _http_client(transport)

    await post_message(
        channel="C1", text="hi", bot_token=SENTINEL_BOT_TOKEN, http_client=http_client
    )

    sent_payload = json.loads(transport.requests[0].content)
    assert "thread_ts" not in sent_payload
    await http_client.aclose()


# --- EDGE-SB-020: the most important test in this file -----------------------


async def test_post_message_not_in_channel_is_classified_non_retryable() -> None:  # EDGE-SB-020
    """HTTP 200 + {"ok": false, "error": "not_in_channel"} must be judged a failure,
    never a success — this is the trap the module docstring calls out."""
    transport = _RecordingTransport()
    transport.queue(httpx2.Response(200, json={"ok": False, "error": "not_in_channel"}))
    http_client = _http_client(transport)

    result = await post_message(
        channel="C1", text="hi", bot_token=SENTINEL_BOT_TOKEN, http_client=http_client
    )

    assert result.ok is False
    assert result.reason_code == "not_in_channel"
    assert result.retryable is False
    assert result.ts is None
    await http_client.aclose()


async def test_post_message_channel_not_found_is_classified_non_retryable() -> None:
    transport = _RecordingTransport()
    transport.queue(httpx2.Response(200, json={"ok": False, "error": "channel_not_found"}))
    http_client = _http_client(transport)

    result = await post_message(
        channel="Cbad", text="hi", bot_token=SENTINEL_BOT_TOKEN, http_client=http_client
    )

    assert result.ok is False
    assert result.reason_code == "channel_not_found"
    assert result.retryable is False
    await http_client.aclose()


async def test_post_message_unknown_slack_error_is_classified_unknown_and_non_retryable() -> None:
    transport = _RecordingTransport()
    transport.queue(httpx2.Response(200, json={"ok": False, "error": "msg_too_long"}))
    http_client = _http_client(transport)

    result = await post_message(
        channel="C1", text="hi", bot_token=SENTINEL_BOT_TOKEN, http_client=http_client
    )

    assert result.ok is False
    assert result.reason_code == "slack_error_unknown"
    assert result.retryable is False
    await http_client.aclose()


# --- HTTP 429 rate limiting ---------------------------------------------------


async def test_post_message_rate_limited_is_retryable_and_parses_retry_after() -> None:
    transport = _RecordingTransport()
    transport.queue(
        httpx2.Response(
            429, headers={"Retry-After": "5"}, json={"ok": False, "error": "rate_limited"}
        )
    )
    http_client = _http_client(transport)

    result = await post_message(
        channel="C1", text="hi", bot_token=SENTINEL_BOT_TOKEN, http_client=http_client
    )

    assert result.ok is False
    assert result.reason_code == "rate_limited"
    assert result.retryable is True
    assert result.retry_after_seconds == 5.0
    await http_client.aclose()


async def test_post_message_rate_limited_without_retry_after_header_has_none() -> None:
    transport = _RecordingTransport()
    transport.queue(httpx2.Response(429, json={"ok": False, "error": "rate_limited"}))
    http_client = _http_client(transport)

    result = await post_message(
        channel="C1", text="hi", bot_token=SENTINEL_BOT_TOKEN, http_client=http_client
    )

    assert result.reason_code == "rate_limited"
    assert result.retryable is True
    assert result.retry_after_seconds is None
    await http_client.aclose()


# --- HTTP 5xx / other HTTP errors --------------------------------------------


async def test_post_message_http_5xx_is_retryable() -> None:
    transport = _RecordingTransport()
    transport.queue(httpx2.Response(500, text="internal server error"))
    http_client = _http_client(transport)

    result = await post_message(
        channel="C1", text="hi", bot_token=SENTINEL_BOT_TOKEN, http_client=http_client
    )

    assert result.ok is False
    assert result.reason_code == "http_error"
    assert result.retryable is True
    await http_client.aclose()


async def test_post_message_http_4xx_non_429_is_not_retryable() -> None:
    transport = _RecordingTransport()
    transport.queue(httpx2.Response(400, text="bad request"))
    http_client = _http_client(transport)

    result = await post_message(
        channel="C1", text="hi", bot_token=SENTINEL_BOT_TOKEN, http_client=http_client
    )

    assert result.ok is False
    assert result.reason_code == "http_error"
    assert result.retryable is False
    await http_client.aclose()


# --- network errors / timeouts, distinguishable -------------------------------


async def test_post_message_network_error_is_retryable() -> None:
    transport = _RecordingTransport()
    transport.queue(httpx2.ConnectError("simulated connection failure"))
    http_client = _http_client(transport)

    result = await post_message(
        channel="C1", text="hi", bot_token=SENTINEL_BOT_TOKEN, http_client=http_client
    )

    assert result.ok is False
    assert result.reason_code == "network_error"
    assert result.retryable is True
    await http_client.aclose()


async def test_post_message_timeout_is_retryable_and_distinct_from_network_error() -> None:
    transport = _RecordingTransport()
    transport.queue(httpx2.ReadTimeout("simulated timeout"))
    http_client = _http_client(transport)

    result = await post_message(
        channel="C1", text="hi", bot_token=SENTINEL_BOT_TOKEN, http_client=http_client
    )

    assert result.ok is False
    assert result.reason_code == "timeout"
    assert result.reason_code != "network_error"
    assert result.retryable is True
    await http_client.aclose()


# --- malformed 2xx response ----------------------------------------------------


async def test_post_message_invalid_json_body_is_not_retryable() -> None:
    transport = _RecordingTransport()
    transport.queue(httpx2.Response(200, content=b"not json"))
    http_client = _http_client(transport)

    result = await post_message(
        channel="C1", text="hi", bot_token=SENTINEL_BOT_TOKEN, http_client=http_client
    )

    assert result.ok is False
    assert result.reason_code == "invalid_response"
    assert result.retryable is False
    await http_client.aclose()


async def test_post_message_ok_true_without_ts_is_not_retryable() -> None:
    transport = _RecordingTransport()
    transport.queue(httpx2.Response(200, json={"ok": True, "channel": "C1"}))
    http_client = _http_client(transport)

    result = await post_message(
        channel="C1", text="hi", bot_token=SENTINEL_BOT_TOKEN, http_client=http_client
    )

    assert result.ok is False
    assert result.reason_code == "invalid_response"
    assert result.retryable is False
    await http_client.aclose()


# --- security: token never logged/repr'd, answer body never logged whole -----


async def test_post_message_never_logs_or_reprs_the_bot_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport = _RecordingTransport()
    transport.queue(httpx2.Response(200, json={"ok": False, "error": "not_in_channel"}))
    http_client = _http_client(transport)
    caplog.set_level(logging.DEBUG)

    result = await post_message(
        channel="C1", text="hello", bot_token=SENTINEL_BOT_TOKEN, http_client=http_client
    )

    assert SENTINEL_BOT_TOKEN not in caplog.text
    assert SENTINEL_BOT_TOKEN not in repr(result)
    await http_client.aclose()


async def test_post_message_network_error_never_logs_the_bot_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport = _RecordingTransport()
    transport.queue(httpx2.ConnectError("boom"))
    http_client = _http_client(transport)
    caplog.set_level(logging.DEBUG)

    await post_message(
        channel="C1", text="hi", bot_token=SENTINEL_BOT_TOKEN, http_client=http_client
    )

    assert SENTINEL_BOT_TOKEN not in caplog.text
    await http_client.aclose()


async def test_post_message_never_logs_the_full_answer_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport = _RecordingTransport()
    transport.queue(httpx2.Response(200, json={"ok": False, "error": "not_in_channel"}))
    http_client = _http_client(transport)
    caplog.set_level(logging.DEBUG)

    await post_message(
        channel="C1",
        text=SENTINEL_ANSWER_BODY,
        bot_token=SENTINEL_BOT_TOKEN,
        http_client=http_client,
    )

    assert SENTINEL_ANSWER_BODY not in caplog.text
    await http_client.aclose()


async def test_post_message_sends_bearer_auth_header_and_never_leaks_token_elsewhere() -> None:
    transport = _RecordingTransport()
    transport.queue(httpx2.Response(200, json={"ok": True, "channel": "C1", "ts": "1.1"}))
    http_client = _http_client(transport)

    await post_message(
        channel="C1", text="hi", bot_token=SENTINEL_BOT_TOKEN, http_client=http_client
    )

    sent_request = transport.requests[0]
    assert sent_request.headers["authorization"] == f"Bearer {SENTINEL_BOT_TOKEN}"
    assert SENTINEL_BOT_TOKEN not in str(sent_request.url)
    assert SENTINEL_BOT_TOKEN.encode() not in sent_request.content
    await http_client.aclose()


# --- timeout is explicit, client injection --------------------------------


async def test_resolve_http_client_builds_lazily_with_explicit_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No earlier test in this file ever exercises the None-client path (every
    # other test injects a MockTransport-backed client explicitly) — so this
    # also pins that importing this module alone never creates one.
    assert client_module._default_http_client is None  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(client_module, "_default_http_client", None)  # always reset after

    resolved = client_module._resolve_http_client(None)  # pyright: ignore[reportPrivateUsage]

    assert resolved is client_module._default_http_client  # pyright: ignore[reportPrivateUsage]
    timeout_seconds = client_module._CHAT_POST_MESSAGE_TIMEOUT_SECONDS  # pyright: ignore[reportPrivateUsage]
    assert resolved.timeout.connect == timeout_seconds
    assert resolved.timeout.read == timeout_seconds
    await resolved.aclose()


async def test_resolve_http_client_returns_injected_client_unchanged() -> None:
    transport = _RecordingTransport()
    injected = _http_client(transport)

    assert client_module._resolve_http_client(injected) is injected  # pyright: ignore[reportPrivateUsage]
    await injected.aclose()


# --- AC-SB-006-4 acknowledgement notice --------------------------------------


def test_acknowledgement_message_is_a_non_empty_reusable_constant() -> None:  # AC-SB-006-4
    assert isinstance(ACKNOWLEDGEMENT_MESSAGE, str)
    assert ACKNOWLEDGEMENT_MESSAGE
