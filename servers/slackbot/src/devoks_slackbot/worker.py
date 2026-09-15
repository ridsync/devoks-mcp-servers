"""ASGI composition root for the ``slack-worker`` Lambda entry point (TASK-014).

``create_app(...) -> Starlette`` is the second factory this image exposes --
same image as ``handler.py`` (``TASK-012``), a different Lambda
(``ImageConfig.Command`` points each Lambda at its own factory, PLAN §1's
"진입점 분기 메커니즘"). Importing this module *does* pull in ``anthropic``
(``DSN-SB-002``) -- that is expected and correct here, unlike ``handler.py``
(``DSN-SB-008``): this factory backs a Lambda with no 3-second ACK budget at
all, only ``CTR-SB-009``'s 300s/1024MB.

🔴 **worker is not an HTTP trigger.** Per the AWS Lambda Web Adapter's own
README (``AWS_LWA_PASS_THROUGH_PATH``, default ``"/events"``): *"the path
for receiving event payloads from non-http triggers"*. ``handler.py``
dispatches here with a ``boto3`` ``Invoke(InvocationType="Event")`` whose
``Payload`` is the **original, unparsed** Slack request body (see
``handler.py``'s ``_dispatch_to_worker``) -- LWA hands that exact payload to
this app as the POST body at ``EVENTS_PATH``. ``EVENTS_PATH`` is a module
constant (never inlined) so ``TASK-020`` (Dockerfile's
``AWS_LWA_PASS_THROUGH_PATH``) and ``TASK-023`` (Lambda environment
variables) reference the same string without a chance to drift.

``HEALTHZ_PATH`` exists for the same reason ``handler.py`` has one: the
Dockerfile's ``AWS_LWA_READINESS_CHECK_PATH`` (``TASK-020``) needs a route to
poll, or Lambda Web Adapter never considers this container ready.

Processing order (FRD §4.1 worker ①~⑥, this workspace's ``context`` handover
note's more granular ①~⑨, and §5.4's state table) -- **do not reorder**:

1. **Worker-side idempotency** (``idempotency.is_event_completed``,
   ``EDGE-SB-005``). Lambda's own async-invoke retry (2 more attempts,
   independent of Slack's own retries that ``handler.py``'s ``claim_event``
   already defends against) can re-deliver the *same* ``event_id`` to this
   Lambda. Already completed -> return without posting anything -- only the
   worker can see this cause of duplication.
2. **Credential lookup** (``identity.resolve_credentials``, ``EDGE-SB-006``,
   ``AC-SB-004-2``). ``user_id`` comes from ``slack/events.py``'s
   ``extract_user_id`` -- never read from the payload directly here
   (``EDGE-SB-019``'s localization stays intact). Not granted -> post the
   fixed denial notice, done -- no Claude call, no coalescing lock touched.
3. **In-flight coalescing** (``idempotency.claim_inflight_query``,
   ``EDGE-SB-015``). Not claimed (another query for this
   ``(user_id, thread_ts)`` is already running) -> acknowledge receipt only,
   no new Claude call. ⚠️ Once claimed, ``release_inflight_query`` **must**
   run no matter how processing exits from here on -- the ``try``/
   ``finally`` in ``_process_event`` around ``_run_query``. Skipping it locks
   that pair out of new questions for ``INFLIGHT_TTL_SECONDS_DEFAULT`` (360s)
   -- this task's own "가장 중요" requirement.
4. **Acknowledgement post** (``slack/client.ACKNOWLEDGEMENT_MESSAGE``,
   ``AC-SB-006-4``) -- posted before the slow step so the user knows their
   question was received.
5. **``ask_claude``** -- this module builds and injects the
   ``anthropic.AsyncAnthropic`` client (``ask.py`` never reads
   ``ANTHROPIC_API_KEY`` itself, by design).
6. **Length policy** (``slack/format.apply_response_length_policy``,
   ``AC-SB-006-2``).
7. **Thread reply** (``slack/client.post_message``, ``thread_ts`` from
   ``extract_reply_target_ts``).
8. **Completion record** (``idempotency.mark_event_completed``) -- run after
   *every* terminal branch that posts something for this ``event_id``
   (denial, coalesced-acknowledge-only, Claude error/refusal, and success
   alike), not only the success path: each of those branches is itself a
   completed unit of work for ``EDGE-SB-005``'s purposes -- a Lambda retry of
   the *same* ``event_id`` must not re-post whichever message was already
   posted, regardless of which branch produced it.
9. **Observation record** (``observability.emit_query_observation``) -- this
   module measures ``duration_ms`` (``time.monotonic()`` deltas) and
   generates ``request_id`` (``uuid.uuid4().hex``); ``observability.py``
   itself reads no clock (its own module docstring). ``CTR-SB-008``: if the
   *final answer* post (step 7) itself fails, the record's ``outcome`` is
   downgraded from ``"ok"`` to ``"error"`` -- a delivered-nowhere answer must
   never aggregate as a success. See ``_post_and_finish``'s docstring for the
   full reasoning and why the acknowledgement post (step 4) is exempt.

``EDGE-SB-013`` -- a Claude API failure/timeout (``ask_result.outcome ==
"error"``) posts ``ask_result.client_message`` (the secret-free, retry-aware
notice ``ask.py`` already built) instead of leaving the user with only the
acknowledgement, waiting forever. ``EDGE-SB-008`` -- this module never adds
its own retry loop around ``ask_claude``; the Anthropic SDK's own
``max_retries`` (owned by the client this module constructs) already retried
before returning, and ``AskResult.retryable`` is read-only information for
the observation record, not an instruction to loop again.

**Any exception this module doesn't expect** -- not one of ``ask_claude``'s
or ``post_message``'s own never-raises contracts, but a genuine bug or an
``IdempotencyStoreError`` surfacing from a completion-record write -- is
caught around the whole claimed-and-processing block in ``_process_event``: a
best-effort generic failure notice is posted, the exception is logged (never
the question/answer/token), and ``release_inflight_query`` still runs from
``finally`` regardless. A caller left with only the acknowledgement and no
resolution otherwise waits forever -- "어떤 실패에도 worker가 조용히 죽으면 안
된다" in this task's handover notes.

🔴 SDK DEBUG logging trap (PLAN §1) -- same guard as ``handler.py``'s
``_configure_logging``, duplicated here rather than shared (PLAN §1
explicitly allows either approach, and both entry points must independently
guarantee this invariant): ``anthropic._base_client`` logs full request
bodies -- per-person MCP tokens and question text included -- at DEBUG. This
module's own log calls never do (see "Security" below); the SDK's internal
logger is the actual risk, and setting ``SLACKBOT_LOG_LEVEL=DEBUG`` for
incident response must not be able to raise it above INFO.

Mention-token stripping (``"<@BOT_USER_ID> question"`` -> ``"question"``)
------------------------------------------------------------------------
Slack's real ``app_mention`` payload's ``event.text`` includes the
triggering mention verbatim (confirmed against the official example payload
this package's test suite already pins, e.g.
``"<@U0LAN0Z89> is it everything a river should be?"``). Sending that literal
Slack markup to Claude as part of the question is noise unrelated to the
user's actual intent -- Claude would see its own caller's ID syntax instead
of a clean natural-language question. This module strips it
(``_strip_bot_mention``) before calling ``ask_claude`` so the question Claude
receives matches what the user actually meant to ask.
``slack/events.py``'s ``extract_question_text`` returns the **raw** text
unmodified (payload parsing is that module's job, ``DSN-SB-007``); the
stripping itself is interpretation, not parsing, so it lives here instead --
see that function's own docstring for the same reasoning from the other side.

Config gap this task had to close (``IDEMPOTENCY_TABLE``)
------------------------------------------------------------------------
FRD §5.2 originally scoped ``IDEMPOTENCY_TABLE`` to handler only. Steps 1 and
3 above need it too (both call into ``idempotency.py`` against the same
table). ``config.py`` now requires it for both roles -- see that module's own
docstring for the full reasoning; the same shape of gap ``TASK-012`` already
closed once for ``WORKER_FUNCTION_NAME``.

Security (mirrors ``ask.py``/``slack/client.py``/``observability.py``'s own
sections -- this module is the one place all four secrets could accidentally
collide in a single log line, so it repeats the invariant explicitly)
------------------------------------------------------------------------
The Anthropic API key, every person's MCP token, the Slack bot token, and the
raw question/answer text must never reach a log line, an exception message
this module constructs, or a ``repr``/``str`` of anything this module builds.
This module's own log calls name only ``event_id``, ``user_id``, status/
reason codes, and lengths -- never a token, a question, or an answer. The
observation record (step 9) carries only ``question_len``/``question_sha256``
(``AC-SB-007-3``), never the question itself -- enforced by
``observability.build_record``, not re-implemented here. The unregistered-
user denial text is ``identity.py``'s own fixed string (``AC-SB-004-3``),
used verbatim, never derived from this module's own knowledge of the mapping.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, cast

import anthropic
import httpx2
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from .ask import ask_claude
from .config import WorkerSettings, load_worker_settings
from .idempotency import (
    IdempotencyStoreError,
    InflightClaimOutcome,
    claim_inflight_query,
    is_event_completed,
    mark_event_completed,
    release_inflight_query,
)
from .identity import CredentialLookupResult, resolve_credentials
from .observability import Outcome, emit_query_observation
from .slack.client import ACKNOWLEDGEMENT_MESSAGE, PostMessageResult, post_message
from .slack.events import (
    extract_channel,
    extract_event_id,
    extract_question_text,
    extract_reply_target_ts,
    extract_user_id,
)
from .slack.format import apply_response_length_policy

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient

#: The endpoint closure shape ``_make_events_endpoint`` returns -- mirrors
#: ``handler.py``'s ``_Endpoint`` alias for the same readability reason.
_Endpoint = Callable[[Request], Awaitable[Response]]

logger = logging.getLogger(__name__)

#: AWS_LWA_PASS_THROUGH_PATH's own default -- see module docstring's "worker
#: is not an HTTP trigger" section. TASK-020/TASK-023 import this constant.
EVENTS_PATH: Final[str] = "/events"

#: Dockerfile's AWS_LWA_READINESS_CHECK_PATH (TASK-020) must point here.
HEALTHZ_PATH: Final[str] = "/healthz"

#: PLAN §1's SDK DEBUG logging trap -- see module docstring.
_NOISY_SDK_LOGGER_NAMES: Final[tuple[str, ...]] = ("anthropic", "httpx2")
_NOISY_SDK_LOGGER_MIN_LEVEL: Final[int] = logging.INFO

#: See module docstring's "Mention-token stripping" section. Formatted with
#: the settings-supplied bot_user_id per call, not module-level (the bot's
#: own ID is a runtime value, not a constant).
_BOT_MENTION_TEMPLATE: Final[str] = r"<@{bot_user_id}>\s*"

#: Posted when this module hits an exception outside every documented
#: never-raises contract it relies on (ask_claude/post_message/identity's own
#: functions) -- see module docstring's "Any exception this module doesn't
#: expect" section. Never leaks any detail about what actually broke.
_GENERIC_ERROR_MESSAGE: Final[str] = (
    "요청을 처리하는 중 예기치 못한 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
)

#: Observability reason_code for a Claude stop_reason == "refusal" outcome
#: (AC-SB-005-4) -- distinct from identity.py's "user_unregistered"/
#: "user_unidentified" so an operator can tell "Claude declined this specific
#: question" from "this person was never granted access" even though both
#: currently share observability's outcome="denied" bucket (CTR-SB-008 only
#: defines three outcome values -- see _run_query).
_REASON_CLAUDE_REFUSAL: Final[str] = "claude_refusal"

#: EDGE-SB-015: this event's only action was "acknowledge receipt, another
#: query for this (user_id, thread_ts) is already running" -- distinct from
#: idempotency.py's own InflightClaimReason values, which are internal to
#: that module's own return type.
_REASON_COALESCED: Final[str] = "coalesced_in_progress"


def _configure_logging(log_level: str) -> None:
    """Set the root logger's threshold, then re-pin the noisy SDK loggers above it.

    Identical logic to ``handler.py``'s function of the same name -- see that
    module's docstring for why order matters (a logger with no explicit
    level of its own inherits whatever the operator just raised the root to).
    Duplicated rather than shared (PLAN §1 explicitly allows either); this is
    the copy that guarantees the invariant for *this* Lambda regardless of
    which entry point a given process happens to configure logging from
    first.
    """
    logging.getLogger().setLevel(log_level)
    for name in _NOISY_SDK_LOGGER_NAMES:
        logging.getLogger(name).setLevel(_NOISY_SDK_LOGGER_MIN_LEVEL)


async def _healthz(request: Request) -> Response:
    """GET /healthz -- unauthenticated LWA readiness probe (TASK-020). Mirrors handler.py's twin."""
    return JSONResponse({"status": "ok"})


