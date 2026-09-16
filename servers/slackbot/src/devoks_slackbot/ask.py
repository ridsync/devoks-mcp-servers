"""Claude API 호출 — MCP 커넥터 경유, 이 모듈 자체는 MCP 클라이언트를 갖지 않는다
(``DSN-SB-002``, TASK-011).

**이 모듈이 존재하는 이유(``EDGE-SB-009``):** Claude API의 MCP 커넥터는
``mcp_servers[].name``과 ``tools[].mcp_server_name``이 반드시 일치해야 하고,
하나만 보내거나 이름이 어긋나면 검증 오류로 거부된다(실제 API로 직접 확인 —
workspace PLAN의 handoff 노트 참고). ``_build_mcp_request``가 이 모듈에서 두
리스트를 만드는 유일한 지점이고, 공유 이름(``_MCP_SERVER_NAME``)은 문자열
리터럴로 딱 한 번만 쓴다 — 나머지는 전부 이 상수를 참조하므로 두 리스트가
구조적으로 어긋날 수 없다. ``ask_claude``는 항상 이 함수 하나로 둘 다 얻는다.

``AC-SB-005-2``/``DSN-SB-002``: 이 모듈은 MCP 클라이언트도, tool 루프도, tool
스키마도 구현하지 않는다 — Anthropic 자체 인프라가 ``mcp_servers[0]``의
``authorization_token``으로 서버 사이드에서 MCP 서버에 직접 접속한다. 이 모듈의
역할은 그 요청 하나를 만들고 응답 하나를 해석하는 것뿐이다.

요청 형태(``CTR-SB-004``, 실제 API로 검증 — SDK 타입만 보고 판단하지 않음)
------------------------------------------------------------------------------
``model``/``max_tokens``/``output_config.effort``는 항상 ``config.py``의
``CLAUDE_MODEL``/``CLAUDE_MAX_TOKENS``/``CLAUDE_EFFORT``다 — ``ask_claude``에는
이 셋을 바꿀 수 있는 파라미터가 없다(``AC-SB-005-5``).

의도적으로 절대 만들지 않는 값 둘:

- ``thinking`` — Opus 5는 기본 adaptive thinking을 쓰고, 명시적으로 설정하면
  ``budget_tokens``와 함께 400 오류가 난다.
- ``budget_tokens``/assistant-role prefill 메시지 — 둘 다 400을 반환한다고
  문서화돼 있다.

``EDGE-SB-014``: ``messages``는 항상 방금 받은 질문 한 개뿐이다. Stage 3
초기 범위는 질문을 독립적으로 답하므로 스레드 이력을 읽거나 보내지 않는다 —
비용/프롬프트 크기가 스레드 길이에 비례해 커지지 않는다.

오류 분류(``EDGE-SB-008``, ``EDGE-SB-017``, ``AC-SB-005-3``)
------------------------------------------------------------------------------
``ask_claude``는 Claude API 쪽 실패로는 절대 raise하지 않는다 —
``slack/client.py``의 ``post_message``처럼 모든 결과를 ``AskResult``로
정규화하고, ``retryable``/``reason_code``/``client_message``로 호출자
(``worker.py``)가 Anthropic 오류 어휘를 몰라도 무엇을 게시할지 알 수 있게 한다.

- **HTTP 400 + "You have reached your specified API usage limits" prefix** —
  운영자가 Anthropic Console에 설정한 지출 한도(``EDGE-SB-017``). **재시도
  불가** — 사람이 올릴 때까지 유지되는 상태.
- **HTTP 400 + "Your credit balance is too low" prefix** — 선결제 크레딧
  소진. 2026-09 프로덕션 인시던트를 실제 API로 재현해 확인한 정확한 본문:
  ``{"type":"error","error":{"type":"invalid_request_error","message":
  "Your credit balance is too low to access the Anthropic API. Please go to
  Plans & Billing to upgrade or purchase credits."}}``. 위 지출 한도 케이스와
  ``reason_code``를 다르게 둔다(``spend_limit_exceeded`` vs
  ``credit_exhausted``) — 운영자가 해야 할 조치가 다르기 때문. **재시도 불가.**
- **HTTP 429 + ``error.details.error_code == "enforced_spend_limit_reached"``**
  — Anthropic 티어 지출 상한. ``type``은 일반 rate limit과 동일해서
  ``error_code``를 안 보면 재시도 가능처럼 보이고 SDK 자체 재시도(기본
  ``max_retries=2``)가 두 번 다 허비된다 — 이 상한은 재시도로 풀리지 않는다.
  **재시도 불가.** 실제 API에서 이 케이스는 ``retry-after`` 헤더도 안 온다
  (일반 429와 다름, 직접 확인).
- 그 외 HTTP 429 — 일반 rate limit. **재시도 가능**(SDK 자체 재시도가 이미
  처리, 이 모듈은 전부 소진됐을 때만 그렇다고 알려주면 됨).
- **HTTP >= 500** — Claude API 쪽 문제. **재시도 가능.**
- **타임아웃**(``anthropic.APITimeoutError``)/**네트워크 실패**
  (``anthropic.APIConnectionError``, ``APITimeoutError``가 그 서브클래스라
  뒤에 체크) — 응답을 아예 못 받음. **재시도 가능**, ``slack/client.py``와
  같은 이유로 별도 reason_code 둘로 구분(호출자가 백오프를 다르게 가져갈 수
  있도록).
- 그 외 모든 HTTP 상태(401/403/404/기타 4xx, 또는 위 두 메시지 어느 쪽과도
  안 맞는 400 — Anthropic이 메시지 문구를 바꾼 경우 포함) — 요청/자격증명
  자체 문제. **재시도 불가** — ``slack/client.py``가 미확인 Slack 오류
  문자열에 취하는 것과 같은 보수적 기본값.

**이 모듈은 SDK 재시도 위에 자체 재시도 루프를 절대 얹지 않는다** —
``ask_claude`` 호출마다 ``client.beta.messages.create``를 딱 한 번만 부른다.
Anthropic SDK 클라이언트(호출자, 예: ``worker.py``가 생성)가 재시도
횟수/백오프(``max_retries``, 기본 2)를 소유한다 — 여기서 또 재시도하면 이미
소진된 실패마다 시도 횟수가 배가된다.

Refusal(``AC-SB-005-4``)
------------------------------------------------------------------------------
``stop_reason == "refusal"``은 예외가 아니라 정상적인 성공 응답이지만,
``content``를 답변으로 노출하면 절대 안 된다(비어있거나 일부만 있거나 애초에
보여줄 의도가 아닐 수 있음). ``ask_claude``는 텍스트를 뽑기 *전에*
``stop_reason``을 먼저 확인해 ``outcome="refused"``, ``answer=None``으로
반환한다.

MCP 서버 콜드 스타트와 이 호출의 타임아웃(``EDGE-SB-016``)
------------------------------------------------------------------------------
MCP 서버 접속은 이 모듈이 아니라 Anthropic 인프라가 요청이 필요로 하는 첫
tool 호출 시점에 수행한다 — 실측 지연은 평상시 약 1.9초, 갓 띄운 MCP 서버
이미지 직후엔 최대 ~8.5초. 이 모듈의 책임은 Anthropic이 느린 첫 tool 호출을
기다리는 동안 *Anthropic을 기다리다가* 먼저 타임아웃하지 않는 것뿐이다.
``_REQUEST_TIMEOUT_SECONDS``(60초)는 관측된 최악 콜드 스타트(약 7배 여유) +
실제 모델 추론 + tool 왕복 한 번 이상을 여유 있게 넘기면서도, 최초 시도 +
SDK 기본 재시도 2회(3 x 60초 = 180초)가 worker 전체 예산
(``CTR-SB-009``, 300초) 안에 신원 조회/Slack 게시/Lambda 오버헤드 여유를
남기고 들어오도록 잡은 값이다. 주입된 클라이언트에 굽지 않고 ``create``
호출마다 ``timeout=``으로 넘긴다 — 이 값에 대한 판단이 호출자의 클라이언트
설정과 무관하게 이 모듈 한곳에 남도록.

보안(``AC-SB-005-3`` 인접 요구사항)
------------------------------------------------------------------------------
아래 두 값은 로그, 이 모듈이 만드는 예외 메시지, 이 모듈이 만드는 어떤
객체의 ``repr``/``str``에도 절대 나오면 안 된다.

1. **Anthropic API 키** — 이 모듈은 아예 건드리지 않는다. 주입된
   ``anthropic.AsyncAnthropic`` 클라이언트가 소유하고(``x-api-key`` 헤더로만
   전송, 실제 API로 직접 확인), 이 모듈의 로그 문장은 클라이언트나 그 설정을
   절대 포맷하지 않는다.
2. **``authorization_token``(호출자 본인의 MCP bearer 토큰)** — Anthropic이
   직렬화하는 요청 본문 안에만 실려 간다. 이 모듈의 로그는 상태 코드,
   Anthropic 자체의 짧은 ``type``/``error_code`` 문자열, ``len(question)``만
   남긴다 — 토큰/질문/답변 텍스트는 절대 남기지 않는다(``observability.py``와
   동일한 ``AC-SB-007-3`` 정책).

응답 추출
------------------------------------------------------------------------------
``usage``는 ``observability.py``의 ``build_record``가 받는 정확히 3개 키
(``input_tokens``/``output_tokens``/``cache_read_input_tokens``)만 담은 평범한
``dict``로 변환한다 — SDK의 ``BetaUsage`` 객체를 그대로 넘기면 JSON 직렬화가
깨지거나 예상 밖 필드가 새 나간다(``observability.py`` docstring과 같은
이유). 답변 텍스트는 ``content``의 ``type == "text"`` 블록을 순서대로 이어
붙인 것 — MCP tool을 쓰는 응답은 텍스트 블록 사이에
``mcp_tool_use``/``mcp_tool_result`` 블록이 섞여 있고, 사용자에게 보여줄 건
텍스트 블록뿐이다.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, cast

import anthropic
from anthropic.types.beta import (
    BetaMCPToolsetParam,
    BetaRequestMCPServerURLDefinitionParam,
    BetaUsage,
)

from .config import CLAUDE_EFFORT, CLAUDE_MAX_TOKENS, CLAUDE_MCP_BETA, CLAUDE_MODEL

logger = logging.getLogger(__name__)

__all__ = [
    "AskOutcome",
    "AskReasonCode",
    "AskResult",
    "ask_claude",
]

#: EDGE-SB-009: ``mcp_servers[].name``과 ``tools[].mcp_server_name``이 공유하는
#: 이름. 문자열 리터럴로 한 번만 쓴다 — 모듈 docstring 첫 절 참고.
_MCP_SERVER_NAME: Final = "devoks-management"

#: 모듈 docstring "MCP 서버 콜드 스타트와 이 호출의 타임아웃" 절 참고.
_REQUEST_TIMEOUT_SECONDS: Final = 60.0

#: HTTP 400 지출 한도 메시지 prefix — 파싱된 응답 본문의 ``error.message``와
#: 비교한다, ``exc.message``가 아니다(SDK가 그건 ``"Error code: ... - {body}"``로
#: 포맷해 원문 메시지가 아님 — 실제 API로 직접 확인).
_SPEND_LIMIT_MESSAGE_PREFIX: Final = "You have reached your specified API usage limits"

#: HTTP 400 크레딧 소진 메시지 prefix(``EDGE-SB-008``, ``EDGE-SB-017``) —
#: 프로덕션 인시던트를 실제 API로 재현해 확인한 정확한 본문:
#: ``"Your credit balance is too low to access the Anthropic API. Please go
#: to Plans & Billing to upgrade or purchase credits."``. 위
#: ``_SPEND_LIMIT_MESSAGE_PREFIX``와 일부러 다른 prefix로 둔다 — HTTP 상태와
#: ``invalid_request_error`` type은 같지만 원인(선결제 크레딧 없음 vs 자체
#: 설정 한도)과 운영자 조치(크레딧 구매 vs Console에서 한도 상향)가 다르다.
_CREDIT_EXHAUSTED_MESSAGE_PREFIX: Final = "Your credit balance is too low"

#: EDGE-SB-008: 이 ``error.details.error_code`` 값 하나가 HTTP 429를 일반
#: (재시도 가능) rate limit에서 티어 지출 상한(재시도 불가)으로 재분류한다.
#: 모듈 docstring "오류 분류" 절 참고.
_ENFORCED_SPEND_LIMIT_ERROR_CODE: Final = "enforced_spend_limit_reached"

AskOutcome = Literal["answered", "refused", "error"]

#: ``outcome == "error"``일 때만 쓰는 운영자 전용 분류. Slack 사용자에게 그대로
#: 보여주지 않는다 — 사용자용 텍스트는 ``client_message``(``AC-SB-005-3``).
AskReasonCode = Literal[
    "spend_limit_exceeded",
    "credit_exhausted",
    "rate_limited",
    "server_error",
    "timeout",
    "network_error",
    "api_error",
]

_RETRYABLE_CLIENT_MESSAGE = "일시적인 오류로 답변을 만들지 못했습니다. 잠시 후 다시 시도해 주세요."
_NON_RETRYABLE_CLIENT_MESSAGE = (
    "답변을 만들지 못했습니다. 문제가 계속되면 관리자에게 문의해 주세요."
)
_SPEND_LIMIT_CLIENT_MESSAGE = (
    "이번 달 API 사용 한도에 도달해 답변을 만들 수 없습니다. 관리자에게 문의해 주세요."
)
#: EDGE-SB-017: 위 ``_SPEND_LIMIT_CLIENT_MESSAGE``와 다르게 둔다 — 둘 다
#: 재시도로는 안 풀리지만 운영자 조치가 다르므로(충전 vs 자체 한도 상향)
#: 사용자에게 보여줄 문구도 "관리자에게 문의"로 뭉뚱그리지 않고 원인을 밝힌다.
_CREDIT_EXHAUSTED_CLIENT_MESSAGE = (
    "API 크레딧이 소진되어 답변을 만들 수 없습니다. 관리자에게 결제/충전을 요청해 주세요."
)
_REFUSAL_CLIENT_MESSAGE = "이 질문에 대한 답변이 거부되었습니다. 다른 방식으로 질문해 주세요."


@dataclass(frozen=True, slots=True)
class AskResult:
    """``ask_claude`` 호출 1회의 결과 — Claude API 쪽 실패로는 절대 raise하지 않는다.

    ``outcome == "answered"``: ``answer``/``stop_reason``/``usage``가 채워지고
    오류 전용 필드 4개는 전부 ``None``.

    ``outcome == "refused"``: ``stop_reason == "refusal"``, ``usage``는 여전히
    채워지지만(refusal에도 토큰 사용량은 집계됨) ``answer``는 ``None`` —
    ``content``를 답변으로 쓰면 안 된다는 ``AC-SB-005-4`` 때문. ``client_message``가
    사용자용 거부 안내를 담는다.

    ``outcome == "error"``: ``answer``/``stop_reason``/``usage``가 전부 ``None``
    (쓸 수 있는 응답이 애초에 없었음). ``retryable``/``reason_code``가 실패를
    분류하고(모듈 docstring "오류 분류" 절), ``client_message``는 비밀 없는
    재시도 안내 문구(``AC-SB-005-3``), ``detail``은 비밀/질문/답변 없이 그대로
    로그에 남겨도 되는 짧은 설명.
    """

    outcome: AskOutcome
    answer: str | None = None
    stop_reason: str | None = None
    usage: dict[str, int | None] | None = None
    retryable: bool | None = None
    reason_code: AskReasonCode | None = None
    client_message: str | None = None
    detail: str | None = None


def _build_mcp_request(
    *, mcp_server_url: str, authorization_token: str
) -> tuple[list[BetaRequestMCPServerURLDefinitionParam], list[BetaMCPToolsetParam]]:
    """``mcp_servers``/``tools`` 쌍을 함께 만든다 — 이 모듈에서 둘을 만드는 유일한 지점.

    ``EDGE-SB-009``: ``ask_claude``는 이 두 리스트를 항상 같이 쓰고 항상
    ``_MCP_SERVER_NAME``을 공유한다 — 이 함수를 거치는 한 하나만 얻거나
    이름이 어긋날 방법이 없다.
    """
    mcp_servers: list[BetaRequestMCPServerURLDefinitionParam] = [
        {
            "type": "url",
            "url": mcp_server_url,
            "name": _MCP_SERVER_NAME,
            "authorization_token": authorization_token,
        }
    ]
    tools: list[BetaMCPToolsetParam] = [
        {"type": "mcp_toolset", "mcp_server_name": _MCP_SERVER_NAME},
    ]
    return mcp_servers, tools


async def ask_claude(
    *,
    question: str,
    mcp_server_url: str,
    authorization_token: str,
    client: anthropic.AsyncAnthropic,
) -> AskResult:
    """Claude API MCP 커넥터로 ``question``을 질의한다(``REQ-SB-005``, ``CTR-SB-004``).

    ``mcp_server_url``은 ``WorkerSettings.mcp_server_url``. ``authorization_token``은
    *질문한 그 Slack 사용자 본인의* MCP 토큰(``identity.py``의
    ``CredentialLookupResult.mcp_token``)이다 — 공용 서비스 자격증명이 아니다.
    사람별 토큰이라야 MCP 서버 자체 감사 로그(Stage 1 ``CTR-003``)도 사람별로
    남는다. ``client``는 호출자(``worker.py``)가 주입한다 — 이 모듈은
    ``ANTHROPIC_API_KEY``를 직접 읽거나 클라이언트를 직접 만들지 않는다(테스트는
    가짜 전송에 연결된 클라이언트를 주입 — ``tests/test_ask.py`` 참고. 이 모듈은
    실제 Anthropic API를 절대 호출하지 않는다).

    ``messages``는 항상 방금 받은 질문 한 개뿐, 이전 스레드 이력 없음
    (``EDGE-SB-014``). ``model``/``max_tokens``/``output_config.effort``는 항상
    ``config.py``의 ``CLAUDE_MODEL``/``CLAUDE_MAX_TOKENS``/``CLAUDE_EFFORT`` —
    호출자가 바꿀 수 있는 파라미터가 없다(``AC-SB-005-5``).

    Claude API 쪽 실패로는 절대 raise하지 않는다 — ``AskResult``로 어떻게
    분류되는지는 모듈 docstring "오류 분류" 절 참고.
    """
    mcp_servers, tools = _build_mcp_request(
        mcp_server_url=mcp_server_url, authorization_token=authorization_token
    )

    try:
        response = await client.beta.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=CLAUDE_MAX_TOKENS,
            messages=[{"role": "user", "content": question}],
            mcp_servers=mcp_servers,
            tools=tools,
            output_config={"effort": CLAUDE_EFFORT},
            betas=[CLAUDE_MCP_BETA],
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
    except anthropic.APITimeoutError as exc:
        # APIConnectionError보다 먼저 잡는다(APITimeoutError가 그 서브클래스) —
        # 타임아웃이 "network_error"로 뭉뚱그려지지 않도록. 모듈 docstring 참고.
        logger.warning(
            "Claude API request timed out (%s, question_len=%d)", type(exc).__name__, len(question)
        )
        return AskResult(
            outcome="error",
            retryable=True,
            reason_code="timeout",
            client_message=_RETRYABLE_CLIENT_MESSAGE,
            detail=f"Claude API request timed out ({type(exc).__name__})",
        )
    except anthropic.APIConnectionError as exc:
        logger.warning(
            "Claude API network error (%s, question_len=%d)", type(exc).__name__, len(question)
        )
        return AskResult(
            outcome="error",
            retryable=True,
            reason_code="network_error",
            client_message=_RETRYABLE_CLIENT_MESSAGE,
            detail=(
                f"Claude API request failed before a response was received ({type(exc).__name__})"
            ),
        )
    except anthropic.APIStatusError as exc:
        return _classify_status_error(exc, question_len=len(question))
    except anthropic.AnthropicError as exc:
        # 위에서 처리 안 된 anthropic 관련 실패의 catch-all(예: 잘못된 형식의 2xx
        # 본문이 APIResponseValidationError를 던지는 경우). 일부러 보수적으로
        # 처리 — 모듈 docstring "오류 분류" 절 참고.
        logger.warning(
            "Claude API call failed unexpectedly (%s, question_len=%d)",
            type(exc).__name__,
            len(question),
        )
        return AskResult(
            outcome="error",
            retryable=False,
            reason_code="api_error",
            client_message=_NON_RETRYABLE_CLIENT_MESSAGE,
            detail=f"Claude API call failed unexpectedly ({type(exc).__name__})",
        )

    usage = _extract_usage(response.usage)

    if response.stop_reason == "refusal":
        logger.info("Claude API call refused the request (stop_reason=refusal)")
        return AskResult(
            outcome="refused",
            stop_reason=response.stop_reason,
            usage=usage,
            client_message=_REFUSAL_CLIENT_MESSAGE,
        )

    return AskResult(
        outcome="answered",
        answer=_extract_answer_text(response.content),
        stop_reason=response.stop_reason,
        usage=usage,
    )


def _classify_status_error(exc: anthropic.APIStatusError, *, question_len: int) -> AskResult:
    """``EDGE-SB-008`` — 전체 분류 규칙은 모듈 docstring "오류 분류" 절 참고."""
    error_message, error_code = _parse_error_body(exc.body)

    if exc.status_code == 400 and error_message.startswith(_SPEND_LIMIT_MESSAGE_PREFIX):
        logger.warning(
            "Claude API spend limit exceeded (status=400, question_len=%d)", question_len
        )
        return AskResult(
            outcome="error",
            retryable=False,
            reason_code="spend_limit_exceeded",
            client_message=_SPEND_LIMIT_CLIENT_MESSAGE,
            detail="Claude API spend limit exceeded (HTTP 400, invalid_request_error)",
        )

    if exc.status_code == 400 and error_message.startswith(_CREDIT_EXHAUSTED_MESSAGE_PREFIX):
        logger.warning("Claude API credit exhausted (status=400, question_len=%d)", question_len)
        return AskResult(
            outcome="error",
            retryable=False,
            reason_code="credit_exhausted",
            client_message=_CREDIT_EXHAUSTED_CLIENT_MESSAGE,
            detail="Claude API credit balance too low (HTTP 400, invalid_request_error)",
        )

    if exc.status_code == 429:
        if error_code == _ENFORCED_SPEND_LIMIT_ERROR_CODE:
            logger.warning(
                "Claude API tier spend cap reached (status=429, question_len=%d)", question_len
            )
            return AskResult(
                outcome="error",
                retryable=False,
                reason_code="spend_limit_exceeded",
                client_message=_SPEND_LIMIT_CLIENT_MESSAGE,
                detail=f"Claude API tier spend cap reached (HTTP 429, error_code={error_code})",
            )
        logger.warning("Claude API rate limited (status=429, question_len=%d)", question_len)
        return AskResult(
            outcome="error",
            retryable=True,
            reason_code="rate_limited",
            client_message=_RETRYABLE_CLIENT_MESSAGE,
            detail="Claude API rate limited (HTTP 429)",
        )

    if exc.status_code >= 500:
        logger.warning(
            "Claude API server error (status=%d, question_len=%d)", exc.status_code, question_len
        )
        return AskResult(
            outcome="error",
            retryable=True,
            reason_code="server_error",
            client_message=_RETRYABLE_CLIENT_MESSAGE,
            detail=f"Claude API server error (HTTP {exc.status_code})",
        )

    # 그 외 4xx(인증/권한/검증 오류, 위 두 메시지와 안 맞는 400 등) — 요청/자격증명
    # 자체 문제, 재시도로 안 풀림.
    logger.warning(
        "Claude API request failed (status=%d, question_len=%d)", exc.status_code, question_len
    )
    return AskResult(
        outcome="error",
        retryable=False,
        reason_code="api_error",
        client_message=_NON_RETRYABLE_CLIENT_MESSAGE,
        detail=f"Claude API request failed (HTTP {exc.status_code})",
    )


def _parse_error_body(body: object | None) -> tuple[str, str | None]:
    """오류 응답 본문에서 ``error.message``/``error.details.error_code``를 뽑는다.

    ``exc.message``(SDK 자체 ``Exception`` 메시지)는 일부러 안 쓴다 —
    ``"Error code: <n> - <body>"`` 형식이라 API 자체 메시지 텍스트가 아니다
    (실제 API로 직접 확인). 문서화된 오류 봉투 형태와 안 맞으면 raise 대신
    ``("", None)``을 반환한다 — 분류 헬퍼가 예상 밖 오류 본문 때문에 자기
    자신이 실패하면 안 된다.
    """
    if not isinstance(body, dict):
        return "", None
    body_obj = cast(dict[str, Any], body)
    error_obj = body_obj.get("error")
    if not isinstance(error_obj, dict):
        return "", None
    error_obj = cast(dict[str, Any], error_obj)
    message = error_obj.get("message")
    message_str = message if isinstance(message, str) else ""
    details = error_obj.get("details")
    error_code: object = None
    if isinstance(details, dict):
        error_code = cast(dict[str, Any], details).get("error_code")
    error_code_str = error_code if isinstance(error_code, str) else None
    return message_str, error_code_str


def _extract_answer_text(content: Sequence[object]) -> str:
    """``type == "text"`` 블록의 ``.text``만 순서대로 이어 붙인다 — 모듈 docstring 참고."""
    parts: list[str] = []
    for block in content:
        if getattr(block, "type", None) != "text":
            continue
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _extract_usage(usage: BetaUsage) -> dict[str, int | None]:
    """3개 키만 있는 평범한 dict — SDK의 ``BetaUsage`` 객체 자체는 절대 넘기지 않는다.

    모듈 docstring 참고.
    """
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_input_tokens": usage.cache_read_input_tokens,
    }
