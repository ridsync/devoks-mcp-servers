"""``slack-handler`` Lambda 진입점의 ASGI composition root (TASK-012).

``create_app(...) -> Starlette``는 Dockerfile ``CMD``가 ``uvicorn
devoks_slackbot.handler:create_app --factory``로 직접 호출하는 **팩토리**(TASK-020) --
``servers/management``와 달리 별도 ``create_app_from_env``가 없다: ``settings``를 안
주면 ``create_app`` 자신이 ``os.environ``에서 Fail-Fast 로드한다
(``config.load_handler_settings``). 모듈을 import만 하는 것으로는 환경을 읽거나
``ConfigError``를 던지지 않는다 -- ``settings`` 없이 ``create_app()``을 *호출*할 때만
그렇다(``devoks_mcp_management.app``과 동일한 module-import-safety 원칙).

처리 순서(FRD §4.1 handler ①~⑤, §5.4 상태표, 그리고 이 워크스페이스 handover가 3단계로
추가한 봇 self-message 체크) -- **절대 재배치 금지**:

1. **서명 검증**(``slack/signature.py``). 실패 시 **401**, body는 파싱 전에 검증한다
   (``EDGE-SB-001``: 파싱 후 검증하면 이미 신뢰하지 않은 입력을 신뢰한 셈). body를 raw
   bytes로 읽어(``await request.body()``) 파싱/재직렬화 없이 그대로
   ``verify_slack_signature``에 넘기는 이유도 같다 -- ``CTR-SB-001``이 Slack이 서명한
   정확한 바이트를 요구하므로, 파싱 후 재직렬화하면 key 순서/공백이 바뀌어 검증이 조용히
   깨진다.
2. ``type == "url_verification"`` -> 서명 검증 통과 **후에만** ``challenge`` 응답
   (``AC-SB-001-6``, ``EDGE-SB-003``) -- 먼저 응답하면 누구나 이 엔드포인트를 무료 "URL
   생존 확인" 오라클로 악용할 수 있다.
3. 봇 self-message -> 200, 무처리(``EDGE-SB-011``, ``slack/events.py``의
   ``is_bot_self_message``) -- idempotency/dispatch 작업 시작 전에 체크.
4. **Idempotency claim**(``idempotency.claim_event``). 중복 ``event_id``(또는 어떤
   이유로든 claim 실패 -- ``IdempotencyStoreError`` 포함, ``_claim_or_fail_safe`` 참고)면
   무처리지만 응답은 그대로 200(``AC-SB-003-1``). ``x-slack-retry-num`` 헤더(대소문자
   무관 조회)가 있으면 claim 결과와 무관하게 warning 로그 -- 존재 자체가 Slack의 3초
   대기가 이미 한 번 지났다는 뜻(``EDGE-SB-004``).
5. **비동기 dispatch** -- ``boto3`` Lambda ``Invoke(InvocationType="Event")``로
   ``slack-worker``를 깨운다(``DSN-SB-001``). 전달되는 ``Payload``는 **원본, 미파싱**
   Slack body 그대로 -- worker의 LWA pass-through 경로가 동일한 ``slack/events.py``
   함수로 파싱할 수 있도록.
6. **즉시 200** -- step 5의 성공 여부와 무관하게 항상(``AC-SB-002-3``: dispatch 실패는
   로그만 남기고 절대 non-2xx로 바꾸지 않는다 -- 5xx는 Slack의 재시도·중복만 유발할 뿐
   아무것도 회복하지 못한다).

``AC-SB-002-2`` -- 이 모듈은 Claude API나 MCP 서버를 절대 호출하지 않는다. 아래
``DSN-SB-008``이 import 수준에서 이를 보장한다.

🔴 ``DSN-SB-008`` -- **이 모듈의 import 그래프에 ``anthropic``이 절대 섞이면 안 된다**
(PLAN §1: import 비용 실측 1,384ms, ``CTR-SB-002`` 3초 budget의 46%). 즉 ``ask.py``나
``worker.py``를 import하면 안 된다(``ask.py``가 ``anthropic``을 직접 import) --
``TASK-013``이 서브프로세스 격리 테스트로 이 불변식을 고정한다(``sys.modules``는 단일
pytest 프로세스 안에서 누적되므로 in-process 체크는 다른 테스트 모듈의 import에 오염될
수 있음); 이 모듈 자체 테스트(``test_handler.py``)도 같은 이유로 재확인한다.

🔴 서드파티 SDK 로거 고정(PLAN §1 "SDK DEBUG 로깅 함정") -- 이 모듈이 ``anthropic``/
``httpx2``를 import하지 않아도, ``_configure_logging``이 이름으로(``logging.
getLogger("anthropic")``는 패키지를 import하지 않고도 로거 객체를 생성/조회한다) 두
로거를 INFO 이상으로 고정한다. 죽은 코드가 아니라 의도적 방어: SDK 로그 레벨 설정은
진입점 공통 책임이며, worker.py(TASK-014)도 동일 가드가 필요하다
(``anthropic._base_client``가 DEBUG에서 요청 본문 전체 -- 사람별 MCP 토큰 포함 --를
로깅하기 때문). 여기서도 고정해두면 어느 Lambda 진입점이 먼저 로깅을 설정하든 "이 두
로거는 INFO를 넘지 않는다"는 불변식이 유지되고, 아무도 안 쓰는 로거라 비용도 0이다.

이 태스크가 메꾼 config 공백(``WORKER_FUNCTION_NAME``)
------------------------------------------------------------------
FRD §5.2 환경변수 표에 worker Lambda 식별자 키가 없었지만, ``AC-SB-002-1``/
``DSN-SB-001``이 실제 invoke를 요구한다. ``config.py``(TASK-002, 이 태스크의 명목상
``file:`` 범위 밖이지만 필요한 최소 확장)가 이제 handler 역할에
``WORKER_FUNCTION_NAME``을 ``IDEMPOTENCY_TABLE``과 함께 요구한다.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any, Final, Protocol, cast

import boto3
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from .config import HandlerSettings, load_handler_settings
from .idempotency import ClaimOutcome, IdempotencyStoreError, claim_event
from .slack.events import (
    extract_challenge,
    extract_event_id,
    is_bot_self_message,
    is_url_verification,
)
from .slack.signature import verify_slack_signature

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient

#: ``_make_slack_events_endpoint``가 반환하는 엔드포인트 클로저 타입 -- 시그니처
#: 가독성을 위해 이름을 붙였다.
_Endpoint = Callable[[Request], Awaitable[Response]]

logger = logging.getLogger(__name__)

#: TASK-024(API Gateway route)가 문자열을 하드코딩하지 않고 이 상수를 import한다 --
#: 둘이 어긋날 수 없게.
SLACK_EVENTS_PATH: Final[str] = "/slack/events"

#: Dockerfile ``AWS_LWA_READINESS_CHECK_PATH``(TASK-020)가 이 경로를 가리켜야
#: 한다 -- 없으면 Lambda Web Adapter가 컨테이너를 준비 완료로 보지 않아 트래픽이
#: 전달되지 않는다.
HEALTHZ_PATH: Final[str] = "/healthz"

#: EDGE-SB-004. ``Headers.get``으로 대소문자 무관 조회(모듈 docstring step 4) --
#: 상수 자체는 한 가지 표기만 있으면 된다.
_RETRY_NUM_HEADER: Final[str] = "x-slack-retry-num"

#: PLAN §1의 SDK DEBUG 로깅 함정 -- 모듈 docstring 참고.
_NOISY_SDK_LOGGER_NAMES: Final[tuple[str, ...]] = ("anthropic", "httpx2")
_NOISY_SDK_LOGGER_MIN_LEVEL: Final[int] = logging.INFO


class WorkerInvoker(Protocol):
    """이 모듈이 필요로 하는 boto3 Lambda 클라이언트 기능 1개(step 5).

    ``mypy_boto3_lambda``를 import하는 대신 구조적 ``Protocol``로 정의
    (``observability.py``의 ``ObservationStream``과 동일 패턴) -- 그 스텁
    패키지는 dev 의존성에 없고(``idempotency.py``용 ``boto3-stubs[dynamodb]``만
    있음), 메서드 시그니처 하나 때문에 의존성을 추가할 이유가 없다. 실제
    ``boto3.client("lambda")``가 구조적으로 이 Protocol을 만족한다. 테스트는
    실배포 패키지가 필요한 ``moto``의 무거운 Lambda 모킹 대신 가벼운 fake를
    주입한다 -- ``idempotency.py``와 달리(그쪽 모듈 docstring 참고) 조건부 쓰기
    같은 의미론적 미묘함이 없어 그 비용을 치를 이유가 없다.
    """

    def invoke(
        self, *, FunctionName: str, InvocationType: str, Payload: bytes
    ) -> Mapping[str, Any]: ...


_default_lambda_client: WorkerInvoker | None = None


def _resolve_lambda_client(client: WorkerInvoker | None) -> WorkerInvoker:
    """``client``가 주어지면 그대로, 아니면 지연 생성·warm 캐시된 기본값을 반환.

    ``idempotency.py``의 ``_resolve_client``와 동일 패턴·동일 이유 --
    ``boto3.client(...)`` 생성 비용 ~82ms(PLAN §1)는 warm Lambda 컨테이너가
    호출마다가 아니라 한 번만 치러야 한다. 캐싱 분기는 프로덕션 호출(``None``)만
    타며, 테스트는 항상 fake ``WorkerInvoker``를 명시적으로 주입한다.
    """
    global _default_lambda_client
    if client is not None:
        return client
    if _default_lambda_client is None:
        # mypy_boto3_lambda 스텁이 없어(WorkerInvoker docstring 참고)
        # boto3.client("lambda")가 여기서 unknown 타입으로 해석된다 -- cast()는
        # WorkerInvoker가 이미 문서화한 구조적 계약을 단언할 뿐, idempotency.py의
        # 기본 클라이언트 캐시에서 mypy_boto3_dynamodb 스텁이 하는 역할과 같다.
        _default_lambda_client = cast(
            "WorkerInvoker",
            boto3.client("lambda"),  # pyright: ignore[reportUnknownMemberType]
        )
    return _default_lambda_client


def _configure_logging(log_level: str) -> None:
    """root 로거 레벨을 설정한 뒤, 시끄러운 SDK 로거들을 그 위로 다시 고정한다.

    순서가 중요하다: 시끄러운 로거의 ``setLevel``은 반드시 root 레벨 설정 *이후*에
    실행돼야 한다 -- 그렇지 않으면 자체 레벨이 없는 로거는 운영자가 방금 올린 root
    레벨(예: 장애 대응용 ``SLACKBOT_LOG_LEVEL=DEBUG``)을 그대로 상속한다. 이 함수가
    막으려는 시나리오가 정확히 그것 -- 모듈 docstring "SDK 로깅 함정" 절 참고.
    """
    logging.getLogger().setLevel(log_level)
    for name in _NOISY_SDK_LOGGER_NAMES:
        logging.getLogger(name).setLevel(_NOISY_SDK_LOGGER_MIN_LEVEL)


async def _healthz(request: Request) -> Response:
    """GET /healthz -- 설계상 인증 없음(LWA readiness probe, TASK-020).

    응답 본문은 의도적으로 최소 -- 설정도 identity도 없이 "이 프로세스가 HTTP
    요청에 응답할 수 있다"는 사실만. 인증 없는 공개 라우트가 설정값을 되돌려주면
    안 된다.
    """
    return JSONResponse({"status": "ok"})


def _make_slack_events_endpoint(
    settings: HandlerSettings,
    lambda_client: WorkerInvoker | None,
    idempotency_client: DynamoDBClient | None,
) -> _Endpoint:
    """``settings``/client 한 쌍에 대한 ``POST SLACK_EVENTS_PATH`` 엔드포인트 클로저를 만든다.

    모듈 레벨 함수가 아니라 클로저인 이유: 엔드포인트가 ``settings``(signing
    secret, worker ``FunctionName`` 등)와 테스트 전용 주입 클라이언트 2개
    (``lambda_client``/``idempotency_client``, 프로덕션은 항상 ``None``으로 두고
    ``_resolve_lambda_client``/``idempotency.claim_event``의 기본값이 지연
    resolve하게 둠)를 필요로 하기 때문. ``create_app``은 호출마다 새 클로저를
    만들어 "독립적 인스턴스" 보장을 그대로 유지한다.
    """

    async def _slack_events(request: Request) -> Response:
        raw_body = await request.body()

        if not verify_slack_signature(
            headers=request.headers, raw_body=raw_body, signing_secret=settings.signing_secret
        ):
            # EDGE-SB-001: 서명만으로 거부 -- 이 경로에서 raw_body는 절대 파싱하지 않는다.
            return PlainTextResponse("unauthorized", status_code=401)

        try:
            parsed_body: object = json.loads(raw_body)
        except json.JSONDecodeError:
            logger.warning("slack event body failed to parse as JSON after a valid signature")
            return PlainTextResponse("malformed request body", status_code=400)
        if not isinstance(parsed_body, dict):
            logger.warning("slack event body was valid JSON but not a JSON object")
            return PlainTextResponse("malformed request body", status_code=400)
        payload = cast(dict[str, Any], parsed_body)

        if is_url_verification(payload):
            # AC-SB-001-6 / EDGE-SB-003: 위 verify_slack_signature가 True를 반환한
            # 이후에만 도달 가능.
            challenge = extract_challenge(payload)
            return JSONResponse({"challenge": challenge})

        if is_bot_self_message(payload, bot_user_id=settings.bot_user_id):
            # EDGE-SB-011: idempotency/dispatch 작업 전에 체크.
            return PlainTextResponse("ok")

        if request.headers.get(_RETRY_NUM_HEADER) is not None:
            # EDGE-SB-004: 값과 무관하게 헤더 존재 자체가 Slack의 3초 대기가
            # 이미 한 번 지났다는 뜻.
            logger.warning(
                "slack retry received (event_id=%s, %s=%s)",
                extract_event_id(payload),
                _RETRY_NUM_HEADER,
                request.headers.get(_RETRY_NUM_HEADER),
            )

        event_id = extract_event_id(payload)
        claim_outcome = _claim_or_fail_safe(event_id, settings=settings, client=idempotency_client)
        if not claim_outcome.claimed:
            # AC-SB-003-1: 중복/claim 실패 -- 200, dispatch 없음.
            return PlainTextResponse("ok")

        _dispatch_to_worker(raw_body, settings=settings, client=lambda_client)
        return PlainTextResponse("ok")

    return _slack_events


def _claim_or_fail_safe(
    event_id: str | None, *, settings: HandlerSettings, client: DynamoDBClient | None
) -> ClaimOutcome:
    """``idempotency.claim_event``를 감싸 저장소 장애를 "무처리, 그래도 200"으로 격하시킨다.

    ``AC-SB-002-3``처럼 특정 AC가 명시한 요구는 아니다(그쪽은 *dispatch* 단계
    한정) -- 이건 컨텍스트 handover가 말한 "멱등 저장소 오류
    (``IdempotencyStoreError``) 시의 정의된 동작"에 대한 이 태스크 자체의 답.
    근거는 ``AC-SB-002-3``과 동일: 저장소 자체가 응답 불가면 "신규"와 "중복"을
    구분할 수 없다 -- 그대로 dispatch하면 idempotency 메커니즘이 막으려는 중복
    응답을 그대로 낼 위험이 있으므로, fail-safe하게 이 이벤트 하나의 dispatch만
    건너뛴다(ERROR 로그, 운영자 가시). 500은 절대 반환하지 않는다 -- Slack이
    같은 고장난 저장소로 재시도만 유발할 뿐이라는 ``AC-SB-002-3``과 같은 이유.

    반환값 ``reason="store_error"``(``event_id``가 실제로 있어도
    ``"missing_event_id"``가 아님)는 운영자가 "저장소 자체가 응답 불가"와
    "이벤트에 id가 계속 없음"을 구분하게 해준다 -- 둘을 섞으면 진단 퇴행이라는
    점은 ``idempotency.ClaimReason``의 주석 참고.
    """
    try:
        return claim_event(
            event_id,
            table_name=settings.idempotency_table,
            ttl_seconds=settings.idempotency_ttl_seconds,
            client=client,
        )
    except IdempotencyStoreError:
        logger.error(
            "idempotency store unreachable (event_id=%s) -- skipping dispatch, still ACKing",
            event_id,
            exc_info=True,
        )
        return ClaimOutcome(claimed=False, reason="store_error")


def _dispatch_to_worker(
    raw_body: bytes, *, settings: HandlerSettings, client: WorkerInvoker | None
) -> None:
    """``slack-worker``를 비동기 invoke한다(``AC-SB-002-1``). 절대 예외를 던지지 않는다.

    ``AC-SB-002-3``: 여기서 발생하는 어떤 실패든(throttling, 함수 미존재/설정
    오류, 네트워크 오류, 그 외 모든 예외) 로그만 남기고 삼킨다 -- 의도적으로
    broad한 bare ``Exception``으로 캐치, ``idempotency.py``의
    ``(BotoCoreError, ClientError)``보다 넓은 범위인 이유는 이 AC가
    "비동기 전달 *자체가* 실패하면"이라는 무조건 요구라 이 모듈이 예상 못한
    실패에도 예외를 두지 않기 때문. 이 단계가 절대 만들면 안 되는 유일한
    결과는 non-2xx 응답이다.
    """
    resolved_client = _resolve_lambda_client(client)
    try:
        resolved_client.invoke(
            FunctionName=settings.worker_function_name,
            InvocationType="Event",
            Payload=raw_body,
        )
    except Exception:
        logger.error("async dispatch to worker Lambda failed", exc_info=True)


def create_app(
    settings: HandlerSettings | None = None,
    *,
    lambda_client: WorkerInvoker | None = None,
    idempotency_client: DynamoDBClient | None = None,
) -> Starlette:
    """``settings``로부터 ``slack-handler`` Lambda용 Starlette 앱 하나를 만든다.

    ``settings`` 기본값은 ``None`` -- 이 경우 ``load_handler_settings``로
    ``os.environ``에서 Fail-Fast 로드한다. 덕분에 ``create_app`` 자신이 별도
    ``create_app_from_env`` 래퍼 없이 Dockerfile ``CMD``(TASK-020)가 요구하는
    무인자 콜러블 ``uvicorn devoks_slackbot.handler:create_app --factory``가
    된다. ``settings``를 명시적으로 주면(이 패키지의 모든 테스트가 그렇게 함)
    환경 접근을 완전히 우회한다.

    ``lambda_client``/``idempotency_client``는 테스트 전용 주입 지점(실배포는
    항상 ``None``) -- 각각 무엇을 받는지는 ``WorkerInvoker``와
    ``idempotency.claim_event``의 ``client`` 파라미터 참고.

    모듈 레벨 싱글턴이 아니라 팩토리 -- 프로세스당 한 번(또는 테스트에서
    ``HandlerSettings``당 한 번) 호출한다. 호출마다 독립된 객체를 만들며 라우트
    클로저를 호출 간에 공유하지 않는다.
    """
    resolved_settings = settings if settings is not None else load_handler_settings(os.environ)
    _configure_logging(resolved_settings.log_level)

    return Starlette(
        routes=[
            Route(HEALTHZ_PATH, endpoint=_healthz, methods=["GET"]),
            Route(
                SLACK_EVENTS_PATH,
                endpoint=_make_slack_events_endpoint(
                    resolved_settings, lambda_client, idempotency_client
                ),
                methods=["POST"],
            ),
        ],
    )