def _strip_bot_mention(raw_text: str, *, bot_user_id: str) -> str:
    """Remove every ``<@bot_user_id>`` mention token from ``raw_text`` and trim.

    See module docstring's "Mention-token stripping" section for why this
    happens here rather than in ``slack/events.py``. Removes every
    occurrence (not just a leading one) -- defensive against a question that
    happens to re-mention the bot mid-sentence, not only at the start.
    """
    pattern = _BOT_MENTION_TEMPLATE.format(bot_user_id=re.escape(bot_user_id))
    return re.sub(pattern, "", raw_text).strip()


def _log_post_failure(result: PostMessageResult, *, event_id: str | None, stage: str) -> None:
    """``EDGE-SB-020``: log the classified reason (never the message text) when a post fails.

    ``slack/client.py`` already logs its own warning with the same
    ``reason_code`` -- this is worker's own record, scoped by ``stage``
    (which branch produced this post: ``"acknowledgement"``, an outcome
    name, or ``"unexpected_error"``) and ``event_id``, so an operator reading
    worker's own logs can see which event failed to post and why without
    cross-referencing ``slack/client.py``'s.
    """
    if result.ok:
        return
    logger.error(
        "slack post failed (stage=%s, event_id=%s, reason_code=%s, retryable=%s)",
        stage,
        event_id,
        result.reason_code,
        result.retryable,
    )


