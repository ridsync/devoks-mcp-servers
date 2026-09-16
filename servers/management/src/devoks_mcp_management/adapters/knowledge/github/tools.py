"""GitHub MCP 툴 4종의 인자 스키마와 응답 변환(TASK-022).

`register(mcp, guard) -> None`이 이 모듈의 진입점 — `tools/registry.py` 계약대로
`server.create_server`가 서버 인스턴스의 `Settings`로 만든 `Guard`를 넘겨 1회
호출한다.

Tool 함수는 모듈 스코프에 **데코레이터 없이** 정의하고 `register()` 안에서만
`guard(...)`로 감싼다 — `def` 위에 `@guard(...)`를 바로 붙이지 않는다. import
시점엔 `guard`가 존재하지 않기 때문이다(`tools.guard.make_tool_guard(settings)`
의 반환값, 서버 인스턴스별로 만들어짐 — 이유는 `tools/guard.py`/
`tools/registry.py` 참고).

**JSON Schema가 wrapper에 오염되지 않는 이유**: `guard`가 `functools.wraps(fn)`
으로 `__wrapped__ = fn`을 복사하므로, SDK가 tool 형태를 유도하는 두 지점
(`func_metadata`의 `inspect.signature(..., eval_str=True)`,
`find_context_parameter`의 `typing.get_type_hints`) 모두 이 체인을 따라가
wrapper의 `*args, **kwargs`가 아닌 원본 `fn`의 실제 시그니처를 본다. 그래서
`ctx: Context`도 정상적으로 스키마에서 제외된다(`skip_names`). 실측: 이
태스크 테스트 파일의 `tools/list` 단언이 4개 tool 모두
`input_schema.properties`에 실제 도메인 파라미터(`repo`/`path`/`ref`/`query`)만
있고 `ctx`/`args`/`kwargs`는 없음을 확인한다 — `tools/guard.py`는 이 태스크를
위해 손댈 필요가 없었다.

**TASK-023(`GitHubToolContext`)과의 lifespan 계약**: tool은 TASK-023의
lifespan이 실행되기 훨씬 전, `create_server` 시점에 *등록*되므로(`tools/
registry.py`의 "Dependency timing" 참고) 모든 tool 본문은 클로저가 아니라
호출 시점마다 `ctx.request_context.lifespan_context`를 통해 `GitHubClient`를
꺼낸다. `GitHubToolContext`는 이 계약의 이 모듈 쪽 Protocol — 런타임 보장은
아니므로(`@runtime_checkable` 아님, attribute 존재만 확인) `_require_lifespan`
이 사용 전 매번 `isinstance`로 재검증해 lifespan 누락/오형식(SDK 기본
lifespan은 빈 `{}` — 설치된 `mcp==2.1.1`에서 실측)을 조용한 `AttributeError`
대신 명확한 `ToolError`로 바꾼다.

**`repo_allowlist`가 `github`와 같은 Protocol에 얹힌 이유**: `list_repos`
(`AC-005-1`)는 `guard(...)`의 `repo_arg`가 검사할 `repo` 인자가 없는 유일한
tool이라 allowlist 필터링이 자신의 몫인데, `register(mcp, guard)`(`Registrar`
시그니처 `(mcp, guard) -> None`)엔 `Settings`를 넘길 방법이 없다. 모든 tool
호출에 닿으면서 `Settings`에도 접근 가능한 유일한 값이 lifespan context라,
`repo_allowlist: frozenset[str]`를 별도 주입 경로 대신 `github` 옆에 함께
실었다.

**`list_repos`가 allowlist 항목별 조회 대신 installation 목록을 통째로
필터링하는 이유**: `GitHubClient`엔 "저장소 1개 조회" 메서드가 없고(새 메서드
추가는 범위 밖 — `client.py`는 "하지 말 것" 목록), `AC-005-1`이 필요한 필드는
`list_installation_repositories()`뿐이다. 그래서 installation의 전체
목록을 가져와 `repo_allowlist`에도 있는 항목만 남긴다
(`auth.policy.is_repo_allowlisted`) — AC-005-1의 "installation이 못 보는
allowlist 항목은 결과에 없어야 한다"를 그대로 만족한다.

`search_code`의 rate-limit 안내(분당 10건, 다른 검색 엔드포인트의 30건보다
좁음 — RES-API-004)는 이 모듈 docstring뿐 아니라 자신의 docstring에도 있다.
tool docstring은 호출 모델에 실제로 전달되는 텍스트이기 때문이다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol, TypedDict, cast

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

from devoks_mcp_management.adapters.knowledge.github.client import (
    FileContent,
    GitHubClient,
    RepositorySummary,
    SearchResultItem,
    SearchResults,
    TreeEntry,
)
from devoks_mcp_management.auth.policy import is_repo_allowlisted
from devoks_mcp_management.types import (
    TOOL_GET_REPO_TREE,
    TOOL_LIST_REPOS,
    TOOL_READ_FILE,
    TOOL_SEARCH_CODE,
)

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    # 타입체크 시점에만 import: `registry.py`가 자신의 모듈 스코프에서 이
    # 모듈의 `register`를 import하므로(`tools/registry.py`의 "how TASK-022
    # appends" 절) 여기서 `registry.py`의 뭔가를 *런타임*에 import하면 순환
    # import가 된다. `Guard`는 아래에서 타입 위치로만 쓰이고, 이 파일 첫
    # import인 `from __future__ import annotations`가 모든 annotation을
    # 런타임엔 평가 안 되는 문자열로 유지하므로, 이 forward reference는
    # 타입체커 밖에서 실제로 resolve될 필요가 없다.
    from devoks_mcp_management.tools.registry import Guard

__all__ = ["GitHubToolContext", "register"]


class GitHubToolContext(Protocol):
    """이 tool들이 필요로 하는 MCP *프로토콜* lifespan context의 형태.

    TASK-023이 자신의 `lifespan()` 안에서 실값을 만들어 `MCPServer(...,
    lifespan=...)`의 인자로 넘긴다 — 왜 구체 dataclass가 아니라 Protocol인지,
    그리고 왜 여기로 읽는 attribute를 호출 시점에 `isinstance`로 재검증하는지는
    모듈 docstring의 "TASK-023과의 lifespan 계약" 절 참고.

    아래 attribute 이름은 정확히 지켜야 한다 — TASK-023은 `github`와
    `repo_allowlist`를 정확히 노출하는 객체를 채워야 한다.
    """

    github: GitHubClient
    """TASK-023이 기동 시 1회 만드는 `GitHubClient`(`DSN-004`)."""

    repo_allowlist: frozenset[str]
    """`CTR-008` 설정 allowlist — `Settings.repo_allowlist` 그대로.
    `list_repos`만 읽는다(이유는 모듈 docstring 참고)."""


def _require_lifespan(ctx: Context) -> tuple[GitHubClient, frozenset[str]]:
    """`ctx`의 lifespan context를 가져와 `GitHubToolContext`와 대조 검증한다.

    lifespan context가 없거나(TASK-023이 실제 lifespan을 연결하기 전엔 SDK
    기본 lifespan이 `{}`를 냄) 기대 형태와 다르면 평범한 `AttributeError`가
    아니라 `ToolError`를 던진다. `isinstance`로 재검증하는 이유는
    `GitHubToolContext`의 docstring 참고.
    """
    lifespan_context = ctx.request_context.lifespan_context
    github = getattr(lifespan_context, "github", None)
    repo_allowlist = getattr(lifespan_context, "repo_allowlist", None)
    if not isinstance(github, GitHubClient) or not isinstance(repo_allowlist, frozenset):
        raise ToolError(
            "GitHub tools are unavailable: the server has not finished configuring its "
            "GitHub client yet. This is a server configuration issue, not a caller "
            "error — contact the server operator."
        )
    # `isinstance(x, frozenset)`는 컨테이너만 좁히고 원소 타입은 못 좁힌다
    # (pyright: reportUnknownVariableType) -- `repo_allowlist`는 이 서버 자신의
    # `Settings.repo_allowlist`(이미 `config.py`가 `frozenset[str]`로 검증)이지
    # 외부/미신뢰 입력이 아니므로 얕은 컨테이너 검사로 충분하고 이 cast는 안전.
    return github, cast(frozenset[str], repo_allowlist)


# --- 구조화 응답 payload (SDK가 `TypedDict` 반환을 자동으로 이 tool의
# `output_schema`/`structured_content`로 인식 — 설치된 `mcp==2.1.1`의
# `func_metadata` docstring 참고) -------------------------------------------------


class RepoSummaryPayload(TypedDict):
    full_name: str
    name: str
    description: str | None
    default_branch: str


class ListReposResult(TypedDict):
    repos: list[RepoSummaryPayload]
    count: int


class TreeEntryPayload(TypedDict):
    name: str
    path: str
    type: str
    size: int


class GetRepoTreeResult(TypedDict):
    repo: str
    path: str
    ref: str | None
    entries: list[TreeEntryPayload]
    count: int


#: `client.ContentStatus`를 그대로 미러링(직접 import는 안 함: 그 alias는
#: `client.py`의 `__all__`에 없어 이 모듈은 그걸 private로 취급하고 자체
#: 사본을 둔다 — 4개 값은 설계상 닫혀 있음, 그 모듈의 `FileContent` docstring
#: "다섯 번째 status 값을 만들지 않은 이유" 참고).
ReadFileStatus = Literal["complete", "truncated", "binary", "unavailable"]


class ReadFileResult(TypedDict):
    repo: str
    path: str
    ref: str | None
    status: ReadFileStatus
    content: str | None
    """파일 내용, 또는 그 byte-prefix — **전체 파일로 신뢰하기 전에 `status`를
    먼저 확인**할 것. 호출 모델에 실제로 전달되는 `read_file` 자신의 docstring
    참고."""
    returned_size: int
    total_size: int
    message: str | None


class SearchResultItemPayload(TypedDict):
    repository: str
    path: str
    excerpt: str | None


class SearchCodeResult(TypedDict):
    query: str
    repo: str
    items: list[SearchResultItemPayload]
    returned_count: int
    total_count: int
    incomplete_results: bool
    message: str | None
    """`returned_count < total_count`일 때만 설정 — cap을 설명하고 (rate-limit
    걸린) 검색 재시도를 모델이 피하도록 유도. `search_code` 자신의 docstring
    참고."""


def _repo_payload(repo: RepositorySummary) -> RepoSummaryPayload:
    return {
        "full_name": repo.full_name,
        "name": repo.name,
        "description": repo.description,
        "default_branch": repo.default_branch,
    }


def _tree_entry_payload(entry: TreeEntry) -> TreeEntryPayload:
    return {"name": entry.name, "path": entry.path, "type": entry.type, "size": entry.size}


def _search_item_payload(item: SearchResultItem) -> SearchResultItemPayload:
    return {"repository": item.repository, "path": item.path, "excerpt": item.excerpt}


# --- Tool 본문(데코레이터 없음 — 이유는 모듈 docstring 참고) -------------------


async def list_repos(ctx: Context) -> ListReposResult:
    """이 서버가 접근 가능한 GitHub 저장소 목록을 반환한다.

    이 서버의 GitHub App installation에 보이는 저장소 AND 서버에 설정된
    repository allowlist에 있는 저장소 — 둘의 교집합만 반환한다. installation
    전체 목록도, installation이 실제로 볼 수 없는 allowlist 이름도 아니다.
    `get_repo_tree`/`read_file`/`search_code`에 넘길 수 있는 `repo`
    값("owner/repo")을 알아내려면 이 tool을 먼저 호출할 것.
    """
    github, repo_allowlist = _require_lifespan(ctx)
    if not repo_allowlist:
        # TASK-044 / EDGE-001: 빈 allowlist는 설계상 모든 저장소를 거부하므로
        # (`is_repo_allowlisted`가 절대 매치 안 됨) 아래 교집합은 installation이
        # 뭘 보든 항상 비어 있다. 여기서 바로 반환해 어차피 전부 필터링될
        # GitHub 왕복을 건너뛴다 — 단순 정리 이상의 의미가 있다: 그 호출은
        # rate-limit 단위를 쓰고(EDGE-003), `MCP_REPO_ALLOWLIST`를 아직 설정
        # 안 한 신규 배포(기본값이 빈 값 — CTR-008)에선 `list_repos`를 부를
        # 때마다 낭비된다.
        #
        # 응답 형태가 "필터링해서 없음" 케이스와 동일해, 클라이언트는
        # "allowlist가 비었음"과 "installation이 allowlist 항목을 하나도 못
        # 봄"을 구분할 수 없다 — allowlist 내용을 새지 않는다는 AC-003-5와
        # 같은 방향.
        return {"repos": [], "count": 0}
    installation_repos = await github.list_installation_repositories()
    allowed = [
        repo for repo in installation_repos if is_repo_allowlisted(repo.full_name, repo_allowlist)
    ]
    return {"repos": [_repo_payload(repo) for repo in allowed], "count": len(allowed)}


async def get_repo_tree(
    repo: str, ctx: Context, path: str = "", ref: str | None = None
) -> GetRepoTreeResult:
    """저장소 안 한 경로의 파일·디렉터리 목록을 반환한다.

    `repo`는 `"owner/repo"` 형태여야 하며 `list_repos`가 반환한 저장소 중
    하나여야 한다. `path`는 기본값이 저장소 루트(`""`)라 처음부터 탐색을
    시작할 수 있고, 하위 디렉터리로 내려가려면 그 경로를 넘긴다. `ref`는
    branch/tag/commit SHA — 생략하면 저장소 기본 branch를 쓴다. 각 항목은
    `name`/`path`/`type`(`"file"` 또는 `"dir"`)/`size`(byte)를 보고한다 —
    특정 파일에 `read_file`을 부르기 전에 이걸로 먼저 탐색할 것.
    """
    github, _ = _require_lifespan(ctx)
    entries = await github.get_repo_tree(repo, path, ref)
    return {
        "repo": repo,
        "path": path,
        "ref": ref,
        "entries": [_tree_entry_payload(entry) for entry in entries],
        "count": len(entries),
    }


async def read_file(repo: str, path: str, ctx: Context, ref: str | None = None) -> ReadFileResult:
    """저장소 안 파일 하나의 텍스트 내용을 읽는다.

    `repo`는 `"owner/repo"` 형태이고 `list_repos`가 반환한 저장소 중
    하나여야 한다. `path`는 `get_repo_tree`가 보고한 파일 경로. `ref`는
    branch/tag/commit SHA — 생략하면 기본 branch를 쓴다.

    `content`를 신뢰하기 전 항상 `status`를 먼저 확인할 것:
    - `"complete"`: `content`가 파일 전체.
    - `"truncated"`: `content`는 파일의 PREFIX일 뿐 — `total_size` 중 실제로
      몇 byte가 반환됐는지는 `message`에 있다. 전체 파일로 취급하거나 절단
      지점 이후 코드에 대해 결론 내리지 말 것.
    - `"binary"`: 유효한 UTF-8 텍스트가 아님 — `content`는 `None`이고
      `total_size`는 그대로 파일 크기를 보고.
    - `"unavailable"`: 이 서버가 아예 읽을 수 없을 만큼 큼(GitHub 100MB
      content API 한도 초과) — `content`는 `None`.
    """
    github, _ = _require_lifespan(ctx)
    result: FileContent = await github.read_file(repo, path, ref)
    return {
        "repo": repo,
        "path": path,
        "ref": ref,
        "status": result.status,
        "content": result.content,
        "returned_size": result.returned_size,
        "total_size": result.total_size,
        "message": result.message,
    }


async def search_code(query: str, repo: str, ctx: Context) -> SearchCodeResult:
    """저장소 하나 안에서 쿼리와 매치하는 코드를 검색한다.

    `repo`는 `"owner/repo"` 형태이고 `list_repos`가 반환한 저장소 중
    하나여야 한다. 매치한 파일 경로와 각 매치의 짧은 발췌를 반환한다.

    중요 rate limit: GitHub 코드 검색 엔드포인트는 인증돼 있어도 분당 10
    요청뿐 — 다른 GitHub 검색 엔드포인트(분당 30)보다 훨씬 좁은 자신만의
    버킷이다. 저장소 탐색을 위해 이 tool을 반복 호출하지 말 것 — 첫 검색
    이후엔 재검색 대신 `get_repo_tree`/`read_file`로 특정 파일을 좁혀갈 것.
    결과는 서버 설정 최대치로 제한되며, 응답의 `returned_count`/
    `total_count`로 반환된 것 너머에 더 있는지 알 수 있다.
    """
    github, _ = _require_lifespan(ctx)
    results: SearchResults = await github.search_code(query, repo)
    returned_count = len(results.items)
    message: str | None = None
    if returned_count < results.total_count:
        message = (
            f"Showing {returned_count} of {results.total_count} total matches "
            "(server-side cap). Narrow your query, or switch to get_repo_tree/"
            "read_file instead of repeating this search — code search is rate "
            "limited to 10 requests/minute."
        )
    return {
        "query": query,
        "repo": repo,
        "items": [_search_item_payload(item) for item in results.items],
        "returned_count": returned_count,
        "total_count": results.total_count,
        "incomplete_results": results.incomplete_results,
        "message": message,
    }


def register(mcp: MCPServer, guard: Guard) -> None:
    """core GitHub tool 4개(`DSN-005`)를 `guard`로 감싸 등록한다.

    `guard(...)`는 여기, 등록 시점에 적용한다 — 위 각 `def` 위에 바로
    `@guard(...)`를 붙이지 않는 이유는 모듈 docstring 참고. `name=`을
    `mcp.add_tool`에 명시적으로 넘기는 이유(우연히 같은 값이 될 `fn.__name__`에
    기대지 않고)는, SDK에 노출되는 tool 이름과 `guard`의 `tool` 인자(`CTR-007`
    RBAC·`CTR-003` 감사 레코드에 쓰임)가 `types.py`의 `TOOL_*` 상수에서 절대
    벗어나지 않게 하기 위함 — tool 함수 이름이 나중에 바뀌어도 안전하다.
    """
    mcp.add_tool(guard(TOOL_LIST_REPOS)(list_repos), name=TOOL_LIST_REPOS)
    mcp.add_tool(
        guard(TOOL_GET_REPO_TREE, repo_arg="repo", audit_args=("repo", "path", "ref"))(
            get_repo_tree
        ),
        name=TOOL_GET_REPO_TREE,
    )
    mcp.add_tool(
        guard(TOOL_READ_FILE, repo_arg="repo", audit_args=("repo", "path", "ref"))(read_file),
        name=TOOL_READ_FILE,
    )
    mcp.add_tool(
        guard(TOOL_SEARCH_CODE, repo_arg="repo", audit_args=("repo", "query"))(search_code),
        name=TOOL_SEARCH_CODE,
    )
