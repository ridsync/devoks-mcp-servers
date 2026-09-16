"""GitHub REST 클라이언트 (TASK-021, `RES-API-002`/`RES-API-003`/`RES-API-004`).

GitHub REST 엔드포인트 3개를 감싸는 얇은 래퍼. 모든 public 메서드는 frozen
dataclass만 반환하며, `httpx2` 예외·GitHub 오류 바디·`InstallationTokenError`를
정규화 없이 흘려보내지 않는다.

오류 정규화 (읽고 나서 손댈 것)
---------------------------------------------------------------------------
`tools/guard.py`(TASK-007)는 `ToolError`/`ResourceError`/`mcp.MCPError`만 그대로
통과시키고 나머지는 일반 메시지로 치환한다(EDGE-009, 원본 메시지 폐기).
`AC-005-7/8/9`가 상태 코드·404 사유·재시도 시점을 그대로 요구하므로, 이 모듈은
모든 GitHub 실패(4xx/5xx, 404, rate limit, 자격증명, 네트워크)를 사설 예외 없이
`ToolError`로 직접 던진다 — 정규화가 이 모듈에서 완결되므로 `TASK-022`의 tool
바디는 아무것도 잡거나 변환하지 않는다(FRD §4.2가 이 책임을 이 모듈에 명시).

`TokenProvider`는 `credentials.InstallationTokenProvider`를 직접 쓰지 않고
`Protocol`로 둔다 — 실제로 쓰는 `get_token`에만 의존해 테스트 더블이 상속 없이
구조적으로 만족하게 한다.

절단/UTF-8 안전성 (`CTR-004`, `AC-005-4`, `EDGE-004`, `EDGE-012`)
---------------------------------------------------------------------------
바이너리 판정(`AC-005-5`)은 **절단 전 전체** 바이트열로 해야 한다 — 먼저 자르고
디코딩하면 캡이 멀티바이트 문자 중간에 걸릴 때 정상 UTF-8 파일을 바이너리로
오판한다. `_decode_utf8_prefix`는 최대 3바이트 백오프로 잘린 멀티바이트 문자를
깨진 글자 대신 통째로 버린다. `_classify_content`가 이 판정을 base64 정상 경로와
`EDGE-012` raw 폴백 경로 양쪽에서 공유해 두 경로 판정이 어긋나지 않게 한다.

`encoding: "none"`(inline 크기 제한 초과)을 과거엔 무조건 `"unavailable"`로
처리했다. GitHub 공식 문서(`contents`, `2022-11-28`) 기준: 1MB 이하는 전체 지원,
**1~100MB는 `raw`/`object` 미디어타입으로만** 내용을 받을 수 있고, **100MB
초과는 엔드포인트 자체가 미지원**이다. `TASK-025`/`EDGE-012`는 `encoding: "none"`을
만나면 메타데이터의 `size`를 보고 100MB 이내면 `Accept:
application/vnd.github.raw+json`으로 1회 추가 요청해 동일한 `_classify_content`로
판정한다 — 1~100MB 텍스트 파일은 이제 `"unavailable"` 대신 `"truncated"`로
돌아온다. 100MB 상한 체크는 요청을 보내기 *전에* 메타데이터만으로 수행해, 이미
실패가 확정된 파일이 rate-limit 단위를 낭비하지 않는다. 다섯 번째 status 값을
따로 만들지 않은 이유: raw-폴백 후 절단은 호출자 입장에서 일반 절단과 필드가
완전히 동일해 구분할 실익이 없고, 100MB 초과는 기존 `"unavailable"` 의미와
정확히 일치한다.

근거 신뢰도 — 이 세션엔 web-search 도구 없음(`ToolSearch`로 확인)
---------------------------------------------------------------------------
1MB/100MB 임계값과 raw 미디어타입 폴백(`EDGE-012`)은 이번 태스크에서 공식 문서로
검증했다(`TASK-021` handover) — 그래서 두 값을 `_CONTENTS_RAW_FALLBACK_MAX_BYTES`에
하드코딩. 나머지(contents 배열/객체 구분, `Link` 페이지네이션, `x-ratelimit-*`
헤더, `search/code`의 `text-match` Accept 값, 403/429 겸용 rate limit 등)는
학습 시점 지식으로 이번 세션엔 재검증하지 않았다 — 문서 접근 가능한 세션에서
재확인할 것.

Base URL은 `credentials.py`의 `_GITHUB_API_BASE_URL`을 공유하지 않고 이 모듈도
따로 선언한다 — 리터럴 하나 때문에 공용 상수 모듈을 새로 만드는 비용이 더 크고,
`credentials.py`(TASK-020)는 최소 수정만 하기로 한 완료 모듈이다.

`search_code(query, repo)`는 다른 메서드와 동일하게 `repo: str` 하나만 받는다 —
`tools/guard.py`의 `repo_arg` 검사가 이름 붙은 문자열 인자 하나만 보는 구조와
맞춘 것. 멀티 리포 검색은 두 번째 허용목록 검사 방식이 필요해 아직 없다.

경로/ref 순회 + 검색 쿼리 인젝션 방어 (`TASK-040`/`TASK-041`, `EDGE-013`/`EDGE-014`)
---------------------------------------------------------------------------
둘 다 내부+외부 2중 레이어 — 한쪽이 뚫려도 다른 쪽이 막는다.

`EDGE-013`(`path`/`ref` 순회, Critical): `guard.py`의 허용목록 검사는 `repo`만
본다. 과거엔 `path`/`ref`가 그대로 `_encode_path`(`/`, `.` 이스케이프 안 함)로
들어가 `../`가 요청 URL에 살아남았다 — `httpx2.URL`이 요청 시 `..`/`.`을
정규화하면서 `path="../../../victim/secret/contents/x"`가 허용된
`/repos/{allowed}/{allowed}/contents` 범위를 벗어나고,
`path="../../../../installation/repositories"`는 이 서버가 노출 안 하는
엔드포인트까지 닿는 것을 목 트랜스포트 테스트로 직접 재현해 확인했다(수정 전).
레이어 1 `_reject_path_traversal`이 `.`/`..` 세그먼트·백슬래시를 `_encode_path`
도달 전에 거부하고, 레이어 2 `_assert_contents_url_scoped`가 정규화된 URL이
여전히 `/repos/{owner}/{name}/contents`로 시작하는지 재검사한다. 레이어 2가
`ToolError`가 아니라 `AssertionError`를 던지는 건 의도적 — 여기 도달했다는 것
자체가 레이어 1의 버그라는 뜻이지 호출자 입력 문제가 아니기 때문(`guard.py`가
EDGE-009로 일반화한다). `read_file`/`get_repo_tree`/raw 폴백이
`_contents_request_target` 하나를 공유해 세 호출부를 한 번에 고쳤다.

`EDGE-014`(`search_code` 쿼리 인젝션, High): 과거엔 `query`를 서버가 붙인
`repo:{repo}` 범위와 그대로 이어붙였다. GitHub 검색 문법이 `repo:`/`org:`/
`user:`/`enterprise:` 다중 지정과 `OR`/`NOT`을 지원하므로
`query="password OR repo:victim/secret"`이 허용 범위 밖으로 넓어진다(OR로
추가될 뿐 덮어써지지 않음). 레이어 1 `_reject_search_qualifier_injection`이
이 네 qualifier나 최상위 `OR`/`NOT`을 전송 전 거부하고, 레이어 2가 응답의 모든
`SearchResultItem.repository`를 `auth.policy.is_repo_allowlisted`로 재검사해
레이어 1이 놓친 경우도 걸러낸다.
"""

