"""Tests for devoks_slackbot.idempotency (TASK-006, TASK-007).

Traces: REQ-SB-003, AC-SB-003-1, AC-SB-003-2, AC-SB-003-3, CTR-SB-007,
EDGE-SB-004, EDGE-SB-005, DSN-SB-004, EDGE-SB-015, CTR-SB-009.

Uses ``moto``'s real DynamoDB conditional-write semantics rather than a
hand-rolled stub — a stub would only prove this module agrees with itself,
not that ``AC-SB-003-2``'s atomicity actually holds against DynamoDB's own
``ConditionalCheckFailedException`` behavior.
"""

from __future__ import annotations

import concurrent.futures
import time
from collections.abc import Generator
from typing import TYPE_CHECKING
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from devoks_slackbot import idempotency
from devoks_slackbot.config import IDEMPOTENCY_TTL_SECONDS_DEFAULT
from devoks_slackbot.idempotency import (
    INFLIGHT_TTL_SECONDS_DEFAULT,
    PARTITION_KEY_ATTR,
    TTL_ATTRIBUTE,
    ClaimOutcome,
    IdempotencyStoreError,
    InflightClaimOutcome,
    claim_event,
    claim_inflight_query,
    is_event_completed,
    mark_event_completed,
    release_inflight_query,
)

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient

_TABLE_NAME = "slackbot-idempotency-test"


@pytest.fixture(autouse=True)
def _fake_aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    # moto intercepts every boto3 call before it reaches AWS, but boto3
    # itself still refuses to build a client with no region/credentials
    # configured at all — these are fixture-only literals, never real
    # credentials (mirrors conftest.py's VALID_TEST_* naming intent).
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")


