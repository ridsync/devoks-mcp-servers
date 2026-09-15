"""Tests for devoks_slackbot.worker (TASK-014).

Traces: AC-SB-006-4, CTR-SB-009, EDGE-SB-005, EDGE-SB-006, EDGE-SB-013, plus
the closely-related EDGE-SB-015 (coalescing), EDGE-SB-020 (post-failure
classification), and AC-SB-006-2 (truncation) this endpoint also wires up.

Every HTTP-level assertion goes through the ASGI transport
(``httpx2.ASGITransport``), same rationale as ``test_handler.py``.
``dynamodb_client`` uses real ``moto`` DynamoDB semantics (mirrors
``test_idempotency.py``/``test_handler.py``). No real AWS/Slack/Anthropic
call — Slack and Claude are both served by ``httpx2.MockTransport``-backed
fakes (same technique ``test_client.py``/``test_ask.py`` use).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Generator
from typing import TYPE_CHECKING, Any

import anthropic
import boto3
import httpx2
import pytest
from moto import mock_aws
from starlette.applications import Starlette

from devoks_slackbot import worker as worker_module
from devoks_slackbot.config import CLAUDE_MODEL, WorkerSettings
from devoks_slackbot.idempotency import (
    PARTITION_KEY_ATTR,
    TTL_ATTRIBUTE,
    InflightClaimOutcome,
    claim_inflight_query,
    is_event_completed,
    mark_event_completed,
)
from devoks_slackbot.slack.client import ACKNOWLEDGEMENT_MESSAGE
from devoks_slackbot.slack.format import TRUNCATION_NOTICE
from devoks_slackbot.worker import EVENTS_PATH, HEALTHZ_PATH, create_app

from .conftest import VALID_TEST_IDEMPOTENCY_TABLE, make_worker_settings

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient

_BASE_URL = "https://worker.example.com"

#: Fixture-only literals — never real credentials/content, same naming
#: convention as tests/conftest.py's VALID_TEST_*/test_ask.py's SENTINEL_*.
SENTINEL_ANTHROPIC_API_KEY = "sk-test-fixture-sentinel-api-key-not-a-real-credential"  # noqa: S105
SENTINEL_MCP_TOKEN = "mcp-test-fixture-sentinel-token-not-a-real-credential"  # noqa: S105
SENTINEL_BOT_TOKEN = "xoxb-test-fixture-sentinel-bot-token-not-a-real-credential"  # noqa: S105
SENTINEL_SIGNING_SECRET = "sentinel-signing-secret-DO-NOT-LEAK-9f3a"
SENTINEL_QUESTION_WORD = "SENTINEL-QUESTION-should-never-appear-in-a-log-line"
SENTINEL_ANSWER_WORD = "SENTINEL-ANSWER-BODY-should-never-appear-in-a-log-line"

_REGISTERED_USER_ID = "U01ABCDEF"
_DEFAULT_TS = "1700000000.000001"


# --- fixtures ------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fake_aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")


@pytest.fixture(autouse=True)
def _restore_noisy_logger_levels() -> Generator[None]:  # pyright: ignore[reportUnusedFunction]
    # _configure_logging mutates global logger objects (logging.getLogger
    # returns the same singleton every call) — restore so this test module
    # never leaks a level change into unrelated tests run afterward.
    originals = {name: logging.getLogger(name).level for name in ("anthropic", "httpx2")}
    yield
    for name, level in originals.items():
        logging.getLogger(name).setLevel(level)


@pytest.fixture
def dynamodb_client() -> Generator[DynamoDBClient]:
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")  # pyright: ignore[reportUnknownMemberType]
        client.create_table(
            TableName=VALID_TEST_IDEMPOTENCY_TABLE,
            KeySchema=[{"AttributeName": PARTITION_KEY_ATTR, "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": PARTITION_KEY_ATTR, "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.update_time_to_live(
            TableName=VALID_TEST_IDEMPOTENCY_TABLE,
            TimeToLiveSpecification={"Enabled": True, "AttributeName": TTL_ATTRIBUTE},
        )
        yield client


class _RecordingTransport:
    """``MockTransport``-backed fake HTTP peer: queues Response/Exception, records requests.

    Shared shape for both the Slack and Claude fakes in this file — mirrors
    ``test_client.py``'s ``_RecordingTransport`` (which also supports queuing
    an exception, needed for the timeout/network-error cases here).
    """

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


def _slack_http_client(transport: _RecordingTransport) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(transport.handle))


def _claude_client(
    transport: _RecordingTransport, *, max_retries: int = 0
) -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(
        api_key=SENTINEL_ANTHROPIC_API_KEY,
        max_retries=max_retries,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(transport.handle)),
    )


def _claude_success_response(*, text: str = "the answer") -> httpx2.Response:
    return httpx2.Response(
        200,
        json={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": CLAUDE_MODEL,
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": None},
        },
    )


def _claude_refusal_response() -> httpx2.Response:
    return httpx2.Response(
        200,
        json={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": CLAUDE_MODEL,
            "content": [],
            "stop_reason": "refusal",
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 0, "cache_read_input_tokens": None},
        },
    )


def _app_mention_payload(
    *,
    event_id: str | None = "Ev0DEFAULT",
    user: str | None = _REGISTERED_USER_ID,
    text: str = "<@U0TESTBOT01> what is the auth flow?",
    channel: str | None = "C123ABC456",
    ts: str = _DEFAULT_TS,
    thread_ts: str | None = None,
) -> dict[str, Any]:
    """Slack's own documented ``app_mention`` payload shape (see test_events.py)."""
    event: dict[str, Any] = {"type": "app_mention", "text": text, "ts": ts, "event_ts": ts}
    if channel is not None:
        event["channel"] = channel
    if user is not None:
        event["user"] = user
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
                "user_id": "U999AUTHOR",
                "is_bot": False,
                "is_enterprise_install": False,
            }
        ],
        "event_time": 123456789,
    }
    if event_id is not None:
        payload["event_id"] = event_id
    return payload


