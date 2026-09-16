"""Slack user ID -> MCP 자격증명 조회, 이 파일에 국지화 (DSN-SB-003, TASK-005).

Stage 1 ``DSN-001``(인증 검증 교체 지점을 ``verifier.py``에 국지화)과 같은
의도 — ``SLACK_USER_TOKEN_MAP``이 OAuth 기반 조회로 교체될 때(Stage 1 §10
트리거) 이 파일만 고치면 되도록, ``WorkerSettings.user_token_map``은 다른
모듈이 직접 인덱싱하지 않고 반드시 ``resolve_credentials``를 거친다.

``REQ-SB-004`` / ``CTR-SB-006``: 사용자마다 자신의 MCP 토큰으로 조회 → Stage 1의
토큰별 감사 추적(``CTR-003``)이 사람별로 이어진다.

보안 요구사항(``AC-SB-004-1..4``, ``EDGE-SB-006``, ``EDGE-SB-012``, 놓치기 쉬움):

1. **정보 노출 금지 (AC-SB-004-3).** 미식별(``user_id is None``)/미등록 둘 다
   클라이언트엔 동일한 고정 문구, 구분은 운영자 전용 ``reason_code``로만 —
   management ``AuthorizationDecision``과 같은 2계층 분리.
2. **토큰 미노출 (AC-SB-004-4).** ``mcp_token``은 ``field(repr=False)``, 어떤
   로그도 토큰 값을 포맷하지 않는다.
3. **타이밍 신호 없음.** 두 거부 경로 모두 같은 모양의 작업(속성 체크 + 옵셔널
   dict 조회)만 하며 한쪽만 조기 반환하지 않는다.

``policy.py``와 달리 여기선 ``logging``을 직접 호출한다 — 매핑 크기 경고,
미식별/미등록 구분 같은 운영자 신호(``EDGE-SB-012``)를 이 파일의 반환값엔
담을 곳이 없어서다. 그래야 ``DSN-SB-003`` 국지화가 유지된다.

입력 계약: ``user_id``는 ``slack/events.py``의 ``extract_user_id``
(``TASK-004``, ``EDGE-SB-019``)가 이미 추출한 값 — 원본 payload가 아니다.
``user`` 필드가 없거나 타입이 안 맞으면 ``None``이 반환되는데, "등록 안 된
진짜 ID"와는 다른 상황이며 둘 다 거부되지만 운영자 로그에서는 구분된다.

Import budget: boto3/anthropic/HTTP/ASGI 없음 — 순수 로직 + stdlib
``logging``만.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger(__name__)

#: AC-SB-004-3: 고정 거부 문구 — 요청 ID/매핑 크기/실패 사유와 무관하게 모든
#: 거부 분기가 이 문자열만 반환한다. "미식별"과 "미등록"을 클라이언트가
#: 구분할 수 없어야 한다.
_CLIENT_DENIAL_MESSAGE = "등록되지 않은 사용자입니다. 관리자에게 등록을 요청해 주세요."

#: 감사 전용 거부 사유 분류. 클라이언트에는 절대 노출하지 않는다(모듈 docstring 참고).
ReasonCode = Literal["user_unidentified", "user_unregistered"]

#: EDGE-SB-012: Stage 1 §10 OAuth 전환 트리거 ①(client 10개)과 동일 임계값을
#: 재사용(재산정 아님). Stage 1 실측 ~123B/client 대비 Lambda 4KB env-var 상한
#: (``EDGE-021``), ``SLACK_USER_TOKEN_MAP``은 항목당 ~60B(Slack user ID 11B +
#: MCP 토큰 43B + JSON 구분자)라 worker가 더 여유 있지만 "여유 있음"이 "무한"은
#: 아니므로, 같은 트리거 지점을 재사용해 판단 지점을 하나로 유지한다.
MAPPING_SIZE_WARNING_THRESHOLD = 10


@dataclass(frozen=True, slots=True)
class CredentialLookupResult:
    """``resolve_credentials`` 호출 1회의 결과.

    ``mcp_token``은 ``granted``가 True일 때만 설정되고, ``repr``에서는 무조건
    제외된다(``AC-SB-004-4``) — 우발적인 로그/예외 출력이 토큰을 노출하지 않는다.
    """

    granted: bool
    mcp_token: str | None = field(default=None, repr=False)
    client_message: str | None = None
    reason_code: ReasonCode | None = None


def resolve_credentials(
    user_id: str | None,
    user_token_map: Mapping[str, str],
) -> CredentialLookupResult:
    """``user_token_map``에서 ``user_id``의 MCP 토큰을 조회한다.

    ``user_id``는 ``slack/events.py``의 ``extract_user_id``가 이미 추출한
    값 — ``None``도 그대로 통과시키며, payload에서 다시 식별하지 않는다.

    ``user_id``가 비어있지 않은 문자열이고 ``user_token_map``에 비어있지 않은
    토큰으로 존재하면 승인(``AC-SB-004-1``). 그 외 모든 경우(``None``, 빈
    문자열, 키 없음, 빈 토큰값 — ``config.py``가 파싱 시점에 이미 막지만 이
    함수는 호출자가 항상 그 경로를 거쳤다고 신뢰하지 않는다)는 동일한 클라이언트
    메시지로 거부(``AC-SB-004-2``, ``AC-SB-004-3``, ``EDGE-SB-006``),
    ``reason_code``만 다르다.

    ``user_token_map``이 ``MAPPING_SIZE_WARNING_THRESHOLD``를 넘으면 호출마다
    운영자 전용 경고 로그를 남긴다(``EDGE-SB-012``) — 매핑의 키/값은 절대
    포함하지 않고 크기만 남긴다.
    """
    _warn_if_mapping_oversized(user_token_map)

    if user_id:
        token = user_token_map.get(user_id) or None
        reason: ReasonCode = "user_unregistered"
    else:
        token = None
        reason = "user_unidentified"

    if token is None:
        logger.info("credential lookup denied (reason=%s)", reason)
        return CredentialLookupResult(
            granted=False,
            client_message=_CLIENT_DENIAL_MESSAGE,
            reason_code=reason,
        )

    return CredentialLookupResult(granted=True, mcp_token=token)


def _warn_if_mapping_oversized(user_token_map: Mapping[str, str]) -> None:
    size = len(user_token_map)
    if size > MAPPING_SIZE_WARNING_THRESHOLD:
        logger.warning(
            "SLACK_USER_TOKEN_MAP has %d entries, exceeding the warning threshold of %d "
            "(EDGE-SB-012) — plan the OAuth transition (Stage 1 §10 trigger ①)",
            size,
            MAPPING_SIZE_WARNING_THRESHOLD,
        )
