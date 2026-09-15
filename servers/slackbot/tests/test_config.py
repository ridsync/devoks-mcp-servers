"""Tests for devoks_slackbot.config (TASK-002).

Traces: CTR-SB-005, CTR-SB-007, AC-SB-005-5, DSN-SB-005, DSN-006.
"""

import json

import pytest

from devoks_slackbot import config
from devoks_slackbot.config import (
    IDEMPOTENCY_TTL_SECONDS_DEFAULT,
    IDEMPOTENCY_TTL_SECONDS_MAX,
    IDEMPOTENCY_TTL_SECONDS_MIN,
    MAX_RESPONSE_CHARS_DEFAULT,
    MAX_RESPONSE_CHARS_MAX,
    MAX_RESPONSE_CHARS_MIN,
    ConfigError,
    HandlerSettings,
    WorkerSettings,
    load_handler_settings,
    load_worker_settings,
)

from .conftest import (
    VALID_TEST_ANTHROPIC_API_KEY,
    VALID_TEST_BOT_TOKEN,
    VALID_TEST_BOT_USER_ID,
    VALID_TEST_IDEMPOTENCY_TABLE,
    VALID_TEST_MCP_SERVER_URL,
    VALID_TEST_SIGNING_SECRET,
    VALID_TEST_WORKER_FUNCTION_NAME,
)

_VALID_TEST_USER_TOKEN_MAP = {"U01ABCDEF": "mcp-fixture-token-not-a-real-credential"}


def _common_env() -> dict[str, str]:
    return {
        "SLACK_SIGNING_SECRET": VALID_TEST_SIGNING_SECRET,
        "SLACK_BOT_TOKEN": VALID_TEST_BOT_TOKEN,
        "SLACK_BOT_USER_ID": VALID_TEST_BOT_USER_ID,
    }


def _handler_env() -> dict[str, str]:
    env = _common_env()
    env["IDEMPOTENCY_TABLE"] = VALID_TEST_IDEMPOTENCY_TABLE
    env["WORKER_FUNCTION_NAME"] = VALID_TEST_WORKER_FUNCTION_NAME
    return env


def _worker_env() -> dict[str, str]:
    env = _common_env()
    env["ANTHROPIC_API_KEY"] = VALID_TEST_ANTHROPIC_API_KEY
    env["MCP_SERVER_URL"] = VALID_TEST_MCP_SERVER_URL
    env["SLACK_USER_TOKEN_MAP"] = json.dumps(_VALID_TEST_USER_TOKEN_MAP)
    # TASK-014: worker also needs the idempotency table (EDGE-SB-005's
    # worker-side completion check, EDGE-SB-015's coalescing lock) — see
    # config.py's module docstring "IDEMPOTENCY_TABLE is common" section.
    env["IDEMPOTENCY_TABLE"] = VALID_TEST_IDEMPOTENCY_TABLE
    return env


# --- normal cases ------------------------------------------------------------


def test_valid_handler_env_loads_settings_successfully() -> None:
    settings = load_handler_settings(_handler_env())

    assert isinstance(settings, HandlerSettings)
    assert settings.signing_secret == VALID_TEST_SIGNING_SECRET
    assert settings.bot_token == VALID_TEST_BOT_TOKEN
    assert settings.bot_user_id == VALID_TEST_BOT_USER_ID
    assert settings.idempotency_table == VALID_TEST_IDEMPOTENCY_TABLE
    assert settings.worker_function_name == VALID_TEST_WORKER_FUNCTION_NAME
    assert settings.idempotency_ttl_seconds == IDEMPOTENCY_TTL_SECONDS_DEFAULT
    assert settings.max_response_chars == MAX_RESPONSE_CHARS_DEFAULT
    assert settings.log_level == "INFO"


