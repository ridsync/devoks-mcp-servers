"""Tests for devoks_mcp_management.adapters.knowledge.github.credentials (TASK-020).

Traces: AC-006-1, AC-006-2, AC-006-3, AC-006-5, CTR-009, EDGE-007, RES-API-001,
DSN-004.

No real GitHub credentials or network calls — a throwaway in-process RSA key
(same technique as ``tests/test_config.py``'s ``_generate_pem``) signs the App
JWT, and every GitHub response is served by ``httpx2.MockTransport`` (FRD §7 /
handover note: real credentials are a Stage 2 deployment-verification item).
"""

from __future__ import annotations

import asyncio
import gc
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from devoks_mcp_management.adapters.knowledge.github.credentials import (
    InstallationTokenError,
    InstallationTokenProvider,
)

APP_ID = "app-123456"
INSTALLATION_ID = "install-789012"
DEFAULT_LEEWAY = 300

Handler = Callable[[httpx2.Request], Awaitable[httpx2.Response]]


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
def keypair() -> tuple[str, str]:
    """``(private_pem, public_pem)`` for the module — signing + verifying the App JWT."""
    private_pem = _generate_pem()
    private_key = serialization.load_pem_private_key(private_pem.encode("utf-8"), password=None)
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem.decode("utf-8")


def _iso(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class _RecordingTransport:
    """``MockTransport``-backed fake GitHub, queuing canned responses and recording requests."""

    def __init__(self, responses: list[httpx2.Response] | None = None) -> None:
        self.requests: list[httpx2.Request] = []
        self._responses = list(responses) if responses is not None else []
        #: Set to add latency so concurrent callers actually overlap in-flight.
        self.delay_seconds = 0.0
        #: When set, `handle` awaits this before returning — gives EDGE-015
        #: cancellation tests deterministic control over "the shared refresh
        #: is still in flight" instead of relying on `sleep`-based timing.
        self.gate: asyncio.Event | None = None
        #: Set the instant a request is received, so a test can `await`
        #: until the shared refresh has actually started before cancelling.
        self.request_received = asyncio.Event()

    def queue(self, response: httpx2.Response) -> None:
        self._responses.append(response)

    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        self.request_received.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self._responses:
            return self._responses.pop(0)
        # Default: a fresh 1-hour token, mirroring GitHub's real TTL.
        return httpx2.Response(
            201, json={"token": "ghs_default", "expires_at": _iso(4_102_444_800.0)}
        )

    @property
    def call_count(self) -> int:
        return len(self.requests)


def _client(transport: _RecordingTransport) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(transport.handle))


def _provider(
    *,
    transport: _RecordingTransport,
    private_key: str,
    clock: Callable[[], float] | None = None,
    leeway: int = DEFAULT_LEEWAY,
) -> tuple[InstallationTokenProvider, httpx2.AsyncClient]:
    client = _client(transport)
    provider = InstallationTokenProvider(
        app_id=APP_ID,
        private_key=private_key,
        installation_id=INSTALLATION_ID,
        http_client=client,
        refresh_leeway_seconds=leeway,
        clock=clock if clock is not None else lambda: 1_700_000_000.0,
    )
    return provider, client


class _ClockBox:
    """A settable clock: ``box.value`` can be changed between calls without
    caring how many times the provider internally reads the clock per
    ``get_token()`` (that count is an implementation detail, not a contract)."""

    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


# --- AC-006-1 / §5.4 "캐시 없음" ---------------------------------------------


async def test_no_cached_token_issues_and_returns_token(keypair: tuple[str, str]) -> None:
    # AC-006-1, §5.4: cache empty -> issue -> cached.
    private_pem, _ = keypair
    now = 1_700_000_000.0
    transport = _RecordingTransport(
        [httpx2.Response(201, json={"token": "ghs_first", "expires_at": _iso(now + 3600)})]
    )
    provider, client = _provider(transport=transport, private_key=private_pem, clock=lambda: now)

    token = await provider.get_token()

    assert token == "ghs_first"
    assert transport.call_count == 1
    await client.aclose()


