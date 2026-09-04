"""Static token table bearer verification (DSN-001, CTR-002, AC-002-1, AC-002-6).

``StaticTableTokenVerifier`` is the *only* place in this codebase that knows
the Stage 1 verification mechanism is a static table sourced from
``MCP_CLIENT_TOKENS``. Every other module — the tool guard (TASK-007), the
server wiring (TASK-008) — consumes only the SDK's ``TokenVerifier`` protocol
and ``AccessToken`` model. When Stage 2 replaces this with an IdP
introspection call (FRD §10), the class satisfies the same protocol, so the
change is confined to this file: a new class is written here (or this one is
edited in place) and the constructor call at the composition root
(``app.py``/``server.py``) is updated to use it — nothing that consumes
``AccessToken`` changes. The class name says "static table" on purpose, so
nobody mistakes it for the general-purpose verifier once a second
implementation exists alongside it.

Where ``role`` rides
---------------------
``CTR-002`` rows carry a ``role``, and ``auth.policy.authorize`` needs that
role at authorization time. The SDK's ``AccessToken`` (verified empirically
against the installed ``mcp==2.1.1`` package — see the module docstring
below for the exact field set) has no ``role`` field of its own; it is an
OAuth-shaped model (``token``, ``client_id``, ``scopes``, ``expires_at``,
``resource``, ``subject``) plus one open extension point: ``claims: dict[str,
Any] | None``, documented by the SDK as "additional claims (e.g. `iss`,
`act`)".

This module stores the resolved role at ``claims[_ROLE_CLAIM_KEY]`` on the
``AccessToken`` it returns, rather than subclassing ``AccessToken``, because:

- the SDK middleware (``mcp.server.auth.middleware.bearer_auth
  .BearerAuthBackend.authenticate``) does ``AuthenticatedUser(auth_info)``
  with the exact object ``verify_token`` returned, and ``get_access_token()``
  (``mcp.server.auth.middleware.auth_context``) later returns that same
  ``auth_info`` unchanged — confirmed by reading both call sites in the
  installed package, not assumed from the docs. A plain ``AccessToken`` with
  populated ``claims`` round-trips through that path with zero risk of a
  subclass being silently narrowed back to the base class somewhere in the
  SDK's own (de)serialization.
- ``claims`` is the field the SDK itself names for exactly this purpose, so
  no extra type juggling is needed by callers that already type-check
  against ``AccessToken``.

``get_role`` below is the single accessor for that key — TASK-007's guard
reads the role through this function, never through a raw
``access_token.claims["role"]`` literal, so the storage key stays owned by
this module (mirrors ``DSN-001``'s "one file" intent at the field level, not
just the class level).

Timing side channel (design note, see task context)
-----------------------------------------------------
Token lookup compares the presented token against every row of the table
using ``secrets.compare_digest`` in a loop that runs to completion (never
breaks early on a match), rather than a single ``dict[token]`` lookup. A
dict lookup is O(1) but its timing depends on the token's hash bucket, not a
property this module wants to reason about under a security review; a full
scan with a constant-time compare per row removes that variable entirely.
The token table for this deployment is a handful of internal service
clients (Stage 1, FRD §6.4) — O(n) here is a handful of ``compare_digest``
calls per request, not a scaling concern.
"""

import secrets
from collections.abc import Mapping

from mcp.server.auth.provider import AccessToken, TokenVerifier

from devoks_mcp_management.config import ClientToken, Settings

#: The key this module owns inside ``AccessToken.claims`` for the CTR-002
#: role. Read only through ``get_role`` below — see module docstring.
_ROLE_CLAIM_KEY = "role"


class StaticTableTokenVerifier:
    """`TokenVerifier` backed by the CTR-002 static token table (DSN-001).

    Satisfies ``mcp.server.auth.provider.TokenVerifier`` structurally (that
    Protocol is not ``@runtime_checkable`` — verified against the installed
    SDK — so conformance is a static/pyright property here, not an
    ``isinstance`` check; see ``tests/test_verifier.py``).
    """

    def __init__(self, client_tokens: Mapping[str, ClientToken]) -> None:
        """Take the already-parsed-and-validated token table by injection.

        ``config.load_settings`` has already enforced table shape and
        role/tool consistency (AC-002-6 direction: tokens only ever come from
        ``Settings``, never a literal in this module or a module-global read
        of the environment).
        """
        self._client_tokens = client_tokens

    @classmethod
    def from_settings(cls, settings: Settings) -> StaticTableTokenVerifier:
        """Convenience constructor for the composition root (TASK-008)."""
        return cls(settings.client_tokens)

    async def verify_token(self, token: str) -> AccessToken | None:
        """Look up ``token`` in the table; ``None`` for anything unregistered.

        Never raises and never logs the token itself (AC-004-3 direction) —
        an unregistered token is an entirely ordinary outcome (a client
        typo, a revoked credential), not an error condition worth
        surfacing beyond the SDK's own 401 (AC-002-2, handled by the SDK,
        not here).
        """
        matched: ClientToken | None = None
        # Constant-total-time scan (see module docstring): every row is
        # compared, the loop never exits early on a hit.
        for candidate, entry in self._client_tokens.items():
            if secrets.compare_digest(candidate, token):
                matched = entry
        if matched is None:
            return None
        return AccessToken(
            token=token,
            client_id=matched.client_id,
            scopes=list(matched.scopes),
            claims={_ROLE_CLAIM_KEY: matched.role},
        )


def get_role(access_token: AccessToken) -> str | None:
    """Read back the CTR-002 role ``StaticTableTokenVerifier`` attached.

    Returns ``None`` if ``access_token.claims`` carries no role — e.g. an
    ``AccessToken`` built by a different verifier — so a caller that forgets
    to check gets a clean "no role" rather than a ``KeyError``/``TypeError``.
    """
    if access_token.claims is None:
        return None
    role = access_token.claims.get(_ROLE_CLAIM_KEY)
    return role if isinstance(role, str) else None


#: Static (pyright, strict mode) proof that ``StaticTableTokenVerifier``
#: satisfies ``TokenVerifier`` structurally. ``TokenVerifier`` is not
#: ``@runtime_checkable`` (verified against the installed ``mcp==2.1.1``: an
#: ``isinstance`` check against it raises ``TypeError``), so this assignment
#: — checked on every ``pyright`` run, not executed for any behavior of its
#: own — is the conformance guarantee in place of an ``isinstance`` check.
_conforms_to_token_verifier: TokenVerifier = StaticTableTokenVerifier({})