def test_valid_worker_env_loads_settings_successfully() -> None:
    settings = load_worker_settings(_worker_env())

    assert isinstance(settings, WorkerSettings)
    assert settings.signing_secret == VALID_TEST_SIGNING_SECRET
    assert settings.bot_token == VALID_TEST_BOT_TOKEN
    assert settings.bot_user_id == VALID_TEST_BOT_USER_ID
    assert settings.anthropic_api_key == VALID_TEST_ANTHROPIC_API_KEY
    assert settings.mcp_server_url == VALID_TEST_MCP_SERVER_URL
    assert dict(settings.user_token_map) == _VALID_TEST_USER_TOKEN_MAP
    assert settings.idempotency_table == VALID_TEST_IDEMPOTENCY_TABLE
    assert settings.idempotency_ttl_seconds == IDEMPOTENCY_TTL_SECONDS_DEFAULT
    assert settings.max_response_chars == MAX_RESPONSE_CHARS_DEFAULT


def test_load_handler_settings_is_deterministic_across_repeated_calls() -> None:
    env = _handler_env()
    assert load_handler_settings(env) == load_handler_settings(env)


def test_load_worker_settings_is_deterministic_across_repeated_calls() -> None:
    env = _worker_env()
    assert load_worker_settings(env) == load_worker_settings(env)


def test_optional_numeric_overrides_within_bounds_are_applied() -> None:
    env = _worker_env()
    env["SLACKBOT_MAX_RESPONSE_CHARS"] = "1000"
    env["SLACKBOT_IDEMPOTENCY_TTL_SECONDS"] = "600"
    env["SLACKBOT_LOG_LEVEL"] = "debug"

    settings = load_worker_settings(env)

    assert settings.max_response_chars == 1000
    assert settings.idempotency_ttl_seconds == 600
    assert settings.log_level == "DEBUG"


# --- role separation (FRD §5.2 handler/worker key split) ---------------------


def test_handler_does_not_require_worker_only_keys() -> None:
    # Worker-only keys (ANTHROPIC_API_KEY, MCP_SERVER_URL,
    # SLACK_USER_TOKEN_MAP) must not be required to start the handler role —
    # they would cost the 3-second path's 4 KB env budget (EDGE-021) for
    # nothing.
    env = _handler_env()
    assert "ANTHROPIC_API_KEY" not in env
    assert "MCP_SERVER_URL" not in env
    assert "SLACK_USER_TOKEN_MAP" not in env

    settings = load_handler_settings(env)

    assert isinstance(settings, HandlerSettings)


def test_worker_does_not_require_handler_only_key() -> None:
    # WORKER_FUNCTION_NAME is handler-only (invokes the worker asynchronously,
    # AC-SB-002-1) -- requiring it for worker would grant worker unused
    # Lambda-invoke permission it never uses. IDEMPOTENCY_TABLE moved to
    # _COMMON_REQUIRED_KEYS in TASK-014 (worker needs it too, see
    # test_valid_worker_env_loads_settings_successfully), so it is no longer
    # part of this "handler-only" claim.
    env = _worker_env()
    assert "WORKER_FUNCTION_NAME" not in env

    settings = load_worker_settings(env)

    assert isinstance(settings, WorkerSettings)


def test_worker_missing_idempotency_table_fails_startup() -> None:
    # TASK-014: worker checks/records completion against the same DynamoDB
    # table as handler (EDGE-SB-005) and uses it for the in-flight coalescing
    # lock (EDGE-SB-015) -- both need IDEMPOTENCY_TABLE, not just handler.
    env = _worker_env()
    del env["IDEMPOTENCY_TABLE"]
    with pytest.raises(ConfigError, match="IDEMPOTENCY_TABLE"):
        load_worker_settings(env)


def test_handler_missing_idempotency_table_fails_startup() -> None:
    env = _handler_env()
    del env["IDEMPOTENCY_TABLE"]
    with pytest.raises(ConfigError, match="IDEMPOTENCY_TABLE"):
        load_handler_settings(env)


