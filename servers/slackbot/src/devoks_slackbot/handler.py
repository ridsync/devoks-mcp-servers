"""ASGI composition root for the ``slack-handler`` Lambda entry point (TASK-012).

``create_app(...) -> Starlette`` is the **factory** Dockerfile ``CMD`` invokes
directly as ``uvicorn devoks_slackbot.handler:create_app --factory`` (TASK-020's
handover expectation) -- unlike ``servers/management``'s ``app.py``, there is
no separate ``create_app_from_env`` wrapper: ``create_app`` itself accepts no
required arguments, loading ``HandlerSettings`` from ``os.environ`` (Fail-Fast,
``config.load_handler_settings``) only when the caller does not supply one
directly. Importing this module never reads the environment or raises
``ConfigError`` -- only *calling* ``create_app()`` with no ``settings`` does,
mirroring ``devoks_mcp_management.app``'s module-import-safety rationale.

Processing order (FRD §4.1 handler steps ①~⑤, §5.4 state table, and this
workspace's ``context`` handover note, which additionally inserts the bot
self-message check as step 3) -- **do not reorder**:

1. **Signature verification** (``slack/signature.py``). Failure -> **401**,
   and the body is never parsed first (``EDGE-SB-001``: verifying after
   parsing means untrusted input was already trusted). This is also why the
   body is read as raw bytes via ``await request.body()`` and handed to
   ``verify_slack_signature`` unparsed/unreserialized -- ``CTR-SB-001``
   requires the *exact* bytes Slack signed; parsing and reserializing changes
   key order/whitespace and silently breaks verification.
2. ``type == "url_verification"`` -> return ``challenge`` **only after**
   signature verification has already passed (``AC-SB-001-6``,
   ``EDGE-SB-003``). Responding before verifying would let anyone use this
   endpoint as a free "is this URL alive" oracle.
3. Bot self-message -> 200, no work (``EDGE-SB-011``,
   ``slack/events.py``'s ``is_bot_self_message``) -- checked before any
   idempotency/dispatch work starts, per that module's own docstring.
4. **Idempotency claim** (``idempotency.claim_event``). A duplicate
   ``event_id`` (or one that fails to claim for any reason, including
   ``IdempotencyStoreError`` -- see ``_claim_or_fail_safe``'s docstring) means
   no work starts, but the response is still 200 (``AC-SB-003-1``). The
   ``x-slack-retry-num`` header (read case-insensitively; Starlette's
   ``Headers.get`` already lowercases the lookup key regardless of how the
   header arrived on the wire) is logged as a warning whenever present,
   independent of the claim outcome -- its presence alone means Slack's own
   3-second wait already elapsed once (``EDGE-SB-004``).
5. **Async dispatch** -- a ``boto3`` Lambda ``Invoke`` (``InvocationType =
   "Event"``) wakes ``slack-worker`` (``DSN-SB-001``). The dispatched
   ``Payload`` is the **original, unparsed** Slack request body -- the exact
   bytes the worker's own LWA pass-through path (``AWS_LWA_PASS_THROUGH_PATH``,
   PLAN §1) will hand it as an HTTP request body, so the worker can parse it
   with the very same ``slack/events.py`` functions this module uses.
6. **Immediate 200** -- always, whether step 5 succeeded or not
   (``AC-SB-002-3``: a failed dispatch is logged, never turned into a non-2xx
   response, because a 500 here only makes Slack retry and add duplicates,
   never actually recovers anything).

``AC-SB-002-2`` -- this module never calls the Claude API or the MCP server.
No function in this file's call graph does either; see ``DSN-SB-008`` below
for the import-level guarantee that backs this.

🔴 ``DSN-SB-008`` -- **this module's import graph must never include
``anthropic``** (workspace PLAN §1: measured import cost 1,384 ms, 46% of
``CTR-SB-002``'s 3-second budget on its own). Concretely, this file must
never import ``ask.py`` or ``worker.py`` (``ask.py`` imports ``anthropic``
directly) -- ``TASK-013`` pins this invariant with a dedicated,
subprocess-isolated test (``sys.modules`` accumulates across a single pytest
process, so an in-process check here would be contaminated by whichever
other test module happened to import ``ask.py`` first); this module's own
test suite (``test_handler.py``) repeats that check for the same reason
noted there.

🔴 Third-party SDK logger pinning (PLAN §1 "SDK DEBUG 로깅 함정") -- even
though this module never imports ``anthropic``/``httpx2``, ``_configure_logging``
below still pins both loggers to at least ``INFO`` by *name* (``logging.
getLogger("anthropic")`` creates/looks up a logger object without importing
the package). This is deliberate defense-in-depth, not dead code: configuring
third-party SDK log verbosity is a *common entry-point responsibility*
(``worker.py``, TASK-014, needs the identical guard for the same reason
documented there -- ``anthropic._base_client`` logs full request bodies,
person MCP tokens included, at DEBUG). Pinning it here as well means the
invariant "these two loggers never exceed INFO" holds regardless of which
Lambda's entry point runs first to configure logging in a given process, and
costs nothing (a logger nobody's using produces no output regardless of
level).

Config gap this task had to close (``WORKER_FUNCTION_NAME``)
------------------------------------------------------------------
FRD §5.2's environment-key table lists no key for the worker Lambda's
identifier, yet ``AC-SB-002-1``/``DSN-SB-001`` require this module to
actually invoke it. ``config.py`` (TASK-002, out of this task's nominal
``file:`` scope but a necessary, minimal extension -- see this task's
handover notes) now requires ``WORKER_FUNCTION_NAME`` for the handler role,
alongside ``IDEMPOTENCY_TABLE``.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any, Final, Protocol, cast

import boto3
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from .config import HandlerSettings, load_handler_settings
from .idempotency import ClaimOutcome, IdempotencyStoreError, claim_event
from .slack.events import (
    extract_challenge,
    extract_event_id,
    is_bot_self_message,
    is_url_verification,
)
from .slack.signature import verify_slack_signature

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient

#: The endpoint closure shape ``_make_slack_events_endpoint`` returns --
#: named so its own signature stays readable.
_Endpoint = Callable[[Request], Awaitable[Response]]

logger = logging.getLogger(__name__)

#: TASK-024 (API Gateway route) imports this rather than hardcoding the
#: string, so the two can never drift.
SLACK_EVENTS_PATH: Final[str] = "/slack/events"

#: Dockerfile's ``AWS_LWA_READINESS_CHECK_PATH`` (TASK-020) must point here --
#: without this route, Lambda Web Adapter never considers the container ready
#: and traffic never reaches it.
HEALTHZ_PATH: Final[str] = "/healthz"

#: EDGE-SB-004. Read case-insensitively via ``Headers.get`` (see module
#: docstring step 4) -- the constant itself only needs one casing.
_RETRY_NUM_HEADER: Final[str] = "x-slack-retry-num"

#: PLAN §1's SDK DEBUG logging trap -- see module docstring.
_NOISY_SDK_LOGGER_NAMES: Final[tuple[str, ...]] = ("anthropic", "httpx2")
_NOISY_SDK_LOGGER_MIN_LEVEL: Final[int] = logging.INFO


class WorkerInvoker(Protocol):
    """The one boto3 Lambda-client capability this module needs (step 5).

    A structural ``Protocol`` -- mirrors ``observability.py``'s
    ``ObservationStream`` -- rather than importing ``mypy_boto3_lambda``:
    that stub package is not part of this project's dev dependencies (only
    ``boto3-stubs[dynamodb]`` is, for ``idempotency.py``), and adding it
    would cost a dependency for exactly one method signature. A real
    ``boto3.client("lambda")`` satisfies this structurally (its ``invoke``
    accepts these exact keyword arguments); tests inject a lightweight fake
    instead of standing up ``moto``'s heavier Lambda mocking, which requires
    a real deployment package -- there is no conditional-write-style
    semantic subtlety here worth paying that cost for (contrast
    ``idempotency.py``'s module docstring, which explains why *that* module's
    tests do use real ``moto`` DynamoDB semantics).
    """

    def invoke(
        self, *, FunctionName: str, InvocationType: str, Payload: bytes
    ) -> Mapping[str, Any]: ...


_default_lambda_client: WorkerInvoker | None = None


def _resolve_lambda_client(client: WorkerInvoker | None) -> WorkerInvoker:
    """Return ``client`` if given, else the lazily-built, warm-cached default.

    Mirrors ``idempotency.py``'s ``_resolve_client`` exactly, and for the
    same reason: ``boto3.client(...)`` construction costs ~82 ms (workspace
    PLAN §1) that a warm Lambda container should pay once, not per
    invocation. Only ``None`` (production callers) ever reaches the caching
    branch -- tests always inject a fake ``WorkerInvoker`` explicitly.
    """
    global _default_lambda_client
    if client is not None:
        return client
    if _default_lambda_client is None:
        # No mypy_boto3_lambda stub is installed (see WorkerInvoker's
        # docstring), so boto3.client("lambda") resolves to an unknown type
        # here -- cast() asserts the structural contract WorkerInvoker
        # already documents, same role the mypy_boto3_dynamodb stub package
        # plays for idempotency.py's own default-client cache.
        _default_lambda_client = cast(
            "WorkerInvoker",
            boto3.client("lambda"),  # pyright: ignore[reportUnknownMemberType]
        )
    return _default_lambda_client


def _configure_logging(log_level: str) -> None:
    """Set the root logger's threshold, then re-pin the noisy SDK loggers above it.

    Order matters: ``setLevel`` on the noisy loggers must run *after* the
    root level is set, since a logger with no explicit level of its own
    would otherwise inherit whatever the operator just raised the root to
    (e.g. ``SLACKBOT_LOG_LEVEL=DEBUG`` for incident response) -- see module
    docstring's "SDK 로깅 함정" section for why that specific scenario is the
    one this function exists to prevent.
    """
    logging.getLogger().setLevel(log_level)
    for name in _NOISY_SDK_LOGGER_NAMES:
        logging.getLogger(name).setLevel(_NOISY_SDK_LOGGER_MIN_LEVEL)


async def _healthz(request: Request) -> Response:
    """GET /healthz -- unauthenticated by design (LWA readiness probe, TASK-020).

    Body is deliberately minimal: no settings, no identity, nothing beyond
    "this process can answer HTTP requests" -- a public, unauthenticated
    route must never echo back configuration.
    """
    return JSONResponse({"status": "ok"})


def _make_slack_events_endpoint(
    settings: HandlerSettings,
    lambda_client: WorkerInvoker | None,
    idempotency_client: DynamoDBClient | None,
) -> _Endpoint:
    """Build the ``POST SLACK_EVENTS_PATH`` endpoint closure for one ``settings``/client pair.

    A closure (not a bare module-level function) because the endpoint needs
    ``settings`` (the signing secret, the worker's ``FunctionName``, ...) and
    the two optional injected clients (``lambda_client``/``idempotency_client``,
    both test-only -- production always leaves them ``None`` and lets
    ``_resolve_lambda_client``/``idempotency.claim_event``'s own default
    resolve lazily). ``create_app`` builds a fresh closure per call, matching
    its own "independent instances" guarantee.
    """

    async def _slack_events(request: Request) -> Response:
        raw_body = await request.body()

        if not verify_slack_signature(
            headers=request.headers, raw_body=raw_body, signing_secret=settings.signing_secret
        ):
            # EDGE-SB-001: rejected on the signature alone -- raw_body is
            # never parsed on this path.
            return PlainTextResponse("unauthorized", status_code=401)

        try:
            parsed_body: object = json.loads(raw_body)
        except json.JSONDecodeError:
            logger.warning("slack event body failed to parse as JSON after a valid signature")
            return PlainTextResponse("malformed request body", status_code=400)
        if not isinstance(parsed_body, dict):
            logger.warning("slack event body was valid JSON but not a JSON object")
            return PlainTextResponse("malformed request body", status_code=400)
        payload = cast(dict[str, Any], parsed_body)

        if is_url_verification(payload):
            # AC-SB-001-6 / EDGE-SB-003: only reachable once verify_slack_signature
            # above has already returned True.
            challenge = extract_challenge(payload)
            return JSONResponse({"challenge": challenge})

        if is_bot_self_message(payload, bot_user_id=settings.bot_user_id):
            # EDGE-SB-011: checked before any idempotency/dispatch work.
            return PlainTextResponse("ok")

        if request.headers.get(_RETRY_NUM_HEADER) is not None:
            # EDGE-SB-004: presence alone (any value) means Slack's own
            # 3-second wait already elapsed once for this event.
            logger.warning(
                "slack retry received (event_id=%s, %s=%s)",
                extract_event_id(payload),
                _RETRY_NUM_HEADER,
                request.headers.get(_RETRY_NUM_HEADER),
            )

        event_id = extract_event_id(payload)
        claim_outcome = _claim_or_fail_safe(event_id, settings=settings, client=idempotency_client)
        if not claim_outcome.claimed:
            # AC-SB-003-1: duplicate/unclaimable -- 200, no dispatch.
            return PlainTextResponse("ok")

        _dispatch_to_worker(raw_body, settings=settings, client=lambda_client)
        return PlainTextResponse("ok")

    return _slack_events


def _claim_or_fail_safe(
    event_id: str | None, *, settings: HandlerSettings, client: DynamoDBClient | None
) -> ClaimOutcome:
    """Wrap ``idempotency.claim_event`` so a store outage degrades to "no work, still 200".

    Not itself required by any single AC the way ``AC-SB-002-3`` is (that one
    names the *dispatch* step specifically) -- this is this task's own
    defined answer for what the context handover calls "멱등 저장소 오류
    (``IdempotencyStoreError``) 시의 정의된 동작". The reasoning mirrors
    ``AC-SB-002-3``'s: if the idempotency store itself is unreachable, this
    module cannot tell "new" from "duplicate" -- proceeding to dispatch
    anyway would risk exactly the double-answer/double-spend outcome the
    whole idempotency mechanism exists to prevent, so the fail-safe choice is
    to skip dispatch for this one event (logged at ERROR, operator-visible)
    rather than risk a duplicate. A 500 is never returned either way, for the
    same reason ``AC-SB-002-3`` gives: Slack would only retry into the same
    broken store.

    The returned ``reason="store_error"`` (never ``"missing_event_id"``,
    even though ``event_id`` may well be present here) is what lets an
    operator tell "the store itself is unreachable" apart from "events keep
    arriving without an id" -- see ``idempotency.ClaimReason``'s own comment
    for why conflating the two would be a real diagnostic regression, not a
    cosmetic one.
    """
    try:
        return claim_event(
            event_id,
            table_name=settings.idempotency_table,
            ttl_seconds=settings.idempotency_ttl_seconds,
            client=client,
        )
    except IdempotencyStoreError:
        logger.error(
            "idempotency store unreachable (event_id=%s) -- skipping dispatch, still ACKing",
            event_id,
            exc_info=True,
        )
        return ClaimOutcome(claimed=False, reason="store_error")


def _dispatch_to_worker(
    raw_body: bytes, *, settings: HandlerSettings, client: WorkerInvoker | None
) -> None:
    """Invoke ``slack-worker`` asynchronously (``AC-SB-002-1``). Never raises.

    ``AC-SB-002-3``: any failure here (throttling, a missing/misconfigured
    function, a network error, or any other exception) is logged and
    swallowed -- deliberately caught as bare ``Exception``, broader than
    ``idempotency.py``'s ``(BotoCoreError, ClientError)`` pattern, because
    this AC is unconditional ("비동기 전달 *자체가* 실패하면") with no carve-out
    for a failure this module didn't anticipate; the one behavior it must
    never produce is a non-2xx response caused by this step.
    """
    resolved_client = _resolve_lambda_client(client)
    try:
        resolved_client.invoke(
            FunctionName=settings.worker_function_name,
            InvocationType="Event",
            Payload=raw_body,
        )
    except Exception:
        logger.error("async dispatch to worker Lambda failed", exc_info=True)


def create_app(
    settings: HandlerSettings | None = None,
    *,
    lambda_client: WorkerInvoker | None = None,
    idempotency_client: DynamoDBClient | None = None,
) -> Starlette:
    """Build one Starlette app for the ``slack-handler`` Lambda, from ``settings``.

    ``settings`` defaults to ``None``, in which case it is loaded from
    ``os.environ`` via ``load_handler_settings`` (Fail-Fast) -- this is what
    lets ``create_app`` itself be the zero-argument callable
    ``uvicorn devoks_slackbot.handler:create_app --factory`` needs (Dockerfile
    ``CMD``, TASK-020), with no separate ``create_app_from_env`` wrapper.
    Passing ``settings`` explicitly (every test in this package's suite does)
    bypasses environment access entirely.

    ``lambda_client``/``idempotency_client`` are test-only injection points
    (both ``None`` in every real deployment) -- see ``WorkerInvoker`` and
    ``idempotency.claim_event``'s own ``client`` parameter for what each
    accepts.

    A factory, not a module-level singleton -- call this once per process (or
    once per ``HandlerSettings`` in a test); each call builds independent
    objects, never sharing route closures across calls.
    """
    resolved_settings = settings if settings is not None else load_handler_settings(os.environ)
    _configure_logging(resolved_settings.log_level)

    return Starlette(
        routes=[
            Route(HEALTHZ_PATH, endpoint=_healthz, methods=["GET"]),
            Route(
                SLACK_EVENTS_PATH,
                endpoint=_make_slack_events_endpoint(
                    resolved_settings, lambda_client, idempotency_client
                ),
                methods=["POST"],
            ),
        ],
    )
