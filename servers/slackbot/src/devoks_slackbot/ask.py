"""Claude API call via the MCP connector — no MCP client of our own (``DSN-SB-002``, TASK-011).

**The one trap this module exists to close (``EDGE-SB-009``):** the Claude
API's MCP connector needs *two* things to agree — ``mcp_servers[].name`` and
``tools[].mcp_server_name`` — and if either is sent without the other, or the
names don't match, the API rejects the request with a validation error
(confirmed by hand against the real API — see the workspace PLAN's handoff
notes). ``_build_mcp_request`` is the *only* place in this module that
constructs either list, and the shared name (``_MCP_SERVER_NAME``) is written
as a string literal exactly once — every other reference is the constant, so
the two lists cannot drift apart by construction. ``ask_claude`` always calls
this one function for both; there is no code path that sends one without the
other.

``AC-SB-005-2`` / ``DSN-SB-002``: this module implements **no MCP client, no
tool loop, no tool schema.** Anthropic's own infrastructure connects to the
MCP server server-side using the ``authorization_token`` on ``mcp_servers[0]``
— this module's only job is constructing that one request and interpreting
the one response it gets back.

Request shape (``CTR-SB-004``) — verified against the real API, not just the
SDK's types
------------------------------------------------------------------------------
``model``/``max_tokens``/``output_config.effort`` come from ``config.py``'s
``CLAUDE_MODEL``/``CLAUDE_MAX_TOKENS``/``CLAUDE_EFFORT`` — never a caller
argument (``AC-SB-005-5``). There is no parameter on ``ask_claude`` that could
let a Slack question change any of the three; a caller physically cannot
override them.

Two more contract requirements enforced by omission rather than a check:

- ``thinking`` is never set — Opus 5 uses adaptive thinking by default, and
  setting it explicitly is unnecessary and, combined with ``budget_tokens``,
  a 400 error.
- ``budget_tokens`` and an assistant-role prefill message are never
  constructed anywhere in this module — both are documented to return a 400.
  There is simply no code path here that could add either.

``EDGE-SB-014``: ``messages`` is always exactly one user message — the
question just asked. Stage 3's initial scope answers each question
independently; no prior thread turns are read or sent, so cost and prompt
size never grow with thread length.

Error classification (``EDGE-SB-008``, ``AC-SB-005-3``)
------------------------------------------------------------------------------
``ask_claude`` never raises for a Claude-API-side failure — like
``slack/client.py``'s ``post_message``, every outcome is normalized into the
returned ``AskResult``, whose ``retryable``/``reason_code``/``client_message``
tell a caller (``worker.py``) what to post without the caller needing to know
Anthropic's error vocabulary:

- **HTTP 400, ``invalid_request_error``, message starting with "You have
  reached your specified API usage limits"** — the operator's own configured
  spend limit. **Not retryable**: this is a status set by a person and stays
  set until they change it; retrying does not help.
- **HTTP 429, ``rate_limit_error``, with ``error.details.error_code ==
  "enforced_spend_limit_reached"``** — Anthropic's tier-level spend cap. Its
  ``type`` is identical to an ordinary rate limit, so without checking
  ``error_code`` this looks retryable and the SDK's own retry (``max_retries``,
  default 2) burns through both attempts before giving up — the cap does not
  clear on any timescale a retry could catch. **Not retryable.** Also
  notably: Anthropic does not send a ``retry-after`` header for this case
  (confirmed against the real API), unlike an ordinary 429.
- Any other HTTP 429 — an ordinary rate limit. **Retryable**; the SDK's own
  retry already handles the common case, this module just needs to say so if
  every attempt is exhausted.
- **HTTP >= 500** — a Claude-API-side problem. **Retryable.**
- **Timeout** (``anthropic.APITimeoutError``) / **network failure**
  (``anthropic.APIConnectionError``, checked after ``APITimeoutError`` since
  the latter subclasses the former) — the request never got a usable
  response. **Retryable**, and kept as two distinct reason codes for the same
  reason ``slack/client.py`` does: a caller may want a different backoff for
  each.
- Any other HTTP status (401/403/404/other 4xx, or a 400 that isn't the spend
  limit message) — a problem with the request/credentials itself, not
  something a retry fixes. **Not retryable** — the conservative default,
  same stance ``slack/client.py`` takes for a Slack error string it does not
  recognize.

**This module never adds its own retry loop on top of the SDK's** — it makes
exactly one ``client.beta.messages.create`` call per ``ask_claude`` call. The
Anthropic SDK client (constructed by the caller, e.g. ``worker.py``) owns
retry count/backoff (``max_retries``, default 2); duplicating that here would
double the number of attempts on every already-exhausted-retry failure.

Refusal (``AC-SB-005-4``)
------------------------------------------------------------------------------
``stop_reason == "refusal"`` is a normal, successful API response — not an
exception — but its ``content`` must never be surfaced as an answer (it may
be empty, partial, or simply not meant to be shown). ``ask_claude`` checks
``stop_reason`` *before* extracting any text and returns ``outcome="refused"``
with ``answer`` left ``None``.

MCP server cold start and this call's timeout (``EDGE-SB-016``)
------------------------------------------------------------------------------
Anthropic's own infrastructure — not this module — connects to the MCP
server on the first tool call each request needs; that connection has been
measured at roughly 1.9s normally and up to ~8.5s right after a fresh MCP
server image. This module's only responsibility for that latency is to not
time out *waiting on Anthropic* while Anthropic is itself waiting on a slow
first tool call. ``_REQUEST_TIMEOUT_SECONDS`` (60s) is set to comfortably
exceed the worst observed cold start (~7x headroom) plus room for actual
model inference and more than one MCP tool round trip, while keeping the
worst case of one initial attempt + the SDK's default 2 retries
(3 x 60s = 180s) well inside the worker's overall ``CTR-SB-009`` budget
(300s), leaving room for identity lookup, Slack posting, and Lambda/runtime
overhead around this one call. Passed as the per-call ``timeout=`` argument
to ``create`` (an SDK-supported per-request override) rather than baked into
the injected client, so this module's own reasoning about the value stays in
one place regardless of how the caller configures its client otherwise.

Security (``AC-SB-005-3`` neighbour requirements)
------------------------------------------------------------------------------
Two values must never reach a log line, an exception message this module
constructs, or a ``repr``/``str`` of anything this module builds:

1. **The Anthropic API key.** This module never touches it directly — the
   injected ``anthropic.AsyncAnthropic`` client owns it (sent only as the
   ``x-api-key`` request header, confirmed by hand against the real API) —
   and no log statement here ever formats the client or its configuration.
2. **``authorization_token`` (the caller's own MCP bearer token).** Sent only
   inside the request body Anthropic itself serializes; this module's own
   log statements name only status codes, Anthropic's own short error
   ``type``/``error_code`` strings, and ``len(question)`` — never the token,
   the question, or the answer text (same ``AC-SB-007-3`` policy
   ``observability.py`` documents).

Response extraction
------------------------------------------------------------------------------
``usage`` is pulled into a plain ``dict[str, int | None]`` with exactly the
three keys ``observability.py``'s ``build_record`` accepts
(``input_tokens``/``output_tokens``/``cache_read_input_tokens``) — never the
SDK's ``BetaUsage`` object itself, for the same reason that module's docstring
gives: passing the SDK object in would either break JSON serialization or
leak fields nothing downstream expects. Answer text is the concatenation of
every ``type == "text"`` block in ``content``, in order — a Claude response
using MCP tools interleaves text blocks with ``mcp_tool_use``/
``mcp_tool_result`` blocks, and only the text blocks are ever meant to reach
the user.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, cast

import anthropic
from anthropic.types.beta import (
    BetaMCPToolsetParam,
    BetaRequestMCPServerURLDefinitionParam,
    BetaUsage,
)

from .config import CLAUDE_EFFORT, CLAUDE_MAX_TOKENS, CLAUDE_MCP_BETA, CLAUDE_MODEL

logger = logging.getLogger(__name__)

__all__ = [
    "AskOutcome",
    "AskReasonCode",
    "AskResult",
    "ask_claude",
]

#: EDGE-SB-009: the single name shared by ``mcp_servers[].name`` and
#: ``tools[].mcp_server_name``. Written as a string literal exactly once —
#: see module docstring's opening section.
_MCP_SERVER_NAME: Final = "devoks-management"

#: See module docstring's "MCP server cold start and this call's timeout".
_REQUEST_TIMEOUT_SECONDS: Final = 60.0

#: HTTP 400 spend-limit message prefix — matched against
#: ``error.message`` from the parsed response body, never against
#: ``exc.message`` (the SDK formats that as ``"Error code: ... - {body}"``,
#: not the raw message text — confirmed by hand against the real API).
_SPEND_LIMIT_MESSAGE_PREFIX: Final = "You have reached your specified API usage limits"

#: EDGE-SB-008: the one ``error.details.error_code`` value that reclassifies
#: an HTTP 429 from an ordinary (retryable) rate limit to a tier spend cap
#: (not retryable). See module docstring's "Error classification".
_ENFORCED_SPEND_LIMIT_ERROR_CODE: Final = "enforced_spend_limit_reached"

AskOutcome = Literal["answered", "refused", "error"]

#: Operator-only classification of an ``outcome == "error"`` result. Never
#: shown to the Slack user as-is — ``client_message`` carries the
#: user-facing text (``AC-SB-005-3``).
AskReasonCode = Literal[
    "spend_limit_exceeded",
    "rate_limited",
    "server_error",
    "timeout",
    "network_error",
    "api_error",
]

_RETRYABLE_CLIENT_MESSAGE = "일시적인 오류로 답변을 만들지 못했습니다. 잠시 후 다시 시도해 주세요."
_NON_RETRYABLE_CLIENT_MESSAGE = (
    "답변을 만들지 못했습니다. 문제가 계속되면 관리자에게 문의해 주세요."
)
_SPEND_LIMIT_CLIENT_MESSAGE = (
    "이번 달 API 사용 한도에 도달해 답변을 만들 수 없습니다. 관리자에게 문의해 주세요."
)
_REFUSAL_CLIENT_MESSAGE = "이 질문에 대한 답변이 거부되었습니다. 다른 방식으로 질문해 주세요."


@dataclass(frozen=True, slots=True)
class AskResult:
    """Result of one ``ask_claude`` call — never raises for a Claude-API-side failure.

    ``outcome == "answered"``: ``answer``/``stop_reason``/``usage`` are set;
    the four error-only fields are all ``None``.

    ``outcome == "refused"``: ``stop_reason == "refusal"``, ``usage`` is
    still set (Anthropic still reports token usage for a refusal), but
    ``answer`` is ``None`` — ``AC-SB-005-4`` forbids using ``content`` as the
    answer in this case. ``client_message`` carries the user-facing refusal
    notice.

    ``outcome == "error"``: ``answer``/``stop_reason``/``usage`` are all
    ``None`` (no usable response was ever produced). ``retryable`` and
    ``reason_code`` classify the failure (module docstring's "Error
    classification"); ``client_message`` is the secret-free, retry-aware
    notice to post (``AC-SB-005-3``); ``detail`` is a short, secret-free,
    question/answer-free description safe to log as-is.
    """

    outcome: AskOutcome
    answer: str | None = None
    stop_reason: str | None = None
    usage: dict[str, int | None] | None = None
    retryable: bool | None = None
    reason_code: AskReasonCode | None = None
    client_message: str | None = None
    detail: str | None = None


def _build_mcp_request(
    *, mcp_server_url: str, authorization_token: str
) -> tuple[list[BetaRequestMCPServerURLDefinitionParam], list[BetaMCPToolsetParam]]:
    """Build the ``mcp_servers``/``tools`` pair together — the only place either is built.

    ``EDGE-SB-009``: the two returned lists are always used together by
    ``ask_claude`` and always share ``_MCP_SERVER_NAME``; there is no way to
    call this function and get one without the other, or get mismatched
    names.
    """
    mcp_servers: list[BetaRequestMCPServerURLDefinitionParam] = [
        {
            "type": "url",
            "url": mcp_server_url,
            "name": _MCP_SERVER_NAME,
            "authorization_token": authorization_token,
        }
    ]
    tools: list[BetaMCPToolsetParam] = [
        {"type": "mcp_toolset", "mcp_server_name": _MCP_SERVER_NAME},
    ]
    return mcp_servers, tools


async def ask_claude(
    *,
    question: str,
    mcp_server_url: str,
    authorization_token: str,
    client: anthropic.AsyncAnthropic,
) -> AskResult:
    """Ask ``question`` via the Claude API MCP connector (``REQ-SB-005``, ``CTR-SB-004``).

    ``mcp_server_url`` is ``WorkerSettings.mcp_server_url``.
    ``authorization_token`` is the *calling Slack user's own* MCP token
    (``identity.py``'s ``CredentialLookupResult.mcp_token``) — never a shared
    service credential; per-person tokens are what makes the MCP server's own
    audit trail (Stage 1 ``CTR-003``) per-person. ``client`` is injected by
    the caller (``worker.py``) so this module never reads
    ``ANTHROPIC_API_KEY`` or constructs a client itself — tests inject a
    client wired to a fake transport (see ``tests/test_ask.py``); this
    module never calls the real Anthropic API.

    ``messages`` is always exactly one user turn — the question just asked,
    with no prior thread history (``EDGE-SB-014``). ``model``/``max_tokens``/
    ``output_config.effort`` are always ``config.py``'s ``CLAUDE_MODEL``/
    ``CLAUDE_MAX_TOKENS``/``CLAUDE_EFFORT`` — there is no parameter here a
    caller could use to change them (``AC-SB-005-5``).

    Never raises for a Claude-API-side failure — see the module docstring's
    "Error classification" section for exactly how each case is classified
    into the returned ``AskResult``.
    """
    mcp_servers, tools = _build_mcp_request(
        mcp_server_url=mcp_server_url, authorization_token=authorization_token
    )

    try:
        response = await client.beta.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=CLAUDE_MAX_TOKENS,
            messages=[{"role": "user", "content": question}],
            mcp_servers=mcp_servers,
            tools=tools,
            output_config={"effort": CLAUDE_EFFORT},
            betas=[CLAUDE_MCP_BETA],
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
    except anthropic.APITimeoutError as exc:
        # Checked before the broader APIConnectionError below (APITimeoutError
        # is itself a subclass of it) so a timeout is never folded into the
        # generic "network_error" bucket — see module docstring.
        logger.warning(
            "Claude API request timed out (%s, question_len=%d)", type(exc).__name__, len(question)
        )
        return AskResult(
            outcome="error",
            retryable=True,
            reason_code="timeout",
            client_message=_RETRYABLE_CLIENT_MESSAGE,
            detail=f"Claude API request timed out ({type(exc).__name__})",
        )
    except anthropic.APIConnectionError as exc:
        logger.warning(
            "Claude API network error (%s, question_len=%d)", type(exc).__name__, len(question)
        )
        return AskResult(
            outcome="error",
            retryable=True,
            reason_code="network_error",
            client_message=_RETRYABLE_CLIENT_MESSAGE,
            detail=(
                f"Claude API request failed before a response was received ({type(exc).__name__})"
            ),
        )
    except anthropic.APIStatusError as exc:
        return _classify_status_error(exc, question_len=len(question))
    except anthropic.AnthropicError as exc:
        # Catch-all for anthropic-specific failures not covered above (e.g. a
        # malformed 2xx body raising APIResponseValidationError). Deliberately
        # conservative — see module docstring's "Error classification".
        logger.warning(
            "Claude API call failed unexpectedly (%s, question_len=%d)",
            type(exc).__name__,
            len(question),
        )
        return AskResult(
            outcome="error",
            retryable=False,
            reason_code="api_error",
            client_message=_NON_RETRYABLE_CLIENT_MESSAGE,
            detail=f"Claude API call failed unexpectedly ({type(exc).__name__})",
        )

    usage = _extract_usage(response.usage)

    if response.stop_reason == "refusal":
        logger.info("Claude API call refused the request (stop_reason=refusal)")
        return AskResult(
            outcome="refused",
            stop_reason=response.stop_reason,
            usage=usage,
            client_message=_REFUSAL_CLIENT_MESSAGE,
        )

    return AskResult(
        outcome="answered",
        answer=_extract_answer_text(response.content),
        stop_reason=response.stop_reason,
        usage=usage,
    )


def _classify_status_error(exc: anthropic.APIStatusError, *, question_len: int) -> AskResult:
    """``EDGE-SB-008`` — see module docstring's "Error classification" for the full rule set."""
    error_message, error_code = _parse_error_body(exc.body)

    if exc.status_code == 400 and error_message.startswith(_SPEND_LIMIT_MESSAGE_PREFIX):
        logger.warning(
            "Claude API spend limit exceeded (status=400, question_len=%d)", question_len
        )
        return AskResult(
            outcome="error",
            retryable=False,
            reason_code="spend_limit_exceeded",
            client_message=_SPEND_LIMIT_CLIENT_MESSAGE,
            detail="Claude API spend limit exceeded (HTTP 400, invalid_request_error)",
        )

    if exc.status_code == 429:
        if error_code == _ENFORCED_SPEND_LIMIT_ERROR_CODE:
            logger.warning(
                "Claude API tier spend cap reached (status=429, question_len=%d)", question_len
            )
            return AskResult(
                outcome="error",
                retryable=False,
                reason_code="spend_limit_exceeded",
                client_message=_SPEND_LIMIT_CLIENT_MESSAGE,
                detail=f"Claude API tier spend cap reached (HTTP 429, error_code={error_code})",
            )
        logger.warning("Claude API rate limited (status=429, question_len=%d)", question_len)
        return AskResult(
            outcome="error",
            retryable=True,
            reason_code="rate_limited",
            client_message=_RETRYABLE_CLIENT_MESSAGE,
            detail="Claude API rate limited (HTTP 429)",
        )

    if exc.status_code >= 500:
        logger.warning(
            "Claude API server error (status=%d, question_len=%d)", exc.status_code, question_len
        )
        return AskResult(
            outcome="error",
            retryable=True,
            reason_code="server_error",
            client_message=_RETRYABLE_CLIENT_MESSAGE,
            detail=f"Claude API server error (HTTP {exc.status_code})",
        )

    # Any other 4xx (auth/permission/validation/a non-spend-limit 400, ...) —
    # a problem with the request/credentials itself, not fixed by a retry.
    logger.warning(
        "Claude API request failed (status=%d, question_len=%d)", exc.status_code, question_len
    )
    return AskResult(
        outcome="error",
        retryable=False,
        reason_code="api_error",
        client_message=_NON_RETRYABLE_CLIENT_MESSAGE,
        detail=f"Claude API request failed (HTTP {exc.status_code})",
    )


def _parse_error_body(body: object | None) -> tuple[str, str | None]:
    """Pull ``error.message``/``error.details.error_code`` out of an error response body.

    ``exc.message`` (the SDK's own ``Exception`` message) is deliberately not
    used for this — it is formatted as ``"Error code: <n> - <body>"``, not
    the API's own message text (confirmed by hand against the real API).
    Returns ``("", None)`` for any shape that doesn't match the documented
    error envelope, rather than raising — a classification helper must not
    itself fail on an unexpected error body.
    """
    if not isinstance(body, dict):
        return "", None
    body_obj = cast(dict[str, Any], body)
    error_obj = body_obj.get("error")
    if not isinstance(error_obj, dict):
        return "", None
    error_obj = cast(dict[str, Any], error_obj)
    message = error_obj.get("message")
    message_str = message if isinstance(message, str) else ""
    details = error_obj.get("details")
    error_code: object = None
    if isinstance(details, dict):
        error_code = cast(dict[str, Any], details).get("error_code")
    error_code_str = error_code if isinstance(error_code, str) else None
    return message_str, error_code_str


def _extract_answer_text(content: Sequence[object]) -> str:
    """Join every ``type == "text"`` block's ``.text`` — see module docstring."""
    parts: list[str] = []
    for block in content:
        if getattr(block, "type", None) != "text":
            continue
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _extract_usage(usage: BetaUsage) -> dict[str, int | None]:
    """Plain 3-key dict — never the SDK's ``BetaUsage`` object itself. See module docstring."""
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_input_tokens": usage.cache_read_input_tokens,
    }
