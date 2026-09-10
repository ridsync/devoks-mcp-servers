"""Environment variables -> immutable ``Settings`` (DSN-006, Fail-Fast).

``load_settings`` is the sole entry point. It takes the environment mapping
as an explicit argument rather than reading ``os.environ`` itself, so tests
can inject a fake env without process-global side effects and so callers
(``app.py`` / ``server.py``) control exactly when a startup failure happens.

All problems found in one call — missing keys, out-of-range numbers, bad
JSON, an unparsable PEM — are collected and raised together in a single
``ConfigError`` (FRD §5.2, AC-001-5, AC-006-4), so a misconfigured deployment
is fixed in one edit-and-restart cycle instead of N.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization

from devoks_mcp_management.types import (
    CORE_GITHUB_TOOLS,
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

#: Required keys (FRD §5.2 / CTR-006). ``MCP_ALLOWED_HOSTS`` is required (not
#: merely recommended) because an empty allowlist does not fail loudly at
#: request time — ``transport_security`` silently answers every request with
#: 421, which looks like a generic transport error to callers (AC-001-5,
#: EDGE-002). Catching that at startup instead of in production traffic is
#: the whole point of Fail-Fast (DSN-006).
_REQUIRED_KEYS: tuple[str, ...] = (
    "MCP_ALLOWED_HOSTS",
    "MCP_PUBLIC_URL",
    "MCP_ISSUER_URL",
    "MCP_CLIENT_TOKENS",
    "MCP_ROLE_TOOLS",
    "GITHUB_APP_ID",
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_APP_INSTALLATION_ID",
)

_DEFAULT_PORT = 8000
_MIN_PORT = 1
_MAX_PORT = 65535

_DEFAULT_LOG_LEVEL = "INFO"
_VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})

#: CTR-008: 'owner/repo' exact match, no wildcards — restricted to GitHub's
#: own owner/repo character set so a stray '*' or '?' is rejected rather than
#: silently accepted as a literal (and never matched) allowlist entry.
_REPO_ALLOWLIST_ENTRY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

#: FRD §5.1 gives CTR-004/005/009 as bounded numeric contracts but §5.2 does
#: not assign them env var names — Stage 1 has no override surface documented
#: for them. These names follow the project's ``MCP_``-prefixed convention so
#: an operator can override the default without a code change; see the
#: handover note on TASK-003 for this gap-fill decision.
_ENV_READ_FILE_MAX_BYTES = "MCP_READ_FILE_MAX_BYTES"
_ENV_SEARCH_CODE_MAX_RESULTS = "MCP_SEARCH_CODE_MAX_RESULTS"
_ENV_TOKEN_REFRESH_LEEWAY_SECONDS = "MCP_TOKEN_REFRESH_LEEWAY_SECONDS"
#: Minimum length for a `MCP_CLIENT_TOKENS` key (TASK-046).
#:
#: WHY THIS VALIDATION EXISTS — a real incident, not a hypothetical:
#: the deployed Lambda was found running with the bearer token
#: `dev-local-token-change-me`, which is the literal placeholder published in
#: this repository's own tracked `.env.example`. The repository is public and
#: the Function URL had been written into a commit message, so the service
#: was effectively open to the internet. Access logs showed no third-party
#: IP, so nothing was exfiltrated, but the exposure was real.
#:
#: The root cause is structural, and it is worth naming precisely: the
#: `GITHUB_APP_PRIVATE_KEY` placeholder in `.env.example` is **invalid on
#: purpose** — `_parse_private_key` rejects it, so a deployment that forgot
#: to replace it fails to start. The token placeholder had no such property:
#: it was a perfectly valid token table, so copying `.env.example` forward
#: produced a *working* server with a *published* credential and no signal
#: at all.
#:
#: 32 characters is the floor rather than a specific format because CTR-002
#: does not constrain token shape. `secrets.token_urlsafe(32)` yields 43
#: characters / 256 bits, comfortably above it; anything a human types by
#: hand falls below it. Paired with a `.env.example` placeholder that is now
#: deliberately **too short to pass**, the placeholder can no longer reach
#: production silently.
_MIN_CLIENT_TOKEN_LENGTH = 32

_ENV_STATELESS_HTTP = "MCP_STATELESS_HTTP"
_ENV_JSON_RESPONSE = "MCP_JSON_RESPONSE"

# CTR-011: both default to True because the deployment target is Lambda +
# Function URL (FRD §10 Stage 2). A Lambda execution environment is frozen
# between invocations and replaced without warning, so a server that issues
# `Mcp-Session-Id` would hand clients a session no later invocation can be
# guaranteed to still hold; and an SSE stream that outlives the response is
# incompatible with the freeze. Flip both to `false` to run this same image
# behind a sticky-session load balancer instead (FRD §7's option (a)).
_DEFAULT_STATELESS_HTTP = True
_DEFAULT_JSON_RESPONSE = True

# Accepted spellings for the two boolean keys. Deliberately a closed set
# rather than Python's `bool(str)` (which makes "false" truthy) or
# `distutils.util.strtobool` (removed in 3.12): a typo'd value must be a
# start-up failure per DSN-006, not a silently-wrong protocol mode.
_TRUE_LITERALS = frozenset({"1", "true", "yes", "on"})
_FALSE_LITERALS = frozenset({"0", "false", "no", "off"})


class ConfigError(Exception):
    """One or more environment values failed validation at startup.

    The message lists every problem found in this call, not just the first.
    """


class _FieldError(Exception):
    """Internal control flow only: carries one field's user-facing message."""


