"""Tests for devoks_mcp_management.adapters.knowledge.github.client (TASK-021, TASK-025,
TASK-040, TASK-041).

Traces: AC-003-3, AC-005-4, AC-005-5, AC-005-6, AC-005-7, AC-005-8, AC-005-9,
CTR-004, CTR-005, CTR-008, EDGE-003, EDGE-004, EDGE-005, EDGE-006, EDGE-012,
EDGE-013, EDGE-014, RES-API-002, RES-API-003, RES-API-004.

No real GitHub network calls — every GitHub response is served by
``httpx2.MockTransport`` (same technique as ``tests/test_credentials.py``),
and the token provider is a minimal in-test stub satisfying `client.TokenProvider`
structurally (no dependency on the real `InstallationTokenProvider`).
"""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from devoks_mcp_management.adapters.knowledge.github.client import GitHubClient
from devoks_mcp_management.adapters.knowledge.github.credentials import InstallationTokenError
from devoks_mcp_management.types import SecurityBoundaryError

DEFAULT_MAX_BYTES = 262_144
DEFAULT_MAX_RESULTS = 30

Handler = Callable[[httpx2.Request], Awaitable[httpx2.Response]]


class _RecordingTransport:
    """``MockTransport``-backed fake GitHub, queuing canned responses and recording requests."""

    def __init__(self, responses: list[httpx2.Response] | None = None) -> None:
        self.requests: list[httpx2.Request] = []
        self._responses = list(responses) if responses is not None else []

    def queue(self, response: httpx2.Response) -> None:
        self._responses.append(response)

    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self._responses:
            return self._responses.pop(0)
        return httpx2.Response(200, json={})

    @property
    def call_count(self) -> int:
        return len(self.requests)


class _StubTokenProvider:
    """Minimal `client.TokenProvider` double: returns a fixed token, records calls."""

    def __init__(self, token: str = "ghs_test_token", *, fail: bool = False) -> None:
        self.token = token
        self.fail = fail
        self.call_count = 0

    async def get_token(self) -> str:
        self.call_count += 1
        if self.fail:
            raise InstallationTokenError("simulated credential failure")
        return self.token


def _http_client(transport: _RecordingTransport) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(transport.handle))


#: Default `repo_allowlist` for `_client()` -- covers the one repo almost
#: every test in this file exercises (`"acme/widgets"`, see `_repo_payload`'s
#: own default) so existing `search_code` assertions on returned items keep
#: working now that `search_code` re-filters its results against this
#: allowlist (`EDGE-014` layer 2, `TASK-041`). Tests exercising the filter
#: itself pass an explicit, different `repo_allowlist`.
DEFAULT_REPO_ALLOWLIST: frozenset[str] = frozenset({"acme/widgets"})


def _client(
    transport: _RecordingTransport,
    *,
    token_provider: _StubTokenProvider | None = None,
    read_file_max_bytes: int = DEFAULT_MAX_BYTES,
    search_code_max_results: int = DEFAULT_MAX_RESULTS,
    repo_allowlist: frozenset[str] = DEFAULT_REPO_ALLOWLIST,
    clock: Callable[[], float] | None = None,
) -> tuple[GitHubClient, httpx2.AsyncClient, _StubTokenProvider]:
    http_client = _http_client(transport)
    provider = token_provider if token_provider is not None else _StubTokenProvider()
    client = GitHubClient(
        http_client=http_client,
        token_provider=provider,
        read_file_max_bytes=read_file_max_bytes,
        search_code_max_results=search_code_max_results,
        repo_allowlist=repo_allowlist,
        clock=clock if clock is not None else lambda: 1_700_000_000.0,
    )
    return client, http_client, provider


def _repo_payload(full_name: str = "acme/widgets") -> dict[str, object]:
    owner, name = full_name.split("/", 1)
    return {
        "id": 1,
        "name": name,
        "full_name": full_name,
        "description": "A widget repo",
        "default_branch": "main",
        "owner": {"login": owner},
    }


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


# --- RES-API-002: list_installation_repositories -----------------------------


async def test_list_repos_single_page_success() -> None:  # AC-005-1, RES-API-002
    transport = _RecordingTransport(
        [
            httpx2.Response(
                200,
                json={
                    "total_count": 1,
                    "repository_selection": "selected",
                    "repositories": [_repo_payload("acme/widgets")],
                },
            )
        ]
    )
    client, http, provider = _client(transport)

    repos = await client.list_installation_repositories()

    assert len(repos) == 1
    assert repos[0].full_name == "acme/widgets"
    assert repos[0].name == "widgets"
    assert repos[0].description == "A widget repo"
    assert repos[0].default_branch == "main"
    assert provider.call_count == 1
    await http.aclose()


async def test_list_repos_paginates_across_multiple_pages() -> None:  # AC-005-1, RES-API-002
    page1 = httpx2.Response(
        200,
        headers={
            "Link": (
                '<https://api.github.com/installation/repositories?per_page=100&page=2>; rel="next"'
            )
        },
        json={
            "total_count": 2,
            "repository_selection": "selected",
            "repositories": [_repo_payload("acme/widgets")],
        },
    )
    page2 = httpx2.Response(
        200,
        json={
            "total_count": 2,
            "repository_selection": "selected",
            "repositories": [_repo_payload("acme/gadgets")],
        },
    )
    transport = _RecordingTransport([page1, page2])
    client, http, provider = _client(transport)

    repos = await client.list_installation_repositories()

    assert [r.full_name for r in repos] == ["acme/widgets", "acme/gadgets"]
    assert transport.call_count == 2
    assert provider.call_count == 2  # fresh token fetched per outbound call
    await http.aclose()


# --- RES-API-003: get_repo_tree ----------------------------------------------