from __future__ import annotations

import base64
import binascii
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal, Protocol, cast
from urllib.parse import quote

import httpx2
from mcp.server.mcpserver.exceptions import ToolError

from devoks_mcp_management.adapters.knowledge.github.credentials import InstallationTokenError
from devoks_mcp_management.auth.policy import is_repo_allowlisted
from devoks_mcp_management.config import Settings
from devoks_mcp_management.types import SecurityBoundaryError

#: `config.py`의 `_REPO_ALLOWLIST_ENTRY`와 같은 형태 — import 대신 모듈
#: 로컬 상수로 중복 선언해, 이 어댑터가 config 모듈의 private 이름에
#: 의존하지 않게 한다.
_SAFE_REPO_PATTERN: Final = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

__all__ = [
    "FileContent",
    "GitHubClient",
    "RepositorySummary",
    "SearchResultItem",
    "SearchResults",
    "TokenProvider",
    "TreeEntry",
]

#: Wall-clock epoch 초(`time.time` 형태) — `Retry-After: <delta-seconds>`
#: 헤더를 절대 재시도 시각으로 바꿀 때만 쓴다. `credentials.py`의 동일한
#: "clock 주입" 근거 참고.
Clock = Callable[[], float]

_GITHUB_API_BASE_URL: Final = "https://api.github.com"
_GITHUB_ACCEPT_HEADER: Final = "application/vnd.github+json"
_GITHUB_SEARCH_ACCEPT_HEADER: Final = "application/vnd.github.text-match+json"
#: `EDGE-012` raw-content 폴백 미디어타입 — 기본 `_GITHUB_ACCEPT_HEADER`
#: 응답이 생략하는(`encoding: "none"`) 1~100MB 파일의 실제 바이트를
#: GitHub 문서가 안내하는 방식으로 가져온다.
_GITHUB_RAW_ACCEPT_HEADER: Final = "application/vnd.github.raw+json"
_GITHUB_API_VERSION_HEADER: Final = "2022-11-28"

#: `EDGE-012`: contents 엔드포인트의 GitHub 문서상 상한 — 이 크기를
#: 넘으면 raw 포함 어떤 미디어타입도 내용을 못 돌려준다(GitHub REST API
#: 문서 "this endpoint is not supported"). 이 상한을 넘겨 raw 요청을
#: 보내면 실패만 하고 rate-limit 단위만 낭비하므로,
#: `_read_file_via_raw_fallback`이 (메타데이터 응답으로 이미 알고 있는)
#: 파일 `size`를 두 번째 요청 *전에* 이 상수와 비교한다. `CTR-004`의 1MB
#: 상한과 같은 binary-unit 표기(`1048576` = 1 MiB)를 써서 모듈 내
#: 일관성을 유지한다.
_CONTENTS_RAW_FALLBACK_MAX_BYTES: Final = 100 * 1024 * 1024

#: `credentials.py`의 GitHub 에러 바디 echo 절단 길이와 동일.
_ERROR_BODY_SNIPPET_LEN: Final = 200

_INSTALLATION_REPOS_PER_PAGE: Final = 100
#: `list_installation_repositories`의 페이지네이션 루프 안전 상한 —
#: 페이지당 100개면 5,000개 저장소로, Stage 1 규모의 installation을 훨씬
#: 넘는다. 실제 installation 규모가 아니라 오형식/무한 루프하는 `Link`
#: 헤더를 방어하기 위함.
_MAX_INSTALLATION_REPOS_PAGES: Final = 50

#: `EDGE-014` layer 1의 qualifier 쪽. `\b...:` word boundary로 대소문자
#: *구분 없이*(`REPO:`도 막아야 함) 매치하되 *온전한* qualifier 토큰만
#: 매치한다 — `myrepo:`는 매치 안 됨(`y`와 `r` 사이엔 경계가 없음)이라
#: 식별자스러운 정상 검색어가 부수적으로 거부되지 않는다.
#: `language:`/`path:`/`extension:` 등 다른 qualifier는 의도적으로
#: 그대로 둔다 — `search_code`의 `repo` 인자가 이미 잡아놓은 범위를
#: 넓힐 수 없기 때문.
_SEARCH_QUALIFIER_PATTERN: Final = re.compile(r"\b(?:repo|org|user|enterprise):", re.IGNORECASE)