def _is_completed_or_fail_open(
    event_id: str | None, *, settings: WorkerSettings, client: DynamoDBClient | None
) -> bool:
    """Wrap ``idempotency.is_event_completed`` so a store outage never silences a new event.

    Fails open (returns False -- "not completed, proceed") on
    ``IdempotencyStoreError``. The alternative (fail-closed: treat an
    unreadable store as "already completed") would silently drop a
    legitimate new question with no notification at all -- the one outcome
    this task's handover notes call out as strictly worse than the rare
    double-post risk this choice accepts ("어떤 실패에도 worker가 조용히 죽으면
    안 된다").
    """
    if not event_id:
        return False
    try:
        return is_event_completed(event_id, table_name=settings.idempotency_table, client=client)
    except IdempotencyStoreError:
        logger.error(
            "idempotency store unreachable checking completion (event_id=%s) -- "
            "proceeding as not-completed (fail-open)",
            event_id,
            exc_info=True,
        )
        return False


def _claim_inflight_or_fail_open(
    user_id: str | None,
    thread_ts: str | None,
    *,
    settings: WorkerSettings,
    client: DynamoDBClient | None,
) -> InflightClaimOutcome:
    """Wrap ``idempotency.claim_inflight_query`` so a store outage never blocks a question.

    Fails open (``claimed=True``) on ``IdempotencyStoreError`` -- mirrors
    ``claim_inflight_query``'s own ``identifiers_missing`` stance (coalescing
    is a cost optimization, not a security boundary; see that function's own
    docstring): if the store can't be consulted, let the question through
    rather than silently dropping it.

    The returned ``reason="store_error"`` (never ``"identifiers_missing"``,
    even though ``user_id``/``thread_ts`` may well both be present here) is
    what lets an operator tell "the store itself is unreachable" apart from
    "coalescing simply wasn't evaluable for this pair" -- see
    ``idempotency.InflightClaimReason``'s own comment for why conflating the
    two would be a real diagnostic regression, not a cosmetic one.
    """
    try:
        return claim_inflight_query(
            user_id, thread_ts, table_name=settings.idempotency_table, client=client
        )
    except IdempotencyStoreError:
        logger.error(
            "idempotency store unreachable claiming in-flight lock (user_id=%s) -- "
            "letting the query proceed (fail-open, coalescing is cost-only)",
            user_id,
            exc_info=True,
        )
        return InflightClaimOutcome(claimed=True, reason="store_error")


