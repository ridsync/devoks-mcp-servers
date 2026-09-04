"""Tests for devoks_mcp_management.auth.verifier (TASK-006).

Traces: AC-002-1, AC-002-6, CTR-002, DSN-001.
"""

import pytest
from mcp.server.auth.provider import AccessToken, TokenVerifier
from pydantic import ValidationError

from devoks_mcp_management.auth.verifier import StaticTableTokenVerifier, get_role
from devoks_mcp_management.config import ClientToken

READER_TOKEN = "reader-token-abc123"
ADMIN_TOKEN = "admin-token-xyz789"
CLIENT_TOKENS: dict[str, ClientToken] = {
    READER_TOKEN: ClientToken(client_id="reader-client", role="reader", scopes=("devoks:read",)),
    ADMIN_TOKEN: ClientToken(
        client_id="admin-client", role="admin", scopes=("devoks:read", "devoks:admin")
    ),
}


# --- AC-002-1: valid token -> AccessToken with matching client_id/scopes ----


async def test_valid_token_returns_access_token_with_matching_fields() -> None:
    # AC-002-1
    verifier = StaticTableTokenVerifier(CLIENT_TOKENS)

    result = await verifier.verify_token(READER_TOKEN)

    assert result is not None
    assert result.client_id == "reader-client"
    assert result.scopes == ["devoks:read"]


async def test_role_round_trips_through_get_role_accessor() -> None:
    # AC-002-1 / CTR-002: role must survive the AccessToken hand-off so
    # TASK-007's authorize() call can read it back.
    verifier = StaticTableTokenVerifier(CLIENT_TOKENS)

    result = await verifier.verify_token(ADMIN_TOKEN)

    assert result is not None
    assert get_role(result) == "admin"


async def test_multiple_clients_each_receive_their_own_identity() -> None:
    # CTR-002: the table may hold several rows; each lookup must not leak
    # another client's identity.
    verifier = StaticTableTokenVerifier(CLIENT_TOKENS)

    reader = await verifier.verify_token(READER_TOKEN)
    admin = await verifier.verify_token(ADMIN_TOKEN)

    assert reader is not None
    assert admin is not None
    assert reader.client_id == "reader-client"
    assert get_role(reader) == "reader"
    assert reader.scopes == ["devoks:read"]
    assert admin.client_id == "admin-client"
    assert get_role(admin) == "admin"
    assert admin.scopes == ["devoks:read", "devoks:admin"]


# --- AC-002-2 precondition: unregistered/malformed tokens -> None ----------


async def test_unregistered_token_returns_none() -> None:
    # AC-002-2 (SDK converts this to 401; this module just returns None)
    verifier = StaticTableTokenVerifier(CLIENT_TOKENS)

    result = await verifier.verify_token("not-a-real-token")

    assert result is None


async def test_empty_string_token_returns_none() -> None:
    verifier = StaticTableTokenVerifier(CLIENT_TOKENS)

    result = await verifier.verify_token("")

    assert result is None


async def test_whitespace_only_token_returns_none() -> None:
    verifier = StaticTableTokenVerifier(CLIENT_TOKENS)

    result = await verifier.verify_token("   ")

    assert result is None


async def test_token_sharing_a_prefix_with_a_registered_token_returns_none() -> None:
    # Guards against a substring/prefix-match bug in the lookup.
    verifier = StaticTableTokenVerifier(CLIENT_TOKENS)

    result = await verifier.verify_token(READER_TOKEN[:-1])

    assert result is None


async def test_empty_table_returns_none_for_any_token() -> None:
    verifier = StaticTableTokenVerifier({})

    result = await verifier.verify_token(READER_TOKEN)

    assert result is None


# --- AC-002-6: no token literals reachable outside a fixture/injected table -


async def test_verifier_only_recognizes_tokens_from_its_injected_table() -> None:
    # AC-002-6 direction: verification behavior is fully determined by the
    # constructor argument, with no module-global fallback that could
    # recognize a token absent from the injected table.
    verifier_with_table = StaticTableTokenVerifier(CLIENT_TOKENS)
    verifier_with_empty_table = StaticTableTokenVerifier({})

    assert await verifier_with_table.verify_token(READER_TOKEN) is not None
    assert await verifier_with_empty_table.verify_token(READER_TOKEN) is None


# --- Protocol conformance and SDK model validity ----------------------------


def test_satisfies_token_verifier_protocol_via_type_annotation() -> None:
    # mcp.server.auth.provider.TokenVerifier is not @runtime_checkable
    # (verified against the installed mcp==2.1.1: isinstance() raises
    # TypeError for it), so structural conformance cannot be asserted with
    # isinstance() here. This assignment is the substitute check: pyright
    # (strict mode, see `uv run pyright`) rejects it if
    # StaticTableTokenVerifier does not structurally satisfy TokenVerifier's
    # `async def verify_token(self, token: str) -> AccessToken | None`.
    verifier: TokenVerifier = StaticTableTokenVerifier(CLIENT_TOKENS)

    assert verifier is not None


async def test_returned_access_token_is_a_valid_sdk_model() -> None:
    verifier = StaticTableTokenVerifier(CLIENT_TOKENS)

    result = await verifier.verify_token(READER_TOKEN)

    assert isinstance(result, AccessToken)


def test_access_token_model_rejects_missing_required_fields() -> None:
    # CTR-002 / SDK model contract: `token`, `client_id`, and `scopes` are
    # required on AccessToken (verified against the installed mcp==2.1.1
    # model_fields) — pydantic must refuse a construction missing them.
    with pytest.raises(ValidationError):
        AccessToken(client_id="c1", scopes=["devoks:read"])  # type: ignore[call-arg]


def test_get_role_returns_none_when_claims_absent() -> None:
    token = AccessToken(token="t", client_id="c1", scopes=["devoks:read"])

    assert get_role(token) is None
