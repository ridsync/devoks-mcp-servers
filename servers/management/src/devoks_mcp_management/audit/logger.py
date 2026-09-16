"""감사 레코드 직렬화·기록(DSN-003, CTR-003).

순수 serialize-and-write 모듈(DSN-003이 말하는 "독립 emit 유닛") —
`AuditRecord`를 직접 만들지 않는다(시계·request_id 생성·소요시간 측정
없음, MCP SDK 비의존). 호출부(`tools/guard.py`, TASK-007)가 `ok`/
`denied`(AC-004-2)/`error`(AC-004-4) 세 결과 모두에 대해 완성된
`AuditRecord`를 조립해 `emit`에 넘긴다. `AuditRecord`가 `outcome` +
`reason_code` + `error_kind`로 세 결과를 이미 모델링하므로 `emit(record)`
하나가 전체 인터페이스다.

**stdout이 안전한 이유(다른 곳에선 아닐 수 있음)**: 이 서버는 Streamable
HTTP 전용 배포(FRD §7)라 stdout에 프로토콜 트래픽이 없어 ECS 로그
드라이버가 줄 단위로 수집한다(SDK 공식 문서도 HTTP 서버의 stdout 로깅은
안전하다고 명시). **stdio transport로 이 서버를 돌리면**(로컬 디버깅 등)
여기서 쓰는 모든 감사 줄이 그 전송(JSON-RPC 프레임)을 깨뜨린다 — 그래서
`stream`을 주입 가능하게 열어뒀다.

**`print()` 대신인 이유**: `print()`는 항상 현재 `sys.stdout`을 대상으로
하고 교체할 방법이 없어, 테스트(전역 상태를 건드리지 않고 출력을 캡처해야
함)와 위 stdio 회피 경로 둘 다 깨뜨린다. `emit`은 기본값 `None`인 `stream`
파라미터를 받아 **호출 시점**(정의 시점이 아니라)에 `sys.stdout`으로
해석한다 — 리터럴 기본값 `stream: AuditStream = sys.stdout`은 import
시점에 한 번만 바인딩되어 이후 `sys.stdout` 재할당을 반영하지 못한다.

**플러시**: 매 레코드를 쓴 직후 flush한다. tool 호출은 네트워크 바운드
(GitHub REST, 토큰 교환)라 호출당 flush 1회는 핫패스 비용이 아니다 —
반면 SIGTERM 유예 후 SIGKILL로 버퍼링된 줄이 유실되면 단순 UX 문제가
아니라 감사 레코드 누락, 즉 컴플라이언스 공백이다.

**마스킹(AC-004-3), 두 계층을 섞지 말 것**:
1. **구조적**(이미 성립, 구현 불필요) — `AuditRecord`엔 애초 bearer
   토큰·private key·파일 내용을 담을 필드가 없다. `args_summary`는
   식별용 인자(repo, path, ref, query)만 문서화됨(`types.AuditRecord`
   참고).
2. **런타임**(`_redact`에서 구현) — `args_summary`에 실수로 민감값이
   들어온 경우를 위한 방어 백스톱: 길이 상한과, FRD가 명시한 시크릿
   형태(PEM 헤더, `Authorization: Bearer` 값, GitHub 토큰 4개 접두사)
   패턴 리댁션. 일반 PII나 모든 누출을 막는 게 아니라, 알려져 있고
   저비용으로 탐지 가능한 시크릿 형태만 방어하는 의도적으로 좁은 범위다.

**한 줄 보장(AC-004-1)**: `json.dumps` 기본 설정(`ensure_ascii=True`,
`indent` 없음)은 문자열 값 안의 모든 제어문자(`\\n`/`\\r` 포함)를
이스케이프하고 리터럴 개행을 만들지 않는다 — 그래서 `args_summary`(또는
다른 필드)에 개행이 섞여도 "레코드 1개 = 줄 1개"가 유지된다. 이건 JSON
인코더 자체의 속성이라 `_redact`가 손대지 않는 필드에도 동일하게
적용된다.
"""

import json
import re
import sys
from typing import Final, Protocol

from devoks_mcp_management.types import AuditRecord