#: `EDGE-014` layer 1의 boolean 쪽. 위 qualifier 패턴과 달리 의도적으로
#: 대소문자를 *구분*한다 — GitHub 문서상 검색 연산자는 대문자여야만
#: 연산자로 인식되고, 소문자 "or"/"not"은 GitHub에게 그냥 평범한 검색어일
#: 뿐 범위 결합 연산자가 아니다. 여기서 대소문자 구분 없이 매치하면
#: 보안 이득 없이(GitHub는 소문자형을 연산자로 절대 취급 안 하므로)
#: "or"/"not"이 들어간 흔한 영어 쿼리를 거부하게 되는데, 이는 이 태스크
#: 지침이 경계하는 과잉 차단 그 자체다. `\b...\b`로 *온전한* 토큰만
#: 잡아, `NOT_FOUND`(밑줄은 `\w` word 문자라 `T`와 `_` 사이에 경계 없음)나
#: `ORACLE`/`ORDER`(`R`과 다음 글자 사이에 경계 없음) 같은 식별자는 절대
#: 거부되지 않는다.
_SEARCH_BOOLEAN_PATTERN: Final = re.compile(r"\b(?:OR|NOT)\b")


class TokenProvider(Protocol):
    """이 모듈이 token 공급자에게 필요로 하는 구조적 인터페이스.

    `credentials.InstallationTokenProvider`가 상속 없이 이를 만족한다 —
    테스트 더블도 이 메서드 하나만 있으면 된다. 구체 클래스가 아니라
    `Protocol`인 이유는 모듈 docstring 참고.
    """

    async def get_token(self) -> str: ...


# --- 반환 타입 (frozen dataclass; FRD §4.2 project convention) --------


@dataclass(frozen=True, slots=True)
class RepositorySummary:
    """installation이 접근 가능한 저장소 1개(`RES-API-002`, `AC-005-1`)."""

    full_name: str
    """``owner/repo`` — `CTR-008` allowlist 항목이 쓰는 형식 그대로."""

    name: str
    description: str | None
    default_branch: str


@dataclass(frozen=True, slots=True)
class TreeEntry:
    """디렉터리 목록 항목 1개(`RES-API-003`, `AC-005-2`)."""

    name: str
    path: str
    type: str
    """`AC-005-2` 기준 ``"file"`` 또는 ``"dir"``; GitHub는 드문 콘텐츠
    타입으로 ``"symlink"``/``"submodule"``도 낼 수 있는데, 에러가 아니므로
    그대로 통과시킨다."""
    size: int


ContentStatus = Literal["complete", "truncated", "binary", "unavailable"]


@dataclass(frozen=True, slots=True)
class FileContent:
    """파일 하나의 내용을 읽은 결과(`RES-API-003`, `CTR-004`, `AC-005-3/4/5`).

    ``status``로 호출부(`TASK-022`)가 네 가지 경우를 구분한다(handover
    요구사항 ④):

    - ``"complete"``: 파일 전체가 byte cap 이내였고 UTF-8로 디코딩됨.
      ``content``는 파일 전체, ``returned_size == total_size``, ``message``는
      ``None``.
    - ``"truncated"``: 설정된 byte cap을 초과함(`CTR-004`, `AC-005-4`,
      `EDGE-004`). ``content``는 파일의 *byte-prefix*를 무손실 UTF-8로
      디코딩한 것(`_decode_utf8_prefix` 참고), ``returned_size``는
      ``content``의 실제 UTF-8 byte 길이(절단 지점이 문자 중간이면 설정된
      cap보다 몇 byte 작을 수 있음), ``total_size``는 GitHub이 보고한 실제
      크기. ``message``에 두 크기 모두 명시. `EDGE-012` raw-미디어타입
      폴백으로 가져온 1~100MB 파일도 이 값으로 돌아온다 — 이 dataclass
      형태만으론 둘을 구분할 수 없게 설계했다(모듈 docstring "다섯 번째
      status 값을 만들지 않은 이유" 참고).
    - ``"binary"``: 전체 파일이 UTF-8 디코딩 실패(`AC-005-5`, `EDGE-005`).
      ``content``는 ``None``, ``returned_size``는 0, ``total_size``는 전체
      파일 크기. ``message``에 크기 명시. 일반 경로와 `EDGE-012` raw 폴백
      양쪽에서 도달 가능.
    - ``"unavailable"``: GitHub contents 엔드포인트가 어떤 미디어타입으로도
      이 파일 내용을 *아예* 못 줌 — ``size``가 GitHub 문서상 100MB 상한을
      초과(`EDGE-012`, `_CONTENTS_RAW_FALLBACK_MAX_BYTES`). ``content``는
      ``None``, ``returned_size``는 0, ``total_size``는 GitHub의 ``size``
      필드에서 옴(이 상한을 넘으면 raw 요청 자체를 시도하지 않으므로 이
      경우 유일한 크기 출처).
    """

    status: ContentStatus
    content: str | None
    returned_size: int
    total_size: int
    message: str | None


@dataclass(frozen=True, slots=True)
class SearchResultItem:
    """코드 검색 매치 1건(`RES-API-004`, `AC-005-6`)."""

    repository: str
    """매치의 ``owner/repo`` (검색이 항상 저장소 1개로 범위가 고정되므로
    `search_code`에 넘긴 ``repo`` 인자와 같아야 함)."""
    path: str
    excerpt: str | None
    """``text-match`` 미디어타입으로 GitHub가 반환한 ``text_matches``
    fragment를 이어붙인 것, GitHub가 아무것도 안 주면 ``None``."""


@dataclass(frozen=True, slots=True)
class SearchResults:
    """`CTR-005`로 상한을 둔 코드 검색 결과(`RES-API-004`, `AC-005-6`)."""

    items: tuple[SearchResultItem, ...]
    """길이는 설정된 `CTR-005` 최대치로 제한 — tool 계층이 아니라 이
    client가(``per_page``와 방어적 재슬라이스 둘 다로) 강제한다."""
    total_count: int
    """GitHub 자체 전체 매치 수 — 정보 제공용, 상한 없음,
    ``len(items)``보다 클 수 있음."""
    incomplete_results: bool


