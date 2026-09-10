"""Tests for devoks_mcp_management.config (TASK-003, TASK-011).

Traces: AC-001-5, AC-002-4, AC-002-6, AC-006-4, CTR-001, CTR-006, EDGE-002,
EDGE-008, DSN-006.
"""

import json
import secrets

import pytest

from conftest import VALID_TEST_TOKEN
from devoks_mcp_management.config import ConfigError, Settings, load_settings
from devoks_mcp_management.types import (
    READ_FILE_MAX_BYTES_DEFAULT,
    READ_FILE_MAX_BYTES_MAX,
    READ_FILE_MAX_BYTES_MIN,
    SEARCH_CODE_MAX_RESULTS_DEFAULT,
    SEARCH_CODE_MAX_RESULTS_MAX,
    SEARCH_CODE_MAX_RESULTS_MIN,
    TOKEN_REFRESH_LEEWAY_SECONDS_DEFAULT,
    TOKEN_REFRESH_LEEWAY_SECONDS_MAX,
    TOKEN_REFRESH_LEEWAY_SECONDS_MIN,
)

# `generate_pem` / the session-scoped `pem` fixture now live in conftest.py
# (TASK-047) — three modules were each paying for their own 2048-bit keygen.


def _valid_env(pem_value: str) -> dict[str, str]:
    return {
        "MCP_ALLOWED_HOSTS": "localhost,mcp.example.com",
        "MCP_PUBLIC_URL": "https://mcp.example.com/mcp",
        "MCP_ISSUER_URL": "https://issuer.example.com",
        "MCP_CLIENT_TOKENS": json.dumps(
            {
                VALID_TEST_TOKEN: {
                    "client_id": "claude-code",
                    "role": "reader",
                    "scopes": ["devoks:read"],
                }
            }
        ),
        "MCP_REPO_ALLOWLIST": "ridsync/devoks-mcp-servers",
        "MCP_ROLE_TOOLS": json.dumps(
            {"reader": ["list_repos", "get_repo_tree", "read_file", "search_code"]}
        ),
        "GITHUB_APP_ID": "123456",
        "GITHUB_APP_PRIVATE_KEY": pem_value,
        "GITHUB_APP_INSTALLATION_ID": "789012",
    }


# --- normal cases ------------------------------------------------------------


def test_valid_env_loads_settings_successfully(pem: str) -> None:
    # AC-002-6, CTR-006: all required keys present and well-formed succeeds.
    settings = load_settings(_valid_env(pem))

    assert isinstance(settings, Settings)
    assert settings.allowed_hosts == ("localhost", "mcp.example.com")
    assert settings.public_url == "https://mcp.example.com/mcp"
    assert settings.issuer_url == "https://issuer.example.com"
    assert settings.repo_allowlist == frozenset({"ridsync/devoks-mcp-servers"})
    assert settings.role_tools == {
        "reader": frozenset({"list_repos", "get_repo_tree", "read_file", "search_code"})
    }
    assert settings.client_tokens[VALID_TEST_TOKEN].client_id == "claude-code"
    assert settings.client_tokens[VALID_TEST_TOKEN].role == "reader"
    assert settings.client_tokens[VALID_TEST_TOKEN].scopes == ("devoks:read",)
    assert settings.github_app_id == "123456"
    assert settings.github_app_installation_id == "789012"
    assert settings.port == 8000
    assert settings.log_level == "INFO"
    assert settings.read_file_max_bytes == READ_FILE_MAX_BYTES_DEFAULT
    assert settings.search_code_max_results == SEARCH_CODE_MAX_RESULTS_DEFAULT
    assert settings.token_refresh_leeway_seconds == TOKEN_REFRESH_LEEWAY_SECONDS_DEFAULT


def test_load_settings_is_deterministic_across_repeated_calls(pem: str) -> None:
    # Repeated-call case: same input twice must yield equal Settings (no
    # hidden global state, no incidental randomness).
    env = _valid_env(pem)
    first = load_settings(env)
    second = load_settings(env)
    assert first == second


