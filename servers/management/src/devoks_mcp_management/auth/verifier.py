"""정적 토큰 테이블 기반 Bearer 인증 (DSN-001, CTR-002, AC-002-1, AC-002-6).

``StaticTableTokenVerifier``만이 Stage 1의 인증 방식이 ``MCP_CLIENT_TOKENS``
정적 테이블이라는 사실을 안다. 다른 모듈(guard, server 등)은 SDK의
``TokenVerifier`` 프로토콜과 ``AccessToken``만 소비한다 — Stage 2에서 IdP
introspection으로 교체될 때(FRD §10) 이 파일만 바뀌면 된다.

역할(role) 저장 위치
---------------------
``AccessToken``(설치된 ``mcp==2.1.1`` 기준)에는 role 필드가 없고 확장 포인트
``claims: dict[str, Any] | None``만 있다. 서브클래싱 대신
``claims[_ROLE_CLAIM_KEY]``에 role을 담아 반환한다 — SDK 미들웨어
(``BearerAuthBackend.authenticate`` / ``get_access_token()``)가
``verify_token``이 반환한 객체를 그대로 왕복시키는지 실제 소스를 읽어
확인했고, 서브클래스가 SDK 내부에서 조용히 base class로 좁혀질 위험을 없애기
위함이다. ``get_role``이 이 키의 유일한 접근자다 — 호출부는
``access_token.claims["role"]``을 직접 읽지 않는다.

타이밍 사이드채널
------------------
토큰 조회는 ``dict`` lookup이 아니라 테이블 전체를 순회하며 매 행마다
``secrets.compare_digest``로 비교한다(매치해도 멈추지 않음) — dict lookup의
O(1) 타이밍이 해시 버킷에 의존하는 특성을 배제하기 위해서다. 테이블 크기가
작아(Stage 1, 내부 서비스 클라이언트 소수) O(n) 스캔 비용은 무시 가능하다.

bytes 비교 이유 (TASK-043)
----------------------------
``secrets.compare_digest``는 두 ``str``이 모두 ASCII일 때만 동작하고, 아니면
``TypeError``를 던진다(로컬 재현 완료) — ``Authorization: Bearer 토큰``처럼
비 ASCII 값이 오면 의도한 401(AC-002-2) 대신 500이 나갔다. 양쪽을 UTF-8
``bytes``로 비교해 이 문제를 없앤다. 테이블 행은 요청마다가 아니라
``__init__``에서 한 번만 인코딩한다.
"""

import secrets
from collections.abc import Mapping

from mcp.server.auth.provider import AccessToken, TokenVerifier

from devoks_mcp_management.config import ClientToken, Settings

#: CTR-002 role를 담는 ``AccessToken.claims`` 키. ``get_role``을 통해서만
#: 읽는다 — 모듈 docstring 참고.
_ROLE_CLAIM_KEY = "role"


class StaticTableTokenVerifier:
    """CTR-002 정적 토큰 테이블 기반 `TokenVerifier`(DSN-001).

    `mcp.server.auth.provider.TokenVerifier`를 구조적으로 만족한다 — 해당
    Protocol은 `@runtime_checkable`이 아니므로(설치된 SDK 소스로 확인) 준수
    여부는 `isinstance` 체크가 아니라 pyright 정적 검사로 보장된다
    (`tests/test_verifier.py` 참고).
    """

    def __init__(self, client_tokens: Mapping[str, ClientToken]) -> None:
        """이미 파싱·검증된 토큰 테이블을 주입받는다.

        `config.load_settings`가 테이블 형태와 role/tool 일관성을 이미
        검증했다(AC-002-6 방향 — 토큰은 항상 `Settings`에서만 오고, 이 모듈의
        리터럴이나 모듈 전역의 환경 읽기로는 오지 않는다).
        """
        self._client_tokens = client_tokens
        # 요청마다가 아니라 여기서 한 번만 인코딩한다(TASK-043 — 모듈
        # docstring 참고). 스냅샷이 안전한 이유: `Settings`는 frozen
        # dataclass이고 `load_settings`가 만든 뒤 토큰 테이블은 변경되지 않는다.
        self._encoded_rows: tuple[tuple[bytes, ClientToken], ...] = tuple(
            (candidate.encode("utf-8"), entry) for candidate, entry in client_tokens.items()
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> StaticTableTokenVerifier:
        """composition root(TASK-008)를 위한 편의 생성자."""
        return cls(settings.client_tokens)

    async def verify_token(self, token: str) -> AccessToken | None:
        """`token`을 테이블에서 조회한다. 등록되지 않았으면 `None`.

        예외를 던지지 않고 토큰 자체를 로그에도 남기지 않는다(AC-004-3 방향)
        — 미등록 토큰은 SDK가 처리하는 401(AC-002-2) 이상으로 드러낼 에러가
        아니라 흔한 결과(오타, 폐기된 자격증명)일 뿐이다.

        비 ASCII를 포함한 **모든** `str`에서 예외 없이 동작한다 — UTF-8
        `bytes`로 비교하는 이유가 바로 이것(TASK-043, 재현은 모듈 docstring
        참고).
        """
        presented = token.encode("utf-8")
        matched: ClientToken | None = None
        # 상수 총 시간 스캔(모듈 docstring 참고) — 매치해도 루프를 일찍
        # 끝내지 않는다.
        for candidate, entry in self._encoded_rows:
            if secrets.compare_digest(candidate, presented):
                matched = entry
        if matched is None:
            return None
        return AccessToken(
            token=token,
            client_id=matched.client_id,
            scopes=list(matched.scopes),
            claims={_ROLE_CLAIM_KEY: matched.role},
        )


def get_role(access_token: AccessToken) -> str | None:
    """`StaticTableTokenVerifier`가 붙인 CTR-002 role을 읽어온다.

    `access_token.claims`에 role이 없으면(예: 다른 verifier가 만든
    `AccessToken`) `None` — 체크를 깜빡한 호출부도 `KeyError`/`TypeError`
    대신 깔끔하게 "role 없음"을 받는다.
    """
    if access_token.claims is None:
        return None
    role = access_token.claims.get(_ROLE_CLAIM_KEY)
    return role if isinstance(role, str) else None


#: `StaticTableTokenVerifier`가 `TokenVerifier`를 구조적으로 만족한다는
#: 정적(pyright strict) 증거. `TokenVerifier`는 `@runtime_checkable`이
#: 아니라서(설치된 `mcp==2.1.1`로 확인 — `isinstance` 체크 시 `TypeError`)
#: `isinstance` 대신 이 대입문을 매 `pyright` 실행마다 검사해 준수를
#: 보장한다(런타임 동작은 없음).
_conforms_to_token_verifier: TokenVerifier = StaticTableTokenVerifier({})
