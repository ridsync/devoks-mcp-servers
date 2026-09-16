"""툴 가드 데코레이터(TASK-007, DSN-003, FRD §4.1 ③단계).

FRD 데이터 흐름도의 "③ 툴 래퍼" 박스 — SDK 인증 계층(이미 ``AccessToken``을
해석했거나 401 반환)과 각 tool 본문 사이에서 세 가지를 한곳에 강제한다:

1. 본문 실행 *전에* ``auth.policy.authorize`` 호출 — 거부 시 본문에 닿지
   않으므로 GitHub를 절대 건드리지 않는다(AC-003-2, AC-003-3).
2. ``ok``/``denied``(AC-004-2)/``error``(AC-004-4) 모든 결과에 대해
   ``CTR-003`` 감사 레코드를 정확히 1건 emit — ``try/except/else/finally``로
   감싸 본문 예외가 emit을 건너뛸 수 없게 한다.
3. 예외 정규화 — 의도적 ``ToolError``/``ResourceError``/``mcp.MCPError``는
   그대로 통과(SDK 계약상 client-safe, TASK-021/022의 GitHub 에러 정규화가
   자신의 메시지가 그대로 도달함에 의존). 그 외는 예기치 않은 크래시
   (EDGE-009) — 트레이스백은 서버 로그에만, 감사엔 ``error_kind``(예외
   클래스명)만, 클라이언트엔 원문 없이 request_id 포함 일반 메시지만.

**팩토리(``make_tool_guard``)인 이유**: ``Settings``와 emit sink가
필요하고 ``ts``/``duration_ms``/``request_id`` 같은 환경값도 테스트가
결정론적으로 검증하려면 주입 가능해야 한다.

    guard = make_tool_guard(settings)

    @guard("read_file", repo_arg="repo", audit_args=("repo", "path", "ref"))
    async def read_file(repo: str, path: str, ref: str | None = None) -> str:
        ...

``clock`` 기본값이 ``time.time``이 아니라 ``time.perf_counter``(모노토닉)
인 이유는 NTP 보정으로 ``duration_ms``가 음수가 되는 걸 막기 위함.

**``repo_arg``/``audit_args``를 추론이 아니라 이름으로 받는 이유**: 이름으로
"이게 repo다"를 추측하면 파라미터 리네임 시 조용히 어긋나고, 잘못된 추측은
allowlist 체크를 건너뛴 채 승인해버려 위험하다. ``repo_arg`` 미지정 tool은
``repo=None``으로 인가된다(``authorize()``의 repo-less tool 계약과 동일).
같은 이유로 kwargs 전부를 ``args_summary``에 덤프하지 않고 ``audit_args``로
명시한 이름만 문자열화한다(미래에 tool이 ``content`` 인자를 추가해도
기본으로 새지 않도록). CTR-003이 요구하는 식별용 인자(repo, path, ref,
query)만 남기고, 파일 본문·tool 반환값은 절대 포함하지 않는다(반환값은
``else`` 분기의 ``return result``에서만 다룬다).

**identity ``None``이 fail-safe *거부*인 이유**: ``get_access_token()``은
HTTP bearer-auth 미들웨어를 거치지 않은 요청(stdio, 인메모리
``Client(mcp)`` 테스트 전송, FRD §7)에서 ``None``을 반환한다. Streamable
HTTP는 이미 미들웨어에서 401 처리하므로, 여기 ``None``이 왔다는 건 인증
계층 자체를 거치지 않았다는 뜻 — "role 미상, 거부"로 처리해야 이 서버가
stdio로 돌아가도 RBAC가 조용히 무력화되지 않는다. 감사 레코드의
``client_id``/``role``은 진짜 ``AccessToken``이 없으므로 둘 다
``"anonymous"``로 남긴다(``config.py``가 빈 role/토큰을 거부하므로 원래
불가능한 빈 값 ``""``과 혼동될 일이 없는 리터럴). ``reason_code``는
``auth.policy``의 ``"role_unknown"``이 아니라 더 구체적인 ``"no_identity"``
를 쓴다(구현은 빈 문자열 role로 ``authorize()``를 호출해 ``role_unknown``
분기의 거부 문자열만 재사용하는 것뿐이지만, 감사 쪽 reason은 운영상 의미가
다른 신호다) — 운영자가 로그에서 "왜 HTTP 인증 없이 tool에 도달했지?"를
물어야지 "왜 이 role에 이 tool이 없지?"를 묻게 하면 안 되기 때문.

**TASK-024(GitHub tool 인메모리 통합 테스트) 영향**: ``Client(mcp)``는
HTTP 인증을 우회하므로, 아래처럼 먼저 ``auth_context_var``를 설정하지
않으면 인메모리 테스트의 모든 tool 호출이 이 fail-safe에 걸려 거부된다.

    from mcp.server.auth.middleware.auth_context import auth_context_var
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

    token = auth_context_var.set(AuthenticatedUser(access_token))
    try:
        ...  # tool 호출
    finally:
        auth_context_var.reset(token)

빠뜨리면 모든 호출이 고정된 거부 메시지로 실패해 정책 버그처럼 보이지만
실제론 테스트 fixture 누락이다.

**Async 전용인 이유**: SDK v2 tool은 기본이 async(sync ``def`` tool은 SDK가
알아서 워커 스레드에서 돌림)이고, 이 Stage가 추가하는 tool(TASK-020-022)도
전부 async다. sync tool까지 감싸려면 이 모듈이 SDK의 스레드 오프로드를
다시 구현해야 하는데 불필요한 케이스다. 그래서 ``guard``는
``Callable[..., Awaitable[Any]]``만 감싸고, 코루틴 함수가 아니면 호출
시점이 아니라 **데코레이션 시점**에 ``TypeError``를 던져 실수를 즉시
드러낸다(``await fn(...)`` 안의 알 수 없는 런타임 실패로 미루지 않는다).
"""

