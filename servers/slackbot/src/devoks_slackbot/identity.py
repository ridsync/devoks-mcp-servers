"""Slack user ID -> MCP credential lookup, localized to this file (DSN-SB-003, TASK-005).

Same intent as Stage 1's ``DSN-001`` localizing the auth-verification swap
point to ``verifier.py``: when the static ``SLACK_USER_TOKEN_MAP`` (env-parsed
by ``config.py``) is eventually replaced by an OAuth-backed lookup (Stage 1
§10 trigger), **this file** is the only edit required. No other module in this
package may index ``WorkerSettings.user_token_map`` directly — every lookup
goes through ``resolve_credentials`` below.

``REQ-SB-004`` / ``CTR-SB-006``: each Slack user queries with *their own* MCP
token, so Stage 1's per-token audit trail (``CTR-003``) becomes per-person.

**Security requirements this module exists to satisfy — the three are easy to
get quietly wrong (``AC-SB-004-1..4``, ``EDGE-SB-006``, ``EDGE-SB-012``):**

1. **No information disclosure (``AC-SB-004-3``).** The user-facing denial
   message is the *same fixed string* regardless of whether the caller was
   unidentifiable (``user_id is None``, see below) or a valid-but-unregistered
   Slack user ID, and regardless of the mapping's size or contents. This is
   the same two-layer split as
   ``servers/management/src/devoks_mcp_management/auth/policy.py``'s
   ``AuthorizationDecision``: a single ``client_message`` for every denial
   reason, plus an operator-only ``reason_code`` that *does* distinguish.
2. **No token exposure (``AC-SB-004-4``).** ``CredentialLookupResult.mcp_token``
   is excluded from ``repr`` (``field(repr=False)``, the same pattern as
   ``config.py``'s ``HandlerSettings``/``WorkerSettings``), and no log
   statement in this module ever formats a token value.
3. **No timing signal.** The identified-but-unregistered and
   unidentifiable-caller paths do the same shape of work (one attribute
   check, one optional dict lookup) rather than one returning early and the
   other doing extra validation — a dict lookup's own timing difference is
   already small, but a structurally different code path would widen it.

Unlike ``policy.py`` (deliberately I/O-free), this module *does* call
``logging`` directly. FRD ``EDGE-SB-012``/the "운영자 로그에서는 구분됨"
requirement asks for operator-visible signals (an oversized-mapping warning,
and unidentified vs. unregistered distinguished) that have no other natural
home — this file returns a lookup *result*, it never posts to Slack itself
(that is ``TASK-010``/``TASK-014``'s job) — so logging them here keeps
``DSN-SB-003``'s localization intact instead of pushing mapping-shaped
knowledge out to every caller.

Input contract: this module takes ``user_id: str | None`` — the **already
extracted** value from ``slack/events.py``'s ``extract_user_id`` (``TASK-004``,
``EDGE-SB-019``'s localization) — never a raw payload. ``extract_user_id`` can
return ``None`` for a structurally valid ``app_mention`` whose ``user`` field
is missing/non-string/empty, which is a distinct situation from "a real user ID
that just isn't registered" — both are denied, but the operator log tells them
apart.

Import budget: no boto3, no anthropic, no HTTP/ASGI (pure logic + stdlib
``logging`` only).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger(__name__)

#: AC-SB-004-3: the single user-facing denial message. Every denial branch in
#: ``resolve_credentials`` returns exactly this string — never one derived
#: from the requested user ID, the mapping's size, or which check failed. A
#: caller must not be able to distinguish "you were never identified" from
#: "your ID isn't registered" from "nobody is registered yet".
_CLIENT_DENIAL_MESSAGE = "등록되지 않은 사용자입니다. 관리자에게 등록을 요청해 주세요."

#: Audit-only classification of why ``resolve_credentials`` denied a lookup.
#: Never surfaced to the client (see module docstring).
ReasonCode = Literal["user_unidentified", "user_unregistered"]

#: EDGE-SB-012: same threshold as Stage 1 §10's OAuth-transition trigger
#: ①(client count 10) — deliberately reused, not re-derived. Stage 1 measured
#: ~123 B/client against the Lambda 4 KB aggregate env-var ceiling
#: (``EDGE-021``); ``SLACK_USER_TOKEN_MAP`` entries cost roughly 60 B each (11 B
#: Slack user ID + 43 B MCP token + JSON delimiters), so worker has more
#: headroom than handler did — but "more headroom" is not "unbounded", and
#: reusing the same trigger point keeps one place, not two, deciding when
#: env-var-based credentials stop scaling.
MAPPING_SIZE_WARNING_THRESHOLD = 10


@dataclass(frozen=True, slots=True)
class CredentialLookupResult:
    """Result of one ``resolve_credentials`` call.

    ``mcp_token`` is only set when ``granted`` is True. It is excluded from
    ``repr`` unconditionally (``AC-SB-004-4``) — a stray log or exception of
    this object can never format the token, even for a granted result.
    """

    granted: bool
    mcp_token: str | None = field(default=None, repr=False)
    client_message: str | None = None
    reason_code: ReasonCode | None = None


def resolve_credentials(
    user_id: str | None,
    user_token_map: Mapping[str, str],
) -> CredentialLookupResult:
    """Look up the MCP token for ``user_id`` in ``user_token_map``.

    ``user_id`` is the value already returned by ``slack/events.py``'s
    ``extract_user_id`` — pass ``None`` through as-is when that function
    could not identify a caller; this function does not re-derive identity
    from a payload.

    Returns a granted result carrying the caller's own MCP token
    (``AC-SB-004-1``) when ``user_id`` is a non-empty string present in
    ``user_token_map`` with a non-empty token value. Every other case —
    ``user_id is None``, an empty string, a missing key, or an empty-string
    token value (defensive: ``config.py`` already rejects these at parse
    time, but this function does not trust that its caller always went
    through that path) — denies with the identical client-facing message
    (``AC-SB-004-2``, ``AC-SB-004-3``, ``EDGE-SB-006``) and only the
    operator-only ``reason_code`` differs.

    Also logs an operator-only warning once per call if ``user_token_map``
    exceeds ``MAPPING_SIZE_WARNING_THRESHOLD`` (``EDGE-SB-012``) — never
    including the mapping's keys/values, only its size.
    """
    _warn_if_mapping_oversized(user_token_map)

    if user_id:
        token = user_token_map.get(user_id) or None
        reason: ReasonCode = "user_unregistered"
    else:
        token = None
        reason = "user_unidentified"

    if token is None:
        logger.info("credential lookup denied (reason=%s)", reason)
        return CredentialLookupResult(
            granted=False,
            client_message=_CLIENT_DENIAL_MESSAGE,
            reason_code=reason,
        )

    return CredentialLookupResult(granted=True, mcp_token=token)


def _warn_if_mapping_oversized(user_token_map: Mapping[str, str]) -> None:
    size = len(user_token_map)
    if size > MAPPING_SIZE_WARNING_THRESHOLD:
        logger.warning(
            "SLACK_USER_TOKEN_MAP has %d entries, exceeding the warning threshold of %d "
            "(EDGE-SB-012) — plan the OAuth transition (Stage 1 §10 trigger ①)",
            size,
            MAPPING_SIZE_WARNING_THRESHOLD,
        )
