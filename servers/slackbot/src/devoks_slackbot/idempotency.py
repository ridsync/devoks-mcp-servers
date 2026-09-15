"""``event_id`` conditional writes -> idempotency store (DSN-SB-004, TASK-006).

``REQ-SB-003``: two independent sources produce a duplicate delivery of the
same Slack event, each needing its own defense:

- ``EDGE-SB-004``: Slack itself retries a webhook up to 3 times (exponential
  backoff) whenever the handler doesn't 2xx within ``CTR-SB-002``'s 3-second
  budget. ``claim_event`` below is the defense — the handler calls it before
  doing any work, and only the caller that wins the race dispatches to the
  worker.
- ``EDGE-SB-005``: a Lambda **async invoke** (the handler -> worker hop) is
  retried by Lambda itself (2 more attempts) whenever the worker's own
  invocation errors or times out — a cause of duplication entirely
  independent of Slack's retries, and one that only the *worker* can see.
  ``mark_event_completed``/``is_event_completed`` are the worker-side
  defense: post the reply, record completion, and check that record before
  posting again.

**``AC-SB-003-2``'s atomicity is the reason this module exists at all.** A
naive "check if ``event_id`` exists, then write if not" is two round trips
with a window between them — two concurrent retries can both observe
"not present" and both proceed. ``claim_event`` never does that: it issues
exactly one conditional ``PutItem`` (``ConditionExpression =
attribute_not_exists(pk)``) and lets DynamoDB itself be the single point of
truth for "did I win the race" — this is the whole reason ``DSN-SB-004``
chose DynamoDB over, say, a plain S3 object (whose conditional-write support
is weaker). Every function below preserves this: no function in this module
ever performs a read to decide whether a write is safe.

**TTL is a cost-cleanup mechanism, not a correctness mechanism — for the**
**``event#`` rows.** DynamoDB's TTL deletion is a *background* sweep — AWS
documents it as typically happening within 48 hours of expiry, not
immediately at the epoch second recorded in ``ttl``. So an
expired-but-not-yet-swept row can still be read. ``AC-SB-003-3`` only asks
this module to *write* the TTL attribute correctly (``now + ttl_seconds``,
epoch seconds, ``CTR-SB-007``'s default 3,600) — it does **not** ask any
function here to treat "TTL has passed" as a signal that an ``event_id`` may
be reprocessed. No function in this module reads or reasons about the
``ttl`` attribute's value for that purpose, and none ever should.

**The ``inflight#`` rows are the one exception — there, TTL *is* a**
**correctness safety net, not just cleanup.** ``release_inflight_query``
(below) is the *primary* release path (an explicit delete once a reply is
posted, success or failure) — but a worker that crashes/OOMs/times out
before its ``finally`` block runs never calls it. Without a short TTL that
user would stay coalescing-blocked forever. See ``INFLIGHT_TTL_SECONDS_DEFAULT``
for why that TTL is deliberately **not** ``CTR-SB-007``'s 3,600s.

**Key scheme is intentionally reusable, not hardcoded to events.** This
table is single-attribute-design: one partition key (``PARTITION_KEY_ATTR``,
below), namespaced by prefix (``"event#<event_id>"`` for ``TASK-006``,
``"inflight#<user_id>#<thread_ts>"`` for ``TASK-007``'s ``EDGE-SB-015``
coalescing lock). The generic, key-scheme-agnostic primitives
(``_claim``/``_mark_completed``/``_is_completed``/``_release``) take an
already-namespaced key string and know nothing about "events" or
"in-flight queries" — ``TASK-007`` reuses the same table under its own
prefix, rather than standing up a second table or duplicating the
conditional-write logic. It reuses ``_claim`` unchanged to lock, but pairs
it with the new ``_release`` (not ``_mark_completed``) to unlock — a
coalescing lock must *disappear* on release, not just change status, see
the note above — and never touches ``_mark_completed``/``_is_completed`` at
all, since a coalescing lock has no "completed" state, only "locked" or
"absent."

**boto3 cost management (``CTR-SB-002``'s 3-second budget).** Measured:
``import boto3`` costs 385 ms, and building a client costs another 82 ms
(workspace PLAN §1). The 385 ms is unavoidable — the handler needs boto3
regardless — but the 82 ms is not paid per-invocation: every public function
here accepts an optional ``client`` (tests inject a ``moto``-backed one),
and when none is given, ``_resolve_client`` builds a real client **lazily,
on first use, and caches it in a module global** so a warm Lambda container
reuses it across invocations instead of rebuilding it every call. Nothing in
this module calls ``boto3.client(...)`` at import time.

Import budget: ``boto3`` only (handler-safe, ``DSN-SB-008`` — this module is
never allowed to import ``anthropic``).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from .config import IDEMPOTENCY_TTL_SECONDS_DEFAULT

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient

logger = logging.getLogger(__name__)

#: Public: infra provisioning (the DynamoDB table itself, RES-SB-API-004) and
#: tests both need these exact attribute names to match what this module
#: writes/reads — kept here as the single source of truth rather than
#: re-hardcoded at each call site.
PARTITION_KEY_ATTR = "pk"
TTL_ATTRIBUTE = "ttl"

#: ``EDGE-SB-015``'s in-flight coalescing lock TTL — deliberately *not*
#: ``IDEMPOTENCY_TTL_SECONDS_DEFAULT`` (``CTR-SB-007``'s 3,600s). That value
#: is sized for how long a *duplicate delivery* can keep arriving (Slack/
#: Lambda retry windows) — it has nothing to do with how long one query
#: should ever legitimately run. A coalescing lock must instead expire
#: within ``CTR-SB-009``'s worker execution limit (300s) + a margin: the
#: worker's own ``try/finally`` calls ``release_inflight_query`` on every
#: normal exit, so this TTL only matters when the worker never gets that far
#: (crash, OOM kill, hard Lambda timeout) — the one path where nothing else
#: ever deletes the row. Reusing 3,600s here would mean that every time a
#: worker dies mid-query, the affected user is locked out of asking a new
#: question for up to **one hour**, for a mechanism whose entire purpose is
#: cost optimization, not a security or correctness boundary — that outcome
#: is strictly worse than the duplicate-processing cost this lock exists to
#: avoid. 300s (worker limit) + 60s (margin for Lambda scheduling/network delay
#: between the worker's actual death and the point a retry would otherwise
#: be blocked) = 360s.
INFLIGHT_TTL_SECONDS_DEFAULT = 360

#: Internal-only: which state an item is in. Never read by infra, only by
#: this module's own claim/completion logic.
_STATUS_ATTR = "status"
_STATUS_CLAIMED = "claimed"
_STATUS_COMPLETED = "completed"

#: TASK-006's key namespace. A future key kind (e.g. TASK-007's in-flight
#: lock) adds its own prefix + `_xxx_key()` helper alongside this one; the
#: generic primitives below never branch on key shape.
_EVENT_KEY_PREFIX = "event"


def _event_key(event_id: str) -> str:
    return f"{_EVENT_KEY_PREFIX}#{event_id}"


#: TASK-007's key namespace (``EDGE-SB-015``). Distinct prefix from
#: ``_EVENT_KEY_PREFIX`` so an in-flight lock and an event claim can never
#: collide even by coincidence of the raw id/ts value.
_INFLIGHT_KEY_PREFIX = "inflight"


def _inflight_key(user_id: str, thread_ts: str) -> str:
    # "#" is safe as the separator between `user_id` and `thread_ts` (as well
    # as after the prefix): a Slack user ID is `[A-Z0-9]+` (CTR-SB-006, e.g.
    # "U01ABCDEF") and a `thread_ts`/`ts` is `<digits>.<digits>` (Slack's
    # message timestamp format) — neither character set can ever contain
    # "#", so two distinct (user_id, thread_ts) pairs can never fold onto the
    # same key string here.
    return f"{_INFLIGHT_KEY_PREFIX}#{user_id}#{thread_ts}"


class IdempotencyStoreError(Exception):
    """A DynamoDB call failed for a reason other than the expected
    conditional-check outcome (table missing, throttled, network error, ...).

    Every public function in this module raises this — never a raw
    ``botocore`` exception — so a caller (``handler.py``/``worker.py``,
    later tasks) can catch exactly one type and decide how to degrade
    without needing to know botocore's exception hierarchy. This is what
    lets the handler keep ``AC-SB-002-3`` ("async dispatch failing still
    returns 200") even when the idempotency store itself is unreachable.
    """


#: Operator-diagnostic only (mirrors identity.py's reason_code pattern) —
#: nothing in this module surfaces `reason` to the Slack-facing caller.
#:
#: "store_error" is reserved for *callers* of this module (``handler.py``'s
#: ``_claim_or_fail_safe``) to use when constructing a ``ClaimOutcome`` after
#: catching this module's own ``IdempotencyStoreError`` — ``claim_event``
#: itself never returns it (a store failure is always raised, never
#: returned, see ``IdempotencyStoreError`` above). It must not be conflated
#: with ``"missing_event_id"``: that value means the DynamoDB call was never
#: attempted because ``event_id`` itself was unusable, a data-quality signal
#: with a completely different remediation than "the store did not
#: respond." Reusing ``"missing_event_id"`` for a store outage would let a
#: real DynamoDB failure masquerade as "events keep arriving without an
#: id" on any dashboard/metric built against this field.
ClaimReason = Literal["claimed", "duplicate", "missing_event_id", "store_error"]


@dataclass(frozen=True, slots=True)
class ClaimOutcome:
    """Result of one ``claim_event`` call.

    ``claimed=True``: this call is the sole winner of the race for
    ``event_id`` (``AC-SB-003-1``, ``AC-SB-003-2``) — the caller should
    dispatch work. ``claimed=False``: no work should start, for one of
    several reasons distinguished only for operator logs, never for caller
    branching: ``"duplicate"`` (``event_id`` was already claimed — a Slack
    retry, ``EDGE-SB-004``), ``"missing_event_id"`` (``event_id`` was
    ``None``/blank; no DynamoDB call was made at all), or ``"store_error"``
    (a caller wrapping this function caught ``IdempotencyStoreError`` — see
    ``ClaimReason``'s own comment; ``claim_event`` itself never produces this
    value, only raises).
    """

    claimed: bool
    reason: ClaimReason


#: Operator-diagnostic only, same intent as ``ClaimReason`` above but for
#: ``claim_inflight_query`` — a distinct type (rather than reusing
#: ``ClaimReason``) because the reasons genuinely differ: ``"in_progress"``
#: is a coalescing block (another query for this (user, thread) is still
#: running), not a duplicate delivery, and ``"identifiers_missing"`` means
#: "allowed to proceed" (``claimed=True``) rather than blocked — the
#: opposite polarity of ``ClaimReason``'s ``"missing_event_id"``.
#:
#: "store_error" exists for the same reason as ``ClaimReason``'s own value
#: (reserved for a caller — ``worker.py``'s ``_claim_inflight_or_fail_open``
#: — to use after catching ``IdempotencyStoreError``; ``claim_inflight_query``
#: itself never returns it). Without a dedicated value, that caller would
#: have to reuse either ``"identifiers_missing"`` (implies the pair was
#: simply unusable, not that the store failed) or ``"in_progress"`` (implies
#: another query is *known* to be running, which an unreachable store cannot
#: confirm) — both would hide a genuine DynamoDB outage behind an unrelated,
#: benign-sounding label.
InflightClaimReason = Literal["claimed", "in_progress", "identifiers_missing", "store_error"]


@dataclass(frozen=True, slots=True)
class InflightClaimOutcome:
    """Result of one ``claim_inflight_query`` call (``EDGE-SB-015``).

    ``claimed=True``: no other query is in flight for this (user_id,
    thread_ts) — the caller should proceed and must eventually call
    ``release_inflight_query`` for the same pair. ``claimed=False``
    (``reason="in_progress"``): another query for the same pair is still
    running; the caller should acknowledge receipt without starting new
    work. ``reason="identifiers_missing"`` always pairs with
    ``claimed=True`` — coalescing is a cost optimization, not a security
    boundary, so a missing ``user_id``/``thread_ts`` (coalescing can't be
    evaluated) must never block a legitimate query. ``reason="store_error"``
    also always pairs with ``claimed=True`` for the same fail-open reason,
    but is only ever constructed by a caller wrapping this function after
    catching ``IdempotencyStoreError`` — see ``InflightClaimReason``'s own
    comment; ``claim_inflight_query`` itself never produces this value.
    """

    claimed: bool
    reason: InflightClaimReason


_default_client: DynamoDBClient | None = None


def _resolve_client(client: DynamoDBClient | None) -> DynamoDBClient:
    """Return ``client`` if given, else the lazily-built, warm-cached default.

    Only ``None`` (production callers) ever reaches the caching branch —
    tests always inject a ``moto``-backed client explicitly, so they never
    touch or depend on this module-global cache.
    """
    global _default_client
    if client is not None:
        return client
    if _default_client is None:
        _default_client = boto3.client("dynamodb")  # pyright: ignore[reportUnknownMemberType]
    return _default_client


def claim_event(
    event_id: str | None,
    *,
    table_name: str,
    ttl_seconds: int = IDEMPOTENCY_TTL_SECONDS_DEFAULT,
    client: DynamoDBClient | None = None,
) -> ClaimOutcome:
    """Atomically claim ``event_id`` for processing (``REQ-SB-003``, ``AC-SB-003-1``).

    ``event_id`` is the value already returned by ``slack/events.py``'s
    ``extract_event_id`` — pass ``None``/empty straight through; this
    function treats a missing ``event_id`` as automatically unclaimable
    (``ClaimOutcome(claimed=False, reason="missing_event_id")``) without
    making any DynamoDB call, rather than pushing that decision onto the
    caller.

    A single conditional ``PutItem`` decides the outcome (``AC-SB-003-2``):
    no prior read, so two concurrent calls for the same ``event_id`` can
    never both succeed — DynamoDB itself rejects the loser with
    ``ConditionalCheckFailedException``, which this function turns into a
    plain ``claimed=False`` result rather than raising.

    Raises ``IdempotencyStoreError`` for any other DynamoDB failure (the
    table doesn't exist, throttling, a network error, ...).
    """
    # Whitespace counts as missing. ``extract_event_id`` passes a whitespace-only
    # value through unchanged (it is a non-empty ``str``), and without this check a
    # blank id would become the storage key ``event#   `` — every malformed event
    # would then collide on that one row and all but the first would be dropped as
    # "duplicates". Slack does not send such ids, but the guard above exists to
    # reject unusable ones, and a blank id is unusable by that same standard.
    if not event_id or not event_id.strip():
        logger.warning("claim_event called with no usable event_id — treating as unclaimable")
        return ClaimOutcome(claimed=False, reason="missing_event_id")

    resolved_client = _resolve_client(client)
    claimed = _claim(resolved_client, table_name, _event_key(event_id), ttl_seconds)
    if claimed:
        return ClaimOutcome(claimed=True, reason="claimed")

    logger.info("duplicate event_id claim rejected (event_id=%s)", event_id)
    return ClaimOutcome(claimed=False, reason="duplicate")


def mark_event_completed(
    event_id: str,
    *,
    table_name: str,
    ttl_seconds: int = IDEMPOTENCY_TTL_SECONDS_DEFAULT,
    client: DynamoDBClient | None = None,
) -> None:
    """Record that ``event_id``'s reply has been posted (``EDGE-SB-005``).

    The worker calls this once it has finished posting to Slack, so a
    retried async invoke of the *same* ``event_id`` (Lambda's own 2 retries
    on a failed/timed-out invocation — a duplication cause independent of
    Slack's own retries, which the handler-side ``claim_event`` cannot see)
    can check ``is_event_completed`` first and skip posting again.

    A no-op for a missing/blank ``event_id`` — defensive, mirrors
    ``claim_event``'s handling; no DynamoDB call is made in that case.

    Raises ``IdempotencyStoreError`` on any DynamoDB failure.
    """
    if not event_id:
        return
    _mark_completed(_resolve_client(client), table_name, _event_key(event_id), ttl_seconds)


def is_event_completed(
    event_id: str,
    *,
    table_name: str,
    client: DynamoDBClient | None = None,
) -> bool:
    """Return True iff ``mark_event_completed(event_id, ...)`` already ran.

    ``EDGE-SB-005``'s worker-side check: call this before doing any Claude
    API work or posting a reply. Returns False (never raises for this case)
    for a missing/blank ``event_id``, and False for an ``event_id`` that was
    only ``claim_event``-claimed but never completed — both mean "safe to
    proceed."

    Raises ``IdempotencyStoreError`` on any DynamoDB failure.
    """
    if not event_id:
        return False
    return _is_completed(_resolve_client(client), table_name, _event_key(event_id))


def claim_inflight_query(
    user_id: str | None,
    thread_ts: str | None,
    *,
    table_name: str,
    ttl_seconds: int = INFLIGHT_TTL_SECONDS_DEFAULT,
    client: DynamoDBClient | None = None,
) -> InflightClaimOutcome:
    """Atomically claim the (``user_id``, ``thread_ts``) pair as "in flight" (``EDGE-SB-015``).

    ``user_id``/``thread_ts`` are the values ``slack/events.py``'s
    ``extract_user_id``/``extract_reply_target_ts`` already return — pass
    them straight through. **Either being ``None``/blank means "coalescing
    can't be evaluated," not "block."** Coalescing exists purely to save
    duplicate Claude API spend on same-user repeat mentions — it is not a
    security boundary — so this function always lets the query proceed
    (``InflightClaimOutcome(claimed=True, reason="identifiers_missing")``)
    without making any DynamoDB call when either identifier is unusable,
    rather than silently dropping a legitimate question.

    Otherwise, exactly one conditional ``PutItem`` (the same ``_claim``
    primitive ``claim_event`` uses) decides the outcome — two concurrent
    mentions from the same user in the same thread can never both win.

    A caller that receives ``claimed=True`` **must** eventually call
    ``release_inflight_query`` with the same ``user_id``/``thread_ts``
    (typically from a ``try``/``finally`` around the reply-posting work) —
    see ``INFLIGHT_TTL_SECONDS_DEFAULT`` for what happens if it never does.

    Raises ``IdempotencyStoreError`` for any other DynamoDB failure.
    """
    if not user_id or not user_id.strip() or not thread_ts or not thread_ts.strip():
        logger.info("coalescing skipped — user_id or thread_ts unavailable, letting query proceed")
        return InflightClaimOutcome(claimed=True, reason="identifiers_missing")

    resolved_client = _resolve_client(client)
    claimed = _claim(resolved_client, table_name, _inflight_key(user_id, thread_ts), ttl_seconds)
    if claimed:
        return InflightClaimOutcome(claimed=True, reason="claimed")

    logger.info(
        "in-flight query already exists (user_id=%s, thread_ts=%s) — coalescing", user_id, thread_ts
    )
    return InflightClaimOutcome(claimed=False, reason="in_progress")


def release_inflight_query(
    user_id: str | None,
    thread_ts: str | None,
    *,
    table_name: str,
    client: DynamoDBClient | None = None,
) -> None:
    """Release a (``user_id``, ``thread_ts``) pair previously claimed by ``claim_inflight_query``.

    The worker calls this from a ``try``/``finally`` once the reply has been
    posted — success or failure — so the *next* mention from that same
    (user_id, thread_ts) is not coalescing-blocked. This is a plain delete
    (not a status update like ``mark_event_completed``'s): the row must
    disappear, not merely change state, because ``claim_inflight_query``'s
    ``_claim`` call only succeeds against an *absent* key
    (``attribute_not_exists``) — leaving the row behind with a "done" status
    would permanently lock out that (user_id, thread_ts) pair.

    A no-op for a missing/blank ``user_id``/``thread_ts`` (mirrors
    ``claim_inflight_query``'s handling — nothing was ever claimed for an
    unusable pair, so there is nothing to release) and safe to call more
    than once for the same pair (deleting an already-absent key is a
    successful no-op in DynamoDB, not an error).

    Raises ``IdempotencyStoreError`` on any DynamoDB failure.
    """
    if not user_id or not user_id.strip() or not thread_ts or not thread_ts.strip():
        return
    _release(_resolve_client(client), table_name, _inflight_key(user_id, thread_ts))


# --- generic, key-scheme-agnostic primitives --------------------------------
# TASK-007 (EDGE-SB-015's in-flight coalescing lock) reuses `_claim` (to
# lock) and adds `_release` (to unlock) under its own key prefix; nothing
# below knows what "event" or "in-flight query" means.


def _claim(client: DynamoDBClient, table_name: str, key: str, ttl_seconds: int) -> bool:
    """One conditional ``PutItem``. True = newly claimed, False = ``key`` already existed.

    The single round trip *is* ``AC-SB-003-2``'s atomicity guarantee — see
    the module docstring. Never split into a read followed by a write.
    """
    try:
        client.put_item(
            TableName=table_name,
            Item={
                PARTITION_KEY_ATTR: {"S": key},
                _STATUS_ATTR: {"S": _STATUS_CLAIMED},
                TTL_ATTRIBUTE: {"N": str(_epoch_ttl(ttl_seconds))},
            },
            ConditionExpression=f"attribute_not_exists({PARTITION_KEY_ATTR})",
        )
    except client.exceptions.ConditionalCheckFailedException:
        return False
    except (BotoCoreError, ClientError) as exc:
        raise IdempotencyStoreError(f"claim failed for table {table_name!r}") from exc
    return True


def _mark_completed(client: DynamoDBClient, table_name: str, key: str, ttl_seconds: int) -> None:
    """Set ``key``'s status to completed, creating the item if ``_claim`` never ran for it.

    Deliberately unconditional (plain ``UpdateExpression``, no
    ``ConditionExpression``): unlike ``_claim``, this is not a race the
    caller needs decided atomically — calling it more than once for the same
    ``key`` is a harmless no-op overwrite, which is exactly what makes a
    retried worker invocation safe to call this again.
    """
    try:
        client.update_item(
            TableName=table_name,
            Key={PARTITION_KEY_ATTR: {"S": key}},
            UpdateExpression="SET #status = :status, #ttl = :ttl",
            ExpressionAttributeNames={"#status": _STATUS_ATTR, "#ttl": TTL_ATTRIBUTE},
            ExpressionAttributeValues={
                ":status": {"S": _STATUS_COMPLETED},
                ":ttl": {"N": str(_epoch_ttl(ttl_seconds))},
            },
        )
    except (BotoCoreError, ClientError) as exc:
        raise IdempotencyStoreError(f"mark_completed failed for table {table_name!r}") from exc


def _is_completed(client: DynamoDBClient, table_name: str, key: str) -> bool:
    try:
        response = client.get_item(TableName=table_name, Key={PARTITION_KEY_ATTR: {"S": key}})
    except (BotoCoreError, ClientError) as exc:
        raise IdempotencyStoreError(f"is_completed check failed for table {table_name!r}") from exc

    item = response.get("Item")
    if item is None:
        return False
    status_value = item.get(_STATUS_ATTR)
    if status_value is None:
        return False
    return status_value.get("S") == _STATUS_COMPLETED


def _release(client: DynamoDBClient, table_name: str, key: str) -> None:
    """Unconditionally delete ``key`` so a future ``_claim`` for it can win again.

    Deliberately unconditional (no ``ConditionExpression``): deleting an
    already-absent key is a successful no-op in DynamoDB, which is exactly
    what makes this safe to call more than once for the same key (a
    ``try``/``finally`` release that runs after the item was already
    deleted, or two racing releases).
    """
    try:
        client.delete_item(TableName=table_name, Key={PARTITION_KEY_ATTR: {"S": key}})
    except (BotoCoreError, ClientError) as exc:
        raise IdempotencyStoreError(f"release failed for table {table_name!r}") from exc


def _epoch_ttl(ttl_seconds: int) -> int:
    """``AC-SB-003-3``/``CTR-SB-007``: TTL is always written as ``now + ttl_seconds``,
    epoch seconds, Number type — never as a duration or as any other unit."""
    return int(time.time()) + ttl_seconds
