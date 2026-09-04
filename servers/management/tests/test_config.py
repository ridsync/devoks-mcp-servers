"""Tests for devoks_mcp_management.config (TASK-003, TASK-011).

Traces: AC-001-5, AC-002-4, AC-002-6, AC-006-4, CTR-001, CTR-006, EDGE-002,
EDGE-008, DSN-006.
"""

import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

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


def _generate_pem() -> str:
    """A throwaway RSA key generated in-process — never committed to disk."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem.decode("utf-8")


@pytest.fixture(scope="module")
def pem() -> str:
    return _generate_pem()


def _valid_env(pem_value: str) -> dict[str, str]:
    return {
        "MCP_ALLOWED_HOSTS": "localhost,mcp.example.com",
        "MCP_PUBLIC_URL": "https://mcp.example.com/mcp",
        "MCP_ISSUER_URL": "https://issuer.example.com",
        "MCP_CLIENT_TOKENS": json.dumps(
            {
                "tok-abc123": {
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
    assert settings.client_tokens["tok-abc123"].client_id == "claude-code"
    assert settings.client_tokens["tok-abc123"].role == "reader"
    assert settings.client_tokens["tok-abc123"].scopes == ("devoks:read",)
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
        {"tok-abc123": {"client_id": "claude-code", "role": "admin", "scopes": ["devoks:read"]}}
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


# --- secret exposure ------------------------------------------------------------


def test_settings_repr_and_str_do_not_expose_token_table_or_private_key(pem: str) -> None:
    settings = load_settings(_valid_env(pem))

    rendered = repr(settings) + str(settings)

    assert "tok-abc123" not in rendered
    assert pem not in rendered
    # A representative substring from the PEM body, in case of re-wrapping.
    assert "PRIVATE KEY" not in rendered