class GitHubClient:
    """`RES-API-002`/`003`/`004`를 감싼다. 이 클래스가 구현하는 에러
    정규화 계약은 모듈 docstring 참고.
    """

    def __init__(
        self,
        *,
        http_client: httpx2.AsyncClient,
        token_provider: TokenProvider,
        read_file_max_bytes: int,
        search_code_max_results: int,
        repo_allowlist: frozenset[str] = frozenset(),
        clock: Clock = time.time,
    ) -> None:
        self._http_client = http_client
        self._token_provider = token_provider
        self._read_file_max_bytes = read_file_max_bytes
        self._search_code_max_results = search_code_max_results
        #: `EDGE-014` layer 2(`search_code`의 결과측 재필터). 기본값은 빈
        #: set — deny-all — `CTR-008`/`config.py`의 "빈 allowlist는 모든
        #: repo 거부" 관례와 맞춘 것. 명시적으로 안 넘긴 호출부는
        #: fail-open이 아니라 fail-safe가 된다.
        self._repo_allowlist = repo_allowlist
        self._clock = clock

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        http_client: httpx2.AsyncClient,
        token_provider: TokenProvider,
        clock: Clock = time.time,
    ) -> GitHubClient:
        """composition root(`TASK-023`의 lifespan)용 편의 생성자."""
        return cls(
            http_client=http_client,
            token_provider=token_provider,
            read_file_max_bytes=settings.read_file_max_bytes,
            search_code_max_results=settings.search_code_max_results,
            repo_allowlist=settings.repo_allowlist,
            clock=clock,
        )

    # --- RES-API-002 --------------------------------------------------------

    async def list_installation_repositories(self) -> tuple[RepositorySummary, ...]:
        """`GET /installation/repositories`, 완전 페이지네이션(`AC-005-1`).

        첫 페이지만 반환하지 않고 ``Link: rel="next"`` 헤더를 소진될 때까지
        따라간다 — `CTR-008`의 allowlist는 보통 installation 규모보다
        작지만, 2페이지에 걸린 repo가 호출부의 allowlist 필터에서 안 보이게
        되면 안 되기 때문.
        """
        summaries: list[RepositorySummary] = []
        url: str | None = f"{_GITHUB_API_BASE_URL}/installation/repositories"
        params: dict[str, Any] | None = {"per_page": _INSTALLATION_REPOS_PER_PAGE}
        pages_fetched = 0

        while url is not None:
            pages_fetched += 1
            if pages_fetched > _MAX_INSTALLATION_REPOS_PAGES:
                raise ToolError(
                    "GitHub installation repository listing exceeded the pagination "
                    f"safety limit ({_MAX_INSTALLATION_REPOS_PAGES} pages)"
                )

            response = await self._request("GET", url, params=params)
            self._raise_for_status(response, not_found_message="GitHub installation was not found")
            payload = self._parse_json_object(response, context="list_repos")

            raw_repos = payload.get("repositories")
            if not isinstance(raw_repos, list):
                raise ToolError(
                    "GitHub installation repositories response is missing 'repositories'"
                )
            for raw_repo in cast(list[Any], raw_repos):
                summaries.append(_parse_repository_summary(_require_object(raw_repo, "list_repos")))

            next_link = response.links.get("next")
            url = next_link["url"] if next_link else None
            params = None  # 다음 URL이 자체 쿼리 문자열을 이미 담고 있음

        return tuple(summaries)

    # --- RES-API-003 --------------------------------------------------------

    async def get_repo_tree(
        self, repo: str, path: str = "", ref: str | None = None
    ) -> tuple[TreeEntry, ...]:
        """`GET /repos/{o}/{r}/contents/{path}?ref=` (`AC-005-2`).

        GitHub는 디렉터리엔 JSON 배열을, 파일엔 JSON 객체 하나를 반환한다.
        후자는 1개 원소 tuple로 감싸서, 파일 경로를 넘긴 호출부도 형태
        차이에 놀라지 않고 목록을 받게 한다.
        """
        payload = await self._get_contents(repo, path, ref)
        raw_entries: list[Any] = (
            cast(list[Any], payload) if isinstance(payload, list) else [payload]
        )
        return tuple(
            _parse_tree_entry(_require_object(entry, "get_repo_tree")) for entry in raw_entries
        )

    async def read_file(self, repo: str, path: str, ref: str | None = None) -> FileContent:
        """`GET /repos/{o}/{r}/contents/{path}?ref=` (`AC-005-3/4/5`, `CTR-004`).

        네 가지 ``status`` 값은 `FileContent`의 docstring, 절단/바이너리
        판정 순서는 모듈 docstring 참고. ``encoding: "none"``(GitHub
        inline-content 한도 초과 파일)이라고 해서 모든 파일이 2차 HTTP
        호출을 타는 건 **아니다** — 실제로 그 조건에 걸린 파일만
        `EDGE-012` raw-폴백 요청 비용을 치른다. 흔한 1MB 이하 경로는 이
        태스크 이전과 동일하게 요청 1건으로 끝난다.
        """
        payload = await self._get_contents(repo, path, ref)
        if isinstance(payload, list):
            raise ToolError(
                f"GitHub path is a directory, not a file: repo={repo!r} path={path!r} ref={ref!r}"
            )
        obj = _require_object(payload, "read_file")

        if obj.get("encoding") == "none":
            return await self._read_file_via_raw_fallback(repo, path, ref, obj)

        content_b64 = obj.get("content")
        if not isinstance(content_b64, str):
            raise ToolError(f"GitHub response is missing file content: repo={repo!r} path={path!r}")
        try:
            raw_bytes = base64.b64decode(content_b64, validate=False)
        except (binascii.Error, ValueError) as exc:
            raise ToolError(
                f"GitHub file content was not valid base64: repo={repo!r} path={path!r}"
            ) from exc

        return _classify_content(
            raw_bytes, total_size=len(raw_bytes), max_bytes=self._read_file_max_bytes
        )

    async def _read_file_via_raw_fallback(
        self, repo: str, path: str, ref: str | None, metadata: dict[str, Any]
    ) -> FileContent:
        """`EDGE-012`: raw 미디어타입으로 1-100MB 파일의 실제 바이트를
        가져오거나, 100MB 초과면 미지원으로 보고한다 — 문서로 검증한
        임계값 근거는 모듈 docstring 참고.

        ``metadata``는 이 폴백을 트리거한(``encoding: "none"``) 이미 파싱된
        메타데이터 전용 응답 — 아래 두 결과 모두 `total_size`는 이
        ``size`` 필드에서만 나온다. raw 응답 자체엔 `size` 필드가 없는
        순수 byte stream이기 때문(handover 요구사항 ⑤).
        """
        total_size = _require_int(metadata, "size", "read_file")

        if total_size > _CONTENTS_RAW_FALLBACK_MAX_BYTES:
            # raw 요청 자체를 아예 안 보낸다: 이 크기를 넘으면 GitHub는 어떤
            # 미디어타입으로도 이 엔드포인트로 내용을 안 주므로, 여기서
            # 요청해봐야 실패만 하고 rate-limit 단위만 날린다.
            return FileContent(
                status="unavailable",
                content=None,
                returned_size=0,
                total_size=total_size,
                message=(
                    "GitHub cannot serve this file's content through the contents API "
                    f"(size {total_size} bytes exceeds GitHub's "
                    f"{_CONTENTS_RAW_FALLBACK_MAX_BYTES}-byte limit for this endpoint, "
                    "even via the raw media type)."
                ),
            )

        url, params = self._contents_request_target(repo, path, ref)
        response = await self._request(
            "GET", url, params=params, headers={"Accept": _GITHUB_RAW_ACCEPT_HEADER}
        )
        self._raise_for_status(
            response, not_found_message=self._contents_not_found_message(repo, path, ref)
        )
        return _classify_content(
            response.content, total_size=total_size, max_bytes=self._read_file_max_bytes
        )

    def _contents_request_target(
        self, repo: str, path: str, ref: str | None
    ) -> tuple[str, dict[str, Any] | None]:
        """메타데이터 요청(`_get_contents`)과 `EDGE-012` raw-폴백 요청이
        공유하는 URL/params 빌더 — 둘 다 완전히 같은 contents 엔드포인트를
        치고, ``Accept`` 헤더만 다르다.

        `EDGE-013`의 2계층 path/ref traversal 방어가 여기 있어 `read_file`/
        `get_repo_tree`(및 이 메서드를 함께 쓰는 raw-폴백 경로)가 별도
        조치 없이 그 방어를 받는다: `_reject_path_traversal`(layer 1)이
        `path`/`ref`의 `.`/`..` 세그먼트나 백슬래시를 `_encode_path` 도달
        전에 거부하고, `_assert_contents_url_scoped`(layer 2)가 빌드된
        URL을 *정규화 후* 재검사한다. 왜 독립된 2계층인지는 모듈 docstring
        참고.
        """
        owner, name = _split_repo(repo)
        _reject_path_traversal(path, field="path")
        if ref is not None:
            _reject_path_traversal(ref, field="ref")
        encoded_path = _encode_path(path)
        suffix = f"/{encoded_path}" if encoded_path else ""
        url = f"{_GITHUB_API_BASE_URL}/repos/{owner}/{name}/contents{suffix}"
        _assert_contents_url_scoped(url, owner=owner, name=name)
        params: dict[str, Any] | None = {"ref": ref} if ref else None
        return url, params

    def _contents_not_found_message(self, repo: str, path: str, ref: str | None) -> str:
        # AC-003-5 / EDGE-006: 이 404는 `tools/guard.py`의 authorize()가
        # 이미 CTR-008 allowlist에 있다고 확인한 `repo`에 대해서만 도달한다
        # — allowlist 게이트가 tool 본문(따라서 이 client)보다 먼저
        # 실행되기 때문. 그래서 여기서 repo/path/ref를 명시해도
        # allowlist 밖 저장소의 존재를 새지 않는다 — 호출부가 이미 알
        # 권한이 있는 것만 설명할 뿐이다.
        return f"GitHub repository, ref, or path not found: repo={repo!r} path={path!r} ref={ref!r}"

    async def _get_contents(self, repo: str, path: str, ref: str | None) -> Any:
        url, params = self._contents_request_target(repo, path, ref)
        response = await self._request("GET", url, params=params)
        self._raise_for_status(
            response, not_found_message=self._contents_not_found_message(repo, path, ref)
        )
        return self._parse_json(response, context="get_repo_tree/read_file")

    # --- RES-API-004 --------------------------------------------------------

    async def search_code(self, query: str, repo: str) -> SearchResults:
        """`GET /search/code`, 저장소 1개로 범위 고정(`AC-005-6`, `CTR-005`).

        GitHub가 excerpt fragment를 포함하도록 ``text-match`` 미디어타입을
        쓴다(이 Accept 값의 문서 신뢰도는 모듈 docstring 참고). 검색은
        기본 API보다 훨씬 좁은 자신만의 rate limit을 갖는데, rate-limit
        판정이 엔드포인트별이 아니라 헤더 기반이라 여기서도 동일하게
        처리된다(`_raise_for_status` 참고).

        `EDGE-014`의 2계층 qualifier-injection 방어: `query`가
        `repo:`/`org:`/`user:`/`enterprise:` qualifier나 최상위 `OR`/`NOT`을
        담고 있으면 `_reject_search_qualifier_injection`(layer 1)이 먼저
        거부하고, 반환된 모든 항목의 `repository`를 무조건 allowlist로
        재검사한다(layer 2, 아래). 두 계층이 왜 다 필요한지는 모듈
        docstring 참고.
        """
        _reject_search_qualifier_injection(query)
        # `repo`는 아래 쿼리 문자열에 그대로 보간되므로 두 번째 injection
        # 경로다 — `_split_repo`가 의도적으로 느슨해서(비어있지 않은
        # owner/name만 요구) `"victim/x OR repo:secret"`도 순순히 쪼개져
        # GitHub엔 qualifier 2개로 도달한다. TASK-049 end-to-end 검증 중
        # 발견.
        #
        # 현재 MCP 표면으로는 도달 불가: `tools/guard.py`가 `search_code`의
        # `repo`를 `CTR-008`로 인가하는데, 이는 `config.py`가 이미
        # `^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$`로 검증한 frozenset 항목과의
        # 정확한 일치라 이 게이트를 통과한 값엔 공백이나 콜론이 있을 수
        # 없다. `EDGE-013`과 같은 2계층 배치의 입력측 절반이다(layer 1이
        # 거부, layer 2가 사후 재검증) — `GitHubClient`를 직접 호출해도
        # 안전하고, 감싸는 호출부에 안전성을 암묵적으로 위임하지 않는다.
        _reject_unsafe_repo(repo)
        # `repo:{repo}`를 (뒤에 붙이지 않고) 쿼리 맨 앞에 둬서 서버가 강제한
        # 범위가 주 qualifier로 읽히고 호출자 텍스트는 명백히 부차적이게
        # 한다 — GitHub 쿼리 파서는 qualifier 순서를 신경 안 쓰므로 순전히
        # 가독성/문서화 목적의 선택이지만, 로그에 남은 쿼리를 읽는 사람에게
        # 의도를 명확히 한다.
        full_query = f"repo:{repo} {query}"
        url = f"{_GITHUB_API_BASE_URL}/search/code"
        params: dict[str, Any] = {"q": full_query, "per_page": self._search_code_max_results}

        response = await self._request(
            "GET", url, params=params, headers={"Accept": _GITHUB_SEARCH_ACCEPT_HEADER}
        )
        self._raise_for_status(
            response,
            not_found_message=f"GitHub search scope not found: repo={repo!r}",
        )
        payload = self._parse_json_object(response, context="search_code")

        total_count = _require_int(payload, "total_count", "search_code", default=0)
        incomplete_results = bool(payload.get("incomplete_results", False))
        raw_items = payload.get("items")
        items_list = cast(list[Any], raw_items) if isinstance(raw_items, list) else []

        # CTR-005: `per_page`로 이미 요청했든 말든 여기서 강제 — GitHub가
        # 요청을 실제로 지켰는지에 대한 신뢰 경계가 아니라 방어적 재슬라이스.
        parsed_items = (
            _parse_search_item(_require_object(item, "search_code"))
            for item in items_list[: self._search_code_max_results]
        )
        # EDGE-014 layer 2: repository가 allowlist에 없는 항목은 모두
        # 버린다(파싱 가능한 `repository`가 아예 없는 항목도 함께 버려짐 —
        # `_parse_search_item`은 그걸 `""`로 보고하는데, 이는 비어있지
        # 않은 allowlist엔 절대 있을 수 없는 값이다 — 이 모듈은 알려진
        # repo에 귀속시킬 수 없는 결과를 보증할 수 없기 때문).
        items = tuple(
            item
            for item in parsed_items
            if is_repo_allowlisted(item.repository, self._repo_allowlist)
        )
        return SearchResults(
            items=items, total_count=total_count, incomplete_results=incomplete_results
        )

    # --- 공유 transport / 에러 정규화 -----------------------------

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx2.Response:
        """최신 installation token을 붙여 요청 1건을 보낸다.

        `get_token()`은 매 호출마다 await(이 client가 호출 간에 캐싱하지
        않음) — token 공급자가 캐싱/갱신을 소유하므로, 여기서 토큰을
        들고 있으면 만료 처리가 무의미해진다. `credentials.py` 모듈
        docstring의 "TASK-021 사용 패턴" 참고.
        """
        try:
            token = await self._token_provider.get_token()
        except InstallationTokenError as exc:
            # InstallationTokenError 자신의 메시지는 이미 비밀값 없음이
            # 보장돼 있음(그 docstring 참고) — 그대로 넣어도 안전.
            raise ToolError(f"GitHub credentials unavailable: {exc}") from exc

        request_headers: dict[str, str] = {
            "Authorization": f"Bearer {token}",
            "Accept": _GITHUB_ACCEPT_HEADER,
            "X-GitHub-Api-Version": _GITHUB_API_VERSION_HEADER,
        }
        if headers:
            request_headers.update(headers)

        try:
            return await self._http_client.request(
                method, url, params=params, headers=request_headers
            )
        except httpx2.HTTPError as exc:
            # str(exc)를 그대로 쓰지 않고 의도적으로 직접 구성 — credentials.py
            # 의 동일한 구조와 같은 이유: 향후 httpx2 버전이 예외 문자열 형태에
            # 뭘 담든 여기엔 요청 헤더가 절대 섞이면 안 된다.
            raise ToolError(
                f"GitHub API request failed before a response was received ({type(exc).__name__})"
            ) from exc

    def _raise_for_status(self, response: httpx2.Response, *, not_found_message: str) -> None:
        """non-2xx 응답을 `ToolError`로 정규화(`AC-005-7/8/9`, `EDGE-003/006`).

        403/429 우선순위(rate limit vs. 일반 권한 거부):
        1. `Retry-After` 헤더 있음 -> rate limited(secondary limit).
        2. `x-ratelimit-remaining: 0` + `x-ratelimit-reset` -> rate
           limited(primary 또는 search limit).
        3. 헤더 둘 다 없는 순수 `429` -> 그래도 rate limited로 처리(HTTP
           시맨틱상 429는 항상 "too many requests"), 재시도 시각만 모름.
        4. 그 외 `403`은 일반 권한 거부, rate limit 아님.
        """
        if not response.is_error:
            return
        status = response.status_code

        if status == 404:
            raise ToolError(not_found_message)

        if status in (403, 429):
            is_rate_limited, retry_at = self._rate_limit_retry_at(response)
            if is_rate_limited:
                raise ToolError(self._rate_limit_message(retry_at, response))

        raise ToolError(self._generic_error_message(response))

    def _rate_limit_retry_at(self, response: httpx2.Response) -> tuple[bool, datetime | None]:
        retry_after_header = response.headers.get("retry-after")
        if retry_after_header is not None:
            try:
                delta_seconds = int(retry_after_header)
            except ValueError:
                return True, None
            return True, datetime.fromtimestamp(self._clock() + delta_seconds, tz=UTC)

        remaining = response.headers.get("x-ratelimit-remaining")
        reset = response.headers.get("x-ratelimit-reset")
        if remaining == "0" and reset is not None:
            try:
                return True, datetime.fromtimestamp(int(reset), tz=UTC)
            except ValueError, OSError, OverflowError:
                return True, None

        if response.status_code == 429:
            return True, None

        return False, None

    def _rate_limit_message(self, retry_at: datetime | None, response: httpx2.Response) -> str:
        resource = response.headers.get("x-ratelimit-resource")
        scope = f" ({resource})" if resource else ""
        if retry_at is not None:
            return f"GitHub API rate limit exceeded{scope}; retry after {retry_at.isoformat()}"
        return f"GitHub API rate limit exceeded{scope}; retry time was not provided by GitHub"

    def _generic_error_message(self, response: httpx2.Response) -> str:
        snippet = response.text[:_ERROR_BODY_SNIPPET_LEN]
        return f"GitHub API request failed: HTTP {response.status_code} {snippet!r}"

    def _parse_json(self, response: httpx2.Response, *, context: str) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise ToolError(f"GitHub {context} response was not valid JSON") from exc

    def _parse_json_object(self, response: httpx2.Response, *, context: str) -> dict[str, Any]:
        return _require_object(self._parse_json(response, context=context), context)


