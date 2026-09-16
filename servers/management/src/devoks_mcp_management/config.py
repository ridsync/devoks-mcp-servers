"""환경변수 -> 불변 ``Settings`` (DSN-006, Fail-Fast).

``load_settings``가 유일한 진입점이다. ``os.environ``을 직접 읽지 않고 환경변수 매핑을
명시적 인자로 받아, 테스트가 프로세스 전역 상태를 건드리지 않고 가짜 env를 주입할 수
있고 호출자(``app.py``/``server.py``)가 기동 실패 시점을 통제할 수 있다.

이번 호출에서 발견된 문제(누락 키, 범위 밖 숫자, 잘못된 JSON, 파싱 불가 PEM 등) 전부를
모아 ``ConfigError`` 하나로 한 번에 던진다(FRD §5.2, AC-001-5, AC-006-4) — 배포자가
수정-재기동을 N번이 아니라 1번만 거치게 하기 위함.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization

from devoks_mcp_management.types import (
    CORE_GITHUB_TOOLS,
    READ_FILE_MAX_BYTES_DEFAULT,
    READ_FILE_MAX_BYTES_MAX,
    READ_FILE_MAX_BYTES_MIN,
    SEARCH_CODE_MAX_RESULTS_DEFAULT,
    SEARCH_CODE_MAX_RESULTS_MAX,
    SEARCH_CODE_MAX_RESULTS_MIN,
    TOKEN_REFRESH_LEEWAY_SECONDS_DEFAULT,
    TOKEN_REFRESH_LEEWAY_SECONDS_MAX,
    TOKEN_REFRESH_LEEWAY_SECONDS_MIN,
)

#: 필수 키(FRD §5.2 / CTR-006). ``MCP_ALLOWED_HOSTS``가 권장이 아니라 필수인 이유 —
#: 빈 allowlist는 요청 시점에 요란하게 실패하지 않는다. ``transport_security``가 모든
#: 요청에 조용히 421만 반환해 호출자 눈엔 일반적인 전송 오류처럼 보인다(AC-001-5,
#: EDGE-002). 이걸 프로덕션 트래픽이 아니라 기동 시점에 잡는 게 Fail-Fast(DSN-006)의
#: 핵심이다.
_REQUIRED_KEYS: tuple[str, ...] = (
    "MCP_ALLOWED_HOSTS",
    "MCP_PUBLIC_URL",
    "MCP_ISSUER_URL",
    "MCP_CLIENT_TOKENS",
    "MCP_ROLE_TOOLS",
    "GITHUB_APP_ID",
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_APP_INSTALLATION_ID",
)

_DEFAULT_PORT = 8000
_MIN_PORT = 1
_MAX_PORT = 65535

_DEFAULT_LOG_LEVEL = "INFO"
_VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})

#: CTR-008: 'owner/repo' 정확히 일치, 와일드카드 없음 — GitHub 고유의 owner/repo
#: 문자셋으로 제한해 실수로 들어간 '*'나 '?'가 (절대 매치되지 않는) 리터럴 allowlist
#: 항목으로 조용히 수용되지 않고 즉시 거부되게 한다.
_REPO_ALLOWLIST_ENTRY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

#: FRD §5.1은 CTR-004/005/009를 범위 있는 숫자 계약으로 주지만 §5.2는 이들에 env var
#: 이름을 부여하지 않는다 — Stage 1엔 오버라이드 경로가 문서화돼 있지 않다. 아래 이름들은
#: 프로젝트의 ``MCP_`` 접두 컨벤션을 따라 운영자가 코드 변경 없이 기본값을 오버라이드할
#: 수 있게 한 것 — 이 gap-fill 결정은 TASK-003 handover 노트 참고.
_ENV_READ_FILE_MAX_BYTES = "MCP_READ_FILE_MAX_BYTES"
_ENV_SEARCH_CODE_MAX_RESULTS = "MCP_SEARCH_CODE_MAX_RESULTS"
_ENV_TOKEN_REFRESH_LEEWAY_SECONDS = "MCP_TOKEN_REFRESH_LEEWAY_SECONDS"
#: `MCP_CLIENT_TOKENS` 키의 최소 길이(TASK-046).
#:
#: 이 검증이 존재하는 이유 — 가정이 아니라 실제 사고: 배포된 Lambda가 이 저장소
#: 자신의 추적 대상 `.env.example`에 실린 그 placeholder 문자열
#: `dev-local-token-change-me`를 bearer 토큰으로 그대로 쓰고 있는 채 운영 중이었다.
#: 저장소는 public이고 Function URL도 커밋 메시지에 노출돼 있어 서비스가 사실상
#: 인터넷에 열려 있던 상태. 접근 로그에 제3자 IP는 없어 실제 유출은 없었지만
#: 노출 자체는 실재했다.
#:
#: 근본 원인은 구조적이다: `.env.example`의 `GITHUB_APP_PRIVATE_KEY` placeholder는
#: **의도적으로 무효**해서(`_parse_private_key`가 거부) 교체를 잊으면 기동 자체가
#: 실패한다. 반면 토큰 placeholder는 그런 장치가 없었다 — 완벽하게 유효한 토큰
#: 테이블이었으므로 `.env.example`을 그대로 복사해도 *동작하는* 서버가 *공개된*
#: 자격증명을 들고 아무 신호 없이 떠 있었다.
#:
#: CTR-002가 토큰 형식을 규정하지 않으므로 32자는 특정 포맷이 아니라 하한선이다.
#: `secrets.token_urlsafe(32)`는 43자/256비트로 여유 있게 상회하고, 사람이 손으로
#: 타이핑한 값은 대부분 미달한다. `.env.example` placeholder도 이제 **의도적으로
#: 이 기준을 통과 못 하는 길이**로 바꿔뒀으므로, placeholder가 조용히 프로덕션까지
#: 흘러갈 수 없다.
_MIN_CLIENT_TOKEN_LENGTH = 32

_ENV_STATELESS_HTTP = "MCP_STATELESS_HTTP"
_ENV_JSON_RESPONSE = "MCP_JSON_RESPONSE"

# CTR-011: 둘 다 기본값 True인 이유는 배포 대상이 Lambda + Function URL이기 때문
# (FRD §10 Stage 2). Lambda 실행 환경은 호출 사이에 얼어붙고 예고 없이 교체되므로,
# `Mcp-Session-Id`를 발급하는 서버는 이후 호출이 그 세션을 계속 들고 있음을 보장 못 할
# 클라이언트에게 세션을 쥐여주는 셈이고, 응답보다 오래 사는 SSE 스트림도 이 freeze와
# 상충한다. 같은 이미지를 sticky-session 로드밸런서 뒤에서 돌리려면(FRD §7 옵션 (a))
# 둘 다 `false`로 뒤집는다.
_DEFAULT_STATELESS_HTTP = True
_DEFAULT_JSON_RESPONSE = True

# 두 boolean 키가 허용하는 표기. Python의 `bool(str)`("false"도 truthy로 만듦)나
# `distutils.util.strtobool`(3.12에서 제거됨) 대신 의도적으로 닫힌 집합을 쓴다 —
# 오타 값은 DSN-006에 따라 기동 실패여야지 조용히 잘못된 프로토콜 모드가 되면 안 된다.
_TRUE_LITERALS = frozenset({"1", "true", "yes", "on"})
_FALSE_LITERALS = frozenset({"0", "false", "no", "off"})


class ConfigError(Exception):
    """시작 시 환경변수 검증 실패 — 메시지에 이번 호출에서 발견된 문제를 전부 모아 담는다."""


class _FieldError(Exception):
    """내부 제어 흐름 전용 — 필드 하나의 사용자용 에러 메시지를 담아 전달한다."""


@dataclass(frozen=True, slots=True)
class ClientToken:
    """CTR-002 토큰 테이블의 행 하나 — bearer 토큰 자체를 키로 삼는다."""

    client_id: str
    role: str
    scopes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Settings:
    """불변이고 완전히 검증된 서버 설정.

    반드시 ``load_settings``로만 생성 — 이 객체가 존재하는 시점엔 모든 필드가 이미
    범위/스키마/PEM 검증을 통과한 상태다. 토큰 테이블과 GitHub App private key는
    ``repr``에서 제외해, ``Settings`` 인스턴스를 로그로 찍거나 예외로 raise해도
    비밀이 새지 않게 한다.
    """

    allowed_hosts: tuple[str, ...]
    public_url: str
    issuer_url: str
    repo_allowlist: frozenset[str]
    role_tools: Mapping[str, frozenset[str]]
    github_app_id: str
    github_app_installation_id: str
    port: int
    log_level: str
    read_file_max_bytes: int
    search_code_max_results: int
    token_refresh_leeway_seconds: int
    stateless_http: bool
    json_response: bool
    client_tokens: Mapping[str, ClientToken] = field(repr=False)
    github_app_private_key: str = field(repr=False)


def load_settings(env: Mapping[str, str]) -> Settings:
    """``env``를 파싱/검증해 ``Settings``로 변환.

    필수 키 누락, 형식/범위/스키마 오류 시 ``ConfigError`` — 모든 문제를 모아 한 번에
    던진다.
    """
    errors: list[str] = []

    missing = [key for key in _REQUIRED_KEYS if not (env.get(key) or "").strip()]
    if missing:
        errors.append("missing required environment variable(s): " + ", ".join(missing))

    allowed_hosts: tuple[str, ...] = ()
    if "MCP_ALLOWED_HOSTS" not in missing:
        try:
            allowed_hosts = _parse_allowed_hosts(env["MCP_ALLOWED_HOSTS"])
        except _FieldError as exc:
            errors.append(str(exc))

    public_url = ""
    if "MCP_PUBLIC_URL" not in missing:
        try:
            public_url = _parse_public_url(env["MCP_PUBLIC_URL"])
        except _FieldError as exc:
            errors.append(str(exc))

    issuer_url = ""
    if "MCP_ISSUER_URL" not in missing:
        try:
            issuer_url = _parse_url(env["MCP_ISSUER_URL"], "MCP_ISSUER_URL")
        except _FieldError as exc:
            errors.append(str(exc))

    client_tokens: Mapping[str, ClientToken] = {}
    if "MCP_CLIENT_TOKENS" not in missing:
        try:
            client_tokens = _parse_client_tokens(env["MCP_CLIENT_TOKENS"])
        except _FieldError as exc:
            errors.append(str(exc))

    role_tools: Mapping[str, frozenset[str]] = {}
    if "MCP_ROLE_TOOLS" not in missing:
        try:
            role_tools = _parse_role_tools(env["MCP_ROLE_TOOLS"])
        except _FieldError as exc:
            errors.append(str(exc))

    if client_tokens and role_tools:
        try:
            _validate_token_roles(client_tokens, role_tools)
        except _FieldError as exc:
            errors.append(str(exc))

    github_app_id = (env.get("GITHUB_APP_ID") or "").strip()
    github_app_installation_id = (env.get("GITHUB_APP_INSTALLATION_ID") or "").strip()

    github_app_private_key = ""
    if "GITHUB_APP_PRIVATE_KEY" not in missing:
        try:
            github_app_private_key = _parse_private_key(env["GITHUB_APP_PRIVATE_KEY"])
        except _FieldError as exc:
            errors.append(str(exc))

    # MCP_REPO_ALLOWLIST: 없거나 비어 있는 게 fail-safe 기본값(EDGE-001) — 빈
    # allowlist는 모든 repo를 거부하므로 missing-key 실패로 취급하지 않는다. 위의
    # 다른 모든 키와 반대 방향.
    repo_allowlist: frozenset[str]
    try:
        repo_allowlist = _parse_repo_allowlist(env.get("MCP_REPO_ALLOWLIST", ""))
    except _FieldError as exc:
        errors.append(str(exc))
        repo_allowlist = frozenset()

    try:
        port = _parse_int_in_range(
            env.get("MCP_PORT"), "MCP_PORT", _DEFAULT_PORT, _MIN_PORT, _MAX_PORT
        )
    except _FieldError as exc:
        errors.append(str(exc))
        port = _DEFAULT_PORT

    try:
        log_level = _parse_log_level(env.get("MCP_LOG_LEVEL"))
    except _FieldError as exc:
        errors.append(str(exc))
        log_level = _DEFAULT_LOG_LEVEL

    try:
        read_file_max_bytes = _parse_int_in_range(
            env.get(_ENV_READ_FILE_MAX_BYTES),
            _ENV_READ_FILE_MAX_BYTES,
            READ_FILE_MAX_BYTES_DEFAULT,
            READ_FILE_MAX_BYTES_MIN,
            READ_FILE_MAX_BYTES_MAX,
        )
    except _FieldError as exc:
        errors.append(str(exc))
        read_file_max_bytes = READ_FILE_MAX_BYTES_DEFAULT

    try:
        search_code_max_results = _parse_int_in_range(
            env.get(_ENV_SEARCH_CODE_MAX_RESULTS),
            _ENV_SEARCH_CODE_MAX_RESULTS,
            SEARCH_CODE_MAX_RESULTS_DEFAULT,
            SEARCH_CODE_MAX_RESULTS_MIN,
            SEARCH_CODE_MAX_RESULTS_MAX,
        )
    except _FieldError as exc:
        errors.append(str(exc))
        search_code_max_results = SEARCH_CODE_MAX_RESULTS_DEFAULT

    try:
        token_refresh_leeway_seconds = _parse_int_in_range(
            env.get(_ENV_TOKEN_REFRESH_LEEWAY_SECONDS),
            _ENV_TOKEN_REFRESH_LEEWAY_SECONDS,
            TOKEN_REFRESH_LEEWAY_SECONDS_DEFAULT,
            TOKEN_REFRESH_LEEWAY_SECONDS_MIN,
            TOKEN_REFRESH_LEEWAY_SECONDS_MAX,
        )
    except _FieldError as exc:
        errors.append(str(exc))
        token_refresh_leeway_seconds = TOKEN_REFRESH_LEEWAY_SECONDS_DEFAULT

    try:
        stateless_http = _parse_bool(
            env.get(_ENV_STATELESS_HTTP), _ENV_STATELESS_HTTP, _DEFAULT_STATELESS_HTTP
        )
    except _FieldError as exc:
        errors.append(str(exc))
        stateless_http = _DEFAULT_STATELESS_HTTP

    try:
        json_response = _parse_bool(
            env.get(_ENV_JSON_RESPONSE), _ENV_JSON_RESPONSE, _DEFAULT_JSON_RESPONSE
        )
    except _FieldError as exc:
        errors.append(str(exc))
        json_response = _DEFAULT_JSON_RESPONSE

    if errors:
        raise ConfigError("; ".join(errors))

    return Settings(
        allowed_hosts=allowed_hosts,
        public_url=public_url,
        issuer_url=issuer_url,
        repo_allowlist=repo_allowlist,
        role_tools=role_tools,
        github_app_id=github_app_id,
        github_app_installation_id=github_app_installation_id,
        port=port,
        log_level=log_level,
        read_file_max_bytes=read_file_max_bytes,
        search_code_max_results=search_code_max_results,
        token_refresh_leeway_seconds=token_refresh_leeway_seconds,
        stateless_http=stateless_http,
        json_response=json_response,
        client_tokens=client_tokens,
        github_app_private_key=github_app_private_key,
    )


def _parse_allowed_hosts(raw: str) -> tuple[str, ...]:
    hosts = tuple(host.strip() for host in raw.split(",") if host.strip())
    if not hosts:
        raise _FieldError("MCP_ALLOWED_HOSTS must list at least one host")
    return hosts


def _parse_url(raw: str, key: str) -> str:
    value = raw.strip()
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise _FieldError(f"{key} is not a valid http(s) URL: {value!r}")
    return value


#: TASK-009 실측 결과(FRD §5.2): SDK는 CTR-001의 well-known discovery 경로를
#: MCP_PUBLIC_URL의 *path 구성요소*에서 유도하는데, path가 잘못 설정돼도 서버 기동은
#: 그 자체로 실패하지 않는다 — RFC 9728 discovery 경로만 조용히
#: '/.well-known/oauth-protected-resource/mcp'가 아닌 다른 곳으로 어긋날 뿐. 빈
#: MCP_ALLOWED_HOSTS와 같은 부류의 조용한 실패라 클라이언트가 발견하게 두지 않고
#: 여기서 잡는다(DSN-006). path *prefix*(예: '/management/mcp')는 정당한
#: reverse-proxy 토폴로지(Stage 2)이므로 계속 허용해야 한다 — 거부 대상은 빈/루트
#: path, trailing slash, 마지막 세그먼트가 'mcp'가 아닌 경우뿐이다.
def _parse_public_url(raw: str) -> str:
    value = _parse_url(raw, "MCP_PUBLIC_URL")
    path = urlsplit(value).path
    if path in ("", "/"):
        raise _FieldError(
            "MCP_PUBLIC_URL must include a path ending in '/mcp' — the well-known "
            "discovery path (CTR-001) is derived from this URL's path, e.g. "
            f"'https://host/mcp'; got {value!r} with no path"
        )
    if path.endswith("/"):
        raise _FieldError(
            "MCP_PUBLIC_URL must not have a trailing slash — it carries into the "
            f"well-known discovery path, e.g. use 'https://host/mcp' not {value!r}"
        )
    if path.rsplit("/", 1)[-1] != "mcp":
        raise _FieldError(
            "MCP_PUBLIC_URL's last path segment must be 'mcp' to match the "
            "server's MCP endpoint (CTR-001), e.g. 'https://host/mcp' or, behind "
            f"a reverse-proxy path prefix, 'https://host/management/mcp'; got {value!r}"
        )
    return value


def _parse_repo_allowlist(raw: str) -> frozenset[str]:
    entries = tuple(entry.strip() for entry in raw.split(",") if entry.strip())
    invalid = [entry for entry in entries if not _REPO_ALLOWLIST_ENTRY.match(entry)]
    if invalid:
        raise _FieldError(
            "MCP_REPO_ALLOWLIST entries must be 'owner/repo' with no wildcards, "
            f"invalid: {', '.join(invalid)}"
        )
    return frozenset(entries)


def _load_json_object(raw: str, key: str) -> dict[str, Any]:
    """``raw``를 JSON으로 파싱하고 object임을 요구한다.

    ``json.loads``는 ``Any``를 반환한다 — 나머지 모듈이 필요로 하는 cast 하나를 여기
    한곳에 모아, 호출부마다 ``isinstance`` narrowing으로 ``Unknown``을 재도출하지 않고
    (pyright는 ``isinstance``로 ``Any``를 narrowing하면 ``Any``가 아니라 ``Unknown``이
    됨) 제대로 타입된 dict를 바로 쓰게 한다.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _FieldError(f"{key} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise _FieldError(f"{key} must be a JSON object")
    return cast(dict[str, Any], data)


def _as_str_list(value: object) -> list[str] | None:
    """``value``를 ``list[str]``로 반환, 아니면 ``None``."""
    if not isinstance(value, list):
        return None
    items = cast(list[Any], value)
    if not all(isinstance(item, str) for item in items):
        return None
    return cast(list[str], items)


def _parse_client_tokens(raw: str) -> Mapping[str, ClientToken]:
    data = _load_json_object(raw, "MCP_CLIENT_TOKENS")

    tokens: dict[str, ClientToken] = {}
    problems: list[str] = []
    # 항목은 순번으로만 식별 — 토큰 값 자체를 echo하지 않는다. 여기 JSON 키가 곧
    # bearer 자격증명이고, 이 에러는 로그로 흘러갈 수 있다.
    for index, (token, entry) in enumerate(data.items(), start=1):
        if not token:
            problems.append(f"MCP_CLIENT_TOKENS entry #{index}: key must be a non-empty string")
            continue
        if len(token) < _MIN_CLIENT_TOKEN_LENGTH:
            # 길이는 보고하되 토큰 자체는 남기지 않는다 — 이 메시지도 로그로 흘러갈 수
            # 있고, 너무 짧은 토큰도 여전히 자격증명이다.
            problems.append(
                f"MCP_CLIENT_TOKENS entry #{index}: token must be at least "
                f"{_MIN_CLIENT_TOKEN_LENGTH} characters, got {len(token)}. "
                "Generate one with: python3 -c "
                "'import secrets; print(secrets.token_urlsafe(32))'"
            )
            continue
        if not isinstance(entry, dict):
            problems.append(f"MCP_CLIENT_TOKENS entry #{index}: value must be an object")
            continue
        entry_obj = cast(dict[str, Any], entry)
        client_id = entry_obj.get("client_id")
        role = entry_obj.get("role")
        scopes = _as_str_list(entry_obj.get("scopes"))
        if not isinstance(client_id, str) or not client_id:
            problems.append(
                f"MCP_CLIENT_TOKENS entry #{index}: 'client_id' must be a non-empty string"
            )
            continue
        if not isinstance(role, str) or not role:
            problems.append(f"MCP_CLIENT_TOKENS entry #{index}: 'role' must be a non-empty string")
            continue
        if scopes is None:
            problems.append(f"MCP_CLIENT_TOKENS entry #{index}: 'scopes' must be a list of strings")
            continue
        tokens[token] = ClientToken(client_id=client_id, role=role, scopes=tuple(scopes))

    if problems:
        raise _FieldError("; ".join(problems))

    return tokens


def _parse_role_tools(raw: str) -> Mapping[str, frozenset[str]]:
    data = _load_json_object(raw, "MCP_ROLE_TOOLS")

    role_tools: dict[str, frozenset[str]] = {}
    problems: list[str] = []
    for role, tool_names_raw in data.items():
        if not role:
            problems.append("MCP_ROLE_TOOLS keys must be non-empty role names")
            continue
        tool_names = _as_str_list(tool_names_raw)
        if tool_names is None:
            problems.append(f"MCP_ROLE_TOOLS role {role!r} must map to a list of tool names")
            continue
        # 서버가 실제로 노출하지 않는 tool을 부여하는 role은 거부 — 그냥 두면 조용한
        # 인가 구멍이 된다(types.py의 CORE_GITHUB_TOOLS docstring 참고).
        unknown = sorted(set(tool_names) - CORE_GITHUB_TOOLS)
        if unknown:
            problems.append(
                f"MCP_ROLE_TOOLS role {role!r} references unknown tool(s): {', '.join(unknown)}"
            )
            continue
        role_tools[role] = frozenset(tool_names)

    if problems:
        raise _FieldError("; ".join(problems))

    return role_tools


def _validate_token_roles(
    client_tokens: Mapping[str, ClientToken],
    role_tools: Mapping[str, frozenset[str]],
) -> None:
    unknown_roles = sorted({token.role for token in client_tokens.values()} - role_tools.keys())
    if unknown_roles:
        raise _FieldError(
            "MCP_CLIENT_TOKENS references role(s) not present in MCP_ROLE_TOOLS: "
            + ", ".join(unknown_roles)
        )


def _parse_private_key(raw: str) -> str:
    # 한 줄짜리 env var로 주입된 PEM은 보통 실제 줄바꿈 대신 리터럴 "\n" 이스케이프
    # 시퀀스로 도착한다 — 파싱 전에 정규화한다.
    normalized = raw.replace("\\n", "\n").strip()
    try:
        serialization.load_pem_private_key(normalized.encode("utf-8"), password=None)
    except (ValueError, TypeError) as exc:
        # 메시지에 키 자료를 절대 포함하지 않는다(EDGE-008, AC-004-3 방향) — 오직
        # GITHUB_APP_PRIVATE_KEY 파싱 실패 사실만.
        raise _FieldError(
            f"GITHUB_APP_PRIVATE_KEY is not a valid PEM private key ({type(exc).__name__})"
        ) from exc
    return normalized


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


def _parse_bool(raw: str | None, key: str, default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in _TRUE_LITERALS:
        return True
    if value in _FALSE_LITERALS:
        return False
    raise _FieldError(
        f"{key} must be one of {sorted(_TRUE_LITERALS | _FALSE_LITERALS)}, got {raw!r}"
    )


def _parse_log_level(raw: str | None) -> str:
    if raw is None or not raw.strip():
        return _DEFAULT_LOG_LEVEL
    value = raw.strip().upper()
    if value not in _VALID_LOG_LEVELS:
        raise _FieldError(f"MCP_LOG_LEVEL must be one of {sorted(_VALID_LOG_LEVELS)}, got {raw!r}")
    return value