async def _post_event(app: Starlette, payload: dict[str, Any]) -> httpx2.Response:
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url=_BASE_URL) as client:
        return await client.post(EVENTS_PATH, content=json.dumps(payload).encode())


async def _get(app: Starlette, path: str) -> httpx2.Response:
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url=_BASE_URL) as client:
        return await client.get(path)


def _settings(**overrides: Any) -> WorkerSettings:
    overrides.setdefault("user_token_map", {_REGISTERED_USER_ID: SENTINEL_MCP_TOKEN})
    overrides.setdefault("bot_token", SENTINEL_BOT_TOKEN)
    overrides.setdefault("anthropic_api_key", SENTINEL_ANTHROPIC_API_KEY)
    overrides.setdefault("signing_secret", SENTINEL_SIGNING_SECRET)
    return make_worker_settings(**overrides)


# --- /healthz -----------------------------------------------------------


async def test_healthz_returns_200() -> None:
    app = create_app(_settings())

    response = await _get(app, HEALTHZ_PATH)

    assert response.status_code == 200


# --- normal flow: ack -> claude -> length policy -> post -> complete -> observe -----


async def test_normal_flow_posts_ack_then_answer_and_records_completion_and_observation(
    dynamodb_client: DynamoDBClient, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = _settings()
    claude_transport = _RecordingTransport()
    claude_transport.queue(_claude_success_response(text="the auth flow uses OAuth 2.1"))
    slack_transport = _RecordingTransport()
    app = create_app(
        settings,
        anthropic_client=_claude_client(claude_transport),
        idempotency_client=dynamodb_client,
        http_client=_slack_http_client(slack_transport),
    )
    payload = _app_mention_payload(event_id="Ev0NORMAL")

    response = await _post_event(app, payload)

    assert response.status_code == 200
    assert len(claude_transport.requests) == 1
    assert len(slack_transport.requests) == 2
    ack_body = json.loads(slack_transport.requests[0].content)
    assert ack_body["text"] == ACKNOWLEDGEMENT_MESSAGE
    answer_body = json.loads(slack_transport.requests[1].content)
    assert answer_body["text"] == "the auth flow uses OAuth 2.1"
    assert answer_body["thread_ts"] == _DEFAULT_TS
    assert (
        is_event_completed(
            "Ev0NORMAL", table_name=settings.idempotency_table, client=dynamodb_client
        )
        is True
    )

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "slack_query"
    assert record["outcome"] == "ok"
    assert record["slack_user_id"] == _REGISTERED_USER_ID
    assert record["client_id"] == _REGISTERED_USER_ID
    assert "usage" in record


# --- EDGE-SB-005: already-completed event_id -> zero posts -------------------


async def test_already_completed_event_id_posts_zero_times_edge_sb_005(
    dynamodb_client: DynamoDBClient,
) -> None:
    settings = _settings()
    mark_event_completed("Ev0DONE", table_name=settings.idempotency_table, client=dynamodb_client)
    claude_transport = _RecordingTransport()
    slack_transport = _RecordingTransport()
    app = create_app(
        settings,
        anthropic_client=_claude_client(claude_transport),
        idempotency_client=dynamodb_client,
        http_client=_slack_http_client(slack_transport),
    )
    payload = _app_mention_payload(event_id="Ev0DONE")

    response = await _post_event(app, payload)

    assert response.status_code == 200
    assert slack_transport.requests == []
    assert claude_transport.requests == []


# --- EDGE-SB-006 / AC-SB-004-2: unregistered/unidentified user ---------------


async def test_unregistered_user_denies_without_calling_claude_edge_sb_006(
    dynamodb_client: DynamoDBClient,
) -> None:
    settings = _settings(user_token_map={})  # nobody registered
    claude_transport = _RecordingTransport()
    slack_transport = _RecordingTransport()
    app = create_app(
        settings,
        anthropic_client=_claude_client(claude_transport),
        idempotency_client=dynamodb_client,
        http_client=_slack_http_client(slack_transport),
    )
    payload = _app_mention_payload(event_id="Ev0DENY", user="U_UNKNOWN99")

    response = await _post_event(app, payload)

    assert response.status_code == 200
    assert claude_transport.requests == []
    assert len(slack_transport.requests) == 1
    body = json.loads(slack_transport.requests[0].content)
    assert body["text"] != ACKNOWLEDGEMENT_MESSAGE
    assert "등록" in body["text"]


async def test_unidentified_user_denies_without_calling_claude_edge_sb_006(
    dynamodb_client: DynamoDBClient,
) -> None:
    settings = _settings()
    claude_transport = _RecordingTransport()
    slack_transport = _RecordingTransport()
    app = create_app(
        settings,
        anthropic_client=_claude_client(claude_transport),
        idempotency_client=dynamodb_client,
        http_client=_slack_http_client(slack_transport),
    )
    payload = _app_mention_payload(event_id="Ev0NOUSER", user=None)

    response = await _post_event(app, payload)

    assert response.status_code == 200
    assert claude_transport.requests == []
    assert len(slack_transport.requests) == 1


# --- EDGE-SB-015: in-flight coalescing ----------------------------------------


async def test_inflight_coalescing_acknowledges_only_no_claude_call_edge_sb_015(
    dynamodb_client: DynamoDBClient,
) -> None:
    settings = _settings()
    claim_inflight_query(
        _REGISTERED_USER_ID,
        _DEFAULT_TS,
        table_name=settings.idempotency_table,
        client=dynamodb_client,
    )
    claude_transport = _RecordingTransport()
    slack_transport = _RecordingTransport()
    app = create_app(
        settings,
        anthropic_client=_claude_client(claude_transport),
        idempotency_client=dynamodb_client,
        http_client=_slack_http_client(slack_transport),
    )
    payload = _app_mention_payload(event_id="Ev0COALESCE", ts=_DEFAULT_TS)

    response = await _post_event(app, payload)

    assert response.status_code == 200
    assert claude_transport.requests == []
    assert len(slack_transport.requests) == 1
    body = json.loads(slack_transport.requests[0].content)
    assert body["text"] == ACKNOWLEDGEMENT_MESSAGE


# --- code review Medium #1: store_error must not be misreported as ----------
# --- identifiers_missing (user_id/thread_ts are present and fine; the store
# --- didn't respond) -- EDGE-SB-015


def test_claim_inflight_or_fail_open_returns_store_error_reason_edge_sb_015(
    dynamodb_client: DynamoDBClient,
) -> None:
    """``_claim_inflight_or_fail_open`` must report ``reason="store_error"``,
    never ``"identifiers_missing"`` -- the review's Medium #1: a real
    DynamoDB outage must never be misclassified as "coalescing wasn't
    evaluable" (``user_id``/``thread_ts`` here are both present and
    well-formed; only the store call itself failed)."""
    settings = _settings(idempotency_table="table-does-not-exist")

    outcome = worker_module._claim_inflight_or_fail_open(  # pyright: ignore[reportPrivateUsage]
        _REGISTERED_USER_ID, _DEFAULT_TS, settings=settings, client=dynamodb_client
    )

    assert outcome == InflightClaimOutcome(claimed=True, reason="store_error")


def test_claim_inflight_or_fail_open_reason_values_unchanged_for_non_store_error_paths_edge_sb_015(
    dynamodb_client: DynamoDBClient,
) -> None:
    """Regression: this fix only changes the store-error branch's ``reason`` --
    ``"claimed"``, ``"in_progress"``, and ``"identifiers_missing"`` must be unchanged."""
    settings = _settings()

    fresh = worker_module._claim_inflight_or_fail_open(  # pyright: ignore[reportPrivateUsage]
        _REGISTERED_USER_ID, _DEFAULT_TS, settings=settings, client=dynamodb_client
    )
    assert fresh == InflightClaimOutcome(claimed=True, reason="claimed")

    blocked = worker_module._claim_inflight_or_fail_open(  # pyright: ignore[reportPrivateUsage]
        _REGISTERED_USER_ID, _DEFAULT_TS, settings=settings, client=dynamodb_client
    )
    assert blocked == InflightClaimOutcome(claimed=False, reason="in_progress")

    missing = worker_module._claim_inflight_or_fail_open(  # pyright: ignore[reportPrivateUsage]
        None, _DEFAULT_TS, settings=settings, client=dynamodb_client
    )
    assert missing == InflightClaimOutcome(claimed=True, reason="identifiers_missing")


# --- release_inflight_query on exception (try/finally) -- the most important -


async def test_release_inflight_called_even_on_unexpected_exception(
    dynamodb_client: DynamoDBClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _raise_unexpected(**_kwargs: Any) -> None:
        raise RuntimeError("boom -- simulated unexpected failure inside ask_claude")

    monkeypatch.setattr(worker_module, "ask_claude", _raise_unexpected)
    settings = _settings()
    slack_transport = _RecordingTransport()
    app = create_app(
        settings,
        anthropic_client=_claude_client(_RecordingTransport()),
        idempotency_client=dynamodb_client,
        http_client=_slack_http_client(slack_transport),
    )
    payload = _app_mention_payload(event_id="Ev0CRASH", ts=_DEFAULT_TS)

    response = await _post_event(app, payload)

    assert response.status_code == 200
    # ack + generic failure notice, user is not left silent
    assert len(slack_transport.requests) == 2
    # the in-flight lock MUST be released -- a fresh claim for the same pair
    # must succeed; if release_inflight_query never ran, this would return
    # claimed=False and the user would be locked out for
    # INFLIGHT_TTL_SECONDS_DEFAULT (360s).
    outcome = claim_inflight_query(
        _REGISTERED_USER_ID,
        _DEFAULT_TS,
        table_name=settings.idempotency_table,
        client=dynamodb_client,
    )
    assert outcome.claimed is True


# --- code review Medium #4: a non-IdempotencyStoreError bug in the finally --
# --- release must not escape as a bare ASGI 500 (it would both erase the ----
# --- error already propagating and leave the in-flight lock held for -------
# --- INFLIGHT_TTL_SECONDS_DEFAULT, EDGE-SB-015) -----------------------------


async def test_release_inflight_unexpected_non_store_exception_does_not_crash_endpoint(
    dynamodb_client: DynamoDBClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def _raise_unexpected(*_args: Any, **_kwargs: Any) -> None:
        raise TypeError("boom -- simulated unexpected bug inside release_inflight_query")

    monkeypatch.setattr(worker_module, "release_inflight_query", _raise_unexpected)
    settings = _settings()
    claude_transport = _RecordingTransport()
    claude_transport.queue(_claude_success_response(text=f"answer with {SENTINEL_ANSWER_WORD}"))
    slack_transport = _RecordingTransport()
    app = create_app(
        settings,
        anthropic_client=_claude_client(claude_transport),
        idempotency_client=dynamodb_client,
        http_client=_slack_http_client(slack_transport),
    )
    payload = _app_mention_payload(
        event_id="Ev0RELEASEBUG",
        text=f"<@U0TESTBOT01> {SENTINEL_QUESTION_WORD}",
    )

    with caplog.at_level(logging.ERROR, logger="devoks_slackbot.worker"):
        # Before the fix, a bare `TypeError` here would replace whatever
        # (there was nothing) was propagating from the `try`, escape
        # `_process_event`'s own `finally`, and surface as a raised
        # exception through httpx2's ASGITransport (raise_app_exceptions=True
        # by default) rather than a normal 200 response.
        response = await _post_event(app, payload)

    assert response.status_code == 200
    # the answer itself still reached the user even though the cleanup call
    # afterward misbehaved.
    assert len(slack_transport.requests) == 2
    # the failure was logged, not silently discarded.
    assert "releasing in-flight lock failed unexpectedly" in caplog.text
    # no secret/question/answer text leaked via this new broad-except path.
    assert SENTINEL_QUESTION_WORD not in caplog.text
    assert SENTINEL_ANSWER_WORD not in caplog.text
    assert SENTINEL_MCP_TOKEN not in caplog.text
    assert SENTINEL_BOT_TOKEN not in caplog.text
    assert SENTINEL_ANTHROPIC_API_KEY not in caplog.text


async def test_mark_completed_unexpected_non_store_exception_does_not_crash_endpoint(
    dynamodb_client: DynamoDBClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``_safe_mark_completed`` has the same "cleanup call reached from a
    ``finally``-adjacent path with no outer handler left" shape as
    ``_release_inflight_or_log`` (its second call site is
    ``_process_event``'s own ``except Exception as exc:`` branch, which has
    no further ``try`` around it) -- see that function's own docstring."""

    def _raise_unexpected(*_args: Any, **_kwargs: Any) -> None:
        raise TypeError("boom -- simulated unexpected bug inside mark_event_completed")

    monkeypatch.setattr(worker_module, "mark_event_completed", _raise_unexpected)
    settings = _settings()
    claude_transport = _RecordingTransport()
    claude_transport.queue(_claude_success_response())
    slack_transport = _RecordingTransport()
    app = create_app(
        settings,
        anthropic_client=_claude_client(claude_transport),
        idempotency_client=dynamodb_client,
        http_client=_slack_http_client(slack_transport),
    )
    payload = _app_mention_payload(event_id="Ev0MARKBUG")

    with caplog.at_level(logging.ERROR, logger="devoks_slackbot.worker"):
        response = await _post_event(app, payload)

    assert response.status_code == 200
    assert "marking event completed failed unexpectedly" in caplog.text
    # the in-flight lock is still released even though marking completion
    # failed unexpectedly -- the *finally* release path must not depend on
    # _safe_mark_completed succeeding.
    outcome = claim_inflight_query(
        _REGISTERED_USER_ID,
        _DEFAULT_TS,
        table_name=settings.idempotency_table,
        client=dynamodb_client,
    )
    assert outcome.claimed is True


# --- EDGE-SB-013 / EDGE-SB-008: Claude error/timeout -> failure notice -------


async def test_claude_timeout_posts_failure_notice_edge_sb_013(
    dynamodb_client: DynamoDBClient,
) -> None:
    async def _timeout_handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("simulated timeout")

    settings = _settings()
    anthropic_client = anthropic.AsyncAnthropic(
        api_key=SENTINEL_ANTHROPIC_API_KEY,
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(_timeout_handler)),
    )
    slack_transport = _RecordingTransport()
    app = create_app(
        settings,
        anthropic_client=anthropic_client,
        idempotency_client=dynamodb_client,
        http_client=_slack_http_client(slack_transport),
    )
    payload = _app_mention_payload(event_id="Ev0TIMEOUT")

    response = await _post_event(app, payload)

    assert response.status_code == 200
    assert len(slack_transport.requests) == 2
    failure_body = json.loads(slack_transport.requests[1].content)
    assert failure_body["text"] != ACKNOWLEDGEMENT_MESSAGE
    assert (
        is_event_completed(
            "Ev0TIMEOUT", table_name=settings.idempotency_table, client=dynamodb_client
        )
        is True
    )


# --- refusal -> decline notice posted -----------------------------------------


async def test_claude_refusal_posts_decline_notice(dynamodb_client: DynamoDBClient) -> None:
    settings = _settings()
    claude_transport = _RecordingTransport()
    claude_transport.queue(_claude_refusal_response())
    slack_transport = _RecordingTransport()
    app = create_app(
        settings,
        anthropic_client=_claude_client(claude_transport),
        idempotency_client=dynamodb_client,
        http_client=_slack_http_client(slack_transport),
    )
    payload = _app_mention_payload(event_id="Ev0REFUSE")

    response = await _post_event(app, payload)

    assert response.status_code == 200
    assert len(slack_transport.requests) == 2
    refusal_body = json.loads(slack_transport.requests[1].content)
    assert refusal_body["text"] != ACKNOWLEDGEMENT_MESSAGE


# --- AC-SB-006-2: answer over the limit is truncated with notice -------------


async def test_answer_over_max_chars_is_truncated_with_notice_ac_sb_006_2(
    dynamodb_client: DynamoDBClient,
) -> None:
    settings = _settings(max_response_chars=50)
    claude_transport = _RecordingTransport()
    claude_transport.queue(_claude_success_response(text="A" * 500))
    slack_transport = _RecordingTransport()
    app = create_app(
        settings,
        anthropic_client=_claude_client(claude_transport),
        idempotency_client=dynamodb_client,
        http_client=_slack_http_client(slack_transport),
    )
    payload = _app_mention_payload(event_id="Ev0TRUNCATE")

    response = await _post_event(app, payload)

    assert response.status_code == 200
    posted_text = json.loads(slack_transport.requests[1].content)["text"]
    assert len(posted_text) <= 50
    assert TRUNCATION_NOTICE.strip("_() \n") in posted_text or "생략" in posted_text


# --- EDGE-SB-020 / CTR-SB-008: post failure error classification appears ----
# --- both in the log (EDGE-SB-020, unchanged) and in the observation record's
# --- structured outcome/reason_code (CTR-SB-008, coordinator follow-up) -----


async def test_post_not_in_channel_failure_is_logged_edge_sb_020(
    dynamodb_client: DynamoDBClient,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = _settings()
    claude_transport = _RecordingTransport()
    claude_transport.queue(_claude_success_response())
    slack_transport = _RecordingTransport()
    slack_transport.queue(httpx2.Response(200, json={"ok": True, "channel": "C1", "ts": "1.1"}))
    slack_transport.queue(httpx2.Response(200, json={"ok": False, "error": "not_in_channel"}))
    app = create_app(
        settings,
        anthropic_client=_claude_client(claude_transport),
        idempotency_client=dynamodb_client,
        http_client=_slack_http_client(slack_transport),
    )
    payload = _app_mention_payload(event_id="Ev0NOTINCHANNEL")

    with caplog.at_level(logging.ERROR, logger="devoks_slackbot.worker"):
        response = await _post_event(app, payload)

    assert response.status_code == 200
    # EDGE-SB-020: the free-text operator log still names the reason.
    assert "not_in_channel" in caplog.text

    # CTR-SB-008 (coordinator follow-up): the *structured* observation record
    # must not say outcome="ok" for an answer that never reached the channel
    # -- an outcome-aggregating dashboard would otherwise count this as a
    # success and the only way to discover the outage would be reading logs
    # one line at a time.
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["outcome"] == "error"
    assert record["reason_code"] == "not_in_channel" or record["error_kind"] == "not_in_channel"
    # AC-SB-007-2: the Claude call already happened and already cost money --
    # a delivery failure afterward must not erase that from the record.
    assert record["usage"]["input_tokens"] == 10


async def test_final_post_network_failure_records_outcome_error_ctr_sb_008(
    dynamodb_client: DynamoDBClient, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = _settings()
    claude_transport = _RecordingTransport()
    claude_transport.queue(_claude_success_response())
    slack_transport = _RecordingTransport()
    # ack succeeds, final answer post fails at the transport level (no HTTP
    # response at all -- distinct from a 200 + ok:false body).
    slack_transport.queue(httpx2.Response(200, json={"ok": True, "channel": "C1", "ts": "1.1"}))
    slack_transport.queue(httpx2.ConnectError("simulated network failure"))
    app = create_app(
        settings,
        anthropic_client=_claude_client(claude_transport),
        idempotency_client=dynamodb_client,
        http_client=_slack_http_client(slack_transport),
    )
    payload = _app_mention_payload(event_id="Ev0POSTNETFAIL")

    response = await _post_event(app, payload)

    assert response.status_code == 200
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    record = json.loads(lines[0])
    assert record["outcome"] == "error"
    assert record["reason_code"] == "network_error" or record["error_kind"] == "network_error"


async def test_acknowledgement_only_failure_keeps_outcome_ok_ctr_sb_008(
    dynamodb_client: DynamoDBClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """The final answer still reaches the user -- from their side, this query succeeded."""
    settings = _settings()
    claude_transport = _RecordingTransport()
    claude_transport.queue(_claude_success_response(text="the real answer"))
    slack_transport = _RecordingTransport()
    # acknowledgement post fails ...
    slack_transport.queue(httpx2.Response(200, json={"ok": False, "error": "not_in_channel"}))
    # ... but the final answer post succeeds.
    slack_transport.queue(httpx2.Response(200, json={"ok": True, "channel": "C1", "ts": "2.2"}))
    app = create_app(
        settings,
        anthropic_client=_claude_client(claude_transport),
        idempotency_client=dynamodb_client,
        http_client=_slack_http_client(slack_transport),
    )
    payload = _app_mention_payload(event_id="Ev0ACKFAILONLY")

    response = await _post_event(app, payload)

    assert response.status_code == 200
    assert len(slack_transport.requests) == 2
    answer_body = json.loads(slack_transport.requests[1].content)
    assert answer_body["text"] == "the real answer"

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    record = json.loads(lines[0])
    assert record["outcome"] == "ok"
    assert record["reason_code"] is None


# --- secret exposure -----------------------------------------------------------


async def test_no_secrets_or_raw_question_answer_leak_into_logs(
    dynamodb_client: DynamoDBClient, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _settings()
    claude_transport = _RecordingTransport()
    claude_transport.queue(_claude_success_response(text=f"answer with {SENTINEL_ANSWER_WORD}"))
    slack_transport = _RecordingTransport()
    app = create_app(
        settings,
        anthropic_client=_claude_client(claude_transport),
        idempotency_client=dynamodb_client,
        http_client=_slack_http_client(slack_transport),
    )
    payload = _app_mention_payload(
        event_id="Ev0SECRET",
        text=f"<@U0TESTBOT01> {SENTINEL_QUESTION_WORD}",
    )

    with caplog.at_level(logging.DEBUG):
        response = await _post_event(app, payload)

    assert response.status_code == 200
    assert SENTINEL_MCP_TOKEN not in caplog.text
    assert SENTINEL_BOT_TOKEN not in caplog.text
    assert SENTINEL_ANTHROPIC_API_KEY not in caplog.text
    assert SENTINEL_SIGNING_SECRET not in caplog.text
    assert SENTINEL_QUESTION_WORD not in caplog.text
    assert SENTINEL_ANSWER_WORD not in caplog.text


# --- 🔴 SDK DEBUG logging trap: anthropic/httpx2 loggers pinned to INFO -----


def test_anthropic_and_httpx2_loggers_pinned_to_info_even_under_debug() -> None:
    settings = _settings(log_level="DEBUG")

    create_app(settings, idempotency_client=None)

    assert logging.getLogger("anthropic").level == logging.INFO
    assert logging.getLogger("httpx2").level == logging.INFO