# --- AC-006-2: remaining > leeway -> reused, no reissue ----------------------


async def test_cached_token_with_remaining_above_leeway_is_reused(
    keypair: tuple[str, str],
) -> None:
    # AC-006-2: strictly greater than CTR-009's leeway must NOT reissue.
    private_pem, _ = keypair
    now = 1_700_000_000.0
    expires_at = now + DEFAULT_LEEWAY + 1  # remaining == leeway + 1 (just above boundary)
    transport = _RecordingTransport(
        [httpx2.Response(201, json={"token": "ghs_reused", "expires_at": _iso(expires_at)})]
    )
    provider, client = _provider(transport=transport, private_key=private_pem, clock=lambda: now)

    first = await provider.get_token()
    second = await provider.get_token()

    assert first == second == "ghs_reused"
    assert transport.call_count == 1  # no reissue
    await client.aclose()


# --- AC-006-3 / CTR-009: remaining <= leeway -> reissued ---------------------


async def test_cached_token_with_remaining_below_leeway_is_reissued(
    keypair: tuple[str, str],
) -> None:
    # AC-006-3, CTR-009: strictly below the leeway must reissue.
    private_pem, _ = keypair
    now = 1_700_000_000.0
    expires_at = now + DEFAULT_LEEWAY - 1  # remaining == leeway - 1 (below boundary)
    transport = _RecordingTransport(
        [
            httpx2.Response(201, json={"token": "ghs_stale", "expires_at": _iso(expires_at)}),
            httpx2.Response(
                201, json={"token": "ghs_fresh", "expires_at": _iso(expires_at + 3600)}
            ),
        ]
    )
    provider, client = _provider(transport=transport, private_key=private_pem, clock=lambda: now)

    first = await provider.get_token()
    second = await provider.get_token()

    assert first == "ghs_stale"
    assert second == "ghs_fresh"
    assert transport.call_count == 2  # reissued
    await client.aclose()


# --- boundary: remaining == leeway exactly -----------------------------------


async def test_remaining_exactly_at_leeway_reissues_not_reuses(keypair: tuple[str, str]) -> None:
    # AC-006-3 says "이하" (<=) reissues — the boundary value itself must
    # reissue, not reuse. Regression guard against an off-by-one toward ">=".
    private_pem, _ = keypair
    now = 1_700_000_000.0
    expires_at_exact_leeway = now + DEFAULT_LEEWAY  # remaining == leeway exactly
    transport = _RecordingTransport(
        [
            httpx2.Response(
                201, json={"token": "ghs_a", "expires_at": _iso(expires_at_exact_leeway)}
            ),
            httpx2.Response(201, json={"token": "ghs_b", "expires_at": _iso(now + 3600)}),
        ]
    )
    provider, client = _provider(transport=transport, private_key=private_pem, clock=lambda: now)

    first = await provider.get_token()
    second = await provider.get_token()

    assert first == "ghs_a"
    assert second == "ghs_b"
    assert transport.call_count == 2
    await client.aclose()


async def test_remaining_one_second_above_leeway_is_reused(keypair: tuple[str, str]) -> None:
    # Symmetric boundary check: leeway + 1 must NOT reissue.
    private_pem, _ = keypair
    now = 1_700_000_000.0
    expires_at = now + DEFAULT_LEEWAY + 1
    transport = _RecordingTransport(
        [httpx2.Response(201, json={"token": "ghs_a", "expires_at": _iso(expires_at)})]
    )
    provider, client = _provider(transport=transport, private_key=private_pem, clock=lambda: now)

    first = await provider.get_token()
    second = await provider.get_token()

    assert first == second == "ghs_a"
    assert transport.call_count == 1
    await client.aclose()


# --- AC-006-5, EDGE-007: concurrent refresh merges into exactly one call ----