async def test_get_repo_tree_directory_listing() -> None:  # AC-005-2, RES-API-003
    transport = _RecordingTransport(
        [
            httpx2.Response(
                200,
                json=[
                    {"name": "src", "path": "src", "type": "dir", "size": 0},
                    {"name": "README.md", "path": "README.md", "type": "file", "size": 42},
                ],
            )
        ]
    )
    client, http, _ = _client(transport)

    entries = await client.get_repo_tree("acme/widgets", path="")

    assert [(e.name, e.type, e.size) for e in entries] == [
        ("src", "dir", 0),
        ("README.md", "file", 42),
    ]
    await http.aclose()


async def test_get_repo_tree_on_a_file_path_wraps_single_object() -> None:  # AC-005-2
    transport = _RecordingTransport(
        [httpx2.Response(200, json={"name": "a.py", "path": "a.py", "type": "file", "size": 10})]
    )
    client, http, _ = _client(transport)

    entries = await client.get_repo_tree("acme/widgets", path="a.py")

    assert len(entries) == 1
    assert entries[0].name == "a.py"
    await http.aclose()


# --- RES-API-003: read_file success + CTR-004 truncation ---------------------


async def test_read_file_success_returns_full_content() -> None:  # AC-005-3
    text = "print('hello world')\n"
    transport = _RecordingTransport(
        [
            httpx2.Response(
                200,
                json={
                    "name": "a.py",
                    "path": "a.py",
                    "size": len(text.encode("utf-8")),
                    "encoding": "base64",
                    "content": _b64(text),
                },
            )
        ]
    )
    client, http, _ = _client(transport)

    result = await client.read_file("acme/widgets", "a.py")

    assert result.status == "complete"
    assert result.content == text
    assert result.returned_size == result.total_size
    assert result.message is None
    await http.aclose()


async def test_read_file_exactly_at_cap_is_not_truncated() -> None:  # CTR-004 boundary
    text = "a" * 100
    transport = _RecordingTransport(
        [
            httpx2.Response(
                200,
                json={
                    "name": "f.txt",
                    "path": "f.txt",
                    "size": len(text),
                    "encoding": "base64",
                    "content": _b64(text),
                },
            )
        ]
    )
    client, http, _ = _client(transport, read_file_max_bytes=100)

    result = await client.read_file("acme/widgets", "f.txt")

    assert result.status == "complete"
    assert result.content == text
    assert result.returned_size == 100
    await http.aclose()


async def test_read_file_one_byte_over_cap_is_truncated_with_total_size() -> None:
    # AC-005-4, EDGE-004: exactly cap+1 bytes -> truncated, total size reported.
    text = "a" * 101
    transport = _RecordingTransport(
        [
            httpx2.Response(
                200,
                json={
                    "name": "f.txt",
                    "path": "f.txt",
                    "size": len(text),
                    "encoding": "base64",
                    "content": _b64(text),
                },
            )
        ]
    )
    client, http, _ = _client(transport, read_file_max_bytes=100)

    result = await client.read_file("acme/widgets", "f.txt")

    assert result.status == "truncated"
    assert result.content == "a" * 100
    assert result.returned_size == 100
    assert result.total_size == 101
    assert result.message is not None
    assert "100" in result.message and "101" in result.message
    await http.aclose()


async def test_read_file_truncation_does_not_split_multibyte_utf8_character() -> None:
    # AC-005-4, EDGE-004: Korean + emoji content, byte cap lands mid-character.
    text = "한글 텍스트 파일입니다 🎉 more filler text so this is longer than the cap"
    encoded = text.encode("utf-8")
    # Choose a cap that lands inside a multi-byte character (Korean chars are
    # 3 bytes in UTF-8; cut 1 byte into the 2nd character).
    cap = len("한글 ".encode()) + 1
    transport = _RecordingTransport(
        [
            httpx2.Response(
                200,
                json={
                    "name": "f.txt",
                    "path": "f.txt",
                    "size": len(encoded),
                    "encoding": "base64",
                    "content": _b64(text),
                },
            )
        ]
    )
    client, http, _ = _client(transport, read_file_max_bytes=cap)

    result = await client.read_file("acme/widgets", "f.txt")

    assert result.status == "truncated"
    assert result.content is not None
    # Must decode cleanly (no exception raised above) and never exceed the cap.
    assert len(result.content.encode("utf-8")) <= cap
    assert result.content.encode("utf-8") == encoded[: len(result.content.encode("utf-8"))]
    await http.aclose()


async def test_read_file_emoji_content_truncates_without_mojibake() -> None:
    # Emoji are 4-byte UTF-8 sequences -- worst-case backoff distance.
    text = "🎉🎊🎈" * 20
    encoded = text.encode("utf-8")
    cap = 10  # guaranteed to land mid-emoji
    transport = _RecordingTransport(
        [
            httpx2.Response(
                200,
                json={
                    "name": "f.txt",
                    "path": "f.txt",
                    "size": len(encoded),
                    "encoding": "base64",
                    "content": _b64(text),
                },
            )
        ]
    )
    client, http, _ = _client(transport, read_file_max_bytes=cap)

    result = await client.read_file("acme/widgets", "f.txt")

    assert result.status == "truncated"
    assert result.content is not None
    # Every character in the truncated output must be a whole emoji from the
    # source (no replacement characters / partial glyphs).
    assert text.startswith(result.content)
    assert len(result.content.encode("utf-8")) <= cap
    await http.aclose()


# --- AC-005-5, EDGE-005: binary detection -------------------------------------


async def test_read_file_binary_content_returns_marker_and_size() -> None:
    raw = bytes([0xFF, 0xFE, 0x00, 0x01, 0x02, 0x80, 0x81])  # invalid UTF-8
    transport = _RecordingTransport(
        [
            httpx2.Response(
                200,
                json={
                    "name": "f.bin",
                    "path": "f.bin",
                    "size": len(raw),
                    "encoding": "base64",
                    "content": base64.b64encode(raw).decode("ascii"),
                },
            )
        ]
    )
    client, http, _ = _client(transport)

    result = await client.read_file("acme/widgets", "f.bin")

    assert result.status == "binary"
    assert result.content is None
    assert result.total_size == len(raw)
    assert result.message is not None and str(len(raw)) in result.message
    await http.aclose()


