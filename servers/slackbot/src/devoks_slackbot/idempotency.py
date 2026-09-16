"""``event_id`` 조건부 쓰기 기반 idempotency 저장소 (DSN-SB-004, TASK-006).

``REQ-SB-003``: 같은 Slack 이벤트가 두 개의 독립된 경로로 중복 전달될 수 있고, 각각
별도 방어가 필요하다.

- ``EDGE-SB-004``: handler가 ``CTR-SB-002``의 3초 안에 2xx를 못 주면 Slack 자신이
  웹훅을 최대 3회(지수 백오프) 재시도한다. ``claim_event``가 방어선 -- handler는
  작업 시작 전에 호출하고, 경쟁에서 이긴 호출만 worker에 dispatch한다.
- ``EDGE-SB-005``: Lambda **async invoke**(handler -> worker)는 worker 자신의
  호출이 에러/타임아웃하면 Lambda가 자체적으로 2회 더 재시도한다 -- Slack 재시도와
  완전히 독립적인 원인이며 *worker*만 볼 수 있다. ``mark_event_completed``/
  ``is_event_completed``가 worker 측 방어 -- 응답을 올리고 완료를 기록한 뒤, 다시
  올리기 전에 그 기록을 먼저 확인한다.

**``AC-SB-003-2``의 원자성이 이 모듈이 존재하는 이유 그 자체다.** "``event_id``
존재 여부를 확인한 뒤 없으면 쓴다"는 순진한 방식은 두 라운드트립 사이에 틈이 생겨
동시 재시도 둘 다 "없음"을 관측하고 둘 다 진행할 수 있다. ``claim_event``는 절대
그렇게 하지 않는다 -- 조건부 ``PutItem``(``ConditionExpression =
attribute_not_exists(pk)``) 단 한 번으로 DynamoDB 자신이 "누가 경쟁에서 이겼는가"의
단일 진실원천이 되게 한다 -- ``DSN-SB-004``가 (조건부 쓰기가 더 약한) 단순 S3 객체
대신 DynamoDB를 고른 이유가 바로 이것. 이 모듈의 모든 함수는 쓰기 안전성을 판단하기
위한 읽기를 절대 먼저 하지 않는다.

**TTL은 ``event#`` 행에서는 정합성이 아니라 비용 정리 수단이다.** DynamoDB TTL
삭제는 *백그라운드* 스윕이라 AWS 문서상 만료 후 보통 48시간 내 처리되며 즉시가
아니다 -- 만료됐지만 아직 안 쓸린 행도 여전히 읽힌다. ``AC-SB-003-3``이 요구하는
건 TTL 속성을 정확히 쓰는 것(``now + ttl_seconds``, epoch seconds, ``CTR-SB-007``
기본 3,600) 뿐이며, "TTL이 지났다"를 재처리 허용 신호로 쓰라는 요구는 없다 -- 이
모듈 어디서도 그렇게 하지 않는다.

**``inflight#`` 행만은 예외 -- 여기서 TTL은 정리가 아니라 정합성 안전망이다.**
``release_inflight_query``가 주 해제 경로(응답을 올린 직후 성공/실패 무관 명시적
삭제)지만, ``finally``가 실행되기 전에 worker가 죽으면(크래시/OOM/타임아웃) 절대
호출되지 않는다. 짧은 TTL이 없으면 그 사용자는 영원히 coalescing에 막힌다. 이 TTL이
``CTR-SB-007``의 3,600초가 아닌 이유는 ``INFLIGHT_TTL_SECONDS_DEFAULT`` 참고.

**키 스킴은 이벤트 전용이 아니라 재사용 가능하도록 설계했다.** 이 테이블은 단일
속성 설계 -- 파티션 키(``PARTITION_KEY_ATTR``) 하나를 prefix로 네임스페이스
(``TASK-006``은 ``"event#<event_id>"``, ``TASK-007``의 coalescing 락
(``EDGE-SB-015``)은 ``"inflight#<user_id>#<thread_ts>"``). 제네릭 원시 함수
(``_claim``/``_mark_completed``/``_is_completed``/``_release``)는 이미
네임스페이스가 붙은 키 문자열만 받고 "이벤트"나 "진행 중 질의"를 전혀 모른다 --
``TASK-007``은 두 번째 테이블을 만들거나 조건부 쓰기 로직을 중복하는 대신 같은
테이블을 자기 prefix로 재사용한다. 락을 걸 때는 ``_claim``을 그대로 재사용하지만
풀 때는 ``_mark_completed``가 아니라 새 ``_release``를 쓴다 -- coalescing 락은 상태
변경이 아니라 *소멸*해야 하기 때문(위 문단 참고)이며, "완료" 상태가 없으므로
``_mark_completed``/``_is_completed``는 아예 건드리지 않는다.

**boto3 비용 관리(``CTR-SB-002``의 3초 budget).** 실측: ``import boto3`` 385ms,
클라이언트 생성 82ms 추가(workspace PLAN §1). 385ms는 handler가 어차피 boto3를
쓰므로 피할 수 없지만, 82ms는 호출마다 치르지 않는다 -- 모든 공개 함수가 옵션
``client``를 받고(테스트는 ``moto`` 클라이언트를 주입), 없으면 ``_resolve_client``가
**최초 사용 시점에 지연 생성**해 모듈 전역에 캐싱한다 -- warm Lambda 컨테이너가
호출마다 새로 만들지 않고 재사용하도록. 이 모듈 어디서도 import 시점에
``boto3.client(...)``를 호출하지 않는다.

Import budget: ``boto3``만(handler-safe, ``DSN-SB-008`` -- 이 모듈은 ``anthropic``을
절대 import하면 안 된다).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from .config import IDEMPOTENCY_TTL_SECONDS_DEFAULT

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient

logger = logging.getLogger(__name__)

#: 공개: 인프라 프로비저닝(DynamoDB 테이블 자체, ``RES-SB-API-004``)과 테스트 모두
#: 이 모듈이 쓰고 읽는 속성명과 정확히 일치해야 한다 -- 호출부마다 하드코딩하지
#: 않도록 여기를 SSOT로 둔다.
PARTITION_KEY_ATTR = "pk"
TTL_ATTRIBUTE = "ttl"

#: ``EDGE-SB-015`` coalescing 락 TTL -- 의도적으로 ``IDEMPOTENCY_TTL_SECONDS_DEFAULT``
#: (``CTR-SB-007``의 3,600초)를 재사용하지 않는다. 그 값은 *중복 전달*이 얼마나
#: 오래 이어질 수 있는지(Slack/Lambda 재시도 윈도)를 위한 값이지, 질의 하나가
#: 정상적으로 얼마나 오래 걸려도 되는지와는 무관하다. coalescing 락은 대신
#: ``CTR-SB-009``의 worker 실행 제한(300초) + 여유분 안에서 만료돼야 한다 --
#: worker는 정상 종료 시 ``try/finally``에서 ``release_inflight_query``를
#: 호출하므로, 이 TTL은 worker가 거기까지 못 간 경우(크래시, OOM, 강제 타임아웃)에만
#: 의미가 있다. 3,600초를 그대로 쓰면 worker가 질의 도중 죽을 때마다 해당 사용자가
#: 최대 **한 시간** 동안 새 질문을 못 하게 되는데, 이 락의 목적은 비용 최적화일
#: 뿐 보안/정합성 경계가 아니므로 그 결과는 막으려는 중복 처리 비용보다 명백히 더
#: 나쁘다. 300초(worker 제한) + 60초(worker의 실제 죽음과 재시도 차단 시점 사이
#: Lambda 스케줄링/네트워크 지연 여유) = 360초.
INFLIGHT_TTL_SECONDS_DEFAULT = 360

#: 내부 전용: 항목의 상태. 인프라는 절대 읽지 않고 이 모듈의 claim/completion
#: 로직만 읽는다.
_STATUS_ATTR = "status"
_STATUS_CLAIMED = "claimed"
_STATUS_COMPLETED = "completed"

#: ``TASK-006``의 키 네임스페이스. 새 키 종류(예: ``TASK-007``의 in-flight 락)는
#: 자기 prefix + `_xxx_key()` 헬퍼를 옆에 추가한다 -- 아래 제네릭 원시 함수는 키
#: 형태로 절대 분기하지 않는다.
_EVENT_KEY_PREFIX = "event"


def _event_key(event_id: str) -> str:
    return f"{_EVENT_KEY_PREFIX}#{event_id}"


#: ``TASK-007``의 키 네임스페이스(``EDGE-SB-015``). ``_EVENT_KEY_PREFIX``와 다른
#: prefix라 in-flight 락과 event claim은 원본 id/ts 값이 우연히 같아도 절대
#: 충돌하지 않는다.
_INFLIGHT_KEY_PREFIX = "inflight"


def _inflight_key(user_id: str, thread_ts: str) -> str:
    # "#"는 `user_id`/`thread_ts` 구분자(그리고 prefix 뒤)로 안전하다: Slack user ID는
    # `[A-Z0-9]+`(CTR-SB-006, 예: "U01ABCDEF")이고 `thread_ts`/`ts`는
    # `<digits>.<digits>`(Slack 메시지 타임스탬프 형식)라 어느 쪽 문자셋도 "#"을
    # 포함할 수 없다 -- 서로 다른 (user_id, thread_ts) 쌍이 같은 키 문자열로 겹칠
    # 수 없다.
    return f"{_INFLIGHT_KEY_PREFIX}#{user_id}#{thread_ts}"


class IdempotencyStoreError(Exception):
    """예상된 조건부 체크 결과가 아닌 다른 이유로 DynamoDB 호출이 실패함
    (테이블 없음, throttling, 네트워크 오류 등).

    이 모듈의 모든 공개 함수는 원본 ``botocore`` 예외 대신 이 타입 하나만
    raise한다 -- 호출자(``handler.py``/``worker.py``)가 botocore 예외 계층을
    몰라도 타입 하나만 잡아 degrade 방식을 결정할 수 있다. 덕분에 idempotency
    저장소 자체가 응답 불가여도 handler는 ``AC-SB-002-3``("비동기 dispatch
    실패해도 200 유지")을 지킬 수 있다.
    """


#: 운영자 진단 전용(identity.py의 reason_code 패턴과 동일) -- 이 모듈은 `reason`을
#: Slack 대상 호출자에게 절대 노출하지 않는다.
#:
#: "store_error"는 이 모듈의 ``IdempotencyStoreError``를 catch한 *호출자*
#: (``handler.py``의 ``_claim_or_fail_safe``)가 ``ClaimOutcome``을 구성할 때 쓰는
#: 예약값이다 -- ``claim_event`` 자신은 절대 이 값을 반환하지 않는다(저장소 실패는
#: 항상 raise되지, 반환되지 않는다). ``"missing_event_id"``와 섞으면 안 된다:
#: 그 값은 ``event_id``가 애초에 못 쓰는 값이라 DynamoDB 호출 자체를 안 했다는
#: 뜻으로, "저장소가 응답 안 함"과는 원인·대응이 완전히 다른 데이터 품질 신호다.
#: 저장소 장애를 ``"missing_event_id"``로 재사용하면 실제 DynamoDB 장애가
#: 대시보드/메트릭에서 "id 없는 이벤트가 계속 들어옴"으로 위장된다.
ClaimReason = Literal["claimed", "duplicate", "missing_event_id", "store_error"]


@dataclass(frozen=True, slots=True)
class ClaimOutcome:
    """``claim_event`` 호출 1회의 결과.

    ``claimed=True``: 이 호출이 ``event_id`` 경쟁의 유일한 승자
    (``AC-SB-003-1``, ``AC-SB-003-2``) -- 호출자는 작업을 dispatch해야 한다.
    ``claimed=False``: 작업을 시작하면 안 되며, 이유는 운영자 로그 구분용일 뿐
    호출자 분기용이 아니다 -- ``"duplicate"``(``event_id``가 이미 claim됨, Slack
    재시도, ``EDGE-SB-004``), ``"missing_event_id"``(``event_id``가 ``None``/빈
    값이라 DynamoDB 호출 자체를 안 함), ``"store_error"``(``IdempotencyStoreError``를
    catch한 호출자가 구성 -- ``ClaimReason`` 주석 참고, ``claim_event`` 자신은 이
    값을 절대 생성하지 않는다).
    """

    claimed: bool
    reason: ClaimReason


#: 운영자 진단 전용, 위 ``ClaimReason``과 같은 취지지만 ``claim_inflight_query``
#: 전용 -- ``ClaimReason``을 재사용하지 않고 별도 타입인 이유는 의미가 실제로
#: 다르기 때문이다: ``"in_progress"``는 중복 전달이 아니라 coalescing 차단(같은
#: (user, thread)의 다른 질의가 아직 실행 중)이고, ``"identifiers_missing"``은
#: 차단이 아니라 "진행 허용"(``claimed=True``)을 뜻한다 -- ``ClaimReason``의
#: ``"missing_event_id"``와 정반대 극성.
#:
#: "store_error"는 ``ClaimReason``과 같은 이유로 존재한다(``IdempotencyStoreError``를
#: catch한 호출자 -- ``worker.py``의 ``_claim_inflight_or_fail_open`` -- 전용
#: 예약값, ``claim_inflight_query`` 자신은 반환하지 않음). 전용 값이 없으면 그
#: 호출자는 ``"identifiers_missing"``(단순히 쌍이 못 쓰는 값이었다는 뜻이지
#: 저장소 실패가 아님)이나 ``"in_progress"``(응답 불가한 저장소가 확인해줄 수
#: 없는데도 다른 질의가 *실행 중임을 안다*는 뜻)를 재사용해야 하는데, 둘 다 실제
#: DynamoDB 장애를 무관하고 무해해 보이는 라벨 뒤에 숨긴다.
InflightClaimReason = Literal["claimed", "in_progress", "identifiers_missing", "store_error"]


@dataclass(frozen=True, slots=True)
class InflightClaimOutcome:
    """``claim_inflight_query`` 호출 1회의 결과(``EDGE-SB-015``).

    ``claimed=True``: 이 (user_id, thread_ts)에 대해 다른 질의가 진행 중이 아니다
    -- 호출자는 진행하고, 반드시 같은 쌍으로 ``release_inflight_query``를 호출해야
    한다. ``claimed=False``(``reason="in_progress"``): 같은 쌍의 다른 질의가 아직
    실행 중 -- 호출자는 새 작업 없이 수신만 확인해야 한다.
    ``reason="identifiers_missing"``은 항상 ``claimed=True``와 짝을 이룬다 --
    coalescing은 비용 최적화일 뿐 보안 경계가 아니므로 ``user_id``/``thread_ts``가
    없어 coalescing을 평가할 수 없을 때 정상 질의를 막으면 안 된다.
    ``reason="store_error"``도 같은 fail-open 이유로 항상 ``claimed=True``와
    짝이지만, ``IdempotencyStoreError``를 catch한 호출자만 구성한다 --
    ``claim_inflight_query`` 자신은 이 값을 절대 생성하지 않는다.
    """

    claimed: bool
    reason: InflightClaimReason


_default_client: DynamoDBClient | None = None


def _resolve_client(client: DynamoDBClient | None) -> DynamoDBClient:
    """``client``가 주어지면 그대로, 아니면 지연 생성·warm 캐시된 기본값을 반환.

    ``None``(프로덕션 호출)만 캐싱 분기를 탄다 -- 테스트는 항상 ``moto`` 클라이언트를
    명시적으로 주입하므로 이 모듈 전역 캐시를 건드리거나 의존하지 않는다.
    """
    global _default_client
    if client is not None:
        return client
    if _default_client is None:
        _default_client = boto3.client("dynamodb")  # pyright: ignore[reportUnknownMemberType]
    return _default_client


def claim_event(
    event_id: str | None,
    *,
    table_name: str,
    ttl_seconds: int = IDEMPOTENCY_TTL_SECONDS_DEFAULT,
    client: DynamoDBClient | None = None,
) -> ClaimOutcome:
    """``event_id``를 원자적으로 claim한다(``REQ-SB-003``, ``AC-SB-003-1``).

    ``event_id``는 ``slack/events.py``의 ``extract_event_id``가 이미 반환한 값 --
    ``None``/빈 값도 그대로 넘기면 된다. 이 함수는 ``event_id``가 없으면 DynamoDB
    호출 없이 자동으로 unclaimable(``ClaimOutcome(claimed=False,
    reason="missing_event_id")``) 처리한다 -- 이 판단을 호출자에게 떠넘기지 않는다.

    조건부 ``PutItem`` 단 한 번으로 결과가 결정된다(``AC-SB-003-2``): 사전 읽기가
    없으므로 같은 ``event_id``에 대한 동시 호출 둘 다 성공할 수 없다 -- DynamoDB가
    패자를 ``ConditionalCheckFailedException``으로 거부하며, 이 함수는 그걸 raise
    대신 단순 ``claimed=False``로 변환한다.

    그 외 모든 DynamoDB 실패(테이블 없음, throttling, 네트워크 오류 등)는
    ``IdempotencyStoreError``를 raise한다.
    """
    # 공백만 있는 값도 "없음"으로 취급한다. ``extract_event_id``는 공백만 있는 값도
    # 그대로 통과시키는데(non-empty ``str``이므로), 이 체크가 없으면 빈 id가 저장
    # 키 ``event#   ``가 돼버려 형식이 잘못된 이벤트들이 전부 그 한 행에서 충돌하고
    # 첫 번째를 제외하곤 "중복"으로 버려진다. Slack이 이런 id를 보내진 않지만, 위
    # 가드는 못 쓰는 값을 거부하기 위함이고 빈 id도 같은 기준으로 못 쓰는 값이다.
    if not event_id or not event_id.strip():
        logger.warning("claim_event called with no usable event_id — treating as unclaimable")
        return ClaimOutcome(claimed=False, reason="missing_event_id")

    resolved_client = _resolve_client(client)
    claimed = _claim(resolved_client, table_name, _event_key(event_id), ttl_seconds)
    if claimed:
        return ClaimOutcome(claimed=True, reason="claimed")

    logger.info("duplicate event_id claim rejected (event_id=%s)", event_id)
    return ClaimOutcome(claimed=False, reason="duplicate")


def mark_event_completed(
    event_id: str,
    *,
    table_name: str,
    ttl_seconds: int = IDEMPOTENCY_TTL_SECONDS_DEFAULT,
    client: DynamoDBClient | None = None,
) -> None:
    """``event_id``의 응답이 이미 Slack에 게시됐음을 기록한다(``EDGE-SB-005``).

    worker가 Slack 게시를 마치면 이 함수를 호출한다 -- 그러면 같은 ``event_id``의
    재시도된 async invoke(실패/타임아웃 호출에 대한 Lambda 자체 재시도 2회,
    handler 측 ``claim_event``가 볼 수 없는 독립적 중복 원인)가 다시 올리기 전에
    ``is_event_completed``로 먼저 확인해 건너뛸 수 있다.

    ``event_id``가 없거나 빈 값이면 방어적으로 no-op(``claim_event``와 동일 처리,
    DynamoDB 호출 없음).

    DynamoDB 실패 시 ``IdempotencyStoreError``를 raise한다.
    """
    if not event_id:
        return
    _mark_completed(_resolve_client(client), table_name, _event_key(event_id), ttl_seconds)


def is_event_completed(
    event_id: str,
    *,
    table_name: str,
    client: DynamoDBClient | None = None,
) -> bool:
    """``mark_event_completed(event_id, ...)``가 이미 실행됐으면 True.

    ``EDGE-SB-005``의 worker 측 체크 -- Claude API 작업이나 응답 게시 전에
    호출한다. ``event_id``가 없거나 빈 값이면 False(raise하지 않음),
    ``claim_event``로만 claim되고 아직 completed 안 된 경우도 False -- 둘 다
    "진행해도 안전"을 뜻한다.

    DynamoDB 실패 시 ``IdempotencyStoreError``를 raise한다.
    """
    if not event_id:
        return False
    return _is_completed(_resolve_client(client), table_name, _event_key(event_id))


def claim_inflight_query(
    user_id: str | None,
    thread_ts: str | None,
    *,
    table_name: str,
    ttl_seconds: int = INFLIGHT_TTL_SECONDS_DEFAULT,
    client: DynamoDBClient | None = None,
) -> InflightClaimOutcome:
    """(``user_id``, ``thread_ts``) 쌍을 "진행 중"으로 원자적으로 claim한다(``EDGE-SB-015``).

    ``user_id``/``thread_ts``는 ``slack/events.py``의 ``extract_user_id``/
    ``extract_reply_target_ts``가 이미 반환한 값을 그대로 넘기면 된다. **둘 중
    하나라도 ``None``/빈 값이면 "coalescing을 평가할 수 없다"는 뜻이지 "차단"이
    아니다.** coalescing은 순전히 같은 사용자의 반복 멘션에 대한 중복 Claude API
    비용을 아끼기 위한 것 -- 보안 경계가 아니다 -- 그래서 식별자 중 하나라도
    못 쓰면 DynamoDB 호출 없이 항상 질의를 진행시킨다
    (``InflightClaimOutcome(claimed=True, reason="identifiers_missing")``).
    정상 질문을 조용히 버리지 않기 위함이다.

    그 외에는 조건부 ``PutItem`` 단 한 번(``claim_event``와 동일한 ``_claim``
    원시 함수)으로 결과가 결정된다 -- 같은 사용자가 같은 스레드에서 동시에
    멘션해도 둘 다 이길 수 없다.

    ``claimed=True``를 받은 호출자는 **반드시** 같은 ``user_id``/``thread_ts``로
    ``release_inflight_query``를 나중에 호출해야 한다(보통 응답 게시 작업을 감싼
    ``try``/``finally``에서) -- 호출하지 않으면 어떻게 되는지는
    ``INFLIGHT_TTL_SECONDS_DEFAULT`` 참고.

    그 외 모든 DynamoDB 실패는 ``IdempotencyStoreError``를 raise한다.
    """
    if not user_id or not user_id.strip() or not thread_ts or not thread_ts.strip():
        logger.info("coalescing skipped — user_id or thread_ts unavailable, letting query proceed")
        return InflightClaimOutcome(claimed=True, reason="identifiers_missing")

    resolved_client = _resolve_client(client)
    claimed = _claim(resolved_client, table_name, _inflight_key(user_id, thread_ts), ttl_seconds)
    if claimed:
        return InflightClaimOutcome(claimed=True, reason="claimed")

    logger.info(
        "in-flight query already exists (user_id=%s, thread_ts=%s) — coalescing", user_id, thread_ts
    )
    return InflightClaimOutcome(claimed=False, reason="in_progress")


def release_inflight_query(
    user_id: str | None,
    thread_ts: str | None,
    *,
    table_name: str,
    client: DynamoDBClient | None = None,
) -> None:
    """``claim_inflight_query``가 이전에 claim한 (``user_id``, ``thread_ts``) 쌍을 해제한다.

    worker는 응답 게시 후(성공/실패 무관) ``try``/``finally``에서 이 함수를
    호출한다 -- 그래야 같은 (user_id, thread_ts)의 *다음* 멘션이 coalescing에
    막히지 않는다. 이건 ``mark_event_completed``처럼 상태를 바꾸는 게 아니라
    단순 삭제다: ``claim_inflight_query``의 ``_claim``은 키가 *없을 때만*
    (``attribute_not_exists``) 성공하므로, 행을 "완료" 상태로 남겨두면 그
    (user_id, thread_ts) 쌍이 영구히 잠긴다.

    ``user_id``/``thread_ts``가 없거나 빈 값이면 no-op(``claim_inflight_query``와
    동일 처리 -- 애초에 claim된 게 없으니 해제할 것도 없음)이고, 같은 쌍에 두 번
    이상 호출해도 안전하다(이미 없는 키를 삭제하는 건 DynamoDB에서 에러가 아니라
    성공적인 no-op).

    DynamoDB 실패 시 ``IdempotencyStoreError``를 raise한다.
    """
    if not user_id or not user_id.strip() or not thread_ts or not thread_ts.strip():
        return
    _release(_resolve_client(client), table_name, _inflight_key(user_id, thread_ts))


# --- 제네릭, 키 스킴 비의존 원시 함수 --------------------------------------------
# TASK-007(EDGE-SB-015의 in-flight coalescing 락)은 자기 키 prefix 아래서
# `_claim`을 그대로 재사용(락)하고 `_release`를 추가(해제)한다 -- 아래 함수들은
# "이벤트"나 "진행 중 질의"가 뭔지 전혀 모른다.


def _claim(client: DynamoDBClient, table_name: str, key: str, ttl_seconds: int) -> bool:
    """조건부 ``PutItem`` 한 번. True = 새로 claim됨, False = ``key``가 이미 존재.

    이 단일 라운드트립 자체가 ``AC-SB-003-2``의 원자성 보장이다 -- 모듈 docstring
    참고. 읽기 후 쓰기로 절대 분리하지 않는다.
    """
    try:
        client.put_item(
            TableName=table_name,
            Item={
                PARTITION_KEY_ATTR: {"S": key},
                _STATUS_ATTR: {"S": _STATUS_CLAIMED},
                TTL_ATTRIBUTE: {"N": str(_epoch_ttl(ttl_seconds))},
            },
            ConditionExpression=f"attribute_not_exists({PARTITION_KEY_ATTR})",
        )
    except client.exceptions.ConditionalCheckFailedException:
        return False
    except (BotoCoreError, ClientError) as exc:
        raise IdempotencyStoreError(f"claim failed for table {table_name!r}") from exc
    return True


def _mark_completed(client: DynamoDBClient, table_name: str, key: str, ttl_seconds: int) -> None:
    """``key``의 상태를 completed로 설정. ``_claim``이 실행된 적 없어도 항목을 새로 만든다.

    의도적으로 무조건 쓰기(``ConditionExpression`` 없는 단순 ``UpdateExpression``):
    ``_claim``과 달리 이건 호출자가 원자적으로 결정해야 할 경쟁이 아니다 -- 같은
    ``key``로 여러 번 호출해도 무해한 덮어쓰기 no-op이며, 그 덕에 재시도된 worker
    invocation이 다시 호출해도 안전하다.
    """
    try:
        client.update_item(
            TableName=table_name,
            Key={PARTITION_KEY_ATTR: {"S": key}},
            UpdateExpression="SET #status = :status, #ttl = :ttl",
            ExpressionAttributeNames={"#status": _STATUS_ATTR, "#ttl": TTL_ATTRIBUTE},
            ExpressionAttributeValues={
                ":status": {"S": _STATUS_COMPLETED},
                ":ttl": {"N": str(_epoch_ttl(ttl_seconds))},
            },
        )
    except (BotoCoreError, ClientError) as exc:
        raise IdempotencyStoreError(f"mark_completed failed for table {table_name!r}") from exc


def _is_completed(client: DynamoDBClient, table_name: str, key: str) -> bool:
    try:
        response = client.get_item(TableName=table_name, Key={PARTITION_KEY_ATTR: {"S": key}})
    except (BotoCoreError, ClientError) as exc:
        raise IdempotencyStoreError(f"is_completed check failed for table {table_name!r}") from exc

    item = response.get("Item")
    if item is None:
        return False
    status_value = item.get(_STATUS_ATTR)
    if status_value is None:
        return False
    return status_value.get("S") == _STATUS_COMPLETED


def _release(client: DynamoDBClient, table_name: str, key: str) -> None:
    """``key``를 무조건 삭제해 이후 ``_claim``이 다시 이길 수 있게 한다.

    의도적으로 무조건 삭제(``ConditionExpression`` 없음): 이미 없는 키를 삭제하는
    건 DynamoDB에서 성공적인 no-op이라, 같은 키에 여러 번 호출해도 안전하다
    (항목이 이미 삭제된 뒤 실행되는 ``try``/``finally`` 해제, 또는 경쟁하는 두
    해제 호출).
    """
    try:
        client.delete_item(TableName=table_name, Key={PARTITION_KEY_ATTR: {"S": key}})
    except (BotoCoreError, ClientError) as exc:
        raise IdempotencyStoreError(f"release failed for table {table_name!r}") from exc


def _epoch_ttl(ttl_seconds: int) -> int:
    """``AC-SB-003-3``/``CTR-SB-007``: TTL은 항상 ``now + ttl_seconds``, epoch
    seconds, Number 타입으로 쓴다 -- 기간(duration)이나 다른 단위로는 절대 쓰지
    않는다."""
    return int(time.time()) + ttl_seconds
