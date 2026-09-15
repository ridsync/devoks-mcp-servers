"""Tests for devoks_slackbot.ask (TASK-011).

Traces: REQ-SB-005, AC-SB-005-1, AC-SB-005-2, AC-SB-005-3, AC-SB-005-4,
AC-SB-005-5, CTR-SB-004, EDGE-SB-008, EDGE-SB-009, EDGE-SB-014, EDGE-SB-016,
DSN-SB-002.

No real Anthropic network calls — every ``AsyncAnthropic`` client here is
built with ``http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(...))``
(same technique ``tests/test_client.py`` uses for Slack, and the technique the
workspace handoff notes confirm captures the *exact* request body the SDK
serializes — the strongest way to lock down EDGE-SB-009's invariant).
"""

from __future__ import annotations

import inspect
import json
import logging
from typing import Any, cast

import anthropic
import httpx2
import pytest

from devoks_slackbot import ask as ask_module
from devoks_slackbot.ask import ask_claude
from devoks_slackbot.config import CLAUDE_EFFORT, CLAUDE_MAX_TOKENS, CLAUDE_MODEL

#: Fixture-only literals — never real credentials/content — named to make
#: that unmistakable, same convention as tests/conftest.py's VALID_TEST_*.
SENTINEL_API_KEY = "sk-test-fixture-sentinel-api-key-not-a-real-credential"  # noqa: S105
SENTINEL_MCP_TOKEN = "mcp-test-fixture-sentinel-token-not-a-real-credential"  # noqa: S105
SENTINEL_QUESTION = "SENTINEL-QUESTION-should-never-appear-in-a-log-line"
SENTINEL_ANSWER = "SENTINEL-ANSWER-BODY-should-never-appear-in-a-log-line"
TEST_MCP_SERVER_URL = "https://mcp.example.com/mcp"


