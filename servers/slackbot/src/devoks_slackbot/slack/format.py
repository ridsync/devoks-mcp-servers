"""Response length policy — pure function (TASK-009).

``CTR-SB-005``: the response posted to Slack must never exceed
``max_chars`` (chars, valid range 1..40000, default **3,500** —
``config.MAX_RESPONSE_CHARS_DEFAULT``, the value this module's default
parameter is bound to so the two can never drift). ``AC-SB-006-2``/
``EDGE-SB-010``: when the answer exceeds that limit, the truncation must be
announced, not silent — Slack itself only cuts at 40,000 chars, so leaving
it to Slack means the caller never learns their answer was cut.

Like ``slack/signature.py`` (TASK-003) and ``slack/events.py`` (TASK-004),
this module is a plain string-in/string-out pure function: no HTTP/ASGI/SDK
import, no config/settings import beyond the two stdlib-only constants below,
never raises. A ``None``/non-``str`` ``answer`` degrades to an empty result
rather than an exception; an out-of-contract ``max_chars`` (<= 0) also
degrades to an empty result rather than raising or looping forever.

Four correctness traps this module exists to close (see the docstring of
``apply_response_length_policy`` and its private helpers for how each is
handled):

1. **The notice itself costs budget.** Truncating at exactly ``max_chars``
   and then appending a notice would push the result past ``max_chars``.
   The notice's length is reserved *before* slicing the content.
2. **Chars, not bytes — and not naive codepoint slicing either.** A cut can
   land inside a ZWJ emoji sequence, right after a variation selector, or
   right after a combining mark, leaving a dangling partial glyph.
3. **An unclosed code fence swallows everything after it.** If the sliced
   content contains an odd number of ` ``` ` markers, Slack renders every
   following line — including our own notice — as one code block.
4. **``max_chars`` can be smaller than the notice.** ``CTR-SB-005``'s range
   starts at 1, which is a valid (if degenerate) input.
"""

from __future__ import annotations

import unicodedata
from typing import Final

from devoks_slackbot.config import MAX_RESPONSE_CHARS_DEFAULT

#: EDGE-SB-010 / AC-SB-006-2: what the user sees appended to a truncated
#: answer. Deliberately a plain module constant (not an f-string baked with
#: a specific number) so TASK-010/TASK-014 can import and reuse — or
#: override via monkeypatch in tests — without this module hardcoding an
#: actual limit that only ``config.py`` owns.
TRUNCATION_NOTICE: Final[str] = (
    "\n\n_(응답이 길어 Slack 게시 상한을 넘겨 이후 내용은 생략되었습니다.)_"
)

#: Unicode general categories for combining marks (nonspacing, spacing
#: combining, enclosing) — e.g. a bare combining acute accent (U+0301).
_COMBINING_CATEGORIES: Final = frozenset({"Mn", "Mc", "Me"})

#: Zero-width joiner — glues adjacent emoji into one visual glyph (e.g. the
#: family emoji is base+ZWJ+base+ZWJ+base+ZWJ+base). Category "Cf", not a
#: mark, so it needs its own check.
_ZERO_WIDTH_JOINER: Final = "‍"

#: Emoji/text variation selectors (U+FE0F etc. select emoji-style rendering
#: for the preceding base character) and their supplementary-plane block.
#: Most of these already fall in "Mn" (see module test suite for the
#: verified category), but the explicit range makes the intent legible
#: without relying on that Unicode-version-specific fact.
_VARIATION_SELECTOR_RANGES: Final = ((0xFE00, 0xFE0F), (0xE0100, 0xE01EF))

#: Fitzpatrick skin-tone modifiers (category "Sk", *not* a mark) — attach to
#: the preceding emoji base, e.g. a thumbs-up + a tone modifier.
_SKIN_TONE_MODIFIER_RANGE: Final = (0x1F3FB, 0x1F3FF)

# Regional indicators pair up: a flag is exactly two of them (🇰 + 🇷 = 🇰🇷).
# They are not "combining" by category, so the risk test above does not catch
# them — split a pair and the leftover renders as a boxed letter, which is the
# one case here that looks visibly wrong rather than merely different.
_REGIONAL_INDICATOR_RANGE: Final = (0x1F1E6, 0x1F1FF)

#: EDGE-SB-010 code-fence guard: Slack (like GitHub-flavored Markdown) opens
#: a code block on ``` and only closes rendering on the next ```. An odd
#: count means the fence was left open by the cut.
_CODE_FENCE: Final = "```"
_CODE_FENCE_CLOSER: Final = "\n" + _CODE_FENCE


