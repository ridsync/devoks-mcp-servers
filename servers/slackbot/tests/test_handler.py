"""Tests for devoks_slackbot.handler (TASK-012).

Traces: REQ-SB-002, AC-SB-001-2, AC-SB-001-6, AC-SB-002-1, AC-SB-002-2,
AC-SB-002-3, CTR-SB-002, EDGE-SB-001, EDGE-SB-003, EDGE-SB-004, DSN-SB-001,
DSN-SB-008.

Every HTTP-level assertion here goes through the ASGI transport
(``httpx2.ASGITransport``), never an in-process function call to the endpoint
closure directly -- FRD §9's own testing strategy note (mirrored from Stage 1)
is that a 401/200 status code is only proven by actually exercising the HTTP
layer. ``dynamodb_client`` uses real ``moto`` DynamoDB semantics (same
rationale as ``test_idempotency.py``); the worker Lambda invoke uses a
lightweight hand-written fake (``_FakeLambdaClient``) rather than ``moto``'s
Lambda mocking -- see ``handler.WorkerInvoker``'s docstring for why.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import subprocess
import sys
import time
from collections.abc import Generator
from typing import TYPE_CHECKING, Any

import boto3
import httpx2
import pytest
from moto import mock_aws
from starlette.applications import Starlette

from devoks_slackbot import handler as handler_module
from devoks_slackbot.handler import HEALTHZ_PATH, SLACK_EVENTS_PATH, create_app
from devoks_slackbot.idempotency import (
    PARTITION_KEY_ATTR,
    TTL_ATTRIBUTE,
    ClaimOutcome,
    claim_event,
)

from .conftest import VALID_TEST_IDEMPOTENCY_TABLE, VALID_TEST_SIGNING_SECRET, make_handler_settings

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient

_BASE_URL = "https://handler.example.com"


# --- fixtures ------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fake_aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    # moto intercepts every boto3 call before it reaches AWS, but boto3
    # itself still refuses to build a client with no region/credentials
    # configured at all -- fixture-only literals, never real credentials
    # (mirrors test_idempotency.py's identical fixture).
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")


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


class _FakeLambdaClient:
    """A hand-written ``WorkerInvoker`` fake -- see that Protocol's docstring."""

    def __init__(self, *, raise_on_invoke: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._raise_on_invoke = raise_on_invoke

    def invoke(self, *, FunctionName: str, InvocationType: str, Payload: bytes) -> dict[str, Any]:
        if self._raise_on_invoke is not None:
            raise self._raise_on_invoke
        self.calls.append(
            {"FunctionName": FunctionName, "InvocationType": InvocationType, "Payload": Payload}
        )
        return {"StatusCode": 202}


# --- fixture-only payload/signature helpers -------------------------------


def _app_mention_payload(*, event_id: str, user: str = "U123ABC456") -> dict[str, Any]:
    """Slack's own documented ``app_mention`` example payload shape (see test_events.py)."""
    return {
        "token": "XXYYZZ",
        "team_id": "T123ABC456",
        "api_app_id": "A123ABC456",
        "event": {
            "type": "app_mention",
            "user": user,
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
        "event_id": event_id,
        "event_time": 123456789,
    }


def _reference_signature(secret: str, timestamp: str, body: bytes) -> str:
    """Independently-built HMAC per CTR-SB-001 (mirrors test_signature.py's helper)."""
    basestring = b"v0:" + timestamp.encode("ascii") + b":" + body
    digest = hmac.new(secret.encode("utf-8"), basestring, hashlib.sha256).hexdigest()
    return "v0=" + digest


def _signed_headers(
    body: bytes, *, secret: str = VALID_TEST_SIGNING_SECRET, timestamp: str | None = None
) -> dict[str, str]:
    ts = timestamp if timestamp is not None else str(int(time.time()))
    return {
        "X-Slack-Signature": _reference_signature(secret, ts, body),
        "X-Slack-Request-Timestamp": ts,
        "Content-Type": "application/json",
    }


async def _post_event(app: Starlette, body: bytes, headers: dict[str, str]) -> httpx2.Response:
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url=_BASE_URL) as client:
        return await client.post(SLACK_EVENTS_PATH, content=body, headers=headers)


async def _get(app: Starlette, path: str) -> httpx2.Response:
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url=_BASE_URL) as client:
        return await client.get(path)


