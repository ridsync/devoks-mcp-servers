"""응답 길이 정책 — 순수 함수(TASK-009).

`CTR-SB-005`: Slack에 게시하는 응답은 `max_chars`(char 단위, 유효 범위
1..40000, 기본 **3,500** — `config.MAX_RESPONSE_CHARS_DEFAULT`. 이 모듈의
기본 파라미터 값도 이걸 그대로 참조해 둘이 드리프트할 수 없다)를 절대
넘지 않아야 한다. `AC-SB-006-2`/`EDGE-SB-010`: 상한을 넘으면 절단 사실을
반드시 알려야 한다 — Slack 자체 상한은 40,000자라, 여기 맡기면 답변이
잘렸다는 사실을 호출자가 알 방법이 없다.

`slack/signature.py`(TASK-003), `slack/events.py`(TASK-004)와 마찬가지로
이 모듈은 순수 string-in/string-out 함수다: HTTP/ASGI/SDK import 없음,
아래 stdlib 전용 상수 2개를 빼면 config/settings import도 없음, 절대
예외를 던지지 않는다. `answer`가 `None`/비-`str`이면 예외 대신 빈 문자열로
degrade하고, 계약 밖 `max_chars`(<= 0)도 예외나 무한루프 대신 빈 문자열로
degrade한다.

이 모듈이 막는 4가지 정확성 함정(각각의 처리는
`apply_response_length_policy`와 하위 헬퍼의 docstring 참고):

1. **알림 문구 자체도 예산을 쓴다.** `max_chars`로 정확히 자른 뒤 알림을
   붙이면 결과가 `max_chars`를 넘는다. 알림 길이는 내용을 슬라이싱하기
   *전에* 미리 예약한다.
2. **char 단위지만 단순 코드포인트 절단도 위험하다.** 절단점이 ZWJ 이모지
   시퀀스 내부, variation selector 직후, combining mark 직후에 걸리면
   글리프가 반쪽만 남는다.
3. **닫히지 않은 코드펜스가 이후 전체를 삼킨다.** 잘린 내용에 ` ``` `
   마커가 홀수 개면 Slack이 이후 모든 줄(우리 알림 포함)을 하나의
   코드블록으로 렌더링한다.
4. **`max_chars`가 알림 문구보다 작을 수 있다.** `CTR-SB-005`의 하한은
   1이므로 (degenerate하지만) 유효한 입력이다.
"""

from __future__ import annotations

import unicodedata
from typing import Final

from devoks_slackbot.config import MAX_RESPONSE_CHARS_DEFAULT

#: EDGE-SB-010 / AC-SB-006-2: 절단된 답변 뒤에 붙는, 사용자가 보게 될
#: 알림 문구. 특정 숫자를 박아넣은 f-string이 아니라 평범한 모듈 상수로
#: 둔 이유는 TASK-010/TASK-014가 그대로 import해 재사용하거나 테스트에서
#: monkeypatch로 override할 수 있게 하기 위함 — `config.py`만 소유하는
#: 실제 상한값을 이 모듈이 하드코딩하지 않는다.
TRUNCATION_NOTICE: Final[str] = (
    "\n\n_(응답이 길어 Slack 게시 상한을 넘겨 이후 내용은 생략되었습니다.)_"
)

#: combining mark의 Unicode 일반 카테고리(nonspacing, spacing combining,
#: enclosing) — 예: 단독 combining acute accent(U+0301).
_COMBINING_CATEGORIES: Final = frozenset({"Mn", "Mc", "Me"})

#: 제로폭 조인자 — 인접한 이모지를 하나의 시각적 글리프로 묶는다(예:
#: family 이모지는 base+ZWJ+base+ZWJ+base+ZWJ+base). 카테고리는 "Cf"로
#: mark가 아니라서 별도 체크가 필요하다.
_ZERO_WIDTH_JOINER: Final = "‍"

#: 이모지/텍스트 variation selector(U+FE0F 등, 앞 문자의 이모지 스타일
#: 렌더링을 선택)와 그 supplementary-plane 블록. 대부분 이미 카테고리
#: "Mn"에 속하지만(실측 확인은 이 모듈 테스트 스위트 참고), Unicode
#: 버전에 따라 달라질 수 있는 사실에 의존하지 않도록 범위를 명시했다.
_VARIATION_SELECTOR_RANGES: Final = ((0xFE00, 0xFE0F), (0xE0100, 0xE01EF))

#: Fitzpatrick 피부톤 수정자(카테고리 "Sk", mark *아님*) — 앞 이모지
#: base에 붙는다(예: 엄지척 이모지 + 톤 수정자).
_SKIN_TONE_MODIFIER_RANGE: Final = (0x1F3FB, 0x1F3FF)