def _release_inflight_or_log(
    user_id: str | None,
    thread_ts: str | None,
    *,
    settings: WorkerSettings,
    client: DynamoDBClient | None,
) -> None:
    """Best-effort ``release_inflight_query`` -- never lets any exception escape ``finally``.

    Called from the ``try``/``finally`` in ``_process_event`` -- see this
    task's own "가장 중요" requirement. A failure here is logged, never
    raised.

    🔴 Catches bare ``Exception``, wider than every other idempotency wrapper
    in this module (which only catch ``IdempotencyStoreError``) -- and wider
    than the usual style rule this codebase otherwise follows. That is
    deliberate here, not an oversight: this call runs *inside a ``finally``
    block*, and Python's own semantics say an exception raised while a
    ``finally`` is executing **replaces** whatever exception was already
    propagating out of the ``try`` above it (``_run_query``'s own failure,
    if any). ``_process_event``'s ``except Exception`` only wraps the
    ``try``, not this ``finally`` -- there is no outer handler left to catch
    a narrower ``except`` failing here, so a non-``IdempotencyStoreError``
    bug in ``release_inflight_query``/``_release`` (a bare botocore
    exception is already turned into ``IdempotencyStoreError`` by that
    layer; only a genuine bug reaching past it would hit this branch) would
    both erase the original error *and* leave the in-flight lock unreleased
    for ``INFLIGHT_TTL_SECONDS_DEFAULT`` (360s) -- the exact "조용히 죽으면 안
    된다" failure this task exists to close, and the one remaining path that
    could still produce it. Logging (``exc_info=True``, never the
    question/answer/tokens this module's own "Security" section forbids) is
    all this function can still do about an unreachable store or an
    unexpected bug; ``release_inflight_query``'s own TTL-based safety net
    still bounds the lockout even when this call never succeeds.
    """
    try:
        release_inflight_query(
            user_id, thread_ts, table_name=settings.idempotency_table, client=client
        )
    except Exception:
        logger.error(
            "releasing in-flight lock failed unexpectedly (user_id=%s)",
            user_id,
            exc_info=True,
        )