@dataclass(frozen=True, slots=True)
class ClientToken:
    """One row of the CTR-002 token table, keyed by the bearer token itself."""

    client_id: str
    role: str
    scopes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable, fully-validated server configuration.

    Construct only via ``load_settings`` — every field has already passed
    range/schema/PEM validation by the time this object exists. The token
    table and the GitHub App private key are excluded from ``repr`` so that
    logging or raising a ``Settings`` instance can never leak a secret.
    """

    allowed_hosts: tuple[str, ...]
    public_url: str
    issuer_url: str
    repo_allowlist: frozenset[str]
    role_tools: Mapping[str, frozenset[str]]
    github_app_id: str
    github_app_installation_id: str
    port: int
    log_level: str
    read_file_max_bytes: int
    search_code_max_results: int
    token_refresh_leeway_seconds: int
    stateless_http: bool
    json_response: bool
    client_tokens: Mapping[str, ClientToken] = field(repr=False)
    github_app_private_key: str = field(repr=False)


def load_settings(env: Mapping[str, str]) -> Settings:
    """Parse and validate ``env`` into a ``Settings``.

    Raises ``ConfigError`` if any required key is missing or any value fails
    format/range/schema validation. All problems are collected before
    raising.
    """
    errors: list[str] = []

    missing = [key for key in _REQUIRED_KEYS if not (env.get(key) or "").strip()]
    if missing:
        errors.append("missing required environment variable(s): " + ", ".join(missing))

    allowed_hosts: tuple[str, ...] = ()
    if "MCP_ALLOWED_HOSTS" not in missing:
        try:
            allowed_hosts = _parse_allowed_hosts(env["MCP_ALLOWED_HOSTS"])
        except _FieldError as exc:
            errors.append(str(exc))

    public_url = ""
    if "MCP_PUBLIC_URL" not in missing:
        try:
            public_url = _parse_public_url(env["MCP_PUBLIC_URL"])
        except _FieldError as exc:
            errors.append(str(exc))

    issuer_url = ""
    if "MCP_ISSUER_URL" not in missing:
        try:
            issuer_url = _parse_url(env["MCP_ISSUER_URL"], "MCP_ISSUER_URL")
        except _FieldError as exc:
            errors.append(str(exc))

    client_tokens: Mapping[str, ClientToken] = {}
    if "MCP_CLIENT_TOKENS" not in missing:
        try:
            client_tokens = _parse_client_tokens(env["MCP_CLIENT_TOKENS"])
        except _FieldError as exc:
            errors.append(str(exc))

    role_tools: Mapping[str, frozenset[str]] = {}
    if "MCP_ROLE_TOOLS" not in missing:
        try:
            role_tools = _parse_role_tools(env["MCP_ROLE_TOOLS"])
        except _FieldError as exc:
            errors.append(str(exc))

    if client_tokens and role_tools:
        try:
            _validate_token_roles(client_tokens, role_tools)
        except _FieldError as exc:
            errors.append(str(exc))

    github_app_id = (env.get("GITHUB_APP_ID") or "").strip()
    github_app_installation_id = (env.get("GITHUB_APP_INSTALLATION_ID") or "").strip()

    github_app_private_key = ""
    if "GITHUB_APP_PRIVATE_KEY" not in missing:
        try:
            github_app_private_key = _parse_private_key(env["GITHUB_APP_PRIVATE_KEY"])
        except _FieldError as exc:
            errors.append(str(exc))

    # MCP_REPO_ALLOWLIST: absent/empty is the fail-safe default (EDGE-001) —
    # an empty allowlist denies every repo, so it is never a missing-key
    # failure. This is the opposite direction from every other key above.
    repo_allowlist: frozenset[str]
    try:
        repo_allowlist = _parse_repo_allowlist(env.get("MCP_REPO_ALLOWLIST", ""))
    except _FieldError as exc:
        errors.append(str(exc))
        repo_allowlist = frozenset()

    try:
        port = _parse_int_in_range(
            env.get("MCP_PORT"), "MCP_PORT", _DEFAULT_PORT, _MIN_PORT, _MAX_PORT
        )
    except _FieldError as exc:
        errors.append(str(exc))
        port = _DEFAULT_PORT

    try:
        log_level = _parse_log_level(env.get("MCP_LOG_LEVEL"))
    except _FieldError as exc:
        errors.append(str(exc))
        log_level = _DEFAULT_LOG_LEVEL

    try:
        read_file_max_bytes = _parse_int_in_range(
            env.get(_ENV_READ_FILE_MAX_BYTES),
            _ENV_READ_FILE_MAX_BYTES,
            READ_FILE_MAX_BYTES_DEFAULT,
            READ_FILE_MAX_BYTES_MIN,
            READ_FILE_MAX_BYTES_MAX,
        )
    except _FieldError as exc:
        errors.append(str(exc))
        read_file_max_bytes = READ_FILE_MAX_BYTES_DEFAULT

    try:
        search_code_max_results = _parse_int_in_range(
            env.get(_ENV_SEARCH_CODE_MAX_RESULTS),
            _ENV_SEARCH_CODE_MAX_RESULTS,
            SEARCH_CODE_MAX_RESULTS_DEFAULT,
            SEARCH_CODE_MAX_RESULTS_MIN,
            SEARCH_CODE_MAX_RESULTS_MAX,
        )
    except _FieldError as exc:
        errors.append(str(exc))
        search_code_max_results = SEARCH_CODE_MAX_RESULTS_DEFAULT

    try:
        token_refresh_leeway_seconds = _parse_int_in_range(
            env.get(_ENV_TOKEN_REFRESH_LEEWAY_SECONDS),
            _ENV_TOKEN_REFRESH_LEEWAY_SECONDS,
            TOKEN_REFRESH_LEEWAY_SECONDS_DEFAULT,
            TOKEN_REFRESH_LEEWAY_SECONDS_MIN,
            TOKEN_REFRESH_LEEWAY_SECONDS_MAX,
        )
    except _FieldError as exc:
        errors.append(str(exc))
        token_refresh_leeway_seconds = TOKEN_REFRESH_LEEWAY_SECONDS_DEFAULT

    try:
        stateless_http = _parse_bool(
            env.get(_ENV_STATELESS_HTTP), _ENV_STATELESS_HTTP, _DEFAULT_STATELESS_HTTP
        )
    except _FieldError as exc:
        errors.append(str(exc))
        stateless_http = _DEFAULT_STATELESS_HTTP

    try:
        json_response = _parse_bool(
            env.get(_ENV_JSON_RESPONSE), _ENV_JSON_RESPONSE, _DEFAULT_JSON_RESPONSE
        )
    except _FieldError as exc:
        errors.append(str(exc))
        json_response = _DEFAULT_JSON_RESPONSE

    if errors:
        raise ConfigError("; ".join(errors))

    return Settings(
        allowed_hosts=allowed_hosts,
        public_url=public_url,
        issuer_url=issuer_url,
        repo_allowlist=repo_allowlist,
        role_tools=role_tools,
        github_app_id=github_app_id,
        github_app_installation_id=github_app_installation_id,
        port=port,
        log_level=log_level,
        read_file_max_bytes=read_file_max_bytes,
        search_code_max_results=search_code_max_results,
        token_refresh_leeway_seconds=token_refresh_leeway_seconds,
        stateless_http=stateless_http,
        json_response=json_response,
        client_tokens=client_tokens,
        github_app_private_key=github_app_private_key,
    )


def _parse_allowed_hosts(raw: str) -> tuple[str, ...]:
    hosts = tuple(host.strip() for host in raw.split(",") if host.strip())
    if not hosts:
        raise _FieldError("MCP_ALLOWED_HOSTS must list at least one host")
    return hosts


def _parse_url(raw: str, key: str) -> str:
    value = raw.strip()
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise _FieldError(f"{key} is not a valid http(s) URL: {value!r}")
    return value


#: TASK-009 empirical finding (FRD §5.2): the SDK derives CTR-001's
#: well-known discovery route from MCP_PUBLIC_URL's *path component*, and a
#: misconfigured path does not fail the server startup on its own — only the
#: RFC 9728 discovery route silently lands somewhere other than
#: '/.well-known/oauth-protected-resource/mcp'. This is the same class of
#: silent failure as an empty MCP_ALLOWED_HOSTS, so it is caught here
#: (DSN-006) rather than left for a client to discover. A path *prefix*
#: (e.g. '/management/mcp') is a legitimate reverse-proxy topology (Stage 2)
#: and must stay allowed — only an empty/root path, a trailing slash, or a
#: final segment other than 'mcp' are rejected.
def _parse_public_url(raw: str) -> str:
    value = _parse_url(raw, "MCP_PUBLIC_URL")
    path = urlsplit(value).path
    if path in ("", "/"):
        raise _FieldError(
            "MCP_PUBLIC_URL must include a path ending in '/mcp' — the well-known "
            "discovery path (CTR-001) is derived from this URL's path, e.g. "
            f"'https://host/mcp'; got {value!r} with no path"
        )
    if path.endswith("/"):
        raise _FieldError(
            "MCP_PUBLIC_URL must not have a trailing slash — it carries into the "
            f"well-known discovery path, e.g. use 'https://host/mcp' not {value!r}"
        )
    if path.rsplit("/", 1)[-1] != "mcp":
        raise _FieldError(
            "MCP_PUBLIC_URL's last path segment must be 'mcp' to match the "
            "server's MCP endpoint (CTR-001), e.g. 'https://host/mcp' or, behind "
            f"a reverse-proxy path prefix, 'https://host/management/mcp'; got {value!r}"
        )
    return value


def _parse_repo_allowlist(raw: str) -> frozenset[str]:
    entries = tuple(entry.strip() for entry in raw.split(",") if entry.strip())
    invalid = [entry for entry in entries if not _REPO_ALLOWLIST_ENTRY.match(entry)]
    if invalid:
        raise _FieldError(
            "MCP_REPO_ALLOWLIST entries must be 'owner/repo' with no wildcards, "
            f"invalid: {', '.join(invalid)}"
        )
    return frozenset(entries)


def _load_json_object(raw: str, key: str) -> dict[str, Any]:
    """Parse ``raw`` as JSON and require it to be an object.

    ``json.loads`` returns ``Any`` — this centralizes the one cast the rest
    of the module needs, so callers work with a properly typed dict instead
    of re-deriving ``Unknown`` from ``isinstance`` narrowing at each call
    site (pyright narrows ``Any`` through ``isinstance`` to ``Unknown``, not
    ``Any``).
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _FieldError(f"{key} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise _FieldError(f"{key} must be a JSON object")
    return cast(dict[str, Any], data)


def _as_str_list(value: object) -> list[str] | None:
    """Return ``value`` as ``list[str]``, or ``None`` if it is not one."""
    if not isinstance(value, list):
        return None
    items = cast(list[Any], value)
    if not all(isinstance(item, str) for item in items):
        return None
    return cast(list[str], items)


def _parse_client_tokens(raw: str) -> Mapping[str, ClientToken]:
    data = _load_json_object(raw, "MCP_CLIENT_TOKENS")

    tokens: dict[str, ClientToken] = {}
    problems: list[str] = []
    # Entries are identified by ordinal position, never by echoing the token
    # value itself — the JSON key here *is* the bearer credential, and this
    # error can end up in logs.
    for index, (token, entry) in enumerate(data.items(), start=1):
        if not token:
            problems.append(f"MCP_CLIENT_TOKENS entry #{index}: key must be a non-empty string")
            continue
        if len(token) < _MIN_CLIENT_TOKEN_LENGTH:
            # The length is reported, the token is not — this message can end
            # up in logs, and a too-short token is still a credential.
            problems.append(
                f"MCP_CLIENT_TOKENS entry #{index}: token must be at least "
                f"{_MIN_CLIENT_TOKEN_LENGTH} characters, got {len(token)}. "
                "Generate one with: python3 -c "
                "'import secrets; print(secrets.token_urlsafe(32))'"
            )
            continue
        if not isinstance(entry, dict):
            problems.append(f"MCP_CLIENT_TOKENS entry #{index}: value must be an object")
            continue
        entry_obj = cast(dict[str, Any], entry)
        client_id = entry_obj.get("client_id")
        role = entry_obj.get("role")
        scopes = _as_str_list(entry_obj.get("scopes"))
        if not isinstance(client_id, str) or not client_id:
            problems.append(
                f"MCP_CLIENT_TOKENS entry #{index}: 'client_id' must be a non-empty string"
            )
            continue
        if not isinstance(role, str) or not role:
            problems.append(f"MCP_CLIENT_TOKENS entry #{index}: 'role' must be a non-empty string")
            continue
        if scopes is None:
            problems.append(f"MCP_CLIENT_TOKENS entry #{index}: 'scopes' must be a list of strings")
            continue
        tokens[token] = ClientToken(client_id=client_id, role=role, scopes=tuple(scopes))

    if problems:
        raise _FieldError("; ".join(problems))

    return tokens


def _parse_role_tools(raw: str) -> Mapping[str, frozenset[str]]:
    data = _load_json_object(raw, "MCP_ROLE_TOOLS")

    role_tools: dict[str, frozenset[str]] = {}
    problems: list[str] = []
    for role, tool_names_raw in data.items():
        if not role:
            problems.append("MCP_ROLE_TOOLS keys must be non-empty role names")
            continue
        tool_names = _as_str_list(tool_names_raw)
        if tool_names is None:
            problems.append(f"MCP_ROLE_TOOLS role {role!r} must map to a list of tool names")
            continue
        # Reject roles that grant tools the server does not actually expose —
        # left unchecked this is a silent authorization hole (see types.py
        # CORE_GITHUB_TOOLS docstring).
        unknown = sorted(set(tool_names) - CORE_GITHUB_TOOLS)
        if unknown:
            problems.append(
                f"MCP_ROLE_TOOLS role {role!r} references unknown tool(s): {', '.join(unknown)}"
            )
            continue
        role_tools[role] = frozenset(tool_names)

    if problems:
        raise _FieldError("; ".join(problems))

    return role_tools


def _validate_token_roles(
    client_tokens: Mapping[str, ClientToken],
    role_tools: Mapping[str, frozenset[str]],
) -> None:
    unknown_roles = sorted({token.role for token in client_tokens.values()} - role_tools.keys())
    if unknown_roles:
        raise _FieldError(
            "MCP_CLIENT_TOKENS references role(s) not present in MCP_ROLE_TOOLS: "
            + ", ".join(unknown_roles)
        )


def _parse_private_key(raw: str) -> str:
    # A PEM injected as a single-line env var commonly arrives with literal
    # "\n" escape sequences instead of real newlines; normalize before
    # parsing.
    normalized = raw.replace("\\n", "\n").strip()
    try:
        serialization.load_pem_private_key(normalized.encode("utf-8"), password=None)
    except (ValueError, TypeError) as exc:
        # Never include key material in the message (EDGE-008, AC-004-3
        # direction) — only that GITHUB_APP_PRIVATE_KEY failed to parse.
        raise _FieldError(
            f"GITHUB_APP_PRIVATE_KEY is not a valid PEM private key ({type(exc).__name__})"
        ) from exc
    return normalized


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


def _parse_bool(raw: str | None, key: str, default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in _TRUE_LITERALS:
        return True
    if value in _FALSE_LITERALS:
        return False
    raise _FieldError(
        f"{key} must be one of {sorted(_TRUE_LITERALS | _FALSE_LITERALS)}, got {raw!r}"
    )


def _parse_log_level(raw: str | None) -> str:
    if raw is None or not raw.strip():
        return _DEFAULT_LOG_LEVEL
    value = raw.strip().upper()
    if value not in _VALID_LOG_LEVELS:
        raise _FieldError(f"MCP_LOG_LEVEL must be one of {sorted(_VALID_LOG_LEVELS)}, got {raw!r}")
    return value