from __future__ import annotations

import inspect
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from functools import wraps
from typing import Any, TypeVar, cast

from mcp import MCPError
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver.exceptions import ResourceError, ToolError

from devoks_mcp_management.audit.logger import emit as _default_emit
from devoks_mcp_management.auth.policy import authorize
from devoks_mcp_management.auth.verifier import get_role
from devoks_mcp_management.config import Settings
from devoks_mcp_management.types import (
    AUDIT_EVENT_TOOL_CALL,
    AuditOutcome,
    AuditRecord,
    SecurityBoundaryError,
    SecurityReasonCode,
)

__all__ = ["make_tool_guard"]

logger = logging.getLogger(__name__)

#: 모든 async tool 본문에 바인딩. wrapper는 ``fn``과 시그니처·반환값이
#: 동일하다(``decorator`` 하단의 ``cast(F, wrapper)``가 이를 문서화).
#: ``ParamSpec``은 데코레이터 팩토리라는 2단 제네릭이 필요해 overkill —
#: 호출부가 MCP SDK의 동적 kwargs 디스패치라 정적 타입체크 이득도 적다.
F = TypeVar("F", bound=Callable[..., Awaitable[Any]])

AuditEmitter = Callable[[AuditRecord], None]
Clock = Callable[[], float]
TimestampFactory = Callable[[], str]
RequestIdFactory = Callable[[], str]

#: "identity None이 fail-safe 거부인 이유" 참고(모듈 docstring).
_NO_IDENTITY_SENTINEL = "anonymous"
_NO_IDENTITY_REASON_CODE = "no_identity"

#: 실제 role일 수 없음(``config.py``가 빈 ``MCP_ROLE_TOOLS`` 키를 거부) —
#: ``authorize()``에 넘기면 항상 ``role_unknown`` 분기로 귀결. identity가
#: 아예 없거나 ``AccessToken``에 role claim이 없을 때 공통으로 쓰인다.
_UNKNOWN_ROLE_SENTINEL = ""

#: 이름 있는 ``repo_arg``가 ``str``이 아닌 값으로 온 경우의 대체값
#: (TASK-045). ``None``이면 안 된다 — ``authorize()``는 ``repo is None``일
#: 때 repo 체크를 통째로 건너뛰므로, 잘못된 타입에 ``None``을 반환하면
#: **fail-open**이었다(``read_file(repo=123, ...)``이 allowlist 체크 없이
#: 승인되고 감사 레코드엔 거부 흔적조차 안 남음). ``\x00`` 접두사는
#: ``config.py``의 ``_REPO_ALLOWLIST_ENTRY``가 널바이트를 허용하지 않으므로
#: 어떤 설정값과도 절대 매치되지 않음을 보장 — 결과는 truthful하게
#: ``denied``/``repo_not_allowlisted``.
#:
#: 실제로는 MCP SDK가 guard 실행 전에 tool 인자를 시그니처로 검증해
#: 타입이 틀린 ``repo``를 pydantic 검증 에러로 거부하므로, 이건 그 계층이
#: 바뀌거나 우회될 경우를 대비한 defense-in-depth다 — 현재 도달 가능한
#: 경로는 아니다.
_MALFORMED_REPO_SENTINEL = "\x00-repo-arg-was-not-a-string"


def _default_timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _default_request_id() -> str:
    return uuid.uuid4().hex