def _safe_mark_completed(
    event_id: str | None, *, settings: WorkerSettings, client: DynamoDBClient | None
) -> None:
    """Best-effort ``mark_event_completed`` -- a failure here must not stop the observation record.

    The Slack post this pairs with (``_post_and_finish``) has already
    happened by the time this runs -- the user already has their answer
    either way. A failure here only risks a possible duplicate post on a
    future Lambda async-invoke retry of the same ``event_id``, not a missed
    reply, so it is logged and swallowed rather than raised.

    🔴 Catches bare ``Exception`` (widened for the same reason as
    ``_release_inflight_or_log``'s own widened ``except``, which explains the
    general rule this repeats): this function is reached both from ordinary
    flow (``_post_and_finish``, itself inside ``_process_event``'s ``try``,
    where a narrower ``except`` failing here would still be caught one level
    up) *and* directly from ``_process_event``'s own ``except Exception as
    exc:`` branch -- its last-resort call after an already-unexpected
    failure, with no further ``try`` around it. A non-``IdempotencyStoreError``
    bug surfacing there would propagate straight out of ``_process_event``,
    past the ``_events`` endpoint's own missing ``try``/``except``, into a
    bare ASGI 500 -- the same "worker가 조용히 죽으면 안 된다" outcome
    ``_release_inflight_or_log``'s docstring describes, just reached from a
    different call site.
    """
    if not event_id:
        return
    try:
        mark_event_completed(
            event_id,
            table_name=settings.idempotency_table,
            ttl_seconds=settings.idempotency_ttl_seconds,
            client=client,
        )
    except Exception:
        logger.error(
            "marking event completed failed unexpectedly (event_id=%s)",
            event_id,
            exc_info=True,
        )


