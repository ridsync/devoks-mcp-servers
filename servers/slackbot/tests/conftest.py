"""Shared test fixtures and factories (TASK-002).

Mirrors the ``make_settings()`` factory pattern in
``servers/management/tests/conftest.py`` (fixture-convention reuse, FRD §6.1)
— adapted for slackbot's two role-specific ``Settings`` types instead of one,
since ``HandlerSettings``/``WorkerSettings`` (``config.py``) have no dataclass
defaults on purpose: the single source of truth for a default is
``config.py``'s own ``*_DEFAULT`` constants, and duplicating those values onto
the dataclasses would let the two drift.
"""

from __future__ import annotations

from collections.abc import Mapping

from devoks_slackbot.config import (
    IDEMPOTENCY_TTL_SECONDS_DEFAULT,
    MAX_RESPONSE_CHARS_DEFAULT,
    HandlerSettings,
    WorkerSettings,
)

#: Fixture-only literals — never real credentials. Named to make that obvious
#: at every call site, the same intent as management's ``VALID_TEST_TOKEN``.
VALID_TEST_SIGNING_SECRET = "test-fixture-signing-secret-not-a-real-credential"
VALID_TEST_BOT_TOKEN = "xoxb-test-fixture-bot-token-not-a-real-credential"
VALID_TEST_BOT_USER_ID = "U0TESTBOT01"
VALID_TEST_ANTHROPIC_API_KEY = "test-fixture-anthropic-key-not-a-real-credential"
VALID_TEST_MCP_SERVER_URL = "https://mcp.example.com/mcp"
VALID_TEST_IDEMPOTENCY_TABLE = "slackbot-idempotency-test"
VALID_TEST_WORKER_FUNCTION_NAME = "slackbot-worker-test"
VALID_TEST_LOG_LEVEL = "INFO"


def make_handler_settings(
    *,
    signing_secret: str = VALID_TEST_SIGNING_SECRET,
    bot_token: str = VALID_TEST_BOT_TOKEN,
    bot_user_id: str = VALID_TEST_BOT_USER_ID,
    idempotency_table: str = VALID_TEST_IDEMPOTENCY_TABLE,
    worker_function_name: str = VALID_TEST_WORKER_FUNCTION_NAME,
    idempotency_ttl_seconds: int = IDEMPOTENCY_TTL_SECONDS_DEFAULT,
    max_response_chars: int = MAX_RESPONSE_CHARS_DEFAULT,
    log_level: str = VALID_TEST_LOG_LEVEL,
) -> HandlerSettings:
    """Build a ``HandlerSettings`` directly, bypassing ``load_handler_settings``.

    Direct construction is for every test module except ``test_config.py``,
    whose subject is env-var parsing itself and therefore must go through
    ``load_handler_settings``.
    """
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


def make_worker_settings(
    *,
    signing_secret: str = VALID_TEST_SIGNING_SECRET,
    bot_token: str = VALID_TEST_BOT_TOKEN,
    bot_user_id: str = VALID_TEST_BOT_USER_ID,
    anthropic_api_key: str = VALID_TEST_ANTHROPIC_API_KEY,
    mcp_server_url: str = VALID_TEST_MCP_SERVER_URL,
    user_token_map: Mapping[str, str] | None = None,
    idempotency_table: str = VALID_TEST_IDEMPOTENCY_TABLE,
    idempotency_ttl_seconds: int = IDEMPOTENCY_TTL_SECONDS_DEFAULT,
    max_response_chars: int = MAX_RESPONSE_CHARS_DEFAULT,
    log_level: str = VALID_TEST_LOG_LEVEL,
) -> WorkerSettings:
    """Build a ``WorkerSettings`` directly, bypassing ``load_worker_settings``.

    See ``make_handler_settings`` for why direct construction is the default.
    ``idempotency_table`` defaults to the same fixture table name
    ``make_handler_settings`` uses (TASK-014: both roles share one table --
    see ``config.py``'s module docstring).
    """
    return WorkerSettings(
        signing_secret=signing_secret,
        bot_token=bot_token,
        bot_user_id=bot_user_id,
        anthropic_api_key=anthropic_api_key,
        mcp_server_url=mcp_server_url,
        user_token_map={} if user_token_map is None else user_token_map,
        idempotency_table=idempotency_table,
        idempotency_ttl_seconds=idempotency_ttl_seconds,
        max_response_chars=max_response_chars,
        log_level=log_level,
    )