# --- 모듈 레벨 파싱 헬퍼 -------------------------------------------


def _split_repo(repo: str) -> tuple[str, str]:
    owner, _, name = repo.partition("/")
    if not owner or not name:
        raise ToolError(f"repo must be 'owner/repo', got {repo!r}")
    return owner, name


def _encode_path(path: str) -> str:
    return quote(path.strip("/"), safe="/")


def _reject_path_traversal(value: str, *, field: str) -> None:
    """`EDGE-013` layer 1: `.`/`..` path segment나 백슬래시가 든 `path`/`ref`
    값을 `_encode_path`/GitHub URL에 닿기 전에 거부한다 — 이게 막는 취약점은
    모듈 docstring 참고.

    substring 검사가 아니라 *세그먼트 단위*(``value.split("/")``)로
    검사하므로 점이 들어간 정상 파일명(``foo..bar``, ``...md``)은 절대
    거부되지 않는다 — *정확히* ``.``나 ``..``인 세그먼트만 traversal
    토큰이다. 백슬래시는 값 어디에 있든(전체 세그먼트가 아니어도) 무조건
    거부 — 정상적인 GitHub `path`/`ref` 세그먼트는 백슬래시가 필요 없고,
    일부 HTTP/URL 스택은 이를 ``/``처럼 경로 구분자로 취급하는데
    세그먼트 전용 검사로는 이를 놓친다.

    거부된 필드와 규칙 이름만 담아 `ToolError`를 던진다 — 값 자체, allowlist
    내용, 다른 저장소 존재 여부는 절대 담지 않는다 — `auth.policy`의 거부
    메시지 계약과 동일(호출자는 입력이 거부됐다는 사실만 알 뿐 서버 설정에
    대해선 아무것도 알 수 없다).
    """
    if "\\" in value:
        raise SecurityBoundaryError(
            f"Invalid {field!r} argument: backslashes are not allowed",
            reason_code="path_traversal_attempt",
        )
    for segment in value.split("/"):
        if segment in (".", ".."):
            raise SecurityBoundaryError(
                f"Invalid {field!r} argument: '.' and '..' path segments are not allowed",
                reason_code="path_traversal_attempt",
            )


