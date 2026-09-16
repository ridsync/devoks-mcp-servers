"""``chat.postMessage`` wrapper — 스레드 답글, 접수 알림, Slack Web API 실패
분류(``REQ-SB-006``, TASK-010).

**이 모듈이 막는 유일한 함정: Slack Web API는 HTTP 상태 코드로 거짓말한다.**
Slack 공식 ``chat.postMessage`` 문서에 따르면 실패해도 **HTTP 200**을
반환하고, 실패는 응답 *body*의 ``{"ok": false, "error": "..."}``로만
드러난다. ``response.status_code``만 보고 400 미만을 성공으로 취급하면
``EDGE-SB-020``(봇이 채널에 초대되지 않음)을 성공한 게시로 조용히 기록해
버리면서 사용자는 답을 영영 못 받는다. 그래서 이 모듈의 모든 2xx 응답은
상태 코드가 아니라 반드시 ``ok`` 필드로 판정한다(``_parse_post_message_response``).

오류 분류(재시도 가능 여부) — ``EDGE-SB-020``
------------------------------------------------------------------
``post_message``는 Slack 쪽 실패에 절대 예외를 던지지 않는다 — ``ok=False``와
``retryable`` 플래그를 담은 ``PostMessageResult``를 반환해, 호출자
(``TASK-014``의 worker)가 Slack의 오류 어휘를 몰라도 판단할 수 있게 한다:

- ``"not_in_channel"``(``EDGE-SB-020``) / ``"channel_not_found"`` —
  **재시도 불가**. 둘 다 운영자만 고칠 수 있는 설정 문제(봇 미초대,
  channel id/권한 오류)라 재시도로는 영원히 해결되지 않는다.
- 그 외 모든 Slack ``error`` 문자열 — **재시도 불가**, ``"slack_error_unknown"``으로
  분류. 의도적으로 보수적인 처리다: 모르는 오류는 일시적일 수도 있지만
  메시지 과다 길이/잘못된 blocks 같은 영구적 앱 레벨 문제일 수도 있어,
  낙관적으로 잘못 추측하면 무한 재시도 루프 위험이 있다. 특정 미지 오류가
  실제로는 재시도 안전하다고 나중에 밝혀지면 호출자가 ``reason_code``로
  개별 분기하면 된다.
- ``"rate_limited"``(HTTP 429) — **재시도 가능**. ``chat.postMessage``의
  Slack 문서 상 제한은 채널당 초당 약 1건. ``retry_after_seconds``는
  Slack이 보낸 ``Retry-After`` 헤더 파싱값. **정책: 이 모듈은 그 헤더를
  읽어 노출만 할 뿐 스스로 sleep/재시도하지 않는다** — ``ask.py``
  (``TASK-011``)와 같은 "재시도 루프는 소유하지 않는다" 원칙을 여기서도
  지켜, 재시도 타이밍/백오프 결정권을 오직 호출자 한 계층에만 둔다.
- ``"http_error"`` — 429가 아닌 non-2xx HTTP 상태. 상태 >= 500(서버 쪽
  문제)일 때만 재시도 가능 — 그 외 4xx는 요청 자체의 문제라 재시도로
  고쳐지지 않는다.
- ``"network_error"`` / ``"timeout"`` — 응답 자체를 못 받은 경우
  (``httpx2.HTTPError``). 둘 다 재시도 가능하지만 별개 reason code로 유지한
  이유는, timeout에는 호출자가 더 긴 백오프를 쓰고 싶을 수 있어서다(구분
  가능해야 한다는 요구사항).
- ``"invalid_response"`` — Slack이 2xx를 반환했지만 문서화된 형태로 파싱
  불가(JSON 아님, object 아님, 또는 ``ok: true``인데 쓸 수 있는 ``ts``
  없음). 재시도 불가 — *요청* 자체는 Slack에 정상 도달했으므로 반복해도
  결과가 달라질 가능성이 낮다.

보안(``AC-SB-006-1`` 인접 요구사항, 별도 ID 없음)
------------------------------------------------------------------------------
아래 두 값은 로그 라인, 예외 메시지, 이 모듈이 만드는 어떤 것의
``repr``/``str``에도 절대 노출되면 안 된다:

1. **봇 토큰.** ``Authorization: Bearer <token>`` 요청 헤더로만 전송하고
   (query/body 파라미터로 절대 넣지 않아 URL/body 로깅으로도 새지 않음)
   이 모듈 어떤 로그 구문에도 포함하지 않는다 — 아래 모든 log 호출은
   상태 코드, Slack 자체의 짧은 ``error`` 문자열, byte/char 길이만 남긴다.
2. **게시하는 메시지(``text``).** Claude 답변에서 온 내부/출처 콘텐츠를
   담을 수 있다(``AC-SB-007-3``과 같은 우려). 이 모듈의 어떤 로그도
   ``text`` 자체는 담지 않는다 — 뭔가 남겨야 할 때도 ``len(text)``뿐이다.

HTTP client 주입과 timeout(``CTR-SB-009``)
-------------------------------------------------------
``http_client``는 선택적 키워드 — 테스트가 ``httpx2.MockTransport``로 연결한
``httpx2.AsyncClient``를 주입할 수 있게 한다(이 테스트 스위트는 실제
Slack을 절대 호출하지 않음). 생략 시 ``_resolve_http_client``가 지연
생성해 모듈 전역에 캐싱한다 — ``idempotency.py``의 ``_resolve_client``가
boto3 client에 쓰는 것과 같은 패턴, 같은 이유(warm Lambda 컨테이너는
호출마다 재생성이 아니라 커넥션 풀을 재사용해야 함). 이 캐시는 프로덕션
호출(``None``)만 건드리며 테스트는 항상 명시적으로 주입한다.

client의 timeout은 ``httpx2.AsyncClient()`` 자체 기본값에 맡기지 않고
**명시적으로 설정**한다 — ``servers/management/.../github/client.py``의
``_GITHUB_HTTP_TIMEOUT_SECONDS``와 같은 이유: worker의 전체 실행 예산은
``CTR-SB-009``의 300초이며 이걸 Claude API 호출 등과 한 invocation 안에서
나눠 써야 하므로, 무제한 ``chat.postMessage`` 호출 하나가 그 예산을 다
먹어선 안 된다. 20.0초(같은 선례 값)는 가볍고 단발성인 Slack API 호출
하나에 충분히 넉넉하면서도 최악의 경우에도 worker 예산을 압도적으로
보존한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Final, Literal, cast

import httpx2

logger = logging.getLogger(__name__)

__all__ = [
    "ACKNOWLEDGEMENT_MESSAGE",
    "PostMessageReasonCode",
    "PostMessageResult",
    "post_message",
]

_SLACK_API_BASE_URL: Final = "https://slack.com/api"
_CHAT_POST_MESSAGE_URL: Final = f"{_SLACK_API_BASE_URL}/chat.postMessage"

#: 모듈 docstring "HTTP client 주입과 timeout" 절 참고.
_CHAT_POST_MESSAGE_TIMEOUT_SECONDS: Final = 20.0

#: AC-SB-006-4: 느린 작업(Claude API 호출) 시작 전 호출자(TASK-014의
#: worker)가 게시 — 사용자가 조용히 기다리는 대신 질문이 접수됐음을 알게
#: 한다. 평범한 모듈 상수로 둔 이유는 ``format.py``의 ``TRUNCATION_NOTICE``와
#: 같다 — 호출부마다 문구를 하드코딩하지 않고 재사용/테스트 monkeypatch가
#: 가능하도록.
ACKNOWLEDGEMENT_MESSAGE: Final[str] = (
    "질문을 확인했습니다. 답변을 준비하는 동안 잠시만 기다려 주세요..."
)

#: 각 값의 의미와 ``PostMessageResult.retryable`` True 여부는 모듈
#: docstring "오류 분류" 절 참고.
PostMessageReasonCode = Literal[
    "not_in_channel",
    "channel_not_found",
    "slack_error_unknown",
    "rate_limited",
    "http_error",
    "network_error",
    "timeout",
    "invalid_response",
]

#: EDGE-SB-020 + 문서화된 유일한 짝. 이 모듈이 실제 별개 reason code를 가진
#: Slack ``error`` 문자열만 여기 있다 — 나머지는 전부
#: ``_classify_slack_error``에서 ``"slack_error_unknown"``으로 떨어진다.
_KNOWN_NON_RETRYABLE_ERRORS: Final[dict[str, PostMessageReasonCode]] = {
    "not_in_channel": "not_in_channel",
    "channel_not_found": "channel_not_found",
}


@dataclass(frozen=True, slots=True)
class PostMessageResult:
    """``post_message`` 호출 1회의 결과 — Slack 쪽 실패에 절대 예외를 던지지 않는다.

    ``ok=True``: ``ts``/``channel``은 새로 게시된 메시지에 대한 Slack 자체
    값(이 분기에서 둘 다 non-``None``); 실패 전용 필드 4개는 모두 ``None``.

    ``ok=False``: ``ts``/``channel``은 ``None``. ``retryable``과
    ``reason_code``가 실패를 분류한다(모듈 docstring 참고). ``detail``은
    비밀/메시지 본문 없이 그대로 로그해도 안전한 짧은 설명.
    ``retry_after_seconds``는 ``reason_code == "rate_limited"``이고 Slack이
    ``Retry-After`` 헤더를 보냈을 때만 non-``None``.
    """

    ok: bool
    ts: str | None = None
    channel: str | None = None
    retryable: bool | None = None
    reason_code: PostMessageReasonCode | None = None
    detail: str | None = None
    retry_after_seconds: float | None = None


_default_http_client: httpx2.AsyncClient | None = None


def _resolve_http_client(http_client: httpx2.AsyncClient | None) -> httpx2.AsyncClient:
    """``http_client``가 주어지면 그대로, 아니면 지연 생성·warm 캐시된 기본값을 반환.

    캐싱 분기는 ``None``(프로덕션 호출)만 탄다 — 테스트는 항상
    ``httpx2.MockTransport`` 기반 client를 명시적으로 주입해 이 모듈 전역
    캐시를 건드리지 않는다. ``idempotency.py``의 ``_resolve_client``와
    동일 이유(warm Lambda 컨테이너는 커넥션 풀을 invocation 간 재사용).
    """
    global _default_http_client
    if http_client is not None:
        return http_client
    if _default_http_client is None:
        _default_http_client = httpx2.AsyncClient(timeout=_CHAT_POST_MESSAGE_TIMEOUT_SECONDS)
    return _default_http_client


async def post_message(
    *,
    channel: str,
    text: str,
    bot_token: str,
    thread_ts: str | None = None,
    http_client: httpx2.AsyncClient | None = None,
) -> PostMessageResult:
    """Slack의 ``chat.postMessage``로 ``text``를 ``channel``에 게시(``AC-SB-006-1``).

    ``thread_ts`` — 주어지면 새 최상위 메시지 대신 그 스레드 안에 답한다
    (``AC-SB-006-1``); ``slack/events.py``의 ``extract_reply_target_ts``가
    반환한 값을 그대로 넘긴다. ``bot_token``은 ``Authorization: Bearer``
    헤더로만 전송(모듈 docstring 보안 절 참고) — 절대 로그하지 않고 body/URL에
    넣지 않는다.

    Slack 쪽/전송 계층 실패에 절대 예외를 던지지 않는다 — 성공, Slack
    ``ok: false`` 오류, HTTP 오류, rate limit, 네트워크 실패/timeout,
    파싱 불가 응답 모두 반환값 ``PostMessageResult``로 정규화된다. 각
    케이스의 정확한 분류는 모듈 docstring "오류 분류" 절 참고.
    """
    client = _resolve_http_client(http_client)
    payload: dict[str, Any] = {"channel": channel, "text": text}
    if thread_ts is not None:
        payload["thread_ts"] = thread_ts

    headers = {
        "Authorization": f"Bearer {bot_token}",
        "Content-Type": "application/json",
    }

    try:
        response = await client.post(_CHAT_POST_MESSAGE_URL, json=payload, headers=headers)
    except httpx2.TimeoutException as exc:
        # TimeoutException은 아래 httpx2.HTTPError의 서브클래스라, 이보다
        # 먼저 체크해야 timeout이 일반 "network_error"로 뭉뚱그려지지 않는다
        # (모듈 docstring 참고).
        logger.warning(
            "chat.postMessage timed out (%s, text_len=%d)", type(exc).__name__, len(text)
        )
        return PostMessageResult(
            ok=False,
            retryable=True,
            reason_code="timeout",
            detail=f"Slack API request timed out ({type(exc).__name__})",
        )
    except httpx2.HTTPError as exc:
        logger.warning(
            "chat.postMessage transport error (%s, text_len=%d)", type(exc).__name__, len(text)
        )
        return PostMessageResult(
            ok=False,
            retryable=True,
            reason_code="network_error",
            detail=(
                f"Slack API request failed before a response was received ({type(exc).__name__})"
            ),
        )

    if response.status_code == 429:
        retry_after = _parse_retry_after(response.headers.get("retry-after"))
        logger.warning("chat.postMessage rate limited (retry_after=%s)", retry_after)
        return PostMessageResult(
            ok=False,
            retryable=True,
            reason_code="rate_limited",
            detail="Slack API rate limit exceeded (HTTP 429)",
            retry_after_seconds=retry_after,
        )

    if response.is_error:
        retryable = response.status_code >= 500
        logger.warning("chat.postMessage HTTP error (status=%d)", response.status_code)
        return PostMessageResult(
            ok=False,
            retryable=retryable,
            reason_code="http_error",
            detail=f"Slack API request failed: HTTP {response.status_code}",
        )

    return _parse_post_message_response(response)


def _parse_post_message_response(response: httpx2.Response) -> PostMessageResult:
    """2xx ``chat.postMessage`` 응답을 상태 코드가 아니라 body로 판정한다.

    모듈 docstring 도입부 참고 — Slack은 자체 앱 레벨 실패에도 HTTP 200을
    반환하므로, 이 시점에 신뢰할 수 있는 성공/실패 신호는 파싱된 body의
    ``ok`` 필드뿐이다.
    """
    try:
        body = response.json()
    except ValueError:
        logger.warning("chat.postMessage response was not valid JSON")
        return PostMessageResult(
            ok=False,
            retryable=False,
            reason_code="invalid_response",
            detail="Slack API response was not valid JSON",
        )

    if not isinstance(body, dict):
        logger.warning("chat.postMessage response was not a JSON object")
        return PostMessageResult(
            ok=False,
            retryable=False,
            reason_code="invalid_response",
            detail="Slack API response was not a JSON object",
        )
    body_obj = cast(dict[str, Any], body)

    if body_obj.get("ok") is True:
        ts = body_obj.get("ts")
        channel = body_obj.get("channel")
        if not isinstance(ts, str) or not ts:
            logger.warning("chat.postMessage reported ok=true without a usable ts")
            return PostMessageResult(
                ok=False,
                retryable=False,
                reason_code="invalid_response",
                detail="Slack API reported ok=true without a ts",
            )
        return PostMessageResult(
            ok=True,
            ts=ts,
            channel=channel if isinstance(channel, str) else None,
        )

    error = body_obj.get("error")
    error_str = error if isinstance(error, str) and error else "unknown"
    reason_code = _classify_slack_error(error_str)
    logger.warning("chat.postMessage failed (error=%s)", error_str)
    return PostMessageResult(
        ok=False,
        retryable=False,
        reason_code=reason_code,
        detail=f"Slack API error: {error_str}",
    )


def _classify_slack_error(error: str) -> PostMessageReasonCode:
    """EDGE-SB-020 + fallback — 모듈 docstring "오류 분류" 절 참고."""
    return _KNOWN_NON_RETRYABLE_ERRORS.get(error, "slack_error_unknown")


def _parse_retry_after(value: str | None) -> float | None:
    """Slack의 ``Retry-After`` 헤더(delta-seconds)를 파싱 — 없거나 형식이 틀리면 ``None``."""
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None
