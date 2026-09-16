"""ASGI 조립 루트: Starlette(`/healthz` + `Mount /mcp`) + lifespan (TASK-009).

`create_app(settings) -> Starlette`는 **팩토리**다 — 모듈 스코프에 `Settings`나
`Starlette` 앱을 만들지 않으므로, import만으로는 환경변수를 읽거나 `ConfigError`가
나지 않는다. 실제 진입점은 `create_app_from_env`(uvicorn/컨테이너가 호출) 하나뿐이다.

라우트 조립(CTR-001) — `routes=` 건드리기 전에 읽을 것
--------------------------------------------------------
`mcp.streamable_http_app()`이 반환하는 앱은 자체 라우트 `/mcp` 외에, `token_verifier=`·
`auth=`를 함께 주면 SDK가 RFC 9728 Protected Resource Metadata 라우트도 추가한다
(`mcp==2.1.1` 실측 확인). 그 경로가 `/.well-known/oauth-protected-resource/mcp`로
정확히 떨어지려면 `MCP_PUBLIC_URL`에 `/mcp` path가 trailing slash 없이 있어야 한다
(세 가지 형태 전부 실측 검증 — `config.py` 참고).

이 때문에 MCP 서브앱을 `/mcp`가 아니라 **루트(`Mount("/", ...)`)에** 마운트한다 —
`/mcp`에 마운트하면 서브앱 자체 경로와 겹쳐 `/mcp/mcp`가 된다(SDK 공식 문서에
명시된 함정). `Mount("/")`는 모든 경로를 먼저 잡아먹으므로 **`/healthz`는 반드시
그 앞에 나열**한다.

Lifespan(DSN-004) — 서로 다른 lifespan 2개가 얽혀 있다
--------------------------------------------------------
1. **ASGI/transport lifespan** — 이 모듈의 `lifespan()`. `mcp.session_manager.run()`을
   연다. 마운트된 서브앱의 자체 lifespan은 Starlette가 호출하지 않으므로, 호스트 앱인
   여기서 명시적으로 열어야 한다 — 생략하면 `/mcp` 첫 요청이
   `RuntimeError: Task group is not initialized`로 죽는다.
2. **MCP 프로토콜 lifespan** — `create_server(settings, lifespan=...)`로 전달한
   `_github_lifespan`. `mcp.session_manager.run()`을 여는 순간 이것도 같은
   `async with` 안에서 함께 열린다(설치된 `mcp==2.1.1` 소스로 확인) — 별도
   `AsyncExitStack`을 두 lifespan에 걸쳐 공유할 필요가 없다.

`_make_github_lifespan`이 실제로 자원을 만드는 순서(모두 `AsyncExitStack`으로 등록,
역순 정리): ① 공유 `httpx2.AsyncClient` 1개(DSN-004) → ② `InstallationTokenProvider`
→ ③ `GitHubClient`. GitHub 호출은 어디서도 즉시 일어나지 않는다(토큰 발급은 지연
평가) — 그래서 GitHub 장애가 서버 기동 실패로 번지지 않는다.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version
from typing import Final

import httpx2
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from devoks_mcp_management.adapters.knowledge.github.client import GitHubClient
from devoks_mcp_management.adapters.knowledge.github.credentials import InstallationTokenProvider
from devoks_mcp_management.config import Settings, load_settings
from devoks_mcp_management.server import SERVER_NAME, create_server

#: `servers/management/pyproject.toml`의 `[project].name`과 일치해야 한다 —
#: `importlib.metadata`가 조회하는 건 배포판 이름이지 임포트 패키지 이름이
#: 아니다(여기선 우연히 철자가 같음).
_DISTRIBUTION_NAME = "devoks_mcp_management"

#: 이 lifespan이 만드는 공유 `httpx2.AsyncClient` 하나의 네트워크
#: 타임아웃(DSN-004) — connect/read/write/pool 전부에 동일하게 적용된다
#: (`httpx2.AsyncClient(timeout=<float>)`는 하나의 값으로 네 개를 다 묶는다).
#: `httpx2.AsyncClient()` 기본값은 `Timeout(timeout=5.0)`(설치된
#: `httpx2>=2.5.0` 패키지로 확인) — 페이지가 많은
#: `list_installation_repositories`나 `EDGE-012` raw-media-type 폴백의
#: 대용량 파일 읽기 같은 평범한 GitHub 지연에도 툴 호출이 헛되이 실패할 만큼
#: 빠듯하다. 타임아웃을 아예 안 두는 것도 답이 아니다 — 이 프로세스는 ECS
#: task라 멈춘 GitHub 커넥션 하나가 task 전체 요청 처리를 무한정 막을 수
#: 있다. 30초는 명시적 중간값 — 재시도 계층 없이 현실적인 GitHub 지연을
#: 흡수할 만큼 넉넉하고, 진짜로 멈춘 커넥션은 요청 수명 안에서 평범한
#: `ToolError`로(`client.py`의 `httpx2.HTTPError` 처리 경유) 드러날 만큼 짧다.
# EDGE-018: 앞단 레이어의 한도보다 반드시 낮게 유지 — 이 서버가 항상 먼저
# 타임아웃해야 한다. API Gateway HTTP API의 integration timeout은 30초가
# 하드 맥스(50~30,000ms 설정 가능, 그 이상 불가)라 여기를 30.0으로 같게
# 두면 여유가 0이 된다 — 느린 GitHub 응답이 이 서버의 에러
# 정규화(EDGE-003 rate-limit 힌트 / EDGE-009 tool-error 가공)를 거치지
# 못한 채 API Gateway 504로 나가 감사 레코드의 `error_kind`까지 유실된다.
_GITHUB_HTTP_TIMEOUT_SECONDS: Final = 20.0


@dataclass(frozen=True, slots=True)
class GitHubLifespanContext:
    """이 앱이 yield하는 MCP 프로토콜 lifespan 값(DSN-004, AC-006-1).

    `adapters.knowledge.github.tools.GitHubToolContext`를 구조적으로
    만족한다 — 그 모듈의 `_require_lifespan`이 호출 시점에 속성 이름·타입을
    `isinstance`로 다시 검증하므로(이유는 그 docstring 참고) 이 dataclass가
    그 `Protocol`을 상속할 필요는 없다 — 속성 이름·타입 두 개가 정확히
    일치하면 된다. 다른 1회성 생성 값(`config.Settings`, `client.py` 반환
    타입 등)과 같은 이 프로젝트 관례대로 frozen.
    """

    github: GitHubClient
    repo_allowlist: frozenset[str]


#: `MCPServer(..., lifespan=...)`(그리고 그대로 전달되는
#: `server.create_server(..., lifespan=...)`)가 요구하는 정확한 타입 —
#: `_make_github_lifespan` 반환 타입이 이 프로젝트의 line-length 제한을
#: 넘지 않도록 이름을 붙였다.
_GitHubLifespan = Callable[
    [MCPServer[GitHubLifespanContext]], AbstractAsyncContextManager[GitHubLifespanContext]
]


def _make_github_lifespan(settings: Settings) -> _GitHubLifespan:
    """`create_server`용 MCP 프로토콜 `lifespan=` 값을 만든다(DSN-004).

    이미 진입한 context manager가 아니라 `settings`를 클로저로 감싼 새
    async-context-manager 팩토리를 반환한다 — `MCPServer(..., lifespan=...)`는
    `mcp.session_manager.run()`이 시작될 때 스스로 호출할 *callable*이
    필요하기 때문(정확한 시점은 모듈 docstring "Lifespan" 절 참고).

    생성 순서와 정리(진입 후)
    --------------------------
    1. `httpx2.AsyncClient` 1개(`_GITHUB_HTTP_TIMEOUT_SECONDS`) — DSN-004가
       요구하는, token provider와 GitHub REST client가 공유하는 단일
       인스턴스. `aclose()`를 다른 무엇보다 먼저 exit stack에 등록한다.
    2. `InstallationTokenProvider.from_settings(settings, http_client=...)`
       — GitHub를 직접 호출하지 않음(토큰 발급은 지연 평가, 모듈 docstring
       참고). `aclose()`(자신의 캐시된 토큰 상태만 버리고 주입된 client는
       건드리지 않음 — 해당 모듈 docstring 참고)를 바로 이어서 등록한다.
    3. `GitHubClient.from_settings(settings, http_client=..., token_provider=...)`
       — 자체 `aclose()` 없음(공유 client와 provider 외에 소유한 자원이 없음,
       둘 다 위에서 이미 커버됨).

    `AsyncExitStack`은 종료 시 *또는* 이 시퀀스 도중 예외가 나도 등록 역순으로
    해제한다 — 그래서 `provider.aclose()`는 항상 `http_client.aclose()`보다
    먼저 실행되고(TASK-023 handover 노트가 요구하는 순서), 1단계 이후
    실패해도 그 단계가 이미 연 client는 누수 없이 닫힌다.
    """

    @asynccontextmanager
    async def _github_lifespan(
        _: MCPServer[GitHubLifespanContext],
    ) -> AsyncGenerator[GitHubLifespanContext]:
        async with AsyncExitStack() as stack:
            http_client = httpx2.AsyncClient(timeout=_GITHUB_HTTP_TIMEOUT_SECONDS)
            stack.push_async_callback(http_client.aclose)

            provider = InstallationTokenProvider.from_settings(settings, http_client=http_client)
            stack.push_async_callback(provider.aclose)

            github = GitHubClient.from_settings(
                settings, http_client=http_client, token_provider=provider
            )

            yield GitHubLifespanContext(github=github, repo_allowlist=settings.repo_allowlist)

    return _github_lifespan


def _server_version() -> str:
    """`/healthz` 응답 본문용 패키지 버전(AC-001-3).

    하드코딩 리터럴이 아니라 설치된 패키지 메타데이터에서 가져와 둘이
    드리프트할 일이 없다. `PackageNotFoundError` 폴백은 방어용일 뿐 — 지원되는
    모든 실행 경로(로컬 `uv sync`, 컨테이너 이미지)가 메타데이터와 함께 이
    배포판을 설치하지만, 배포판이 없다고 해서 공개·비인증 health check가
    500이 되어서는 안 된다.
    """
    try:
        return _package_version(_DISTRIBUTION_NAME)
    except PackageNotFoundError:
        return "0.0.0+unknown"


def _expand_allowed_hosts(hosts: tuple[str, ...]) -> list[str]:
    """`Settings.allowed_hosts`로부터 `TransportSecuritySettings.allowed_hosts`를 만든다.

    `allowed_hosts` 항목은 요청 `Host` 헤더와 완전 문자열 일치로 매칭된다
    (설치된 `mcp==2.1.1`의 `TransportSecurityMiddleware._validate_host`
    소스로 확인) — 맨 `"mcp.example.com"`은 포트 없는 `Host` 헤더만,
    `"mcp.example.com:*"`는 포트 있는 헤더만 매칭한다. 실제 `Host` 헤더의
    포트 유무는 앞단 구성에 달렸다(로컬 `uvicorn`은 거의 항상 포함, 표준
    443/80 리스너 앞 ALB는 보통 없음) — SDK 배포 가이드도 이 이유로 두
    형태를 나란히 제시한다. 이 중복을 `MCP_ALLOWED_HOSTS`(DSN-007: env로
    주입되는 리스트라 중복 항목마다 맞추고 환경 간 동기화할 거리가 늘어남)
    값마다 떠넘기는 대신, 이미 포트를 명시한(`:` 포함) 경우가 아니면 여기서
    설정된 host마다 두 형태로 확장한다 — 운영자가 의도적으로 고정 포트를
    박았다면 그 포트만 매칭되길 원한 것이므로 제외한다.
    """
    expanded: list[str] = []
    for host in hosts:
        expanded.append(host)
        if ":" not in host:
            expanded.append(f"{host}:*")
    return expanded


async def _healthz(request: Request) -> JSONResponse:
    """GET /healthz (CTR-001, AC-001-3) — 설계상 비인증.

    ALB/오케스트레이터 health check는 Bearer 토큰을 싣지 않으므로 이
    라우트는 MCP 서브앱 `Mount` 바깥, 바깥쪽 Starlette 앱에 직접 둔다 —
    `TransportSecurity`나 `TokenVerifier`를 전혀 거치지 않는다. 응답 본문은
    의도적으로 최소(name + version만) — 공개 엔드포인트이므로 `Settings`나
    다른 설정값을 절대 되돌려주지 않는다.
    """
    return JSONResponse({"name": SERVER_NAME, "version": _server_version()})


def create_app(settings: Settings) -> Starlette:
    """`settings`로부터 CTR-001의 세 라우트를 갖춘 Starlette 앱 하나를 만든다.

    모듈 스코프 싱글턴이 아니라 팩토리 — 모듈 docstring 참고. 프로세스당 한
    번(테스트에선 `Settings`당 한 번) 호출한다 — 두 번 호출해도 안전/위험
    여부는 없고 매번 독립된 객체를 만들 뿐이다(`server.create_server`와 같은
    보장).
    """
    mcp = create_server(settings, lifespan=_make_github_lifespan(settings))

    # root logger *threshold*만 바꾼다 — 두 번째 logging.basicConfig(...)가
    # 아니다: create_server() -> MCPServer.__init__()이 이미
    # mcp.server.mcpserver.utilities.logging.configure_logging("INFO")를
    # 호출했다(SDK 내부 하드코딩 기본값 — server.py엔 오버라이드 파라미터가
    # 없음). 이게 logging.basicConfig(...)를 호출해 root logger 핸들러를
    # 설치한다. basicConfig()는 핸들러가 아직 없는 root logger에만 효과가
    # 있어 여기서 다시 불러도 조용히 아무 일도 안 한다. setLevel()은 누가
    # 핸들러를 설치했든 threshold를 바꾸므로, 이게 실제로 MCP_LOG_LEVEL이
    # 서버 로그 verbosity(예: transport security 미들웨어의 Host/Origin
    # 거부 경고, AC-001-4)에 영향을 주는 방식이다. 감사 로깅(audit/logger.py는
    # logging 모듈을 완전히 우회해 JSON 줄을 stdout에 직접 씀)과는
    # 의도적으로 분리돼 있다 — 운영자가 MCP_LOG_LEVEL을 올려도 감사 레코드는
    # 절대 필터되지 않고, 이 서버 로그 스트림만 영향받는다.
    logging.getLogger().setLevel(settings.log_level)

    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_expand_allowed_hosts(settings.allowed_hosts),
        # EDGE-011 / DSN-007: CTR-006(FRD §5.2)은 브라우저 대상 Origin
        # allowlist용 env 키를 정의하지 않고, 이 태스크는 새 키를 추가할
        # 권한이 없다(새 env 키는 FRD 계약 변경 — 메인 루프가 결정할 사항은
        # handover 노트 참고). 그동안은 빈 리스트가 안전한 기본값이다:
        # TransportSecurityMiddleware는 Origin 헤더가 있을 때만 검사하므로
        # (동일 출처 요청과 브라우저가 아닌 모든 MCP 클라이언트는 Origin을
        # 보내지 않음,
        # mcp.server.transport_security.TransportSecurityMiddleware._validate_origin
        # 기준) 아무도 설정하지 않은 allowlist를 추측하는 대신 브라우저
        # 기반 호출을 그냥 차단한다 — 보수적인 방향. FRD §2 맥락상 Stage
        # 1엔 브라우저 클라이언트가 없다.
        allowed_origins=[],
    )

    # CTR-011 / FRD §7 "배포 타깃 제약": 두 플래그 모두 settings에서 오므로
    # 같은 이미지가 Lambda(둘 다 True — 기본값)와, 변경 없이 sticky-session
    # 로드밸런서 뒤(둘 다 False)에서 각각 돌아간다.
    #
    # `stateless_http=True`라도 아래 `lifespan()`이 필요 없어지지 않는다.
    # 설치된 mcp==2.1.1 소스로 확인: MCP 프로토콜 lifespan(위에서
    # `create_server`에 넘긴 `_make_github_lifespan`)을 여는 건
    # `StreamableHTTPSessionManager.run()`이고, `_handle_stateless_request`가
    # 요청마다 여는 transport의 anyio task group도 이게 만든다. stateless
    # 모드는 매니저가 `_server_instances`/`_session_owners`를 추적하지
    # 않게만 할 뿐 `run()`을 선택적으로 만들지 않는다. `run()`은 인스턴스당
    # 두 번 호출되면 RuntimeError도 던진다 — 그래서 앱은 프로세스당 한 번만
    # 만들어야 하고(uvicorn은 컨테이너당 한 번 기동), Lambda invocation마다
    # 만들면 안 된다.
    mcp_app = mcp.streamable_http_app(
        transport_security=security,
        stateless_http=settings.stateless_http,
        json_response=settings.json_response,
    )

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncGenerator[None]:
        """앱 수명 동안 StreamableHTTP session manager를 열고 닫는다.

        MCP 앱을 독립 실행이 아니라 *마운트*했을 때 왜 이 한 줄이 필요한지,
        이걸 여는 게 왜 위에서 `create_server`에 준 MCP 프로토콜 lifespan
        (`_make_github_lifespan`)도 함께 여는지는 모듈 docstring("Lifespan
        (DSN-004)") 참고 — TASK-023을 위해 이 함수에서 더 할 일은 없다.
        """
        async with mcp.session_manager.run():
            yield

    return Starlette(
        routes=[
            # 아래 Mount("/", ...)보다 반드시 앞에 와야 한다 — Starlette는
            # 리스트 순서대로 라우트를 매칭하고 Mount("/")는 모든 경로를
            # 잡아먹어서, 그 뒤에 나열된 건 도달 불가능해진다.
            Route("/healthz", endpoint=_healthz, methods=["GET"]),
            Mount("/", app=mcp_app),
        ],
        lifespan=lifespan,
    )


def create_app_from_env(env: Mapping[str, str] | None = None) -> Starlette:
    """프로세스 진입점: `uvicorn devoks_mcp_management.app:create_app_from_env --factory`.

    지연 평가되는 모듈 레벨 `app = create_app(...)` 속성 대신 이 방식을 택해
    **이 모듈을 import하는 것만으로는 환경을 읽거나 `ConfigError`가 나지
    않는다** — 이 함수를 호출해야만 그렇다. TASK-030(Dockerfile `CMD`),
    TASK-031(CI health-check smoke test), TASK-032(README 실행 안내)가
    호출하도록 기대되는 바로 그 진입점이다. `env`는 기본값이 `os.environ`이고,
    호출부(미래의 `__main__`, 테스트)가 프로세스 상태를 바꾸지 않고 다른
    매핑을 주입할 수 있도록만 존재한다.
    """
    return create_app(load_settings(env if env is not None else os.environ))
