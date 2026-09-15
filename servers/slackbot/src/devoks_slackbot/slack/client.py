"""``chat.postMessage`` wrapper — thread replies, acknowledgement notice, and
Slack Web API failure classification (``REQ-SB-006``, TASK-010).

**The one trap this module exists to close: Slack's Web API lies with its
HTTP status code.** Per Slack's official ``chat.postMessage`` docs, a failed
call still returns **HTTP 200** — the failure only shows up in the response
*body* as ``{"ok": false, "error": "..."}"``. Checking ``response.status_code``
alone and calling anything under 400 a success would silently record
``EDGE-SB-020`` (bot not invited to the channel) as a successful post while
the user never receives a reply. Every 2xx response in this module is
therefore parsed and judged on its ``ok`` field, never on status code alone
(``_parse_post_message_response``).

Error classification (retryable vs. not) — ``EDGE-SB-020``
------------------------------------------------------------------
``post_message`` never raises for a Slack-side failure; it returns a
``PostMessageResult`` with ``ok=False`` and a ``retryable`` flag so a caller
(``TASK-014``'s worker) can decide what to do without needing to know Slack's
error vocabulary itself:

- ``"not_in_channel"`` (``EDGE-SB-020``) / ``"channel_not_found"`` —
  **not retryable**. Both name a configuration problem (the bot was never
  invited, or the channel id/permission is wrong) that only an operator can
  fix (e.g. ``/invite``); retrying the same call forever cannot succeed.
- any other Slack ``error`` string — **not retryable**, classified as
  ``"slack_error_unknown"``. Deliberately conservative: an error this module
  does not recognize might just as easily be another permanent app-level
  problem (message too long, invalid blocks, ...) as a transient one, and
  guessing wrong the optimistic way risks an unbounded retry loop. A caller
  that later learns a specific unknown error is actually safe to retry can
  special-case it against ``reason_code``.
- ``"rate_limited"`` (HTTP 429) — **retryable**. Slack's documented limit
  for ``chat.postMessage`` is roughly one message per channel per second.
  ``retry_after_seconds`` carries the parsed ``Retry-After`` header when
  Slack sends one. **Policy: this module reads and exposes that header but
  never sleeps or retries itself** — same "no owned retry loop" principle
  ``TASK-011``'s ``ask.py`` uses, kept in this module too so exactly one
  layer (the caller) ever decides retry timing/backoff.
- ``"http_error"`` — a non-2xx, non-429 HTTP status. Retryable iff the
  status is >= 500 (a server-side problem); a 4xx here (outside the 429/body
  cases above) reflects something wrong with the request itself, not
  something a retry fixes.
- ``"network_error"`` / ``"timeout"`` — the request never got a response at
  all (``httpx2.HTTPError``). Both retryable; kept as two distinct reason
  codes (rather than folded together) because a caller may want a longer
  backoff specifically for a timeout, per this task's own requirement that
  these be "구분 가능하게" (distinguishable).
- ``"invalid_response"`` — Slack replied 2xx with a body this module could
  not parse as the documented shape (not JSON, not an object, or
  ``ok: true`` without a usable ``ts``). Not retryable: the *request*
  reached Slack fine, so repeating it is unlikely to change what comes back.

Security (``AC-SB-006-1`` neighbour requirements, not independently ID'd)
------------------------------------------------------------------------------
Two values must never reach a log line, an exception message, or a
``repr``/``str`` of anything this module builds:

1. **The bot token.** Sent only as the ``Authorization: Bearer <token>``
   request header (never as a query/body parameter, so it cannot leak via
   URL logging or request-body logging either) and never included in any
   log statement here — every log call below names only status codes,
   Slack's own short ``error`` strings, and byte/char lengths.
2. **The message being posted (``text``).** It can carry internal/source
   content pulled from Claude's answer (same concern as ``AC-SB-007-3``).
   No log statement in this module includes ``text`` itself; only
   ``len(text)`` where a log needs to say anything about it at all.

HTTP client injection and timeout (``CTR-SB-009``)
-------------------------------------------------------
``http_client`` is an optional keyword so tests can inject an
``httpx2.AsyncClient`` wired to ``httpx2.MockTransport`` (real Slack is never
called from this test suite). When omitted, ``_resolve_http_client`` builds
one lazily and caches it in a module global — the same pattern
``idempotency.py``'s ``_resolve_client`` uses for its boto3 client, for the
same reason: a warm Lambda container should reuse the connection pool across
invocations rather than rebuild it every call, and only production callers
(never tests, which always inject explicitly) ever touch that cache.

The client's timeout is **explicitly set**, never left at whatever
``httpx2.AsyncClient()``'s own default happens to be — the same reasoning
``servers/management/.../github/client.py``'s ``_GITHUB_HTTP_TIMEOUT_SECONDS``
documents: the worker's entire execution budget is ``CTR-SB-009``'s 300
seconds, shared with the Claude API call and everything else in one
invocation, so one unbounded ``chat.postMessage`` call must not be able to
consume it. 20.0s (matching that same precedent value) is generous for a
single, lightweight Slack API call while leaving the worker's budget
overwhelmingly intact even in the worst case.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Final, Literal, cast

import httpx2

logger = logging.getLogger(__name__)

__all__ = [
    "ACKNOWLEDGEMENT_MESSAGE",
    "PostMessageReasonCode",
    "PostMessageResult",
    "post_message",
]

_SLACK_API_BASE_URL: Final = "https://slack.com/api"
_CHAT_POST_MESSAGE_URL: Final = f"{_SLACK_API_BASE_URL}/chat.postMessage"

#: See module docstring, "HTTP client injection and timeout".
_CHAT_POST_MESSAGE_TIMEOUT_SECONDS: Final = 20.0

#: AC-SB-006-4: posted by the caller (TASK-014's worker) before starting the
#: slow work (Claude API call), so the user knows their question was
#: received rather than silently waiting. A plain module constant — like
#: ``format.py``'s ``TRUNCATION_NOTICE`` — so it stays reusable and easy to
#: change/monkeypatch in tests without this module hardcoding the wording at
#: every call site.
ACKNOWLEDGEMENT_MESSAGE: Final[str] = (
    "질문을 확인했습니다. 답변을 준비하는 동안 잠시만 기다려 주세요..."
)

#: See module docstring's "Error classification" section for what each value
#: means and whether ``PostMessageResult.retryable`` is True for it.
PostMessageReasonCode = Literal[
    "not_in_channel",
    "channel_not_found",
    "slack_error_unknown",
    "rate_limited",
    "http_error",
    "network_error",
    "timeout",
    "invalid_response",
]

#: EDGE-SB-020 + its one documented sibling. Only Slack ``error`` strings
#: this module has an actual, distinct reason code for live here — anything
#: else falls through to ``"slack_error_unknown"`` in
#: ``_classify_slack_error``.
_KNOWN_NON_RETRYABLE_ERRORS: Final[dict[str, PostMessageReasonCode]] = {
    "not_in_channel": "not_in_channel",
    "channel_not_found": "channel_not_found",
}


@dataclass(frozen=True, slots=True)
class PostMessageResult:
    """Result of one ``post_message`` call — never raises for a Slack-side failure.

    ``ok=True``: ``ts``/``channel`` are Slack's own values for the newly
    posted message (both non-``None`` in this branch); the four
    failure-only fields are all ``None``.

    ``ok=False``: ``ts``/``channel`` are ``None``. ``retryable`` and
    ``reason_code`` classify the failure (see module docstring).
    ``detail`` is a short, secret-free, message-body-free description safe
    to log as-is. ``retry_after_seconds`` is only ever non-``None`` for
    ``reason_code == "rate_limited"`` when Slack sent a ``Retry-After``
    header.
    """

    ok: bool
    ts: str | None = None
    channel: str | None = None
    retryable: bool | None = None
    reason_code: PostMessageReasonCode | None = None
    detail: str | None = None
    retry_after_seconds: float | None = None


_default_http_client: httpx2.AsyncClient | None = None


def _resolve_http_client(http_client: httpx2.AsyncClient | None) -> httpx2.AsyncClient:
    """Return ``http_client`` if given, else the lazily-built, warm-cached default.

    Only ``None`` (production callers) ever reaches the caching branch —
    tests always inject an ``httpx2.MockTransport``-backed client explicitly,
    so they never touch or depend on this module-global cache. Mirrors
    ``idempotency.py``'s ``_resolve_client`` for the same reason (a warm
    Lambda container reuses the connection pool across invocations).
    """
    global _default_http_client
    if http_client is not None:
        return http_client
    if _default_http_client is None:
        _default_http_client = httpx2.AsyncClient(timeout=_CHAT_POST_MESSAGE_TIMEOUT_SECONDS)
    return _default_http_client


async def post_message(
    *,
    channel: str,
    text: str,
    bot_token: str,
    thread_ts: str | None = None,
    http_client: httpx2.AsyncClient | None = None,
) -> PostMessageResult:
    """Post ``text`` to ``channel`` via Slack's ``chat.postMessage`` (``AC-SB-006-1``).

    ``thread_ts`` — when given, Slack replies inside that thread instead of
    starting a new top-level message (``AC-SB-006-1``); pass the value
    ``slack/events.py``'s ``extract_reply_target_ts`` returned. ``bot_token``
    is sent only as an ``Authorization: Bearer`` header (see module
    docstring's security section) — never logged, never put in the request
    body or URL.

    Never raises for a Slack-side or transport-side failure — every outcome
    (success, a Slack ``ok: false`` error, an HTTP error, a rate limit, a
    network failure/timeout, or an unparsable response) is normalized into
    the returned ``PostMessageResult``. See the module docstring's "Error
    classification" section for exactly how each case is classified.
    """
    client = _resolve_http_client(http_client)
    payload: dict[str, Any] = {"channel": channel, "text": text}
    if thread_ts is not None:
        payload["thread_ts"] = thread_ts

    headers = {
        "Authorization": f"Bearer {bot_token}",
        "Content-Type": "application/json",
    }

    try:
        response = await client.post(_CHAT_POST_MESSAGE_URL, json=payload, headers=headers)
    except httpx2.TimeoutException as exc:
        # Checked before the broader httpx2.HTTPError below (TimeoutException
        # is itself a subclass of it) so a timeout is never folded into the
        # generic "network_error" bucket — see module docstring.
        logger.warning(
            "chat.postMessage timed out (%s, text_len=%d)", type(exc).__name__, len(text)
        )
        return PostMessageResult(
            ok=False,
            retryable=True,
            reason_code="timeout",
            detail=f"Slack API request timed out ({type(exc).__name__})",
        )
    except httpx2.HTTPError as exc:
        logger.warning(
            "chat.postMessage transport error (%s, text_len=%d)", type(exc).__name__, len(text)
        )
        return PostMessageResult(
            ok=False,
            retryable=True,
            reason_code="network_error",
            detail=(
                f"Slack API request failed before a response was received ({type(exc).__name__})"
            ),
        )

    if response.status_code == 429:
        retry_after = _parse_retry_after(response.headers.get("retry-after"))
        logger.warning("chat.postMessage rate limited (retry_after=%s)", retry_after)
        return PostMessageResult(
            ok=False,
            retryable=True,
            reason_code="rate_limited",
            detail="Slack API rate limit exceeded (HTTP 429)",
            retry_after_seconds=retry_after,
        )

    if response.is_error:
        retryable = response.status_code >= 500
        logger.warning("chat.postMessage HTTP error (status=%d)", response.status_code)
        return PostMessageResult(
            ok=False,
            retryable=retryable,
            reason_code="http_error",
            detail=f"Slack API request failed: HTTP {response.status_code}",
        )

    return _parse_post_message_response(response)


def _parse_post_message_response(response: httpx2.Response) -> PostMessageResult:
    """Judge a 2xx ``chat.postMessage`` response on its body, never on status alone.

    See module docstring's opening section — Slack returns HTTP 200 for its
    own app-level failures, so ``ok`` in the parsed body is the only
    trustworthy success/failure signal at this point.
    """
    try:
        body = response.json()
    except ValueError:
        logger.warning("chat.postMessage response was not valid JSON")
        return PostMessageResult(
            ok=False,
            retryable=False,
            reason_code="invalid_response",
            detail="Slack API response was not valid JSON",
        )

    if not isinstance(body, dict):
        logger.warning("chat.postMessage response was not a JSON object")
        return PostMessageResult(
            ok=False,
            retryable=False,
            reason_code="invalid_response",
            detail="Slack API response was not a JSON object",
        )
    body_obj = cast(dict[str, Any], body)

    if body_obj.get("ok") is True:
        ts = body_obj.get("ts")
        channel = body_obj.get("channel")
        if not isinstance(ts, str) or not ts:
            logger.warning("chat.postMessage reported ok=true without a usable ts")
            return PostMessageResult(
                ok=False,
                retryable=False,
                reason_code="invalid_response",
                detail="Slack API reported ok=true without a ts",
            )
        return PostMessageResult(
            ok=True,
            ts=ts,
            channel=channel if isinstance(channel, str) else None,
        )

    error = body_obj.get("error")
    error_str = error if isinstance(error, str) and error else "unknown"
    reason_code = _classify_slack_error(error_str)
    logger.warning("chat.postMessage failed (error=%s)", error_str)
    return PostMessageResult(
        ok=False,
        retryable=False,
        reason_code=reason_code,
        detail=f"Slack API error: {error_str}",
    )


def _classify_slack_error(error: str) -> PostMessageReasonCode:
    """EDGE-SB-020 + fallback — see module docstring's "Error classification"."""
    return _KNOWN_NON_RETRYABLE_ERRORS.get(error, "slack_error_unknown")


def _parse_retry_after(value: str | None) -> float | None:
    """Parse Slack's ``Retry-After`` header (delta-seconds) — ``None`` if absent/malformed."""
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None
