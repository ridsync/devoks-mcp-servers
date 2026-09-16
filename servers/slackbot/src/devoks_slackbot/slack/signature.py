"""Slack 요청 서명 검증 — 순수 함수 (DSN-SB-007, TASK-003).

``CTR-SB-001``: ``sig_basestring = "v0:" + timestamp + ":" + raw_body`` ->
HMAC-SHA256(signing secret) -> hex digest -> ``"v0=" + digest`` 를
``X-Slack-Signature``와 constant-time 비교(``AC-SB-001-4``).
``CTR-SB-003``: ``X-Slack-Request-Timestamp``가 "now"와 300초 이상 차이나면
서명이 유효해도 거부 — replay 방지(``EDGE-SB-002``).

이 모듈은 bytes/str/숫자만 받아 ``bool``만 반환하며 HTTP/ASGI/Lambda 관련
import를 하지 않는다(``DSN-SB-007``). 실제 요청에서 ``raw_body``/헤더를
꺼내는 배선은 TASK-012의 몫 — 이 경계 덕분에 replay/변조 케이스를 라이브
요청 없이 순수 단위 테스트로 고정할 수 있다.

**``raw_body``는 반드시 파싱 전 원본 bytes여야 한다(``CTR-SB-001``).**
JSON body를 파싱한 뒤 재직렬화해서 서명을 계산하면 key 순서/공백이 바뀌어
서명 검증이 조용히 깨진다 — Slack 연동에서 가장 잘 알려진 함정 중 하나라
``test_signature.py``가 별도 케이스로 고정해 둔다.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Mapping

#: CTR-SB-003 — 300초(5분), FRD 실측값.
DEFAULT_TIMESTAMP_TOLERANCE_SECONDS = 300

_SIGNATURE_VERSION = "v0"

#: Slack 표준 헤더 이름. 조회는 대소문자 무시(AC-SB-001-5) — ``_read_header`` 참고.
SIGNATURE_HEADER = "X-Slack-Signature"
TIMESTAMP_HEADER = "X-Slack-Request-Timestamp"


def verify_slack_signature(
    *,
    headers: Mapping[str, str],
    raw_body: bytes,
    signing_secret: str,
    now: float | None = None,
    tolerance_seconds: int = DEFAULT_TIMESTAMP_TOLERANCE_SECONDS,
) -> bool:
    """``headers``/``raw_body``가 유효한 Slack 서명이면 ``True``.

    순서: 헤더 두 개 모두 존재(대소문자 무시, ``AC-SB-001-5``) -> timestamp
    파싱 및 ``tolerance_seconds`` 이내(``AC-SB-001-3``/``CTR-SB-003``/
    ``EDGE-SB-002``) -> 재계산한 서명을 constant-time 비교
    (``AC-SB-001-1``/``AC-SB-001-4``).

    절대 예외를 던지지 않는다 — 헤더 누락, 숫자가 아닌 timestamp, 잘못된
    서명 형식, non-ASCII 입력 모두 ``False``로 귀결된다(``EDGE-SB-001``:
    부분 파싱이 아니라 거부). ``auth/verifier.py``(TASK-043)의 byte 정규화
    비교 교훈과 동일.

    ``now``는 기본값 ``time.time()`` — 테스트가 시계를 고정할 수 있도록.
    """
    signature = _read_header(headers, SIGNATURE_HEADER)
    timestamp_raw = _read_header(headers, TIMESTAMP_HEADER)
    if not signature or not timestamp_raw:
        return False

    timestamp = _parse_timestamp(timestamp_raw)
    if timestamp is None:
        return False

    current_time = time.time() if now is None else now
    if abs(current_time - timestamp) > tolerance_seconds:
        return False

    expected = _compute_signature(timestamp_raw, raw_body, signing_secret)
    return _constant_time_equals(expected, signature)


def _read_header(headers: Mapping[str, str], name: str) -> str | None:
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


def _parse_timestamp(raw: str) -> int | None:
    try:
        return int(raw)
    except ValueError:
        return None


def _compute_signature(timestamp_raw: str, raw_body: bytes, signing_secret: str) -> str:
    sig_basestring = f"{_SIGNATURE_VERSION}:{timestamp_raw}:".encode() + raw_body
    digest = hmac.new(signing_secret.encode("utf-8"), sig_basestring, hashlib.sha256).hexdigest()
    return f"{_SIGNATURE_VERSION}={digest}"


def _constant_time_equals(expected: str, presented: str) -> bool:
    # str이 아닌 UTF-8 bytes로 비교: non-ASCII str에는 hmac.compare_digest가
    # TypeError를 던지므로, byte 정규화 비교라야 "절대 예외 없음" 보장이
    # 유지된다(TASK-043 교훈, auth/verifier.py 참고).
    return hmac.compare_digest(expected.encode("utf-8"), presented.encode("utf-8"))