def test_handler_missing_worker_function_name_fails_startup() -> None:
    # TASK-012: the handler dispatches to the worker via a boto3 Lambda
    # Invoke (AC-SB-002-1) and needs the worker's FunctionName to do it.
    env = _handler_env()
    del env["WORKER_FUNCTION_NAME"]
    with pytest.raises(ConfigError, match="WORKER_FUNCTION_NAME"):
        load_handler_settings(env)


@pytest.mark.parametrize(
    "missing_key", ["ANTHROPIC_API_KEY", "MCP_SERVER_URL", "SLACK_USER_TOKEN_MAP"]
)
def test_worker_missing_worker_only_key_fails_startup(missing_key: str) -> None:
    env = _worker_env()
    del env[missing_key]
    with pytest.raises(ConfigError, match=missing_key):
        load_worker_settings(env)


_COMMON_KEYS = [
    "SLACK_SIGNING_SECRET",
    "SLACK_BOT_TOKEN",
    "SLACK_BOT_USER_ID",
    "IDEMPOTENCY_TABLE",
]


@pytest.mark.parametrize("missing_key", _COMMON_KEYS)
def test_handler_missing_common_key_fails_startup(missing_key: str) -> None:
    env = _handler_env()
    del env[missing_key]
    with pytest.raises(ConfigError, match=missing_key):
        load_handler_settings(env)


@pytest.mark.parametrize("missing_key", _COMMON_KEYS)
def test_worker_missing_common_key_fails_startup(missing_key: str) -> None:
    env = _worker_env()
    del env[missing_key]
    with pytest.raises(ConfigError, match=missing_key):
        load_worker_settings(env)


def test_empty_signing_secret_value_fails_as_missing() -> None:
    # Present but blank counts as missing, not a valid empty secret.
    env = _handler_env()
    env["SLACK_SIGNING_SECRET"] = "   "
    with pytest.raises(ConfigError, match="SLACK_SIGNING_SECRET"):
        load_handler_settings(env)


def test_handler_missing_multiple_keys_reports_all_names_at_once() -> None:
    env = _handler_env()
    del env["SLACK_SIGNING_SECRET"]
    del env["IDEMPOTENCY_TABLE"]

    with pytest.raises(ConfigError) as exc_info:
        load_handler_settings(env)

    message = str(exc_info.value)
    assert "SLACK_SIGNING_SECRET" in message
    assert "IDEMPOTENCY_TABLE" in message


def test_worker_missing_multiple_keys_reports_all_names_at_once() -> None:
    env = _worker_env()
    del env["ANTHROPIC_API_KEY"]
    del env["MCP_SERVER_URL"]

    with pytest.raises(ConfigError) as exc_info:
        load_worker_settings(env)

    message = str(exc_info.value)
    assert "ANTHROPIC_API_KEY" in message
    assert "MCP_SERVER_URL" in message


def test_missing_key_and_range_error_are_reported_together() -> None:
    # DSN-006: all problems in one run, not one per fix-and-restart cycle.
    env = _worker_env()
    del env["ANTHROPIC_API_KEY"]
    env["SLACKBOT_MAX_RESPONSE_CHARS"] = "0"

    with pytest.raises(ConfigError) as exc_info:
        load_worker_settings(env)

    message = str(exc_info.value)
    assert "ANTHROPIC_API_KEY" in message
    assert "SLACKBOT_MAX_RESPONSE_CHARS" in message


# --- CTR-SB-005: SLACKBOT_MAX_RESPONSE_CHARS range (1..40000) ---------------


@pytest.mark.parametrize("value", [MAX_RESPONSE_CHARS_MIN, MAX_RESPONSE_CHARS_MAX])
def test_max_response_chars_accepts_inclusive_bounds(value: int) -> None:
    env = _worker_env()
    env["SLACKBOT_MAX_RESPONSE_CHARS"] = str(value)
    settings = load_worker_settings(env)
    assert settings.max_response_chars == value