# --- /healthz ---------------------------------------------------------------


async def test_healthz_returns_200() -> None:
    app = create_app(make_handler_settings())

    response = await _get(app, HEALTHZ_PATH)

    assert response.status_code == 200


# --- AC-SB-002-1: valid signature + app_mention -> 200, worker invoked once --


async def test_valid_signature_app_mention_dispatches_worker_once_ac_sb_002_1(
    dynamodb_client: DynamoDBClient,
) -> None:
    settings = make_handler_settings()
    fake_lambda = _FakeLambdaClient()
    app = create_app(settings, lambda_client=fake_lambda, idempotency_client=dynamodb_client)
    body = json.dumps(_app_mention_payload(event_id="Ev0DISPATCH")).encode()
    headers = _signed_headers(body, secret=settings.signing_secret)

    response = await _post_event(app, body, headers)

    assert response.status_code == 200
    assert len(fake_lambda.calls) == 1
    call = fake_lambda.calls[0]
    assert call["FunctionName"] == settings.worker_function_name
    assert call["InvocationType"] == "Event"
    # DSN-SB-001: the worker receives the original, unparsed Slack payload.
    assert call["Payload"] == body


async def test_same_event_id_posted_twice_over_http_only_dispatches_once(
    dynamodb_client: DynamoDBClient,
) -> None:
    """Repeat/rapid-fire case: the *same* signed request replayed twice end-to-end."""
    settings = make_handler_settings()
    fake_lambda = _FakeLambdaClient()
    app = create_app(settings, lambda_client=fake_lambda, idempotency_client=dynamodb_client)
    body = json.dumps(_app_mention_payload(event_id="Ev0TWICE")).encode()

    first = await _post_event(app, body, _signed_headers(body, secret=settings.signing_secret))
    second = await _post_event(app, body, _signed_headers(body, secret=settings.signing_secret))

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(fake_lambda.calls) == 1


# --- AC-SB-001-2 / EDGE-SB-001: signature mismatch -> 401, no body parsing --


async def test_signature_mismatch_returns_401_without_parsing_body_ac_sb_001_2_edge_sb_001() -> (
    None
):
    settings = make_handler_settings()
    app = create_app(settings, lambda_client=_FakeLambdaClient())
    # Deliberately not valid JSON -- if the handler tried to parse it before
    # checking the signature, this would surface as something other than a
    # clean 401 (a 500, or a 400 from the JSON-decode path).
    malformed_body = b"{not valid json at all"
    headers = _signed_headers(malformed_body, secret="a-completely-different-secret-value")

    response = await _post_event(app, malformed_body, headers)

    assert response.status_code == 401


async def test_missing_signature_headers_returns_401() -> None:
    settings = make_handler_settings()
    app = create_app(settings, lambda_client=_FakeLambdaClient())
    body = json.dumps(_app_mention_payload(event_id="Ev0NOSIG")).encode()

    response = await _post_event(app, body, {"Content-Type": "application/json"})

    assert response.status_code == 401


# --- EDGE-SB-002: timestamp outside 5 minutes -> 401 ------------------------


async def test_timestamp_outside_five_minutes_returns_401_edge_sb_002() -> None:
    settings = make_handler_settings()
    app = create_app(settings, lambda_client=_FakeLambdaClient())
    body = json.dumps(_app_mention_payload(event_id="Ev0STALE")).encode()
    stale_timestamp = str(int(time.time()) - 400)
    headers = _signed_headers(body, secret=settings.signing_secret, timestamp=stale_timestamp)

    response = await _post_event(app, body, headers)

    assert response.status_code == 401


