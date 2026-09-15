"""Environment variables -> immutable, role-scoped ``Settings`` (DSN-SB-005).

This module deliberately duplicates the Fail-Fast pattern from
``servers/management/src/devoks_mcp_management/config.py`` (all problems
collected and raised together in one ``ConfigError``, DSN-006) rather than
importing it from a shared package — FRD §4.4 rejects a shared config
package because it would couple the two servers' deployments.

**Why two entry points instead of one ``load_settings(env, role)``:**
One image serves two Lambdas (FRD §4.4) with genuinely different required
keys (FRD §5.2) — handler needs ``WORKER_FUNCTION_NAME`` but never
``ANTHROPIC_API_KEY``; worker is the reverse. Splitting into
``load_handler_settings``/``load_worker_settings`` (and ``HandlerSettings``/
``WorkerSettings``) makes it a type error, not just a runtime omission, for
handler code to reach for a worker-only field. Loading the wrong role's
settings for a Lambda that doesn't need them would also cost handler's
3-second/4 KB env budget (``EDGE-021``) for keys it never uses.

**``IDEMPOTENCY_TABLE`` is common, not handler-only (2026-09-14, ``TASK-014``
correction).** FRD §5.2's environment-key table originally scoped this key to
handler alone. ``TASK-014`` (``worker.py``) found this incomplete: ``EDGE-SB-005``
requires the *worker* to also check/record completion against the same
DynamoDB table (Lambda's own async-invoke retry is a worker-visible
duplication cause handler cannot see at all), and ``EDGE-SB-015``'s in-flight
coalescing lock (``idempotency.claim_inflight_query``/``release_inflight_query``)
reuses that identical table under its own key prefix. Both roles now require
it — the same kind of FRD gap ``TASK-012`` already closed once for
``WORKER_FUNCTION_NAME`` (see ``_HANDLER_REQUIRED_KEYS`` below).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import urlsplit

# CTR-SB-004: the Claude API call contract's fixed values. FRD §5.2's
# environment-key table has no entries for these three — AC-SB-005-5 requires
# that user input can never change them, so they are code constants rather
# than env-overridable settings. ``ask.py`` (TASK-011) imports these directly;
# there is no env var that reaches them, by construction.
CLAUDE_MODEL = "claude-opus-5"
CLAUDE_MAX_TOKENS = 8000
CLAUDE_EFFORT = "medium"
CLAUDE_MCP_BETA = "mcp-client-2025-11-20"

MAX_RESPONSE_CHARS_DEFAULT = 3500
MAX_RESPONSE_CHARS_MIN = 1
MAX_RESPONSE_CHARS_MAX = 40000

IDEMPOTENCY_TTL_SECONDS_DEFAULT = 3600
IDEMPOTENCY_TTL_SECONDS_MIN = 300
IDEMPOTENCY_TTL_SECONDS_MAX = 86400

_DEFAULT_LOG_LEVEL = "INFO"
_VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})

#: FRD §5.2 keys with no handler-only/worker-only annotation, plus
#: ``IDEMPOTENCY_TABLE`` (moved here by TASK-014 -- see module docstring's
#: "IDEMPOTENCY_TABLE is common, not handler-only" section). Required by
#: both roles.
_COMMON_REQUIRED_KEYS: tuple[str, ...] = (
    "SLACK_SIGNING_SECRET",
    "SLACK_BOT_TOKEN",
    "SLACK_BOT_USER_ID",
    "IDEMPOTENCY_TABLE",
)

#: FRD §5.2's environment-key table does not list a key for the worker
#: Lambda's identifier -- an unavoidable gap TASK-012 (handler.py) fills:
#: DSN-SB-001 splits handler/worker into two Lambdas specifically so the
#: handler can hand off work async and return within CTR-SB-002's 3-second
#: budget, and AC-SB-002-1 requires that handoff to actually happen (a
#: ``boto3`` Lambda ``Invoke``), which needs the worker's ``FunctionName``
#: from somewhere. Handler-only -- worker never invokes itself, so it never
#: needs this key.
_HANDLER_REQUIRED_KEYS: tuple[str, ...] = ("WORKER_FUNCTION_NAME",)

_WORKER_REQUIRED_KEYS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "MCP_SERVER_URL",
    "SLACK_USER_TOKEN_MAP",
)


class ConfigError(Exception):
    """One or more environment values failed validation at startup.

    The message lists every problem found in this call, not just the first.
    """


class _FieldError(Exception):
    """Internal control flow only: carries one field's user-facing message."""