async def test_read_file_binary_takes_precedence_over_truncation() -> None:
    # A binary file larger than the cap must still report status="binary",
    # not "truncated" -- binary detection happens on the full content first.
    raw = bytes([0xFF, 0xFE]) * 200
    transport = _RecordingTransport(
        [
            httpx2.Response(
                200,
                json={
                    "name": "f.bin",
                    "path": "f.bin",
                    "size": len(raw),
                    "encoding": "base64",
                    "content": base64.b64encode(raw).decode("ascii"),
                },
            )
        ]
    )
    client, http, _ = _client(transport, read_file_max_bytes=50)

    result = await client.read_file("acme/widgets", "f.bin")

    assert result.status == "binary"
    assert result.content is None
    await http.aclose()


# --- TASK-025 / EDGE-012: encoding: "none" -> raw media-type fallback -------
#
# GitHub's contents endpoint omits `content` (`encoding: "none"`) once a file
# exceeds its inline-content limit. `_CONTENTS_RAW_FALLBACK_MAX_BYTES` (100 MB)
# is the module's own doc-derived ceiling for when a second, raw-media-type
# request can still recover the content vs. when GitHub does not support the
# endpoint at all.


def _metadata_none_response(*, size: int, name: str = "big.txt") -> httpx2.Response:
    return httpx2.Response(
        200,
        json={"name": name, "path": name, "size": size, "encoding": "none", "content": ""},
    )


async def test_read_file_encoding_none_within_100mb_falls_back_to_raw_and_truncates() -> None:
    # AC-005-4, EDGE-012: a 1-100 MB file must still come back truncated at
    # CTR-004's cap (not "unavailable") via one additional raw request.
    total_size = 2_000_000
    raw_text = "a" * total_size
    transport = _RecordingTransport(
        [
            _metadata_none_response(size=total_size),
            httpx2.Response(200, content=raw_text.encode("utf-8")),
        ]
    )
    client, http, _ = _client(transport)

    result = await client.read_file("acme/widgets", "big.txt")

    assert result.status == "truncated"
    assert result.content == "a" * DEFAULT_MAX_BYTES
    assert result.returned_size == DEFAULT_MAX_BYTES
    assert result.total_size == total_size
    assert result.message is not None
    assert str(DEFAULT_MAX_BYTES) in result.message
    assert str(total_size) in result.message
    assert transport.call_count == 2
    await http.aclose()


async def test_read_file_over_100mb_reports_unavailable_without_sending_raw_request() -> None:
    # EDGE-012: past GitHub's 100 MB ceiling the contents endpoint serves no
    # content through any media type, so this client must not even attempt
    # the raw request -- it would only fail and burn a rate-limit unit.
    total_size = 150_000_000
    transport = _RecordingTransport([_metadata_none_response(size=total_size, name="huge.bin")])
    client, http, _ = _client(transport)

    result = await client.read_file("acme/widgets", "huge.bin")

    assert result.status == "unavailable"
    assert result.content is None
    assert result.returned_size == 0
    assert result.total_size == total_size
    assert result.message is not None and str(total_size) in result.message
    assert transport.call_count == 1  # raw fallback request never sent
    await http.aclose()


async def test_read_file_raw_fallback_binary_content_returns_marker_and_size() -> None:
    # AC-005-5 reached through the EDGE-012 raw fallback, not the base64 path.
    raw = bytes([0xFF, 0xFE, 0x00, 0x01, 0x02, 0x80, 0x81]) * 100  # invalid UTF-8
    total_size = len(raw)
    transport = _RecordingTransport(
        [
            _metadata_none_response(size=total_size, name="f.bin"),
            httpx2.Response(200, content=raw),
        ]
    )
    client, http, _ = _client(transport)

    result = await client.read_file("acme/widgets", "f.bin")

    assert result.status == "binary"
    assert result.content is None
    assert result.total_size == total_size
    assert result.message is not None and str(total_size) in result.message
    await http.aclose()


async def test_read_file_raw_fallback_truncation_does_not_split_multibyte_utf8_character() -> None:
    # EDGE-012 + AC-005-4: the byte cap can still land mid-character when
    # content arrives via the raw fallback -- must use the same UTF-8-safe
    # backoff as the base64 path (shared `_classify_content`).
    text = "한글 텍스트 파일입니다 🎉 more filler text so this is longer than the cap"
    encoded = text.encode("utf-8")
    cap = len("한글 ".encode()) + 1
    transport = _RecordingTransport(
        [
            _metadata_none_response(size=len(encoded), name="f.txt"),
            httpx2.Response(200, content=encoded),
        ]
    )
    client, http, _ = _client(transport, read_file_max_bytes=cap)

    result = await client.read_file("acme/widgets", "f.txt")

    assert result.status == "truncated"
    assert result.content is not None
    assert len(result.content.encode("utf-8")) <= cap
    assert result.content.encode("utf-8") == encoded[: len(result.content.encode("utf-8"))]
    await http.aclose()


async def test_read_file_normal_path_still_issues_a_single_http_request() -> None:
    # Regression guard: EDGE-012's fallback must never add a second request
    # to the common <=1 MB path (that would double the cost of every read).
    text = "print('hello world')\n"
    transport = _RecordingTransport(
        [
            httpx2.Response(
                200,
                json={
                    "name": "a.py",
                    "path": "a.py",
                    "size": len(text.encode("utf-8")),
                    "encoding": "base64",
                    "content": _b64(text),
                },
            )
        ]
    )
    client, http, _ = _client(transport)

    result = await client.read_file("acme/widgets", "a.py")

    assert result.status == "complete"
    assert transport.call_count == 1
    await http.aclose()


