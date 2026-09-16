"""Management MCP 서버의 도메인 타입/상수.

순수 선언만 담는다 — I/O·직렬화·검증 로직 없음. 직렬화는 ``audit.logger``, 범위
검증은 ``config``가 맡고, 여기는 FRD §5 계약값의 유일한 정의처(SSOT) 역할만 한다.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from mcp.server.mcpserver.exceptions import ToolError

# --- 툴 이름 (CTR-007 role/tool 매핑 키) --------------------------------------

TOOL_LIST_REPOS: Final = "list_repos"
TOOL_GET_REPO_TREE: Final = "get_repo_tree"
TOOL_READ_FILE: Final = "read_file"
TOOL_SEARCH_CODE: Final = "search_code"

#: Stage 1 툴 전체 목록. role에 부여 가능한 모든 이름은 여기 있어야 하며, ``config``가
#: 존재하지 않는 툴을 참조하는 role/tool 매핑을 거부할 수 있게 한다(안 그러면 조용한
#: 인가 구멍이 된다).
CORE_GITHUB_TOOLS: Final[frozenset[str]] = frozenset(
    {
        TOOL_LIST_REPOS,
        TOOL_GET_REPO_TREE,
        TOOL_READ_FILE,
        TOOL_SEARCH_CODE,
    }
)

# --- 감사 레코드 (CTR-003) -----------------------------------------------------

#: ``ok``=툴 실행·반환 성공, ``denied``=본문 실행 전 인가 거부, ``error``=본문 실행 중 실패.
AuditOutcome = Literal["ok", "denied", "error"]

AUDIT_EVENT_TOOL_CALL: Final = "tool_call"


# --- 보안 경계 위반 거부 (TASK-049) ---------------------------------------------

#: 툴 인자가 보안 경계를 넘으려 한 경우의 감사 ``reason_code`` 값. 본문 실행 *전*
#: 인가 판정을 기술하는 ``auth.policy.ReasonCode``와는 별개다.
SecurityReasonCode = Literal["path_traversal_attempt", "query_qualifier_injection"]


class SecurityBoundaryError(ToolError):
    """보안 경계를 넘으려 한 툴 인자를 거부할 때 쓰는 예외.

    존재 이유 — 관측 공백 해소
    ---------------------------
    ``EDGE-013``(path traversal)·``EDGE-014``(검색 qualifier 인젝션)는 툴 본문
    깊숙한 ``adapters.knowledge.github.client``에서 검증되는데, 감사 레코드는
    ``tools.guard`` wrapper가 쓴다. 그대로 두면 두 거부 모두 일반 입력 오류와
    똑같이 ``outcome="error" error_kind="ToolError"``로만 남는다 — 실제 배포
    서버 CloudWatch 로그로 확인. "누가 allowlist를 찔러보나"를 찾는 운영자는
    다른 모든 경계 거부(``repo_not_allowlisted`` 등)가 모이는
    ``outcome="denied"``를 본다. allowlist 우회 시도가 "파일 없음"·"rate
    limited"와 같은 ``error`` 버킷에 섞이면 잡음과 구분이 안 된다.

    ``ToolError`` 서브클래스인 이유
    --------------------------------
    서브클래싱은 호출자 경험을 그대로 유지한다 — ``guard``는 ``ToolError``를
    그대로 통과시켜 툴 메시지가 모델에 그대로 전달되고, ``AC-003-5``는 거부가
    호출자에게 구분되지 않아야 한다고 요구한다. 바뀌는 건 감사 분류뿐이다:
    ``guard``가 일반 ``ToolError`` 절보다 먼저 이 타입을 잡아
    ``outcome="denied"`` + ``reason_code``를 기록한 뒤 같은 예외를 재발생시킨다.
    호출자는 같은 문자열을 보고, 운영자는 조회 가능한 신호를 얻는다.
    """

    def __init__(self, message: str, *, reason_code: SecurityReasonCode) -> None:
        super().__init__(message)
        self.reason_code: SecurityReasonCode = reason_code


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """감사 로그 한 줄. 필드 구성은 CTR-003으로 고정.

    Frozen이라 생성 후 emit 전까지 변경 불가 — redaction 보장(AC-004-3)이 유지된다:
    ``args_summary``엔 repo/path/query 값만 담고 파일 내용은 절대 담지 않으며,
    어떤 필드도 bearer 토큰이나 private key를 담지 않는다.
    """

    ts: str
    """이벤트 시각, UTC 오프셋 포함 ISO 8601."""

    event: str
    """레코드 종류. 툴 호출이면 ``AUDIT_EVENT_TOOL_CALL``."""

    client_id: str
    """검증된 access token에서 얻은 호출 OAuth client."""

    role: str
    """인가 판정 기준이 된 role."""

    tool: str
    """MCP로 노출되는 툴 이름."""

    args_summary: Mapping[str, str]
    """식별용 인자만(repo/path/ref/query). 파일 내용은 없음."""

    outcome: AuditOutcome

    reason_code: str | None
    """``outcome`` == ``denied``일 때 어떤 인가 규칙이 거부했는지, 아니면 ``None``.

    운영자 전용 필드. AC-003-5는 거부 시 호출자에게 동일한 메시지만 보여야 한다고
    요구하므로, 어느 규칙이 걸렸는지는 클라이언트에 노출할 수 없다 — 대신 이 필드가
    ``auth.policy``의 2-계층 분리(클라이언트=고정 문자열, 감사=reason code)를
    받는다. ``error_kind``와 분리해 둬서 "인가가 거부했다"와 "본문이 실패했다"를
    값 파싱 없이 쿼리로 구분할 수 있다."""

    error_kind: str | None
    """``outcome`` == ``error``일 때 예외 클래스 이름, 아니면 ``None``. 메시지·
    traceback은 절대 담지 않음(서버 로그에만 남긴다)."""

    duration_ms: int

    request_id: str
    """같은 요청의 서버 로그 라인과 이 레코드를 연결하는 키."""


# --- 계약 범위/기본값 -----------------------------------------------------------
# 범위는 FRD §5.1 근거. `config`가 MIN/MAX로 검증하고 실패 시 DEFAULT로 폴백 —
# 범위 밖 환경값은 모델 컨텍스트/rate limit 예산을 보호하는 한도를 조용히
# 약화시키는 대신 기동 시점에 실패한다.

#: CTR-004 — ``read_file`` 응답 truncation 전 최대 바이트 수.
READ_FILE_MAX_BYTES_DEFAULT: Final = 262_144
READ_FILE_MAX_BYTES_MIN: Final = 1
READ_FILE_MAX_BYTES_MAX: Final = 1_048_576

#: CTR-005 — ``search_code`` 결과 개수 상한(호출자 컨텍스트 보호).
SEARCH_CODE_MAX_RESULTS_DEFAULT: Final = 30
SEARCH_CODE_MAX_RESULTS_MIN: Final = 1
SEARCH_CODE_MAX_RESULTS_MAX: Final = 100

#: CTR-009 — installation 토큰 남은 수명이 이 값 이하로 떨어지면 갱신 — 호출 도중
#: 만료되는 토큰으로 시작하는 일을 막는다.
TOKEN_REFRESH_LEEWAY_SECONDS_DEFAULT: Final = 300
TOKEN_REFRESH_LEEWAY_SECONDS_MIN: Final = 60
TOKEN_REFRESH_LEEWAY_SECONDS_MAX: Final = 1_800