# regional indicator는 쌍으로 동작한다: 국기 하나는 정확히 2개
# (🇰 + 🇷 = 🇰🇷). 카테고리상 "combining"이 아니라 위 risk 체크로는
# 잡히지 않는다 — 쌍을 쪼개면 남은 하나가 박스 문자로 렌더링되어, 여기서
# 유일하게 "다르게"가 아니라 "눈에 띄게 잘못" 보이는 경우다.
_REGIONAL_INDICATOR_RANGE: Final = (0x1F1E6, 0x1F1FF)

#: EDGE-SB-010 코드펜스 가드: Slack은(GitHub 마크다운처럼) ``` 로
#: 코드블록을 열고 다음 ``` 에서만 렌더링을 닫는다. 개수가 홀수면 절단으로
#: 펜스가 열린 채 남았다는 뜻이다.
_CODE_FENCE: Final = "```"
_CODE_FENCE_CLOSER: Final = "\n" + _CODE_FENCE


def apply_response_length_policy(
    answer: object, max_chars: int = MAX_RESPONSE_CHARS_DEFAULT
) -> str:
    """`answer`가 `max_chars` 안에 들어오면 그대로, 아니면 잘라 알림을 붙여 반환한다.

    `max_chars` 기본값은 `config.MAX_RESPONSE_CHARS_DEFAULT`
    (`CTR-SB-005` = 3,500) — 검증된 `Settings.max_response_chars`를 가진
    호출부는 그 값을 명시적으로 넘겨야 한다. 이 기본값은 계약 자체의
    baseline만 신경 쓰는 호출부(와 테스트)를 위해 존재한다.

    동작 순서:

    - `answer`가 `None`이거나 `str`이 아니면 -> `""`(방어적: 호출부
      실수에도 절대 예외를 던지지 않는다 — 게시할 만한 것이 애초에 없음).
    - `len(answer) <= max_chars` -> `answer` 그대로, **알림 없음** — 잘리지
      않은 답변에 "잘렸다"는 거짓 표시를 붙이면 안 된다(`CTR-SB-005`).
    - `max_chars <= 0` -> `""`. `CTR-SB-005`의 유효 범위(1..40000) 밖이고
      (`config.py`가 설정 로드 시점에 이미 거부하지만) 이 함수는 여전히
      예외를 던지거나 무한루프에 빠지면 안 되므로, 담을 수 있는 게 아무것도
      없다는 뜻으로 처리한다.
    - 그 외엔 절단 후 알림을 붙인다(`AC-SB-006-2`, `EDGE-SB-010`) — 단
      `max_chars`가 알림조차 담지 못할 만큼 작으면(위 함정 #4) 알림을
      생략하고 grapheme 경계 백오프(함정 #2)만 적용해 `max_chars`까지
      하드 절단한다. 그 분기엔 코드펜스 처리(함정 #3)가 없다 — 쓸 예산이
      없기 때문.

    **grapheme 경계 백오프가 다루는 범위와 다루지 않는 범위**(함정 #2):
    char 예산으로 슬라이싱한 뒤, 절단점을 뒤로 물려 trailing combining
    mark(Unicode 카테고리 Mn/Mc/Me), zero-width joiner, variation
    selector, skin-tone modifier 바로 뒤에 걸리지 않게 한다
    (`_back_off_combining_boundary` 참고). 완전한 Unicode
    grapheme-cluster segmentation은 아니다(stdlib에 없음) — 특히 분해형
    (NFD) 한글 음절의 자모는 mark가 아니라 일반 문자(카테고리 "Lo")라
    여기서 별도로 가드되지 않는다. 실무에서는 이 모듈의 입력(Claude 답변,
    Slack 메시지)이 완성형(NFC) 텍스트라 문제되지 않는다 — 음절 하나가
    이미 코드포인트 하나라 일반적인 코드포인트 인덱스 슬라이싱으로는
    쪼개지지 않는다. regional-indicator 국기 조합(예: 🇰🇷)은 가드된다 —
    쪼개면 남은 하나가 박스 문자로 렌더링된다.
    """
    text = answer if isinstance(answer, str) else ""
    if not text:
        return text
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text

    if len(TRUNCATION_NOTICE) >= max_chars:
        return _back_off_combining_boundary(text[:max_chars], text)

    content_budget = max_chars - len(TRUNCATION_NOTICE)
    content = _back_off_combining_boundary(text[:content_budget], text)
    content = _close_unbalanced_fence(content, content_budget)
    return content + TRUNCATION_NOTICE