# --- AC-SB-001-6 / EDGE-SB-003: url_verification ----------------------------


async def test_url_verification_with_valid_signature_returns_challenge_ac_sb_001_6() -> None:
    settings = make_handler_settings()
    app = create_app(settings, lambda_client=_FakeLambdaClient())
    payload = {"token": "XXYYZZ", "challenge": "3eZbrw1aBm2rZgRNFdxV2", "type": "url_verification"}
    body = json.dumps(payload).encode()
    headers = _signed_headers(body, secret=settings.signing_secret)

    response = await _post_event(app, body, headers)

    assert response.status_code == 200
    assert response.json() == {"challenge": "3eZbrw1aBm2rZgRNFdxV2"}


async def test_url_verification_without_valid_signature_returns_401_edge_sb_003() -> None:
    settings = make_handler_settings()
    app = create_app(settings, lambda_client=_FakeLambdaClient())
    payload = {"token": "XXYYZZ", "challenge": "should-not-be-echoed", "type": "url_verification"}
    body = json.dumps(payload).encode()
    headers = _signed_headers(body, secret="wrong-secret-value")

    response = await _post_event(app, body, headers)

    assert response.status_code == 401
    assert "should-not-be-echoed" not in response.text


# --- AC-SB-003-1: duplicate event_id -> 200, worker invoked 0 times ---------


async def test_duplicate_event_id_returns_200_without_dispatch_ac_sb_003_1(
    dynamodb_client: DynamoDBClient,
) -> None:
    settings = make_handler_settings()
    event_id = "Ev0DUP"
    claim_event(event_id, table_name=settings.idempotency_table, client=dynamodb_client)
    fake_lambda = _FakeLambdaClient()
    app = create_app(settings, lambda_client=fake_lambda, idempotency_client=dynamodb_client)
    body = json.dumps(_app_mention_payload(event_id=event_id)).encode()
    headers = _signed_headers(body, secret=settings.signing_secret)

    response = await _post_event(app, body, headers)

    assert response.status_code == 200
    assert fake_lambda.calls == []


async def test_event_callback_missing_event_id_returns_200_without_dispatch(
    dynamodb_client: DynamoDBClient,
) -> None:
    settings = make_handler_settings()
    fake_lambda = _FakeLambdaClient()
    app = create_app(settings, lambda_client=fake_lambda, idempotency_client=dynamodb_client)
    payload = _app_mention_payload(event_id="Ev0PLACEHOLDER")
    del payload["event_id"]
    body = json.dumps(payload).encode()
    headers = _signed_headers(body, secret=settings.signing_secret)

    response = await _post_event(app, body, headers)

    assert response.status_code == 200
    assert fake_lambda.calls == []


# --- EDGE-SB-004: x-slack-retry-num (any casing) logs a warning ------------


@pytest.mark.parametrize(
    "header_name", ["x-slack-retry-num", "X-Slack-Retry-Num", "X-SLACK-RETRY-NUM"]
)
async def test_retry_num_header_any_case_logs_warning_edge_sb_004(
    header_name: str, dynamodb_client: DynamoDBClient, caplog: pytest.LogCaptureFixture
) -> None:
    settings = make_handler_settings()
    app = create_app(
        settings, lambda_client=_FakeLambdaClient(), idempotency_client=dynamodb_client
    )
    body = json.dumps(_app_mention_payload(event_id=f"Ev0RETRY{abs(hash(header_name))}")).encode()
    headers = _signed_headers(body, secret=settings.signing_secret)
    headers[header_name] = "1"

    with caplog.at_level(logging.WARNING, logger="devoks_slackbot.handler"):
        response = await _post_event(app, body, headers)

    assert response.status_code == 200
    assert "retry" in caplog.text.lower()