async def test_read_file_raw_fallback_request_uses_raw_accept_header_and_auth() -> None:
    total_size = 2_000_000
    transport = _RecordingTransport(
        [
            _metadata_none_response(size=total_size),
            httpx2.Response(200, content=b"a" * total_size),
        ]
    )
    client, http, provider = _client(transport)

    await client.read_file("acme/widgets", "big.txt")

    raw_request = transport.requests[1]
    assert raw_request.headers["accept"] == "application/vnd.github.raw+json"
    assert raw_request.headers["authorization"] == f"Bearer {provider.token}"
    await http.aclose()


async def test_read_file_raw_fallback_fetches_a_fresh_token() -> None:
    total_size = 2_000_000
    transport = _RecordingTransport(
        [
            _metadata_none_response(size=total_size),
            httpx2.Response(200, content=b"a" * total_size),
        ]
    )
    client, http, provider = _client(transport)

    await client.read_file("acme/widgets", "big.txt")

    assert provider.call_count == 2  # metadata request + raw fallback request, never cached
    await http.aclose()


async def test_read_file_raw_fallback_4xx_is_normalized_without_traceback() -> None:
    total_size = 2_000_000
    transport = _RecordingTransport(
        [
            _metadata_none_response(size=total_size),
            httpx2.Response(422, text="Validation Failed"),
        ]
    )
    client, http, _ = _client(transport)

    with pytest.raises(ToolError) as excinfo:
        await client.read_file("acme/widgets", "big.txt")

    message = str(excinfo.value)
    assert "422" in message
    assert "Traceback" not in message
    assert 'File "' not in message
    assert transport.call_count == 2  # metadata request + attempted raw fallback
    await http.aclose()


async def test_get_repo_tree_directory_listing_distinguishes_from_read_file_status() -> None:
    # Handover requirement (4): truncated / binary / unavailable are all
    # distinct FileContent.status values, never conflated. The third case
    # uses a size past the EDGE-012 100 MB ceiling so it resolves to
    # "unavailable" from the metadata response alone (single mocked call).
    statuses: set[str] = set()

    for content_json in (
        {
            "name": "a",
            "path": "a",
            "size": 300,
            "encoding": "base64",
            "content": _b64("a" * 300),
        },
        {
            "name": "b",
            "path": "b",
            "size": len(bytes([0xFF, 0xFE])),
            "encoding": "base64",
            "content": base64.b64encode(bytes([0xFF, 0xFE])).decode("ascii"),
        },
        {"name": "c", "path": "c", "size": 150_000_000, "encoding": "none", "content": ""},
    ):
        transport = _RecordingTransport([httpx2.Response(200, json=content_json)])
        client, http, _ = _client(transport, read_file_max_bytes=100)
        result = await client.read_file("acme/widgets", "x")
        statuses.add(result.status)
        await http.aclose()

    assert statuses == {"truncated", "binary", "unavailable"}


# --- AC-005-8, EDGE-006: 404 -------------------------------------------------


async def test_read_file_not_found_names_what_was_missing() -> None:
    transport = _RecordingTransport([httpx2.Response(404, json={"message": "Not Found"})])
    client, http, _ = _client(transport)

    with pytest.raises(ToolError) as excinfo:
        await client.read_file("acme/widgets", "missing.py", ref="deadbeef")

    message = str(excinfo.value)
    assert "acme/widgets" in message
    assert "missing.py" in message
    assert "deadbeef" in message
    await http.aclose()


async def test_get_repo_tree_not_found() -> None:
    transport = _RecordingTransport([httpx2.Response(404, json={"message": "Not Found"})])
    client, http, _ = _client(transport)

    with pytest.raises(ToolError):
        await client.get_repo_tree("acme/widgets", path="nope")
    await http.aclose()


# --- AC-005-7: generic 4xx/5xx, no stack trace in message --------------------


async def test_read_file_generic_4xx_is_normalized_without_traceback() -> None:
    transport = _RecordingTransport([httpx2.Response(422, text="Validation Failed")])
    client, http, _ = _client(transport)

    with pytest.raises(ToolError) as excinfo:
        await client.read_file("acme/widgets", "a.py")

    message = str(excinfo.value)
    assert "422" in message
    assert "Traceback" not in message
    assert 'File "' not in message
    await http.aclose()


async def test_read_file_5xx_is_normalized() -> None:
    transport = _RecordingTransport([httpx2.Response(503, text="Service Unavailable")])
    client, http, _ = _client(transport)

    with pytest.raises(ToolError) as excinfo:
        await client.read_file("acme/widgets", "a.py")

    assert "503" in str(excinfo.value)
    await http.aclose()


# --- AC-005-9, EDGE-003: rate limiting ---------------------------------------


async def test_primary_rate_limit_403_includes_retry_time() -> None:
    reset_epoch = 1_700_003_600
    transport = _RecordingTransport(
        [
            httpx2.Response(
                403,
                headers={
                    "x-ratelimit-remaining": "0",
                    "x-ratelimit-reset": str(reset_epoch),
                    "x-ratelimit-resource": "core",
                },
                json={"message": "API rate limit exceeded"},
            )
        ]
    )
    client, http, _ = _client(transport)

    with pytest.raises(ToolError) as excinfo:
        await client.get_repo_tree("acme/widgets", path="")

    message = str(excinfo.value)
    expected_time = datetime.fromtimestamp(reset_epoch, tz=UTC).isoformat()
    assert expected_time in message
    assert "rate limit" in message.lower()
    await http.aclose()