def _is_combining_boundary_risk(char: str) -> bool:
    if char == _ZERO_WIDTH_JOINER:
        return True
    codepoint = ord(char)
    if _SKIN_TONE_MODIFIER_RANGE[0] <= codepoint <= _SKIN_TONE_MODIFIER_RANGE[1]:
        return True
    for low, high in _VARIATION_SELECTOR_RANGES:
        if low <= codepoint <= high:
            return True
    return unicodedata.category(char) in _COMBINING_CATEGORIES


def _is_regional_indicator(char: str) -> bool:
    return _REGIONAL_INDICATOR_RANGE[0] <= ord(char) <= _REGIONAL_INDICATOR_RANGE[1]


def _trailing_regional_indicators(text: str, end: int) -> int:
    count = 0
    while count < end and _is_regional_indicator(text[end - 1 - count]):
        count += 1
    return count


def _back_off_combining_boundary(text: str, source: str = "") -> str:
    """절단점이 grapheme cluster 내부에 걸리지 않을 때까지 뒤로 물린다.

    세 조건이 경계를 왼쪽으로 옮긴다(항상 1문자만 제거, 추가는 없음 —
    호출부의 길이 예산을 그대로 유지):

    1. 남는 텍스트가 **joiner**로 끝나면 안 된다. ZWJ는 뒤에 이어질
       요소를 기대하는데, 매달린 채로 두면 이을 대상이 없다. combining
       mark/variation selector/skin-tone modifier는 반대 경우다 — 이들은
       자신이 끝내는 cluster를 *완성*하므로, 잘라내면 멀쩡한 글리프가
       손상된다(규칙 2와 결합하면 경계가 0까지 밀릴 수도 있다).
    2. **버려지는** 문자가 남는 텍스트에 들러붙는 것이면 안 된다. 거기서
       자르면 cluster가 쪼개진다: `👨‍👩‍👧‍👦`가 세 명짜리 가족이 되고,
       `👍🏽`가 피부톤을 잃고, `é`가 `e`가 된다. 각각 유효한 글리프지만
       원문 작성자가 쓴 것과는 다르다. 이게 주 규칙이고, 규칙 1은 이
       규칙이 보지 못하는 "매달린 joiner" 케이스만 보완한다.
    3. regional indicator는 홀로 남으면 안 된다. `source`는 절단이 일어난
       원문이다 — 이게 없으면 규칙 1만 체크할 수 있어서, 모든 호출부가
       이를 넘긴다.

    완전한 Unicode grapheme segmentation은 stdlib에 없으므로 이건 완전한
    방어가 아니라 표적 방어다. 분해형(NFD) 한글 자모는 일반 문자(카테고리
    "Lo")라 가드되지 않은 채 남지만, 완성형(NFC) 텍스트에서는 음절 하나가
    코드포인트 하나라 코드포인트 슬라이싱으로 쪼개질 수 없으므로 이
    모듈의 입력에는 실질적 문제가 없다. 위험 문자로만 이뤄진 입력은
    예외 대신 `""`로 back off한다.
    """
    end = len(text)
    while end > 0:
        if text[end - 1] == _ZERO_WIDTH_JOINER:
            end -= 1
            continue
        if end < len(source) and _is_combining_boundary_risk(source[end]):
            end -= 1
            continue
        if (
            end < len(source)
            and _is_regional_indicator(source[end])
            and _trailing_regional_indicators(text, end) % 2 == 1
        ):
            end -= 1
            continue
        break
    return text[:end]


def _close_unbalanced_fence(content: str, budget: int) -> str:
    """절단으로 열린 채 남은 코드펜스를 `budget` chars 안에서 닫는다(함정 #3).

    `content`는 이미 `budget` 이하다. ``` 마커가 홀수 개 있으면 닫는
    펜스를 붙인다 — 그대로는 안 들어가면 `content`를 더 줄여(새 절단점에
    grapheme 백오프를 재적용) 공간을 만든다. `budget` 자체가 닫는
    펜스보다 작으면 예산을 넘기는 대신 펜스를 열린 채로 남겨둔다 — 실제
    답변이 열린 펜스를 담으려면 애초에 최소 수십 자는 필요하므로, 이는
    이미 degenerate한 작은 `max_chars` 구간(`CTR-SB-005` 범위의 끝단)에서만
    발생한다.
    """
    if content.count(_CODE_FENCE) % 2 == 0:
        return content
    closer_len = len(_CODE_FENCE_CLOSER)
    if len(content) + closer_len <= budget:
        return content + _CODE_FENCE_CLOSER
    if closer_len > budget:
        return content
    shrunk = _back_off_combining_boundary(content[: budget - closer_len], content)
    return shrunk + _CODE_FENCE_CLOSER