@dataclass(frozen=True, slots=True)
class HandlerSettings:
    """Validated configuration for the ``slack-handler`` Lambda entry point.

    ``signing_secret``/``bot_token`` are excluded from ``repr`` so logging or
    raising a ``HandlerSettings`` instance can never leak a credential.
    """

    signing_secret: str = field(repr=False)
    bot_token: str = field(repr=False)
    bot_user_id: str
    idempotency_table: str
    worker_function_name: str
    idempotency_ttl_seconds: int
    max_response_chars: int
    log_level: str


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    """Validated configuration for the ``slack-worker`` Lambda entry point.

    ``signing_secret``/``bot_token``/``anthropic_api_key``/``user_token_map``
    are excluded from ``repr`` — ``user_token_map`` values are MCP bearer
    tokens (CTR-SB-006), a credential exactly like the other three.
    """

    signing_secret: str = field(repr=False)
    bot_token: str = field(repr=False)
    bot_user_id: str
    anthropic_api_key: str = field(repr=False)
    mcp_server_url: str
    user_token_map: Mapping[str, str] = field(repr=False)
    idempotency_table: str
    idempotency_ttl_seconds: int
    max_response_chars: int
    log_level: str


def load_handler_settings(env: Mapping[str, str]) -> HandlerSettings:
    """Parse and validate ``env`` into a ``HandlerSettings``.

    Raises ``ConfigError`` if any required key (common or handler-only) is
    missing, or any value fails format/range validation. All problems are
    collected before raising (DSN-006).
    """
    errors: list[str] = []

    required = _COMMON_REQUIRED_KEYS + _HANDLER_REQUIRED_KEYS
    missing = [key for key in required if not (env.get(key) or "").strip()]
    if missing:
        errors.append("missing required environment variable(s): " + ", ".join(missing))

    signing_secret = (env.get("SLACK_SIGNING_SECRET") or "").strip()
    bot_token = (env.get("SLACK_BOT_TOKEN") or "").strip()
    bot_user_id = (env.get("SLACK_BOT_USER_ID") or "").strip()
    idempotency_table = (env.get("IDEMPOTENCY_TABLE") or "").strip()
    worker_function_name = (env.get("WORKER_FUNCTION_NAME") or "").strip()

    try:
        max_response_chars = _parse_int_in_range(
            env.get("SLACKBOT_MAX_RESPONSE_CHARS"),
            "SLACKBOT_MAX_RESPONSE_CHARS",
            MAX_RESPONSE_CHARS_DEFAULT,
            MAX_RESPONSE_CHARS_MIN,
            MAX_RESPONSE_CHARS_MAX,
        )
    except _FieldError as exc:
        errors.append(str(exc))
        max_response_chars = MAX_RESPONSE_CHARS_DEFAULT

    try:
        idempotency_ttl_seconds = _parse_int_in_range(
            env.get("SLACKBOT_IDEMPOTENCY_TTL_SECONDS"),
            "SLACKBOT_IDEMPOTENCY_TTL_SECONDS",
            IDEMPOTENCY_TTL_SECONDS_DEFAULT,
            IDEMPOTENCY_TTL_SECONDS_MIN,
            IDEMPOTENCY_TTL_SECONDS_MAX,
        )
    except _FieldError as exc:
        errors.append(str(exc))
        idempotency_ttl_seconds = IDEMPOTENCY_TTL_SECONDS_DEFAULT

    try:
        log_level = _parse_log_level(env.get("SLACKBOT_LOG_LEVEL"))
    except _FieldError as exc:
        errors.append(str(exc))
        log_level = _DEFAULT_LOG_LEVEL

    if errors:
        raise ConfigError("; ".join(errors))

    return HandlerSettings(
        signing_secret=signing_secret,
        bot_token=bot_token,
        bot_user_id=bot_user_id,
        idempotency_table=idempotency_table,
        worker_function_name=worker_function_name,
        idempotency_ttl_seconds=idempotency_ttl_seconds,
        max_response_chars=max_response_chars,
        log_level=log_level,
    )


