"""GitHub App installation access token provider (TASK-020, DSN-004, REQ-006).

Owns the **one** piece of mutable process state this server carries (FRD §4.2
"상태 소유 결정"): a cached installation access token and its absolute
expiry. Everything else in this codebase — tools, policy, audit — is
stateless by design; this module is the deliberate, isolated exception.

Two-step credential flow (`RES-API-001`, GitHub's documented App
authentication flow)
---------------------------------------------------------------------------
1. **App JWT** — sign a short-lived JWT with the App's RSA private key.
   Claims per GitHub's documented claim table (`iss`/`iat`/`exp`; no `aud`):
   ``iss`` = the App ID (GitHub accepts the client ID or the App ID and
   recommends the client ID; this server passes through whatever
   ``GITHUB_APP_ID`` holds, so either works), ``iat`` = now − 60s
   (clock-drift slack the docs ask for), ``exp`` = ``iat`` + 600s — see
   `_build_app_jwt` for why `exp` is measured from `iat` rather than from
   `now`. Algorithm is RS256 — GitHub App keys are always RSA.
2. **Installation token exchange** — ``POST
   /app/installations/{installation_id}/access_tokens`` authenticated with
   that JWT as a Bearer token (**not** an installation token — this is the
   one call in the whole adapter that uses the App-level credential
   directly), headers ``Accept: application/vnd.github+json`` and
   ``X-GitHub-Api-Version: 2022-11-28`` (GitHub's current REST API version
   header). The response body's ``token`` (an installation access token,
   ``ghs_...``) and ``expires_at`` (ISO 8601, 1 hour out) are what this
   module caches.

Usage from `TASK-021` (the REST client)
---------------------------------------------------------------------------
::

    token = await provider.get_token()
    response = await http_client.get(
        "https://api.github.com/...",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )

Call ``get_token()`` once per outbound GitHub REST call (it is cheap on the
cache-hit path — no lock contention, see below) rather than caching the
return value across calls; that is what lets a mid-flight expiry never leak
into a request.

Caching, refresh boundary, and the `AC-006-5` / `EDGE-007` merge
---------------------------------------------------------------------------
`get_token()` reuses the cached token while its remaining lifetime is
**strictly greater than** `CTR-009`'s leeway (`AC-006-2`); at exactly the
leeway or below, it reissues (`AC-006-3` — "이하" is inclusive). This module
never re-derives that boundary itself: `config.load_settings` already
range-checks the leeway value (`TOKEN_REFRESH_LEEWAY_SECONDS_{MIN,MAX}`);
this module only ever compares against whatever `Settings` handed it.

Concurrent callers that all observe an invalid cache are merged into exactly
one outbound token request (`AC-006-5`, `EDGE-007`) via double-checked
locking around a single `asyncio.Lock`, combined with a **shared in-flight
`asyncio.Task`**:

1. Check the cache *before* taking the lock (cheap, no `await`, safe under
   asyncio's cooperative scheduling since nothing here suspends between the
   two attribute reads).
2. If invalid, acquire the lock, then check again — a coroutine that was
   queued on the lock while another was already mid-refresh must reuse that
   refresh's result, not start a second one.
3. Only the coroutine that finds the cache *and* the in-flight slot both
   empty, after acquiring the lock, creates the refresh task
   (`asyncio.ensure_future(self._issue_installation_token())`), stores it in
   `self._inflight_refresh`, and attaches `_on_inflight_refresh_done` to it
   as a done-callback (point 6 below) — all still under the lock, before any
   other caller can observe the task. Every other concurrent caller —
   whether it raced for the lock initially or was queued behind it — finds
   that task already there and awaits it (point 5).
4. `asyncio.Task` natively supports multiple concurrent awaiters, each
   getting the same outcome — so this merges not only the success path but
   also a *failure*: if the single outbound call fails, every caller that
   was merged into it raises the same `InstallationTokenError`, none is left
   silently waiting forever or handed a stale/empty token.
5. **`EDGE-015`** — every waiter awaits the shared task through
   `asyncio.shield(inflight)`, never a bare `await inflight`. asyncio's
   documented cancellation behavior is that cancelling a task that is
   currently suspended on another Future/Task cancels that Future/Task too
   (`Task.cancel()` forwards to whatever it is awaiting). Without the
   shield, one caller being cancelled — a client disconnect, an ALB idle
   timeout, a caller-side `asyncio.wait_for` — would cancel the *shared*
   refresh and, with it, every other caller merged into it, even though
   they have nothing to do with the cancelled request (independently
   reproduced with a standalone asyncio script during code review).
   `asyncio.shield` interposes an outer future: a waiter's own cancellation
   only reaches that outer future, never `inflight` itself, so the refresh
   keeps running and every *other* waiter's shield still resolves normally
   against it.
6. Slot cleanup and the cache write are owned by the task itself, via
   `_on_inflight_refresh_done` — a plain (synchronous) `add_done_callback`
   attached once, at creation, directly to `self._inflight_refresh` — never
   by whichever caller happens to still be awaiting when the task finishes.
   This is the part `shield` alone does not solve: if cleanup instead lived
   in a `finally` around each waiter's `await` (as it did before this
   callback existed), a *cancelled* waiter's `finally` still runs
   (cancellation propagates through `finally`) and could clear
   `self._inflight_refresh` while `inflight` is still running — the next
   `get_token()` call would then see an empty slot and start a **second**,
   duplicate refresh, exactly what `AC-006-5` forbids. A done-callback fires
   exactly once, only when the task actually completes, and — because it is
   registered on `inflight` before any waiter's `shield()` call registers
   its own forwarding callback on the same task, and asyncio runs a
   Future's done-callbacks in registration order — always runs before any
   waiter's `await asyncio.shield(inflight)` can resume. So the slot is
   cleared, and on success `self._cached_token`/`self._cached_expires_at`
   are written, strictly before any waiter observes completion; on failure
   nothing is written to the cache, exactly FRD §5.4's "발급 실패 → 캐시
   없음" transition. This also covers the case where *every* waiter is
   cancelled: `inflight` is never aborted, so it runs to completion as an
   orphan, and the callback still clears the slot and — on success —
   populates the cache for the next `get_token()` call to reuse instead of
   re-issuing. On failure it still calls `task.exception()` so a task that
   nobody was left to `await` does not trigger asyncio's "Task exception
   was never retrieved" warning when it is garbage collected.

Clock injection
---------------------------------------------------------------------------
Expiry is judged against GitHub's absolute `expires_at`, so this module
compares wall-clock time (`time.time`/epoch seconds), never a monotonic
clock — a monotonic clock has no relationship to an absolute timestamp
GitHub handed back. `clock: Clock = time.time` is injected so tests can pin
"now" deterministically; the same clock also stamps the JWT's `iat`/`exp`.

Ownership of the injected HTTP client
---------------------------------------------------------------------------
`http_client: httpx2.AsyncClient` is constructor-injected, never constructed
here (`DSN-004`: the ASGI lifespan, TASK-023, owns one client for the whole
process and shares it with this provider and TASK-021's REST client).
`aclose()` on this provider only clears its own cached token state — it
never closes the injected client. Closing the client is the lifespan's job.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any, Final, cast

import httpx2
import jwt

from devoks_mcp_management.config import Settings

__all__ = ["InstallationTokenError", "InstallationTokenProvider"]

#: Wall-clock epoch seconds (`time.time`-shaped), never monotonic — see the
#: module docstring's "Clock injection" section.
Clock = Callable[[], float]

#: GitHub's documented clock-drift slack for the App JWT's `iat`.
_APP_JWT_CLOCK_DRIFT_BACKDATE_SECONDS: Final = 60

#: GitHub's documented maximum App JWT lifetime.
_APP_JWT_TTL_SECONDS: Final = 600

_APP_JWT_ALGORITHM: Final = "RS256"

_GITHUB_API_BASE_URL: Final = "https://api.github.com"
_GITHUB_ACCEPT_HEADER: Final = "application/vnd.github+json"
_GITHUB_API_VERSION_HEADER: Final = "2022-11-28"

#: Truncation for any GitHub error body echoed into `InstallationTokenError`
#: — long enough to be useful, short enough that a pathological response
#: body cannot balloon a log line.
_ERROR_BODY_SNIPPET_LEN: Final = 200


class InstallationTokenError(Exception):
    """Raised when an installation access token could not be obtained.

    Never includes the App private key, the signed App JWT, or a previously
    issued installation token in its message (`AC-004-3` direction) — this
    can flow into a tool's error response or the audit log via `TASK-021`'s
    error normalization. Only status codes and GitHub's own (token-free)
    error response bodies are safe to include.
    """


class InstallationTokenProvider:
    """Caches, refreshes, and merges concurrent refreshes of one GitHub App installation token.

    Construct once (typically via `from_settings`) and share the instance —
    it is the single owner of the process's one piece of mutable state (see
    module docstring). Not a context manager: lifetime is lifespan-scoped
    (`DSN-004`), so `aclose()` is called explicitly, not via `async with`.
    """

    def __init__(
        self,
        *,
        app_id: str,
        private_key: str,
        installation_id: str,
        http_client: httpx2.AsyncClient,
        refresh_leeway_seconds: int,
        clock: Clock = time.time,
    ) -> None:
        self._app_id = app_id
        self._private_key = private_key
        self._installation_id = installation_id
        self._http_client = http_client
        self._refresh_leeway_seconds = refresh_leeway_seconds
        self._clock = clock

        self._lock = asyncio.Lock()
        self._cached_token: str | None = None
        #: Epoch seconds (same units as `clock()`), or `None` when there is
        #: no cached token.
        self._cached_expires_at: float | None = None
        #: The single in-flight refresh, shared by every concurrent caller
        #: merged into it (`AC-006-5`/`EDGE-007`) — see module docstring.
        #: `None` whenever no refresh is currently in progress.
        self._inflight_refresh: asyncio.Task[tuple[str, float]] | None = None

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        http_client: httpx2.AsyncClient,
        clock: Clock = time.time,
    ) -> InstallationTokenProvider:
        """Convenience constructor for the composition root (`TASK-023`'s lifespan)."""
        return cls(
            app_id=settings.github_app_id,
            private_key=settings.github_app_private_key,
            installation_id=settings.github_app_installation_id,
            http_client=http_client,
            refresh_leeway_seconds=settings.token_refresh_leeway_seconds,
            clock=clock,
        )

    async def get_token(self) -> str:
        """Return a valid installation access token, refreshing if needed (`AC-006-1`).

        See the module docstring for the caching boundary (`AC-006-2`/
        `AC-006-3`) and the concurrent-refresh merge (`AC-006-5`/`EDGE-007`/
        `EDGE-015`: double-checked locking + shared in-flight task, shielded
        against a waiter's own cancellation, cleaned up by the task itself).
        """
        if self._has_valid_cached_token():
            return cast(str, self._cached_token)

        async with self._lock:
            # Double-checked: a coroutine that queued on the lock while
            # another was already mid-refresh must reuse that refresh's
            # result rather than start a second one.
            if self._has_valid_cached_token():
                return cast(str, self._cached_token)
            if self._inflight_refresh is None:
                inflight = asyncio.ensure_future(self._issue_installation_token())
                # Attached here, before any waiter's `shield()` call below
                # can register its own forwarding callback on the same task
                # — this guarantees it is always the first done-callback to
                # run (`EDGE-015`; see module docstring point 6).
                inflight.add_done_callback(self._on_inflight_refresh_done)
                self._inflight_refresh = inflight
            inflight = self._inflight_refresh

        # `asyncio.shield`: this caller's own cancellation must reach only
        # the wrapper future `shield` returns, never `inflight` itself — see
        # `EDGE-015` in the module docstring.
        token, _expires_at = await asyncio.shield(inflight)
        return token

    def _on_inflight_refresh_done(self, task: asyncio.Task[tuple[str, float]]) -> None:
        """Slot cleanup + cache write, owned by the refresh task itself (`EDGE-015`).

        Runs as a plain, synchronous `add_done_callback` rather than in a
        `finally` on the awaiting side — see the module docstring for why
        that distinction is what keeps a cancelled waiter from tearing down
        a still-running refresh or wedging the slot open. No lock is needed
        here: this callback contains no `await`, so asyncio can never
        interleave it with another coroutine's execution — the slot check
        and the writes below are effectively atomic with respect to
        `get_token`.
        """
        if self._inflight_refresh is task:
            self._inflight_refresh = None
        if task.cancelled():
            return
        # Always retrieved, even when every waiter was cancelled before this
        # ran and nobody else ever calls `.result()`/`.exception()` on
        # `task` — otherwise asyncio logs "Task exception was never
        # retrieved" once `task` is garbage collected. (On 3.14,
        # `asyncio.shield` also independently guards this same all-cancelled
        # case with its own fallback callback, logged as a distinct
        # "... exception in shielded future" message — but retrieving it
        # explicitly here keeps this module's failure-handling
        # self-contained rather than depending on that stdlib internal.)
        error = task.exception()
        if error is not None:
            return
        token, expires_at = task.result()
        self._cached_token = token
        self._cached_expires_at = expires_at

    async def aclose(self) -> None:
        """Drop the cached token. Never closes the injected HTTP client (see module docstring)."""
        async with self._lock:
            self._cached_token = None
            self._cached_expires_at = None

    def _has_valid_cached_token(self) -> bool:
        """`AC-006-2`/`AC-006-3` boundary: remaining lifetime strictly greater than the leeway.

        Exactly at the leeway counts as "reissue" (`AC-006-3` says "이하"
        — inclusive of the boundary), so this returns `False` at that exact
        value, not `True`.
        """
        if self._cached_token is None or self._cached_expires_at is None:
            return False
        remaining = self._cached_expires_at - self._clock()
        return remaining > self._refresh_leeway_seconds

    def _build_app_jwt(self, now: float) -> str:
        """Sign the App JWT used to exchange for an installation token.

        Claim shape and TTL follow GitHub's own reference example for
        "Authenticating as a GitHub App" (see module docstring) — not
        guessed. Backdating `iat` and bounding `exp` to GitHub's documented
        10-minute maximum are both measured from the injected clock, so a
        test can assert the exact claim values deterministically.
        """
        issued_at = int(now) - _APP_JWT_CLOCK_DRIFT_BACKDATE_SECONDS
        # `exp` is measured from `iat`, not from `now`, so the JWT's validity
        # window is exactly `_APP_JWT_TTL_SECONDS` and `exp` lands comfortably
        # inside GitHub's "no more than 10 minutes into the future" bound.
        #
        # `int(now) + _APP_JWT_TTL_SECONDS` would satisfy that bound only at its
        # exact edge while stretching the window to 660s (backdate + TTL). Two
        # of GitHub's own reference examples disagree here — the Ruby one uses
        # `now + 600` with a backdated `iat`, the Python one uses `now + 600`
        # with `iat = now` — and only the second keeps the window at 600s. This
        # form satisfies both readings at once, which matters because the
        # failure mode is a rejected JWT that only shows up against live
        # GitHub: our clock running slightly ahead of theirs is enough.
        expires_at = issued_at + _APP_JWT_TTL_SECONDS
        payload = {"iat": issued_at, "exp": expires_at, "iss": self._app_id}
        return jwt.encode(payload, self._private_key, algorithm=_APP_JWT_ALGORITHM)

    async def _issue_installation_token(self) -> tuple[str, float]:
        """Call `RES-API-001` and return `(token, expires_at_epoch_seconds)`.

        Raises `InstallationTokenError` for every failure mode (network
        error, non-2xx status, unparsable body, missing/malformed fields) —
        never lets an `httpx2` exception or an unnormalized GitHub error
        escape this module.
        """
        app_jwt = self._build_app_jwt(self._clock())
        url = f"{_GITHUB_API_BASE_URL}/app/installations/{self._installation_id}/access_tokens"

        try:
            response = await self._http_client.post(
                url,
                headers={
                    "Authorization": f"Bearer {app_jwt}",
                    "Accept": _GITHUB_ACCEPT_HEADER,
                    "X-GitHub-Api-Version": _GITHUB_API_VERSION_HEADER,
                },
            )
        except httpx2.HTTPError as exc:
            # Deliberately not str(exc) verbatim: an httpx2 transport
            # exception's repr can include the request URL/method, which is
            # safe, but never the headers we just sent — this message is
            # hand-built to guarantee that regardless of what a future
            # httpx2 version puts in the exception's own string form.
            raise InstallationTokenError(
                f"GitHub installation token request failed before a response was received "
                f"({type(exc).__name__})"
            ) from exc

        if response.is_error:
            snippet = response.text[:_ERROR_BODY_SNIPPET_LEN]
            raise InstallationTokenError(
                f"GitHub installation token request failed: HTTP {response.status_code} {snippet!r}"
            )

        try:
            raw_payload = response.json()
        except ValueError as exc:
            raise InstallationTokenError(
                "GitHub installation token response was not valid JSON"
            ) from exc

        if not isinstance(raw_payload, dict):
            raise InstallationTokenError("GitHub installation token response was not a JSON object")
        # `response.json()` is typed `Any`; isinstance narrows `Any` to
        # `Unknown` under pyright strict rather than `dict[str, Any]` (same
        # workaround `config._load_json_object` uses), so the cast is what
        # actually recovers useful member typing for `.get(...)` below.
        payload = cast(dict[str, Any], raw_payload)

        token = payload.get("token")
        expires_at_raw = payload.get("expires_at")
        if not isinstance(token, str) or not token:
            raise InstallationTokenError(
                "GitHub installation token response is missing a 'token' string"
            )
        if not isinstance(expires_at_raw, str) or not expires_at_raw:
            raise InstallationTokenError(
                "GitHub installation token response is missing an 'expires_at' string"
            )

        try:
            expires_at = datetime.fromisoformat(expires_at_raw.replace("Z", "+00:00")).timestamp()
        except ValueError as exc:
            raise InstallationTokenError(
                "GitHub installation token response has an unparsable 'expires_at'"
            ) from exc

        return token, expires_at