@pytest.mark.parametrize("value", [MAX_RESPONSE_CHARS_MIN - 1, MAX_RESPONSE_CHARS_MAX + 1])
def test_max_response_chars_out_of_range_fails_startup(value: int) -> None:
    env = _worker_env()
    env["SLACKBOT_MAX_RESPONSE_CHARS"] = str(value)
    with pytest.raises(ConfigError, match="SLACKBOT_MAX_RESPONSE_CHARS"):
        load_worker_settings(env)


def test_max_response_chars_non_integer_fails_startup() -> None:
    env = _worker_env()
    env["SLACKBOT_MAX_RESPONSE_CHARS"] = "not-an-int"
    with pytest.raises(ConfigError, match="SLACKBOT_MAX_RESPONSE_CHARS"):
        load_worker_settings(env)


# --- CTR-SB-007: SLACKBOT_IDEMPOTENCY_TTL_SECONDS range (300..86400) --------


@pytest.mark.parametrize("value", [IDEMPOTENCY_TTL_SECONDS_MIN, IDEMPOTENCY_TTL_SECONDS_MAX])
def test_idempotency_ttl_accepts_inclusive_bounds(value: int) -> None:
    env = _handler_env()
    env["SLACKBOT_IDEMPOTENCY_TTL_SECONDS"] = str(value)
    settings = load_handler_settings(env)
    assert settings.idempotency_ttl_seconds == value


@pytest.mark.parametrize(
    "value", [IDEMPOTENCY_TTL_SECONDS_MIN - 1, IDEMPOTENCY_TTL_SECONDS_MAX + 1]
)
def test_idempotency_ttl_out_of_range_fails_startup(value: int) -> None:
    env = _handler_env()
    env["SLACKBOT_IDEMPOTENCY_TTL_SECONDS"] = str(value)
    with pytest.raises(ConfigError, match="SLACKBOT_IDEMPOTENCY_TTL_SECONDS"):
        load_handler_settings(env)


# --- SLACKBOT_LOG_LEVEL -------------------------------------------------------


def test_invalid_log_level_fails_startup() -> None:
    env = _handler_env()
    env["SLACKBOT_LOG_LEVEL"] = "VERBOSE"
    with pytest.raises(ConfigError, match="SLACKBOT_LOG_LEVEL"):
        load_handler_settings(env)


# --- MCP_SERVER_URL (worker only) --------------------------------------------


def test_invalid_mcp_server_url_fails_startup() -> None:
    env = _worker_env()
    env["MCP_SERVER_URL"] = "not-a-url"
    with pytest.raises(ConfigError, match="MCP_SERVER_URL"):
        load_worker_settings(env)


# --- SLACK_USER_TOKEN_MAP (worker only, CTR-SB-006 mapping) -----------------


def test_invalid_json_user_token_map_fails_without_leaking_content() -> None:
    env = _worker_env()
    secret_token = "tok-super-secret-real-looking-value"
    env["SLACK_USER_TOKEN_MAP"] = f"{{not valid json but contains {secret_token}"

    with pytest.raises(ConfigError) as exc_info:
        load_worker_settings(env)

    message = str(exc_info.value)
    assert "SLACK_USER_TOKEN_MAP" in message
    assert secret_token not in message


def test_user_token_map_non_object_fails_startup() -> None:
    env = _worker_env()
    env["SLACK_USER_TOKEN_MAP"] = json.dumps(["U01ABCDEF", "some-token"])
    with pytest.raises(ConfigError, match="SLACK_USER_TOKEN_MAP"):
        load_worker_settings(env)


