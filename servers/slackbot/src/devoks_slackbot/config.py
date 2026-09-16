"""환경변수 -> 불변, 역할별 ``Settings`` (DSN-SB-005).

``servers/management``의 config.py와 동일한 Fail-Fast 패턴(에러를 모두 모아 한 번에
``ConfigError``로 raise, DSN-006)을 공유 패키지로 추출하지 않고 의도적으로 중복한다 —
FRD §4.4가 두 서버의 배포 독립성을 위해 공용 config 패키지를 금지한다.

**``load_settings(env, role)`` 하나가 아니라 진입점을 둘로 나눈 이유:** 이미지 하나가
Lambda 두 개(FRD §4.4)를 서빙하는데 필수 키가 역할마다 다르다(FRD §5.2) — handler는
``WORKER_FUNCTION_NAME``이 필요하고 ``ANTHROPIC_API_KEY``는 필요 없다(worker는 반대).
``load_handler_settings``/``load_worker_settings``(``HandlerSettings``/``WorkerSettings``)로
분리하면 잘못된 역할의 필드 접근이 런타임 누락이 아니라 타입 에러로 즉시 드러난다.

**``IDEMPOTENCY_TABLE``은 handler 전용이 아니라 공통이다(2026-09-14, ``TASK-014`` 수정).**
FRD §5.2 원문은 이 키를 handler 전용으로 뒀지만, ``TASK-014``(worker.py)에서 worker도
같은 DynamoDB 테이블로 완료 여부를 확인/기록해야 함(``EDGE-SB-005`` — Lambda 자체의
async-invoke 재시도는 handler가 볼 수 없는 worker 전용 중복 원인)과 in-flight coalescing
락(``EDGE-SB-015``)이 같은 테이블을 키 prefix만 다르게 재사용한다는 게 드러나 두 역할
모두 필수로 바뀌었다 — ``WORKER_FUNCTION_NAME``에서 ``TASK-012``가 이미 겪은 것과 같은
FRD 누락 패턴(아래 ``_HANDLER_REQUIRED_KEYS`` 참고).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import urlsplit

# CTR-SB-004: Claude API 호출 계약의 고정값. AC-SB-005-5가 사용자 입력으로 절대 바뀌지
# 않아야 한다고 요구하므로 env로 오버라이드 가능한 설정이 아니라 코드 상수로 둔다.
# ask.py(TASK-011)가 직접 import한다 — 이 값에 닿는 env var는 없다.
CLAUDE_MODEL = "claude-opus-5"
CLAUDE_MAX_TOKENS = 8000
CLAUDE_EFFORT = "medium"
CLAUDE_MCP_BETA = "mcp-client-2025-11-20"

MAX_RESPONSE_CHARS_DEFAULT = 3500
MAX_RESPONSE_CHARS_MIN = 1
MAX_RESPONSE_CHARS_MAX = 40000

IDEMPOTENCY_TTL_SECONDS_DEFAULT = 3600
IDEMPOTENCY_TTL_SECONDS_MIN = 300
IDEMPOTENCY_TTL_SECONDS_MAX = 86400

_DEFAULT_LOG_LEVEL = "INFO"
_VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})

#: FRD §5.2에서 역할 구분 없는 키 + ``IDEMPOTENCY_TABLE``(TASK-014, 모듈 docstring
#: "IDEMPOTENCY_TABLE is common" 절 참고). 두 역할 모두 필수.
_COMMON_REQUIRED_KEYS: tuple[str, ...] = (
    "SLACK_SIGNING_SECRET",
    "SLACK_BOT_TOKEN",
    "SLACK_BOT_USER_ID",
    "IDEMPOTENCY_TABLE",
)

#: FRD §5.2에 없던 키지만, AC-SB-002-1(``boto3`` Lambda ``Invoke``로 비동기 handoff)이
#: worker의 ``FunctionName``을 필요로 해 TASK-012(handler.py)가 추가(DSN-SB-001: handler가
#: CTR-SB-002의 3초 budget 안에서 비동기로 넘기도록 두 Lambda로 분리). handler 전용 —
#: worker는 자기 자신을 invoke하지 않는다.
_HANDLER_REQUIRED_KEYS: tuple[str, ...] = ("WORKER_FUNCTION_NAME",)

_WORKER_REQUIRED_KEYS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "MCP_SERVER_URL",
    "SLACK_USER_TOKEN_MAP",
)


class ConfigError(Exception):
    """시작 시 환경변수 검증 실패 — 메시지에 이번 호출에서 발견된 문제를 전부 모아 담는다."""


class _FieldError(Exception):
    """내부 제어 흐름 전용 — 필드 하나의 사용자용 에러 메시지를 담아 전달한다."""


@dataclass(frozen=True, slots=True)
class HandlerSettings:
    """``slack-handler`` Lambda 진입점의 검증된 설정.

    ``signing_secret``/``bot_token``은 ``repr`` 제외 — 로그나 예외 출력으로 자격증명이
    새는 걸 막는다.
    """

    signing_secret: str = field(repr=False)
    bot_token: str = field(repr=False)
    bot_user_id: str
    idempotency_table: str
    worker_function_name: str
    idempotency_ttl_seconds: int
    max_response_chars: int
    log_level: str


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    """``slack-worker`` Lambda 진입점의 검증된 설정.

    ``signing_secret``/``bot_token``/``anthropic_api_key``/``user_token_map``는
    ``repr`` 제외 — ``user_token_map``의 값도 MCP bearer 토큰(CTR-SB-006)이라 나머지
    셋과 동일한 자격증명이다.
    """

    signing_secret: str = field(repr=False)
    bot_token: str = field(repr=False)
    bot_user_id: str
    anthropic_api_key: str = field(repr=False)
    mcp_server_url: str
    user_token_map: Mapping[str, str] = field(repr=False)
    idempotency_table: str
    idempotency_ttl_seconds: int
    max_response_chars: int
    log_level: str


def load_handler_settings(env: Mapping[str, str]) -> HandlerSettings:
    """``env``를 파싱/검증해 ``HandlerSettings``로 변환.

    필수 키(공통 또는 handler 전용) 누락, 형식/범위 오류 시 ``ConfigError`` — 모든 문제를
    모아 한 번에 던진다(DSN-006).
    """
    errors: list[str] = []

    required = _COMMON_REQUIRED_KEYS + _HANDLER_REQUIRED_KEYS
    missing = [key for key in required if not (env.get(key) or "").strip()]
    if missing:
        errors.append("missing required environment variable(s): " + ", ".join(missing))

    signing_secret = (env.get("SLACK_SIGNING_SECRET") or "").strip()
    bot_token = (env.get("SLACK_BOT_TOKEN") or "").strip()
    bot_user_id = (env.get("SLACK_BOT_USER_ID") or "").strip()
    idempotency_table = (env.get("IDEMPOTENCY_TABLE") or "").strip()
    worker_function_name = (env.get("WORKER_FUNCTION_NAME") or "").strip()

    try:
        max_response_chars = _parse_int_in_range(
            env.get("SLACKBOT_MAX_RESPONSE_CHARS"),
            "SLACKBOT_MAX_RESPONSE_CHARS",
            MAX_RESPONSE_CHARS_DEFAULT,
            MAX_RESPONSE_CHARS_MIN,
            MAX_RESPONSE_CHARS_MAX,
        )
    except _FieldError as exc:
        errors.append(str(exc))
        max_response_chars = MAX_RESPONSE_CHARS_DEFAULT

    try:
        idempotency_ttl_seconds = _parse_int_in_range(
            env.get("SLACKBOT_IDEMPOTENCY_TTL_SECONDS"),
            "SLACKBOT_IDEMPOTENCY_TTL_SECONDS",
            IDEMPOTENCY_TTL_SECONDS_DEFAULT,
            IDEMPOTENCY_TTL_SECONDS_MIN,
            IDEMPOTENCY_TTL_SECONDS_MAX,
        )
    except _FieldError as exc:
        errors.append(str(exc))
        idempotency_ttl_seconds = IDEMPOTENCY_TTL_SECONDS_DEFAULT

    try:
        log_level = _parse_log_level(env.get("SLACKBOT_LOG_LEVEL"))
    except _FieldError as exc:
        errors.append(str(exc))
        log_level = _DEFAULT_LOG_LEVEL

    if errors:
        raise ConfigError("; ".join(errors))

    return HandlerSettings(
        signing_secret=signing_secret,
        bot_token=bot_token,
        bot_user_id=bot_user_id,
        idempotency_table=idempotency_table,
        worker_function_name=worker_function_name,
        idempotency_ttl_seconds=idempotency_ttl_seconds,
        max_response_chars=max_response_chars,
        log_level=log_level,
    )


def load_worker_settings(env: Mapping[str, str]) -> WorkerSettings:
    """``env``를 파싱/검증해 ``WorkerSettings``로 변환.

    필수 키(공통 또는 worker 전용) 누락, 형식/범위/스키마 오류 시 ``ConfigError`` — 모든
    문제를 모아 한 번에 던진다(DSN-006).
    """
    errors: list[str] = []

    required = _COMMON_REQUIRED_KEYS + _WORKER_REQUIRED_KEYS
    missing = [key for key in required if not (env.get(key) or "").strip()]
    if missing:
        errors.append("missing required environment variable(s): " + ", ".join(missing))

    signing_secret = (env.get("SLACK_SIGNING_SECRET") or "").strip()
    bot_token = (env.get("SLACK_BOT_TOKEN") or "").strip()
    bot_user_id = (env.get("SLACK_BOT_USER_ID") or "").strip()
    anthropic_api_key = (env.get("ANTHROPIC_API_KEY") or "").strip()
    idempotency_table = (env.get("IDEMPOTENCY_TABLE") or "").strip()

    mcp_server_url = ""
    if "MCP_SERVER_URL" not in missing:
        try:
            mcp_server_url = _parse_url(env["MCP_SERVER_URL"], "MCP_SERVER_URL")
        except _FieldError as exc:
            errors.append(str(exc))

    user_token_map: Mapping[str, str] = {}
    if "SLACK_USER_TOKEN_MAP" not in missing:
        try:
            user_token_map = _parse_user_token_map(env["SLACK_USER_TOKEN_MAP"])
        except _FieldError as exc:
            errors.append(str(exc))

    try:
        max_response_chars = _parse_int_in_range(
            env.get("SLACKBOT_MAX_RESPONSE_CHARS"),
            "SLACKBOT_MAX_RESPONSE_CHARS",
            MAX_RESPONSE_CHARS_DEFAULT,
            MAX_RESPONSE_CHARS_MIN,
            MAX_RESPONSE_CHARS_MAX,
        )
    except _FieldError as exc:
        errors.append(str(exc))
        max_response_chars = MAX_RESPONSE_CHARS_DEFAULT

    try:
        idempotency_ttl_seconds = _parse_int_in_range(
            env.get("SLACKBOT_IDEMPOTENCY_TTL_SECONDS"),
            "SLACKBOT_IDEMPOTENCY_TTL_SECONDS",
            IDEMPOTENCY_TTL_SECONDS_DEFAULT,
            IDEMPOTENCY_TTL_SECONDS_MIN,
            IDEMPOTENCY_TTL_SECONDS_MAX,
        )
    except _FieldError as exc:
        errors.append(str(exc))
        idempotency_ttl_seconds = IDEMPOTENCY_TTL_SECONDS_DEFAULT

    try:
        log_level = _parse_log_level(env.get("SLACKBOT_LOG_LEVEL"))
    except _FieldError as exc:
        errors.append(str(exc))
        log_level = _DEFAULT_LOG_LEVEL

    if errors:
        raise ConfigError("; ".join(errors))

    return WorkerSettings(
        signing_secret=signing_secret,
        bot_token=bot_token,
        bot_user_id=bot_user_id,
        anthropic_api_key=anthropic_api_key,
        mcp_server_url=mcp_server_url,
        user_token_map=user_token_map,
        idempotency_table=idempotency_table,
        idempotency_ttl_seconds=idempotency_ttl_seconds,
        max_response_chars=max_response_chars,
        log_level=log_level,
    )


def _parse_url(raw: str, key: str) -> str:
    value = raw.strip()
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise _FieldError(f"{key} is not a valid http(s) URL: {value!r}")
    return value


def _load_json_object(raw: str, key: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        # str(exc)는 위치 정보만 담고 원문은 포함하지 않음 — 이 키의 값이 자격증명
        # JSON(CTR-SB-006)이라도 안전.
        raise _FieldError(f"{key} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise _FieldError(f"{key} must be a JSON object")
    return cast(dict[str, Any], data)


def _parse_user_token_map(raw: str) -> Mapping[str, str]:
    data = _load_json_object(raw, "SLACK_USER_TOKEN_MAP")

    mapping: dict[str, str] = {}
    problems: list[str] = []
    # 항목은 순번으로만 식별 — Slack user ID나 MCP 토큰을 echo하지 않는다(CTR-SB-006,
    # 이 에러가 로그로 흘러갈 수 있음).
    for index, (slack_user_id, token) in enumerate(data.items(), start=1):
        if not slack_user_id:
            problems.append(f"SLACK_USER_TOKEN_MAP entry #{index}: key must be a non-empty string")
            continue
        if not isinstance(token, str) or not token:
            problems.append(
                f"SLACK_USER_TOKEN_MAP entry #{index}: value must be a non-empty string"
            )
            continue
        mapping[slack_user_id] = token

    if problems:
        raise _FieldError("; ".join(problems))

    return mapping


def _parse_int_in_range(raw: str | None, key: str, default: int, minimum: int, maximum: int) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise _FieldError(f"{key} must be an integer, got {raw!r}") from exc
    if not (minimum <= value <= maximum):
        raise _FieldError(f"{key} must be between {minimum} and {maximum}, got {value}")
    return value


def _parse_log_level(raw: str | None) -> str:
    if raw is None or not raw.strip():
        return _DEFAULT_LOG_LEVEL
    value = raw.strip().upper()
    if value not in _VALID_LOG_LEVELS:
        raise _FieldError(
            f"SLACKBOT_LOG_LEVEL must be one of {sorted(_VALID_LOG_LEVELS)}, got {raw!r}"
        )
    return value
