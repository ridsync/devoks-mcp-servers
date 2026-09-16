"""쿼리 관측 레코드 -- 쿼리 1건당 JSON 한 줄, 원본 질문은 담지 않음(TASK-008).

``REQ-SB-007``: 모든 Slack 쿼리는 결과와 무관하게 stdout에 구조화된 한 줄을 남겨,
운영자가 DB 없이 CloudWatch Logs Insights만으로 "누가 뭘 언제 물었고, 얼마나
걸렸고, 비용이 얼마였는지"에 답할 수 있게 한다. ``CTR-SB-008``(FRD §5.1)이 필드셋
SSOT다 -- 아래 필드명/순서는 반드시 그대로 맞춰야 한다(운영자가 이 이름으로
쿼리하므로 rename/reorder 금지).

이 모듈이 레코드를 직접 조립하는 이유(Stage 1의 ``audit/logger.py``와 다른 점)
--------------------------------------------------------------------------
Stage 1의 ``DSN-003``은 ``audit/logger.py``를 레코드 조립 로직(clock, ``request_id``
생성 등) 없이 의도적으로 순수하게 유지한다 -- 호출자가 완성된 ``AuditRecord``를
만들어 ``emit``에 넘긴다. 이 모듈도 같은 serialize-and-emit 형태를 재사용하지만
(``to_json_line``/``emit``은 직접 포팅), 책임 하나를 더 진다: ``build_record``가
원본 질문 문자열을 ``question_len`` + ``question_sha256``으로 바꾸고 *원본 문자열은
어디에도 반환·저장하지 않는다*. ``AC-SB-007-3``이 원본 질문이 레코드에 절대
닿으면 안 된다고 요구하는데, 이를 보장하는 가장 안전한 방법은 길이/해시 변환을
이 모듈 한 곳에서만 하는 것이다 -- 각 호출부(``worker.py``, ``handler.py`` 등)가
직접 레코드를 조립하기 전에 해시하는 걸 잊지 않길 바라는 것보다 낫다.

해시를 16자로 자르는 이유(``QUESTION_HASH_PREFIX_LEN``)
-------------------------------------------------------------------------------
레코드에 이미 ``question_len``이 있으므로 해시의 역할은 "이 행이 다른 행과 같은
질문이다"를 알아채거나, 질문 원문을 가진 지원 티켓과 재해시해 대조하는 정도다 --
의도적 공격자에 대한 충돌 내성까지는 필요 없다. 16 hex 문자(64비트)면 로그 볼륨을
낮게 유지하면서도 이 목적엔 사실상 충분히 유일하다. SHA-256을 쓰는 이유는 새
의존성이 필요 없고, 단방향이며, 16자 prefix만으로 원본 질문을 복원하는 게
계산적으로 불가능하기 때문 -- truncation으로 잃는 건 애초에 필요 없던 충돌 내성
여유분뿐, 실질적인 역상 저항성은 아니다.

``usage`` -- SDK 객체가 아니라 순수 숫자만(``AC-SB-007-2``, ``EDGE-SB-017``)
-------------------------------------------------------------------------------------------
**이 모듈은 ``anthropic``을 절대 import하지 않는다.** 실측 비용 1,384ms(workspace
PLAN §1) -- import하는 Lambda의 콜드스타트마다 치러야 하고, 이 모듈은 worker뿐
아니라 handler의 3초 ACK 예산(``CTR-SB-002``)에서도 import 가능해야 한다.
그래서 ``build_record``는 ``usage``를 순수 ``Mapping[str, int | None]``(또는
``None``)로 받는다 -- 호출자(``ask.py``/``worker.py``)가 여기 넘기기 *전에*
Anthropic SDK의 usage 객체에서 ``input_tokens``/``output_tokens``/
``cache_read_input_tokens``를 뽑아둬야 한다. SDK 객체 자체를 넘기면 JSON
직렬화가 아예 깨지거나 그 객체가 우연히 가진 다른 필드가 유출될 수 있다.
``denied``/대부분의 ``error`` 경로에서는 Claude API 호출 자체가 없었으므로
``usage``가 ``None``이고, 세 키 중 일부가 없어도(응답 실패/부분 응답) 예외 없이
처리된다.

한 줄 보장(``AC-SB-007-1``)
------------------------------------------
Stage 1의 ``CTR-003`` 레코드와 같은 메커니즘: ``indent`` 없는 ``json.dumps``는
리터럴 줄바꿈을 절대 만들지 않고, 모든 제어 문자(예를 들어 한국어 ``reason_code``
문자열에 섞인 ``\\n``/``\\r``도)를 멀티 문자 escape 시퀀스로 바꾼다. 어떤
``reason_code``/``error_kind``가 들어와도 CloudWatch/Lambda 로그 한 줄 == JSON
레코드 하나.

``ensure_ascii=True``(의도적 선택, 라이브러리 기본값의 우연한 부작용이 아님)
-------------------------------------------------------------------------
``reason_code``는 비ASCII 텍스트(이 패키지 다른 곳에서 온 한국어 사유)를 담을 수
있다. ``ensure_ascii=True``는 모든 비ASCII 문자를 ``\\uXXXX``로 escape해, 다운스트림
로그 수집기가 어떤 인코딩을 가정하든 결과 줄이 순수 ASCII이게 한다 -- Stage 1의
``CTR-003``과 동일한 이유로 동일한 선택이라, 두 서버의 로그 줄 모두 같은 도구로
`grep`/파이프해도 mojibake 위험이 없다.

실패 정책: 기록이 호출자의 실제 작업을 절대 방해하면 안 된다
--------------------------------------------------------------------
``signature.py``/``events.py``는 계약상 never-raises다. 이 모듈도 같은 태도를
다른 이유로 택한다 -- Claude가 이미 답했거나(또는 정당하게 거부됐거나) 한 Slack
쿼리는 *이* 관측 줄 기록이 실패해도(닫힌 stdout, ``write``/``flush``에서
raise하는 스트림) 사용자에게 반드시 도달해야 한다. 그래서 ``emit``은 쓰기 중
발생하는 모든 ``Exception``을 catch해 표준 ``logging``으로 한 번 로그만 남기고
(운영자에게는 보이되 re-raise는 안 함) 반환한다. ``emit_query_observation``은
``build_record`` 자체에도 같은 보장을 확장해, "이 쿼리를 기록하되 기록이 절대
다른 걸 깨면 안 된다"만 원하는 호출자가 함수 하나만 부르면 되게 한다. 개별
조각이 필요한 호출자(테스트, 또는 emit 전에 레코드를 검사하고 싶은 호출자)는
``build_record``/``to_json_line``/``emit``을 직접 불러도 된다 -- 이들은
억제되지 않고 그대로 raise한다(잘못된 호출이면), 그래야 테스트가 "기록이
degrade됐다"와 "기록 자체가 버그다"를 구분할 수 있다.

여기서 stdout이 안전한 이유
---------------------------
이 패키지의 두 Lambda(handler, worker) 모두 stdio 기반 프로토콜이 아니라 Lambda
런타임이 직접 invoke한다 -- 즉 stdout이 어느 쪽에서도 wire 트래픽을 나르지
않으므로, AWS Lambda 로그 드라이버가 줄 단위로 수집해(CloudWatch 로그 그룹으로)
가는 것이 정확히 의도된 용도다. Stage 1의 ``audit/logger.py``와 같은 근거.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal, Protocol

logger = logging.getLogger(__name__)

#: CTR-SB-008: 레코드 종류. 고정값 -- 이 모듈이 emit하는 모든 레코드가 정확히 이
#: 값을 가지며, 이 파일에는 다른 종류의 레코드가 없다.
EVENT_SLACK_QUERY: Final = "slack_query"

#: 모듈 docstring의 "해시를 16자로 자르는 이유" 절 참고.
QUESTION_HASH_PREFIX_LEN: Final = 16

__all__ = [
    "EVENT_SLACK_QUERY",
    "QUESTION_HASH_PREFIX_LEN",
    "ObservationRecord",
    "ObservationStream",
    "Outcome",
    "UsageSummary",
    "build_record",
    "emit",
    "emit_query_observation",
    "to_json_line",
]

#: ``CTR-SB-008``의 outcome 값 3종. Stage 1의 ``AuditOutcome``(``types.py``)과
#: 형태가 정확히 같지만 import하지 않고 여기서 독립된 alias로 둔다 -- FRD §4.4가
#: 두 서버 간 공유 패키지를 금지하므로, 이 작은 계약도 각자 자기 복사본을 둔다.
Outcome = Literal["ok", "denied", "error"]


class ObservationStream(Protocol):
    """``emit``에 필요한 최소 스트림 기능 -- Stage 1의 ``AuditStream``과 동일 패턴.

    구조적 ``Protocol``(write + flush만)이라 ``sys.stdout``, 테스트의
    ``io.StringIO``, 또는 write+flush를 갖춘 다른 어떤 sink도 서브클래싱 없이
    만족시킬 수 있다.
    """

    def write(self, s: str, /) -> object: ...
    def flush(self) -> None: ...


@dataclass(frozen=True, slots=True)
class UsageSummary:
    """``CTR-SB-008``이 지정한 Claude API usage 필드 3개 -- 그 외에는 없음.

    모든 필드가 독립적으로 nullable: 응답이 어떻게 종료됐는지(``EDGE-SB-017``)에
    따라 Anthropic SDK의 usage 객체는 이 중 임의의 부분집합만 채워져 올 수 있고,
    이 타입은 항상 순수 숫자만 담는다 -- SDK 객체 자체는 절대 담지 않는다(모듈
    docstring 참고).
    """

    input_tokens: int | None
    output_tokens: int | None
    cache_read_input_tokens: int | None


@dataclass(frozen=True, slots=True)
class ObservationRecord:
    """``CTR-SB-008`` 레코드 1개. 필드셋/이름은 계약으로 고정 -- 모듈 docstring 참고.

    눈에 띄게 빠진 것: 원본 질문 텍스트. 이를 담을 수 있는 필드가 여기 없다
    (``AC-SB-007-3``) -- ``question_len``/``question_sha256``만 있고,
    ``build_record``가 원본을 저장하지 않은 채 이 값들을 유도한다.
    """

    ts: str
    """이벤트 시각, UTC offset 포함 ISO 8601 -- 호출자가 제공(이 모듈은 clock을
    읽지 않는다, Stage 1의 ``DSN-003``과 같은 이유: 순수 레코드 타입이면 시간을
    고정하지 않고도 테스트가 쉽다)."""

    event: str
    slack_user_id: str | None
    """호출자를 아예 식별할 수 없을 때 ``None``(``identity.py``의
    ``user_unidentified`` 경로) -- 식별은 됐지만 미등록인 사용자와는 다르다,
    그 경우 ``slack_user_id``는 알려져 있고 기록된다."""

    client_id: str | None
    """조회된 MCP client/person 식별자(예: ``"okwon"``) -- 토큰이 아님. 자격증명이
    전혀 resolve되지 않으면(denied/unidentified) ``None``."""

    channel: str | None
    thread_ts: str | None
    question_len: int
    question_sha256: str
    """질문의 SHA-256 hex digest, ``QUESTION_HASH_PREFIX_LEN``자로 자름. 질문
    원문은 절대 아님."""

    outcome: Outcome
    reason_code: str | None
    """``denied``/``error`` outcome의 운영자 전용 분류(예: ``identity.py``의
    ``reason_code``, ``idempotency.py``의 ``reason``). ``outcome="ok"``면
    ``None``."""

    error_kind: str | None
    """``outcome="error"``일 때 예외 클래스명, 아니면 ``None``."""

    duration_ms: int
    usage: UsageSummary | None
    """이 쿼리에 대해 Claude API 호출이 아예 없었으면(대부분의 ``denied``/일부
    ``error``) ``None`` -- 호출은 됐지만 usage 필드 일부가 없는 경우
    (``UsageSummary``의 필드 일부가 ``None``)와는 다르다."""

    request_id: str


def build_record(
    *,
    ts: str,
    slack_user_id: str | None,
    client_id: str | None,
    channel: str | None,
    thread_ts: str | None,
    question: str,
    outcome: Outcome,
    reason_code: str | None,
    error_kind: str | None,
    duration_ms: int,
    usage: Mapping[str, int | None] | None,
    request_id: str,
) -> ObservationRecord:
    """``question``에 대한 ``ObservationRecord`` 1개를 만든다(``AC-SB-007-3``, ``CTR-SB-008``).

    ``question``은 ``question_len``/``question_sha256`` 계산에만 쓰인다 --
    반환되는 레코드에 절대 복사되지 않고 이 함수는 로그도 남기지 않는다. 이
    패키지에서 질문 문자열로부터 길이/해시 쌍을 유도해야 하는 곳은 *여기뿐*이다
    -- ``ObservationRecord``를 만드는 모든 호출자는 직접 해시하지 말고 이 함수를
    거쳐야 한다.

    ``usage``는 정수 키 최대 3개(``input_tokens``, ``output_tokens``,
    ``cache_read_input_tokens``)를 가진 순수 매핑만 받는다 -- Anthropic SDK의
    usage 객체는 절대 안 됨(모듈 docstring 참고). Claude API 호출이 없었으면
    ``None``을 넘긴다. 매핑이 있어도 세 키 중 일부를 안전하게 생략할 수 있다.
    """
    return ObservationRecord(
        ts=ts,
        event=EVENT_SLACK_QUERY,
        slack_user_id=slack_user_id,
        client_id=client_id,
        channel=channel,
        thread_ts=thread_ts,
        question_len=len(question),
        question_sha256=_hash_question(question),
        outcome=outcome,
        reason_code=reason_code,
        error_kind=error_kind,
        duration_ms=duration_ms,
        usage=_summarize_usage(usage),
        request_id=request_id,
    )


def _hash_question(question: str) -> str:
    return hashlib.sha256(question.encode("utf-8")).hexdigest()[:QUESTION_HASH_PREFIX_LEN]


def _summarize_usage(usage: Mapping[str, int | None] | None) -> UsageSummary | None:
    if usage is None:
        return None
    return UsageSummary(
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        cache_read_input_tokens=usage.get("cache_read_input_tokens"),
    )


def to_json_line(record: ObservationRecord) -> str:
    """``record``를 ``CTR-SB-008`` JSON 한 줄로 직렬화한다(끝에 줄바꿈 없음).

    순수 함수 -- I/O 없음 -- 이라 스트림을 캡처하지 않고도 직렬화를 바로 테스트할
    수 있다. 필드명/순서는 ``CTR-SB-008``과 정확히 일치해야 한다 -- 운영자가 이
    키로 CloudWatch Logs Insights를 쿼리하므로 rename 금지. ``ensure_ascii=True``가
    왜 의도적인지는 모듈 docstring 참고.
    """
    usage_payload: dict[str, int | None] | None = (
        None
        if record.usage is None
        else {
            "input_tokens": record.usage.input_tokens,
            "output_tokens": record.usage.output_tokens,
            "cache_read_input_tokens": record.usage.cache_read_input_tokens,
        }
    )
    payload: dict[str, object] = {
        "ts": record.ts,
        "event": record.event,
        "slack_user_id": record.slack_user_id,
        "client_id": record.client_id,
        "channel": record.channel,
        "thread_ts": record.thread_ts,
        "question_len": record.question_len,
        "question_sha256": record.question_sha256,
        "outcome": record.outcome,
        "reason_code": record.reason_code,
        "error_kind": record.error_kind,
        "duration_ms": record.duration_ms,
        "usage": usage_payload,
        "request_id": record.request_id,
    }
    return json.dumps(payload, ensure_ascii=True)


def emit(record: ObservationRecord, *, stream: ObservationStream | None = None) -> None:
    """``record``에 대한 관측 줄 하나를 쓰고 flush한다. 절대 raise하지 않는다.

    ``stream`` 기본값은 호출 시점(함수 정의 시점이 아님)에 resolve되는 호출자의
    현재 ``sys.stdout`` -- Stage 1의 ``audit/logger.py``와 동일. 쓰기/flush 중
    실패(닫힌 스트림, raise하는 스트림)는 catch해 표준 ``logging``으로 한 번
    로그만 남기고 삼킨다 -- 모듈 docstring "실패 정책" 절 참고. 줄과 끝의
    줄바꿈은 단일 ``write`` 호출로 함께 쓴다 -- 그래야 같은 스트림을 공유하는
    다른 무언가가 그 사이에 부분 줄을 끼워 넣을 수 없다.
    """
    out: ObservationStream = sys.stdout if stream is None else stream
    try:
        out.write(to_json_line(record) + "\n")
        out.flush()
    except Exception:
        logger.error(
            "failed to write query observation record (request_id=%s)",
            record.request_id,
            exc_info=True,
        )


def emit_query_observation(
    *,
    ts: str,
    slack_user_id: str | None,
    client_id: str | None,
    channel: str | None,
    thread_ts: str | None,
    question: str,
    outcome: Outcome,
    reason_code: str | None,
    error_kind: str | None,
    duration_ms: int,
    usage: Mapping[str, int | None] | None,
    request_id: str,
    stream: ObservationStream | None = None,
) -> None:
    """쿼리 1건의 관측 레코드를 만들고 emit까지, 절대 raise하지 않는 호출 1번으로.

    ``worker.py``/``handler.py``의 권장 진입점: 레코드를 만들고(``build_record``)
    쓰는(``emit``) 두 단계를 ``try`` 하나 안에서 수행해, *둘 중 어느 단계*가
    실패하든(쓰기뿐 아니라) 이미 결정된 Slack 응답을 방해하며 호출자에게 전파되는
    일이 없게 한다. 모듈 docstring "실패 정책" 절 참고.
    """
    try:
        record = build_record(
            ts=ts,
            slack_user_id=slack_user_id,
            client_id=client_id,
            channel=channel,
            thread_ts=thread_ts,
            question=question,
            outcome=outcome,
            reason_code=reason_code,
            error_kind=error_kind,
            duration_ms=duration_ms,
            usage=usage,
            request_id=request_id,
        )
    except Exception:
        logger.error(
            "failed to build query observation record (request_id=%s)", request_id, exc_info=True
        )
        return
    emit(record, stream=stream)