class _RecordingTransport:
    """``MockTransport``-backed fake Claude API: queues canned responses, records requests."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self._outcomes: list[httpx2.Response] = []

    def queue(self, outcome: httpx2.Response) -> None:
        self._outcomes.append(outcome)

    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self._outcomes:
            return self._outcomes.pop(0)
        return _success_response()


def _success_response(
    *,
    text_blocks: list[str] | None = None,
    stop_reason: str = "end_turn",
    input_tokens: int = 10,
    output_tokens: int = 5,
    cache_read_input_tokens: int | None = None,
    include_tool_blocks: bool = False,
) -> httpx2.Response:
    blocks: list[dict[str, object]] = []
    texts = text_blocks if text_blocks is not None else ["hello"]
    for i, text in enumerate(texts):
        if include_tool_blocks and i > 0:
            blocks.append(
                {
                    "type": "mcp_tool_use",
                    "id": f"tu_{i}",
                    "name": "search",
                    "input": {},
                    "server_name": "devoks-management",
                }
            )
            blocks.append(
                {
                    "type": "mcp_tool_result",
                    "tool_use_id": f"tu_{i}",
                    "is_error": False,
                    "content": [{"type": "text", "text": "result"}],
                }
            )
        blocks.append({"type": "text", "text": text})
    return httpx2.Response(
        200,
        json={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": CLAUDE_MODEL,
            "content": blocks,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read_input_tokens,
            },
        },
    )


def _error_response(
    status_code: int, *, error_type: str, message: str, details: dict[str, Any] | None = None
) -> httpx2.Response:
    error: dict[str, object] = {"type": error_type, "message": message}
    if details is not None:
        error["details"] = details
    return httpx2.Response(status_code, json={"type": "error", "error": error})


def _client(transport: _RecordingTransport, *, max_retries: int = 0) -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(
        api_key=SENTINEL_API_KEY,
        max_retries=max_retries,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(transport.handle)),
    )


def _sent_body(transport: _RecordingTransport, index: int = 0) -> dict[str, Any]:
    body: Any = json.loads(transport.requests[index].content)
    assert isinstance(body, dict)
    return cast(dict[str, Any], body)


# --- AC-SB-005-1 / EDGE-SB-009: the most important test in this file ---------


async def test_ask_claude_sends_mcp_servers_and_tools_paired_with_matching_name() -> None:
    transport = _RecordingTransport()
    transport.queue(_success_response())
    client = _client(transport)

    await ask_claude(
        question="what is the auth flow?",
        mcp_server_url=TEST_MCP_SERVER_URL,
        authorization_token=SENTINEL_MCP_TOKEN,
        client=client,
    )

    body = _sent_body(transport)
    assert "mcp_servers" in body
    assert "tools" in body
    mcp_server_name = body["mcp_servers"][0]["name"]
    tool_server_name = body["tools"][0]["mcp_server_name"]
    assert mcp_server_name == tool_server_name
    assert body["tools"][0]["type"] == "mcp_toolset"
    assert body["mcp_servers"][0]["type"] == "url"
    assert body["mcp_servers"][0]["url"] == TEST_MCP_SERVER_URL
    await client.close()


async def test_ask_claude_sends_callers_own_authorization_token() -> None:
    # AC-SB-004-1: the token used is the one passed in for that specific call.
    transport = _RecordingTransport()
    transport.queue(_success_response())
    client = _client(transport)

    await ask_claude(
        question="q",
        mcp_server_url=TEST_MCP_SERVER_URL,
        authorization_token=SENTINEL_MCP_TOKEN,
        client=client,
    )

    body = _sent_body(transport)
    assert body["mcp_servers"][0]["authorization_token"] == SENTINEL_MCP_TOKEN
    await client.close()


# --- AC-SB-005-5: fixed values, not overridable -------------------------------


async def test_ask_claude_fixes_model_max_tokens_effort_from_config() -> None:
    transport = _RecordingTransport()
    transport.queue(_success_response())
    client = _client(transport)

    await ask_claude(
        question="q",
        mcp_server_url=TEST_MCP_SERVER_URL,
        authorization_token=SENTINEL_MCP_TOKEN,
        client=client,
    )

    body = _sent_body(transport)
    assert body["model"] == CLAUDE_MODEL
    assert body["max_tokens"] == CLAUDE_MAX_TOKENS
    assert body["output_config"]["effort"] == CLAUDE_EFFORT
    await client.close()


def test_ask_claude_has_no_parameter_that_could_override_fixed_values() -> None:
    params = inspect.signature(ask_claude).parameters
    assert set(params) == {"question", "mcp_server_url", "authorization_token", "client"}


# --- CTR-SB-004: forbidden parameters never sent ------------------------------


async def test_ask_claude_never_sends_budget_tokens_or_prefill() -> None:
    transport = _RecordingTransport()
    transport.queue(_success_response())
    client = _client(transport)

    await ask_claude(
        question="q",
        mcp_server_url=TEST_MCP_SERVER_URL,
        authorization_token=SENTINEL_MCP_TOKEN,
        client=client,
    )

    body = _sent_body(transport)
    assert "budget_tokens" not in body
    assert "thinking" not in json.dumps(body)
    for message in body["messages"]:
        assert message["role"] != "assistant"
    await client.close()


async def test_ask_claude_never_sends_thinking() -> None:
    transport = _RecordingTransport()
    transport.queue(_success_response())
    client = _client(transport)

    await ask_claude(
        question="q",
        mcp_server_url=TEST_MCP_SERVER_URL,
        authorization_token=SENTINEL_MCP_TOKEN,
        client=client,
    )

    body = _sent_body(transport)
    assert "thinking" not in body
    await client.close()


# --- EDGE-SB-014: no thread history -------------------------------------------


async def test_ask_claude_sends_single_question_message_no_thread_history() -> None:
    transport = _RecordingTransport()
    transport.queue(_success_response())
    client = _client(transport)

    await ask_claude(
        question="only this question",
        mcp_server_url=TEST_MCP_SERVER_URL,
        authorization_token=SENTINEL_MCP_TOKEN,
        client=client,
    )

    body = _sent_body(transport)
    assert len(body["messages"]) == 1
    assert body["messages"][0]["role"] == "user"
    assert body["messages"][0]["content"] == "only this question"
    await client.close()


# --- AC-SB-005-4: refusal --------------------------------------------------


async def test_ask_claude_refusal_is_reported_and_content_not_used() -> None:
    transport = _RecordingTransport()
    transport.queue(_success_response(text_blocks=["should not be shown"], stop_reason="refusal"))
    client = _client(transport)

    result = await ask_claude(
        question="q",
        mcp_server_url=TEST_MCP_SERVER_URL,
        authorization_token=SENTINEL_MCP_TOKEN,
        client=client,
    )

    assert result.outcome == "refused"
    assert result.stop_reason == "refusal"
    assert result.answer is None
    assert result.client_message
    await client.close()


# --- EDGE-SB-008: error classification ----------------------------------------


async def test_ask_claude_classifies_400_spend_limit_as_non_retryable() -> None:
    transport = _RecordingTransport()
    transport.queue(
        _error_response(
            400,
            error_type="invalid_request_error",
            message="You have reached your specified API usage limits.",
        )
    )
    client = _client(transport)

    result = await ask_claude(
        question="q",
        mcp_server_url=TEST_MCP_SERVER_URL,
        authorization_token=SENTINEL_MCP_TOKEN,
        client=client,
    )

    assert result.outcome == "error"
    assert result.reason_code == "spend_limit_exceeded"
    assert result.retryable is False
    assert len(transport.requests) == 1  # no owned retry loop on top of the SDK's
    await client.close()


async def test_ask_claude_classifies_400_credit_exhausted_edge_sb_008_edge_sb_017() -> None:
    # EDGE-SB-008 / EDGE-SB-017: exact body reproduced by hand against the
    # real API during a production incident (see ask.py's module docstring).
    transport = _RecordingTransport()
    transport.queue(
        _error_response(
            400,
            error_type="invalid_request_error",
            message=(
                "Your credit balance is too low to access the Anthropic API. "
                "Please go to Plans & Billing to upgrade or purchase credits."
            ),
        )
    )
    client = _client(transport)

    result = await ask_claude(
        question="q",
        mcp_server_url=TEST_MCP_SERVER_URL,
        authorization_token=SENTINEL_MCP_TOKEN,
        client=client,
    )

    assert result.outcome == "error"
    assert result.reason_code == "credit_exhausted"
    assert result.reason_code != "spend_limit_exceeded"  # distinct operator action
    assert result.reason_code != "api_error"  # must not be lumped into the generic bucket
    assert result.retryable is False
    assert len(transport.requests) == 1  # no owned retry loop on top of the SDK's
    await client.close()


async def test_ask_claude_classifies_429_enforced_spend_limit_as_non_retryable() -> None:
    transport = _RecordingTransport()
    transport.queue(
        _error_response(
            429,
            error_type="rate_limit_error",
            message="rate limited",
            details={"error_code": "enforced_spend_limit_reached"},
        )
    )
    client = _client(transport)

    result = await ask_claude(
        question="q",
        mcp_server_url=TEST_MCP_SERVER_URL,
        authorization_token=SENTINEL_MCP_TOKEN,
        client=client,
    )

    assert result.outcome == "error"
    assert result.reason_code == "spend_limit_exceeded"
    assert result.retryable is False
    await client.close()


async def test_ask_claude_classifies_general_429_as_retryable() -> None:
    transport = _RecordingTransport()
    transport.queue(_error_response(429, error_type="rate_limit_error", message="rate limited"))
    client = _client(transport)

    result = await ask_claude(
        question="q",
        mcp_server_url=TEST_MCP_SERVER_URL,
        authorization_token=SENTINEL_MCP_TOKEN,
        client=client,
    )

    assert result.outcome == "error"
    assert result.reason_code == "rate_limited"
    assert result.retryable is True
    await client.close()


async def test_ask_claude_classifies_5xx_as_retryable() -> None:
    transport = _RecordingTransport()
    transport.queue(_error_response(500, error_type="api_error", message="internal server error"))
    client = _client(transport)

    result = await ask_claude(
        question="q",
        mcp_server_url=TEST_MCP_SERVER_URL,
        authorization_token=SENTINEL_MCP_TOKEN,
        client=client,
    )

    assert result.outcome == "error"
    assert result.reason_code == "server_error"
    assert result.retryable is True
    await client.close()


async def test_ask_claude_classifies_timeout_as_retryable() -> None:
    async def timeout_handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("simulated timeout")

    client = anthropic.AsyncAnthropic(
        api_key=SENTINEL_API_KEY,
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(timeout_handler)),
    )

    result = await ask_claude(
        question="q",
        mcp_server_url=TEST_MCP_SERVER_URL,
        authorization_token=SENTINEL_MCP_TOKEN,
        client=client,
    )

    assert result.outcome == "error"
    assert result.reason_code == "timeout"
    assert result.retryable is True
    await client.close()


async def test_ask_claude_classifies_network_error_as_retryable_and_distinct_from_timeout() -> None:
    async def connect_error_handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("simulated connection failure")

    client = anthropic.AsyncAnthropic(
        api_key=SENTINEL_API_KEY,
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(connect_error_handler)),
    )

    result = await ask_claude(
        question="q",
        mcp_server_url=TEST_MCP_SERVER_URL,
        authorization_token=SENTINEL_MCP_TOKEN,
        client=client,
    )

    assert result.outcome == "error"
    assert result.reason_code == "network_error"
    assert result.reason_code != "timeout"
    assert result.retryable is True
    await client.close()


async def test_ask_claude_classifies_other_4xx_as_non_retryable() -> None:
    transport = _RecordingTransport()
    transport.queue(
        _error_response(401, error_type="authentication_error", message="invalid api key")
    )
    client = _client(transport)

    result = await ask_claude(
        question="q",
        mcp_server_url=TEST_MCP_SERVER_URL,
        authorization_token=SENTINEL_MCP_TOKEN,
        client=client,
    )

    assert result.outcome == "error"
    assert result.reason_code == "api_error"
    assert result.retryable is False
    await client.close()


async def test_ask_claude_400_with_changed_credit_wording_falls_back_safely_edge_sb_008() -> None:
    # EDGE-SB-008: if Anthropic ever rewords the credit-exhaustion message,
    # substring matching must not raise — it should fall back to the
    # conservative generic 400 classification, not crash or silently
    # misclassify as retryable.
    transport = _RecordingTransport()
    transport.queue(
        _error_response(
            400,
            error_type="invalid_request_error",
            message="Your balance is insufficient to complete this request.",
        )
    )
    client = _client(transport)

    result = await ask_claude(
        question="q",
        mcp_server_url=TEST_MCP_SERVER_URL,
        authorization_token=SENTINEL_MCP_TOKEN,
        client=client,
    )

    assert result.outcome == "error"
    assert result.reason_code == "api_error"
    assert result.retryable is False
    await client.close()


async def test_ask_claude_three_400_reasons_have_distinct_client_messages_edge_sb_008() -> None:
    # EDGE-SB-008 / AC-SB-005-3: the user needs to know *what to expect*
    # (retry later vs. contact admin about a limit vs. contact admin about
    # billing) — the three reason codes must not collapse to the same text.
    async def _result_for(message: str) -> Any:
        transport = _RecordingTransport()
        transport.queue(_error_response(400, error_type="invalid_request_error", message=message))
        client = _client(transport)
        result = await ask_claude(
            question="q",
            mcp_server_url=TEST_MCP_SERVER_URL,
            authorization_token=SENTINEL_MCP_TOKEN,
            client=client,
        )
        await client.close()
        return result

    spend_limit_result = await _result_for("You have reached your specified API usage limits.")
    credit_result = await _result_for("Your credit balance is too low to access the Anthropic API.")
    generic_result = await _result_for("Some other validation error.")

    messages = {
        spend_limit_result.client_message,
        credit_result.client_message,
        generic_result.client_message,
    }
    assert len(messages) == 3  # all three distinct
    assert spend_limit_result.reason_code == "spend_limit_exceeded"
    assert credit_result.reason_code == "credit_exhausted"
    assert generic_result.reason_code == "api_error"


# --- security: no secrets/content in logs -------------------------------------


async def test_ask_claude_never_logs_api_key_mcp_token_question_or_answer_on_success(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Scoped to this module's own logger only — the Anthropic SDK's internal
    # ``anthropic._base_client`` logger emits the full request body (including
    # the token) at DEBUG on its own; that is the SDK's behavior, not
    # something this module's own log statements do, and out of scope for
    # what AC-SB-005-3 asks this module to guarantee.
    transport = _RecordingTransport()
    transport.queue(_success_response(text_blocks=[SENTINEL_ANSWER]))
    client = _client(transport)
    caplog.set_level(logging.DEBUG, logger="devoks_slackbot.ask")

    result = await ask_claude(
        question=SENTINEL_QUESTION,
        mcp_server_url=TEST_MCP_SERVER_URL,
        authorization_token=SENTINEL_MCP_TOKEN,
        client=client,
    )

    assert result.answer == SENTINEL_ANSWER  # sanity: the answer *is* extracted...
    assert SENTINEL_API_KEY not in caplog.text
    assert SENTINEL_MCP_TOKEN not in caplog.text
    assert SENTINEL_QUESTION not in caplog.text
    assert SENTINEL_ANSWER not in caplog.text  # ...but never logged (AC-SB-005-3)
    assert SENTINEL_API_KEY not in repr(result)
    assert SENTINEL_MCP_TOKEN not in repr(result)
    await client.close()


async def test_ask_claude_never_logs_secrets_or_question_on_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # See test_ask_claude_never_logs_api_key_mcp_token_question_or_answer_on_success
    # for why this is scoped to this module's own logger.
    transport = _RecordingTransport()
    transport.queue(_error_response(500, error_type="api_error", message="boom"))
    client = _client(transport)
    caplog.set_level(logging.DEBUG, logger="devoks_slackbot.ask")

    result = await ask_claude(
        question=SENTINEL_QUESTION,
        mcp_server_url=TEST_MCP_SERVER_URL,
        authorization_token=SENTINEL_MCP_TOKEN,
        client=client,
    )

    assert result.outcome == "error"
    assert SENTINEL_API_KEY not in caplog.text
    assert SENTINEL_MCP_TOKEN not in caplog.text
    assert SENTINEL_QUESTION not in caplog.text
    assert result.detail is not None
    assert SENTINEL_API_KEY not in result.detail
    assert SENTINEL_MCP_TOKEN not in result.detail
    await client.close()


async def test_ask_claude_credit_exhausted_never_logs_secrets_or_question_edge_sb_008(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # EDGE-SB-008 / AC-SB-005-3: the new credit_exhausted path must uphold
    # the same no-secrets guarantee as every other error path.
    transport = _RecordingTransport()
    transport.queue(
        _error_response(
            400,
            error_type="invalid_request_error",
            message=(
                "Your credit balance is too low to access the Anthropic API. "
                "Please go to Plans & Billing to upgrade or purchase credits."
            ),
        )
    )
    client = _client(transport)
    caplog.set_level(logging.DEBUG, logger="devoks_slackbot.ask")

    result = await ask_claude(
        question=SENTINEL_QUESTION,
        mcp_server_url=TEST_MCP_SERVER_URL,
        authorization_token=SENTINEL_MCP_TOKEN,
        client=client,
    )

    assert result.reason_code == "credit_exhausted"
    assert SENTINEL_API_KEY not in caplog.text
    assert SENTINEL_MCP_TOKEN not in caplog.text
    assert SENTINEL_QUESTION not in caplog.text
    assert result.detail is not None
    assert SENTINEL_API_KEY not in result.detail
    assert SENTINEL_MCP_TOKEN not in result.detail
    assert result.client_message is not None
    assert SENTINEL_API_KEY not in result.client_message
    assert SENTINEL_MCP_TOKEN not in result.client_message
    assert SENTINEL_API_KEY not in repr(result)
    assert SENTINEL_MCP_TOKEN not in repr(result)
    await client.close()


async def test_ask_claude_error_client_message_carries_no_stack_trace_or_secrets() -> None:
    # AC-SB-005-3
    transport = _RecordingTransport()
    transport.queue(_error_response(503, error_type="overloaded_error", message="overloaded"))
    client = _client(transport)

    result = await ask_claude(
        question="q",
        mcp_server_url=TEST_MCP_SERVER_URL,
        authorization_token=SENTINEL_MCP_TOKEN,
        client=client,
    )

    assert result.client_message is not None
    assert "Traceback" not in result.client_message
    assert SENTINEL_API_KEY not in result.client_message
    assert SENTINEL_MCP_TOKEN not in result.client_message
    assert result.retryable is not None  # always tells the caller retry-ability
    await client.close()


# --- usage extraction ----------------------------------------------------------


async def test_ask_claude_extracts_usage_as_plain_dict_with_three_fields() -> None:
    transport = _RecordingTransport()
    transport.queue(_success_response(input_tokens=42, output_tokens=17, cache_read_input_tokens=3))
    client = _client(transport)

    result = await ask_claude(
        question="q",
        mcp_server_url=TEST_MCP_SERVER_URL,
        authorization_token=SENTINEL_MCP_TOKEN,
        client=client,
    )

    assert result.usage == {
        "input_tokens": 42,
        "output_tokens": 17,
        "cache_read_input_tokens": 3,
    }
    assert type(result.usage) is dict
    await client.close()


async def test_ask_claude_usage_cache_read_input_tokens_may_be_none() -> None:
    transport = _RecordingTransport()
    transport.queue(_success_response(cache_read_input_tokens=None))
    client = _client(transport)

    result = await ask_claude(
        question="q",
        mcp_server_url=TEST_MCP_SERVER_URL,
        authorization_token=SENTINEL_MCP_TOKEN,
        client=client,
    )

    assert result.usage is not None
    assert result.usage["cache_read_input_tokens"] is None
    await client.close()


# --- answer text extraction: interleaved text/tool blocks ---------------------


async def test_ask_claude_joins_multiple_text_blocks_skipping_tool_blocks() -> None:
    transport = _RecordingTransport()
    transport.queue(
        _success_response(
            text_blocks=["Let me check. ", "Here is the answer."],
            include_tool_blocks=True,
        )
    )
    client = _client(transport)

    result = await ask_claude(
        question="q",
        mcp_server_url=TEST_MCP_SERVER_URL,
        authorization_token=SENTINEL_MCP_TOKEN,
        client=client,
    )

    assert result.outcome == "answered"
    assert result.answer == "Let me check. Here is the answer."
    await client.close()


async def test_ask_claude_answered_stop_reason_is_reported() -> None:
    transport = _RecordingTransport()
    transport.queue(_success_response(stop_reason="end_turn"))
    client = _client(transport)

    result = await ask_claude(
        question="q",
        mcp_server_url=TEST_MCP_SERVER_URL,
        authorization_token=SENTINEL_MCP_TOKEN,
        client=client,
    )

    assert result.stop_reason == "end_turn"
    assert result.reason_code is None
    assert result.retryable is None
    await client.close()


# --- module-internal invariant: shared name defined once ---------------------


def test_mcp_server_name_constant_is_used_for_both_mcp_servers_and_tools() -> None:
    mcp_servers, tools = ask_module._build_mcp_request(  # pyright: ignore[reportPrivateUsage]
        mcp_server_url=TEST_MCP_SERVER_URL, authorization_token=SENTINEL_MCP_TOKEN
    )
    assert mcp_servers[0]["name"] == tools[0]["mcp_server_name"]
    assert mcp_servers[0]["name"] == ask_module._MCP_SERVER_NAME  # pyright: ignore[reportPrivateUsage]