@pytest.fixture
def dynamodb_client() -> Generator[DynamoDBClient]:
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")  # pyright: ignore[reportUnknownMemberType]
        client.create_table(
            TableName=_TABLE_NAME,
            KeySchema=[{"AttributeName": PARTITION_KEY_ATTR, "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": PARTITION_KEY_ATTR, "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.update_time_to_live(
            TableName=_TABLE_NAME,
            TimeToLiveSpecification={"Enabled": True, "AttributeName": TTL_ATTRIBUTE},
        )
        yield client


# --- AC-SB-003-1: new vs. duplicate claim ------------------------------------


def test_claim_new_event_id_succeeds(dynamodb_client: DynamoDBClient) -> None:
    result = claim_event("Ev0NEW", table_name=_TABLE_NAME, client=dynamodb_client)

    assert result == ClaimOutcome(claimed=True, reason="claimed")


def test_claim_duplicate_event_id_is_rejected(dynamodb_client: DynamoDBClient) -> None:
    first = claim_event("Ev0DUP", table_name=_TABLE_NAME, client=dynamodb_client)
    second = claim_event("Ev0DUP", table_name=_TABLE_NAME, client=dynamodb_client)

    assert first == ClaimOutcome(claimed=True, reason="claimed")
    assert second == ClaimOutcome(claimed=False, reason="duplicate")


def test_claim_repeated_retries_of_same_event_id_all_rejected_after_first(
    dynamodb_client: DynamoDBClient,
) -> None:
    # A Slack retry storm (x-slack-retry-num 1|2|3, EDGE-SB-004) hitting the
    # same event_id repeatedly — only the very first call may win.
    outcomes = [
        claim_event("Ev0RETRYSTORM", table_name=_TABLE_NAME, client=dynamodb_client)
        for _ in range(5)
    ]

    assert [outcome.claimed for outcome in outcomes] == [True, False, False, False, False]


def test_claim_distinct_event_ids_each_succeed_independently(
    dynamodb_client: DynamoDBClient,
) -> None:
    result_a = claim_event("Ev0A", table_name=_TABLE_NAME, client=dynamodb_client)
    result_b = claim_event("Ev0B", table_name=_TABLE_NAME, client=dynamodb_client)

    assert result_a.claimed is True
    assert result_b.claimed is True


# --- AC-SB-003-2: atomicity — exactly one winner, no read-then-write --------


def test_claim_concurrent_racing_calls_only_one_succeeds(dynamodb_client: DynamoDBClient) -> None:
    event_id = "Ev0CONCURRENT"

    def attempt(_: int) -> ClaimOutcome:
        return claim_event(event_id, table_name=_TABLE_NAME, client=dynamodb_client)

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        results = list(executor.map(attempt, range(16)))

    claimed_count = sum(1 for result in results if result.claimed)
    assert claimed_count == 1


def test_claim_uses_conditional_put_without_any_get(dynamodb_client: DynamoDBClient) -> None:
    with (
        patch.object(dynamodb_client, "get_item", wraps=dynamodb_client.get_item) as get_spy,
        patch.object(dynamodb_client, "put_item", wraps=dynamodb_client.put_item) as put_spy,
    ):
        claim_event("Ev0STRUCTURAL", table_name=_TABLE_NAME, client=dynamodb_client)

    get_spy.assert_not_called()
    put_spy.assert_called_once()
    call = put_spy.call_args
    assert call is not None
    assert call.kwargs["ConditionExpression"] == f"attribute_not_exists({PARTITION_KEY_ATTR})"
    assert call.kwargs["Item"][PARTITION_KEY_ATTR] == {"S": "event#Ev0STRUCTURAL"}


# --- AC-SB-003-3 / CTR-SB-007: TTL attribute ---------------------------------


def test_claim_writes_ttl_as_epoch_seconds_from_default(dynamodb_client: DynamoDBClient) -> None:
    before = int(time.time())
    claim_event("Ev0TTLDEFAULT", table_name=_TABLE_NAME, client=dynamodb_client)
    after = int(time.time())

    response = dynamodb_client.get_item(
        TableName=_TABLE_NAME, Key={PARTITION_KEY_ATTR: {"S": "event#Ev0TTLDEFAULT"}}
    )
    item = response.get("Item")
    assert item is not None
    ttl_value = int(item[TTL_ATTRIBUTE].get("N", "0"))

    assert (
        before + IDEMPOTENCY_TTL_SECONDS_DEFAULT
        <= ttl_value
        <= after + IDEMPOTENCY_TTL_SECONDS_DEFAULT
    )


def test_claim_writes_ttl_as_epoch_seconds_from_custom_ttl(dynamodb_client: DynamoDBClient) -> None:
    custom_ttl_seconds = 600
    before = int(time.time())
    claim_event(
        "Ev0TTLCUSTOM",
        table_name=_TABLE_NAME,
        ttl_seconds=custom_ttl_seconds,
        client=dynamodb_client,
    )
    after = int(time.time())

    response = dynamodb_client.get_item(
        TableName=_TABLE_NAME, Key={PARTITION_KEY_ATTR: {"S": "event#Ev0TTLCUSTOM"}}
    )
    item = response.get("Item")
    assert item is not None
    ttl_value = int(item[TTL_ATTRIBUTE].get("N", "0"))

    assert before + custom_ttl_seconds <= ttl_value <= after + custom_ttl_seconds


# --- claim_event: missing event_id is unclaimable, no DynamoDB call --------


def test_claim_none_event_id_is_unclaimable_without_dynamodb_call(
    dynamodb_client: DynamoDBClient,
) -> None:
    with patch.object(dynamodb_client, "put_item", wraps=dynamodb_client.put_item) as put_spy:
        result = claim_event(None, table_name=_TABLE_NAME, client=dynamodb_client)

    assert result == ClaimOutcome(claimed=False, reason="missing_event_id")
    put_spy.assert_not_called()


def test_claim_empty_string_event_id_is_unclaimable_without_dynamodb_call(
    dynamodb_client: DynamoDBClient,
) -> None:
    with patch.object(dynamodb_client, "put_item", wraps=dynamodb_client.put_item) as put_spy:
        result = claim_event("", table_name=_TABLE_NAME, client=dynamodb_client)

    assert result == ClaimOutcome(claimed=False, reason="missing_event_id")
    put_spy.assert_not_called()


# --- EDGE-SB-005: completion record + query ----------------------------------


def test_is_event_completed_false_before_mark_event_completed(
    dynamodb_client: DynamoDBClient,
) -> None:
    claim_event("Ev0PENDING", table_name=_TABLE_NAME, client=dynamodb_client)

    assert is_event_completed("Ev0PENDING", table_name=_TABLE_NAME, client=dynamodb_client) is False


def test_mark_event_completed_then_is_event_completed_true(
    dynamodb_client: DynamoDBClient,
) -> None:
    claim_event("Ev0DONE", table_name=_TABLE_NAME, client=dynamodb_client)

    mark_event_completed("Ev0DONE", table_name=_TABLE_NAME, client=dynamodb_client)

    assert is_event_completed("Ev0DONE", table_name=_TABLE_NAME, client=dynamodb_client) is True


def test_mark_event_completed_is_idempotent_across_repeated_worker_retries(
    dynamodb_client: DynamoDBClient,
) -> None:
    # EDGE-SB-005: Lambda's own async-invoke retry can call the worker again
    # for an event_id already marked complete — calling mark_event_completed
    # a second (or third) time must not raise or corrupt the record.
    claim_event("Ev0RETRY", table_name=_TABLE_NAME, client=dynamodb_client)

    mark_event_completed("Ev0RETRY", table_name=_TABLE_NAME, client=dynamodb_client)
    mark_event_completed("Ev0RETRY", table_name=_TABLE_NAME, client=dynamodb_client)
    mark_event_completed("Ev0RETRY", table_name=_TABLE_NAME, client=dynamodb_client)

    assert is_event_completed("Ev0RETRY", table_name=_TABLE_NAME, client=dynamodb_client) is True


def test_is_event_completed_false_for_unknown_event_id(dynamodb_client: DynamoDBClient) -> None:
    assert (
        is_event_completed("Ev0NEVERSEEN", table_name=_TABLE_NAME, client=dynamodb_client) is False
    )


def test_mark_event_completed_no_op_for_empty_event_id(dynamodb_client: DynamoDBClient) -> None:
    with patch.object(dynamodb_client, "update_item", wraps=dynamodb_client.update_item) as spy:
        mark_event_completed("", table_name=_TABLE_NAME, client=dynamodb_client)

    spy.assert_not_called()


def test_is_event_completed_false_for_empty_event_id_without_dynamodb_call(
    dynamodb_client: DynamoDBClient,
) -> None:
    with patch.object(dynamodb_client, "get_item", wraps=dynamodb_client.get_item) as spy:
        result = is_event_completed("", table_name=_TABLE_NAME, client=dynamodb_client)

    assert result is False
    spy.assert_not_called()


# --- DynamoDB errors other than ConditionalCheckFailedException -------------


def test_claim_against_missing_table_raises_idempotency_store_error(
    dynamodb_client: DynamoDBClient,
) -> None:
    with pytest.raises(IdempotencyStoreError):
        claim_event("Ev0NOTABLE", table_name="table-does-not-exist", client=dynamodb_client)


def test_is_event_completed_against_missing_table_raises_idempotency_store_error(
    dynamodb_client: DynamoDBClient,
) -> None:
    with pytest.raises(IdempotencyStoreError):
        is_event_completed("Ev0NOTABLE", table_name="table-does-not-exist", client=dynamodb_client)


def test_mark_event_completed_against_missing_table_raises_idempotency_store_error(
    dynamodb_client: DynamoDBClient,
) -> None:
    with pytest.raises(IdempotencyStoreError):
        mark_event_completed(
            "Ev0NOTABLE", table_name="table-does-not-exist", client=dynamodb_client
        )


# --- boto3 cost management: lazy, cached default client ---------------------


def test_default_client_is_lazily_created_only_on_first_use_and_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No earlier test in this suite ever exercises the None-client path
    # (every other test injects a moto client explicitly) — so this also
    # pins that importing this module alone never creates one.
    assert idempotency._default_client is None  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(idempotency, "_default_client", None)  # always reset after this test

    with (
        mock_aws(),
        patch.object(boto3, "client", wraps=boto3.client) as client_spy,  # pyright: ignore[reportUnknownMemberType]
    ):
        first = idempotency._resolve_client(None)  # pyright: ignore[reportPrivateUsage]
        second = idempotency._resolve_client(None)  # pyright: ignore[reportPrivateUsage]

    client_spy.assert_called_once()
    assert first is second


def test_claim_event_treats_whitespace_only_event_id_as_missing_ac_sb_003_1(
    dynamodb_client: DynamoDBClient,
) -> None:
    """A blank ``event_id`` is unusable, like ``None`` and ``""`` (``AC-SB-003-1``).

    Without this, a whitespace-only id becomes the storage key ``event#   `` and
    every malformed event collides on that single row: the first is processed and
    the rest are silently dropped as duplicates.
    """
    for blank in (" ", "   ", "\t", "\n", " \t\n "):
        outcome = claim_event(
            blank, table_name=_TABLE_NAME, ttl_seconds=3600, client=dynamodb_client
        )
        assert outcome.claimed is False
        assert outcome.reason == "missing_event_id"


# --- EDGE-SB-015 (TASK-007): in-flight query coalescing ----------------------


def test_claim_inflight_query_first_call_succeeds(dynamodb_client: DynamoDBClient) -> None:
    result = claim_inflight_query(
        "U0FIRST", "1700000000.000100", table_name=_TABLE_NAME, client=dynamodb_client
    )

    assert result == InflightClaimOutcome(claimed=True, reason="claimed")


def test_claim_inflight_query_second_call_same_user_thread_is_blocked_edge_sb_015(
    dynamodb_client: DynamoDBClient,
) -> None:
    user_id, thread_ts = "U0REPEAT", "1700000000.000200"

    first = claim_inflight_query(user_id, thread_ts, table_name=_TABLE_NAME, client=dynamodb_client)
    second = claim_inflight_query(
        user_id, thread_ts, table_name=_TABLE_NAME, client=dynamodb_client
    )

    assert first == InflightClaimOutcome(claimed=True, reason="claimed")
    assert second == InflightClaimOutcome(claimed=False, reason="in_progress")


def test_release_inflight_query_then_reclaim_succeeds_edge_sb_015(
    dynamodb_client: DynamoDBClient,
) -> None:
    """The lockout-regression guard: EDGE-SB-015 only blocks a query *in flight* —
    without an explicit release, a user who asked once would stay
    coalescing-blocked until the TTL sweep, which is the bug this test pins
    against.
    """
    user_id, thread_ts = "U0RELEASE", "1700000000.000300"

    first = claim_inflight_query(user_id, thread_ts, table_name=_TABLE_NAME, client=dynamodb_client)
    blocked = claim_inflight_query(
        user_id, thread_ts, table_name=_TABLE_NAME, client=dynamodb_client
    )

    release_inflight_query(user_id, thread_ts, table_name=_TABLE_NAME, client=dynamodb_client)

    after_release = claim_inflight_query(
        user_id, thread_ts, table_name=_TABLE_NAME, client=dynamodb_client
    )

    assert first.claimed is True
    assert blocked == InflightClaimOutcome(claimed=False, reason="in_progress")
    assert after_release == InflightClaimOutcome(claimed=True, reason="claimed")


def test_claim_inflight_query_same_user_different_thread_both_succeed_edge_sb_015(
    dynamodb_client: DynamoDBClient,
) -> None:
    user_id = "U0MULTITHREAD"

    first = claim_inflight_query(
        user_id, "1700000000.000401", table_name=_TABLE_NAME, client=dynamodb_client
    )
    second = claim_inflight_query(
        user_id, "1700000000.000402", table_name=_TABLE_NAME, client=dynamodb_client
    )

    assert first.claimed is True
    assert second.claimed is True


def test_claim_inflight_query_different_user_same_thread_both_succeed_edge_sb_015(
    dynamodb_client: DynamoDBClient,
) -> None:
    thread_ts = "1700000000.000500"

    first = claim_inflight_query("U0A", thread_ts, table_name=_TABLE_NAME, client=dynamodb_client)
    second = claim_inflight_query("U0B", thread_ts, table_name=_TABLE_NAME, client=dynamodb_client)

    assert first.claimed is True
    assert second.claimed is True


def test_claim_inflight_query_concurrent_racing_calls_only_one_succeeds_edge_sb_015(
    dynamodb_client: DynamoDBClient,
) -> None:
    user_id, thread_ts = "U0CONCURRENT", "1700000000.000600"

    def attempt(_: int) -> InflightClaimOutcome:
        return claim_inflight_query(
            user_id, thread_ts, table_name=_TABLE_NAME, client=dynamodb_client
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        results = list(executor.map(attempt, range(16)))

    claimed_count = sum(1 for result in results if result.claimed)
    assert claimed_count == 1


def test_claim_inflight_query_writes_short_ttl_not_idempotency_default_edge_sb_015(
    dynamodb_client: DynamoDBClient,
) -> None:
    """Pins the lockout-prevention design decision: an in-flight lock must expire
    within ``CTR-SB-009``'s worker limit + margin, never ``CTR-SB-007``'s
    3,600s idempotency default — else a crashed worker locks its user out for
    up to an hour.
    """
    assert INFLIGHT_TTL_SECONDS_DEFAULT != IDEMPOTENCY_TTL_SECONDS_DEFAULT
    assert INFLIGHT_TTL_SECONDS_DEFAULT < IDEMPOTENCY_TTL_SECONDS_DEFAULT

    before = int(time.time())
    claim_inflight_query(
        "U0TTL", "1700000000.000700", table_name=_TABLE_NAME, client=dynamodb_client
    )
    after = int(time.time())

    response = dynamodb_client.get_item(
        TableName=_TABLE_NAME,
        Key={PARTITION_KEY_ATTR: {"S": "inflight#U0TTL#1700000000.000700"}},
    )
    item = response.get("Item")
    assert item is not None
    ttl_value = int(item[TTL_ATTRIBUTE].get("N", "0"))

    assert (
        before + INFLIGHT_TTL_SECONDS_DEFAULT <= ttl_value <= after + INFLIGHT_TTL_SECONDS_DEFAULT
    )


@pytest.mark.parametrize(
    ("user_id", "thread_ts"),
    [
        (None, "1700000000.000800"),
        ("U0BLANKTHREAD", None),
        ("", "1700000000.000800"),
        ("U0BLANKTHREAD", ""),
        ("   ", "1700000000.000800"),
        ("U0BLANKTHREAD", "\t\n"),
        (None, None),
    ],
)
def test_claim_inflight_query_missing_identifiers_never_blocks_edge_sb_015(
    dynamodb_client: DynamoDBClient, user_id: str | None, thread_ts: str | None
) -> None:
    with patch.object(dynamodb_client, "put_item", wraps=dynamodb_client.put_item) as put_spy:
        result = claim_inflight_query(
            user_id, thread_ts, table_name=_TABLE_NAME, client=dynamodb_client
        )

    assert result == InflightClaimOutcome(claimed=True, reason="identifiers_missing")
    put_spy.assert_not_called()


def test_release_inflight_query_called_twice_is_safe_edge_sb_015(
    dynamodb_client: DynamoDBClient,
) -> None:
    user_id, thread_ts = "U0DOUBLERELEASE", "1700000000.000900"
    claim_inflight_query(user_id, thread_ts, table_name=_TABLE_NAME, client=dynamodb_client)

    release_inflight_query(user_id, thread_ts, table_name=_TABLE_NAME, client=dynamodb_client)
    release_inflight_query(user_id, thread_ts, table_name=_TABLE_NAME, client=dynamodb_client)

    after = claim_inflight_query(user_id, thread_ts, table_name=_TABLE_NAME, client=dynamodb_client)
    assert after.claimed is True


def test_release_inflight_query_missing_identifiers_no_op_edge_sb_015(
    dynamodb_client: DynamoDBClient,
) -> None:
    with patch.object(dynamodb_client, "delete_item", wraps=dynamodb_client.delete_item) as spy:
        release_inflight_query(None, None, table_name=_TABLE_NAME, client=dynamodb_client)
        release_inflight_query("", "", table_name=_TABLE_NAME, client=dynamodb_client)

    spy.assert_not_called()


def test_claim_inflight_query_against_missing_table_raises_idempotency_store_error(
    dynamodb_client: DynamoDBClient,
) -> None:
    with pytest.raises(IdempotencyStoreError):
        claim_inflight_query(
            "U0NOTABLE",
            "1700000000.001000",
            table_name="table-does-not-exist",
            client=dynamodb_client,
        )


def test_release_inflight_query_against_missing_table_raises_idempotency_store_error(
    dynamodb_client: DynamoDBClient,
) -> None:
    with pytest.raises(IdempotencyStoreError):
        release_inflight_query(
            "U0NOTABLE",
            "1700000000.001000",
            table_name="table-does-not-exist",
            client=dynamodb_client,
        )