async def test_429_with_retry_after_includes_retry_time() -> None:
    now = 1_700_000_000.0
    transport = _RecordingTransport(
        [httpx2.Response(429, headers={"retry-after": "30"}, json={"message": "too fast"})]
    )
    client, http, _ = _client(transport, clock=lambda: now)

    with pytest.raises(ToolError) as excinfo:
        await client.search_code("foo", "acme/widgets")

    message = str(excinfo.value)
    expected_time = datetime.fromtimestamp(now + 30, tz=UTC).isoformat()
    assert expected_time in message
    await http.aclose()


async def test_secondary_rate_limit_403_with_retry_after() -> None:
    now = 1_700_000_000.0
    transport = _RecordingTransport(
        [
            httpx2.Response(
                403,
                headers={"retry-after": "60"},
                json={"message": "You have exceeded a secondary rate limit"},
            )
        ]
    )
    client, http, _ = _client(transport, clock=lambda: now)

    with pytest.raises(ToolError) as excinfo:
        await client.read_file("acme/widgets", "a.py")

    expected_time = datetime.fromtimestamp(now + 60, tz=UTC).isoformat()
    assert expected_time in str(excinfo.value)
    await http.aclose()


async def test_ordinary_403_permission_denied_is_not_treated_as_rate_limit() -> None:
    # Distinguishing signal: no retry-after, no x-ratelimit-remaining=0.
    transport = _RecordingTransport(
        [
            httpx2.Response(
                403,
                headers={"x-ratelimit-remaining": "42"},
                json={"message": "Resource not accessible by integration"},
            )
        ]
    )
    client, http, _ = _client(transport)

    with pytest.raises(ToolError) as excinfo:
        await client.read_file("acme/widgets", "a.py")

    message = str(excinfo.value)
    assert "403" in message
    assert "rate limit" not in message.lower()
    await http.aclose()


async def test_429_without_any_rate_limit_headers_is_still_treated_as_rate_limited() -> None:
    transport = _RecordingTransport([httpx2.Response(429, json={"message": "slow down"})])
    client, http, _ = _client(transport)

    with pytest.raises(ToolError) as excinfo:
        await client.read_file("acme/widgets", "a.py")

    message = str(excinfo.value)
    assert "rate limit" in message.lower()
    assert "not provided" in message.lower()
    await http.aclose()


# --- InstallationTokenError normalization ------------------------------------


async def test_credential_failure_is_normalized_without_leaking_secrets() -> None:
    # AC-004-3 direction: InstallationTokenError message is safe to embed,
    # but this test still asserts no PEM/JWT-shaped content leaks through.
    transport = _RecordingTransport()
    provider = _StubTokenProvider(fail=True)
    client, http, _ = _client(transport, token_provider=provider)

    with pytest.raises(ToolError) as excinfo:
        await client.list_installation_repositories()

    message = str(excinfo.value)
    assert "simulated credential failure" in message
    assert "PRIVATE KEY" not in message
    assert transport.call_count == 0  # never reached GitHub at all
    await http.aclose()


# --- network timeout ----------------------------------------------------------


async def test_network_timeout_is_normalized() -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("simulated timeout", request=request)

    http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    client = GitHubClient(
        http_client=http_client,
        token_provider=_StubTokenProvider(),
        read_file_max_bytes=DEFAULT_MAX_BYTES,
        search_code_max_results=DEFAULT_MAX_RESULTS,
    )

    with pytest.raises(ToolError) as excinfo:
        await client.read_file("acme/widgets", "a.py")

    assert "ReadTimeout" in str(excinfo.value)
    await http_client.aclose()


async def test_network_connect_error_is_normalized() -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("simulated connect failure", request=request)

    http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    client = GitHubClient(
        http_client=http_client,
        token_provider=_StubTokenProvider(),
        read_file_max_bytes=DEFAULT_MAX_BYTES,
        search_code_max_results=DEFAULT_MAX_RESULTS,
    )

    with pytest.raises(ToolError) as excinfo:
        await client.list_installation_repositories()

    assert "ConnectError" in str(excinfo.value)
    await http_client.aclose()


# --- RES-API-004: search_code + CTR-005 cap -----------------------------------


async def test_search_code_success_with_excerpt() -> None:  # AC-005-6, RES-API-004
    transport = _RecordingTransport(
        [
            httpx2.Response(
                200,
                json={
                    "total_count": 1,
                    "incomplete_results": False,
                    "items": [
                        {
                            "path": "src/main.py",
                            "repository": {"full_name": "acme/widgets"},
                            "text_matches": [{"fragment": "def foo(): ..."}],
                        }
                    ],
                },
            )
        ]
    )
    client, http, _ = _client(transport)

    results = await client.search_code("foo", "acme/widgets")

    assert results.total_count == 1
    assert results.incomplete_results is False
    assert len(results.items) == 1
    assert results.items[0].path == "src/main.py"
    assert results.items[0].repository == "acme/widgets"
    assert results.items[0].excerpt == "def foo(): ..."

    request = transport.requests[0]
    assert request.headers["accept"] == "application/vnd.github.text-match+json"
    assert "repo%3Aacme%2Fwidgets" in str(request.url) or "repo:acme/widgets" in str(request.url)
    await http.aclose()


async def test_search_code_caps_results_at_ctr_005_limit() -> None:  # CTR-005
    many_items = [
        {"path": f"file{i}.py", "repository": {"full_name": "acme/widgets"}} for i in range(10)
    ]
    transport = _RecordingTransport(
        [
            httpx2.Response(
                200,
                json={"total_count": 10, "incomplete_results": False, "items": many_items},
            )
        ]
    )
    client, http, _ = _client(transport, search_code_max_results=3)

    results = await client.search_code("foo", "acme/widgets")

    assert len(results.items) == 3
    assert results.total_count == 10  # informational count is not clamped
    await http.aclose()