async def test_concurrent_refresh_issues_token_once() -> None:  # AC-006-5, EDGE-007
    private_pem = _generate_pem()
    now = 1_700_000_000.0
    transport = _RecordingTransport(
        [httpx2.Response(201, json={"token": "ghs_shared", "expires_at": _iso(now + 3600)})]
    )
    transport.delay_seconds = 0.05  # force overlap: every caller is in-flight together
    provider, client = _provider(transport=transport, private_key=private_pem, clock=lambda: now)

    results = await asyncio.gather(*[provider.get_token() for _ in range(10)])

    assert transport.call_count == 1
    assert results == ["ghs_shared"] * 10
    await client.aclose()


async def test_concurrent_refresh_while_cache_valid_never_calls_github() -> None:
    # A concurrency edge that is easy to get backwards: once a valid token is
    # cached, a stampede of callers must not touch GitHub at all.
    private_pem = _generate_pem()
    now = 1_700_000_000.0
    transport = _RecordingTransport(
        [httpx2.Response(201, json={"token": "ghs_cached", "expires_at": _iso(now + 3600)})]
    )
    provider, client = _provider(transport=transport, private_key=private_pem, clock=lambda: now)
    await provider.get_token()
    assert transport.call_count == 1

    results = await asyncio.gather(*[provider.get_token() for _ in range(10)])

    assert transport.call_count == 1
    assert results == ["ghs_cached"] * 10
    await client.aclose()


# --- §5.4 "발급 실패 -> 캐시 없음": failure does not permanently wedge ------


async def test_refresh_failure_does_not_block_next_attempt(keypair: tuple[str, str]) -> None:
    private_pem, _ = keypair
    now = 1_700_000_000.0
    transport = _RecordingTransport(
        [
            httpx2.Response(401, json={"message": "Bad credentials"}),
            httpx2.Response(201, json={"token": "ghs_recovered", "expires_at": _iso(now + 3600)}),
        ]
    )
    provider, client = _provider(transport=transport, private_key=private_pem, clock=lambda: now)

    with pytest.raises(InstallationTokenError):
        await provider.get_token()

    # Not permanently wedged: the next attempt retries and can succeed.
    token = await provider.get_token()
    assert token == "ghs_recovered"
    assert transport.call_count == 2
    await client.aclose()


async def test_concurrent_callers_during_a_failed_refresh_all_see_the_error(
    keypair: tuple[str, str],
) -> None:
    # None of the waiters are left hanging or given a stale/empty token when
    # the single in-flight refresh they were merged into fails.
    private_pem, _ = keypair
    now = 1_700_000_000.0
    transport = _RecordingTransport([httpx2.Response(500, text="internal error")])
    transport.delay_seconds = 0.05
    provider, client = _provider(transport=transport, private_key=private_pem, clock=lambda: now)

    results = await asyncio.gather(
        *[provider.get_token() for _ in range(5)], return_exceptions=True
    )

    assert transport.call_count == 1
    assert all(isinstance(result, InstallationTokenError) for result in results)
    await client.aclose()


# --- EDGE-015 / AC-006-5: a merged waiter's own cancellation must not -------
# --- propagate into the shared in-flight refresh -----------------------------


async def test_cancelling_one_merged_waiter_does_not_cancel_shared_refresh(
    keypair: tuple[str, str],
) -> None:
    # EDGE-015, AC-006-5: cancelling one caller merged into the shared
    # in-flight refresh must not cancel that shared refresh — every other
    # merged caller must still receive the same token from exactly one
    # GitHub call. This is the core regression guard for TASK-042: before
    # the `asyncio.shield` fix, cancelling a single waiter tore down the
    # shared task and every other waiter also raised `CancelledError`.
    private_pem, _ = keypair
    now = 1_700_000_000.0
    transport = _RecordingTransport(
        [httpx2.Response(201, json={"token": "ghs_shared", "expires_at": _iso(now + 3600)})]
    )
    # Gate (not `sleep`) makes "the shared refresh is still in flight when
    # we cancel" deterministic rather than timing-dependent.
    transport.gate = asyncio.Event()
    provider, client = _provider(transport=transport, private_key=private_pem, clock=lambda: now)

    waiters = [asyncio.ensure_future(provider.get_token()) for _ in range(5)]
    await asyncio.wait_for(transport.request_received.wait(), timeout=1.0)

    victim = waiters.pop(2)
    victim.cancel()
    with pytest.raises(asyncio.CancelledError):
        await victim

    transport.gate.set()
    results = await asyncio.gather(*waiters)

    assert results == ["ghs_shared"] * 4
    assert transport.call_count == 1  # no duplicate issuance from the cancellation
    await client.aclose()