def _assert_contents_url_scoped(url: str, *, owner: str, name: str) -> None:
    """`EDGE-013` layer 2: 위 `_reject_path_traversal`과 독립적으로, RFC 3986
    정규화(`httpx2.URL`) *이후*의 `url`을 재검사한다 — 이 모듈이 예상 못한
    미래의 dot-segment 우회(다른 인코딩, 정규화의 별난 동작)도 이 repo의
    `contents` scope 밖 GitHub 엔드포인트엔 절대 닿지 못하게 하기 위함.
    원본 `url` 문자열이 아니라 *정규화된* `.path`(dot-segment 해석, percent
    디코딩됨)를 검사한다 — 정규화 자체가 원래 취약점을 뚫었던 그 단계라,
    이번엔 같은 단계를 공격이 아니라 방어로 다시 돌리는 것. 전체 근거와
    재현은 모듈 docstring 참고.

    `ToolError`가 아니라 `AssertionError`를 던진다: 위 layer 1이 이미 실무상
    이 지점을 도달 불가능하게 만들어야 하므로, 여기 걸렸다면 호출자 입력
    문제가 아니라 이 모듈 자신의 URL 구성에 버그가 있다는 뜻이다.
    `tools/guard.py`가 어차피 처리 안 된 예외를 일반 client-safe 에러로
    바꾸지만(EDGE-009, 불일치 내용은 새지 않음), 이 메시지는 디버깅할
    사람을 위해 서버 로그엔 그대로 남는다.
    """
    expected_prefix = f"/repos/{owner}/{name}/contents"
    normalized_path = httpx2.URL(url).path
    if normalized_path != expected_prefix and not normalized_path.startswith(f"{expected_prefix}/"):
        raise AssertionError(
            "GitHub contents URL escaped its expected scope after normalization: "
            f"expected prefix {expected_prefix!r}, got {normalized_path!r}"
        )