async def test_no_retry_num_header_logs_no_retry_warning(
    dynamodb_client: DynamoDBClient, caplog: pytest.LogCaptureFixture
) -> None:
    settings = make_handler_settings()
    app = create_app(
        settings, lambda_client=_FakeLambdaClient(), idempotency_client=dynamodb_client
    )
    body = json.dumps(_app_mention_payload(event_id="Ev0NORETRY")).encode()
    headers = _signed_headers(body, secret=settings.signing_secret)

    with caplog.at_level(logging.WARNING, logger="devoks_slackbot.handler"):
        response = await _post_event(app, body, headers)

    assert response.status_code == 200
    assert "retry" not in caplog.text.lower()


# --- EDGE-SB-011: bot self message -> 200, worker invoked 0 times ----------


async def test_bot_self_message_returns_200_without_dispatch_edge_sb_011(
    dynamodb_client: DynamoDBClient,
) -> None:
    settings = make_handler_settings()
    fake_lambda = _FakeLambdaClient()
    app = create_app(settings, lambda_client=fake_lambda, idempotency_client=dynamodb_client)
    payload = _app_mention_payload(event_id="Ev0BOTSELF", user=settings.bot_user_id)
    body = json.dumps(payload).encode()
    headers = _signed_headers(body, secret=settings.signing_secret)

    response = await _post_event(app, body, headers)

    assert response.status_code == 200
    assert fake_lambda.calls == []


# --- AC-SB-002-3: async dispatch failing still returns 200 ------------------


async def test_async_dispatch_failure_still_returns_200_ac_sb_002_3(
    dynamodb_client: DynamoDBClient,
) -> None:
    settings = make_handler_settings()
    failing_lambda = _FakeLambdaClient(raise_on_invoke=RuntimeError("boom"))
    app = create_app(settings, lambda_client=failing_lambda, idempotency_client=dynamodb_client)
    body = json.dumps(_app_mention_payload(event_id="Ev0DISPATCHFAIL")).encode()
    headers = _signed_headers(body, secret=settings.signing_secret)

    response = await _post_event(app, body, headers)

    assert response.status_code == 200


# --- IdempotencyStoreError: defined fail-safe behavior ----------------------


async def test_idempotency_store_error_returns_200_without_dispatch(
    dynamodb_client: DynamoDBClient,
) -> None:
    settings = make_handler_settings(idempotency_table="table-does-not-exist")
    fake_lambda = _FakeLambdaClient()
    app = create_app(settings, lambda_client=fake_lambda, idempotency_client=dynamodb_client)
    body = json.dumps(_app_mention_payload(event_id="Ev0STOREERR")).encode()
    headers = _signed_headers(body, secret=settings.signing_secret)

    response = await _post_event(app, body, headers)

    assert response.status_code == 200
    assert fake_lambda.calls == []


# --- code review Medium #1: store_error must not be misreported as ----------
# --- missing_event_id (event_id is present and fine; the store didn't respond)


def test_claim_or_fail_safe_returns_store_error_reason_for_idempotency_store_error(
    dynamodb_client: DynamoDBClient,
) -> None:
    """``_claim_or_fail_safe`` must report ``reason="store_error"``, never
    ``"missing_event_id"`` -- the review's Medium #1: a real DynamoDB outage
    must never be misclassified as "event_id was absent" (``event_id`` here
    is present and well-formed; only the store call itself failed)."""
    settings = make_handler_settings(idempotency_table="table-does-not-exist")

    outcome = handler_module._claim_or_fail_safe(  # pyright: ignore[reportPrivateUsage]
        "Ev0STOREERRREASON", settings=settings, client=dynamodb_client
    )

    assert outcome == ClaimOutcome(claimed=False, reason="store_error")