async def _post_and_finish(
    *,
    channel: str,
    thread_ts: str | None,
    text: str,
    settings: WorkerSettings,
    http_client: httpx2.AsyncClient | None,
    event_id: str | None,
    idempotency_client: DynamoDBClient | None,
    request_id: str,
    user_id: str | None,
    client_id: str | None,
    question: str,
    outcome: Outcome,
    reason_code: str | None,
    error_kind: str | None,
    started_monotonic: float,
    usage: dict[str, int | None] | None,
) -> None:
    """Post ``text``, mark ``event_id`` completed, and emit one observation record.

    The shared tail every terminal branch in ``_process_event``/``_run_query``
    reaches (module docstring's steps 7~9). ``mark_event_completed`` runs
    regardless of whether the post itself succeeded -- ``EDGE-SB-020`` is a
    configuration problem (bot not invited) a retry cannot fix either, so
    there is nothing to gain by leaving this ``event_id`` unmarked and every
    reason to avoid a duplicate post attempt on a Lambda retry.

    ``CTR-SB-008`` correction (2026-09-14 coordinator follow-up): if ``text``
    was the *final answer* (``outcome == "ok"``, the only call site that ever
    passes it) and the post itself failed, the observation record must not
    say ``"ok"`` -- a caller aggregating by ``outcome`` would otherwise count
    an answer that never reached the channel (``not_in_channel``, a network
    error, ...) as a success, and the only way to discover the outage would
    be reading free-text logs one line at a time instead of querying the
    structured field ``REQ-SB-007`` exists for. So this function -- not its
    caller -- downgrades to ``outcome="error"`` and carries
    ``PostMessageResult.reason_code`` (e.g. ``"not_in_channel"``) in
    ``reason_code``, mirroring how ``ask_claude`` failures are already
    recorded elsewhere in this module (a classification string, not a Python
    exception -- ``error_kind`` stays reserved for an actual exception class
    name, see ``_process_event``'s ``except`` branch).

    This deliberately does **not** apply to the *acknowledgement* post in
    ``_run_query`` -- that call never reaches this function at all (it is
    posted, logged, and forgotten before ``ask_claude`` even runs). If only
    the acknowledgement failed but the real answer later posted fine, the
    user still received their answer -- the correct outcome from their point
    of view is success, and CTR-SB-008's per-query aggregate should say so.

    ``usage`` is recorded exactly as given, independent of whether this post
    succeeds -- the Claude API call (if one happened) already cost money by
    the time this function runs, and cost aggregation is this record's other
    purpose (``AC-SB-007-2``); a post-delivery failure does not refund that
    spend.
    """
    result = await post_message(
        channel=channel,
        text=text,
        bot_token=settings.bot_token,
        thread_ts=thread_ts,
        http_client=http_client,
    )
    _log_post_failure(result, event_id=event_id, stage=outcome)
    _safe_mark_completed(event_id, settings=settings, client=idempotency_client)

    effective_outcome: Outcome = outcome
    effective_reason_code = reason_code
    if outcome == "ok" and not result.ok:
        effective_outcome = "error"
        effective_reason_code = result.reason_code

    duration_ms = int((time.monotonic() - started_monotonic) * 1000)
    emit_query_observation(
        ts=datetime.now(UTC).isoformat(),
        slack_user_id=user_id,
        client_id=client_id,
        channel=channel,
        thread_ts=thread_ts,
        question=question,
        outcome=effective_outcome,
        reason_code=effective_reason_code,
        error_kind=error_kind,
        duration_ms=duration_ms,
        usage=usage,
        request_id=request_id,
    )


async def _run_query(
    *,
    channel: str,
    thread_ts: str | None,
    question: str,
    credential: CredentialLookupResult,
    settings: WorkerSettings,
    anthropic_client: anthropic.AsyncAnthropic,
    http_client: httpx2.AsyncClient | None,
    event_id: str | None,
    idempotency_client: DynamoDBClient | None,
    request_id: str,
    user_id: str | None,
    started_monotonic: float,
) -> None:
    """Steps 4~9 for a claimed (non-coalesced) query -- only reached once the lock is held.

    ``credential.mcp_token`` is asserted non-``None`` via ``cast`` -- safe
    because ``_process_event`` only calls this function after confirming
    ``credential.granted`` is True, and ``CredentialLookupResult`` guarantees
    ``mcp_token`` is set exactly when ``granted`` is True (see that
    dataclass's own docstring).
    """
    ack_result = await post_message(
        channel=channel,
        text=ACKNOWLEDGEMENT_MESSAGE,
        bot_token=settings.bot_token,
        thread_ts=thread_ts,
        http_client=http_client,
    )
    _log_post_failure(ack_result, event_id=event_id, stage="acknowledgement")

    ask_result = await ask_claude(
        question=question,
        mcp_server_url=settings.mcp_server_url,
        authorization_token=cast(str, credential.mcp_token),
        client=anthropic_client,
    )

    if ask_result.outcome == "error":
        # EDGE-SB-013 / EDGE-SB-008: post the SDK-retry-aware notice ask.py
        # already built -- no retry loop of our own around ask_claude.
        await _post_and_finish(
            channel=channel,
            thread_ts=thread_ts,
            text=ask_result.client_message or _GENERIC_ERROR_MESSAGE,
            settings=settings,
            http_client=http_client,
            event_id=event_id,
            idempotency_client=idempotency_client,
            request_id=request_id,
            user_id=user_id,
            client_id=user_id,
            question=question,
            outcome="error",
            reason_code=ask_result.reason_code,
            error_kind=None,
            started_monotonic=started_monotonic,
            usage=ask_result.usage,
        )
        return

    if ask_result.outcome == "refused":
        await _post_and_finish(
            channel=channel,
            thread_ts=thread_ts,
            text=ask_result.client_message or _GENERIC_ERROR_MESSAGE,
            settings=settings,
            http_client=http_client,
            event_id=event_id,
            idempotency_client=idempotency_client,
            request_id=request_id,
            user_id=user_id,
            client_id=user_id,
            question=question,
            outcome="denied",
            reason_code=_REASON_CLAUDE_REFUSAL,
            error_kind=None,
            started_monotonic=started_monotonic,
            usage=ask_result.usage,
        )
        return

    answer = apply_response_length_policy(ask_result.answer, settings.max_response_chars)
    await _post_and_finish(
        channel=channel,
        thread_ts=thread_ts,
        text=answer,
        settings=settings,
        http_client=http_client,
        event_id=event_id,
        idempotency_client=idempotency_client,
        request_id=request_id,
        user_id=user_id,
        client_id=user_id,
        question=question,
        outcome="ok",
        reason_code=None,
        error_kind=None,
        started_monotonic=started_monotonic,
        usage=ask_result.usage,
    )