def _reject_unsafe_repo(repo: str) -> None:
    """`EDGE-014` 동반 검사: 검색 쿼리에 보간되기 전에 평범한 ``owner/repo``
    형태가 아닌 `repo`를 거부한다.

    ``config.py``가 `MCP_REPO_ALLOWLIST` 항목에 강제하는 패턴을 그대로
    미러링해, 여기서 허용되는 값은 실제로 *설정 가능한* 값뿐이다 —
    공백·콜론·두 번째 슬래시가 있으면(전부 GitHub 쿼리 파서가 추가
    qualifier로 읽을 수 있음) 거부. `query`측 injection과 같은
    `SecurityBoundaryError` reason code를 쓴다 — 다른 파라미터를 통한 같은
    공격이기 때문.
    """
    if not _SAFE_REPO_PATTERN.match(repo):
        raise SecurityBoundaryError(
            "Invalid 'repo' argument: must be a plain 'owner/repo' name",
            reason_code="query_qualifier_injection",
        )


def _reject_search_qualifier_injection(query: str) -> None:
    """`EDGE-014` layer 1: GitHub 검색 qualifier(`repo:`/`org:`/`user:`/
    `enterprise:`)나 최상위 boolean(`OR`/`NOT`)으로 범위를 넓히거나
    리다이렉트하려는 `search_code` `query`를 거부한다 — 둘 다 GitHub 자체
    문서화된 multi-qualifier/boolean 쿼리 문법을 통해 서버가 붙인
    `repo:{repo}` scope에 공격자가 고른 추가 scope를 결합시킬 수 있다.
    전체 재현과 qualifier 검사는 대소문자 무시인데 boolean 검사는 아닌
    이유는 모듈 docstring 참고.

    어떤 규칙을 위반했는지만 담아 `ToolError`를 던진다 — allowlist 내용이나
    다른 저장소 존재 여부는 절대 담지 않는다.
    """
    qualifier_match = _SEARCH_QUALIFIER_PATTERN.search(query)
    if qualifier_match is not None:
        raise SecurityBoundaryError(
            f"Invalid search query: {qualifier_match.group().rstrip(':')!r}-style "
            "qualifiers are not allowed; search is always scoped to the requested "
            "repository",
            reason_code="query_qualifier_injection",
        )
    if _SEARCH_BOOLEAN_PATTERN.search(query) is not None:
        raise SecurityBoundaryError(
            "Invalid search query: top-level OR/NOT boolean operators are not allowed",
            reason_code="query_qualifier_injection",
        )