async def test_search_code_sends_per_page_matching_max_results() -> None:
    transport = _RecordingTransport(
        [httpx2.Response(200, json={"total_count": 0, "incomplete_results": False, "items": []})]
    )
    client, http, _ = _client(transport, search_code_max_results=17)

    await client.search_code("foo", "acme/widgets")

    request = transport.requests[0]
    assert "per_page=17" in str(request.url)
    await http.aclose()


# --- fresh token per call (no client-side caching) ----------------------------


async def test_get_token_called_once_per_outbound_request_read_file() -> None:
    def _response() -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "name": "a.py",
                "path": "a.py",
                "size": 5,
                "encoding": "base64",
                "content": _b64("hello"),
            },
        )

    transport = _RecordingTransport([_response(), _response()])
    client, http, provider = _client(transport)

    await client.read_file("acme/widgets", "a.py")
    await client.read_file("acme/widgets", "a.py")

    assert provider.call_count == 2  # never cached across calls by this client
    await http.aclose()


async def test_get_token_called_once_per_outbound_request_search() -> None:
    transport = _RecordingTransport(
        [
            httpx2.Response(200, json={"total_count": 0, "incomplete_results": False, "items": []})
            for _ in range(3)
        ]
    )
    client, http, provider = _client(transport)

    for _ in range(3):
        await client.search_code("foo", "acme/widgets")

    assert provider.call_count == 3
    await http.aclose()


# --- repeated / rapid-fire calls (repetition case per test-quality-bar §6) ---


async def test_repeated_read_file_calls_are_independent() -> None:
    text_a = "alpha content"
    text_b = "beta content, different length here"
    transport = _RecordingTransport(
        [
            httpx2.Response(
                200,
                json={
                    "name": "a.py",
                    "path": "a.py",
                    "size": len(text_a.encode("utf-8")),
                    "encoding": "base64",
                    "content": _b64(text_a),
                },
            ),
            httpx2.Response(
                200,
                json={
                    "name": "b.py",
                    "path": "b.py",
                    "size": len(text_b.encode("utf-8")),
                    "encoding": "base64",
                    "content": _b64(text_b),
                },
            ),
        ]
    )
    client, http, _ = _client(transport)

    first = await client.read_file("acme/widgets", "a.py")
    second = await client.read_file("acme/widgets", "b.py")

    assert first.content == text_a
    assert second.content == text_b
    assert transport.call_count == 2
    await http.aclose()


# --- request shape sanity (headers, ref propagation) --------------------------


async def test_read_file_request_includes_ref_and_standard_headers() -> None:
    transport = _RecordingTransport(
        [
            httpx2.Response(
                200,
                json={
                    "name": "a.py",
                    "path": "a.py",
                    "size": 5,
                    "encoding": "base64",
                    "content": _b64("hello"),
                },
            )
        ]
    )
    client, http, _ = _client(transport)

    await client.read_file("acme/widgets", "a.py", ref="feature-branch")

    request = transport.requests[0]
    assert request.headers["accept"] == "application/vnd.github+json"
    assert request.headers["x-github-api-version"] == "2022-11-28"
    assert "ref=feature-branch" in str(request.url)
    assert "/repos/acme/widgets/contents/a.py" in str(request.url)
    await http.aclose()


# --- EDGE-013 / AC-003-3 / CTR-008: path & ref traversal (TASK-040) ----------
#
# The two attack scenarios directly below are the exact reproduction that
# proved this vulnerability real (see client.py's module docstring): the
# allowlist gate (`tools/guard.py`) only ever checks the `repo` argument, so
# an unvalidated `path` could walk `../` segments clean out of the intended
# repository. `transport.call_count` -- not just "an exception was raised"
# -- is asserted to be 0 in every blocked case below, since the original
# bug's own audit trail showed `outcome=ok`; an exception alone would not
# have caught that class of failure.


# EDGE-013, AC-003-3, CTR-008
async def test_read_file_path_traversal_escapes_to_another_repo_is_blocked() -> None:
    transport = _RecordingTransport()
    client, http, _ = _client(transport)

    with pytest.raises(ToolError):
        await client.read_file("acme/widgets", "../../../victim-org/secret-repo/contents/.env")

    assert transport.call_count == 0
    await http.aclose()


# EDGE-013, AC-003-3, CTR-008
async def test_get_repo_tree_path_traversal_reaches_installation_endpoint_is_blocked() -> None:
    transport = _RecordingTransport()
    client, http, _ = _client(transport)

    with pytest.raises(ToolError):
        await client.get_repo_tree("acme/widgets", path="../../../../installation/repositories")

    assert transport.call_count == 0
    await http.aclose()


@pytest.mark.parametrize(
    "path",
    [
        "..",
        "../foo",
        "foo/..",
        "foo/../bar",
        "foo/bar/..",
        ".",
        "./foo",
        "foo/.",
        "foo/./bar",
    ],
    ids=[
        "bare-dotdot",
        "leading-dotdot",
        "trailing-dotdot",
        "middle-dotdot",
        "trailing-dotdot-nested",
        "bare-dot",
        "leading-dot",
        "trailing-dot",
        "middle-dot",
    ],
)
async def test_read_file_rejects_dot_segment_at_any_position(path: str) -> None:  # EDGE-013
    transport = _RecordingTransport()
    client, http, _ = _client(transport)

    with pytest.raises(ToolError):
        await client.read_file("acme/widgets", path)

    assert transport.call_count == 0
    await http.aclose()


async def test_get_repo_tree_rejects_dot_segment_too() -> None:  # EDGE-013
    # Both tools share `_contents_request_target` -- this proves
    # `get_repo_tree` gets the same protection, not just `read_file`.
    transport = _RecordingTransport()
    client, http, _ = _client(transport)

    with pytest.raises(ToolError):
        await client.get_repo_tree("acme/widgets", path="src/../../../etc")

    assert transport.call_count == 0
    await http.aclose()


