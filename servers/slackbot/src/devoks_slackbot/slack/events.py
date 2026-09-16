"""Slack 이벤트 payload 파싱 — 순수 함수 (TASK-004).

``url_verification`` 핸드셰이크와 ``event_callback``/``app_mention``
이벤트를 구분하고, 어떤 작업이든 시작하기 전에 봇 자신의 메시지를 걸러내며
(``EDGE-SB-011``, ``AC-SB-006-3``), 이 패키지의 다른 모든 모듈이 "누가
물었나?"를 물을 수 있는 단일 창구(``extract_user_id``, ``EDGE-SB-019``)를
제공한다.

``CTR-SB-006``은 질문자의 식별자가 ``U...`` 형태의 Slack user ID인
``event.user``라고 가정한다. 이 가정은 ``TASK-030``까지 실제 워크스페이스로
검증되지 않았다 — Enterprise Grid는 워크스페이스마다 ID 형태가 다를 수 있고,
``authorizations[].user_id``(질문자가 아니라 앱 *설치자*)는 얼핏 봐서는
형태가 동일하다. 이 코드베이스의 모든 호출자는 ``payload["event"]["user"]``를
직접 읽지 말고 반드시 ``extract_user_id``를 거쳐야 한다 — ``TASK-030``이 실제
payload로 필드/형태를 확정하거나 정정할 때, 고칠 곳이 이 함수 하나뿐이도록.

``slack/signature.py``(TASK-003)와 마찬가지로 이 모듈은 평범한
``Mapping``/``str``/``dict``를 받아 평범한 값을 반환하며, HTTP/ASGI/Lambda
관련 import를 절대 하지 않는다(``DSN-SB-007``). 여기 모든 함수는 절대 예외를
던지지 않는다 — 기형/예상 밖 payload(``event`` 누락, non-dict ``event``,
필드 누락/타입 불일치, 알 수 없는 ``type``)는 예외가 아니라 항상
``False``/``None``으로 귀결된다 — 실제 payload가 예상을 벗어나도 500이 아니라
"이 이벤트는 건너뛴다"로 완화된다.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

#: 이 모듈이 구분하는 Slack 최상위 payload ``type`` 값.
TYPE_URL_VERIFICATION = "url_verification"
TYPE_EVENT_CALLBACK = "event_callback"

#: 이 Stage가 처리하는 유일한 내부 ``event.type``(FRD §1 — @-멘션).
EVENT_TYPE_APP_MENTION = "app_mention"


def is_url_verification(payload: Mapping[str, Any]) -> bool:
    """``payload``가 Slack의 ``url_verification`` 핸드셰이크면 True.

    ``EDGE-SB-003``: 신뢰하기 *전에* 요청 서명을 검증하는 건 호출자의 책임 —
    이 함수는 그런 체크를 하지 않는다.
    """
    return _get_str(payload, "type") == TYPE_URL_VERIFICATION


def extract_challenge(payload: Mapping[str, Any]) -> str | None:
    """되돌려줄 ``challenge`` 값을 반환, 없거나 기형이면 None.

    ``is_url_verification(payload)``가 True인 뒤에 호출되는 것을 전제하며,
    이 함수 자체는 ``type``을 확인하지 않는다. ``challenge``가 없거나
    문자열이 아니면(이 모듈이 예외 없이 버텨야 하는 payload 변형) None을
    반환해 호출자가 죽지 않고 응답 방식을 결정하게 한다.
    """
    return _get_str(payload, "challenge")


def is_app_mention_event(payload: Mapping[str, Any]) -> bool:
    """``payload``가 ``app_mention`` 내부 이벤트를 감싼 ``event_callback``이면
    True(``CTR-SB-006``의 payload 형태).

    그 외 최상위 ``type``(알 수 없음, 없음, 비문자열), 누락/non-dict
    ``event``, ``app_mention``이 아닌 내부 ``event.type``은 모두 예외 없이
    False로 귀결된다.
    """
    if _get_str(payload, "type") != TYPE_EVENT_CALLBACK:
        return False
    event = _get_event(payload)
    if event is None:
        return False
    return _get_str(event, "type") == EVENT_TYPE_APP_MENTION


def is_bot_self_message(payload: Mapping[str, Any], *, bot_user_id: str) -> bool:
    """이 이벤트가 봇 자신의 메시지로 재트리거된 것이면 True
    (``EDGE-SB-011``, ``AC-SB-006-3``).

    봇 자신의 메시지가 가질 수 있는 신호 **둘 다** 확인한다 — 하나만 보면
    Slack이 실제로 보내는 케이스를 놓칠 수 있다:
    - ``event.user == bot_user_id`` — 봇이 자기 자신으로 게시.
    - ``event.bot_id`` 존재 — 봇 메시지는 ``user`` 필드 없이 ``bot_id``만
      가질 수 있다.

    어떤 작업이든 시작하기 전에 반드시 체크해야 한다(``EDGE-SB-011``): 여기서
    false negative가 나면 봇 자신의 답변이 스스로를 재트리거하고 비용이
    무한정 누적된다. ``bot_user_id``는 settings에서 읽지 않고 평범한 키워드
    인자로 받는다 — 이 함수를 순수하고 config 독립적인 단위로 유지하기
    위해서다. ``SLACK_BOT_USER_ID``를 넘기는 건 호출자의 몫.
    """
    event = _get_event(payload)
    if event is None:
        return False
    user_id = _get_str(event, "user")
    if user_id is not None and user_id == bot_user_id:
        return True
    return _get_str(event, "bot_id") is not None


def extract_user_id(payload: Mapping[str, Any]) -> str | None:
    """질문을 트리거한 Slack user ID를 반환, 없으면 None.

    ``CTR-SB-006``의 user-mapping 조회를 포함해 이 패키지의 모든 호출자가
    반드시 거쳐야 하는 단일 창구다 — ``payload["event"]["user"]``를 직접
    읽지 않는다(모듈 docstring 참고, ``EDGE-SB-019``).
    ``payload["authorizations"][0]["user_id"]``는 질문자가 아니라 앱
    설치자이며 형태(``U...``)가 동일해 보이므로 여기서 의도적으로 읽지
    않는다.

    ``event``가 없거나 non-dict, ``user``가 없거나 빈 문자열/non-string이면
    None — 절대 예외를 던지지 않는다.
    """
    event = _get_event(payload)
    if event is None:
        return None
    return _get_str(event, "user")


def extract_event_id(payload: Mapping[str, Any]) -> str | None:
    """idempotency key로 쓰이는 최상위 ``event_id``를 반환, 없으면 None.

    None은 "이 payload엔 idempotency key가 없다"는 신호일 뿐 — 거부/로그/
    폴백 중 어떻게 처리할지는 호출자의 몫이며, 이 함수는 대체 key를
    만들어내지 않는다.
    """
    return _get_str(payload, "event_id")


def extract_channel(payload: Mapping[str, Any]) -> str | None:
    """답변을 게시할 Slack channel ID를 반환, 없으면 None.

    ``AC-SB-006-1``의 게시 지점(``slack/client.py`` ``post_message``의
    ``channel`` 인자) — 호출자는 worker.py(``TASK-014``). ``event``가
    없거나 non-dict, ``channel``이 없거나 non-string이면 None — 이 모듈의
    다른 extractor와 동일한 계약으로 절대 예외를 던지지 않는다.
    """
    event = _get_event(payload)
    if event is None:
        return None
    return _get_str(event, "channel")


def extract_question_text(payload: Mapping[str, Any]) -> str | None:
    """원본 ``event.text`` 필드를 그대로 반환, 없으면 None.

    "원본"은 Slack이 보낸 그대로라는 뜻 — ``app_mention``이면 트리거한
    ``<@BOT_USER_ID>`` 멘션 토큰까지 그대로 포함한다(예:
    ``"<@U0LAN0Z89> question"``, 공식 예제 payload로 이 모듈 테스트가
    고정). 이 토큰을 제거하는 건 파싱이 아니라 해석이라 여기서 하지
    않는다 — 그 책임은 worker.py(``TASK-014``)가 진다(``DSN-SB-007``:
    이 모듈은 "payload가 문자 그대로 뭐라 했나"만 책임진다). ``event``가
    없거나 non-dict, ``text``가 없거나 non-string이면 None — 절대 예외를
    던지지 않는다.
    """
    event = _get_event(payload)
    if event is None:
        return None
    return _get_str(event, "text")


def extract_reply_target_ts(payload: Mapping[str, Any]) -> str | None:
    """답변을 게시할 대상 timestamp를 반환(``AC-SB-006-1``).

    기존 스레드 안에서의 멘션이면 ``event.thread_ts``, 아니면
    ``event.ts`` — 최상위 멘션은 자신이 새 스레드를 시작한다. 둘 다
    비어있지 않은 문자열로 없으면 None.
    """
    event = _get_event(payload)
    if event is None:
        return None
    thread_ts = _get_str(event, "thread_ts")
    if thread_ts is not None:
        return thread_ts
    return _get_str(event, "ts")


def _get_event(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    event = payload.get("event")
    if isinstance(event, Mapping):
        return cast(Mapping[str, Any], event)
    return None


def _get_str(source: Mapping[str, Any], key: str) -> str | None:
    value = source.get(key)
    if isinstance(value, str) and value:
        return value
    return None
