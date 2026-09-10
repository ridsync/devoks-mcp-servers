"""Shared test fixtures and factories (TASK-047).

Three things were duplicated across five-plus test modules before this file
existed, and each duplication had a concrete cost:

1. **RSA key generation** (`rsa.generate_private_key`) appeared in
   `test_config.py`, `test_github_lifespan.py`, and `test_github_tools.py`.
   A 2048-bit keygen is the single slowest operation in this suite, so three
   module-scoped copies meant paying for it three times per run.

2. **The 15-field `Settings(...)` constructor call** appeared in seven
   modules. That count is why adding two fields for `CTR-011`
   (`stateless_http`/`json_response`) broke 76 tests at once: every copy had
   to be edited. `make_settings` below is now the single place a new
   `Settings` field has to be defaulted for tests.

3. **A bearer-token literal**, which is now load-bearing: `config.py`
   enforces a 32-character floor (`TASK-046`), so a short literal is a
   start-up failure. `VALID_TEST_TOKEN` is the one spelling that is known to
   clear it.

`Settings` itself deliberately has **no dataclass defaults** — the single
source of truth for a default is `config.py`, and duplicating those values
onto the dataclass would let the two drift. That decision is what makes a
shared test factory necessary rather than merely convenient.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from devoks_mcp_management.config import ClientToken, Settings
from devoks_mcp_management.types import (
    READ_FILE_MAX_BYTES_DEFAULT,
    SEARCH_CODE_MAX_RESULTS_DEFAULT,
    TOKEN_REFRESH_LEEWAY_SECONDS_DEFAULT,
)

#: Clears `config.py`'s `_MIN_CLIENT_TOKEN_LENGTH` floor (TASK-046) with room
#: to spare, and says out loud that it is a fixture rather than a credential —
#: the incident TASK-046 exists to prevent was a *test-shaped* token reaching
#: production, so a literal that reads like a real token is itself a hazard.
VALID_TEST_TOKEN = "test-fixture-token-not-a-real-credential"

#: Default host/URL trio used by the HTTP-layer tests. Kept here so the
#: `MCP_PUBLIC_URL` path rule (`CTR-001`: ends in `/mcp`, no trailing slash)
#: is satisfied once instead of restated per module.
DEFAULT_ALLOWED_HOST = "mcp.example.com"
DEFAULT_PUBLIC_URL = f"https://{DEFAULT_ALLOWED_HOST}/mcp"
DEFAULT_ISSUER_URL = "https://issuer.example.com"

#: The four Stage 1 GitHub tools, and the single Stage 1 role.
READER_ROLE = "reader"
ALL_FOUR_TOOLS = frozenset({"list_repos", "get_repo_tree", "read_file", "search_code"})


def generate_pem() -> str:
    """A throwaway RSA private key, generated in-process, never written to disk."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")


@pytest.fixture(scope="session")
def pem() -> str:
    """Session-scoped so the 2048-bit keygen runs once for the whole suite.

    Session rather than module scope is safe because the value is an
    immutable `str` that no test mutates.
    """
    return generate_pem()


def make_settings(
    *,
    allowed_hosts: tuple[str, ...] = (DEFAULT_ALLOWED_HOST,),
    public_url: str = DEFAULT_PUBLIC_URL,
    issuer_url: str = DEFAULT_ISSUER_URL,
    repo_allowlist: frozenset[str] = frozenset({"ridsync/devoks-mcp-servers"}),
    role_tools: Mapping[str, frozenset[str]] | None = None,
    client_tokens: Mapping[str, ClientToken] | None = None,
    github_app_id: str = "app-id",
    github_app_installation_id: str = "install-id",
    github_app_private_key: str = "unused-unless-a-test-issues-a-token",
    port: int = 8000,
    log_level: str = "INFO",
    read_file_max_bytes: int = READ_FILE_MAX_BYTES_DEFAULT,
    search_code_max_results: int = SEARCH_CODE_MAX_RESULTS_DEFAULT,
    token_refresh_leeway_seconds: int = TOKEN_REFRESH_LEEWAY_SECONDS_DEFAULT,
    stateless_http: bool = True,
    json_response: bool = True,
) -> Settings:
    """Build a `Settings` directly, bypassing `load_settings`.

    Direct construction is deliberate for every caller except
    `test_config.py`: those modules test the server/HTTP/tool layers, and
    routing through `load_settings` would make each of them depend on
    environment-variable *parsing* too. `test_config.py` is the one module
    that must go through `load_settings`, because parsing is its subject.

    `github_app_private_key` defaults to a non-PEM string on purpose. Nothing
    validates it at this layer (validation lives in `load_settings`), and a
    test that actually issues an installation token must pass the real `pem`
    fixture — so a placeholder that could never be mistaken for a key makes
    that requirement obvious at the call site.

    `stateless_http`/`json_response` default to `True`, matching `config.py`'s
    own defaults (`CTR-011`) so tests exercise the deployed configuration
    unless they say otherwise.
    """
    return Settings(
        allowed_hosts=allowed_hosts,
        public_url=public_url,
        issuer_url=issuer_url,
        repo_allowlist=repo_allowlist,
        role_tools={READER_ROLE: ALL_FOUR_TOOLS} if role_tools is None else role_tools,
        github_app_id=github_app_id,
        github_app_installation_id=github_app_installation_id,
        port=port,
        log_level=log_level,
        read_file_max_bytes=read_file_max_bytes,
        search_code_max_results=search_code_max_results,
        token_refresh_leeway_seconds=token_refresh_leeway_seconds,
        stateless_http=stateless_http,
        json_response=json_response,
        client_tokens={} if client_tokens is None else client_tokens,
        github_app_private_key=github_app_private_key,
    )