#: 런타임 리댁션 상한(AC-004-3). 정상적인 식별값(긴 repo 경로, 긴 검색어)은
#: 넉넉히 담으면서 캡처될 만한 파일 내용보다는 훨씬 작다. 다른 마스킹 내부
#: 요소와 달리 public — 경계 테스트가 숫자를 중복 정의하지 않고 직접
#: 참조하도록.
MAX_ARG_VALUE_CHARS: Final = 500
_TRUNCATION_SUFFIX: Final = "...<truncated>"

#: FRD가 명시적으로 지목한 시크릿 형태(AC-004-3): PEM 헤더, Bearer
#: 자격증명, GitHub 토큰 4개 접두사. 대소문자 무시는 의도적 — 시크릿처럼
#: 보이기만 하는 값을 과잉 리댁션하는 쪽이 안전한 방향이고, 실제 시크릿을
#: 놓치는 쪽은 아니다(다른 곳의 `auth.policy`와 같은 fail-safe 태도).
_SECRET_PATTERN: Final = re.compile(
    r"-----BEGIN"
    r"|Bearer\s+\S+"
    r"|\b(?:ghp|gho|ghs)_[A-Za-z0-9]+"
    r"|\bgithub_pat_[A-Za-z0-9_]+",
    re.IGNORECASE,
)

_REDACTED: Final = "[REDACTED]"

__all__ = ["MAX_ARG_VALUE_CHARS", "AuditStream", "emit", "to_json_line"]


class AuditStream(Protocol):
    """`emit`에 필요한 최소한의 스트림 기능.

    `typing.TextIO`보다 의도적으로 좁다 — `emit`은 write/flush만 쓰므로,
    구조적 `Protocol`이면 `sys.stdout`이나 테스트용 `io.StringIO` 등 어떤
    write+flush sink든 상속 없이 만족시킬 수 있다.
    """

    def write(self, s: str, /) -> object: ...
    def flush(self) -> None: ...


def to_json_line(record: AuditRecord) -> str:
    """`record`를 CTR-003 JSON 한 줄로 직렬화한다(끝에 개행 없음).

    순수 함수(I/O 없음) — 스트림을 캡처하지 않고도 직렬화·마스킹을 직접
    테스트할 수 있다. 필드명은 CTR-003과 정확히 일치 — 운영자가
    CloudWatch Logs Insights에서 이 키로 조회하므로 이름을 바꾸지 않는다.
    """
    payload: dict[str, object] = {
        "ts": record.ts,
        "event": record.event,
        "client_id": record.client_id,
        "role": record.role,
        "tool": record.tool,
        "args_summary": {key: _redact(value) for key, value in record.args_summary.items()},
        "outcome": record.outcome,
        "reason_code": record.reason_code,
        "error_kind": record.error_kind,
        "duration_ms": record.duration_ms,
        "request_id": record.request_id,
    }
    return json.dumps(payload, ensure_ascii=True)


def emit(record: AuditRecord, *, stream: AuditStream | None = None) -> None:
    """`record`의 감사 줄 하나를 쓰고 flush한다.

    `stream` 기본값은 호출 시점의 `sys.stdout`(이유는 모듈 docstring
    참고). 줄과 끝 개행을 `write` 한 번에 써서, 스트림을 공유하는 다른
    코드가 그 사이에 부분 줄을 끼워넣을 수 없게 한다.
    """
    out: AuditStream = sys.stdout if stream is None else stream
    out.write(to_json_line(record) + "\n")
    out.flush()


def _redact(value: str) -> str:
    """여기까지 오면 안 됐을 값을 위한 런타임 백스톱(AC-004-3).

    절단 전 **전체** 원문 값을 먼저 패턴 검사한다 — 먼저 잘라내면
    `MAX_ARG_VALUE_CHARS` 너머에 있는 시크릿 패턴이 탐지되지 않은 채
    살아남을 수 있다.
    """
    if _SECRET_PATTERN.search(value):
        return _REDACTED
    if len(value) > MAX_ARG_VALUE_CHARS:
        return value[:MAX_ARG_VALUE_CHARS] + _TRUNCATION_SUFFIX
    return value
