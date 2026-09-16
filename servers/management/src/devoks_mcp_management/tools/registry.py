"""Tool 레지스트리 — 계층 어댑터들의 tool을 모으는 단일 수집점(DSN-005, TASK-008).

`register_tools`만이 어떤 어댑터가 존재하는지 안다. 다른 모듈(`server.py`,
어댑터 자신의 `tools.py`)은 그 목록을 몰라도 된다. 새 소스(Notion, Sentry,
두 번째 GitHub org 등)를 추가하는 비용은 디렉토리 1개 + 아래 튜플에 한
줄로 고정돼 있다:

1. `adapters/<layer>/<source>/tools.py`에 `register(mcp: MCPServer, guard:
   Guard) -> None`을 구현한다 — 자신의 async tool 함수를 `guard(...)`로
   감싸(`@mcp.tool()` 또는 `mcp.add_tool(...)`) `mcp`에 등록한다.
2. 그 함수를 `_ADAPTER_REGISTRARS` 위에서 import해 튜플에 추가한다::

       from devoks_mcp_management.adapters.knowledge.github.tools import (
           register as register_github_tools,
       )

       _ADAPTER_REGISTRARS: tuple[Registrar, ...] = (register_github_tools,)

Stage 1은 어댑터 **0개**로 출발한다 — `adapters/knowledge/github/tools.py`는
TASK-022 소관(TASK-020/021 의존, 이 태스크 범위 밖)이다. 그래서
`_ADAPTER_REGISTRARS`는 빈 튜플로 시작하고 `register_tools`는 빈 튜플에
대해 의도적 no-op — 서버는 빈 tool 표면으로 기동하고 `tools/list`는
`[]`를 반환한다(AC-001-2). 이 모듈은 그 공백을 메우려고
placeholder/dummy tool을 절대 등록하지 않는다 — 가짜 tool이 이 서버가
보호하려는 실제 감사 대상 tool 표면에 섞여들기 때문이다(근거는 TASK-008
handover 노트 참고).

의존성 타이밍(TASK-022/023용, DSN-004 / DSN-005)
---------------------------------------------------
tool은 서버 구성 시점(`create_server`, 프로세스 기동 시 1회)에 여기서
*등록*되지만, TASK-022의 tool이 필요로 하는 GitHub client는 그 시점엔
아직 없다 — DSN-004가 그 생성을 ASGI 앱의 `lifespan`(TASK-023,
`app.py`)에 두었고, 이는 서버가 요청을 받기 시작해야 실행된다. 등록과
의존성 생성은 서로 다른 시점이므로, registrar 함수가 import/등록
과정에서 만들어진 client 인스턴스를 클로저로 가두려 하면 안 된다.

SDK가 제시하는 답은 `Context` 파라미터다(`mcp.server.mcpserver.context.
Context`,
<https://py.sdk.modelcontextprotocol.io/handlers/lifespan/index.md>) —
tool 함수에 `ctx: Context` 파라미터를 추가하면 호출마다 주입되고,
`ctx.request_context.lifespan_context`가 바로 `lifespan`이 기동 시
yield한 값이다. TASK-022/023이 따를 예상 형태::

    # app.py(TASK-023)가 @asynccontextmanager lifespan 함수(MCPServer(...,
    # lifespan=...)에 전달) 안에서 client를 한 번 만들어 lifespan context로
    # yield한다(값이 여럿이면 dataclass/TypedDict).

    # adapters/knowledge/github/tools.py(TASK-022)
    @guard("read_file", repo_arg="repo", audit_args=("repo", "path", "ref"))
    async def read_file(repo: str, path: str, ctx: Context, ref: str | None = None) -> str:
        client: GitHubClient = ctx.request_context.lifespan_context.github_client
        ...

    def register(mcp: MCPServer, guard: Guard) -> None:
        mcp.add_tool(read_file)
        ...

이 레지스트리는 그 `Context` 배선을 전혀 몰라도 된다 — `mcp`/`guard`를
그대로 각 registrar에 전달할 뿐이므로, TASK-023이 lifespan을 배선해도 이
파일은 바뀔 필요가 없다.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from devoks_mcp_management.adapters.knowledge.github.tools import register as register_github_tools

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

#: `tools.guard.make_tool_guard(settings)`가 반환하는 데코레이터 팩토리 —
#: `guard(tool, *, repo_arg=None, audit_args=())`를 tool 함수에 적용한다.
#: registrar는 이미 만들어진 것을 전달받는다(서버 인스턴스당 1개) — 자체
#: 생성하지 않으므로, 등록된 모든 tool이 하나의 `Settings`/감사 sink를
#: 공유한다.
#:
#: `guard.py`는 이를 더 정밀하게 async-tool `F`에 대한
#: `Callable[..., Callable[[F], F]]`로 타이핑하지만, 그 TypeVar는 실제 tool
#: 함수를 데코레이트하는 지점(어댑터 자신의 `tools.py`)에서만 풀린다 —
#: 여기서는 callable을 그대로 전달만 할 뿐 적용하지 않는다. 안쪽
#: `Callable`을 아무 데도 안 묶인 로컬 TypeVar 대신 구체적인 `Any`
#: 경계로 적는 것이 이 alias를 non-generic으로 유지해 아래 평범한 함수
#: 시그니처에서 쓸 수 있게 한다.
Guard = Callable[..., Callable[[Callable[..., Any]], Callable[..., Any]]]

#: 어댑터당 registrar 1개: `(mcp, guard) -> None`. `mcp`에 자신의 tool을
#: 등록하고(보통 `@guard(...)` 후 `mcp.add_tool`) 아무것도 반환하지 않는다.
Registrar = Callable[["MCPServer", Guard], None]

#: DSN-005 수집점. TASK-022가 여기에 어떻게 추가하는지는 모듈 docstring
#: 참고.
_ADAPTER_REGISTRARS: tuple[Registrar, ...] = (register_github_tools,)


def register_tools(mcp: MCPServer, guard: Guard) -> None:
    """모든 계층 어댑터의 tool을 `mcp`에 등록한다.

    `server.create_server`가 해당 서버 인스턴스의 `guard`를 만든 뒤 1회
    호출한다. `_ADAPTER_REGISTRARS`가 비어 있으면(Stage 1의 현재 상태,
    AC-001-2) no-op — 예외도 없고 placeholder tool도 없다.
    """
    for register in _ADAPTER_REGISTRARS:
        register(mcp, guard)