def test_optional_numeric_overrides_within_bounds_are_applied(pem: str) -> None:
    env = _valid_env(pem)
    env["MCP_PORT"] = "9000"
    env["MCP_LOG_LEVEL"] = "debug"
    env["MCP_READ_FILE_MAX_BYTES"] = "4096"
    env["MCP_SEARCH_CODE_MAX_RESULTS"] = "10"
    env["MCP_TOKEN_REFRESH_LEEWAY_SECONDS"] = "120"

    settings = load_settings(env)

    assert settings.port == 9000
    assert settings.log_level == "DEBUG"
    assert settings.read_file_max_bytes == 4096
    assert settings.search_code_max_results == 10
    assert settings.token_refresh_leeway_seconds == 120


# --- boundary cases ------------------------------------------------------------


@pytest.mark.parametrize("value", [READ_FILE_MAX_BYTES_MIN, READ_FILE_MAX_BYTES_MAX])
def test_read_file_max_bytes_accepts_inclusive_bounds(pem: str, value: int) -> None:
    # CTR-004 boundary: MIN and MAX are valid, not off-by-one excluded.
    env = _valid_env(pem)
    env["MCP_READ_FILE_MAX_BYTES"] = str(value)
    settings = load_settings(env)
    assert settings.read_file_max_bytes == value


@pytest.mark.parametrize("value", [READ_FILE_MAX_BYTES_MIN - 1, READ_FILE_MAX_BYTES_MAX + 1])
def test_read_file_max_bytes_out_of_range_fails_startup(pem: str, value: int) -> None:
    # CTR-004 boundary: one past MIN/MAX must fail.
    env = _valid_env(pem)
    env["MCP_READ_FILE_MAX_BYTES"] = str(value)
    with pytest.raises(ConfigError, match="MCP_READ_FILE_MAX_BYTES"):
        load_settings(env)


@pytest.mark.parametrize(
    "value", [SEARCH_CODE_MAX_RESULTS_MIN - 1, SEARCH_CODE_MAX_RESULTS_MAX + 1]
)
def test_search_code_max_results_out_of_range_fails_startup(pem: str, value: int) -> None:
    # CTR-005 boundary.
    env = _valid_env(pem)
    env["MCP_SEARCH_CODE_MAX_RESULTS"] = str(value)
    with pytest.raises(ConfigError, match="MCP_SEARCH_CODE_MAX_RESULTS"):
        load_settings(env)


@pytest.mark.parametrize(
    "value", [TOKEN_REFRESH_LEEWAY_SECONDS_MIN - 1, TOKEN_REFRESH_LEEWAY_SECONDS_MAX + 1]
)
def test_token_refresh_leeway_out_of_range_fails_startup(pem: str, value: int) -> None:
    # CTR-009 boundary.
    env = _valid_env(pem)
    env["MCP_TOKEN_REFRESH_LEEWAY_SECONDS"] = str(value)
    with pytest.raises(ConfigError, match="MCP_TOKEN_REFRESH_LEEWAY_SECONDS"):
        load_settings(env)


def test_repo_allowlist_empty_value_succeeds_with_empty_set(pem: str) -> None:
    # EDGE-001: empty allowlist is the fail-safe default, must NOT fail startup.
    env = _valid_env(pem)
    env["MCP_REPO_ALLOWLIST"] = ""
    settings = load_settings(env)
    assert settings.repo_allowlist == frozenset()


def test_repo_allowlist_missing_key_succeeds_with_empty_set(pem: str) -> None:
    # EDGE-001: key entirely absent is also the fail-safe default, not a
    # missing-required-key failure — opposite direction from MCP_ALLOWED_HOSTS.
    env = _valid_env(pem)
    del env["MCP_REPO_ALLOWLIST"]
    settings = load_settings(env)
    assert settings.repo_allowlist == frozenset()


# --- error cases ------------------------------------------------------------