def make_tool_guard(
    settings: Settings,
    *,
    emit: AuditEmitter = _default_emit,
    clock: Clock = time.perf_counter,
    timestamp_factory: TimestampFactory = _default_timestamp,
    request_id_factory: RequestIdFactory = _default_request_id,
) -> Callable[..., Callable[[F], F]]:
    """서버 인스턴스 1개용 ``guard`` 데코레이터 팩토리를 만든다.

    ``settings``는 ``auth.policy.authorize``에 넘길 ``role_tools``/
    ``repo_allowlist``를 제공한다. ``emit``/``clock``/``timestamp_factory``/
    ``request_id_factory``는 실제 구현(``audit.logger.emit``,
    ``time.perf_counter``, wall-clock ISO 8601, ``uuid4``)이 기본값이고
    테스트가 교체할 수 있도록 존재한다 — 이유는 모듈 docstring 참고.
    """

    def guard(
        tool: str,
        *,
        repo_arg: str | None = None,
        audit_args: tuple[str, ...] = (),
    ) -> Callable[[F], F]:
        """tool 1개용 데코레이터. 전체 계약은 모듈 docstring 참고.

        ``tool``은 ``role_tools``/감사 레코드에 쓰이는 CTR-007 tool 이름 —
        래핑된 함수의 ``__name__``과 무관하므로 Python 식별자와 다른
        이름으로 등록 가능. ``repo_arg``는 이 호출이 대상으로 하는 repo를
        담은 파라미터 이름(없으면 생략). ``audit_args``는 ``args_summary``
        에 문자열화할 파라미터 이름들(``repo_arg``는 ``audit_args``에
        없어도 항상 포함).
        """
        log_arg_names = tuple(dict.fromkeys((*audit_args, *((repo_arg,) if repo_arg else ()))))

        def decorator(fn: F) -> F:
            if not inspect.iscoroutinefunction(fn):
                raise TypeError(
                    f"make_tool_guard only wraps async tool functions; {fn!r} is not "
                    "one (sync tools are out of scope for this guard — see module "
                    "docstring)"
                )
            signature = inspect.signature(fn)

            @wraps(fn)
            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                start = clock()
                ts = timestamp_factory()
                request_id = request_id_factory()

                bound = signature.bind_partial(*args, **kwargs)
                bound.apply_defaults()
                repo = _extract_str_arg(bound.arguments, repo_arg)
                args_summary = _build_args_summary(bound.arguments, log_arg_names)

                def record(
                    *,
                    outcome: AuditOutcome,
                    client_id: str,
                    role: str,
                    reason_code: str | None = None,
                    error_kind: str | None = None,
                ) -> None:
                    elapsed_ms = max(0, round((clock() - start) * 1000))
                    emit(
                        AuditRecord(
                            ts=ts,
                            event=AUDIT_EVENT_TOOL_CALL,
                            client_id=client_id,
                            role=role,
                            tool=tool,
                            args_summary=args_summary,
                            outcome=outcome,
                            reason_code=reason_code,
                            error_kind=error_kind,
                            duration_ms=elapsed_ms,
                            request_id=request_id,
                        )
                    )

                access_token = get_access_token()
                identity_present = access_token is not None
                client_id = (
                    access_token.client_id if access_token is not None else _NO_IDENTITY_SENTINEL
                )
                role = get_role(access_token) if access_token is not None else None
                effective_role = role if role is not None else _UNKNOWN_ROLE_SENTINEL

                decision = authorize(
                    effective_role,
                    tool,
                    repo,
                    role_tools=settings.role_tools,
                    repo_allowlist=settings.repo_allowlist,
                )
                if not decision.allowed:
                    client_message = decision.client_message
                    reason_code = decision.reason_code
                    assert client_message is not None  # policy guarantees this when denied
                    assert reason_code is not None
                    # AC-003-2 / AC-004-2: 감사엔 남기되 본문은 실행 안 함.
                    # `client_id`는 identity 없을 때 이미 위에서 sentinel로
                    # 치환됐고, `role`/`reason_code`도 여기서 같은 이유로
                    # override — `effective_role`/`decision.reason_code`는
                    # sentinel role에 대해 authorize()가 만든
                    # ""/"role_unknown" 값이지 no-identity 전용 값이 아니다.
                    record(
                        outcome="denied",
                        client_id=client_id,
                        role=effective_role if identity_present else _NO_IDENTITY_SENTINEL,
                        reason_code=reason_code if identity_present else _NO_IDENTITY_REASON_CODE,
                    )
                    raise ToolError(client_message)

                outcome: AuditOutcome = "ok"
                error_kind: str | None = None
                # TASK-049: SecurityBoundaryError 분기에서만 설정되고 아래
                # `finally`(이 호출의 유일한 emit 지점)에서 읽힌다. `except`
                # 안에 `record()`를 추가하면 tool 호출 1건에 감사 줄이
                # 2개 생긴다.
                security_reason_code: SecurityReasonCode | None = None
                try:
                    result = await fn(*args, **kwargs)
                except SecurityBoundaryError as exc:
                    # TASK-049. 아래 일반 ToolError 분기보다 먼저 와야 한다
                    # — SecurityBoundaryError는 ToolError의 서브클래스라,
                    # 순서가 바뀌면 여기서 못 잡고 `error`/`ToolError`로
                    # 기록된다.
                    #
                    # `error`가 아니라 `denied`로 분류하는 이유: "누가
                    # 경계를 찔러보고 있나?"를 조회하는 버킷이 `denied`이기
                    # 때문 — 다른 경계 거부(`repo_not_allowlisted`,
                    # `tool_not_permitted`)도 전부 거기 있다. `error`에
                    # 두면 "file not found", "rate limited"와 구분이 안
                    # 된다.
                    #
                    # 예외는 변환 없이 그대로 재발생 — 이미 client-safe한
                    # ToolError이고 메시지엔 위반 규칙만 담기므로 호출자가
                    # 보는 결과는 이전과 동일(AC-003-5), 감사 줄에만 조회
                    # 가능한 reason_code가 추가된다.
                    #
                    # `error_kind`는 None 유지 — 이건 실패가 아니라 거부이고,
                    # CTR-003이 두 필드를 분리해둔 이유가 값 파싱 없이
                    # 로그에서 구분하게 하기 위함이다.
                    outcome = "denied"
                    security_reason_code = exc.reason_code
                    logger.warning(
                        "Tool %r rejected a security-boundary violation (%s): %s",
                        tool,
                        exc.reason_code,
                        exc,
                    )
                    raise
                except (ToolError, ResourceError, MCPError) as exc:
                    # 의도적으로 던진, 이미 client-safe한 예외(SDK 계약) —
                    # 감사만 남기고 그대로 통과시켜 하위 tool의 메시지가
                    # 모델에 그대로 도달하게 한다.
                    outcome = "error"
                    error_kind = type(exc).__name__
                    logger.info("Tool %r failed with a deliberate %s: %s", tool, error_kind, exc)
                    raise
                except Exception as exc:
                    # EDGE-009 / AC-004-4: 예기치 않은 크래시. 트레이스백은
                    # 서버 로그에만 남기고, 클라이언트엔 원본 예외 텍스트
                    # 없이(내부 경로·쿼리 문자열 등이 섞여 있을 수 있어
                    # 검증 불가) request_id가 포함된 일반 메시지만 전달.
                    outcome = "error"
                    error_kind = type(exc).__name__
                    logger.exception("Tool %r crashed", tool)
                    raise ToolError(
                        f"Internal error while executing tool {tool!r}. request_id={request_id}"
                    ) from exc
                else:
                    return result
                finally:
                    record(
                        outcome=outcome,
                        client_id=client_id,
                        role=effective_role,
                        reason_code=security_reason_code,
                        error_kind=error_kind,
                    )

            return cast(F, wrapper)

        return decorator

    return guard