@pytest.mark.parametrize("path", ["foo\\bar", "foo\\..\\bar", "..\\victim"])
async def test_read_file_rejects_backslash_in_path(path: str) -> None:  # EDGE-013
    transport = _RecordingTransport()
    client, http, _ = _client(transport)

    with pytest.raises(ToolError):
        await client.read_file("acme/widgets", path)

    assert transport.call_count == 0
    await http.aclose()


# EDGE-013
async def test_read_file_percent_encoded_dot_dot_does_not_escape_the_repo_scope() -> None:
    # A caller-supplied literal "%2e%2e" is not the same thing as "..":
    # `quote()` re-escapes the `%` (see client.py's module docstring,
    # "already confirmed safe"), so this must not be rejected by the
    # segment check *and* must not resolve outside the repo's contents
    # scope once the URL is normalized -- proven here by checking the
    # actual outbound request URL stayed correctly scoped, not merely "no
    # exception was raised".
    transport = _RecordingTransport(
        [
            httpx2.Response(
                200,
                json={
                    "name": "secret",
                    "path": "secret",
                    "size": 5,
                    "encoding": "base64",
                    "content": _b64("hello"),
                },
            )
        ]
    )
    client, http, _ = _client(transport)

    await client.read_file("acme/widgets", "%2e%2e/%2e%2e/secret")

    assert transport.call_count == 1
    assert transport.requests[0].url.path.startswith("/repos/acme/widgets/contents")
    await http.aclose()


async def test_get_repo_tree_legitimate_special_character_paths_still_work() -> None:  # EDGE-013
    # Regression guard -- the traversal fix must not over-block ordinary
    # paths: root, a nested path, and filenames with Korean text, spaces,
    # `+`, and `#`.
    for path in ("", "src/main.py", "한글 파일.txt", "a+b#c.py", "a b.txt"):
        transport = _RecordingTransport(
            [
                httpx2.Response(
                    200, json={"name": "x", "path": path or "x", "type": "file", "size": 0}
                )
            ]
        )
        client, http, _ = _client(transport)

        await client.get_repo_tree("acme/widgets", path=path)

        assert transport.call_count == 1
        await http.aclose()


async def test_read_file_rejects_dot_segment_in_ref() -> None:  # EDGE-013
    # `ref` is validated by the same rule as `path` (currently a query
    # parameter with no URL-path effect, but the contract is fixed here so
    # a future move of `ref` into the URL path stays protected).
    transport = _RecordingTransport()
    client, http, _ = _client(transport)

    with pytest.raises(ToolError):
        await client.read_file("acme/widgets", "a.py", ref="../etc/passwd")

    assert transport.call_count == 0
    await http.aclose()


async def test_read_file_rejects_backslash_in_ref() -> None:  # EDGE-013
    transport = _RecordingTransport()
    client, http, _ = _client(transport)

    with pytest.raises(ToolError):
        await client.read_file("acme/widgets", "a.py", ref="foo\\bar")

    assert transport.call_count == 0
    await http.aclose()


async def test_read_file_legitimate_ref_with_slash_still_works() -> None:  # EDGE-013
    # Regression guard -- a real branch name containing `/` (e.g.
    # "feature/foo") must not be rejected as a traversal.
    transport = _RecordingTransport(
        [
            httpx2.Response(
                200,
                json={
                    "name": "a.py",
                    "path": "a.py",
                    "size": 5,
                    "encoding": "base64",
                    "content": _b64("hello"),
                },
            )
        ]
    )
    client, http, _ = _client(transport)

    result = await client.read_file("acme/widgets", "a.py", ref="feature/my-branch")

    assert result.status == "complete"
    assert transport.call_count == 1
    await http.aclose()


# --- EDGE-014 / AC-003-3 / AC-005-6 / CTR-008: search qualifier injection (TASK-041) ---


# EDGE-014, AC-003-3, CTR-008
async def test_search_code_rejects_repo_qualifier_or_boolean_injection() -> None:
    # Exact reproduction: `OR repo:` combining an attacker-chosen repo scope
    # with the server-appended one.
    transport = _RecordingTransport()
    client, http, _ = _client(transport)

    with pytest.raises(ToolError):
        await client.search_code("password OR repo:victim-org/secret-repo", "acme/widgets")

    assert transport.call_count == 0
    await http.aclose()


@pytest.mark.parametrize(
    "query",
    [
        "repo:victim/secret",
        "REPO:victim/secret",
        "Repo:victim/secret",
        "org:victim-org",
        "ORG:victim-org",
        "user:victim",
        "User:victim",
        "enterprise:victim-corp",
        "ENTERPRISE:victim-corp",
        "password repo:victim/secret",
    ],
)
async def test_search_code_rejects_forbidden_qualifiers_case_insensitively(
    query: str,
) -> None:  # EDGE-014
    # Qualifier names are matched case-insensitively (unlike the boolean
    # check below): `REPO:` must be blocked exactly like `repo:`.
    transport = _RecordingTransport()
    client, http, _ = _client(transport)

    with pytest.raises(ToolError):
        await client.search_code(query, "acme/widgets")

    assert transport.call_count == 0
    await http.aclose()


@pytest.mark.parametrize("query", ["foo OR bar", "foo NOT bar", "OR foo", "NOT foo"])
async def test_search_code_rejects_top_level_boolean_without_a_qualifier(
    query: str,
) -> None:  # EDGE-014
    # A bare top-level OR/NOT is rejected on its own, independent of whether
    # a forbidden qualifier is also present (defense-in-depth against
    # qualifiers this module has not enumerated).
    transport = _RecordingTransport()
    client, http, _ = _client(transport)

    with pytest.raises(ToolError):
        await client.search_code(query, "acme/widgets")

    assert transport.call_count == 0
    await http.aclose()