async def _process_event(
    payload: dict[str, Any],
    *,
    settings: WorkerSettings,
    anthropic_client: anthropic.AsyncAnthropic,
    idempotency_client: DynamoDBClient | None,
    http_client: httpx2.AsyncClient | None,
) -> None:
    """Run one Slack event through worker steps ①~⑨ (module docstring). Never raises."""
    started_monotonic = time.monotonic()
    request_id = uuid.uuid4().hex

    event_id = extract_event_id(payload)
    user_id = extract_user_id(payload)
    channel = extract_channel(payload)
    thread_ts = extract_reply_target_ts(payload)
    question = _strip_bot_mention(
        extract_question_text(payload) or "", bot_user_id=settings.bot_user_id
    )

    if _is_completed_or_fail_open(event_id, settings=settings, client=idempotency_client):
        logger.info("event_id=%s already completed -- skipping (EDGE-SB-005)", event_id)
        return

    if channel is None:
        # Nothing to post to -- log and drop rather than guess a destination.
        logger.error("cannot determine reply channel -- dropping event_id=%s", event_id)
        return

    credential = resolve_credentials(user_id, settings.user_token_map)
    if not credential.granted:
        await _post_and_finish(
            channel=channel,
            thread_ts=thread_ts,
            text=credential.client_message or _GENERIC_ERROR_MESSAGE,
            settings=settings,
            http_client=http_client,
            event_id=event_id,
            idempotency_client=idempotency_client,
            request_id=request_id,
            user_id=user_id,
            client_id=None,
            question=question,
            outcome="denied",
            reason_code=credential.reason_code,
            error_kind=None,
            started_monotonic=started_monotonic,
            usage=None,
        )
        return

    inflight = _claim_inflight_or_fail_open(
        user_id, thread_ts, settings=settings, client=idempotency_client
    )
    if not inflight.claimed:
        await _post_and_finish(
            channel=channel,
            thread_ts=thread_ts,
            text=ACKNOWLEDGEMENT_MESSAGE,
            settings=settings,
            http_client=http_client,
            event_id=event_id,
            idempotency_client=idempotency_client,
            request_id=request_id,
            user_id=user_id,
            client_id=user_id,
            question=question,
            outcome="denied",
            reason_code=_REASON_COALESCED,
            error_kind=None,
            started_monotonic=started_monotonic,
            usage=None,
        )
        return

    try:
        await _run_query(
            channel=channel,
            thread_ts=thread_ts,
            question=question,
            credential=credential,
            settings=settings,
            anthropic_client=anthropic_client,
            http_client=http_client,
            event_id=event_id,
            idempotency_client=idempotency_client,
            request_id=request_id,
            user_id=user_id,
            started_monotonic=started_monotonic,
        )
    except Exception as exc:
        # "어떤 실패에도 worker가 조용히 죽으면 안 된다" -- see module docstring's
        # "Any exception this module doesn't expect" section.
        logger.error("unexpected exception while processing event_id=%s", event_id, exc_info=True)
        fallback_result = await post_message(
            channel=channel,
            text=_GENERIC_ERROR_MESSAGE,
            bot_token=settings.bot_token,
            thread_ts=thread_ts,
            http_client=http_client,
        )
        _log_post_failure(fallback_result, event_id=event_id, stage="unexpected_error")
        _safe_mark_completed(event_id, settings=settings, client=idempotency_client)
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        emit_query_observation(
            ts=datetime.now(UTC).isoformat(),
            slack_user_id=user_id,
            client_id=user_id,
            channel=channel,
            thread_ts=thread_ts,
            question=question,
            outcome="error",
            reason_code=None,
            error_kind=type(exc).__name__,
            duration_ms=duration_ms,
            usage=None,
            request_id=request_id,
        )
    finally:
        # ⚠️ Runs no matter how the try block above exits (normal return,
        # early return inside _run_query, or the except branch) -- this is
        # the one line this task's handover notes calls "가장 중요".
        _release_inflight_or_log(user_id, thread_ts, settings=settings, client=idempotency_client)