def _is_valid_utf8(data: bytes) -> bool:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _classify_content(raw_bytes: bytes, *, total_size: int, max_bytes: int) -> FileContent:
    """`read_file`의 두 응답 형태(base64 ``content`` 필드,
    `EDGE-012` raw-미디어타입 폴백)가 공유하는 바이너리 판정 +
    `CTR-004` 절단 결정 — 왜 구현이 둘이 아니라 하나여야 하는지는 모듈
    docstring "절단/UTF-8 안전성" 절 참고.

    ``total_size``는 호출부가 넘긴 파일 실제 크기의 근거(base64 경로
    자체 응답의 GitHub ``size`` 필드, 또는 raw-폴백 경로의 메타데이터
    전용 응답 — raw 응답 자체엔 ``size`` 필드가 없음). ``raw_bytes``는
    실제로 디코딩/절단된 값 — base64 경로에선 둘이 같은 값이지만
    raw-폴백 경로에선 출처가 다르다(handover 요구사항 ⑤).
    """
    # 바이너리 판정은 절단된 prefix가 아니라 *디코딩된 전체* 내용으로 —
    # 모듈 docstring 참고.
    if not _is_valid_utf8(raw_bytes):
        return FileContent(
            status="binary",
            content=None,
            returned_size=0,
            total_size=total_size,
            message=f"File is binary (not valid UTF-8); size is {total_size} bytes.",
        )

    if len(raw_bytes) <= max_bytes:
        return FileContent(
            status="complete",
            content=raw_bytes.decode("utf-8"),
            returned_size=len(raw_bytes),
            total_size=total_size,
            message=None,
        )

    truncated_bytes = raw_bytes[:max_bytes]
    text = _decode_utf8_prefix(truncated_bytes)
    returned_size = len(text.encode("utf-8"))
    return FileContent(
        status="truncated",
        content=text,
        returned_size=returned_size,
        total_size=total_size,
        message=(
            f"Content truncated to {returned_size} of {total_size} bytes (limit {max_bytes} bytes)."
        ),
    )


def _decode_utf8_prefix(data: bytes) -> str:
    """이미 유효한 UTF-8로 확인된 문서의 byte-prefix를 디코딩한다.

    `CTR-004`는 byte 수로 절단하지만 그 지점이 multi-byte UTF-8 문자 중간에
    걸릴 수 있다. 최대 3바이트(UTF-8 문자의 최대 길이) 백오프하면 항상
    유효한 경계를 찾는다 — *전체* 문서가 이미 디코딩에 성공했으므로 prefix
    디코딩이 실패할 수 있는 경우는 맨 끝 문자가 잘린 경우뿐이기 때문.
    ``errors="ignore"``보다 이 방식을 쓰는 이유는, 우연히 만난 다른 깨진
    byte sequence까지 조용히 버리는 대신 끝에서 최대 한 문자만 통째로
    버리기 때문이다.
    """
    for backoff in range(4):
        candidate = data[: len(data) - backoff] if backoff else data
        try:
            return candidate.decode("utf-8")
        except UnicodeDecodeError:
            continue
    raise AssertionError(
        "unreachable: _decode_utf8_prefix requires the full document to already be valid UTF-8"
    )


def _require_object(value: object, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ToolError(f"GitHub {context} response contained a non-object entry")
    return cast(dict[str, Any], value)


def _require_str(obj: Mapping[str, Any], key: str, context: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str):
        raise ToolError(f"GitHub {context} response is missing a valid {key!r} field")
    return value


def _require_int(obj: Mapping[str, Any], key: str, context: str, default: int | None = None) -> int:
    value = obj.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        if default is not None:
            return default
        raise ToolError(f"GitHub {context} response is missing a valid {key!r} field")
    return value


def _parse_repository_summary(raw: Mapping[str, Any]) -> RepositorySummary:
    description = raw.get("description")
    return RepositorySummary(
        full_name=_require_str(raw, "full_name", "list_repos"),
        name=_require_str(raw, "name", "list_repos"),
        description=description if isinstance(description, str) else None,
        default_branch=_require_str(raw, "default_branch", "list_repos"),
    )


def _parse_tree_entry(raw: Mapping[str, Any]) -> TreeEntry:
    return TreeEntry(
        name=_require_str(raw, "name", "get_repo_tree"),
        path=_require_str(raw, "path", "get_repo_tree"),
        type=_require_str(raw, "type", "get_repo_tree"),
        size=_require_int(raw, "size", "get_repo_tree", default=0),
    )


def _parse_search_item(raw: Mapping[str, Any]) -> SearchResultItem:
    path = _require_str(raw, "path", "search_code")
    repo_obj = raw.get("repository")
    full_name = None
    if isinstance(repo_obj, dict):
        candidate = cast(dict[str, Any], repo_obj).get("full_name")
        full_name = candidate if isinstance(candidate, str) else None
    return SearchResultItem(
        repository=full_name or "", path=path, excerpt=_extract_excerpt(raw.get("text_matches"))
    )


def _extract_excerpt(text_matches: object) -> str | None:
    if not isinstance(text_matches, list):
        return None
    fragments: list[str] = []
    for match in cast(list[Any], text_matches):
        if isinstance(match, dict):
            fragment = cast(dict[str, Any], match).get("fragment")
            if isinstance(fragment, str):
                fragments.append(fragment)
    return "\n---\n".join(fragments) if fragments else None