def test_missing_allowed_hosts_key_fails_startup(pem: str) -> None:
    # AC-001-5, EDGE-002.
    env = _valid_env(pem)
    del env["MCP_ALLOWED_HOSTS"]
    with pytest.raises(ConfigError, match="MCP_ALLOWED_HOSTS"):
        load_settings(env)


def test_empty_allowed_hosts_value_fails_startup(pem: str) -> None:
    # AC-001-5, EDGE-002: present but blank counts as missing.
    env = _valid_env(pem)
    env["MCP_ALLOWED_HOSTS"] = "   "
    with pytest.raises(ConfigError, match="MCP_ALLOWED_HOSTS"):
        load_settings(env)


def test_missing_multiple_required_keys_reports_all_names_at_once(pem: str) -> None:
    # AC-001-5, AC-006-4: N missing keys reported in a single error, not one
    # exception per restart.
    env = _valid_env(pem)
    del env["MCP_PUBLIC_URL"]
    del env["GITHUB_APP_ID"]

    with pytest.raises(ConfigError) as exc_info:
        load_settings(env)

    message = str(exc_info.value)
    assert "MCP_PUBLIC_URL" in message
    assert "GITHUB_APP_ID" in message


def test_missing_github_app_env_reports_missing_key_name(pem: str) -> None:
    # AC-006-4.
    env = _valid_env(pem)
    del env["GITHUB_APP_INSTALLATION_ID"]
    with pytest.raises(ConfigError, match="GITHUB_APP_INSTALLATION_ID"):
        load_settings(env)


def test_invalid_public_url_fails_startup(pem: str) -> None:
    env = _valid_env(pem)
    env["MCP_PUBLIC_URL"] = "not-a-url"
    with pytest.raises(ConfigError, match="MCP_PUBLIC_URL"):
        load_settings(env)


# --- MCP_PUBLIC_URL path validation (TASK-011) -------------------------------


def test_public_url_with_mcp_path_succeeds(pem: str) -> None:  # CTR-001, AC-002-4
    # Normal form: a bare '/mcp' path is the documented CTR-001 shape.
    env = _valid_env(pem)
    env["MCP_PUBLIC_URL"] = "https://mcp.example.com/mcp"
    settings = load_settings(env)
    assert settings.public_url == "https://mcp.example.com/mcp"


def test_public_url_without_path_fails_startup(pem: str) -> None:  # CTR-001, DSN-006
    # The bare domain derives '/.well-known/oauth-protected-resource' with no
    # '/mcp' suffix at all (TASK-009 empirical finding) — must fail startup,
    # not silently break well-known discovery in production.
    env = _valid_env(pem)
    env["MCP_PUBLIC_URL"] = "https://mcp.example.com"
    with pytest.raises(ConfigError) as exc_info:
        load_settings(env)
    message = str(exc_info.value)
    assert "MCP_PUBLIC_URL" in message
    assert "/mcp" in message  # correct-example guidance is present


def test_public_url_slash_only_path_fails_startup(pem: str) -> None:  # CTR-001, DSN-006
    env = _valid_env(pem)
    env["MCP_PUBLIC_URL"] = "https://mcp.example.com/"
    with pytest.raises(ConfigError, match="MCP_PUBLIC_URL"):
        load_settings(env)


def test_public_url_trailing_slash_fails_startup(pem: str) -> None:  # CTR-001, DSN-006
    # A trailing slash on the path carries into the well-known route
    # ('.../oauth-protected-resource/mcp/'), breaking CTR-001's exact path.
    env = _valid_env(pem)
    env["MCP_PUBLIC_URL"] = "https://mcp.example.com/mcp/"
    with pytest.raises(ConfigError) as exc_info:
        load_settings(env)
    message = str(exc_info.value)
    assert "MCP_PUBLIC_URL" in message
    assert "trailing slash" in message
    assert "https://host/mcp" in message  # correct-example guidance is present