def _make_events_endpoint(
    settings: WorkerSettings,
    anthropic_client: anthropic.AsyncAnthropic | None,
    idempotency_client: DynamoDBClient | None,
    http_client: httpx2.AsyncClient | None,
) -> _Endpoint:
    """Build the ``POST EVENTS_PATH`` endpoint closure for one ``settings``/client set.

    ``anthropic_client`` is resolved **once per app** (not once per request)
    -- the context handover's "worker가 생성해 주입한다" requirement -- so a
    warm Lambda container reuses the same client's connection pool across
    invocations, mirroring ``handler.py``'s/``idempotency.py``'s/
    ``slack/client.py``'s own warm-cache pattern for their respective
    clients. ``idempotency_client``/``http_client`` stay per-request-resolved
    inside the functions they're threaded through (``idempotency.py``'s/
    ``slack/client.py``'s own default-client caching already handles the
    ``None`` production case).
    """
    resolved_anthropic_client = (
        anthropic_client
        if anthropic_client is not None
        else anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    )

    async def _events(request: Request) -> Response:
        raw_body = await request.body()
        try:
            parsed_body: object = json.loads(raw_body)
        except json.JSONDecodeError:
            logger.error("worker received a non-JSON payload on the LWA pass-through path")
            return PlainTextResponse("ok")
        if not isinstance(parsed_body, dict):
            logger.error("worker received a JSON payload that was not an object")
            return PlainTextResponse("ok")
        payload = cast(dict[str, Any], parsed_body)

        await _process_event(
            payload,
            settings=settings,
            anthropic_client=resolved_anthropic_client,
            idempotency_client=idempotency_client,
            http_client=http_client,
        )
        return PlainTextResponse("ok")

    return _events


def create_app(
    settings: WorkerSettings | None = None,
    *,
    anthropic_client: anthropic.AsyncAnthropic | None = None,
    idempotency_client: DynamoDBClient | None = None,
    http_client: httpx2.AsyncClient | None = None,
) -> Starlette:
    """Build one Starlette app for the ``slack-worker`` Lambda, from ``settings``.

    ``settings`` defaults to ``None`` -> loaded from ``os.environ`` via
    ``load_worker_settings`` (Fail-Fast) -- lets this be the zero-argument
    callable ``uvicorn devoks_slackbot.worker:create_app --factory`` needs
    (Dockerfile ``CMD`` for the worker Lambda, ``TASK-020``/``TASK-023``).
    Every test in this package's suite passes ``settings`` explicitly,
    bypassing environment access entirely.

    ``anthropic_client``/``idempotency_client``/``http_client`` are test-only
    injection points (all ``None`` in every real deployment): production
    lets ``anthropic_client`` build lazily from ``settings.anthropic_api_key``
    (once per app, see ``_make_events_endpoint``), and lets
    ``idempotency.py``'s/``slack/client.py``'s own default-client resolution
    handle the other two.

    A factory, not a module-level singleton -- call once per process (or
    once per ``WorkerSettings`` in a test), matching ``handler.py``'s own
    "independent instances" guarantee.
    """
    resolved_settings = settings if settings is not None else load_worker_settings(os.environ)
    _configure_logging(resolved_settings.log_level)

    return Starlette(
        routes=[
            Route(HEALTHZ_PATH, endpoint=_healthz, methods=["GET"]),
            Route(
                EVENTS_PATH,
                endpoint=_make_events_endpoint(
                    resolved_settings, anthropic_client, idempotency_client, http_client
                ),
                methods=["POST"],
            ),
        ],
    )