def test_user_token_map_schema_violation_fails_without_leaking_token_value() -> None:
    env = _worker_env()
    secret_token = "tok-schema-violation-secret-value"
    # A non-string value (an int) is a schema violation, but the surrounding
    # JSON string still literally contains the secret-looking token text.
    env["SLACK_USER_TOKEN_MAP"] = json.dumps({"U01ABCDEF": 12345, "_note": secret_token})

    with pytest.raises(ConfigError) as exc_info:
        load_worker_settings(env)

    message = str(exc_info.value)
    assert "SLACK_USER_TOKEN_MAP" in message
    assert secret_token not in message


def test_user_token_map_empty_key_fails_startup() -> None:
    env = _worker_env()
    env["SLACK_USER_TOKEN_MAP"] = json.dumps({"": "some-token"})
    with pytest.raises(ConfigError, match="SLACK_USER_TOKEN_MAP"):
        load_worker_settings(env)


def test_user_token_map_empty_value_fails_startup() -> None:
    env = _worker_env()
    env["SLACK_USER_TOKEN_MAP"] = json.dumps({"U01ABCDEF": ""})
    with pytest.raises(ConfigError, match="SLACK_USER_TOKEN_MAP"):
        load_worker_settings(env)


def test_user_token_map_parses_multiple_entries() -> None:
    env = _worker_env()
    mapping = {"U01ABCDEF": "token-one-fixture", "U02GHIJKL": "token-two-fixture"}
    env["SLACK_USER_TOKEN_MAP"] = json.dumps(mapping)

    settings = load_worker_settings(env)

    assert dict(settings.user_token_map) == mapping


# --- secret exposure ----------------------------------------------------------


def test_handler_settings_repr_does_not_expose_secrets() -> None:
    settings = load_handler_settings(_handler_env())
    rendered = repr(settings) + str(settings)
    assert VALID_TEST_SIGNING_SECRET not in rendered
    assert VALID_TEST_BOT_TOKEN not in rendered


def test_worker_settings_repr_does_not_expose_secrets() -> None:
    settings = load_worker_settings(_worker_env())
    rendered = repr(settings) + str(settings)
    assert VALID_TEST_SIGNING_SECRET not in rendered
    assert VALID_TEST_BOT_TOKEN not in rendered
    assert VALID_TEST_ANTHROPIC_API_KEY not in rendered
    for token in _VALID_TEST_USER_TOKEN_MAP.values():
        assert token not in rendered


def test_handler_missing_key_error_message_does_not_expose_other_secret_values() -> None:
    env = _handler_env()
    del env["SLACK_SIGNING_SECRET"]

    with pytest.raises(ConfigError) as exc_info:
        load_handler_settings(env)

    message = str(exc_info.value)
    assert VALID_TEST_BOT_TOKEN not in message


# --- AC-SB-005-5: CTR-SB-004 model/max_tokens/effort are fixed code constants,
# never read from the environment ---------------------------------------------


def test_claude_call_contract_values_are_fixed_code_constants() -> None:
    assert config.CLAUDE_MODEL == "claude-opus-5"
    assert config.CLAUDE_MAX_TOKENS == 8000
    assert config.CLAUDE_EFFORT == "medium"


def test_claude_call_contract_values_are_not_overridable_by_env() -> None:
    # No key in FRD §5.2's environment-key table names the model, max_tokens,
    # or effort — this pins that omission is intentional. Even hostile env
    # noise using plausible names must not change the module constants nor
    # leak through into WorkerSettings.
    env = _worker_env()
    env["CLAUDE_MODEL"] = "claude-haiku-1"
    env["ANTHROPIC_MODEL"] = "claude-haiku-1"
    env["CLAUDE_MAX_TOKENS"] = "1"
    env["CLAUDE_EFFORT"] = "low"

    settings = load_worker_settings(env)

    assert config.CLAUDE_MODEL == "claude-opus-5"
    assert config.CLAUDE_MAX_TOKENS == 8000
    assert config.CLAUDE_EFFORT == "medium"
    assert not hasattr(settings, "model")
    assert not hasattr(settings, "max_tokens")
    assert not hasattr(settings, "effort")