# EDGE-014
async def test_search_code_does_not_over_block_lowercase_or_and_not_as_ordinary_words() -> None:
    # GitHub only recognizes uppercase OR/NOT as operators, so the common
    # English words "or"/"not" must remain usable in an ordinary query --
    # over-blocking regression guard.
    transport = _RecordingTransport(
        [httpx2.Response(200, json={"total_count": 0, "incomplete_results": False, "items": []})]
    )
    client, http, _ = _client(transport)

    await client.search_code("fix this or that, not the other thing", "acme/widgets")

    assert transport.call_count == 1
    await http.aclose()


@pytest.mark.parametrize("qualifier", ["language:python", "path:src", "extension:py"])
async def test_search_code_allows_legitimate_qualifiers(qualifier: str) -> None:  # EDGE-014
    # language:/path:/extension: are exactly the qualifiers `search_code`'s
    # own docstring says are legitimate -- must keep working.
    transport = _RecordingTransport(
        [httpx2.Response(200, json={"total_count": 0, "incomplete_results": False, "items": []})]
    )
    client, http, _ = _client(transport)

    await client.search_code(qualifier, "acme/widgets")

    assert transport.call_count == 1
    await http.aclose()


async def test_search_code_puts_repo_qualifier_first_in_the_outgoing_query() -> None:  # EDGE-014
    transport = _RecordingTransport(
        [httpx2.Response(200, json={"total_count": 0, "incomplete_results": False, "items": []})]
    )
    client, http, _ = _client(transport)

    await client.search_code("language:python", "acme/widgets")

    request_url = str(transport.requests[0].url)
    # Decode the `q` param back out rather than assume a particular
    # percent-encoding of `:`/`/` -- robust to encoding-library choices.
    q_value = parse_qs(urlsplit(request_url).query)["q"][0]
    assert q_value.startswith("repo:acme/widgets")
    await http.aclose()


# EDGE-014
async def test_search_code_filters_out_allowlist_violating_results_returned_by_github() -> None:
    # Layer 2, tested in isolation: a *legitimate* query (nothing for layer
    # 1 to reject) where GitHub's own response carries an item scoped to a
    # repository outside the allowlist. This must never reach the caller,
    # regardless of why GitHub returned it.
    transport = _RecordingTransport(
        [
            httpx2.Response(
                200,
                json={
                    "total_count": 3,
                    "incomplete_results": False,
                    "items": [
                        {"path": "src/a.py", "repository": {"full_name": "acme/widgets"}},
                        {
                            "path": "secret.env",
                            "repository": {"full_name": "victim-org/secret-repo"},
                        },
                        {"path": "orphan.py"},  # no `repository` field at all
                    ],
                },
            )
        ]
    )
    client, http, _ = _client(transport, repo_allowlist=frozenset({"acme/widgets"}))

    results = await client.search_code("language:python", "acme/widgets")

    assert [item.path for item in results.items] == ["src/a.py"]
    assert results.total_count == 3  # GitHub's own count is left uncapped/unfiltered
    await http.aclose()


# --- EDGE-014 companion: the `repo` parameter is an injection surface too ------


@pytest.mark.parametrize(
    "repo",
    [
        "victim/x OR repo:secret",  # the actual reproduction
        "victim/x:y",  # colon starts a qualifier
        "victim/x y",  # whitespace separates qualifiers
        "a/b/c",  # a second slash is not an owner/repo name
        "acme/widgets NOT repo:other",
        "",  # empty
    ],
)
async def test_search_code_rejects_a_repo_that_is_not_a_plain_owner_repo_name(
    repo: str,
) -> None:
    """`repo` is interpolated straight into `q=repo:{repo} {query}`.

    Found while verifying TASK-049 end to end: `_split_repo` is deliberately
    loose (non-empty owner and name is all it requires), so
    `"victim/x OR repo:secret"` split happily and reached GitHub as *two*
    qualifiers — the same boundary escape `EDGE-014` closes on the `query`
    side, through a different parameter.

    Not reachable through the MCP surface today, because `tools/guard.py`
    authorizes this argument against `CTR-008`'s exact-membership allowlist
    and `config.py` only admits `^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$` entries.
    Asserted here anyway so `GitHubClient` is safe to call directly rather
    than inheriting its safety from whichever caller wraps it — the same
    reason `EDGE-013` keeps two independent layers.
    """
    transport = _RecordingTransport()
    client, http, _ = _client(transport)

    with pytest.raises(SecurityBoundaryError) as excinfo:
        await client.search_code("safe", repo)

    assert excinfo.value.reason_code == "query_qualifier_injection"
    assert transport.call_count == 0
    # The message must not echo the rejected value back (same contract as
    # auth.policy's denial messages).
    assert repo not in str(excinfo.value) or repo == ""
    await http.aclose()


async def test_search_code_accepts_a_plain_owner_repo_name() -> None:
    """The new check must not reject legitimate repository names.

    Dots, hyphens and underscores are all valid in GitHub owner/repo names,
    so a check that only allowed `[A-Za-z0-9]` would break real callers.
    """
    for repo in ["acme/widgets", "acme-org/my_repo.v2", "a/b"]:
        transport = _RecordingTransport(
            [httpx2.Response(200, json={"total_count": 0, "items": []})]
        )
        client, http, _ = _client(transport, repo_allowlist=frozenset({repo}))

        result = await client.search_code("safe", repo)

        assert result.items == ()
        # The request actually went out — proving the check let it through
        # rather than the assertion above passing on an empty short-circuit.
        assert transport.call_count == 1
        # `url.params` decodes percent-encoding; the raw URL string shows
        # `repo%3Aacme%2Fwidgets`, which would make a substring check on it
        # silently vacuous.
        assert transport.requests[0].url.params["q"] == f"repo:{repo} safe"
        await http.aclose()
