"""``slack-worker`` Lambda 진입점의 ASGI composition root (TASK-014).

``create_app(...) -> Starlette``는 이 이미지가 노출하는 두 번째 팩토리 -- ``handler.py``
(``TASK-012``)와 같은 이미지, 다른 Lambda(``ImageConfig.Command``가 각 Lambda를 자기
팩토리로 분기, PLAN §1의 "진입점 분기 메커니즘"). 이 모듈을 import하면 ``anthropic``이
*실제로* 딸려온다(``DSN-SB-002``) -- ``handler.py``(``DSN-SB-008``)와 달리 여기선
의도된 정상 동작이다: 이 팩토리가 지원하는 Lambda는 3초 ACK 예산이 아예 없고
``CTR-SB-009``의 300초/1024MB만 있다.

🔴 **worker는 HTTP 트리거가 아니다.** AWS Lambda Web Adapter 자체 README
(``AWS_LWA_PASS_THROUGH_PATH``, 기본값 ``"/events"``) 정의: "non-http 트리거로부터
이벤트 페이로드를 수신하는 경로". ``handler.py``는 ``boto3`` ``Invoke(InvocationType=
"Event")``로 여기에 dispatch하며 그 ``Payload``는 **원본, 미파싱** Slack 요청 본문
그대로다(``handler.py``의 ``_dispatch_to_worker`` 참고) -- LWA가 그 페이로드를
``EVENTS_PATH``의 POST 본문으로 그대로 이 앱에 넘긴다. ``EVENTS_PATH``는 모듈 상수로
고정해(절대 인라인하지 않음) ``TASK-020``(Dockerfile의
``AWS_LWA_PASS_THROUGH_PATH``)과 ``TASK-023``(Lambda 환경변수)이 같은 문자열을
참조해 서로 어긋날 수 없게 한다.

``HEALTHZ_PATH``도 ``handler.py``와 같은 이유로 존재한다: Dockerfile의
``AWS_LWA_READINESS_CHECK_PATH``(``TASK-020``)가 폴링할 경로가 없으면 Lambda Web
Adapter가 이 컨테이너를 절대 준비 완료로 보지 않는다.

🔴 **불변식 -- 이 앱의 신뢰 경계는 Slack 서명이 아니라 AWS IAM이다.** ``POST
/events``는 ``handler.py``와 달리 서명 검증을 하지 않는다(``handler.py``가 이미
검증을 끝낸 뒤 ``boto3`` ``Invoke``로 전달한 페이로드만 받는다는 전제). 어느
사용자의 MCP 토큰을 쓸지는 오직 페이로드의 ``user`` 필드로 정해진다
(``identity.resolve_credentials``) -- 이 경로에 도달하는 모든 호출자를 그 필드가
가리키는 사람으로 **그대로 신뢰**한다. 현재 안전한 이유는 이 함수에 Function URL도
API Gateway 라우트도 없고, ``handler.py`` 실행 역할의 ``lambda:InvokeFunction``
으로만 호출 가능하기 때문이다(``infra/07-slackbot-lambda.sh``,
``infra/08-slackbot-route.sh`` 참고). **이 함수에 Function URL, API Gateway 라우트,
EventBridge/SNS 트리거를 추가하거나 리소스 정책을 완화하지 말 것** -- 그 순간
서명 없이 임의 ``user`` 값을 실어 보내는 것만으로 그 사람의 MCP 토큰을 도용해
사내 저장소를 조회하고 봇 토큰으로 Slack에 게시할 수 있는 완전 무인증 경로가
열린다(보안 검증 결과, 2026-09-16).

처리 순서(FRD §4.1 worker ①~⑥, 이 워크스페이스 handover 노트의 더 세분화된 ①~⑨,
§5.4의 상태표) -- **절대 재배치 금지**:

1. **worker 측 idempotency**(``idempotency.is_event_completed``,
   ``EDGE-SB-005``). Lambda 자체 async-invoke 재시도(추가 2회, ``handler.py``의
   ``claim_event``가 이미 방어하는 Slack 자체 재시도와는 독립적인 원인)가 같은
   ``event_id``를 이 Lambda에 재전달할 수 있다. 이미 완료됐으면 -> 아무것도
   게시하지 않고 반환 -- 이 중복 원인은 worker만 볼 수 있다.
2. **자격 조회**(``identity.resolve_credentials``, ``EDGE-SB-006``,
   ``AC-SB-004-2``). ``user_id``는 ``slack/events.py``의 ``extract_user_id``에서
   온다 -- 여기서 페이로드를 절대 직접 읽지 않는다(``EDGE-SB-019``의 지역화가 그대로
   유지됨). 권한 없으면 -> 고정 거부 안내를 게시하고 종료 -- Claude 호출 없음,
   coalescing 락도 건드리지 않음.
3. **in-flight coalescing**(``idempotency.claim_inflight_query``,
   ``EDGE-SB-015``). claim 실패(같은 ``(user_id, thread_ts)``의 다른 질의가 이미
   실행 중)면 -> 수신 확인만 게시, 새 Claude 호출 없음. ⚠️ 일단 claim되면 이후
   처리가 여기서부터 어떻게 끝나든 ``release_inflight_query``는 **반드시**
   실행돼야 한다 -- ``_process_event``에서 ``_run_query``를 감싼
   ``try``/``finally``. 건너뛰면 그 쌍이 ``INFLIGHT_TTL_SECONDS_DEFAULT``
   (360초) 동안 새 질문을 못 하게 잠긴다 -- 이 태스크 자신의 "가장 중요"
   요구사항이다.
4. **접수 알림 게시**(``slack/client.ACKNOWLEDGEMENT_MESSAGE``,
   ``AC-SB-006-4``) -- 느린 단계 전에 먼저 게시해 사용자가 질문이 접수됐음을 알게
   한다.
5. **``ask_claude``** -- 이 모듈이 ``anthropic.AsyncAnthropic`` 클라이언트를 만들어
   주입한다(``ask.py``는 설계상 ``ANTHROPIC_API_KEY``를 절대 직접 읽지 않음).
6. **길이 정책**(``slack/format.apply_response_length_policy``,
   ``AC-SB-006-2``).
7. **스레드 답글**(``slack/client.post_message``, ``thread_ts``는
   ``extract_reply_target_ts``에서).
8. **완료 기록**(``idempotency.mark_event_completed``) -- 이 ``event_id``로
   뭔가를 게시한 *모든* 종료 분기(거부, coalesced로 확인만, Claude 오류/거절,
   성공) 뒤에 실행한다, 성공 경로에만 국한하지 않는다: ``EDGE-SB-005`` 관점에서
   각 분기는 그 자체로 완료된 작업 단위다 -- 같은 ``event_id``의 Lambda 재시도가
   어느 분기가 냈든 이미 게시된 메시지를 다시 게시하면 안 된다.
9. **관측 기록**(``observability.emit_query_observation``) -- 이 모듈이
   ``duration_ms``(``time.monotonic()`` 차분)를 재고 ``request_id``
   (``uuid.uuid4().hex``)를 생성한다; ``observability.py`` 자신은 시계를 전혀
   읽지 않는다(그 모듈 자체 docstring). ``CTR-SB-008``: *최종 답변* 게시(7단계)
   자체가 실패하면 기록의 ``outcome``을 ``"ok"``에서 ``"error"``로 격하한다 --
   어디에도 전달되지 않은 답변이 성공으로 집계되면 절대 안 된다. 전체 근거와
   접수 알림 게시(4단계)가 왜 예외인지는 ``_post_and_finish`` docstring 참고.

``EDGE-SB-013`` -- Claude API 실패/타임아웃(``ask_result.outcome == "error"``)은
``ask_result.client_message``(``ask.py``가 이미 만들어둔 비밀 없는 재시도 안내)를
게시한다 -- 사용자를 접수 알림뿐인 채로 영원히 기다리게 두지 않는다.
``EDGE-SB-008`` -- 이 모듈은 ``ask_claude`` 주변에 자체 재시도 루프를 절대
추가하지 않는다: 이 모듈이 생성하는 클라이언트가 소유한 Anthropic SDK 자체
``max_retries``가 반환 전에 이미 재시도를 마쳤고, ``AskResult.retryable``은
관측 기록용 읽기 전용 정보일 뿐 다시 루프를 돌라는 지시가 아니다.

**이 모듈이 예상하지 못한 예외** -- ``ask_claude``/``post_message`` 자신의
never-raises 계약에 속하지 않는, 진짜 버그이거나 완료 기록을 쓰다가 올라온
``IdempotencyStoreError`` -- 는 ``_process_event``에서 claim-후-처리 블록 전체를
감싸 잡는다: best-effort 범용 실패 안내를 게시하고, 예외를 로그로 남기며(질문/
답변/토큰은 절대 남기지 않음), ``release_inflight_query``는 ``finally``에서
무조건 실행된다. 그렇게 하지 않으면 사용자는 접수 알림만 받은 채 아무 해결 없이
영원히 기다리게 된다 -- 이 태스크의 handover 노트가 "어떤 실패에도 worker가
조용히 죽으면 안 된다"고 못박은 바로 그 요구사항이다.

🔴 SDK DEBUG 로깅 함정(PLAN §1) -- ``handler.py``의 ``_configure_logging``과
동일한 가드를 공유 대신 여기서도 중복 구현한다(PLAN §1이 둘 다 허용하며, 두
진입점 모두 이 불변식을 독립적으로 보장해야 함): ``anthropic._base_client``가
DEBUG에서 요청 본문 전체 -- 사람별 MCP 토큰과 질문 텍스트 포함 -- 를 로깅한다.
이 모듈 자체 로그 호출은 절대 그러지 않는다(아래 "보안" 절 참고) -- 진짜 위험은
SDK 내부 로거이고, 장애 대응용으로 ``SLACKBOT_LOG_LEVEL=DEBUG``를 설정해도 그
로거만은 INFO 위로 올라가면 안 된다.

멘션 토큰 제거(``"<@BOT_USER_ID> question"`` -> ``"question"``)
------------------------------------------------------------------
실제 ``app_mention`` 페이로드의 ``event.text``는 트리거한 멘션을 그대로
포함한다(이 패키지 테스트 스위트가 이미 고정한 공식 예시 페이로드로 확인, 예:
``"<@U0LAN0Z89> is it everything a river should be?"``). 이 Slack 마크업
그대로를 질문에 실어 Claude에 보내는 건 사용자 의도와 무관한 잡음이다 -- Claude가
깔끔한 자연어 질문 대신 자기 자신을 부른 ID 구문을 보게 된다. 이 모듈이
``ask_claude``를 호출하기 전에 ``_strip_bot_mention``으로 이를 제거해, Claude가
받는 질문이 사용자가 실제로 의도한 것과 일치하도록 한다. ``slack/events.py``의
``extract_question_text``는 **원본** 텍스트를 그대로 반환한다(페이로드 파싱은
그 모듈의 몫, ``DSN-SB-007``) -- 제거 자체는 파싱이 아니라 해석이라 여기서 한다
-- 같은 근거를 반대쪽 관점에서 설명한 그 함수 자체 docstring도 참고.

이 태스크가 메꾼 config 공백(``IDEMPOTENCY_TABLE``)
------------------------------------------------------------------
FRD §5.2는 원래 ``IDEMPOTENCY_TABLE``을 handler 전용으로만 scope했다. 위
1·3단계도 필요하다(둘 다 같은 테이블로 ``idempotency.py``를 호출). ``config.py``가
이제 두 역할 모두에 이를 요구한다 -- 전체 근거는 그 모듈 자체 docstring 참고,
``TASK-012``가 ``WORKER_FUNCTION_NAME``으로 이미 한 번 메꾼 것과 같은 형태의
공백이다.

보안(``ask.py``/``slack/client.py``/``observability.py`` 자체 "보안" 절의 반복
-- 이 모듈이 네 가지 비밀이 한 로그 줄에 동시에 섞일 수 있는 유일한 지점이라
불변식을 다시 명시한다)
------------------------------------------------------------------
Anthropic API 키, 사람별 MCP 토큰, Slack 봇 토큰, 질문/답변 원문은 로그 줄, 이
모듈이 만드는 예외 메시지, 이 모듈이 만드는 어떤 객체의 ``repr``/``str``에도
절대 나오면 안 된다. 이 모듈 자체 로그 호출은 ``event_id``, ``user_id``, 상태/
이유 코드, 길이만 남긴다 -- 토큰·질문·답변은 절대 남기지 않는다. 관측 기록
(9단계)은 ``question_len``/``question_sha256``만 싣는다(``AC-SB-007-3``, 질문
자체는 절대 없음) -- ``observability.build_record``가 강제하며 여기서
재구현하지 않는다. 미등록 사용자 거부 문구는 ``identity.py`` 자체 고정 문자열
(``AC-SB-004-3``)을 그대로 쓸 뿐, 이 모듈이 매핑을 알아서 만들어내지 않는다.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, cast

import anthropic
import httpx2
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from .ask import ask_claude
from .config import WorkerSettings, load_worker_settings
from .idempotency import (
    IdempotencyStoreError,
    InflightClaimOutcome,
    claim_inflight_query,
    is_event_completed,
    mark_event_completed,
    release_inflight_query,
)
from .identity import CredentialLookupResult, resolve_credentials
from .observability import Outcome, emit_query_observation
from .slack.client import ACKNOWLEDGEMENT_MESSAGE, PostMessageResult, post_message
from .slack.events import (
    extract_channel,
    extract_event_id,
    extract_question_text,
    extract_reply_target_ts,
    extract_user_id,
)
from .slack.format import apply_response_length_policy

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient

#: ``_make_events_endpoint``가 반환하는 엔드포인트 클로저 타입 -- 같은 가독성
#: 이유로 ``handler.py``의 ``_Endpoint`` 별칭을 그대로 따른다.
_Endpoint = Callable[[Request], Awaitable[Response]]

logger = logging.getLogger(__name__)

#: AWS_LWA_PASS_THROUGH_PATH 자체 기본값 -- 모듈 docstring "worker는 HTTP
#: 트리거가 아니다" 절 참고. TASK-020/TASK-023이 이 상수를 import한다.
EVENTS_PATH: Final[str] = "/events"

#: Dockerfile의 AWS_LWA_READINESS_CHECK_PATH(TASK-020)가 이 경로를 가리켜야 한다.
HEALTHZ_PATH: Final[str] = "/healthz"

#: PLAN §1의 SDK DEBUG 로깅 함정 -- 모듈 docstring 참고.
_NOISY_SDK_LOGGER_NAMES: Final[tuple[str, ...]] = ("anthropic", "httpx2")
_NOISY_SDK_LOGGER_MIN_LEVEL: Final[int] = logging.INFO

#: 모듈 docstring "멘션 토큰 제거" 절 참고. 호출마다 settings가 준 bot_user_id로
#: 포맷한다, 모듈 레벨 고정값이 아니다(봇 자신의 ID는 런타임 값이지 상수가 아님).
_BOT_MENTION_TEMPLATE: Final[str] = r"<@{bot_user_id}>\s*"

#: 이 모듈이 의존하는 모든 문서화된 never-raises 계약(ask_claude/post_message/
#: identity 자체 함수들) 밖에서 예외를 만났을 때 게시한다 -- 모듈 docstring
#: "이 모듈이 예상하지 못한 예외" 절 참고. 실제로 무엇이 깨졌는지에 대한 어떤
#: 정보도 절대 흘리지 않는다.
_GENERIC_ERROR_MESSAGE: Final[str] = (
    "요청을 처리하는 중 예기치 못한 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
)

#: Claude ``stop_reason == "refusal"`` 결과의 관측 reason_code(``AC-SB-005-4``)
#: -- ``identity.py``의 ``"user_unregistered"``/``"user_unidentified"``와
#: 구분해 "Claude가 이 특정 질문을 거절함"과 "이 사람은 애초에 권한이 없었음"을
#: 운영자가 구분할 수 있게 한다 -- 둘 다 현재 observability의
#: ``outcome="denied"`` 버킷을 공유하지만(``CTR-SB-008``은 outcome 값을 3개만
#: 정의, ``_run_query`` 참고).
_REASON_CLAUDE_REFUSAL: Final[str] = "claude_refusal"

#: ``EDGE-SB-015``: 이 이벤트의 유일한 동작이 "수신 확인만, 같은
#: (user_id, thread_ts)의 다른 질의가 이미 실행 중"이었음을 뜻한다 --
#: ``idempotency.py`` 자체 ``InflightClaimReason`` 값(그 모듈 반환 타입
#: 내부용)과는 별개다.
_REASON_COALESCED: Final[str] = "coalesced_in_progress"


def _configure_logging(log_level: str) -> None:
    """root 로거 레벨을 설정한 뒤, 시끄러운 SDK 로거들을 그 위로 다시 고정한다.

    ``handler.py``의 동명 함수와 로직이 동일하다 -- 순서가 왜 중요한지(자체
    레벨 없는 로거는 운영자가 방금 올린 root 레벨을 그대로 상속)는 그 모듈
    docstring 참고. 공유 대신 중복 구현한다(PLAN §1이 둘 다 허용) -- 이 사본은
    어느 진입점이 먼저 로깅을 설정하든 *이* Lambda에 대해 불변식을 보장한다.
    """
    logging.getLogger().setLevel(log_level)
    for name in _NOISY_SDK_LOGGER_NAMES:
        logging.getLogger(name).setLevel(_NOISY_SDK_LOGGER_MIN_LEVEL)


async def _healthz(request: Request) -> Response:
    """GET /healthz -- 인증 없는 LWA readiness probe(TASK-020). handler.py 쌍둥이 함수와 동일."""
    return JSONResponse({"status": "ok"})


def _strip_bot_mention(raw_text: str, *, bot_user_id: str) -> str:
    """``raw_text``에서 ``<@bot_user_id>`` 멘션 토큰을 전부 제거하고 trim한다.

    이 작업이 ``slack/events.py``가 아니라 여기서 일어나는 이유는 모듈
    docstring "멘션 토큰 제거" 절 참고. 첫 등장뿐 아니라 모든 occurrence를
    제거한다 -- 질문 중간에 봇을 다시 멘션하는 경우까지 방어한다.
    """
    pattern = _BOT_MENTION_TEMPLATE.format(bot_user_id=re.escape(bot_user_id))
    return re.sub(pattern, "", raw_text).strip()


def _log_post_failure(result: PostMessageResult, *, event_id: str | None, stage: str) -> None:
    """``EDGE-SB-020``: 게시 실패 시 분류된 이유(메시지 텍스트는 절대 없이)를 로그로 남긴다.

    ``slack/client.py``가 같은 ``reason_code``로 이미 자체 warning을 남기지만,
    이건 worker 자체 기록이다 -- ``stage``(어느 분기가 이 게시를 만들었는지:
    ``"acknowledgement"``, outcome 이름, 또는 ``"unexpected_error"``)와
    ``event_id``로 범위를 좁혀서, 운영자가 worker 로그만 읽고도
    ``slack/client.py`` 로그와 대조하지 않아도 어느 이벤트가 왜 게시에
    실패했는지 알 수 있게 한다.
    """
    if result.ok:
        return
    logger.error(
        "slack post failed (stage=%s, event_id=%s, reason_code=%s, retryable=%s)",
        stage,
        event_id,
        result.reason_code,
        result.retryable,
    )


def _is_completed_or_fail_open(
    event_id: str | None, *, settings: WorkerSettings, client: DynamoDBClient | None
) -> bool:
    """``idempotency.is_event_completed``를 감싸 저장소 장애가 새 이벤트를 조용히 묻지 않게 한다.

    ``IdempotencyStoreError``에서 fail-open(False 반환 -- "완료 안 됨, 진행")
    한다. 반대(fail-closed: 읽을 수 없는 저장소를 "이미 완료"로 취급)는 정상적인
    새 질문을 아무 알림도 없이 조용히 떨어뜨리는 셈이다 -- 이 선택이 감수하는
    드문 중복 게시 위험보다 명백히 더 나쁜 결과라고 이 태스크의 handover 노트가
    못박은 바로 그 경우다("어떤 실패에도 worker가 조용히 죽으면 안 된다").
    """
    if not event_id:
        return False
    try:
        return is_event_completed(event_id, table_name=settings.idempotency_table, client=client)
    except IdempotencyStoreError:
        logger.error(
            "idempotency store unreachable checking completion (event_id=%s) -- "
            "proceeding as not-completed (fail-open)",
            event_id,
            exc_info=True,
        )
        return False


def _claim_inflight_or_fail_open(
    user_id: str | None,
    thread_ts: str | None,
    *,
    settings: WorkerSettings,
    client: DynamoDBClient | None,
) -> InflightClaimOutcome:
    """``idempotency.claim_inflight_query``를 감싸 저장소 장애가 질문을 막지 않게 한다.

    ``IdempotencyStoreError``에서 fail-open(``claimed=True``)한다 --
    ``claim_inflight_query`` 자체의 ``identifiers_missing`` 입장과 동일하다
    (coalescing은 비용 최적화일 뿐 보안 경계가 아니다, 그 함수 자체 docstring
    참고): 저장소를 확인할 수 없으면 조용히 버리는 대신 질문을 통과시킨다.

    반환하는 ``reason="store_error"``(``user_id``/``thread_ts``가 둘 다 있어도
    ``"identifiers_missing"``이 아님)는 운영자가 "저장소 자체가 응답 불가"와
    "이 쌍은 coalescing을 평가할 수 없었을 뿐"을 구분하게 해준다 -- 둘을
    섞으면 진단 퇴행이라는 점은 ``idempotency.InflightClaimReason`` 자체 주석
    참고.
    """
    try:
        return claim_inflight_query(
            user_id, thread_ts, table_name=settings.idempotency_table, client=client
        )
    except IdempotencyStoreError:
        logger.error(
            "idempotency store unreachable claiming in-flight lock (user_id=%s) -- "
            "letting the query proceed (fail-open, coalescing is cost-only)",
            user_id,
            exc_info=True,
        )
        return InflightClaimOutcome(claimed=True, reason="store_error")


def _release_inflight_or_log(
    user_id: str | None,
    thread_ts: str | None,
    *,
    settings: WorkerSettings,
    client: DynamoDBClient | None,
) -> None:
    """best-effort ``release_inflight_query`` -- 어떤 예외도 ``finally``를 빠져나가게 두지 않는다.

    ``_process_event``의 ``try``/``finally``에서 호출된다 -- 이 태스크 자체의
    "가장 중요" 요구사항 참고. 여기서 실패하면 로그만 남기고 절대 raise하지
    않는다.

    🔴 이 모듈의 다른 idempotency 래퍼(``IdempotencyStoreError``만 catch)보다,
    그리고 이 저장소가 보통 따르는 스타일 규칙보다도 더 넓게 bare
    ``Exception``을 catch한다 -- 실수가 아니라 의도적이다: 이 호출은
    *``finally`` 블록 안에서* 실행되는데, Python 자체 의미론상 ``finally``
    실행 중 발생한 예외는 그 위 ``try``에서 이미 전파 중이던 예외(있다면
    ``_run_query`` 자체의 실패)를 **대체**해버린다. ``_process_event``의
    ``except Exception``은 ``try``만 감쌀 뿐 이 ``finally``는 감싸지 않는다 --
    여기서 더 좁은 ``except``가 실패해도 잡아줄 바깥 핸들러가 없다. 그래서
    ``release_inflight_query``/``_release``의 ``IdempotencyStoreError``가 아닌
    버그(bare botocore 예외는 그 계층에서 이미 ``IdempotencyStoreError``로
    변환되므로, 그걸 뚫고 나온 진짜 버그만 이 분기에 닿는다)는 원래 예외를
    지워버리는 동시에 in-flight 락을 ``INFLIGHT_TTL_SECONDS_DEFAULT``(360초)
    동안 풀리지 않은 채로 남긴다 -- 이 태스크가 막으려는 바로 그 "조용히
    죽으면 안 된다" 실패이자, 여전히 그 결과를 만들 수 있는 마지막 남은
    경로다. 로깅(``exc_info=True``, 이 모듈 자체 "보안" 절이 금지하는
    질문/답변/토큰은 절대 없이)이 응답 불가한 저장소나 예상 밖 버그에 대해 이
    함수가 여전히 할 수 있는 전부다 -- 이 호출이 끝내 성공하지 못해도
    ``release_inflight_query`` 자체의 TTL 기반 안전망이 lockout 기간을
    그래도 제한한다.
    """
    try:
        release_inflight_query(
            user_id, thread_ts, table_name=settings.idempotency_table, client=client
        )
    except Exception:
        logger.error(
            "releasing in-flight lock failed unexpectedly (user_id=%s)",
            user_id,
            exc_info=True,
        )


def _safe_mark_completed(
    event_id: str | None, *, settings: WorkerSettings, client: DynamoDBClient | None
) -> None:
    """best-effort ``mark_event_completed`` -- 여기서 실패해도 관측 기록까지 막으면 안 된다.

    이 함수와 짝을 이루는 Slack 게시(``_post_and_finish``)는 이 함수가
    실행되는 시점엔 이미 끝난 뒤다 -- 사용자는 어느 쪽이든 이미 답을 받았다.
    여기서 실패해도 위험은 같은 ``event_id``의 미래 Lambda async-invoke
    재시도에서 중복 게시가 일어날 가능성뿐이지 응답 누락이 아니므로, raise
    대신 로그만 남기고 삼킨다.

    🔴 bare ``Exception``을 catch한다(``_release_inflight_or_log``의 넓힌
    ``except``와 같은 이유 -- 그쪽이 이 규칙의 일반형을 설명한다): 이 함수는
    평범한 흐름(``_post_and_finish``, 이 역시 ``_process_event``의 ``try``
    안이라 더 좁은 ``except``가 여기서 실패해도 한 단계 위에서 잡힘)에서도,
    ``_process_event`` 자체의 ``except Exception as exc:`` 분기에서
    직접(이미 예상 밖 실패 뒤의 최후 수단 호출로, 그 주변에 더 이상의
    ``try``가 없음)도 도달한다. 그 지점에서 ``IdempotencyStoreError``가 아닌
    버그가 올라오면 ``_process_event`` 밖으로, ``_events`` 엔드포인트
    자체에 없는 ``try``/``except``마저 지나쳐, 맨 ASGI 500으로 그대로
    전파된다 -- ``_release_inflight_or_log`` docstring이 설명한 것과 같은
    "worker가 조용히 죽으면 안 된다" 결과가 다른 호출 지점에서 똑같이
    재현되는 것이다.
    """
    if not event_id:
        return
    try:
        mark_event_completed(
            event_id,
            table_name=settings.idempotency_table,
            ttl_seconds=settings.idempotency_ttl_seconds,
            client=client,
        )
    except Exception:
        logger.error(
            "marking event completed failed unexpectedly (event_id=%s)",
            event_id,
            exc_info=True,
        )


async def _post_and_finish(
    *,
    channel: str,
    thread_ts: str | None,
    text: str,
    settings: WorkerSettings,
    http_client: httpx2.AsyncClient | None,
    event_id: str | None,
    idempotency_client: DynamoDBClient | None,
    request_id: str,
    user_id: str | None,
    client_id: str | None,
    question: str,
    outcome: Outcome,
    reason_code: str | None,
    error_kind: str | None,
    started_monotonic: float,
    usage: dict[str, int | None] | None,
) -> None:
    """``text``를 게시하고, ``event_id``를 완료 처리하고, 관측 기록 하나를 낸다.

    ``_process_event``/``_run_query``의 모든 종료 분기가 도달하는 공통 꼬리
    (모듈 docstring 7~9단계). 게시 자체의 성공 여부와 무관하게
    ``mark_event_completed``를 실행한다 -- ``EDGE-SB-020``은 재시도로도 못
    고치는 설정 문제(봇 미초대)라, 이 ``event_id``를 미완료로 남겨서 얻을 게
    없고 Lambda 재시도에서 중복 게시를 피할 이유만 있다.

    ``CTR-SB-008`` 수정(2026-09-14 coordinator follow-up): ``text``가 *최종
    답변*(``outcome == "ok"``, 이 값을 넘기는 유일한 호출부)이고 게시 자체가
    실패했다면 관측 기록은 ``"ok"``라고 하면 안 된다 -- ``outcome``으로
    집계하는 호출자가 채널에 전혀 도달 안 한 답변(``not_in_channel``, 네트워크
    오류 등)을 성공으로 셀 것이고, 그 장애를 알아챌 방법은
    ``REQ-SB-007``이 존재하는 이유인 구조화된 필드를 쿼리하는 대신 자유
    텍스트 로그를 한 줄씩 읽는 것뿐이게 된다. 그래서 호출자가 아니라 이
    함수가 ``outcome="error"``로 격하하고 ``PostMessageResult.reason_code``
    (예: ``"not_in_channel"``)를 ``reason_code``에 싣는다 -- 이 모듈 다른
    곳에서 ``ask_claude`` 실패를 기록하는 방식(Python 예외가 아니라 분류
    문자열 -- ``error_kind``는 실제 예외 클래스 이름 전용으로 남겨둠,
    ``_process_event``의 ``except`` 분기 참고)과 같은 형태다.

    이건 ``_run_query``의 *접수 알림* 게시에는 의도적으로 적용되지
    **않는다** -- 그 호출은 애초에 이 함수에 도달하지 않는다(``ask_claude``가
    실행되기도 전에 게시, 로그, 그리고 잊혀짐). 접수 알림만 실패하고 실제
    답변은 나중에 정상 게시됐다면 사용자는 어쨌든 답을 받은 것 -- 사용자
    관점에서 올바른 결과는 성공이고, ``CTR-SB-008``의 질의당 집계도 그렇게
    말해야 한다.

    ``usage``는 이 게시의 성공 여부와 무관하게 받은 그대로 기록한다 -- Claude
    API 호출(있었다면)은 이 함수가 실행되는 시점에 이미 비용을 치렀고, 비용
    집계는 이 기록의 또 다른 목적(``AC-SB-007-2``)이다; 전달 실패가 그
    지출을 환불해주진 않는다.
    """
    result = await post_message(
        channel=channel,
        text=text,
        bot_token=settings.bot_token,
        thread_ts=thread_ts,
        http_client=http_client,
    )
    _log_post_failure(result, event_id=event_id, stage=outcome)
    _safe_mark_completed(event_id, settings=settings, client=idempotency_client)

    effective_outcome: Outcome = outcome
    effective_reason_code = reason_code
    if outcome == "ok" and not result.ok:
        effective_outcome = "error"
        effective_reason_code = result.reason_code

    duration_ms = int((time.monotonic() - started_monotonic) * 1000)
    emit_query_observation(
        ts=datetime.now(UTC).isoformat(),
        slack_user_id=user_id,
        client_id=client_id,
        channel=channel,
        thread_ts=thread_ts,
        question=question,
        outcome=effective_outcome,
        reason_code=effective_reason_code,
        error_kind=error_kind,
        duration_ms=duration_ms,
        usage=usage,
        request_id=request_id,
    )


async def _run_query(
    *,
    channel: str,
    thread_ts: str | None,
    question: str,
    credential: CredentialLookupResult,
    settings: WorkerSettings,
    anthropic_client: anthropic.AsyncAnthropic,
    http_client: httpx2.AsyncClient | None,
    event_id: str | None,
    idempotency_client: DynamoDBClient | None,
    request_id: str,
    user_id: str | None,
    started_monotonic: float,
) -> None:
    """claim된(coalescing 안 된) 질의의 4~9단계 -- 락을 쥔 뒤에만 도달한다.

    ``credential.mcp_token``은 ``cast``로 non-``None``임을 단언한다 -- 안전한
    이유는 ``_process_event``가 ``credential.granted``가 True임을 확인한
    뒤에만 이 함수를 호출하고, ``CredentialLookupResult``가 ``granted``가
    True일 때 정확히 ``mcp_token``도 설정됨을 보장하기 때문(그 dataclass
    자체 docstring 참고).
    """
    ack_result = await post_message(
        channel=channel,
        text=ACKNOWLEDGEMENT_MESSAGE,
        bot_token=settings.bot_token,
        thread_ts=thread_ts,
        http_client=http_client,
    )
    _log_post_failure(ack_result, event_id=event_id, stage="acknowledgement")

    ask_result = await ask_claude(
        question=question,
        mcp_server_url=settings.mcp_server_url,
        authorization_token=cast(str, credential.mcp_token),
        client=anthropic_client,
    )

    if ask_result.outcome == "error":
        # EDGE-SB-013 / EDGE-SB-008: ask.py가 이미 만든 SDK-재시도-인지 안내를
        # 게시한다 -- ask_claude 주변에 자체 재시도 루프는 없다.
        await _post_and_finish(
            channel=channel,
            thread_ts=thread_ts,
            text=ask_result.client_message or _GENERIC_ERROR_MESSAGE,
            settings=settings,
            http_client=http_client,
            event_id=event_id,
            idempotency_client=idempotency_client,
            request_id=request_id,
            user_id=user_id,
            client_id=user_id,
            question=question,
            outcome="error",
            reason_code=ask_result.reason_code,
            error_kind=None,
            started_monotonic=started_monotonic,
            usage=ask_result.usage,
        )
        return

    if ask_result.outcome == "refused":
        await _post_and_finish(
            channel=channel,
            thread_ts=thread_ts,
            text=ask_result.client_message or _GENERIC_ERROR_MESSAGE,
            settings=settings,
            http_client=http_client,
            event_id=event_id,
            idempotency_client=idempotency_client,
            request_id=request_id,
            user_id=user_id,
            client_id=user_id,
            question=question,
            outcome="denied",
            reason_code=_REASON_CLAUDE_REFUSAL,
            error_kind=None,
            started_monotonic=started_monotonic,
            usage=ask_result.usage,
        )
        return

    answer = apply_response_length_policy(ask_result.answer, settings.max_response_chars)
    await _post_and_finish(
        channel=channel,
        thread_ts=thread_ts,
        text=answer,
        settings=settings,
        http_client=http_client,
        event_id=event_id,
        idempotency_client=idempotency_client,
        request_id=request_id,
        user_id=user_id,
        client_id=user_id,
        question=question,
        outcome="ok",
        reason_code=None,
        error_kind=None,
        started_monotonic=started_monotonic,
        usage=ask_result.usage,
    )


async def _process_event(
    payload: dict[str, Any],
    *,
    settings: WorkerSettings,
    anthropic_client: anthropic.AsyncAnthropic,
    idempotency_client: DynamoDBClient | None,
    http_client: httpx2.AsyncClient | None,
) -> None:
    """Slack 이벤트 하나를 worker ①~⑨단계(모듈 docstring)로 실행한다. 절대 raise하지 않는다."""
    started_monotonic = time.monotonic()
    request_id = uuid.uuid4().hex

    event_id = extract_event_id(payload)
    user_id = extract_user_id(payload)
    channel = extract_channel(payload)
    thread_ts = extract_reply_target_ts(payload)
    question = _strip_bot_mention(
        extract_question_text(payload) or "", bot_user_id=settings.bot_user_id
    )

    if _is_completed_or_fail_open(event_id, settings=settings, client=idempotency_client):
        logger.info("event_id=%s already completed -- skipping (EDGE-SB-005)", event_id)
        return

    if channel is None:
        # 게시할 곳이 없다 -- 목적지를 추측하는 대신 로그만 남기고 버린다.
        logger.error("cannot determine reply channel -- dropping event_id=%s", event_id)
        return

    credential = resolve_credentials(user_id, settings.user_token_map)
    if not credential.granted:
        await _post_and_finish(
            channel=channel,
            thread_ts=thread_ts,
            text=credential.client_message or _GENERIC_ERROR_MESSAGE,
            settings=settings,
            http_client=http_client,
            event_id=event_id,
            idempotency_client=idempotency_client,
            request_id=request_id,
            user_id=user_id,
            client_id=None,
            question=question,
            outcome="denied",
            reason_code=credential.reason_code,
            error_kind=None,
            started_monotonic=started_monotonic,
            usage=None,
        )
        return

    inflight = _claim_inflight_or_fail_open(
        user_id, thread_ts, settings=settings, client=idempotency_client
    )
    if not inflight.claimed:
        await _post_and_finish(
            channel=channel,
            thread_ts=thread_ts,
            text=ACKNOWLEDGEMENT_MESSAGE,
            settings=settings,
            http_client=http_client,
            event_id=event_id,
            idempotency_client=idempotency_client,
            request_id=request_id,
            user_id=user_id,
            client_id=user_id,
            question=question,
            outcome="denied",
            reason_code=_REASON_COALESCED,
            error_kind=None,
            started_monotonic=started_monotonic,
            usage=None,
        )
        return

    try:
        await _run_query(
            channel=channel,
            thread_ts=thread_ts,
            question=question,
            credential=credential,
            settings=settings,
            anthropic_client=anthropic_client,
            http_client=http_client,
            event_id=event_id,
            idempotency_client=idempotency_client,
            request_id=request_id,
            user_id=user_id,
            started_monotonic=started_monotonic,
        )
    except Exception as exc:
        # "어떤 실패에도 worker가 조용히 죽으면 안 된다" -- 모듈 docstring
        # "이 모듈이 예상하지 못한 예외" 절 참고.
        logger.error("unexpected exception while processing event_id=%s", event_id, exc_info=True)
        fallback_result = await post_message(
            channel=channel,
            text=_GENERIC_ERROR_MESSAGE,
            bot_token=settings.bot_token,
            thread_ts=thread_ts,
            http_client=http_client,
        )
        _log_post_failure(fallback_result, event_id=event_id, stage="unexpected_error")
        _safe_mark_completed(event_id, settings=settings, client=idempotency_client)
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        emit_query_observation(
            ts=datetime.now(UTC).isoformat(),
            slack_user_id=user_id,
            client_id=user_id,
            channel=channel,
            thread_ts=thread_ts,
            question=question,
            outcome="error",
            reason_code=None,
            error_kind=type(exc).__name__,
            duration_ms=duration_ms,
            usage=None,
            request_id=request_id,
        )
    finally:
        # ⚠️ 위 try 블록이 어떻게 끝나든(정상 반환, _run_query 내부의 조기 반환,
        # except 분기) 무조건 실행된다 -- 이 태스크의 handover 노트가 "가장
        # 중요"라고 부르는 바로 그 한 줄이다.
        _release_inflight_or_log(user_id, thread_ts, settings=settings, client=idempotency_client)


def _make_events_endpoint(
    settings: WorkerSettings,
    anthropic_client: anthropic.AsyncAnthropic | None,
    idempotency_client: DynamoDBClient | None,
    http_client: httpx2.AsyncClient | None,
) -> _Endpoint:
    """한 ``settings``/client 세트에 대한 ``POST EVENTS_PATH`` 엔드포인트 클로저를 만든다.

    ``anthropic_client``는 요청마다가 아니라 **앱당 한 번** resolve한다 --
    context handover가 요구한 대로 worker가 직접 생성해 주입하는 것 -- warm
    Lambda 컨테이너가 호출 사이에 같은 클라이언트의 커넥션 풀을 재사용하도록,
    ``handler.py``/``idempotency.py``/``slack/client.py``가 각자 클라이언트에
    쓰는 warm-cache 패턴과 동일하다. ``idempotency_client``/``http_client``는
    그대로 요청마다 threaded되는 함수들 내부에서 resolve된다
    (``idempotency.py``/``slack/client.py`` 자체 기본 클라이언트 캐싱이
    프로덕션의 ``None`` 케이스를 이미 처리한다).
    """
    resolved_anthropic_client = (
        anthropic_client
        if anthropic_client is not None
        else anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    )

    async def _events(request: Request) -> Response:
        raw_body = await request.body()
        try:
            parsed_body: object = json.loads(raw_body)
        except json.JSONDecodeError:
            logger.error("worker received a non-JSON payload on the LWA pass-through path")
            return PlainTextResponse("ok")
        if not isinstance(parsed_body, dict):
            logger.error("worker received a JSON payload that was not an object")
            return PlainTextResponse("ok")
        payload = cast(dict[str, Any], parsed_body)

        await _process_event(
            payload,
            settings=settings,
            anthropic_client=resolved_anthropic_client,
            idempotency_client=idempotency_client,
            http_client=http_client,
        )
        return PlainTextResponse("ok")

    return _events


def create_app(
    settings: WorkerSettings | None = None,
    *,
    anthropic_client: anthropic.AsyncAnthropic | None = None,
    idempotency_client: DynamoDBClient | None = None,
    http_client: httpx2.AsyncClient | None = None,
) -> Starlette:
    """``settings``로부터 ``slack-worker`` Lambda용 Starlette 앱 하나를 만든다.

    ``settings`` 기본값은 ``None`` -> ``load_worker_settings``로
    ``os.environ``에서 Fail-Fast 로드한다 -- 덕분에 Dockerfile ``CMD``(worker
    Lambda용, ``TASK-020``/``TASK-023``)가 요구하는 무인자 콜러블
    ``uvicorn devoks_slackbot.worker:create_app --factory``가 된다. 이
    패키지 테스트 스위트의 모든 테스트는 ``settings``를 명시적으로 넘겨 환경
    접근을 완전히 우회한다.

    ``anthropic_client``/``idempotency_client``/``http_client``는 테스트
    전용 주입 지점(실배포는 전부 ``None``): 프로덕션은 ``anthropic_client``를
    ``settings.anthropic_api_key``로 지연 생성하고(앱당 한 번,
    ``_make_events_endpoint`` 참고), 나머지 둘은 ``idempotency.py``/
    ``slack/client.py`` 자체 기본 클라이언트 resolve가 처리한다.

    모듈 레벨 싱글턴이 아니라 팩토리 -- 프로세스당 한 번(또는 테스트에서
    ``WorkerSettings``당 한 번) 호출한다. ``handler.py`` 자체의 "독립적
    인스턴스" 보장과 동일하다.
    """
    resolved_settings = settings if settings is not None else load_worker_settings(os.environ)
    _configure_logging(resolved_settings.log_level)

    return Starlette(
        routes=[
            Route(HEALTHZ_PATH, endpoint=_healthz, methods=["GET"]),
            Route(
                EVENTS_PATH,
                endpoint=_make_events_endpoint(
                    resolved_settings, anthropic_client, idempotency_client, http_client
                ),
                methods=["POST"],
            ),
        ],
    )