def test_public_url_with_reverse_proxy_path_prefix_succeeds(pem: str) -> None:
    # CTR-001, DSN-006: a reverse-proxy path prefix (e.g. ALB mapping
    # '/management/mcp' -> container '/mcp') is a legitimate Stage 2
    # topology and must NOT be rejected — regression guard against
    # over-strict validation.
    env = _valid_env(pem)
    env["MCP_PUBLIC_URL"] = "https://mcp.example.com/management/mcp"
    settings = load_settings(env)
    assert settings.public_url == "https://mcp.example.com/management/mcp"


def test_public_url_last_segment_not_mcp_fails_startup(pem: str) -> None:  # CTR-001, AC-002-4
    # The server always serves the MCP endpoint at '/mcp' (CTR-001); a
    # public URL whose final segment isn't 'mcp' produces a well-known
    # 'resource' that doesn't correspond to where the endpoint actually is.
    env = _valid_env(pem)
    env["MCP_PUBLIC_URL"] = "https://mcp.example.com/api"
    with pytest.raises(ConfigError, match="MCP_PUBLIC_URL"):
        load_settings(env)


def test_public_url_path_error_reported_with_other_config_errors(pem: str) -> None:
    # AC-001-5, AC-006-4: the "collect everything, report once" contract
    # must also hold for this new validation rule.
    env = _valid_env(pem)
    env["MCP_PUBLIC_URL"] = "https://mcp.example.com/mcp/"
    del env["GITHUB_APP_ID"]

    with pytest.raises(ConfigError) as exc_info:
        load_settings(env)

    message = str(exc_info.value)
    assert "MCP_PUBLIC_URL" in message
    assert "GITHUB_APP_ID" in message


def test_invalid_pem_private_key_fails_and_omits_key_content(pem: str) -> None:
    # EDGE-008, AC-006-4, AC-004-3 direction: fails, message names the key,
    # never echoes key material.
    env = _valid_env(pem)
    bogus_key = "not-a-valid-pem-key-material"
    env["GITHUB_APP_PRIVATE_KEY"] = bogus_key

    with pytest.raises(ConfigError) as exc_info:
        load_settings(env)

    message = str(exc_info.value)
    assert "GITHUB_APP_PRIVATE_KEY" in message
    assert bogus_key not in message


def test_private_key_with_literal_newline_escapes_is_normalized(pem: str) -> None:
    # EDGE-008: PEM injected as a single-line env var with literal "\n".
    env = _valid_env(pem)
    env["GITHUB_APP_PRIVATE_KEY"] = pem.replace("\n", "\\n")

    settings = load_settings(env)

    assert "\n" in settings.github_app_private_key
    assert "\\n" not in settings.github_app_private_key


def test_invalid_json_client_tokens_fails_startup(pem: str) -> None:
    env = _valid_env(pem)
    env["MCP_CLIENT_TOKENS"] = "{not valid json"
    with pytest.raises(ConfigError, match="MCP_CLIENT_TOKENS"):
        load_settings(env)


def test_client_tokens_schema_violation_fails_without_leaking_token(pem: str) -> None:
    env = _valid_env(pem)
    secret_token = "tok-super-secret-value"
    env["MCP_CLIENT_TOKENS"] = json.dumps({secret_token: {"client_id": "x"}})  # missing role/scopes

    with pytest.raises(ConfigError) as exc_info:
        load_settings(env)

    message = str(exc_info.value)
    assert "MCP_CLIENT_TOKENS" in message
    assert secret_token not in message


def test_invalid_json_role_tools_fails_startup(pem: str) -> None:
    env = _valid_env(pem)
    env["MCP_ROLE_TOOLS"] = "not json"
    with pytest.raises(ConfigError, match="MCP_ROLE_TOOLS"):
        load_settings(env)


def test_role_tools_referencing_unknown_tool_fails_startup(pem: str) -> None:
    # A role/tool mapping that names a tool the server does not expose is a
    # silent authorization hole — must be rejected at startup.
    env = _valid_env(pem)
    env["MCP_ROLE_TOOLS"] = json.dumps({"reader": ["list_repos", "delete_repo"]})
    with pytest.raises(ConfigError, match="delete_repo"):
        load_settings(env)