async def test_all_waiters_cancelled_orphaned_success_still_populates_cache(
    keypair: tuple[str, str],
) -> None:
    # EDGE-015: even if *every* merged waiter is cancelled, the shared
    # refresh must not be aborted — it runs to completion as an orphan, and
    # its result must still land in the cache so the next caller reuses it
    # instead of triggering a second GitHub call.
    private_pem, _ = keypair
    now = 1_700_000_000.0
    transport = _RecordingTransport(
        [httpx2.Response(201, json={"token": "ghs_orphan", "expires_at": _iso(now + 3600)})]
    )
    transport.gate = asyncio.Event()
    provider, client = _provider(transport=transport, private_key=private_pem, clock=lambda: now)

    waiters = [asyncio.ensure_future(provider.get_token()) for _ in range(5)]
    await asyncio.wait_for(transport.request_received.wait(), timeout=1.0)

    for waiter in waiters:
        waiter.cancel()
    results = await asyncio.gather(*waiters, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)

    transport.gate.set()
    token = await provider.get_token()

    assert token == "ghs_orphan"
    assert transport.call_count == 1  # the orphan's result was cached, not lost
    await client.aclose()


async def test_all_waiters_cancelled_before_failure_no_leak_and_next_call_recovers(
    keypair: tuple[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    # EDGE-015: when every merged waiter is cancelled before the shared
    # refresh settles and it then fails, (1) nobody is left to observe the
    # failure, so the exception must still be retrieved -- it must not log
    # asyncio's classic "Task exception was never retrieved" warning when
    # garbage collected (on 3.14, `asyncio.shield` itself also emits its own
    # distinct, informational "... exception in shielded future" error via
    # the loop's exception handler once every waiter has detached from the
    # shared task; that one is expected/benign and is not what this asserts
    # against), and (2) the slot must not be left permanently wedged behind
    # that failure — the next call must be able to retry and succeed (§5.4
    # "발급 실패 -> 캐시 없음").
    private_pem, _ = keypair
    now = 1_700_000_000.0
    transport = _RecordingTransport(
        [
            httpx2.Response(500, text="internal error"),
            httpx2.Response(201, json={"token": "ghs_recovered", "expires_at": _iso(now + 3600)}),
        ]
    )
    transport.gate = asyncio.Event()
    provider, client = _provider(transport=transport, private_key=private_pem, clock=lambda: now)
    caplog.set_level(logging.ERROR, logger="asyncio")

    tasks_before = set(asyncio.all_tasks())
    waiters = [asyncio.ensure_future(provider.get_token()) for _ in range(5)]
    await asyncio.wait_for(transport.request_received.wait(), timeout=1.0)
    # The shared refresh is a real `asyncio.Task` too (created internally by
    # the provider) — find it by set difference so we can deterministically
    # wait for *its own* completion below without touching the provider's
    # private attributes.
    new_tasks = set(asyncio.all_tasks()) - tasks_before - set(waiters)
    assert len(new_tasks) == 1, "expected exactly the one shared refresh task"
    orphan = new_tasks.pop()
    del new_tasks, tasks_before

    for waiter in waiters:
        waiter.cancel()
    cancel_results = await asyncio.gather(*waiters, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in cancel_results)
    waiters.clear()
    del cancel_results

    # Wait for the orphan's own completion via a plain side-channel callback
    # -- deliberately NOT by calling `.result()`/`.exception()` on it
    # ourselves, which would itself retrieve the exception and mask exactly
    # the defect this test guards against.
    settled = asyncio.Event()
    orphan.add_done_callback(lambda _: settled.set())
    transport.gate.set()
    await asyncio.wait_for(settled.wait(), timeout=1.0)
    del orphan  # drop the only remaining reference so gc can finalize it

    gc.collect()
    assert "never retrieved" not in caplog.text.lower()

    token = await provider.get_token()
    assert token == "ghs_recovered"
    assert transport.call_count == 2  # retried, not permanently wedged
    await client.aclose()


# --- 4xx/5xx -> clear exception, no secret leakage ---------------------------


async def test_4xx_response_raises_error_without_leaking_secrets(keypair: tuple[str, str]) -> None:
    # AC-004-3 direction: message names the failure, never the PEM/JWT/token.
    private_pem, _ = keypair
    transport = _RecordingTransport([httpx2.Response(401, json={"message": "Bad credentials"})])
    provider, client = _provider(transport=transport, private_key=private_pem)

    with pytest.raises(InstallationTokenError) as excinfo:
        await provider.get_token()

    message = str(excinfo.value)
    assert "401" in message
    sent_jwt = transport.requests[0].headers["authorization"].removeprefix("Bearer ")
    assert sent_jwt not in message
    assert private_pem not in message
    assert "PRIVATE KEY" not in message
    await client.aclose()


async def test_5xx_response_raises_error_without_leaking_secrets(keypair: tuple[str, str]) -> None:
    private_pem, _ = keypair
    transport = _RecordingTransport([httpx2.Response(503, text="service unavailable")])
    provider, client = _provider(transport=transport, private_key=private_pem)

    with pytest.raises(InstallationTokenError) as excinfo:
        await provider.get_token()

    message = str(excinfo.value)
    assert "503" in message
    assert private_pem not in message
    assert "PRIVATE KEY" not in message
    await client.aclose()


async def test_malformed_success_body_raises_error(keypair: tuple[str, str]) -> None:
    # 2xx but missing the fields this module depends on — must not crash with
    # a raw KeyError/TypeError, and must not cache a token.
    private_pem, _ = keypair
    transport = _RecordingTransport([httpx2.Response(201, json={"unexpected": "shape"})])
    provider, client = _provider(transport=transport, private_key=private_pem)

    with pytest.raises(InstallationTokenError, match="token"):
        await provider.get_token()
    await client.aclose()


# --- App JWT: real signature, expected claims --------------------------------


async def test_signed_app_jwt_is_verifiable_with_expected_claims(
    keypair: tuple[str, str],
) -> None:
    private_pem, public_pem = keypair
    now = 1_700_000_000.0
    transport = _RecordingTransport(
        [httpx2.Response(201, json={"token": "ghs_x", "expires_at": _iso(now + 3600)})]
    )
    provider, client = _provider(transport=transport, private_key=private_pem, clock=lambda: now)

    await provider.get_token()

    sent_jwt = transport.requests[0].headers["authorization"].removeprefix("Bearer ")
    # `verify_exp` off deliberately: the injected `now` (2023) is in the past
    # relative to the real wall clock the test runs on, so pyjwt's own
    # real-time exp check would fail here for a reason unrelated to what
    # this test actually verifies (signature validity + claim values).
    claims = jwt.decode(sent_jwt, public_pem, algorithms=["RS256"], options={"verify_exp": False})
    assert claims["iss"] == APP_ID
    assert claims["iat"] == int(now) - 60
    # `exp` is measured from `iat`, so the validity window is exactly the
    # 600s GitHub documents as the maximum, and `exp` sits 540s from `now`
    # rather than exactly on GitHub's "no more than 10 minutes" edge.
    assert claims["exp"] == int(now) - 60 + 600
    assert claims["exp"] - claims["iat"] == 600, "window must not exceed GitHub's 10-minute maximum"
    assert claims["exp"] - int(now) < 600, "exp must sit inside the bound, not exactly on it"
    await client.aclose()


async def test_request_headers_match_github_app_auth_contract(keypair: tuple[str, str]) -> None:
    # RES-API-001: Accept + API version header, JWT (not an installation
    # token) as the bearer credential, correct installation-scoped path.
    private_pem, _ = keypair
    transport = _RecordingTransport(
        [httpx2.Response(201, json={"token": "ghs_x", "expires_at": _iso(1_700_003_600.0)})]
    )
    provider, client = _provider(transport=transport, private_key=private_pem)

    await provider.get_token()

    request = transport.requests[0]
    assert request.headers["accept"] == "application/vnd.github+json"
    assert request.headers["x-github-api-version"] == "2022-11-28"
    assert str(request.url) == (
        f"https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens"
    )
    await client.aclose()


# --- clock injection actually drives the expiry decision (determinism) ------


async def test_expiry_check_uses_injected_clock_not_wall_clock(keypair: tuple[str, str]) -> None:
    # A clock parked far in the future must force a reissue on the very next
    # call even though GitHub's own expires_at is "far away" by any real
    # wall-clock measure — if this module secretly read time.time() instead
    # of the injected clock, the second call below would incorrectly reuse.
    private_pem, _ = keypair
    far_future_now = 9_999_999_999.0  # year ~2286
    transport = _RecordingTransport(
        [
            httpx2.Response(
                201, json={"token": "ghs_a", "expires_at": _iso(4_102_444_800.0)}
            ),  # year 2100 by wall-clock terms, but far in the *past* of far_future_now
            httpx2.Response(201, json={"token": "ghs_b", "expires_at": _iso(4_102_444_800.0)}),
        ]
    )
    provider, client = _provider(
        transport=transport, private_key=private_pem, clock=lambda: far_future_now
    )

    first = await provider.get_token()
    second = await provider.get_token()

    assert first == "ghs_a"
    assert second == "ghs_b"
    assert transport.call_count == 2  # reissued: injected clock, not real time, drove this
    await client.aclose()


async def test_clock_value_changes_between_calls_are_honored(keypair: tuple[str, str]) -> None:
    # Complementary determinism check using a settable clock: each
    # `get_token()` call must consult the clock *at call time*, not a value
    # captured once at construction time.
    private_pem, _ = keypair
    t0 = 1_700_000_000.0
    clock = _ClockBox(t0)
    transport = _RecordingTransport(
        [
            httpx2.Response(
                201, json={"token": "ghs_a", "expires_at": _iso(t0 + DEFAULT_LEEWAY + 5)}
            ),
            httpx2.Response(201, json={"token": "ghs_b", "expires_at": _iso(t0 + 10_000)}),
        ]
    )
    provider, client = _provider(transport=transport, private_key=private_pem, clock=clock)

    first = await provider.get_token()  # now=t0; issues, expires in leeway+5s
    clock.value = t0 + DEFAULT_LEEWAY + 10  # advance past the leeway boundary
    second = await provider.get_token()  # already past leeway -> reissue
    third = await provider.get_token()  # unchanged clock, fresh cache -> reused

    assert first == "ghs_a"
    assert second == third == "ghs_b"
    assert transport.call_count == 2
    await client.aclose()


# --- explicit close/aclose ----------------------------------------------------


async def test_aclose_clears_cache_but_never_closes_injected_http_client(
    keypair: tuple[str, str],
) -> None:
    private_pem, _ = keypair
    now = 1_700_000_000.0
    transport = _RecordingTransport(
        [
            httpx2.Response(201, json={"token": "ghs_a", "expires_at": _iso(now + 3600)}),
            httpx2.Response(201, json={"token": "ghs_b", "expires_at": _iso(now + 3600)}),
        ]
    )
    provider, client = _provider(transport=transport, private_key=private_pem, clock=lambda: now)
    await provider.get_token()
    assert transport.call_count == 1

    await provider.aclose()

    # Ownership: the client belongs to whoever injected it (the lifespan),
    # not to this provider.
    assert client.is_closed is False

    token_after_close = await provider.get_token()
    assert token_after_close == "ghs_b"
    assert transport.call_count == 2  # cache was actually cleared, not left stale

    await client.aclose()
    assert client.is_closed is True