def load_worker_settings(env: Mapping[str, str]) -> WorkerSettings:
    """Parse and validate ``env`` into a ``WorkerSettings``.

    Raises ``ConfigError`` if any required key (common or worker-only) is
    missing, or any value fails format/range/schema validation. All problems
    are collected before raising (DSN-006).
    """
    errors: list[str] = []

    required = _COMMON_REQUIRED_KEYS + _WORKER_REQUIRED_KEYS
    missing = [key for key in required if not (env.get(key) or "").strip()]
    if missing:
        errors.append("missing required environment variable(s): " + ", ".join(missing))

    signing_secret = (env.get("SLACK_SIGNING_SECRET") or "").strip()
    bot_token = (env.get("SLACK_BOT_TOKEN") or "").strip()
    bot_user_id = (env.get("SLACK_BOT_USER_ID") or "").strip()
    anthropic_api_key = (env.get("ANTHROPIC_API_KEY") or "").strip()
    idempotency_table = (env.get("IDEMPOTENCY_TABLE") or "").strip()

    mcp_server_url = ""
    if "MCP_SERVER_URL" not in missing:
        try:
            mcp_server_url = _parse_url(env["MCP_SERVER_URL"], "MCP_SERVER_URL")
        except _FieldError as exc:
            errors.append(str(exc))

    user_token_map: Mapping[str, str] = {}
    if "SLACK_USER_TOKEN_MAP" not in missing:
        try:
            user_token_map = _parse_user_token_map(env["SLACK_USER_TOKEN_MAP"])
        except _FieldError as exc:
            errors.append(str(exc))

    try:
        max_response_chars = _parse_int_in_range(
            env.get("SLACKBOT_MAX_RESPONSE_CHARS"),
            "SLACKBOT_MAX_RESPONSE_CHARS",
            MAX_RESPONSE_CHARS_DEFAULT,
            MAX_RESPONSE_CHARS_MIN,
            MAX_RESPONSE_CHARS_MAX,
        )
    except _FieldError as exc:
        errors.append(str(exc))
        max_response_chars = MAX_RESPONSE_CHARS_DEFAULT

    try:
        idempotency_ttl_seconds = _parse_int_in_range(
            env.get("SLACKBOT_IDEMPOTENCY_TTL_SECONDS"),
            "SLACKBOT_IDEMPOTENCY_TTL_SECONDS",
            IDEMPOTENCY_TTL_SECONDS_DEFAULT,
            IDEMPOTENCY_TTL_SECONDS_MIN,
            IDEMPOTENCY_TTL_SECONDS_MAX,
        )
    except _FieldError as exc:
        errors.append(str(exc))
        idempotency_ttl_seconds = IDEMPOTENCY_TTL_SECONDS_DEFAULT

    try:
        log_level = _parse_log_level(env.get("SLACKBOT_LOG_LEVEL"))
    except _FieldError as exc:
        errors.append(str(exc))
        log_level = _DEFAULT_LOG_LEVEL

    if errors:
        raise ConfigError("; ".join(errors))

    return WorkerSettings(
        signing_secret=signing_secret,
        bot_token=bot_token,
        bot_user_id=bot_user_id,
        anthropic_api_key=anthropic_api_key,
        mcp_server_url=mcp_server_url,
        user_token_map=user_token_map,
        idempotency_table=idempotency_table,
        idempotency_ttl_seconds=idempotency_ttl_seconds,
        max_response_chars=max_response_chars,
        log_level=log_level,
    )


def _parse_url(raw: str, key: str) -> str:
    value = raw.strip()
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise _FieldError(f"{key} is not a valid http(s) URL: {value!r}")
    return value


def _load_json_object(raw: str, key: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        # str(exc) is line/column/char position only, never the input text —
        # safe even though this key's value is a JSON object of credentials
        # (CTR-SB-006).
        raise _FieldError(f"{key} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise _FieldError(f"{key} must be a JSON object")
    return cast(dict[str, Any], data)


def _parse_user_token_map(raw: str) -> Mapping[str, str]:
    data = _load_json_object(raw, "SLACK_USER_TOKEN_MAP")

    mapping: dict[str, str] = {}
    problems: list[str] = []
    # Entries are identified by ordinal position, never by echoing the Slack
    # user ID or the MCP token — the value here *is* a bearer credential
    # (CTR-SB-006), and this error can reach a log.
    for index, (slack_user_id, token) in enumerate(data.items(), start=1):
        if not slack_user_id:
            problems.append(f"SLACK_USER_TOKEN_MAP entry #{index}: key must be a non-empty string")
            continue
        if not isinstance(token, str) or not token:
            problems.append(
                f"SLACK_USER_TOKEN_MAP entry #{index}: value must be a non-empty string"
            )
            continue
        mapping[slack_user_id] = token

    if problems:
        raise _FieldError("; ".join(problems))

    return mapping


def _parse_int_in_range(raw: str | None, key: str, default: int, minimum: int, maximum: int) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise _FieldError(f"{key} must be an integer, got {raw!r}") from exc
    if not (minimum <= value <= maximum):
        raise _FieldError(f"{key} must be between {minimum} and {maximum}, got {value}")
    return value


def _parse_log_level(raw: str | None) -> str:
    if raw is None or not raw.strip():
        return _DEFAULT_LOG_LEVEL
    value = raw.strip().upper()
    if value not in _VALID_LOG_LEVELS:
        raise _FieldError(
            f"SLACKBOT_LOG_LEVEL must be one of {sorted(_VALID_LOG_LEVELS)}, got {raw!r}"
        )
    return value
