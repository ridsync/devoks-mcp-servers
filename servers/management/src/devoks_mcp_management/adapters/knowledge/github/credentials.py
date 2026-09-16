"""GitHub App installation access token 공급자(TASK-020, DSN-004, REQ-006).

이 서버가 갖는 **유일한** 가변 프로세스 상태(FRD §4.2 "상태 소유 결정")를
소유한다 — 캐시된 installation access token과 절대 만료 시각. 그 외
tools/policy/audit는 전부 stateless가 원칙이며, 이 모듈이 의도적으로
분리된 예외다.

**2단계 자격증명 흐름**(``RES-API-001``, GitHub 공식 App 인증 흐름)
1. **App JWT** — App RSA private key로 단기 JWT 서명. claim은 GitHub 문서
   그대로: ``iss`` = App ID(``GITHUB_APP_ID`` 값을 그대로 pass-through,
   client ID/App ID 둘 다 허용됨), ``iat`` = now − 60초(clock drift 여유,
   GitHub 문서 권장값), ``exp`` = ``iat`` + 600초(``_build_app_jwt`` 참고
   — ``now``가 아니라 ``iat`` 기준으로 재는 이유는 해당 함수 docstring).
   알고리즘은 RS256(GitHub App key는 항상 RSA).
2. **Installation token 교환** — ``POST
   /app/installations/{installation_id}/access_tokens``를 그 JWT를
   Bearer로 써서 호출(installation token이 아니라 App 레벨 자격증명을
   직접 쓰는 이 어댑터 전체에서 유일한 호출), ``Accept``/
   ``X-GitHub-Api-Version`` 헤더 포함. 응답의 ``token``(``ghs_...``)과
   ``expires_at``(ISO 8601, 1시간 뒤)을 캐시한다.

**호출부(TASK-021 REST 클라이언트) 사용 패턴**: ``get_token()``은 GitHub
REST 호출마다 매번 부르는 게 맞다(cache-hit 경로가 lock contention 없이
저렴함) — 반환값을 호출부가 따로 캐싱하면 만료가 요청 중간에 새어들 수
있다.

**캐싱 경계 및 ``AC-006-5``/``EDGE-007`` 병합**: 남은 수명이 ``CTR-009``
leeway보다 **엄격히 커야**(``AC-006-2``) 캐시를 재사용하고, leeway
이하면(``AC-006-3``, "이하"는 경계 포함) 재발급한다. leeway 값 자체의
범위 검증은 ``config.load_settings``가 이미 수행하므로 이 모듈은
``Settings``가 넘긴 값을 그대로 비교만 한다.

동시 호출자가 전부 무효 캐시를 관측해도 실제 발급 요청은 double-checked
locking + 공유 ``asyncio.Task``로 정확히 1건에 병합된다:

1. lock 밖에서 먼저 캐시를 확인(``await`` 없이 저렴, 두 속성 읽기 사이에
   suspend가 없어 협력적 스케줄링 하에서 안전).
2. 무효면 lock을 잡고 재확인 — lock 대기 중 다른 코루틴이 이미 갱신을
   시작했다면 그 결과를 재사용해야지 두 번째 갱신을 시작하면 안 된다.
3. 캐시와 in-flight 슬롯이 **둘 다** 빈 코루틴만, lock을 잡은 상태에서
   갱신 task를 생성(``asyncio.ensure_future``)해 ``self._inflight_refresh``
   에 저장하고, 다른 호출자가 관측하기 전에 ``_on_inflight_refresh_done``
   을 done-callback으로 건다(아래 6번). 나머지 호출자는 이미 있는 task를
   찾아 그걸 기다린다(아래 5번).
4. ``asyncio.Task``는 다수의 동시 대기자에게 동일한 결과를 주므로 성공뿐
   아니라 **실패도 병합**된다 — 발급 요청이 실패하면 병합된 모든
   호출자가 같은 ``InstallationTokenError``를 받는다(무한 대기하거나
   stale/빈 토큰을 받는 호출자는 없음).
5. **``EDGE-015``** — 모든 대기자는 ``await inflight``가 아니라
   ``asyncio.shield(inflight)``로 기다린다. asyncio는 대기 중인
   Future/Task로 취소를 전파하므로(``Task.cancel()``이 현재 await 중인
   대상까지 취소), shield가 없으면 한 호출자의 취소(클라이언트 연결
   끊김, ALB idle timeout, 호출부의 ``asyncio.wait_for`` 등)가 공유
   갱신 자체를 취소시켜 무관한 다른 모든 호출자까지 끌고 들어간다(코드
   리뷰 중 별도 asyncio 스크립트로 독립 재현 확인). ``shield``는 바깥쪽
   future를 하나 끼워 넣어 대기자 개별 취소가 ``inflight``엔 절대 닿지
   않게 막는다 — 갱신은 계속 진행되고 다른 대기자들의 shield는 정상
   resolve된다.
6. 슬롯 정리와 캐시 쓰기는 task 자신이 ``_on_inflight_refresh_done``
   (생성 시점에 ``self._inflight_refresh``에 직접 건, 단 한 번뿐인 동기
   ``add_done_callback``)으로 수행한다 — 마지막까지 기다리던 호출자가
   아니라. ``shield``만으론 부족한 지점이다: 정리가 각 대기자의
   ``finally``에 있었다면(이 콜백 도입 전 방식) 취소된 대기자의
   ``finally``도 취소와 무관하게 실행되므로, ``inflight``가 아직 도는데
   슬롯을 비워버려 다음 ``get_token()``이 **중복** 갱신을 새로 시작할 수
   있다(``AC-006-5`` 위반). done-callback은 정확히 한 번, task가 실제로
   끝날 때만 실행되고 — ``inflight``에 어떤 대기자의 ``shield()``보다도
   먼저 등록돼 있어(asyncio는 Future의 done-callback을 등록 순서대로
   실행) 항상 대기자의 재개보다 먼저 끝난다. 그래서 슬롯 정리와(성공 시)
   ``self._cached_token``/``self._cached_expires_at`` 쓰기가 어떤
   대기자의 완료 관측보다도 먼저 끝나고, 실패 시엔 캐시에 아무것도
   쓰이지 않는다(FRD §5.4 "발급 실패 → 캐시 없음" 그대로). 모든 대기자가
   취소돼도 ``inflight``는 중단되지 않고 orphan으로 완주하며, 콜백이
   여전히 슬롯을 정리하고(성공 시) 다음 ``get_token()``이 재사용할 캐시를
   채운다. 실패 시에도 ``task.exception()``을 호출해, 아무도 기다리지
   않은 task가 GC될 때 asyncio의 "Task exception was never retrieved"
   경고가 뜨지 않게 한다.

**clock 주입**: GitHub의 절대 ``expires_at``과 비교해야 하므로 항상
wall-clock(``time.time``, epoch 초)을 쓰고 모노토닉 clock은 쓰지 않는다
(모노토닉 값은 GitHub가 돌려준 절대 타임스탬프와 아무 관계가 없다).
``clock: Clock = time.time``을 주입받아 테스트가 "지금"을 결정론적으로
고정할 수 있게 하며, 같은 clock이 JWT의 ``iat``/``exp``도 찍는다.

**주입된 HTTP 클라이언트의 소유권**: ``http_client: httpx2.AsyncClient``는
생성자 주입이며 여기서 만들지 않는다(``DSN-004`` — ASGI lifespan
(TASK-023)이 프로세스 전체에서 클라이언트 1개를 소유하고 이 provider와
TASK-021 REST 클라이언트가 함께 공유). 이 provider의 ``aclose()``는
자신의 캐시된 토큰 상태만 지우고 주입된 클라이언트는 절대 닫지 않는다 —
닫는 건 lifespan의 몫이다.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any, Final, cast

import httpx2
import jwt

from devoks_mcp_management.config import Settings

__all__ = ["InstallationTokenError", "InstallationTokenProvider"]

#: Wall-clock epoch 초(`time.time` 형태), 모노토닉 아님 — 모듈 docstring
#: "clock 주입" 절 참고.
Clock = Callable[[], float]

#: App JWT `iat`의 GitHub 문서 권장 clock-drift 여유.
_APP_JWT_CLOCK_DRIFT_BACKDATE_SECONDS: Final = 60

#: GitHub 문서상 App JWT 최대 수명.
_APP_JWT_TTL_SECONDS: Final = 600

_APP_JWT_ALGORITHM: Final = "RS256"

_GITHUB_API_BASE_URL: Final = "https://api.github.com"
_GITHUB_ACCEPT_HEADER: Final = "application/vnd.github+json"
_GITHUB_API_VERSION_HEADER: Final = "2022-11-28"

#: `InstallationTokenError`에 담기는 GitHub 에러 바디의 절단 길이 —
#: 유용할 만큼 길고, 비정상적인 응답 바디가 로그 줄을 부풀리지 않을 만큼
#: 짧게.
_ERROR_BODY_SNIPPET_LEN: Final = 200


class InstallationTokenError(Exception):
    """installation access token을 얻지 못했을 때 발생.

    메시지에 App private key, 서명된 App JWT, 이전에 발급된 installation
    token을 절대 포함하지 않는다(`AC-004-3` 방향) — `TASK-021`의 에러
    정규화를 거쳐 tool 에러 응답이나 감사 로그로 흘러갈 수 있다. 상태
    코드와 GitHub 자체의 (토큰 없는) 에러 응답 본문만 포함해도 안전하다.
    """


class InstallationTokenProvider:
    """GitHub App installation token 1개를 캐싱·갱신하고, 동시 갱신을 병합한다.

    1회 생성(보통 `from_settings`)해 인스턴스를 공유한다 — 프로세스가
    갖는 유일한 가변 상태의 단일 소유자(모듈 docstring 참고). context
    manager가 아님: 수명이 lifespan 범위(`DSN-004`)라 `async with`가
    아니라 `aclose()`를 명시적으로 호출한다.
    """

    def __init__(
        self,
        *,
        app_id: str,
        private_key: str,
        installation_id: str,
        http_client: httpx2.AsyncClient,
        refresh_leeway_seconds: int,
        clock: Clock = time.time,
    ) -> None:
        self._app_id = app_id
        self._private_key = private_key
        self._installation_id = installation_id
        self._http_client = http_client
        self._refresh_leeway_seconds = refresh_leeway_seconds
        self._clock = clock

        self._lock = asyncio.Lock()
        self._cached_token: str | None = None
        #: epoch 초(`clock()`와 같은 단위), 캐시된 토큰이 없으면 `None`.
        self._cached_expires_at: float | None = None
        #: 유일한 in-flight 갱신 — 병합된 모든 동시 호출자가 공유
        #: (`AC-006-5`/`EDGE-007`, 모듈 docstring 참고). 진행 중인 갱신이
        #: 없으면 `None`.
        self._inflight_refresh: asyncio.Task[tuple[str, float]] | None = None

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        http_client: httpx2.AsyncClient,
        clock: Clock = time.time,
    ) -> InstallationTokenProvider:
        """composition root(`TASK-023`의 lifespan)용 편의 생성자."""
        return cls(
            app_id=settings.github_app_id,
            private_key=settings.github_app_private_key,
            installation_id=settings.github_app_installation_id,
            http_client=http_client,
            refresh_leeway_seconds=settings.token_refresh_leeway_seconds,
            clock=clock,
        )

    async def get_token(self) -> str:
        """유효한 installation access token을 반환, 필요하면 갱신(`AC-006-1`).

        캐싱 경계(`AC-006-2`/`AC-006-3`)와 동시 갱신 병합(`AC-006-5`/
        `EDGE-007`/`EDGE-015`)은 모듈 docstring 참고.
        """
        if self._has_valid_cached_token():
            return cast(str, self._cached_token)

        async with self._lock:
            # Double-checked: lock 대기 중 다른 코루틴이 이미 갱신 중이었다면
            # 그 결과를 재사용해야지 두 번째 갱신을 시작하면 안 된다.
            if self._has_valid_cached_token():
                return cast(str, self._cached_token)
            if self._inflight_refresh is None:
                inflight = asyncio.ensure_future(self._issue_installation_token())
                # 아래 어떤 대기자의 `shield()`가 같은 task에 콜백을
                # 등록하기 전에 여기서 먼저 건다 — 항상 가장 먼저
                # 실행되는 done-callback임을 보장(`EDGE-015`, 모듈
                # docstring 참고).
                inflight.add_done_callback(self._on_inflight_refresh_done)
                self._inflight_refresh = inflight
            inflight = self._inflight_refresh

        # `asyncio.shield`: 이 호출자의 취소는 `shield`가 반환한 wrapper
        # future에만 닿아야지 `inflight` 자체엔 닿으면 안 된다
        # (`EDGE-015`, 모듈 docstring 참고).
        token, _expires_at = await asyncio.shield(inflight)
        return token

    def _on_inflight_refresh_done(self, task: asyncio.Task[tuple[str, float]]) -> None:
        """슬롯 정리 + 캐시 쓰기, 갱신 task 자신이 소유(`EDGE-015`).

        대기 측 `finally`가 아니라 동기 `add_done_callback`으로 실행 —
        그 차이가 왜 취소된 대기자가 아직 도는 갱신을 무너뜨리거나
        슬롯을 열어둔 채로 만드는 걸 막는지는 모듈 docstring 참고. lock
        불필요: 이 콜백엔 `await`가 없어 asyncio가 다른 코루틴과 끼워
        넣을 수 없으므로, 아래 슬롯 확인·쓰기는 `get_token`에 대해
        사실상 원자적이다.
        """
        if self._inflight_refresh is task:
            self._inflight_refresh = None
        if task.cancelled():
            return
        # 이 콜백 실행 전에 모든 대기자가 취소돼 아무도 `task`의
        # `.result()`/`.exception()`을 안 불러도 항상 호출한다 — 안
        # 그러면 GC 시점에 asyncio가 "Task exception was never retrieved"
        # 를 로그로 남긴다. (3.14는 `asyncio.shield`도 all-cancelled
        # 케이스를 자체 fallback 콜백으로 따로 방어하지만, stdlib 내부
        # 동작에 기대지 않고 이 모듈이 자체적으로 처리하기 위해 여기서
        # 명시적으로 호출한다.)
        error = task.exception()
        if error is not None:
            return
        token, expires_at = task.result()
        self._cached_token = token
        self._cached_expires_at = expires_at

    async def aclose(self) -> None:
        """캐시된 토큰을 지운다. 주입된 HTTP 클라이언트는 절대 닫지 않는다(모듈 docstring 참고)."""
        async with self._lock:
            self._cached_token = None
            self._cached_expires_at = None

    def _has_valid_cached_token(self) -> bool:
        """`AC-006-2`/`AC-006-3` 경계: 남은 수명이 leeway보다 엄격히 커야 유효.

        정확히 leeway와 같으면 "재발급"에 해당(`AC-006-3`의 "이하"는
        경계 포함) — 그래서 이 값에선 `True`가 아니라 `False`를 반환한다.
        """
        if self._cached_token is None or self._cached_expires_at is None:
            return False
        remaining = self._cached_expires_at - self._clock()
        return remaining > self._refresh_leeway_seconds

    def _build_app_jwt(self, now: float) -> str:
        """installation token 교환에 쓸 App JWT를 서명한다.

        claim 구조와 TTL은 GitHub의 "Authenticating as a GitHub App"
        레퍼런스 예제를 그대로 따른다(추측 아님, 모듈 docstring 참고).
        `iat` backdate와 `exp` 상한(10분) 모두 주입된 clock 기준이라
        테스트가 claim 값을 결정론적으로 단언할 수 있다.
        """
        issued_at = int(now) - _APP_JWT_CLOCK_DRIFT_BACKDATE_SECONDS
        # `exp`는 `now`가 아니라 `iat` 기준 — JWT 유효 구간이 정확히
        # `_APP_JWT_TTL_SECONDS`가 되고 GitHub의 "10분 이내" 상한에
        # 여유 있게 들어간다.
        #
        # `int(now) + _APP_JWT_TTL_SECONDS`는 그 상한을 경계값에서만
        # 만족시키고 구간을 660초(backdate+TTL)로 늘린다. GitHub
        # 레퍼런스 예제 자체가 서로 다르다 — Ruby는 backdate된 `iat` +
        # `now + 600`, Python은 `iat = now` + `now + 600`이고 구간이
        # 600초로 유지되는 건 후자뿐. 이 코드는 둘 다 만족시킨다 —
        # 실패 모드가 실제 GitHub 대상으로만 드러나는 JWT 거부(우리
        # clock이 GitHub보다 살짝 빠르기만 해도 발생)라 중요하다.
        expires_at = issued_at + _APP_JWT_TTL_SECONDS
        payload = {"iat": issued_at, "exp": expires_at, "iss": self._app_id}
        return jwt.encode(payload, self._private_key, algorithm=_APP_JWT_ALGORITHM)

    async def _issue_installation_token(self) -> tuple[str, float]:
        """`RES-API-001`을 호출해 `(token, expires_at_epoch_seconds)`를 반환한다.

        모든 실패 모드(네트워크 에러, non-2xx, 파싱 불가 바디, 필드
        누락/오형식)에서 `InstallationTokenError`를 던진다 — `httpx2`
        예외나 정규화 안 된 GitHub 에러가 이 모듈 밖으로 새어나가지
        않는다.
        """
        app_jwt = self._build_app_jwt(self._clock())
        url = f"{_GITHUB_API_BASE_URL}/app/installations/{self._installation_id}/access_tokens"

        try:
            response = await self._http_client.post(
                url,
                headers={
                    "Authorization": f"Bearer {app_jwt}",
                    "Accept": _GITHUB_ACCEPT_HEADER,
                    "X-GitHub-Api-Version": _GITHUB_API_VERSION_HEADER,
                },
            )
        except httpx2.HTTPError as exc:
            # 의도적으로 str(exc)를 그대로 쓰지 않음 — httpx2 transport
            # 예외의 repr엔 요청 URL/메서드(안전)는 담길 수 있지만 방금
            # 보낸 헤더는 절대 아니어야 한다. 향후 httpx2 버전이 예외
            # 문자열에 뭘 담든 이 메시지는 직접 구성해 그 보장을 지킨다.
            raise InstallationTokenError(
                f"GitHub installation token request failed before a response was received "
                f"({type(exc).__name__})"
            ) from exc

        if response.is_error:
            snippet = response.text[:_ERROR_BODY_SNIPPET_LEN]
            raise InstallationTokenError(
                f"GitHub installation token request failed: HTTP {response.status_code} {snippet!r}"
            )

        try:
            raw_payload = response.json()
        except ValueError as exc:
            raise InstallationTokenError(
                "GitHub installation token response was not valid JSON"
            ) from exc

        if not isinstance(raw_payload, dict):
            raise InstallationTokenError("GitHub installation token response was not a JSON object")
        # `response.json()`은 `Any` 타입 — isinstance는 pyright strict에서
        # `Any`를 `dict[str, Any]`가 아니라 `Unknown`으로 좁히므로
        # (`config._load_json_object`와 같은 workaround) 아래
        # `.get(...)`이 실제로 타입 정보를 갖게 하려면 이 cast가
        # 필요하다.
        payload = cast(dict[str, Any], raw_payload)

        token = payload.get("token")
        expires_at_raw = payload.get("expires_at")
        if not isinstance(token, str) or not token:
            raise InstallationTokenError(
                "GitHub installation token response is missing a 'token' string"
            )
        if not isinstance(expires_at_raw, str) or not expires_at_raw:
            raise InstallationTokenError(
                "GitHub installation token response is missing an 'expires_at' string"
            )

        try:
            expires_at = datetime.fromisoformat(expires_at_raw.replace("Z", "+00:00")).timestamp()
        except ValueError as exc:
            raise InstallationTokenError(
                "GitHub installation token response has an unparsable 'expires_at'"
            ) from exc

        return token, expires_at