def _extract_str_arg(arguments: Mapping[str, Any], name: str | None) -> str | None:
    """tool이 지정한 repo 인자를 읽는다(이름으로만 받음 — 모듈 docstring 참고).

    의도적으로 분리한 세 가지 결과(TASK-045):

    - ``name is None`` — tool에 repo 인자가 없음, ``None``은 "repo 체크
      스킵"이라는 올바른 의미.
    - ``str``인 인자 — ``authorize()``가 ``CTR-008``로 검사하도록 그대로
      반환.
    - 인자는 있지만 ``str``이 **아님** — ``None``(스킵, fail-open)이 아니라
      ``_MALFORMED_REPO_SENTINEL``을 반환해 repo 체크가 실행되고 거부되게
      한다.
    """
    if name is None:
        return None
    if name not in arguments:
        # tool엔 repo 인자가 있지만 이 호출에서 바인딩되지 않음 —
        # allowlist와 대조할 게 없다. "non-string에 바인딩됨"(잘못된
        # 호출)과는 다른 경우.
        return None
    value = arguments[name]
    if isinstance(value, str):
        return value
    if value is None:
        # 명시적으로 optional인 repo 인자가 비어 있음 — 바인딩 안 된 것과
        # 같은 의미(``bind_partial().apply_defaults()``는 생략된
        # 파라미터를 ``None``에 바인딩한다).
        return None
    return _MALFORMED_REPO_SENTINEL


def _build_args_summary(arguments: Mapping[str, Any], names: tuple[str, ...]) -> dict[str, str]:
    """CTR-003 ``args_summary``: 명시적으로 지정한 인자만 문자열화한다.

    기본값으로 남은 파라미터(예: 생략된 ``ref``)는
    ``bind_partial().apply_defaults()``에 의해 ``None``으로 바인딩되는데,
    이런 값은 문자열 ``"None"``으로 직렬화하지 않고 summary에서 아예
    뺀다.
    """
    summary: dict[str, str] = {}
    for name in names:
        if name not in arguments:
            continue
        value = arguments[name]
        if value is None:
            continue
        summary[name] = str(value)
    return summary