def test_client_token_role_not_defined_in_role_tools_fails_startup(pem: str) -> None:
    env = _valid_env(pem)
    env["MCP_CLIENT_TOKENS"] = json.dumps(
        {VALID_TEST_TOKEN: {"client_id": "claude-code", "role": "admin", "scopes": ["devoks:read"]}}
    )
    # MCP_ROLE_TOOLS only defines "reader", not "admin".
    with pytest.raises(ConfigError, match="admin"):
        load_settings(env)


def test_repo_allowlist_rejects_wildcard_entries(pem: str) -> None:
    # CTR-008: exact-match only, no wildcards.
    env = _valid_env(pem)
    env["MCP_REPO_ALLOWLIST"] = "ridsync/*"
    with pytest.raises(ConfigError, match="MCP_REPO_ALLOWLIST"):
        load_settings(env)


def test_invalid_port_value_fails_startup(pem: str) -> None:
    env = _valid_env(pem)
    env["MCP_PORT"] = "not-an-int"
    with pytest.raises(ConfigError, match="MCP_PORT"):
        load_settings(env)


def test_invalid_log_level_fails_startup(pem: str) -> None:
    env = _valid_env(pem)
    env["MCP_LOG_LEVEL"] = "VERBOSE"
    with pytest.raises(ConfigError, match="MCP_LOG_LEVEL"):
        load_settings(env)


# --- CTR-011: MCP_STATELESS_HTTP / MCP_JSON_RESPONSE (TASK-051) ------------------


def test_protocol_mode_defaults_to_stateless_json_for_lambda(pem: str) -> None:
    """Both default to ``True`` because the deployment target is Lambda +
    Function URL (FRD §10 Stage 2). Asserted as an explicit fact rather than
    left implicit: flipping either default silently changes the wire
    protocol -- ``False``/``False`` makes the server issue ``Mcp-Session-Id``
    and stream SSE, which a Lambda execution environment cannot honor across
    invocations (FRD §7 "배포 타깃 제약").
    """
    settings = load_settings(_valid_env(pem))
    assert settings.stateless_http is True
    assert settings.json_response is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("true", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("false", False),
        ("False", False),
        ("0", False),
        ("no", False),
        ("off", False),
        ("  true  ", True),
    ],
)
def test_protocol_mode_accepts_documented_literals_case_insensitively(
    pem: str, raw: str, expected: bool
) -> None:
    env = _valid_env(pem)
    env["MCP_STATELESS_HTTP"] = raw
    env["MCP_JSON_RESPONSE"] = raw
    settings = load_settings(env)
    assert settings.stateless_http is expected
    assert settings.json_response is expected


def test_protocol_mode_rejects_unknown_literal_instead_of_guessing(pem: str) -> None:
    """``bool("false")`` is ``True`` in Python, so a permissive parser would
    turn the typo ``MCP_STATELESS_HTTP=flase`` into stateless mode silently.
    DSN-006 requires the opposite: fail start-up.
    """
    env = _valid_env(pem)
    env["MCP_STATELESS_HTTP"] = "flase"
    with pytest.raises(ConfigError, match="MCP_STATELESS_HTTP"):
        load_settings(env)


def test_both_protocol_mode_errors_are_reported_together(pem: str) -> None:
    """DSN-006's all-errors-collected contract: an operator fixing a bad
    deployment must see both bad keys in one run, not discover the second
    only after fixing the first.
    """
    env = _valid_env(pem)
    env["MCP_STATELESS_HTTP"] = "maybe"
    env["MCP_JSON_RESPONSE"] = "sometimes"
    with pytest.raises(ConfigError) as excinfo:
        load_settings(env)
    message = str(excinfo.value)
    assert "MCP_STATELESS_HTTP" in message
    assert "MCP_JSON_RESPONSE" in message


def test_empty_protocol_mode_value_falls_back_to_default(pem: str) -> None:
    """An env key present but empty (a very common shape when a deployment
    template renders an unset variable) must behave as unset, matching how
    every other optional key in CTR-006 already treats blanks.
    """
    env = _valid_env(pem)
    env["MCP_STATELESS_HTTP"] = ""
    env["MCP_JSON_RESPONSE"] = "   "
    settings = load_settings(env)
    assert settings.stateless_http is True
    assert settings.json_response is True


