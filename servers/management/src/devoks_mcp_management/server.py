"""Composition root: 인증·RBAC·감사를 연결한 ``MCPServer``를 만든다 (TASK-008).

``create_server(settings) -> MCPServer``는 **팩토리**다 — 모듈 스코프에 인스턴스를
두지 않아, import만으로 환경변수를 읽거나 ``ConfigError``가 나는 일이 없다.

``token_verifier=``와 ``auth=AuthSettings(...)``는 항상 함께 넘긴다(FRD §7). 설치된
``mcp==2.1.1`` 소스 확인 — 둘 중 하나만 주면 생성 시점에 ``ValueError``. 이 모듈엔
둘을 따로 줄 경로가 없어 구조적으로 발생 불가능하다.

다음 조립 계층은 ``app.py``(TASK-009) — 여기서 만든 ``MCPServer``를 Starlette 앱에
마운트한다. ASGI 관련 코드는 이 모듈에 두지 않는다.

``lifespan=`` (TASK-023, DSN-004) — ASGI가 아니라 *MCP 프로토콜* lifespan
--------------------------------------------------------------------------
``lifespan=``은 ``MCPServer(...)``로 그대로 전달만 한다. 이 모듈은 직접 만들지도
``adapters/*``를 import하지도 않는다 — GitHub 자격증명/HTTP 클라이언트 수명은
``app.py``가 소유하며, 이 모듈의 책임(§7 인증·RBAC guard·툴 등록)과는 무관하다.
오버로드 2개로 기존 호출부(``lifespan`` 미지정)의 타입 ``MCPServer[None]``을
그대로 유지한다. 계약 근거:
<https://py.sdk.modelcontextprotocol.io/handlers/lifespan/index.md>.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Final, overload

from mcp.server import MCPServer
from mcp.server.auth.settings import AuthSettings

from devoks_mcp_management.auth.verifier import StaticTableTokenVerifier
from devoks_mcp_management.config import Settings
from devoks_mcp_management.tools.guard import make_tool_guard
from devoks_mcp_management.tools.registry import Guard, register_tools

#: CTR-002 / FRD §5.1이 Stage 1의 scope를 ``["devoks:read"]``로 고정. ``Settings``가
#: 아니라 모듈 상수로 둔 이유 — 배포별 옵션이 아니라 계약 자체이기 때문. 환경변수로
#: 오버라이드 가능하게 하면 스펙 변경 없이 AC-002-5/EDGE-010의 의미가 조용히 바뀐다.
REQUIRED_SCOPES: Final[list[str]] = ["devoks:read"]

#: MCP 클라이언트에 노출되는 서버 이름(``MCPServer.name``). ``Settings``에서
#: 끌어오지 않음 — 와이어상 서버 정체성은 ``public_url`` 같은 배포 설정과 달리
#: 코드 레벨 상수다.
SERVER_NAME: Final = "devoks-management-mcp"


@overload
def create_server(settings: Settings) -> MCPServer[None]: ...


#: ``[LifespanResultT]``(PEP 695, 이 프로젝트의 Python 3.14 하한)는 MCP 프로토콜
#: lifespan이 yield하는 컨텍스트 타입 — 호출부가 넘긴 ``lifespan=``에 따라 호출별로
#: 해석된다(예: ``app.py``의 ``GitHubLifespanContext``). ``lifespan=``을 생략하면
#: 위 오버로드로 ``None``이 되어 SDK 기본 lifespan(``mcp.server.lowlevel.server.lifespan``,
#: 아무것도 yield하지 않음)과 일치한다.
@overload
def create_server[LifespanResultT](
    settings: Settings,
    *,
    lifespan: Callable[[MCPServer[LifespanResultT]], AbstractAsyncContextManager[LifespanResultT]],
) -> MCPServer[LifespanResultT]: ...


def create_server(
    settings: Settings,
    *,
    lifespan: Callable[[MCPServer[Any]], AbstractAsyncContextManager[Any]] | None = None,
) -> MCPServer[Any]:
    """``settings``로부터 ``MCPServer`` 인스턴스 하나를 만든다.

    - ``lifespan``: ``MCPServer(..., lifespan=...)``로 그대로 전달(TASK-023,
      DSN-004) — 생략 시 첫 오버로드에 의해 ``MCPServer[None]``이 된다.
    - ``token_verifier``: ``settings.client_tokens``에 바인딩된
      ``StaticTableTokenVerifier``(DSN-001) — SDK auth 미들웨어가 통과시킨 모든
      요청의 Bearer 토큰을 ``AccessToken``으로 해석한다.
    - ``auth``: ``resource_server_url``/``issuer_url``이 각각
      ``settings.public_url``/``settings.issuer_url``과 일치(AC-002-4 전제 —
      RFC 9728 메타데이터의 ``resource``가 설정된 public URL과 정확히 같아야 함)하고,
      ``required_scopes``는 위 CTR-002 scope 목록(AC-002-5, EDGE-010).

      ``issuer_url``/``resource_server_url``은 ``config.load_settings``가 이미
      검증한 **순수 문자열 그대로** 넘긴다 — ``pydantic.AnyHttpUrl(...)``로 미리
      감싸지 않는다. 설치된 ``mcp==2.1.1``(``AuthSettings.model_fields``) 확인:
      ``url_preserve_empty_path=True`` 설정은 pydantic이 **raw string**을 검증할
      때만 적용된다. 이미 만들어진 ``AnyHttpUrl``을 넘기면(과거 초안이 그랬음) 이
      재검증을 건너뛰고 ``AnyHttpUrl(...)``이 이미 적용한 정규화 — path 없는 URL에
      무조건 trailing ``/`` 추가 — 가 그대로 남는다. 흔한 케이스인 path 없는
      ``settings.public_url``이 없던 trailing slash를 달고 나와 AC-002-4의 정확
      일치 전제가 깨진다. 문자열을 그대로 넘기면 ``AuthSettings``가 직접 검증해
      정규형(trailing slash 없음)을 유지한다.
    - ``tools.guard.make_tool_guard`` 인스턴스를 같은 ``settings``로 여기서 한 번
      만들어 ``tools.registry.register_tools``에 넘긴다 — 현재는 no-op(DSN-005,
      아직 어댑터 없음. TASK-022 hook-in 지점은 그 모듈 docstring 참고).
    """
    token_verifier = StaticTableTokenVerifier.from_settings(settings)
    auth = AuthSettings(
        # str -> AnyHttpUrl 변환은 pydantic 런타임 검증에서 일어나지만(위 참고),
        # pyright가 생성한 `AuthSettings.__init__` 스텁은 변환 후 타입으로
        # 잡혀 있어 여기 `str`을 넘기는 건 pyright 입장에서 정상적인 타입
        # 불일치다 — 다르게 우회할 실수가 아니다.
        issuer_url=settings.issuer_url,  # pyright: ignore[reportArgumentType]
        resource_server_url=settings.public_url,  # pyright: ignore[reportArgumentType]
        required_scopes=REQUIRED_SCOPES,
    )
    mcp: MCPServer[Any] = MCPServer(
        SERVER_NAME,
        token_verifier=token_verifier,
        auth=auth,
        lifespan=lifespan,
    )

    # `Guard`로 명시 타입 지정: `make_tool_guard`의 반환 타입
    # (`Callable[..., Callable[[F], F]]`)에 있는 TypeVar는 실제 툴 함수를
    # 데코레이트할 때만 풀리는데 이 모듈에선 그 일이 없다 — 명시 타입이 없으면
    # pyright가 unresolved TypeVar를 unknown으로 보고한다.
    guard: Guard = make_tool_guard(settings)
    register_tools(mcp, guard)

    return mcp