def apply_response_length_policy(
    answer: object, max_chars: int = MAX_RESPONSE_CHARS_DEFAULT
) -> str:
    """Return ``answer`` as-is if it fits in ``max_chars``, else truncate + announce it.

    ``max_chars`` defaults to ``config.MAX_RESPONSE_CHARS_DEFAULT``
    (``CTR-SB-005`` = 3,500) — callers with a validated ``Settings.max_response_chars``
    should pass it explicitly; this default exists for callers (and tests)
    that only care about the contract's own baseline.

    Behavior, in order:

    - ``answer`` is ``None`` or not a ``str`` -> ``""`` (defensive: never
      raises on a caller mistake; nothing sensible to post).
    - ``len(answer) <= max_chars`` -> ``answer`` unchanged, **no notice** —
      an unmodified answer under the limit must not carry a false "this was
      cut" claim (``CTR-SB-005``).
    - ``max_chars <= 0`` -> ``""``. Outside ``CTR-SB-005``'s valid range
      (1..40000; ``config.py`` rejects this at settings load time), but this
      function still must not raise or loop, so nothing can fit.
    - Otherwise, truncated with the notice appended (``AC-SB-006-2``,
      ``EDGE-SB-010``), unless ``max_chars`` is too small to even hold the
      notice (trap #4 above) — in which case the notice is omitted and the
      answer is hard-truncated to ``max_chars`` with only the grapheme-
      boundary back-off (trap #2) applied; no code-fence handling in that
      branch (trap #3), since there is no budget to spend on it.

    **What the grapheme-boundary back-off does and does not cover** (trap
    #2): after slicing to a char budget, this function walks the cut point
    backward past any trailing combining mark (Unicode category Mn/Mc/Me),
    zero-width joiner, variation selector, or skin-tone modifier, so the cut
    never lands immediately after one of those — see
    ``_back_off_combining_boundary``. It does **not** implement full
    Unicode grapheme-cluster segmentation (not available in the stdlib);
    notably, a *decomposed* (NFD) Hangul syllable's jamo are plain letters
    (category "Lo"), not marks, so a cut between them is not specifically
    guarded here. In practice this is a non-issue for this module's inputs:
    Claude's answers and Slack messages are precomposed (NFC) text, where
    one Hangul syllable is already exactly one Python codepoint, so ordinary
    codepoint-index slicing cannot split it. Regional-indicator flag pairs
    (e.g. 🇰🇷) *are* guarded: splitting one leaves a lone indicator that
    renders as a boxed letter.
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
    """Pull the cut point back until it does not fall inside a grapheme cluster.

    Three conditions move the boundary left, each removing one character
    (never adding), so the caller's length budget is preserved:

    1. The kept text must not end on a **joiner**. A ZWJ expects an element
       after it; left dangling it has nothing to join. Combining marks,
       variation selectors and skin-tone modifiers are the opposite case —
       they *complete* the cluster they end, and trimming them would strip a
       correct glyph (and, combined with rule 2, walk the boundary to zero).
    2. The character being **dropped** must not attach to what is kept.
       Cutting there splits a cluster: ``👨‍👩‍👧‍👦`` becomes a three-person
       family, ``👍🏽`` loses its skin tone, ``é`` becomes ``e``. Each is a
       valid glyph but not the one the author wrote. This is the main rule;
       rule 1 only covers the dangling-joiner case it cannot see.
    3. Regional indicators must not be left unpaired. ``source`` is the text
       the cut came from; without it only rule 1 can be checked, which is why
       every call site passes it.

    Full Unicode grapheme segmentation is not in the stdlib, so this is a
    targeted defence rather than a complete one. Decomposed (NFD) Hangul
    jamo are plain letters (category "Lo") and stay unguarded — a non-issue
    for precomposed (NFC) text, where one syllable is one codepoint and
    codepoint slicing cannot split it. An input made entirely of at-risk
    characters backs off to ``""`` rather than raising.
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
    """Balance a code fence left open by truncation, within ``budget`` chars (trap #3).

    ``content`` is already <= ``budget`` chars. If it contains an odd number
    of ``` markers, appends a closing fence — shrinking ``content`` further
    (and re-applying the grapheme back-off to the new cut point) if the
    closer would not otherwise fit. If ``budget`` itself is smaller than the
    closer, the fence is left unbalanced rather than exceeding the budget —
    a real answer needs dozens of chars to contain an open fence in the
    first place, so this only bites the already-degenerate small-``max_chars``
    end of ``CTR-SB-005``'s range.
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