# --- secret exposure ------------------------------------------------------------


def test_settings_repr_and_str_do_not_expose_token_table_or_private_key(pem: str) -> None:
    settings = load_settings(_valid_env(pem))

    rendered = repr(settings) + str(settings)

    assert VALID_TEST_TOKEN not in rendered
    assert pem not in rendered
    # A representative substring from the PEM body, in case of re-wrapping.
    assert "PRIVATE KEY" not in rendered


# --- TASK-046: MCP_CLIENT_TOKENS minimum length --------------------------------


def test_the_published_env_example_placeholder_token_cannot_start_the_server(
    pem: str,
) -> None:
    """The exact string that reached production, pinned as a test.

    The deployed Lambda was found running with ``dev-local-token-change-me``
    — the literal placeholder published in this repository's own tracked
    ``.env.example``, in a public repo, behind a public Function URL. Access
    logs showed no third-party IP, but the credential was public.

    The root cause was structural: ``GITHUB_APP_PRIVATE_KEY``'s placeholder
    is invalid on purpose, so forgetting to replace it fails start-up, while
    the token placeholder was a *valid* token table and produced a working
    server with a published credential and no signal at all. This test is the
    regression guard for that specific string.
    """
    env = _valid_env(pem)
    env["MCP_CLIENT_TOKENS"] = json.dumps(
        {
            "dev-local-token-change-me": {
                "client_id": "local-dev",
                "role": "reader",
                "scopes": ["devoks:read"],
            }
        }
    )

    with pytest.raises(ConfigError, match="at least 32 characters"):
        load_settings(env)


@pytest.mark.parametrize("length", [1, 8, 25, 31])
def test_tokens_below_the_floor_fail_startup(pem: str, length: int) -> None:
    env = _valid_env(pem)
    env["MCP_CLIENT_TOKENS"] = json.dumps(
        {"x" * length: {"client_id": "c", "role": "reader", "scopes": ["devoks:read"]}}
    )

    with pytest.raises(ConfigError) as excinfo:
        load_settings(env)
    message = str(excinfo.value)
    assert "at least 32 characters" in message
    assert f"got {length}" in message
    # The token itself must never appear in an error that can reach a log.
    assert "x" * length not in message


@pytest.mark.parametrize("length", [32, 43, 64])
def test_tokens_at_or_above_the_floor_are_accepted(pem: str, length: int) -> None:
    env = _valid_env(pem)
    token = "y" * length
    env["MCP_CLIENT_TOKENS"] = json.dumps(
        {token: {"client_id": "c", "role": "reader", "scopes": ["devoks:read"]}}
    )

    settings = load_settings(env)

    assert token in settings.client_tokens


def test_secrets_token_urlsafe_32_clears_the_floor(pem: str) -> None:
    """The value README/`.env.example` tell operators to generate must pass.

    Pinned so a future floor increase cannot silently invalidate the
    documented recipe.
    """
    token = secrets.token_urlsafe(32)
    assert len(token) >= 32

    env = _valid_env(pem)
    env["MCP_CLIENT_TOKENS"] = json.dumps(
        {token: {"client_id": "c", "role": "reader", "scopes": ["devoks:read"]}}
    )

    assert token in load_settings(env).client_tokens


def test_short_token_error_is_collected_with_other_config_errors(pem: str) -> None:
    """DSN-006: all errors in one run, not one per fix-and-retry cycle."""
    env = _valid_env(pem)
    env["MCP_CLIENT_TOKENS"] = json.dumps(
        {"short": {"client_id": "c", "role": "reader", "scopes": ["devoks:read"]}}
    )
    env["MCP_PORT"] = "not-an-int"

    with pytest.raises(ConfigError) as excinfo:
        load_settings(env)
    message = str(excinfo.value)
    assert "at least 32 characters" in message
    assert "MCP_PORT" in message
