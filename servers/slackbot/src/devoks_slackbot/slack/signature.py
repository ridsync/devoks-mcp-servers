"""Slack request signature verification — pure function (DSN-SB-007, TASK-003).

``CTR-SB-001``: ``sig_basestring = "v0:" + timestamp + ":" + raw_body`` ->
HMAC-SHA256 keyed by the signing secret -> hex digest -> ``"v0=" + digest``,
compared against ``X-Slack-Signature`` in constant time (``AC-SB-001-4``).
``CTR-SB-003``: a request whose ``X-Slack-Request-Timestamp`` differs from
"now" by more than 300 seconds is rejected even if the signature is
otherwise valid — replay defense (``EDGE-SB-002``).

This module takes plain bytes/strings/numbers in and returns a ``bool``; it
never imports anything HTTP/ASGI/Lambda-shaped (``DSN-SB-007``). The wiring
that pulls ``raw_body``/headers off an actual request is TASK-012's job, not
this file's — keeping the boundary here is what lets every replay/tamper
case in this module be pinned with a plain unit test instead of a live
request.

``raw_body`` must be the **pre-parse bytes** of the request (``CTR-SB-001``).
Parsing the JSON body and re-serializing it before computing the signature
changes key order/whitespace and silently breaks verification — one of the
best-documented Slack integration footguns, which is why
``test_signature.py`` pins it as its own case rather than leaving it to be
rediscovered in production.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Mapping

#: CTR-SB-003 — 300 seconds (5 minutes), the FRD's measured Slack value.
DEFAULT_TIMESTAMP_TOLERANCE_SECONDS = 300

_SIGNATURE_VERSION = "v0"

#: Canonical Slack header names. Lookup is case-insensitive (AC-SB-001-5,
#: Slack's own docs say not to assume casing) — see ``_read_header``.
SIGNATURE_HEADER = "X-Slack-Signature"
TIMESTAMP_HEADER = "X-Slack-Request-Timestamp"


def verify_slack_signature(
    *,
    headers: Mapping[str, str],
    raw_body: bytes,
    signing_secret: str,
    now: float | None = None,
    tolerance_seconds: int = DEFAULT_TIMESTAMP_TOLERANCE_SECONDS,
) -> bool:
    """Return ``True`` iff ``headers``/``raw_body`` carry a valid Slack signature.

    Checks, in order: both headers present (read case-insensitively,
    ``AC-SB-001-5``), the timestamp parses and is within
    ``tolerance_seconds`` of ``now`` (``AC-SB-001-3``, ``CTR-SB-003``,
    ``EDGE-SB-002``), then the recomputed signature matches the presented one
    via constant-time comparison (``AC-SB-001-1``/``AC-SB-001-4``).

    Never raises. A missing header, a non-numeric timestamp, a malformed
    signature (no ``v0=`` prefix, non-hex digest), or non-ASCII input all
    fall through to an ordinary ``False`` rather than an exception —
    ``EDGE-SB-001`` requires the request be rejected, not partially parsed,
    on any of these; matches the byte-normalize-before-compare lesson
    recorded in ``servers/management``'s ``auth/verifier.py`` (TASK-043).

    ``now`` defaults to ``time.time()`` and exists only so tests can pin the
    clock instead of racing real time.
    """
    signature = _read_header(headers, SIGNATURE_HEADER)
    timestamp_raw = _read_header(headers, TIMESTAMP_HEADER)
    if not signature or not timestamp_raw:
        return False

    timestamp = _parse_timestamp(timestamp_raw)
    if timestamp is None:
        return False

    current_time = time.time() if now is None else now
    if abs(current_time - timestamp) > tolerance_seconds:
        return False

    expected = _compute_signature(timestamp_raw, raw_body, signing_secret)
    return _constant_time_equals(expected, signature)


def _read_header(headers: Mapping[str, str], name: str) -> str | None:
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


def _parse_timestamp(raw: str) -> int | None:
    try:
        return int(raw)
    except ValueError:
        return None


def _compute_signature(timestamp_raw: str, raw_body: bytes, signing_secret: str) -> str:
    sig_basestring = f"{_SIGNATURE_VERSION}:{timestamp_raw}:".encode() + raw_body
    digest = hmac.new(signing_secret.encode("utf-8"), sig_basestring, hashlib.sha256).hexdigest()
    return f"{_SIGNATURE_VERSION}={digest}"


def _constant_time_equals(expected: str, presented: str) -> bool:
    # Compared as UTF-8 bytes, not str: hmac.compare_digest raises TypeError
    # on a non-ASCII str (both sides), so a byte-normalized comparison is
    # what keeps this function's "never raises" guarantee true for any
    # presented header value, including non-ASCII (TASK-043 lesson, see
    # servers/management/src/devoks_mcp_management/auth/verifier.py).
    return hmac.compare_digest(expected.encode("utf-8"), presented.encode("utf-8"))
