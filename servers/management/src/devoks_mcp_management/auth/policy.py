"""인가 정책(DSN-002).

순수 함수만 존재 — I/O·네트워크·시계·로깅·전역 상태 없음. 인가 버그는 보안
사고이므로 GitHub 호출 없이 role x tool x repo 모든 조합을 전수 검증할 수
있는, 가장 테스트하기 쉬운 형태로 유지한다(FRD §4.3 DSN-002).

`authorize`는 예외 대신 값을 반환한다 — 호출부(`tools/guard.py`, TASK-007)가
결과를 클라이언트 응답과 감사 로그 두 용도로 써야 하는데, 예외로 던지면
"허용" 경로까지 try/except로 감싸야 하기 때문이다.

거부 사유는 두 계층으로 의도적으로 분리한다(AC-003-5):

  - `client_message` — 클라이언트에게 보여주는 고정 문자열. role 미상, tool
    미허용, allowlist 밖 repo, 빈 allowlist 등 사유와 무관하게 항상 같은
    문자열 — 호출부가 거부 사유를 구분할 수 있으면 allowlist 구성을 역으로
    캐낼 수 있기 때문.
  - `reason_code` — 감사 로그 전용 운영자용 코드. 클라이언트에는 절대
    전송하지 않는다. 클라이언트 응답을 약화하지 않고도 운영자가 거부
    사유를 디버깅할 수 있게 해준다.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

#: AC-003-5: 클라이언트에 노출되는 유일한 거부 메시지. 이 모듈의 모든 거부
#: 분기가 이 문자열을 그대로 반환한다 — 요청 repo·allowlist 내용·실패한
#: 체크 종류에서 파생시키지 않는다.
_CLIENT_DENIAL_MESSAGE = "Not authorized to perform this request."

#: `authorize`가 거부한 이유의 감사 전용 분류. 클라이언트에는 절대
#: 노출되지 않는다(모듈 docstring 참고).
ReasonCode = Literal["role_unknown", "tool_not_permitted", "repo_not_allowlisted"]


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    """`authorize` 호출 1회의 결과.

    `allowed`가 `True`면 `client_message`/`reason_code`는 둘 다 `None` —
    호출부는 `allowed`가 `False`로 확인된 뒤에만 거부 필드를 읽어야 한다.
    """

    allowed: bool
    client_message: str | None
    reason_code: ReasonCode | None


def authorize(
    role: str,
    tool: str,
    repo: str | None = None,
    *,
    role_tools: Mapping[str, frozenset[str]],
    repo_allowlist: frozenset[str],
) -> AuthorizationDecision:
    """`role`이 `tool`을(선택적으로 `repo`를 대상으로) 실행할 수 있는지 판정한다.

    `role_tools`/`repo_allowlist`는 `config`에서 직접 읽지 않고 인자로
    받는다 — 이 함수를 순수하게 유지하기 위함이며, `Settings`에서 한 번
    가져오는 일은 호출부(`tools/guard.py`)의 몫이다.

    `repo`는 선택적이다 — 단일 repo를 대상으로 하지 않는 tool(`list_repos`,
    `search_code`의 전역 검색)은 `None`을 넘기고 repo 체크를 건너뛴다. 이
    경우 결과를 allowlist로 필터링하는 건 아래 `is_repo_allowlisted`/
    `list_allowlisted_repos`를 쓰는 호출부 몫이다.

    순서: role -> tool 권한(AC-003-1, AC-003-2)을 먼저, repo 체크
    (AC-003-3, AC-003-4)를 나중에 — role/tool에서 거부되면 요청한 repo가
    repo 체크를 통과했을지는 절대 드러나지 않는다.
    """
    allowed_tools = role_tools.get(role)
    if allowed_tools is None:
        return _deny("role_unknown")
    if tool not in allowed_tools:
        return _deny("tool_not_permitted")
    if repo is not None and not is_repo_allowlisted(repo, repo_allowlist):
        return _deny("repo_not_allowlisted")
    return AuthorizationDecision(allowed=True, client_message=None, reason_code=None)


def is_repo_allowlisted(repo: str, repo_allowlist: frozenset[str]) -> bool:
    """CTR-008: 'owner/repo' 완전일치, 와일드카드 없음.

    의도적으로 대소문자를 구분한다. CTR-008은 이를 "완전일치"라 부른다 —
    대소문자 무시 비교는 명시적으로 구성된 것보다 더 많은 문자열을 허용하게
    되어, 이 모듈 전반의 fail-safe 방향(role 미상, tool 미허용, 빈
    allowlist 모두 거부)에 어긋난다. 대소문자 구분 비교는 대소문자 무시
    비교보다 항상 더 많이 *거부*할 뿐 더 많이 허용하지 않으므로, 드문
    대소문자 변형 요청은 우회가 아니라 거부로 처리된다. 대소문자 무시
    매칭이 필요하면 호출부가 스스로 입력을 정규화해야 한다 — 이 모듈이
    암묵적으로 정규화하지 않는다.

    앞뒤 공백만 제거하고 그 외에는 있는 그대로 비교한다.

    빈 `repo_allowlist`는 구조상 모든 repo를 거부한다 — 빈 집합에 대한
    멤버십은 항상 `False`이므로 별도 분기가 필요 없다(EDGE-001, AC-003-4).
    """
    return repo.strip() in repo_allowlist


def list_allowlisted_repos(repo_allowlist: frozenset[str]) -> tuple[str, ...]:
    """필터링용으로 구성된 allowlist를 나열한다.

    `list_repos`(AC-005-1)는 allowlist에 있는 repo만 반환해야 하며, 이
    접근자가 그 필터링 기준을 제공한다. 인가된 호출부가 이 접근자로
    allowlist를 읽는 것과, 거부 응답 안에 allowlist를 흘리는 것(위
    `AuthorizationDecision.client_message`)은 별개의 문제다.
    """
    return tuple(sorted(repo_allowlist))


def filter_allowlisted(repos: Iterable[str], repo_allowlist: frozenset[str]) -> list[str]:
    """`repos` 중 `repo_allowlist`에 있는 항목만 남긴다.

    repo 후보 목록(예: GitHub API 결과)을 한 번에 필터링하는 호출부를 위한
    `is_repo_allowlisted` 편의 래퍼.
    """
    return [repo for repo in repos if is_repo_allowlisted(repo, repo_allowlist)]


def _deny(reason_code: ReasonCode) -> AuthorizationDecision:
    return AuthorizationDecision(
        allowed=False, client_message=_CLIENT_DENIAL_MESSAGE, reason_code=reason_code
    )