def test_claim_or_fail_safe_reason_values_unchanged_for_non_store_error_paths(
    dynamodb_client: DynamoDBClient,
) -> None:
    """Regression: this fix only changes the store-error branch's ``reason`` --
    ``"claimed"``, ``"duplicate"``, and ``"missing_event_id"`` must be unchanged."""
    settings = make_handler_settings()

    fresh = handler_module._claim_or_fail_safe(  # pyright: ignore[reportPrivateUsage]
        "Ev0FAILSAFEFRESH", settings=settings, client=dynamodb_client
    )
    assert fresh == ClaimOutcome(claimed=True, reason="claimed")

    duplicate = handler_module._claim_or_fail_safe(  # pyright: ignore[reportPrivateUsage]
        "Ev0FAILSAFEFRESH", settings=settings, client=dynamodb_client
    )
    assert duplicate == ClaimOutcome(claimed=False, reason="duplicate")

    missing = handler_module._claim_or_fail_safe(  # pyright: ignore[reportPrivateUsage]
        None, settings=settings, client=dynamodb_client
    )
    assert missing == ClaimOutcome(claimed=False, reason="missing_event_id")


# --- POST-only route -----------------------------------------------------


async def test_slack_events_path_rejects_get_method() -> None:
    app = create_app(make_handler_settings())

    response = await _get(app, SLACK_EVENTS_PATH)

    assert response.status_code == 405


# --- secret exposure ---------------------------------------------------------


async def test_no_secrets_leak_into_responses_or_logs(
    dynamodb_client: DynamoDBClient, caplog: pytest.LogCaptureFixture
) -> None:
    sentinel_secret = "sentinel-signing-secret-DO-NOT-LEAK-9f3a"
    sentinel_bot_token = "xoxb-sentinel-bot-token-DO-NOT-LEAK-9f3a"
    settings = make_handler_settings(signing_secret=sentinel_secret, bot_token=sentinel_bot_token)
    fake_lambda = _FakeLambdaClient()
    app = create_app(settings, lambda_client=fake_lambda, idempotency_client=dynamodb_client)

    responses: list[httpx2.Response] = []
    with caplog.at_level(logging.DEBUG):
        valid_body = json.dumps(_app_mention_payload(event_id="Ev0SECRETOK")).encode()
        responses.append(
            await _post_event(app, valid_body, _signed_headers(valid_body, secret=sentinel_secret))
        )

        bad_sig_headers = _signed_headers(valid_body, secret="wrong-secret")
        responses.append(await _post_event(app, valid_body, bad_sig_headers))

        retry_body = json.dumps(_app_mention_payload(event_id="Ev0SECRETRETRY")).encode()
        retry_headers = _signed_headers(retry_body, secret=sentinel_secret)
        retry_headers["x-slack-retry-num"] = "1"
        responses.append(await _post_event(app, retry_body, retry_headers))

        uv_payload = {"token": "X", "challenge": "chal-sentinel-ok", "type": "url_verification"}
        uv_body = json.dumps(uv_payload).encode()
        responses.append(
            await _post_event(app, uv_body, _signed_headers(uv_body, secret=sentinel_secret))
        )

        responses.append(await _get(app, HEALTHZ_PATH))

    for response in responses:
        assert sentinel_secret not in response.text
        assert sentinel_bot_token not in response.text
    assert sentinel_secret not in caplog.text
    assert sentinel_bot_token not in caplog.text


# --- DSN-SB-008: no anthropic in the import graph ---------------------------


def test_importing_handler_never_imports_anthropic_dsn_sb_008() -> None:
    """Subprocess-isolated (duplicates TASK-013's own dedicated test, deliberately).

    A same-process ``sys.modules`` check would be contaminated by whichever
    other test module in this suite happens to import ``ask.py`` (and
    therefore ``anthropic``) first -- pytest collects all test modules into
    one process, so ``sys.modules`` accumulates across the whole run. A fresh
    subprocess that imports only ``devoks_slackbot.handler`` is the only way
    to prove this module's *own* import graph is clean.
    """
    script = (
        "import sys\n"
        "import devoks_slackbot.handler\n"
        "leaked = sorted(\n"
        "    m for m in sys.modules if m == 'anthropic' or m.startswith('anthropic.')\n"
        ")\n"
        "assert not leaked, leaked\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
